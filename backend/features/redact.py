"""
Redaction & security feature router.

True, PDF-native redaction (content is removed from the content streams, not
covered), search-and-redact with PII presets, document sanitization (hidden
information removal) and AES-256 protection.

Coordinate convention (IMPORTANT)
---------------------------------
Every rect that crosses this API is ``[x0, y0, x1, y1]`` in PDF points with a
top-left origin, in the *visible* page space — i.e. the space of the rendered
page image, which already has the page's /Rotate applied (``page.rect``).
PyMuPDF text extraction and annotation methods work in *unrotated* space, so
this module converts at the boundary with ``page.rotation_matrix`` /
``page.derotation_matrix``. For unrotated pages (the common case) the two
spaces are identical.

To convert a rendered-pixel coordinate to visible points the frontend divides
by (rendered_width_px / page.rect.width).

Workflow
--------
1. Mark: ``POST /{id}/redact/mark`` stores *Redact annotations* in the PDF
   (Acrobat-compatible "marked for redaction" state). Each mark carries its own
   fill colour and overlay text.
2. Review: ``GET /{id}/redact/marks``; remove with ``DELETE``.
3. Apply: ``POST /{id}/redact/apply`` burns in every pending mark — text,
   image pixels and vector art beneath are removed — then saves with garbage
   collection so the old content streams are not left in the file, then
   purges every undo snapshot and derived text cache (analysis.json, chat
   history) for the document. Applying redactions is NOT undoable.

Every mutating route calls ``snapshot()`` first so undo/redo works (apply then
purges history, see above).
"""

from __future__ import annotations

import os
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Literal, Optional, Union

import fitz  # PyMuPDF
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend import advanced_ops
from backend.advanced_ops import snapshot

router = APIRouter(prefix="/api/pdf")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

MAX_MATCHES = 5000
MAX_QUERY_LEN = 500

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _doc_path(doc_id: str) -> Path:
    """Resolve the working PDF (same storage layout as main.py/advanced_ops)."""
    if not _UUID_RE.match(doc_id or ""):
        raise HTTPException(status_code=400, detail="Invalid document ID")
    # Read UPLOAD_DIR at call time so snapshot() and this module always agree.
    path = Path(advanced_ops.UPLOAD_DIR) / doc_id / "original.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    return path


def _open(doc_id: str) -> tuple[fitz.Document, Path]:
    path = _doc_path(doc_id)
    try:
        doc = fitz.open(str(path))
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to open document")
    if doc.needs_pass:
        doc.close()
        raise HTTPException(
            status_code=423,
            detail="Document is password-protected. Unlock it before editing.",
        )
    return doc, path


def _save_in_place(doc: fitz.Document, path: Path) -> None:
    """Save with full garbage collection.

    garbage=4 drops unreferenced objects (e.g. the pre-redaction content
    stream) and de-duplicates; without it the redacted text could survive as
    an orphaned object inside the file.
    """
    tmp = str(path) + f".{uuid.uuid4().hex}.tmp"
    try:
        doc.save(tmp, garbage=4, deflate=True, clean=True)
    finally:
        doc.close()
    os.replace(tmp, str(path))


def _parse_color(value: Union[str, list[float], None], default=(0.0, 0.0, 0.0)) -> tuple:
    if value is None:
        return default
    if isinstance(value, str):
        s = value.strip().lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
            raise HTTPException(status_code=400, detail=f"Invalid color: {value}")
        return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    if len(value) != 3:
        raise HTTPException(status_code=400, detail="Color must be [r, g, b]")
    vals = [float(v) for v in value]
    if any(v > 1.0 for v in vals):  # accept 0-255 too
        vals = [v / 255.0 for v in vals]
    return tuple(max(0.0, min(1.0, v)) for v in vals)


def _color_hex(rgb) -> Optional[str]:
    if not rgb:
        return None
    return "#" + "".join(f"{int(round(max(0, min(1, c)) * 255)):02x}" for c in rgb[:3])


def _page(doc: fitz.Document, page_num: int) -> fitz.Page:
    if page_num < 0 or page_num >= len(doc):
        raise HTTPException(status_code=400, detail=f"Invalid page number: {page_num}")
    return doc[page_num]


def _to_unrotated(page: fitz.Page, rect: list[float]) -> fitz.Rect:
    r = fitz.Rect(rect)
    if page.rotation:
        r = r * page.derotation_matrix
    r.normalize()
    return r


def _to_visible(page: fitz.Page, rect) -> list[float]:
    r = fitz.Rect(rect)
    if page.rotation:
        r = r * page.rotation_matrix
    r.normalize()
    return [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]


def _valid_rect(rect: list[float]) -> bool:
    if len(rect) != 4:
        return False
    r = fitz.Rect(rect)
    r.normalize()
    return r.width > 0.5 and r.height > 0.5


# ─── Text model for search (char-accurate) ───────────────────────────────────


def _page_chars(page: fitz.Page) -> tuple[str, list[Optional[tuple]]]:
    """Return the page text and a parallel list of char bboxes (unrotated).

    Lines are separated by '\\n', blocks by '\\n\\n'. A synthetic space (bbox
    None) is inserted where a visible gap separates glyphs without an explicit
    space, so "Hello" + "World" positioned apart still reads "Hello World".
    Entries are (x0, y0, x1, y1, line_key).
    """
    raw = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP)
    text_parts: list[str] = []
    boxes: list[Optional[tuple]] = []
    for bi, block in enumerate(raw.get("blocks", [])):
        if block.get("type") != 0:
            continue
        if text_parts:
            text_parts.append("\n\n")
            boxes.extend([None, None])
        for li, line in enumerate(block.get("lines", [])):
            if li > 0:
                text_parts.append("\n")
                boxes.append(None)
            prev = None
            for span in line.get("spans", []):
                size = span.get("size", 10) or 10
                for ch in span.get("chars", []):
                    c = ch.get("c", "")
                    if not c:
                        continue
                    b = ch["bbox"]
                    if (
                        prev is not None
                        and c != " "
                        and text_parts
                        and text_parts[-1] != " "
                        and b[0] - prev[2] > size * 0.25
                    ):
                        text_parts.append(" ")
                        boxes.append(None)
                    text_parts.append(c)
                    boxes.append((b[0], b[1], b[2], b[3], (bi, li)))
                    prev = b
    return "".join(text_parts), boxes


def _rects_for_span(boxes, start: int, end: int) -> list[fitz.Rect]:
    """Union char boxes per line for text[start:end]."""
    by_line: dict = {}
    order: list = []
    for i in range(start, end):
        b = boxes[i]
        if b is None:
            continue
        key = b[4]
        r = fitz.Rect(b[:4])
        if r.is_empty:  # zero-width glyphs (spaces) — still extend vertically
            r = fitz.Rect(b[0], b[1], max(b[2], b[0] + 0.1), b[3])
        if key not in by_line:
            by_line[key] = r
            order.append(key)
        else:
            by_line[key] |= r
    return [by_line[k] for k in order]


# ─── PII presets ─────────────────────────────────────────────────────────────


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _ssn_ok(m: re.Match) -> bool:
    digits = re.sub(r"\D", "", m.group(0))
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    return area not in ("000", "666") and not area.startswith("9") and group != "00" and serial != "0000"


def _card_ok(m: re.Match) -> bool:
    digits = re.sub(r"\D", "", m.group(0))
    return 13 <= len(digits) <= 19 and _luhn_ok(digits)


_MONTHS = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
)
_STREET_SUFFIX = (
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|"
    r"Place|Pl|Terrace|Ter|Circle|Cir|Parkway|Pkwy|Highway|Hwy|Square|Sq|Trail|Trl)\.?"
)

PRESETS: dict[str, dict] = {
    "ssn": {
        "label": "Social Security numbers",
        "pattern": re.compile(r"(?<![\d-])\d{3}[- ]\d{2}[- ]\d{4}(?![\d-])"),
        "validate": _ssn_ok,
    },
    "phone": {
        "label": "Phone numbers",
        "pattern": re.compile(
            r"(?<![\w+])(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\d)"
        ),
        "validate": None,
    },
    "email": {
        "label": "Email addresses",
        "pattern": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        "validate": None,
    },
    "credit_card": {
        "label": "Credit card numbers (Luhn-checked)",
        "pattern": re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d])"),
        "validate": _card_ok,
    },
    "date": {
        "label": "Dates",
        "pattern": re.compile(
            r"\b(?:"
            r"\d{1,2}[/.-]\d{1,2}[/.-](?:\d{4}|\d{2})"
            r"|\d{4}-\d{1,2}-\d{1,2}"
            rf"|{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
            rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS},?\s+\d{{4}}"
            r")(?!\d)",
            re.IGNORECASE,
        ),
        "validate": None,
    },
    "money": {
        "label": "Money amounts",
        "pattern": re.compile(
            r"(?:[$€£]\s?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?(?:\s?(?:million|billion|[MBK])\b)?"
            r"|\b\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?\s?(?:USD|EUR|GBP|dollars)\b)",
            re.IGNORECASE,
        ),
        "validate": None,
    },
    "address": {
        "label": "US street addresses (best effort)",
        "pattern": re.compile(
            r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Z0-9][A-Za-z0-9'.-]*\s+){1,4}" + _STREET_SUFFIX
            + r"(?:,?\s+(?:Apt|Apartment|Suite|Ste|Unit|#)\.?\s*[A-Za-z0-9-]+)?"
            + r"(?:,?\s+[A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+){0,2},?\s+[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?)?"
        ),
        "validate": None,
    },
}


# ─── Models ───────────────────────────────────────────────────────────────────


class RedactArea(BaseModel):
    page: int
    rect: list[float] = Field(..., description="[x0,y0,x1,y1] visible-page PDF points")


class MarkRequest(BaseModel):
    areas: list[RedactArea]
    fill_color: Union[str, list[float]] = "#000000"
    overlay_text: Optional[str] = None
    overlay_text_color: Union[str, list[float]] = "#ffffff"
    overlay_font_size: float = 10
    label: Optional[str] = None  # stored in the annot /Contents for review


class ApplyRequest(BaseModel):
    # Optional extra areas to mark-and-apply in one call (e.g. quick redact).
    areas: list[RedactArea] = []
    fill_color: Union[str, list[float]] = "#000000"
    overlay_text: Optional[str] = None
    overlay_text_color: Union[str, list[float]] = "#ffffff"
    overlay_font_size: float = 10
    pages: Optional[list[int]] = None  # None → apply pending marks on every page
    images: Literal["pixels", "remove", "none"] = "pixels"
    graphics: Literal["covered", "touched", "none"] = "covered"


class SearchRequest(BaseModel):
    query: Optional[str] = None
    mode: Literal["text", "regex"] = "text"
    case_sensitive: bool = False
    whole_word: bool = False
    presets: list[str] = []
    pages: Optional[list[int]] = None


class SanitizeRequest(BaseModel):
    metadata: bool = True
    xmp_metadata: bool = True
    embedded_files: bool = True
    javascript: bool = True
    hidden_text: bool = True  # render-mode-3 / invisible text (incl. OCR layers)
    white_text: bool = True  # fill-white text (likely hidden on white paper)
    annotations: bool = True  # comments/markup (pending Redact marks are kept)
    form_data: bool = True  # reset field values
    links: bool = True
    thumbnails: bool = True


PERMISSION_FLAGS = {
    "print": fitz.PDF_PERM_PRINT | fitz.PDF_PERM_PRINT_HQ,
    "copy": fitz.PDF_PERM_COPY,
    "modify": fitz.PDF_PERM_MODIFY,
    "annotate": fitz.PDF_PERM_ANNOTATE,
    "fill_forms": fitz.PDF_PERM_FORM,
    "assemble": fitz.PDF_PERM_ASSEMBLE,
}


class ProtectRequest(BaseModel):
    user_password: str = ""  # password to OPEN; empty = opens freely
    owner_password: str  # permissions password
    permissions: list[str] = ["print", "copy", "fill_forms"]
    apply_to_document: bool = False  # False → return encrypted copy as a download


class UnlockRequest(BaseModel):
    password: str


# ─── Redaction: words / marks / apply ─────────────────────────────────────────


@router.get("/{doc_id}/redact/words/{page_num}")
async def get_words(doc_id: str, page_num: int):
    """Words with visible-space rects — for click-to-mark."""
    doc, _ = _open(doc_id)
    try:
        page = _page(doc, page_num)
        words = [
            {"text": w[4], "rect": _to_visible(page, w[:4]), "block": w[5], "line": w[6], "word": w[7]}
            for w in page.get_text("words")
        ]
        return {
            "page": page_num,
            "width": page.rect.width,
            "height": page.rect.height,
            "words": words,
        }
    finally:
        doc.close()


def _add_marks(doc: fitz.Document, areas: list[RedactArea], fill, text, text_color, font_size, label) -> list[dict]:
    added = []
    for area in areas:
        if not _valid_rect(area.rect):
            raise HTTPException(status_code=400, detail=f"Invalid rect: {area.rect}")
        page = _page(doc, area.page)
        # Clip to the page (both in unrotated space).
        rect = _to_unrotated(page, area.rect) & fitz.Rect(_to_unrotated(page, list(page.rect)))
        if rect.is_empty or rect.width < 0.5 or rect.height < 0.5:
            raise HTTPException(status_code=400, detail=f"Rect outside page: {area.rect}")
        annot = page.add_redact_annot(
            rect,
            text=text or None,
            fontsize=font_size,
            fill=fill,
            text_color=text_color,
            cross_out=True,
        )
        if label:
            annot.set_info(content=label[:200])
            annot.update()
        added.append({"page": area.page, "xref": annot.xref, "rect": _to_visible(page, annot.rect)})
    return added


def _list_marks(doc: fitz.Document, pages: Optional[list[int]] = None) -> list[dict]:
    out = []
    for pno in range(len(doc)):
        if pages is not None and pno not in pages:
            continue
        page = doc[pno]
        for annot in page.annots(types=[fitz.PDF_ANNOT_REDACT]):
            info = annot.info
            out.append({
                "page": pno,
                "xref": annot.xref,
                "rect": _to_visible(page, annot.rect),
                "label": info.get("content") or "",
                "overlay_text": "",  # MuPDF stores it in /OverlayText; read below
                "fill_color": _color_hex(annot.colors.get("fill")),
            })
            try:
                ot = doc.xref_get_key(annot.xref, "OverlayText")
                if ot[0] == "string":
                    out[-1]["overlay_text"] = ot[1]
            except Exception:
                pass
    return out


@router.post("/{doc_id}/redact/mark")
async def mark_redactions(doc_id: str, req: MarkRequest):
    """Mark areas for redaction (stored as PDF Redact annotations; nothing removed yet)."""
    if not req.areas:
        raise HTTPException(status_code=400, detail="No areas given")
    fill = _parse_color(req.fill_color)
    tcol = _parse_color(req.overlay_text_color, (1, 1, 1))
    doc, path = _open(doc_id)
    # Validate everything before snapshotting/mutating.
    for a in req.areas:
        _page(doc, a.page)
        if not _valid_rect(a.rect):
            doc.close()
            raise HTTPException(status_code=400, detail=f"Invalid rect: {a.rect}")
    snapshot(doc_id, f"Mark {len(req.areas)} area(s) for redaction")
    try:
        added = _add_marks(doc, req.areas, fill, req.overlay_text, tcol, req.overlay_font_size, req.label)
    except Exception:
        doc.close()
        raise
    _save_in_place(doc, path)
    return {"status": "ok", "marked": added, "count": len(added)}


@router.get("/{doc_id}/redact/marks")
async def get_marks(doc_id: str):
    doc, _ = _open(doc_id)
    try:
        marks = _list_marks(doc)
        return {"marks": marks, "count": len(marks)}
    finally:
        doc.close()


@router.delete("/{doc_id}/redact/marks/{page_num}/{xref}")
async def delete_mark(doc_id: str, page_num: int, xref: int):
    doc, path = _open(doc_id)
    page = _page(doc, page_num)
    target = None
    for annot in page.annots(types=[fitz.PDF_ANNOT_REDACT]):
        if annot.xref == xref:
            target = annot
            break
    if target is None:
        doc.close()
        raise HTTPException(status_code=404, detail="Redaction mark not found")
    snapshot(doc_id, "Remove redaction mark")
    page.delete_annot(target)
    _save_in_place(doc, path)
    return {"status": "ok"}


@router.delete("/{doc_id}/redact/marks")
async def clear_marks(doc_id: str):
    doc, path = _open(doc_id)
    total = sum(len(list(p.annots(types=[fitz.PDF_ANNOT_REDACT]))) for p in doc)
    if total == 0:
        doc.close()
        return {"status": "ok", "removed": 0}
    snapshot(doc_id, "Clear redaction marks")
    for page in doc:
        for annot in list(page.annots(types=[fitz.PDF_ANNOT_REDACT])):
            page.delete_annot(annot)
    _save_in_place(doc, path)
    return {"status": "ok", "removed": total}


_IMAGE_MODES = {
    "pixels": fitz.PDF_REDACT_IMAGE_PIXELS,
    "remove": fitz.PDF_REDACT_IMAGE_REMOVE,
    "none": fitz.PDF_REDACT_IMAGE_NONE,
}
_GRAPHICS_MODES = {
    "covered": fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
    "touched": fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
    "none": fitz.PDF_REDACT_LINE_ART_NONE,
}


def _glyphs_in(page: fitz.Page, rects: list[fitz.Rect]) -> list[str]:
    """Non-space glyphs whose centre lies inside any of rects (unrotated).

    Each glyph is counted once even if rects overlap.
    """
    out = []
    raw = page.get_text("rawdict", flags=fitz.TEXT_MEDIABOX_CLIP)
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                for ch in span["chars"]:
                    if ch["c"].isspace():
                        continue
                    b = ch["bbox"]
                    centre = fitz.Point((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
                    if any(centre in r for r in rects):
                        out.append(ch["c"])
    return out


@router.post("/{doc_id}/redact/apply")
async def apply_redactions(doc_id: str, req: ApplyRequest):
    """IRREVERSIBLY remove all content under pending redaction marks.

    Text under a mark is deleted from the content stream; images are blanked
    (``pixels``) or removed; vector art fully covered (or touched) is removed.
    """
    doc, path = _open(doc_id)
    for a in req.areas:
        _page(doc, a.page)
        if not _valid_rect(a.rect):
            doc.close()
            raise HTTPException(status_code=400, detail=f"Invalid rect: {a.rect}")
    if req.pages is not None:
        for p in req.pages:
            _page(doc, p)

    pending = _list_marks(doc, req.pages)
    if not pending and not req.areas:
        doc.close()
        raise HTTPException(status_code=400, detail="Nothing to redact: no marked areas")

    snapshot(doc_id, "Apply redactions")
    if req.areas:
        _add_marks(
            doc, req.areas, _parse_color(req.fill_color), req.overlay_text,
            _parse_color(req.overlay_text_color, (1, 1, 1)), req.overlay_font_size, None,
        )

    images = _IMAGE_MODES[req.images]
    graphics = _GRAPHICS_MODES[req.graphics]
    pages_affected = []
    applied = 0
    removed_chars = 0
    verify: dict[int, tuple[list[fitz.Rect], Counter]] = {}
    for pno in range(len(doc)):
        if req.pages is not None and pno not in req.pages:
            continue
        page = doc[pno]
        annots = list(page.annots(types=[fitz.PDF_ANNOT_REDACT]))
        if not annots:
            continue
        rects = [fitz.Rect(a.rect) for a in annots]
        # Overlay text is (legitimately) written back inside the areas, so it
        # is the only text allowed to remain there after applying.
        allowed: Counter = Counter()
        for a in annots:
            kind, val = doc.xref_get_key(a.xref, "OverlayText")
            if kind == "string":
                allowed.update(c for c in val if not c.isspace())
        removed_chars += len(_glyphs_in(page, rects))
        page.apply_redactions(images=images, graphics=graphics, text=fitz.PDF_REDACT_TEXT_REMOVE)
        applied += len(rects)
        pages_affected.append(pno)
        verify[pno] = (rects, allowed)

    # Self-verification: re-extract and confirm nothing but overlay text
    # remains inside any applied area.
    leftover = 0
    for pno, (rects, allowed) in verify.items():
        remaining = Counter(_glyphs_in(doc[pno], rects)) - allowed
        leftover += sum(remaining.values())

    _save_in_place(doc, path)
    # The snapshot taken above (and every earlier one) still contains the
    # content that was just removed. Redaction is irreversible by design, so
    # purge all of it rather than leave the secret recoverable via undo or by
    # reading uploads/<id>/history/ directly.
    purge = advanced_ops.purge_history_after_redaction(doc_id, "Apply redactions")
    return {
        "status": "ok",
        "undoable": False,
        "history_purged": True,
        "purged_files": purge["removed_files"],
        "applied": applied,
        "pages_affected": pages_affected,
        "removed_chars": removed_chars,
        "verified": leftover == 0,
        "leftover_chars": leftover,
    }


# ─── Search & redact ─────────────────────────────────────────────────────────


@router.get("/redact/presets")
async def list_presets():
    return {"presets": [{"id": k, "label": v["label"]} for k, v in PRESETS.items()]}


@router.post("/{doc_id}/redact/search")
async def search_for_redaction(doc_id: str, req: SearchRequest):
    """Find candidate redactions; returns matches for review (nothing is changed)."""
    patterns: list[tuple[str, re.Pattern, Optional[callable]]] = []
    if req.query:
        if len(req.query) > MAX_QUERY_LEN:
            raise HTTPException(status_code=400, detail="Query too long")
        flags = 0 if req.case_sensitive else re.IGNORECASE
        src = req.query if req.mode == "regex" else re.escape(req.query)
        if req.mode == "text":
            # Let any run of whitespace in the query match line breaks/gaps.
            src = re.sub(r"(\\ |\\\s|\s)+", r"\\s+", src)
        if req.whole_word:
            src = rf"(?<!\w)(?:{src})(?!\w)"
        try:
            patterns.append(("text" if req.mode == "text" else "regex", re.compile(src, flags), None))
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"Invalid regular expression: {e}")
    for p in req.presets:
        if p not in PRESETS:
            raise HTTPException(status_code=400, detail=f"Unknown preset: {p}")
        patterns.append((p, PRESETS[p]["pattern"], PRESETS[p]["validate"]))
    if not patterns:
        raise HTTPException(status_code=400, detail="Provide a query or at least one preset")

    doc, _ = _open(doc_id)
    matches = []
    truncated = False
    try:
        pages = req.pages if req.pages is not None else range(len(doc))
        for pno in pages:
            page = _page(doc, pno)
            text, boxes = _page_chars(page)
            seen: set = set()
            for kind, pat, validate in patterns:
                for m in pat.finditer(text):
                    if m.end() <= m.start():
                        continue
                    if validate and not validate(m):
                        continue
                    key = (m.start(), m.end())
                    if key in seen:
                        continue
                    rects = _rects_for_span(boxes, m.start(), m.end())
                    if not rects:
                        continue
                    seen.add(key)
                    ctx_s = max(0, m.start() - 30)
                    ctx_e = min(len(text), m.end() + 30)
                    matches.append({
                        "id": f"{pno}:{m.start()}:{m.end()}",
                        "page": pno,
                        "text": m.group(0),
                        "kind": kind,
                        "rects": [_to_visible(page, r) for r in rects],
                        "context": text[ctx_s:ctx_e].replace("\n", " "),
                    })
                    if len(matches) >= MAX_MATCHES:
                        truncated = True
                        break
                if truncated:
                    break
            if truncated:
                break
    finally:
        doc.close()
    matches.sort(key=lambda x: (x["page"], x["rects"][0][1], x["rects"][0][0]))
    return {"matches": matches, "count": len(matches), "truncated": truncated}


# ─── Security audit / sanitize ───────────────────────────────────────────────


def _hidden_text(doc: fitz.Document, limit: int = 500) -> list[dict]:
    """Find text a reader cannot see: invisible render mode, zero opacity,
    white fill, sub-1pt size, or positioned off the page."""
    found = []
    for pno, page in enumerate(doc):
        bounds = page.cropbox
        for span in page.get_texttrace():
            text = "".join(chr(c[0]) for c in span.get("chars", ()))
            if not text.strip():
                continue
            bbox = fitz.Rect(span["bbox"])
            reason = None
            if span.get("type") == 3:
                reason = "invisible"  # text render mode 3 (e.g. OCR layer)
            elif (span.get("opacity") or 1.0) <= 0.01:
                reason = "transparent"
            elif span.get("color") and all(c >= 0.98 for c in span["color"][:3]) and len(span["color"]) >= 3:
                reason = "white"
            elif span.get("size", 10) < 1.0:
                reason = "tiny"
            elif not bbox.intersects(bounds):
                reason = "off_page"
            if reason:
                found.append({
                    "page": pno,
                    "reason": reason,
                    "text": text[:120],
                    "rect": _to_visible(page, bbox),
                    "_unrotated": bbox,
                })
                if len(found) >= limit:
                    return found
    return found


_JS_ACTION_RE = re.compile(r"/S\s*/JavaScript\b|/JS\s*(?:\((?!\))|<(?!>)|\d+\s+0\s+R)")


def _javascript_xrefs(doc: fitz.Document) -> list[int]:
    """Objects that are (non-empty) JavaScript actions."""
    out = []
    for x in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(x, compressed=False)
        except Exception:
            continue
        if _JS_ACTION_RE.search(obj):
            out.append(x)
    return out


def _audit(doc: fitz.Document) -> dict:
    meta = {k: v for k, v in (doc.metadata or {}).items() if v and k not in ("format", "encryption")}
    annots: dict[str, int] = {}
    pending_redactions = 0
    links = 0
    for page in doc:
        for a in page.annots():
            t = a.type[1]
            if a.type[0] == fitz.PDF_ANNOT_REDACT:
                pending_redactions += 1
            else:
                annots[t] = annots.get(t, 0) + 1
        links += len(page.get_links())
    fields = 0
    filled = 0
    for page in doc:
        for w in page.widgets():
            fields += 1
            if w.field_value not in (None, "", "Off", False):
                filled += 1
    hidden = _hidden_text(doc)
    xmp = ""
    try:
        xmp = doc.get_xml_metadata() or ""
    except Exception:
        pass
    return {
        "encrypted": bool(doc.metadata and doc.metadata.get("encryption")),
        "encryption": (doc.metadata or {}).get("encryption"),
        "metadata": meta,
        "has_xmp_metadata": bool(xmp.strip()),
        "embedded_files": doc.embfile_names(),
        "javascript_objects": len(_javascript_xrefs(doc)),
        "annotations": annots,
        "annotation_count": sum(annots.values()),
        "pending_redactions": pending_redactions,
        "links": links,
        "form_fields": fields,
        "form_fields_filled": filled,
        "hidden_text": [{k: v for k, v in h.items() if not k.startswith("_")} for h in hidden],
        "hidden_text_count": len(hidden),
    }


def _stash_marks(doc: fitz.Document, page: fitz.Page) -> list[dict]:
    """Remove the page's pending Redact annots, returning what is needed to
    recreate them (rect, fill, overlay text, text colour/size, label)."""
    stash = []
    for annot in list(page.annots(types=[fitz.PDF_ANNOT_REDACT])):
        kind, overlay = doc.xref_get_key(annot.xref, "OverlayText")
        _, da = doc.xref_get_key(annot.xref, "DA")
        size = 11.0
        tcol = (0.0, 0.0, 0.0)
        m = re.search(r"([\d.]+)\s+Tf", da or "")
        if m:
            size = float(m.group(1))
        m = re.search(r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg", da or "")
        if m:
            tcol = tuple(float(v) for v in m.groups())
        stash.append({
            "rect": fitz.Rect(annot.rect),
            "fill": annot.colors.get("fill") or None,
            "text": overlay if kind == "string" else None,
            "size": size,
            "text_color": tcol,
            "content": annot.info.get("content") or "",
        })
        page.delete_annot(annot)
    return stash


def _restore_marks(page: fitz.Page, stash: list[dict]) -> None:
    for m in stash:
        a = page.add_redact_annot(
            m["rect"], text=m["text"], fontsize=m["size"], fill=m["fill"],
            text_color=m["text_color"], cross_out=True,
        )
        if m["content"]:
            a.set_info(content=m["content"])
            a.update()


@router.get("/{doc_id}/security/audit")
async def security_audit(doc_id: str):
    """Report hidden / sensitive information without changing anything."""
    path = _doc_path(doc_id)
    doc = fitz.open(str(path))
    try:
        if doc.needs_pass:
            return {"encrypted": True, "needs_password": True}
        result = _audit(doc)
        result["needs_password"] = False
        result["permissions"] = doc.permissions
        return result
    finally:
        doc.close()


@router.post("/{doc_id}/security/sanitize")
async def sanitize_document(doc_id: str, req: SanitizeRequest):
    doc, path = _open(doc_id)
    before = _audit(doc)
    snapshot(doc_id, "Sanitize document")
    actions: list[str] = []
    try:
        # 1. Hidden text (invisible render mode, white, transparent, tiny,
        #    off-page): true-redact those spans without touching images or
        #    vector art.
        # NOTE: PyMuPDF 1.27.1 scrub(hidden_text=True) was verified NOT to
        # remove render-mode-3 text, so invisible text is handled here too.
        wanted = set()
        if req.hidden_text:
            wanted.add("invisible")
        if req.white_text:
            wanted.update({"white", "transparent", "tiny", "off_page"})
        if wanted:
            spans = [h for h in _hidden_text(doc, limit=100000) if h["reason"] in wanted]
            by_page: dict[int, list] = {}
            for h in spans:
                by_page.setdefault(h["page"], []).append(h["_unrotated"])
            for pno, rects in by_page.items():
                page = doc[pno]
                # apply_redactions() burns in EVERY Redact annot on the page,
                # so set the user's pending marks aside and restore them after.
                stash = _stash_marks(doc, page)
                for r in rects:
                    page.add_redact_annot(r, fill=False, cross_out=False)
                page.apply_redactions(
                    images=fitz.PDF_REDACT_IMAGE_NONE,
                    graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                    text=fitz.PDF_REDACT_TEXT_REMOVE,
                )
                _restore_marks(page, stash)
            if spans:
                actions.append(f"Removed {len(spans)} hidden text span(s)")

        # 2. Comments & markup (keep pending Redact marks — those are the user's work).
        if req.annotations:
            n = 0
            for page in doc:
                for a in list(page.annots()):
                    if a.type[0] in (fitz.PDF_ANNOT_REDACT, fitz.PDF_ANNOT_LINK, fitz.PDF_ANNOT_WIDGET):
                        continue
                    page.delete_annot(a)
                    n += 1
            if n:
                actions.append(f"Removed {n} annotation(s)/comment(s)")

        # 3. PyMuPDF's built-in scrub for the rest.
        doc.scrub(
            attached_files=req.embedded_files,
            embedded_files=req.embedded_files,
            clean_pages=True,
            hidden_text=req.hidden_text,
            javascript=req.javascript,
            metadata=req.metadata,
            redactions=False,  # never silently burn in pending marks
            redact_images=0,
            remove_links=req.links,
            reset_fields=req.form_data,
            reset_responses=req.annotations,
            thumbnails=req.thumbnails,
            xml_metadata=req.xmp_metadata,
        )

        # 4. Belt-and-braces for JavaScript: scrub removes /JS actions from
        #    annotations and the document-level Names tree; also drop any
        #    catalog /OpenAction and /AA that point at JavaScript.
        if req.javascript:
            cat = doc.pdf_catalog()
            for key in ("OpenAction", "AA"):
                kind, val = doc.xref_get_key(cat, key)
                if kind != "null":
                    target = val
                    m = re.match(r"(\d+) 0 R", val or "")
                    if m:
                        target = doc.xref_object(int(m.group(1)), compressed=False)
                    if "JavaScript" in (target or "") or "/JS" in (target or ""):
                        doc.xref_set_key(cat, key, "null")
            if doc.xref_get_key(cat, "Names/JavaScript")[0] != "null":
                doc.xref_set_key(cat, "Names/JavaScript", "null")
        if req.metadata:
            doc.set_metadata({})
    except HTTPException:
        doc.close()
        raise
    except Exception as e:
        doc.close()
        raise HTTPException(status_code=500, detail=f"Sanitize failed: {e}")

    _save_in_place(doc, path)
    doc2 = fitz.open(str(path))
    try:
        after = _audit(doc2)
    finally:
        doc2.close()

    for key, label in (
        ("metadata", "metadata"), ("xmp_metadata", "XMP metadata"), ("embedded_files", "embedded files"),
        ("javascript", "JavaScript"), ("hidden_text", "invisible text"), ("form_data", "form data"),
        ("links", "links"), ("thumbnails", "thumbnails"),
    ):
        if getattr(req, key):
            actions.append(f"Removed {label}")
    # Earlier snapshots still hold the hidden data that was just removed, so
    # purge them the same way apply-redactions does.
    purge = advanced_ops.purge_history_after_redaction(doc_id, "Sanitize document")
    return {
        "status": "ok",
        "before": before,
        "after": after,
        "actions": actions,
        "undoable": False,
        "history_purged": True,
        "purged_files": purge["removed_files"],
    }


# ─── Protect / unlock ────────────────────────────────────────────────────────


def _perm_bits(perms: list[str]) -> int:
    bits = fitz.PDF_PERM_ACCESSIBILITY  # always allow screen readers
    for p in perms:
        if p not in PERMISSION_FLAGS:
            raise HTTPException(status_code=400, detail=f"Unknown permission: {p}")
        bits |= PERMISSION_FLAGS[p]
    return bits


@router.post("/{doc_id}/security/protect")
async def protect_document(doc_id: str, req: ProtectRequest):
    """AES-256 encrypt.

    apply_to_document=False (default): return an encrypted copy for download;
    the working copy stays editable. apply_to_document=True: encrypt the
    working copy in place — only allowed without an open password, otherwise
    the editor could no longer render the document.
    """
    if not req.owner_password:
        raise HTTPException(status_code=400, detail="A permissions (owner) password is required")
    if req.user_password and req.user_password == req.owner_password:
        raise HTTPException(status_code=400, detail="Open and permissions passwords must differ")
    perms = _perm_bits(req.permissions)
    doc, path = _open(doc_id)
    if req.apply_to_document and req.user_password:
        doc.close()
        raise HTTPException(
            status_code=400,
            detail="An open password cannot be applied to the working copy; download the protected copy instead.",
        )
    try:
        data = doc.tobytes(
            garbage=3,
            deflate=True,
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw=req.owner_password,
            user_pw=req.user_password or None,
            permissions=perms,
        )
    finally:
        doc.close()

    if req.apply_to_document:
        snapshot(doc_id, "Protect document (AES-256)")
        tmp = str(path) + f".{uuid.uuid4().hex}.tmp"
        Path(tmp).write_bytes(data)
        os.replace(tmp, str(path))
        return {"status": "ok", "encryption": "AES-256", "applied_to_document": True}

    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="protected.pdf"'},
    )


@router.post("/{doc_id}/security/unlock")
async def unlock_document(doc_id: str, req: UnlockRequest):
    """Remove encryption. Requires the permissions (owner) password."""
    path = _doc_path(doc_id)
    doc = fitz.open(str(path))
    if not (doc.is_encrypted or doc.needs_pass or (doc.metadata or {}).get("encryption")):
        doc.close()
        raise HTTPException(status_code=400, detail="Document is not encrypted")
    rc = doc.authenticate(req.password)
    if not rc & 4:
        doc.close()
        if rc:
            raise HTTPException(
                status_code=403,
                detail="That is the open password; removing security requires the permissions password.",
            )
        raise HTTPException(status_code=403, detail="Incorrect password")
    snapshot(doc_id, "Remove password protection")
    tmp = str(path) + f".{uuid.uuid4().hex}.tmp"
    try:
        doc.save(tmp, garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_NONE)
    finally:
        doc.close()
    os.replace(tmp, str(path))
    return {"status": "ok"}
