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
stream) we cannot address the placement in the page stream; those operations
fall back to redaction-based removal (``images=PDF_REDACT_IMAGE_REMOVE``,
text and line-art untouched) plus re-insertion, and the response reports
``"method": "redact"``.
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


def _redraw_paths(page: fitz.Page, paths: list[dict], m: fitz.Matrix):
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
        shape.commit(overlay=True)


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
    """Move and/or resize one image placement to new_bbox (page coords)."""
    doc, path = _open(doc_id)
    page = _page(doc, req.page)
    pl = _find_placement(page, req.xref, req.occurrence, req.bbox)
    new = _from_disp(page, _rect(req.new_bbox))
    snapshot(doc_id, f"Move image on page {req.page + 1}")
    if pl["method"] == "stream":
        r = _rect_map(fitz.Rect(pl["bbox"]), new)
        _rewrite_placement(doc, page, pl, new_transform=fitz.Matrix(pl["transform"]) * r)
    else:
        _redact_remove_image(page, pl["bbox"])
        page.insert_image(new, xref=pl["xref"], keep_proportion=False)
    out = _to_disp(page, new)
    _save(doc, path)
    return {"status": "ok", "method": pl["method"], "bbox": out}


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
    """Move/resize a grouped vector object. Paths are re-created as real PDF
    vector paths at the new place (drawn on top) and the originals removed."""
    doc, path = _open(doc_id)
    page = _page(doc, req.page)
    obj, paths = _find_group(page, req.index, req.bbox)
    new = _from_disp(page, _rect(req.new_bbox))
    snapshot(doc_id, f"Move vector object on page {req.page + 1}")
    m = _rect_map(fitz.Rect(obj["bbox"]), new)
    _remove_paths(page, obj["bbox"], paths)
    _redraw_paths(page, paths, m)
    out = _to_disp(page, new)
    _save(doc, path)
    return {"status": "ok", "bbox": out}


@router.post("/{doc_id}/objects/drawing/delete")
async def delete_drawing(doc_id: str, req: DrawingRef):
    """Delete a grouped vector object; text and images in the area survive."""
    doc, path = _open(doc_id)
    page = _page(doc, req.page)
    obj, paths = _find_group(page, req.index, req.bbox)
    snapshot(doc_id, f"Delete vector object on page {req.page + 1}")
    _remove_paths(page, obj["bbox"], paths)
    _save(doc, path)
    return {"status": "ok"}


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
