"""
Image & vector-object editing (feature slug: ``objects``).

Everything here edits the PDF itself (content streams / XObjects), never
annotations, so results survive any viewer, printing and flattening.

Coordinate system
-----------------
All rects/points in requests and responses are **PyMuPDF page coordinates**:
PDF points (1/72 in), origin at the TOP-LEFT of ``page.rect`` and y growing
downward. ``page_width`` / ``page_height`` are returned by the list endpoint so
clients can convert to rendered pixels with ``px = pt * rendered_width / page_width``.

How image placements are edited
-------------------------------
An image placement on a page is a ``/Name Do`` operator in the page content
stream, executed under some CTM. ``page.get_image_info()`` reports that
placement's transform ``T_f`` in PyMuPDF space, where ``T_f = F * T_pdf * P``
(``F`` flips the image unit square, ``P`` = ``page.transformation_matrix``).
To give the placement a new transform ``T_f'`` we wrap the single ``Do`` as
``q M cm /Name Do Q`` with ``M = F * T_f' * T_f^-1 * F``. This touches only that
one placement - surrounding text, vectors and other images are untouched and
z-order is preserved.

If an image is drawn from inside a Form XObject (not directly by the page
stream), moves/deletes edit the `Do` inside that form instead (same formula; the
response reports ``"method": "form"``) when the form is used by no other page or
placement and its BBox clip would not cut the new position off. Otherwise those
operations fall back to redaction-based removal (``images=PDF_REDACT_IMAGE_REMOVE``,
text and line-art untouched) plus re-insertion (``"method": "redact"``), which
puts the image on top.

How vector objects are edited
-----------------------------
``_scan_paths`` finds every painted path in the page stream (byte range, CTM,
geometry); ``_map_paths_to_segments`` aligns them with ``get_drawings()``. A
move wraps the existing operators as ``q X cm <path> Q`` (graphics-state ops
found inside the path are re-emitted after the ``Q``), a delete removes them -
so stacking order is preserved. Every in-place batch is VERIFIED by re-reading
the page (all image/path boxes must be exactly as expected); on any mismatch it
is rolled back and the legacy remove + redraw method is used.

``POST /objects/batch`` applies several move/delete/front/back/duplicate ops as
one change (one undo snapshot); ``POST /objects/arrange`` brings one object to
the front or sends it to the back.
"""

from __future__ import annotations

import io
import math
import os
import re
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend.advanced_ops import snapshot

router = APIRouter(prefix="/api/pdf")

# Same default/env var as backend/main.py so we find the same documents.
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "uploads"))

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

BBOX_TOLERANCE = 1.5  # points; stale-selection check
GROUP_TOLERANCE = 2.0  # points; vector paths closer than this form one object
BIG_PATH_FRACTION = 0.6  # paths covering > 60% of the page never merge (backgrounds)
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

_FLIP = fitz.Matrix(1, 0, 0, -1, 0, 1)  # unit-square flip (self-inverse)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _doc_path(doc_id: str) -> Path:
    if not _UUID_RE.match(doc_id):
        raise HTTPException(status_code=400, detail="Invalid document ID")
    path = UPLOAD_DIR / doc_id / "original.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    return path


def _open(doc_id: str) -> tuple[fitz.Document, Path]:
    path = _doc_path(doc_id)
    return fitz.open(str(path)), path


def _page(doc: fitz.Document, page: int) -> fitz.Page:
    if page < 0 or page >= len(doc):
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid page number")
    return doc[page]


def _save(doc: fitz.Document, path: Path, garbage: int = 0):
    tmp = str(path) + ".objects.tmp"
    doc.save(tmp, garbage=garbage, deflate=True)
    doc.close()
    os.replace(tmp, str(path))


def _rect(v: list[float]) -> fitz.Rect:
    if len(v) != 4:
        raise HTTPException(status_code=400, detail="Rect must be [x0, y0, x1, y1]")
    r = fitz.Rect(v).normalize()
    if r.is_empty or r.width < 1 or r.height < 1:
        raise HTTPException(status_code=400, detail="Rect is empty or too small")
    return r


def _color(v: Optional[list[float]]) -> Optional[tuple]:
    if v is None:
        return None
    if len(v) != 3:
        raise HTTPException(status_code=400, detail="Colors must be [r, g, b] in 0..1")
    if any(c > 1 for c in v):  # accept 0..255 too
        v = [c / 255.0 for c in v]
    return tuple(max(0.0, min(1.0, float(c))) for c in v)


def _close_bbox(a, b, tol=BBOX_TOLERANCE) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _to_disp(page: fitz.Page, bbox) -> list[float]:
    """Unrotated page coords -> displayed (rotation-applied) coords."""
    r = (fitz.Rect(bbox) * page.rotation_matrix).normalize()
    return [round(v, 3) for v in (r.x0, r.y0, r.x1, r.y1)]


def _from_disp(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect:
    """Displayed coords -> unrotated page coords (what PyMuPDF draws/inserts in)."""
    return (fitz.Rect(rect) * page.derotation_matrix).normalize()


def _decode_name(raw: bytes) -> str:
    """Decode a PDF name token (without leading '/'), resolving #xx escapes."""
    return re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw).decode(
        "latin-1"
    )


# ─── Content-stream tokenizer (just enough to find `/Name Do` safely) ─────────

_WS = b" \t\r\n\x00\x0c"
_DELIM = b"()<>[]{}/%"


def _find_do_ops(data: bytes) -> list[tuple[int, int, str]]:
    """Return (start, end, name) for every `/Name Do` in a content stream.

    Skips string literals, hex strings, comments and inline-image binary data so
    that bytes that merely *look* like an operator are never matched.
    """
    out: list[tuple[int, int, str]] = []
    i, n = 0, len(data)
    prev: Optional[tuple[int, int, str]] = None  # last token if it was a name
    while i < n:
        c = data[i]
        if c in _WS:
            i += 1
            continue
        if c == 0x25:  # % comment
            while i < n and data[i] not in b"\r\n":
                i += 1
            prev = None
            continue
        if c == 0x28:  # ( string )
            depth, i = 1, i + 1
            while i < n and depth:
                ch = data[i]
                if ch == 0x5C:  # backslash escape
                    i += 2
                    continue
                if ch == 0x28:
                    depth += 1
                elif ch == 0x29:
                    depth -= 1
                i += 1
            prev = None
            continue
        if c == 0x3C:  # < hex string > or << dict
            if i + 1 < n and data[i + 1] == 0x3C:
                i += 2
            else:
                j = data.find(b">", i + 1)
                i = n if j < 0 else j + 1
            prev = None
            continue
        if c in b">[]{})":
            i += 1
            prev = None
            continue
        if c == 0x2F:  # /Name
            j = i + 1
            while j < n and data[j] not in _WS and data[j] not in _DELIM:
                j += 1
            prev = (i, j, _decode_name(data[i + 1 : j]))
            i = j
            continue
        # regular token (operator / number / keyword)
        j = i
        while j < n and data[j] not in _WS and data[j] not in _DELIM:
            j += 1
        tok = data[i:j]
        if tok == b"Do" and prev is not None:
            out.append((prev[0], j, prev[2]))
        elif tok == b"BI":
            # inline image: skip dictionary to ID, then binary data to EI
            k = data.find(b"ID", j)
            if k < 0:
                break
            k += 3
            m = re.compile(rb"[\s]EI(?=[\s/\[<(%]|$)").search(data, k)
            j = n if m is None else m.end()
        prev = None
        i = j
    return out


# ─── Generic lexer + path scanner (in-place, z-order preserving edits) ────────

_NUM_RE = re.compile(rb"^[+-]?(\d+\.?\d*|\.\d+)$")
_PAINT_OPS = {b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"n"}
_CONSTRUCT_OPS = {b"m", b"l", b"c", b"v", b"y", b"h", b"re"}
# graphics-state operators that are legal (or tolerated) between path operators
_STATE_OPS = {
    b"w", b"J", b"j", b"M", b"d", b"ri", b"i", b"gs", b"CS", b"cs", b"SC", b"SCN",
    b"sc", b"scn", b"G", b"g", b"RG", b"rg", b"K", b"k",
}


def _lex(data: bytes) -> list[tuple[str, int, int, bytes]]:
    """Tokenize a content stream into (kind, start, end, raw) tuples.

    kind: "num" | "name" | "op" | "other" (strings, arrays, dict delimiters) |
    "inline" (a whole BI ... EI inline image). Same skipping rules as
    ``_find_do_ops`` so operator look-alikes inside strings never match.
    """
    out: list[tuple[str, int, int, bytes]] = []
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        if c in _WS:
            i += 1
            continue
        if c == 0x25:
            while i < n and data[i] not in b"\r\n":
                i += 1
            continue
        if c == 0x28:
            s, depth, i = i, 1, i + 1
            while i < n and depth:
                ch = data[i]
                if ch == 0x5C:
                    i += 2
                    continue
                if ch == 0x28:
                    depth += 1
                elif ch == 0x29:
                    depth -= 1
                i += 1
            out.append(("other", s, i, b""))
            continue
        if c == 0x3C:
            s = i
            if i + 1 < n and data[i + 1] == 0x3C:
                i += 2
            else:
                j = data.find(b">", i + 1)
                i = n if j < 0 else j + 1
            out.append(("other", s, i, b""))
            continue
        if c in b">[]{})":
            out.append(("other", i, i + 1, b""))
            i += 1
            continue
        if c == 0x2F:
            j = i + 1
            while j < n and data[j] not in _WS and data[j] not in _DELIM:
                j += 1
            out.append(("name", i, j, data[i:j]))
            i = j
            continue
        j = i
        while j < n and data[j] not in _WS and data[j] not in _DELIM:
            j += 1
        if j == i:  # stray delimiter
            i += 1
            continue
        tok = data[i:j]
        if tok == b"BI":
            k = data.find(b"ID", j)
            if k < 0:
                out.append(("inline", i, n, b"BI"))
                break
            m = re.compile(rb"[\s]EI(?=[\s/\[<(%]|$)").search(data, k + 3)
            j = n if m is None else m.end()
            out.append(("inline", i, j, b"BI"))
        elif _NUM_RE.match(tok):
            out.append(("num", i, j, tok))
        else:
            out.append(("op", i, j, tok))
        i = j
    return out


def _q_balance(data: bytes) -> int:
    """Number of `q` left open at the end of a content stream (>= 0)."""
    depth = 0
    for kind, _, _, raw in _lex(data):
        if kind == "op":
            if raw == b"q":
                depth += 1
            elif raw == b"Q" and depth:
                depth -= 1
    return depth


def _scan_paths(data: bytes) -> list[dict]:
    """Every painted path in a content stream with its byte range and geometry.

    Each entry: start/end (bytes from the first operand of the first path
    operator through the painting operator), op (painting operator), ctm (CTM
    in effect, PDF space), points (path points in PDF default space), clip
    (W/W* seen), state (raw graphics-state operator snippets found *inside* the
    path, re-emitted after an in-place wrap so later content keeps them), bad
    (something we cannot safely wrap, e.g. a `cm` inside the path).
    """
    segs: list[dict] = []
    ctm = fitz.Matrix(1, 0, 0, 1, 0, 0)
    stack: list[fitz.Matrix] = []
    operands: list[tuple[str, int, int, bytes]] = []
    cur: Optional[dict] = None

    def nums(k: int) -> Optional[list[float]]:
        vals = [float(t[3]) for t in operands[-k:] if t[0] == "num"] if k else []
        return vals if len(vals) == k and len(operands) >= k else None

    for tok in _lex(data):
        kind, s, e, raw = tok
        if kind != "op":
            if kind == "inline":
                if cur is not None:
                    cur["bad"] = True
                operands = []
            else:
                operands.append(tok)
            continue
        op_start = operands[0][1] if operands else s
        if raw in _CONSTRUCT_OPS:
            if cur is None:
                cur = {"start": op_start, "ctm": fitz.Matrix(ctm), "points": [], "clip": False,
                       "state": [], "bad": False}
            pts: list[tuple[float, float]] = []
            if raw in (b"m", b"l"):
                v = nums(2)
                pts = [(v[0], v[1])] if v else []
            elif raw == b"c":
                v = nums(6)
                pts = [(v[0], v[1]), (v[2], v[3]), (v[4], v[5])] if v else []
            elif raw in (b"v", b"y"):
                v = nums(4)
                pts = [(v[0], v[1]), (v[2], v[3])] if v else []
            elif raw == b"re":
                v = nums(4)
                if v:
                    x, y, w, h = v
                    pts = [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]
            if raw != b"h" and not pts:
                cur["bad"] = True
            cm = cur["ctm"]
            cur["points"].extend(fitz.Point(px, py) * cm for px, py in pts)
        elif raw in (b"W", b"W*"):
            if cur is not None:
                cur["clip"] = True
        elif raw in _PAINT_OPS:
            if cur is not None:
                cur["end"] = e
                cur["op"] = raw.decode()
                segs.append(cur)
                cur = None
        elif raw in _STATE_OPS:
            if cur is not None:
                cur["state"].append(data[op_start:e])
        elif raw == b"q":
            stack.append(fitz.Matrix(ctm))
            if cur is not None:
                cur["bad"] = True
        elif raw == b"Q":
            if stack:
                ctm = stack.pop()
            if cur is not None:
                cur["bad"] = True
        elif raw == b"cm":
            v = nums(6)
            if v:
                ctm = fitz.Matrix(*v) * ctm
            if cur is not None:
                cur["bad"] = True
        elif cur is not None:
            cur["bad"] = True  # text / Do / anything else inside a path: not ours to touch
        operands = []
    return segs


def _seg_rect(seg: dict, page: fitz.Page) -> Optional[fitz.Rect]:
    pts = [p * page.transformation_matrix for p in seg["points"]]
    if not pts:
        return None
    return fitz.Rect(min(p.x for p in pts), min(p.y for p in pts), max(p.x for p in pts), max(p.y for p in pts))


def _rect_close(a, b, tol: float = 0.75) -> bool:
    return all(abs(float(x) - float(y)) <= tol for x, y in zip(tuple(a), tuple(b)))


def _map_paths_to_segments(page: fitz.Page, data: bytes) -> tuple[list[dict], dict[int, int]]:
    """Map get_drawings() seqno -> index into _scan_paths(page stream).

    Page-level paths appear in the same order in both lists, so a forward
    greedy alignment on geometry is exact; paths drawn from inside Form
    XObjects find no partner and stay unmapped (callers fall back).
    """
    segs = _scan_paths(data)
    cand = [i for i, sg in enumerate(segs) if sg["op"] != "n"]  # `W n` clips are not drawings
    mapping: dict[int, int] = {}
    j = 0
    for p in sorted(page.get_drawings(), key=lambda d: d["seqno"]):
        for k in range(j, len(cand)):
            r = _seg_rect(segs[cand[k]], page)
            if r is not None and _rect_close(r, p["rect"]):
                mapping[p["seqno"]] = cand[k]
                j = k + 1
                break
    return segs, mapping


def _wrap_segment(data: bytes, seg: dict, page: fitz.Page, m: Optional[fitz.Matrix]) -> bytes:
    """Replacement bytes for a path segment: transformed by m (PyMuPDF space)
    via a `cm` around the unchanged operators, or deleted when m is None.
    Graphics-state ops that lived inside the path are re-emitted afterwards."""
    tail = b"".join(b" " + st for st in seg["state"])
    if m is None:
        return tail + b" "
    p = page.transformation_matrix
    ctm = seg["ctm"]
    x = ctm * p * m * ~p * ~ctm
    body = data[seg["start"] : seg["end"]]
    return f"q {_mat_str(x)} cm ".encode() + body + b" Q" + tail + b" "


def _write_contents(doc: fitz.Document, page: fitz.Page, data: bytes):
    """Replace the page's content with a single, fresh (unshared) stream."""
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, data)
    doc.xref_set_key(page.xref, "Contents", f"{xref} 0 R")


def _mat_str(m: fitz.Matrix) -> str:
    return " ".join(f"{v:.6f}".rstrip("0").rstrip(".") or "0" for v in (m.a, m.b, m.c, m.d, m.e, m.f))


# ─── Image placement model ────────────────────────────────────────────────────


def _image_placements(page: fitz.Page) -> list[dict]:
    """All image placements on a page in content-stream order."""
    infos = page.get_image_info(xrefs=True)
    # one xref may be registered under several resource names (fzImg0, fzImg1...)
    direct_names: dict[int, set[str]] = {}
    for item in page.get_images(full=True):
        xref, name, referencer = item[0], item[7], item[9]
        if referencer == 0:
            direct_names.setdefault(xref, set()).add(name)

    do_ops = _find_do_ops(page.read_contents())
    do_count: dict[str, int] = {}
    for _, _, name in do_ops:
        do_count[name] = do_count.get(name, 0) + 1

    per_xref_total: dict[int, int] = {}
    for info in infos:
        per_xref_total[info["xref"]] = per_xref_total.get(info["xref"], 0) + 1

    seen: dict[int, int] = {}
    out = []
    for info in infos:
        xref = info["xref"]
        occ = seen.get(xref, 0)
        seen[xref] = occ + 1
        names = direct_names.get(xref, set())
        if xref == 0:
            method = "none"  # inline image: no xref to address
        elif names and sum(do_count.get(nm, 0) for nm in names) == per_xref_total[xref]:
            method = "stream"
        else:
            method = "redact"
        bbox = [round(v, 3) for v in info["bbox"]]
        out.append(
            {
                "id": f"img-{xref}-{occ}",
                "kind": "image",
                "xref": xref,
                "occurrence": occ,
                "bbox": bbox,
                "transform": list(info["transform"]),
                "width": info["width"],
                "height": info["height"],
                "colorspace": info.get("cs-name", ""),
                "bpc": info.get("bpc"),
                "size": info.get("size"),
                "has_mask": bool(info.get("has-mask")),
                "names": sorted(names),
                "editable": xref != 0,
                "method": method,
            }
        )
    return out


def _find_placement(page: fitz.Page, xref: int, occurrence: int, bbox: Optional[list[float]]) -> dict:
    for pl in _image_placements(page):
        if pl["xref"] == xref and pl["occurrence"] == occurrence:
            if bbox is not None and not _close_bbox(_to_disp(page, pl["bbox"]), bbox):
                raise HTTPException(
                    status_code=409, detail="Image has changed since it was selected; refresh"
                )
            if not pl["editable"]:
                raise HTTPException(status_code=400, detail="Inline images cannot be edited")
            return pl
    raise HTTPException(status_code=404, detail="Image placement not found on this page")


def _rewrite_placement(
    doc: fitz.Document,
    page: fitz.Page,
    pl: dict,
    *,
    new_transform: Optional[fitz.Matrix] = None,
    new_name: Optional[str] = None,
    delete: bool = False,
    drop_last_of: Optional[str] = None,
):
    """Edit exactly one direct `/Name Do` placement in the page content.

    new_transform: desired PyMuPDF-space image transform (T_f').
    new_name:      draw a different image resource at this placement.
    delete:        remove the placement.
    drop_last_of:  also remove the LAST `Do` of this resource name (used to undo
                   the placement that `insert_image` appends when we only want it
                   to register a new image resource).
    """
    data = page.read_contents()
    ops = _find_do_ops(data)
    target = [op for op in ops if op[2] in pl["names"]]
    if pl["occurrence"] >= len(target):
        raise HTTPException(status_code=409, detail="Placement not found in page content")
    start, end, cur_name = target[pl["occurrence"]]

    edits: list[tuple[int, int, bytes]] = []
    if delete:
        repl = b""
    else:
        name = new_name or cur_name
        do = f"/{name} Do".encode("latin-1")
        if new_transform is not None:
            t_old = fitz.Matrix(pl["transform"])
            if abs(t_old.a * t_old.d - t_old.b * t_old.c) < 1e-9:
                raise HTTPException(status_code=400, detail="Image transform is degenerate")
            m = _FLIP * new_transform * ~t_old * _FLIP
            repl = f"q {_mat_str(m)} cm ".encode() + do + b" Q"
        else:
            repl = do
    edits.append((start, end, repl))

    if drop_last_of:
        extra = [op for op in ops if op[2] == drop_last_of]
        if extra:
            s2, e2, _ = extra[-1]
            edits.append((s2, e2, b""))

    for s, e, r in sorted(edits, key=lambda t: t[0], reverse=True):
        data = data[:s] + r + data[e:]
    _write_contents(doc, page, data)


def _register_image(page: fitz.Page, stream: bytes) -> tuple[int, str]:
    """Add an image resource to the page; return (xref, resource name).

    insert_image also appends a placement; callers remove it via drop_last_of.
    """
    xref = page.insert_image(page.rect, stream=stream, keep_proportion=False)
    for item in page.get_images(full=True):
        if item[0] == xref and item[9] == 0:
            return xref, item[7]
    raise HTTPException(status_code=500, detail="Failed to register image")


def _redact_remove_image(page: fitz.Page, bbox: list[float]):
    """Fallback: remove images overlapping bbox; text and vector art survive."""
    page.add_redact_annot(fitz.Rect(bbox), fill=False)
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_REMOVE,
        graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        text=fitz.PDF_REDACT_TEXT_NONE,
    )


def _image_pixmap(doc: fitz.Document, xref: int) -> fitz.Pixmap:
    """Decoded image as an RGB(A) pixmap, soft mask applied as alpha."""
    pix = fitz.Pixmap(doc, xref)
    smask = doc.xref_get_key(xref, "SMask")
    if smask[0] == "xref":
        try:
            mask = fitz.Pixmap(doc, int(smask[1].split()[0]))
            if pix.alpha:
                pix = fitz.Pixmap(pix, 0)
            pix = fitz.Pixmap(pix, mask)
        except Exception:
            pass
    if pix.colorspace is not None and pix.colorspace.n not in (1, 3):
        pix = fitz.Pixmap(fitz.csRGB, pix)
    return pix


def _rect_map(src: fitz.Rect, dst: fitz.Rect) -> fitz.Matrix:
    """Matrix mapping rect src onto rect dst (scale + translate)."""
    sx = dst.width / src.width if src.width else 1
    sy = dst.height / src.height if src.height else 1
    return fitz.Matrix(1, 0, 0, 1, -src.x0, -src.y0) * fitz.Matrix(sx, 0, 0, sy, dst.x0, dst.y0)


def _rotation_about(center: fitz.Point, angle: float) -> fitz.Matrix:
    return (
        fitz.Matrix(1, 0, 0, 1, -center.x, -center.y)
        * fitz.Matrix(angle)
        * fitz.Matrix(1, 0, 0, 1, center.x, center.y)
    )


def _png_bytes(pix: fitz.Pixmap) -> bytes:
    return pix.tobytes("png")


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Image too large")
    try:
        fitz.Pixmap(data)  # validate decodable
    except Exception:
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(data))
            buf = io.BytesIO()
            img.save(buf, "PNG")
            data = buf.getvalue()
        except Exception:
            raise HTTPException(status_code=400, detail="Unsupported or corrupt image file")
    return data


# ─── Vector drawings model ────────────────────────────────────────────────────


def _union(a: tuple, b: tuple) -> tuple:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _near(a: tuple, b: tuple, tol: float = GROUP_TOLERANCE) -> bool:
    # explicit math: fitz.Rect treats zero-height lines as "empty" and ignores them
    return a[0] - tol <= b[2] and b[0] - tol <= a[2] and a[1] - tol <= b[3] and b[1] - tol <= a[3]


def _drawing_groups(page: fitz.Page) -> tuple[list[dict], list[list[dict]]]:
    paths = page.get_drawings()
    pr = page.rect
    page_area = max(pr.width * pr.height, 1)
    rects = [tuple(fitz.Rect(p["rect"]).normalize()) for p in paths]
    big = [((r[2] - r[0]) * (r[3] - r[1]) / page_area) > BIG_PATH_FRACTION for r in rects]

    groups: list[list[int]] = []
    gboxes: list[tuple] = []
    for i, r in enumerate(rects):
        if big[i]:
            groups.append([i])
            gboxes.append(r)
            continue
        groups.append([i])
        gboxes.append(r)
        keep = len(groups) - 1
        # merge with every group the (growing) box touches, until stable
        changed = True
        while changed:
            changed = False
            for g in range(len(groups) - 1, -1, -1):
                if g == keep or big[groups[g][0]]:
                    continue
                if _near(gboxes[g], gboxes[keep]):
                    groups[keep].extend(groups[g])
                    gboxes[keep] = _union(gboxes[keep], gboxes[g])
                    del groups[g]
                    del gboxes[g]
                    if g < keep:
                        keep -= 1
                    changed = True

    objs, members = [], []
    order = sorted(range(len(groups)), key=lambda g: min(paths[i]["seqno"] for i in groups[g]))
    for idx, g in enumerate(order):
        mem = sorted(groups[g], key=lambda i: paths[i]["seqno"])
        ps = [paths[i] for i in mem]
        bb = fitz.Rect(gboxes[g])
        first = ps[0]
        objs.append(
            {
                "id": f"drw-{idx}",
                "kind": "drawing",
                "index": idx,
                "bbox": [round(v, 3) for v in (bb.x0, bb.y0, bb.x1, bb.y1)],
                "path_count": len(ps),
                "seqnos": [p["seqno"] for p in ps],
                "stroke": list(first["color"]) if first.get("color") else None,
                "fill": list(first["fill"]) if first.get("fill") else None,
                "width": first.get("width"),
                "background": bool(big[mem[0]]) and len(ps) == 1,
            }
        )
        members.append(ps)
    return objs, members


def _find_group(page: fitz.Page, index: int, bbox: Optional[list[float]]) -> tuple[dict, list[dict]]:
    objs, members = _drawing_groups(page)
    if index < 0 or index >= len(objs):
        raise HTTPException(status_code=404, detail="Drawing object not found on this page")
    if bbox is not None and not _close_bbox(_to_disp(page, objs[index]["bbox"]), bbox):
        raise HTTPException(status_code=409, detail="Drawing has changed since it was selected; refresh")
    return objs[index], members[index]


def _remove_paths(page: fitz.Page, bbox: list[float], paths: list[dict]):
    """Remove vector paths fully inside the (inflated) group bbox. Text/images survive."""
    pad = max([(p.get("width") or 0) for p in paths] + [0]) / 2 + 1
    r = fitz.Rect(bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
    page.add_redact_annot(r, fill=False)
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_NONE,
        graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
        text=fitz.PDF_REDACT_TEXT_NONE,
    )


def _redraw_paths(page: fitz.Page, paths: list[dict], m: fitz.Matrix, overlay: bool = True):
    """Re-create vector paths transformed by m (PyMuPDF space) as real PDF paths."""
    scale = math.sqrt(abs(m.a * m.d - m.b * m.c)) or 1.0
    for p in paths:
        shape = page.new_shape()
        for item in p["items"]:
            op = item[0]
            if op == "l":
                shape.draw_line(fitz.Point(item[1]) * m, fitz.Point(item[2]) * m)
            elif op == "re":
                if abs(m.b) < 1e-9 and abs(m.c) < 1e-9:  # axis-aligned: stays a true `re`
                    shape.draw_rect((fitz.Rect(item[1]) * m).normalize())
                else:
                    shape.draw_quad(fitz.Rect(item[1]).quad * m)
            elif op == "qu":
                shape.draw_quad(fitz.Quad(item[1]) * m)
            elif op == "c":
                shape.draw_bezier(*(fitz.Point(q) * m for q in item[1:5]))
        lc = p.get("lineCap") or (0,)
        width = p.get("width")
        shape.finish(
            color=p.get("color"),
            fill=p.get("fill"),
            width=(width * scale) if width else 0,
            dashes=p.get("dashes") or None,
            even_odd=bool(p.get("even_odd")),
            closePath=bool(p.get("closePath")),
            lineJoin=int(p.get("lineJoin") or 0),
            lineCap=int(max(lc)) if isinstance(lc, (tuple, list)) else int(lc),
            stroke_opacity=p.get("stroke_opacity") if p.get("stroke_opacity") is not None else 1,
            fill_opacity=p.get("fill_opacity") if p.get("fill_opacity") is not None else 1,
        )
        shape.commit(overlay=overlay)


# ─── Batch engine: in-place (z-order preserving) edits with verified fallback ─


def _form_image_site(doc: fitz.Document, page: fitz.Page, pl: dict) -> Optional[tuple[int, int, int, str]]:
    """(form_xref, start, end, name) of the `/Name Do` that draws this placement
    from inside a Form XObject, when that Do can be edited without affecting any
    other placement: one referencing form, used by no other page, and its Do
    count equals the number of placements of the image on this page."""
    if pl["method"] != "redact" or pl["xref"] <= 0:
        return None
    refs: dict[int, set[str]] = {}
    for item in page.get_images(full=True):
        if item[0] == pl["xref"]:
            if item[9] == 0:
                return None  # also drawn directly by the page: ambiguous
            refs.setdefault(item[9], set()).add(item[7])
    if len(refs) != 1:
        return None
    fx, names = next(iter(refs.items()))
    if not doc.xref_is_stream(fx):
        return None
    ops = [op for op in _find_do_ops(doc.xref_stream(fx)) if op[2] in names]
    total = sum(1 for p in _image_placements(page) if p["xref"] == pl["xref"])
    if len(ops) != total or pl["occurrence"] >= len(ops):
        return None
    for pno in range(len(doc)):
        if pno != page.number and any(x[0] == fx for x in doc[pno].get_xobjects()):
            return None  # shared with another page: editing it would move that one too
    s, e, nm = ops[pl["occurrence"]]
    return fx, s, e, nm


def _clip_ok(page: fitz.Page, orig, new) -> bool:
    """False when a clip that contains the original placement (e.g. the BBox of
    the Form XObject drawing it) would cut off the new placement."""
    o, n = fitz.Rect(orig), fitz.Rect(new)
    for d in page.get_drawings(extended=True):
        if d.get("type") != "clip" or d.get("scissor") is None:
            continue
        sc = fitz.Rect(d["scissor"])
        grown = fitz.Rect(sc.x0 - 1, sc.y0 - 1, sc.x1 + 1, sc.y1 + 1)
        if grown.contains(o) and not grown.contains(n):
            return False
    return True


def _abs_image_cm(pl: dict, page: fitz.Page, t: Optional[fitz.Matrix] = None) -> fitz.Matrix:
    """CTM (PDF space, from the identity) that draws the image with PyMuPDF transform t."""
    t = fitz.Matrix(pl["transform"]) if t is None else t
    return _FLIP * t * ~page.transformation_matrix


def _wrap_with(data: bytes, prepend: bytes, append: bytes) -> bytes:
    if not prepend and not append:
        return data
    close = b"Q\n" * (1 + _q_balance(data))
    return prepend + b"q\n" + data + b"\n" + close + append


def _append_content(doc: fitz.Document, page: fitz.Page, snippet: bytes, front: bool = True):
    data = page.read_contents()
    if front:
        _write_contents(doc, page, _wrap_with(data, b"", snippet))
    else:
        _write_contents(doc, page, _wrap_with(data, snippet, b""))


def _match_rects(expected: list, actual: list, tol: float = 1.0) -> bool:
    """Multiset equality of (tag, rect) pairs within tol."""
    if len(expected) != len(actual):
        return False
    pool = sorted(actual, key=lambda t: (t[0], t[1][0], t[1][1]))
    used = [False] * len(pool)
    for tag, r in sorted(expected, key=lambda t: (t[0], t[1][0], t[1][1])):
        for k, (tag2, r2) in enumerate(pool):
            if not used[k] and tag2 == tag and _rect_close(r, r2, tol):
                used[k] = True
                break
        else:
            return False
    return True


_BATCH_OPS = ("move", "delete", "front", "back", "duplicate")


def _resolve_ops(doc: fitz.Document, page: fitz.Page, ops: list) -> list[dict]:
    if not ops:
        raise HTTPException(status_code=400, detail="No operations given")
    if len(ops) > 500:
        raise HTTPException(status_code=400, detail="Too many operations in one batch")
    seen: set = set()
    out = []
    for o in ops:
        if o.op not in _BATCH_OPS:
            raise HTTPException(status_code=400, detail=f"Unknown op '{o.op}'")
        item: dict = {"op": o.op, "kind": o.kind}
        if o.kind == "image":
            if o.xref is None:
                raise HTTPException(status_code=400, detail="Image ops need xref")
            item["pl"] = _find_placement(page, o.xref, o.occurrence, o.bbox)
            key = ("image", o.xref, o.occurrence)
        elif o.kind == "drawing":
            if o.index is None:
                raise HTTPException(status_code=400, detail="Drawing ops need index")
            item["obj"], item["paths"] = _find_group(page, o.index, o.bbox)
            key = ("drawing", o.index)
        else:
            raise HTTPException(status_code=400, detail="kind must be 'image' or 'drawing'")
        if key in seen:
            raise HTTPException(status_code=400, detail="The same object appears twice in one batch")
        seen.add(key)
        if o.op == "move":
            if o.new_bbox is None:
                raise HTTPException(status_code=400, detail="move needs new_bbox")
            item["new"] = _from_disp(page, _rect(o.new_bbox))
        if o.op == "duplicate":
            v = (fitz.Point(o.dx, o.dy) * page.derotation_matrix) - (fitz.Point(0, 0) * page.derotation_matrix)
            item["offset"] = fitz.Matrix(1, 0, 0, 1, v.x, v.y)
        out.append(item)
    return out


def _plan_inplace(doc: fitz.Document, page: fitz.Page, items: list[dict], data: bytes, force_legacy: bool):
    """Split items into in-place stream edits (z-order preserved) and legacy ops."""
    do_ops = _find_do_ops(data)
    segs, seqmap = _map_paths_to_segments(page, data)
    plan = {"page_edits": [], "prepend": [], "append": [], "form_edits": {}, "img_expect": {},
            "path_expect": {}, "legacy": [], "post": []}
    for it in items:
        op, kind = it["op"], it["kind"]
        if op == "duplicate":
            plan["post"].append(it)
            it["method"] = "duplicate"
            continue
        if force_legacy:
            plan["legacy"].append(it)
            continue
        if kind == "image":
            pl = it["pl"]
            ikey = (pl["xref"], pl["occurrence"])
            t_old = fitz.Matrix(pl["transform"])
            if abs(t_old.a * t_old.d - t_old.b * t_old.c) < 1e-9:
                plan["legacy"].append(it)
                continue
            new_t = t_old * _rect_map(fitz.Rect(pl["bbox"]), it["new"]) if op == "move" else None
            if pl["method"] == "stream":
                target = [d for d in do_ops if d[2] in pl["names"]]
                if pl["occurrence"] >= len(target):
                    plan["legacy"].append(it)
                    continue
                st, en, name = target[pl["occurrence"]]
                do = f"/{name} Do".encode("latin-1")
                if op == "move":
                    m = _FLIP * new_t * ~t_old * _FLIP
                    plan["page_edits"].append((st, en, f"q {_mat_str(m)} cm ".encode() + do + b" Q"))
                    plan["img_expect"][ikey] = it["new"]
                elif op == "delete":
                    plan["page_edits"].append((st, en, b""))
                    plan["img_expect"][ikey] = None
                else:  # front / back: same placement, drawn first or last
                    plan["page_edits"].append((st, en, b""))
                    snip = f"q {_mat_str(_abs_image_cm(pl, page))} cm ".encode() + do + b" Q\n"
                    plan["append" if op == "front" else "prepend"].append(snip)
                it["method"] = "stream"
                continue
            site = _form_image_site(doc, page, pl) if op in ("move", "delete") else None
            if site and (op == "delete" or _clip_ok(page, pl["bbox"], it["new"])):
                fx, st, en, name = site
                do = f"/{name} Do".encode("latin-1")
                if op == "move":
                    m = _FLIP * new_t * ~t_old * _FLIP
                    repl = f"q {_mat_str(m)} cm ".encode() + do + b" Q"
                    plan["img_expect"][ikey] = it["new"]
                else:
                    repl = b""
                    plan["img_expect"][ikey] = None
                plan["form_edits"].setdefault(fx, []).append((st, en, repl))
                it["method"] = "form"
                continue
            plan["legacy"].append(it)
        else:
            idxs = [seqmap.get(p["seqno"]) for p in it["paths"]]
            ok = all(i is not None and not segs[i]["bad"] and not segs[i]["clip"] for i in idxs)
            if not ok:
                plan["legacy"].append(it)
                continue
            m = _rect_map(fitz.Rect(it["obj"]["bbox"]), it["new"]) if op == "move" else None
            for i in idxs:
                plan["page_edits"].append((segs[i]["start"], segs[i]["end"], _wrap_segment(data, segs[i], page, m)))
            for p in it["paths"]:
                plan["path_expect"][p["seqno"]] = m
            if op in ("front", "back"):
                plan["post"].append(it)  # removed in place, redrawn on top / underneath
            it["method"] = "stream"
    return plan


def _apply_plan(doc: fitz.Document, page: fitz.Page, plan: dict, data: bytes) -> None:
    edits = sorted(plan["page_edits"], key=lambda t: t[0])
    for a, b in zip(edits, edits[1:]):
        if a[1] > b[0]:
            raise HTTPException(status_code=409, detail="Overlapping edits; refresh and retry")
    new = data
    for st, en, repl in reversed(edits):
        new = new[:st] + repl + new[en:]
    new = _wrap_with(new, b"".join(plan["prepend"]), b"".join(plan["append"]))
    if new != data:
        _write_contents(doc, page, new)
    for fx, fedits in plan["form_edits"].items():
        fdata = doc.xref_stream(fx)
        for st, en, repl in sorted(fedits, key=lambda t: t[0], reverse=True):
            fdata = fdata[:st] + repl + fdata[en:]
        doc.update_stream(fx, fdata)


def _verify_plan(page: fitz.Page, plan: dict, before_imgs: list[dict], before_paths: list[dict]) -> bool:
    exp_i, act_i = [], []
    for pl in before_imgs:
        key = (pl["xref"], pl["occurrence"])
        if key in plan["img_expect"]:
            r = plan["img_expect"][key]
            if r is not None:
                exp_i.append((pl["xref"], tuple(r)))
        else:
            exp_i.append((pl["xref"], tuple(pl["bbox"])))
    for pl in _image_placements(page):
        act_i.append((pl["xref"], tuple(pl["bbox"])))
    exp_p, act_p = [], []
    for p in before_paths:
        if p["seqno"] in plan["path_expect"]:
            m = plan["path_expect"][p["seqno"]]
            if m is None:
                continue
            exp_p.append((0, tuple(fitz.Rect(p["rect"]) * m)))
        else:
            exp_p.append((0, tuple(fitz.Rect(p["rect"]))))
    for p in page.get_drawings():
        act_p.append((0, tuple(fitz.Rect(p["rect"]))))
    return _match_rects(exp_i, act_i) and _match_rects(exp_p, act_p)


def _run_legacy(page: fitz.Page, it: dict) -> None:
    op = it["op"]
    if it["kind"] == "image":
        pl = it["pl"]
        _redact_remove_image(page, pl["bbox"])
        if op == "move":
            page.insert_image(it["new"], xref=pl["xref"], keep_proportion=False)
        elif op in ("front", "back"):
            page.insert_image(fitz.Rect(pl["bbox"]), xref=pl["xref"], keep_proportion=False, overlay=op == "front")
        it["method"] = "redact"
    else:
        obj, paths = it["obj"], it["paths"]
        _remove_paths(page, obj["bbox"], paths)
        if op == "move":
            _redraw_paths(page, paths, _rect_map(fitz.Rect(obj["bbox"]), it["new"]))
        elif op in ("front", "back"):
            _redraw_paths(page, paths, fitz.Identity, overlay=op == "front")
        it["method"] = "redraw"


def _run_post(doc: fitz.Document, page: fitz.Page, it: dict) -> None:
    op = it["op"]
    if it["kind"] == "image":
        pl = it["pl"]
        t = fitz.Matrix(pl["transform"]) * it["offset"]
        if pl["method"] == "stream" and pl["names"]:
            snip = f"q {_mat_str(_abs_image_cm(pl, page, t))} cm /{pl['names'][0]} Do Q\n".encode("latin-1")
            _append_content(doc, page, snip, front=True)
        else:
            page.insert_image(fitz.Rect(pl["bbox"]) * it["offset"], xref=pl["xref"], keep_proportion=False)
    else:
        if op == "duplicate":
            _redraw_paths(page, it["paths"], it["offset"])
        else:  # front/back after in-place removal
            _redraw_paths(page, it["paths"], fitz.Identity, overlay=op == "front")


def _apply_ops(doc: fitz.Document, page: fitz.Page, ops: list) -> tuple[fitz.Page, list[dict]]:
    """Apply several object edits to one page as ONE change (callers snapshot once).

    Moves/deletes are done in place in the content stream (the existing `Do` or
    path operators get a `cm` wrap, or are removed) so stacking order is kept;
    images drawn from a Form XObject are edited inside that form when it is safe.
    The result is verified by re-reading the page; if anything else moved, the
    edit is rolled back and the legacy redaction-based method is used instead.
    """
    items = _resolve_ops(doc, page, ops)
    data = page.read_contents()
    before_imgs = _image_placements(page)
    before_paths = page.get_drawings()
    form_backup: dict[int, bytes] = {}
    plan = _plan_inplace(doc, page, items, data, force_legacy=False)
    inplace = bool(plan["page_edits"] or plan["prepend"] or plan["append"] or plan["form_edits"])
    if inplace:
        for fx in plan["form_edits"]:
            form_backup[fx] = doc.xref_stream(fx)
        contents_key = doc.xref_get_key(page.xref, "Contents")
        _apply_plan(doc, page, plan, data)
        page = doc.reload_page(page)
        if not _verify_plan(page, plan, before_imgs, before_paths):
            # roll back exactly, then redo everything the legacy way
            doc.xref_set_key(page.xref, "Contents", contents_key[1])
            for fx, fdata in form_backup.items():
                doc.update_stream(fx, fdata)
            page = doc.reload_page(page)
            plan = _plan_inplace(doc, page, items, data, force_legacy=True)
    for it in plan["legacy"]:
        _run_legacy(page, it)
    for it in plan["post"]:
        _run_post(doc, page, it)
    results = []
    for it in items:
        res = {"op": it["op"], "kind": it["kind"], "method": it.get("method", "redact")}
        if it["op"] == "move":
            res["bbox"] = _to_disp(page, it["new"])
        results.append(res)
    return page, results


# ─── Models ───────────────────────────────────────────────────────────────────


class ImageRef(BaseModel):
    page: int
    xref: int
    occurrence: int = 0
    bbox: Optional[list[float]] = None  # client's view of the bbox; 409 if stale


class MoveImageRequest(ImageRef):
    new_bbox: list[float]


class CropImageRequest(ImageRef):
    crop_bbox: list[float]  # page coordinates; intersected with the placement


class RotateImageRequest(ImageRef):
    angle: int = 90  # clockwise as displayed: 90, 180 or 270


class DrawingRef(BaseModel):
    page: int
    index: int
    bbox: Optional[list[float]] = None


class MoveDrawingRequest(DrawingRef):
    new_bbox: list[float]


class BatchOp(BaseModel):
    op: str  # move | delete | front | back | duplicate
    kind: str  # image | drawing
    xref: Optional[int] = None  # images
    occurrence: int = 0
    index: Optional[int] = None  # drawings
    bbox: Optional[list[float]] = None  # client's view; 409 if stale
    new_bbox: Optional[list[float]] = None  # move
    dx: float = 0.0  # duplicate offset (displayed page points)
    dy: float = 0.0


class BatchRequest(BaseModel):
    page: int
    ops: list[BatchOp]
    label: Optional[str] = None


class ArrangeRequest(BaseModel):
    page: int
    kind: str
    where: str  # front | back
    xref: Optional[int] = None
    occurrence: int = 0
    index: Optional[int] = None
    bbox: Optional[list[float]] = None


class ShapeRequest(BaseModel):
    page: int
    type: str  # rect | ellipse | line | arrow
    rect: Optional[list[float]] = None  # for rect / ellipse
    start: Optional[list[float]] = None  # for line / arrow
    end: Optional[list[float]] = None
    stroke_color: Optional[list[float]] = Field(default_factory=lambda: [0, 0, 0])
    fill_color: Optional[list[float]] = None
    width: float = 2.0
    stroke_opacity: float = 1.0
    fill_opacity: float = 1.0
    dashed: bool = False


# ─── Endpoints: listing ───────────────────────────────────────────────────────


@router.get("/{doc_id}/objects/{page_num}")
async def list_objects(doc_id: str, page_num: int):
    """Every image placement and grouped vector object on a page."""
    doc, _ = _open(doc_id)
    page = _page(doc, page_num)
    try:
        images = _image_placements(page)
        drawings, _ = _drawing_groups(page)
        for o in images + drawings:  # clients get what they see on screen
            o["bbox"] = _to_disp(page, o["bbox"])
        return {
            "page": page_num,
            "page_width": page.rect.width,
            "page_height": page.rect.height,
            "rotation": page.rotation,
            "images": images,
            "drawings": drawings,
        }
    finally:
        doc.close()


# ─── Endpoints: images ────────────────────────────────────────────────────────


@router.post("/{doc_id}/objects/image/move")
async def move_image(doc_id: str, req: MoveImageRequest):
    """Move and/or resize one image placement to new_bbox (page coords).

    The placement keeps its place in the stacking order (also for images drawn
    from inside a Form XObject, when that form can be edited safely)."""
    doc, path = _open(doc_id)
    page = _page(doc, req.page)
    op = BatchOp(op="move", kind="image", xref=req.xref, occurrence=req.occurrence, bbox=req.bbox,
                 new_bbox=req.new_bbox)
    _resolve_ops(doc, page, [op])  # validate (404/409/400) before snapshotting
    snapshot(doc_id, f"Move image on page {req.page + 1}")
    page, results = _apply_ops(doc, page, [op])
    _save(doc, path)
    return {"status": "ok", "method": results[0]["method"], "bbox": results[0]["bbox"]}


@router.post("/{doc_id}/objects/image/rotate")
async def rotate_image(doc_id: str, req: RotateImageRequest):
    """Rotate one image placement about its centre (90/180/270 clockwise)."""
    if req.angle % 90 != 0 or req.angle % 360 == 0:
        raise HTTPException(status_code=400, detail="Angle must be 90, 180 or 270")
    doc, path = _open(doc_id)
    page = _page(doc, req.page)
    pl = _find_placement(page, req.xref, req.occurrence, req.bbox)
    snapshot(doc_id, f"Rotate image on page {req.page + 1}")
    bb = fitz.Rect(pl["bbox"])
    # PyMuPDF space is y-down, so a positive Matrix(angle) turns clockwise on screen.
    rot = _rotation_about((bb.tl + bb.br) / 2, req.angle % 360)
    if pl["method"] == "stream":
        _rewrite_placement(doc, page, pl, new_transform=fitz.Matrix(pl["transform"]) * rot)
    else:
        _redact_remove_image(page, pl["bbox"])
        new = (bb * rot).normalize()
        page.insert_image(new, xref=pl["xref"], keep_proportion=False, rotate=(360 - req.angle % 360) % 360)
    _save(doc, path)
    return {"status": "ok", "method": pl["method"]}


@router.post("/{doc_id}/objects/image/delete")
async def delete_image_placement(doc_id: str, req: ImageRef):
    """Delete one image placement. If it was the last use on the page the image
    resource is dropped and the object garbage-collected (bytes leave the file)."""
    doc, path = _open(doc_id)
    page = _page(doc, req.page)
    pl = _find_placement(page, req.xref, req.occurrence, req.bbox)
    snapshot(doc_id, f"Delete image on page {req.page + 1}")
    if pl["method"] == "stream":
        _rewrite_placement(doc, page, pl, delete=True)
        page.clean_contents(sanitize=1)  # drops now-unused resources
    else:
        _redact_remove_image(page, pl["bbox"])
    _save(doc, path, garbage=1)
    return {"status": "ok", "method": pl["method"]}


@router.post("/{doc_id}/objects/image/crop")
async def crop_image(doc_id: str, req: CropImageRequest):
    """Crop one placement to crop_bbox. Pixels outside are really removed: the
    placement now draws a new, cropped image object (other uses are untouched)."""
    doc, path = _open(doc_id)
    page = _page(doc, req.page)
    pl = _find_placement(page, req.xref, req.occurrence, req.bbox)
    t = fitz.Matrix(pl["transform"])
    crop = _from_disp(page, fitz.Rect(req.crop_bbox)) & fitz.Rect(pl["bbox"])
    if crop.is_empty or crop.width < 1 or crop.height < 1:
        raise HTTPException(status_code=400, detail="Crop area does not overlap the image")

    # crop corners -> image unit square (PyMuPDF orientation: v=0 is the top row)
    inv = ~t
    pts = [p * inv for p in (crop.tl, crop.tr, crop.bl, crop.br)]
    u0 = max(0.0, min(p.x for p in pts))
    u1 = min(1.0, max(p.x for p in pts))
    v0 = max(0.0, min(p.y for p in pts))
    v1 = min(1.0, max(p.y for p in pts))
    pix = _image_pixmap(doc, pl["xref"])
    x0, x1 = int(math.floor(u0 * pix.width)), int(math.ceil(u1 * pix.width))
    y0, y1 = int(math.floor(v0 * pix.height)), int(math.ceil(v1 * pix.height))
    if x1 - x0 < 1 or y1 - y0 < 1:
        raise HTTPException(status_code=400, detail="Crop area is smaller than one pixel")
    from PIL import Image

    pil = Image.open(io.BytesIO(_png_bytes(pix)))
    pil = pil.crop((x0, y0, x1, y1))
    buf = io.BytesIO()
    pil.save(buf, "PNG")

    # snap to whole pixels so the image isn't stretched
    u0, u1 = x0 / pix.width, x1 / pix.width
    v0, v1 = y0 / pix.height, y1 / pix.height
    sub = fitz.Matrix(u1 - u0, 0, 0, v1 - v0, u0, v0)  # new unit square -> old unit square

    snapshot(doc_id, f"Crop image on page {req.page + 1}")
    if pl["method"] == "stream":
        _, new_name = _register_image(page, buf.getvalue())
        _rewrite_placement(doc, page, pl, new_transform=sub * t, new_name=new_name, drop_last_of=new_name)
        page.clean_contents(sanitize=1)
    else:
        _redact_remove_image(page, pl["bbox"])
        page.insert_image(fitz.Rect(fitz.Rect(0, 0, 1, 1) * (sub * t)), stream=buf.getvalue(), keep_proportion=False)
    _save(doc, path, garbage=1)
    return {"status": "ok", "method": pl["method"], "pixels": [x1 - x0, y1 - y0]}


@router.post("/{doc_id}/objects/image/replace")
async def replace_image(
    doc_id: str,
    page: int = Form(...),
    xref: int = Form(...),
    occurrence: int = Form(0),
    scope: str = Form("placement"),  # "placement" | "all"
    file: UploadFile = File(...),
):
    """Replace an image. scope=placement swaps only this placement (same box);
    scope=all replaces the image object everywhere it is used (page.replace_image)."""
    if scope not in ("placement", "all"):
        raise HTTPException(status_code=400, detail="scope must be 'placement' or 'all'")
    data = await _read_upload(file)
    doc, path = _open(doc_id)
    pg = _page(doc, page)
    pl = _find_placement(pg, xref, occurrence, None)
    snapshot(doc_id, f"Replace image on page {page + 1}")
    method = pl["method"]
    if scope == "all":
        pg.replace_image(xref, stream=data)
        method = "replace_image"
    elif pl["method"] == "stream":
        _, new_name = _register_image(pg, data)
        _rewrite_placement(doc, pg, pl, new_name=new_name, drop_last_of=new_name)
        pg.clean_contents(sanitize=1)
    else:
        _redact_remove_image(pg, pl["bbox"])
        pg.insert_image(fitz.Rect(pl["bbox"]), stream=data, keep_proportion=False)
    _save(doc, path, garbage=1)
    return {"status": "ok", "method": method}


@router.get("/{doc_id}/objects/image/{xref}/extract")
async def extract_image(doc_id: str, xref: int, format: str = "original"):
    """Download an image. format=original returns the embedded bytes untouched
    (e.g. the original JPEG); format=png returns decoded pixels with alpha."""
    doc, _ = _open(doc_id)
    try:
        if xref <= 0 or xref >= doc.xref_length() or not doc.xref_is_image(xref):
            raise HTTPException(status_code=404, detail="Image not found")
        if format == "png":
            data, ext = _png_bytes(_image_pixmap(doc, xref)), "png"
        elif format == "original":
            info = doc.extract_image(xref)
            data, ext = info["image"], info["ext"]
        else:
            raise HTTPException(status_code=400, detail="format must be 'original' or 'png'")
    finally:
        doc.close()
    media = {
        "png": "image/png",
        "jpeg": "image/jpeg",
        "jpg": "image/jpeg",
        "jpx": "image/jp2",
        "jb2": "image/x-jbig2",
        "tiff": "image/tiff",
        "bmp": "image/bmp",
    }.get(ext, "application/octet-stream")
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="image-{xref}.{ext}"'},
    )


@router.post("/{doc_id}/objects/image/insert")
async def insert_image(
    doc_id: str,
    page: int = Form(...),
    x0: float = Form(...),
    y0: float = Form(...),
    x1: float = Form(...),
    y1: float = Form(...),
    keep_proportion: bool = Form(True),
    file: UploadFile = File(...),
):
    """Insert an uploaded image (PNG/JPEG/…; transparency kept) into a rect."""
    data = await _read_upload(file)
    rect = _rect([x0, y0, x1, y1])
    doc, path = _open(doc_id)
    pg = _page(doc, page)
    rect = _from_disp(pg, rect)
    snapshot(doc_id, f"Insert image on page {page + 1}")
    xref = pg.insert_image(rect, stream=data, keep_proportion=keep_proportion)
    _save(doc, path)
    return {"status": "ok", "xref": xref}


# ─── Endpoints: vector drawings & shapes ──────────────────────────────────────


@router.post("/{doc_id}/objects/drawing/move")
async def move_drawing(doc_id: str, req: MoveDrawingRequest):
    """Move/resize a grouped vector object. The existing path operators are
    transformed in place (stacking order kept); paths that cannot be addressed
    in the page stream are re-created at the new place and the originals removed."""
    doc, path = _open(doc_id)
    page = _page(doc, req.page)
    op = BatchOp(op="move", kind="drawing", index=req.index, bbox=req.bbox, new_bbox=req.new_bbox)
    _resolve_ops(doc, page, [op])
    snapshot(doc_id, f"Move vector object on page {req.page + 1}")
    page, results = _apply_ops(doc, page, [op])
    _save(doc, path)
    return {"status": "ok", "bbox": results[0]["bbox"], "method": results[0]["method"]}


@router.post("/{doc_id}/objects/drawing/delete")
async def delete_drawing(doc_id: str, req: DrawingRef):
    """Delete a grouped vector object; text and images in the area survive."""
    doc, path = _open(doc_id)
    page = _page(doc, req.page)
    op = BatchOp(op="delete", kind="drawing", index=req.index, bbox=req.bbox)
    _resolve_ops(doc, page, [op])
    snapshot(doc_id, f"Delete vector object on page {req.page + 1}")
    page, results = _apply_ops(doc, page, [op])
    _save(doc, path)
    return {"status": "ok", "method": results[0]["method"]}


@router.post("/{doc_id}/objects/batch")
async def batch_objects(doc_id: str, req: BatchRequest):
    """Apply several move/delete/front/back/duplicate ops on one page as ONE
    change: a single undo snapshot, one save. Every target is validated against
    the client's bbox first, so a stale selection changes nothing (409)."""
    doc, path = _open(doc_id)
    page = _page(doc, req.page)
    _resolve_ops(doc, page, req.ops)
    n = len(req.ops)
    label = req.label or (f"Edit {n} objects on page {req.page + 1}" if n > 1 else f"Edit object on page {req.page + 1}")
    snapshot(doc_id, label[:120])
    page, results = _apply_ops(doc, page, req.ops)
    if any(r["op"] == "delete" for r in results):
        _save(doc, path, garbage=1)
    else:
        _save(doc, path)
    return {"status": "ok", "results": results}


@router.post("/{doc_id}/objects/arrange")
async def arrange_object(doc_id: str, req: ArrangeRequest):
    """Bring an object to the front (drawn last) or send it to the back (drawn first)."""
    if req.where not in ("front", "back"):
        raise HTTPException(status_code=400, detail="where must be 'front' or 'back'")
    doc, path = _open(doc_id)
    page = _page(doc, req.page)
    op = BatchOp(op=req.where, kind=req.kind, xref=req.xref, occurrence=req.occurrence, index=req.index, bbox=req.bbox)
    _resolve_ops(doc, page, [op])
    snapshot(doc_id, f"{'Bring to front' if req.where == 'front' else 'Send to back'} on page {req.page + 1}")
    page, results = _apply_ops(doc, page, [op])
    _save(doc, path)
    return {"status": "ok", "method": results[0]["method"]}


@router.post("/{doc_id}/objects/shape")
async def add_shape(doc_id: str, req: ShapeRequest):
    """Draw rect / ellipse / line / arrow as real PDF vector content (not an annotation)."""
    kind = req.type
    if kind not in ("rect", "ellipse", "line", "arrow"):
        raise HTTPException(status_code=400, detail="type must be rect, ellipse, line or arrow")
    if not (0 <= req.width <= 100):
        raise HTTPException(status_code=400, detail="width must be between 0 and 100")
    stroke = _color(req.stroke_color)
    fill = _color(req.fill_color)
    if stroke is None and fill is None:
        raise HTTPException(status_code=400, detail="Shape needs a stroke or fill color")
    dashes = f"[{max(req.width * 3, 3):g}] 0" if req.dashed else None
    so = max(0.0, min(1.0, req.stroke_opacity))
    fo = max(0.0, min(1.0, req.fill_opacity))

    if kind in ("rect", "ellipse"):
        if req.rect is None:
            raise HTTPException(status_code=400, detail="rect is required")
        rect = _rect(req.rect)
    else:
        if req.start is None or req.end is None or len(req.start) != 2 or len(req.end) != 2:
            raise HTTPException(status_code=400, detail="start and end points are required")
        p1, p2 = fitz.Point(req.start), fitz.Point(req.end)
        if abs(p2 - p1) < 1:
            raise HTTPException(status_code=400, detail="Line is too short")

    doc, path = _open(doc_id)
    page = _page(doc, req.page)
    if kind in ("rect", "ellipse"):
        rect = _from_disp(page, rect)
    else:
        p1, p2 = p1 * page.derotation_matrix, p2 * page.derotation_matrix
    snapshot(doc_id, f"Add {kind} on page {req.page + 1}")
    shape = page.new_shape()
    if kind == "rect":
        shape.draw_rect(rect)
        shape.finish(color=stroke, fill=fill, width=req.width, dashes=dashes, stroke_opacity=so, fill_opacity=fo)
    elif kind == "ellipse":
        shape.draw_oval(rect)
        shape.finish(color=stroke, fill=fill, width=req.width, dashes=dashes, stroke_opacity=so, fill_opacity=fo)
    elif kind == "line":
        shape.draw_line(p1, p2)
        shape.finish(color=stroke or fill, width=req.width or 1, dashes=dashes, stroke_opacity=so, closePath=False)
    else:  # arrow: shaft + filled triangular head
        col = stroke or fill
        w = req.width or 1
        length = abs(p2 - p1)
        head = min(max(w * 4, 8), length * 0.6)
        ux, uy = (p2.x - p1.x) / length, (p2.y - p1.y) / length
        base = fitz.Point(p2.x - ux * head, p2.y - uy * head)
        half = head * 0.5
        left = fitz.Point(base.x - uy * half, base.y + ux * half)
        right = fitz.Point(base.x + uy * half, base.y - ux * half)
        shape.draw_line(p1, base)
        shape.finish(color=col, width=w, dashes=dashes, stroke_opacity=so, closePath=False)
        shape.draw_polyline([left, p2, right])
        shape.finish(color=col, fill=col, width=0, closePath=True, stroke_opacity=so, fill_opacity=so)
    shape.commit(overlay=True)
    _save(doc, path)
    return {"status": "ok"}
