"""
Page organization, headers/footers/Bates, bookmarks, and comments/markup.

Every route lives under /api/pdf/{doc_id}/organize/... so it cannot collide
with routes owned by other feature modules.

COORDINATE CONVENTION (applies to every request and response in this module):
  All rectangles and points are in *visible page points*: PDF points (1/72 in)
  with a TOP-LEFT origin, measured on the page exactly as it is displayed, i.e.
  after the page's /Rotate and relative to its CropBox. That is the coordinate
  system of the rendered page image divided by the render scale
  (rendered_px / (dpi / 72)). For an unrotated, uncropped page it is identical
  to PyMuPDF's native page coordinates. The backend converts to PyMuPDF's
  unrotated page space with page.derotation_matrix, and to MediaBox space (for
  set_cropbox) by adding the current CropBox origin.

Page indexes are 0-based everywhere in this API.

Every mutating route calls snapshot(doc_id, ...) before it touches the file so
the existing /undo and /redo endpoints work.
"""

from __future__ import annotations

import io
import os
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import fitz  # PyMuPDF
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend import advanced_ops
from backend.advanced_ops import snapshot

router = APIRouter(prefix="/api/pdf")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_INSERT_BYTES = int(os.environ.get("MAX_FILE_SIZE_MB", "50")) * 1024 * 1024

PAPER_SIZES = {
    "letter": (612.0, 792.0),
    "legal": (612.0, 1008.0),
    "a4": (595.276, 841.89),
    "a3": (841.89, 1190.551),
    "a5": (419.528, 595.276),
    "tabloid": (792.0, 1224.0),
}

BASE14_FONTS = {
    "helv", "heit", "hebo", "hebi",
    "tiro", "tiit", "tibo", "tibi",
    "cour", "coit", "cobo", "cobi",
}

STAMP_NAMES = {
    name[len("STAMP_"):]: getattr(fitz, name)
    for name in dir(fitz)
    if name.startswith("STAMP_")
}

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _upload_dir() -> Path:
    # Read the attribute at call time so it always matches where snapshot() writes.
    return Path(advanced_ops.UPLOAD_DIR)


def _doc_path(doc_id: str) -> Path:
    if not _UUID_RE.match(doc_id):
        raise HTTPException(status_code=400, detail="Invalid document ID")
    path = _upload_dir() / doc_id / "original.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    return path


def _open(doc_id: str) -> tuple[fitz.Document, Path]:
    path = _doc_path(doc_id)
    return fitz.open(str(path)), path


def _save(doc: fitz.Document, path: Path) -> None:
    tmp = str(path) + ".organize.tmp"
    # garbage=1 drops unreferenced objects WITHOUT renumbering xrefs, so comment
    # ids (annotation xrefs) stay stable across saves.
    doc.save(tmp, garbage=1, deflate=True)
    doc.close()
    os.replace(tmp, str(path))


def _check_pages(doc: fitz.Document, pages: list[int]) -> list[int]:
    if not pages:
        raise HTTPException(status_code=400, detail="No pages given")
    n = len(doc)
    for p in pages:
        if p < 0 or p >= n:
            raise HTTPException(status_code=400, detail=f"Invalid page number {p}")
    # de-duplicate, keep order
    seen: set[int] = set()
    out = []
    for p in pages:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _check_page(doc: fitz.Document, page: int) -> fitz.Page:
    if page < 0 or page >= len(doc):
        raise HTTPException(status_code=400, detail=f"Invalid page number {page}")
    return doc[page]


def _vis_rect_to_page(page: fitz.Page, r) -> fitz.Rect:
    """Visible (rotated, top-left) points -> PyMuPDF unrotated page coords."""
    rect = fitz.Rect(r) * page.derotation_matrix
    rect.normalize()
    return rect


def _vis_point_to_page(page: fitz.Page, p) -> fitz.Point:
    return fitz.Point(p) * page.derotation_matrix


def _page_rect_to_vis(page: fitz.Page, r) -> fitz.Rect:
    rect = fitz.Rect(r) * page.rotation_matrix
    rect.normalize()
    return rect


def _pdf_date(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("D:%Y%m%d%H%M%SZ")


def _parse_pdf_date(s: str) -> Optional[str]:
    """'D:20261001101500Z' / "D:20261001101500+02'00'" -> ISO 8601, else None."""
    if not s:
        return None
    m = re.match(r"^D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?([Zz+\-])?(\d{2})?'?(\d{2})?'?", s)
    if not m:
        return None
    y, mo, d, h, mi, se, tz, tzh, tzm = m.groups()
    iso = f"{y}-{mo or '01'}-{d or '01'}T{h or '00'}:{mi or '00'}:{se or '00'}"
    if tz in ("Z", "z"):
        iso += "Z"
    elif tz in ("+", "-") and tzh:
        iso += f"{tz}{tzh}:{tzm or '00'}"
    return iso


def _new_doc_dir(pdf: fitz.Document) -> str:
    new_id = str(uuid.uuid4())
    d = _upload_dir() / new_id
    d.mkdir(parents=True)
    pdf.save(str(d / "original.pdf"), garbage=3, deflate=True)
    (d / "annotations.json").write_text("{}")
    return new_id


def _pdf_response(pdf: fitz.Document, filename: str) -> Response:
    data = pdf.tobytes(garbage=3, deflate=True)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─── Page operations ──────────────────────────────────────────────────────────


class InsertBlankRequest(BaseModel):
    position: int = Field(..., description="Index the new page(s) will occupy (0..page_count)")
    size: Literal["neighbor", "letter", "legal", "a4", "a3", "a5", "tabloid", "custom"] = "neighbor"
    width: Optional[float] = None
    height: Optional[float] = None
    landscape: bool = False
    count: int = 1


@router.post("/{doc_id}/organize/insert-blank")
async def insert_blank(doc_id: str, req: InsertBlankRequest):
    doc, path = _open(doc_id)
    n = len(doc)
    if req.position < 0 or req.position > n:
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid position")
    if req.count < 1 or req.count > 500:
        doc.close()
        raise HTTPException(status_code=400, detail="count must be 1..500")

    if req.size == "neighbor":
        if n == 0:
            w, h = PAPER_SIZES["letter"]
        else:
            nb = doc[req.position - 1] if req.position > 0 else doc[0]
            w, h = nb.rect.width, nb.rect.height  # visible size (respects rotation)
    elif req.size == "custom":
        if not req.width or not req.height or req.width < 36 or req.height < 36 or req.width > 14400 or req.height > 14400:
            doc.close()
            raise HTTPException(status_code=400, detail="Custom size needs width/height between 36 and 14400 points")
        w, h = req.width, req.height
    else:
        w, h = PAPER_SIZES[req.size]
    if req.landscape and req.size != "neighbor":
        w, h = max(w, h), min(w, h)

    snapshot(doc_id, f"Insert {req.count} blank page(s)")
    for i in range(req.count):
        doc.new_page(pno=req.position + i, width=w, height=h)
    page_count = len(doc)
    _save(doc, path)
    return {"status": "ok", "page_count": page_count, "inserted_at": req.position, "width": w, "height": h}


@router.post("/{doc_id}/organize/insert-file")
async def insert_file(
    doc_id: str,
    position: int = Form(...),
    file: Optional[UploadFile] = File(None),
    source_doc_id: Optional[str] = Form(None),
    page_from: Optional[int] = Form(None),
    page_to: Optional[int] = Form(None),
):
    """Insert pages [page_from..page_to] (0-based, inclusive) of another PDF at `position`.

    The other PDF is either uploaded as `file` or referenced by `source_doc_id`
    (an already-uploaded document)."""
    path = _doc_path(doc_id)
    if file is None and not source_doc_id:
        raise HTTPException(status_code=400, detail="Provide a file or source_doc_id")
    try:
        if file is not None:
            data = await file.read()
            if len(data) > MAX_INSERT_BYTES:
                raise HTTPException(status_code=413, detail="File too large")
            src = fitz.open(stream=data, filetype="pdf")
        else:
            src = fitz.open(str(_doc_path(source_doc_id)))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Could not read the source PDF")

    if src.needs_pass:
        src.close()
        raise HTTPException(status_code=400, detail="Source PDF is password protected")
    sn = len(src)
    pf = 0 if page_from is None else page_from
    pt = sn - 1 if page_to is None else page_to
    if sn == 0 or pf < 0 or pt >= sn or pf > pt:
        src.close()
        raise HTTPException(status_code=400, detail="Invalid source page range")

    doc = fitz.open(str(path))
    if position < 0 or position > len(doc):
        doc.close()
        src.close()
        raise HTTPException(status_code=400, detail="Invalid position")

    snapshot(doc_id, f"Insert {pt - pf + 1} page(s) from file")
    doc.insert_pdf(src, from_page=pf, to_page=pt, start_at=position, annots=True)
    src.close()
    page_count = len(doc)
    _save(doc, path)
    return {"status": "ok", "page_count": page_count, "inserted": pt - pf + 1, "inserted_at": position}


class PagesRequest(BaseModel):
    pages: list[int]


class ExtractRequest(PagesRequest):
    delete_after: bool = False
    filename: str = "extracted.pdf"


@router.post("/{doc_id}/organize/extract")
async def extract_pages(doc_id: str, req: ExtractRequest):
    """Return a new PDF (download) containing `pages` in the given order."""
    doc, path = _open(doc_id)
    pages = _check_pages(doc, req.pages)
    out = fitz.open()
    for p in pages:
        out.insert_pdf(doc, from_page=p, to_page=p, annots=True)
    safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", os.path.basename(req.filename)) or "extracted.pdf"
    if not safe_name.lower().endswith(".pdf"):
        safe_name += ".pdf"
    resp = _pdf_response(out, safe_name)
    out.close()

    if req.delete_after:
        if len(pages) >= len(doc):
            doc.close()
            raise HTTPException(status_code=400, detail="Cannot delete every page of the document")
        snapshot(doc_id, f"Extract and delete {len(pages)} page(s)")
        doc.delete_pages(sorted(pages))
        _save(doc, path)
    else:
        doc.close()
    return resp


@router.post("/{doc_id}/organize/duplicate")
async def duplicate_pages(doc_id: str, req: PagesRequest):
    """Insert a copy of each page directly after the original (annotations included)."""
    doc, path = _open(doc_id)
    pages = _check_pages(doc, req.pages)
    src = fitz.open(stream=doc.tobytes(), filetype="pdf")
    snapshot(doc_id, f"Duplicate {len(pages)} page(s)")
    # Descending so earlier insertions don't shift later original indexes.
    for p in sorted(pages, reverse=True):
        doc.insert_pdf(src, from_page=p, to_page=p, start_at=p + 1, annots=True)
    src.close()
    page_count = len(doc)
    _save(doc, path)
    return {"status": "ok", "page_count": page_count}


class RotateRequest(PagesRequest):
    angle: int = 90
    relative: bool = True


@router.post("/{doc_id}/organize/rotate")
async def rotate_pages(doc_id: str, req: RotateRequest):
    if req.angle % 90 != 0:
        raise HTTPException(status_code=400, detail="angle must be a multiple of 90")
    doc, path = _open(doc_id)
    pages = _check_pages(doc, req.pages)
    snapshot(doc_id, f"Rotate {len(pages)} page(s)")
    result = {}
    for p in pages:
        page = doc[p]
        new_rot = ((page.rotation if req.relative else 0) + req.angle) % 360
        page.set_rotation(new_rot)
        result[p] = new_rot
    _save(doc, path)
    return {"status": "ok", "rotations": result}


@router.post("/{doc_id}/organize/delete")
async def delete_pages(doc_id: str, req: PagesRequest):
    doc, path = _open(doc_id)
    pages = _check_pages(doc, req.pages)
    if len(pages) >= len(doc):
        doc.close()
        raise HTTPException(status_code=400, detail="Cannot delete every page of the document")
    snapshot(doc_id, f"Delete {len(pages)} page(s)")
    doc.delete_pages(sorted(pages))
    page_count = len(doc)
    _save(doc, path)
    return {"status": "ok", "page_count": page_count}


class Margins(BaseModel):
    top: float = 0
    right: float = 0
    bottom: float = 0
    left: float = 0


class CropRequest(PagesRequest):
    mode: Literal["box", "margins", "auto", "reset"] = "box"
    box: Optional[list[float]] = None  # visible points [x0, y0, x1, y1]
    margins: Optional[Margins] = None  # visible points trimmed from each visible edge
    padding: float = 0  # auto mode: points of white space to keep around content
    threshold: int = 250  # auto mode: gray level (0-255) at/above which a pixel counts as white


def _auto_content_rect(page: fitz.Page, threshold: int) -> Optional[fitz.Rect]:
    """Visible-coordinate bounding box of non-white pixels, or None if blank."""
    import numpy as np

    zoom = 2.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width]
    mask = arr < threshold
    if not mask.any():
        return None
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    return fitz.Rect(cols[0] / zoom, rows[0] / zoom, (cols[-1] + 1) / zoom, (rows[-1] + 1) / zoom)


def _set_visible_crop(page: fitz.Page, vis: fitz.Rect) -> fitz.Rect:
    """Crop to `vis` (visible coords). Returns the new CropBox (MediaBox coords)."""
    vis = vis & page.rect  # can only shrink the visible area
    if vis.is_empty or vis.width < 9 or vis.height < 9:
        raise HTTPException(status_code=400, detail="Crop box is too small or outside the page")
    unrot = _vis_rect_to_page(page, vis)
    cb = page.cropbox
    target = fitz.Rect(unrot.x0 + cb.x0, unrot.y0 + cb.y0, unrot.x1 + cb.x0, unrot.y1 + cb.y0)
    target = target & page.mediabox
    page.set_cropbox(target)
    return page.cropbox


@router.post("/{doc_id}/organize/crop")
async def crop_pages(doc_id: str, req: CropRequest):
    doc, path = _open(doc_id)
    pages = _check_pages(doc, req.pages)
    if req.mode == "box" and (not req.box or len(req.box) != 4):
        doc.close()
        raise HTTPException(status_code=400, detail="box mode needs box=[x0,y0,x1,y1]")
    if req.mode == "margins" and req.margins is None:
        doc.close()
        raise HTTPException(status_code=400, detail="margins mode needs margins")

    snapshot(doc_id, f"Crop {len(pages)} page(s) ({req.mode})")
    results = []
    try:
        for p in pages:
            page = doc[p]
            if req.mode == "reset":
                page.set_cropbox(page.mediabox)
                results.append({"page": p, "cropbox": list(page.cropbox)})
                continue
            if req.mode == "box":
                vis = fitz.Rect(req.box)
            elif req.mode == "margins":
                m = req.margins
                r = page.rect
                vis = fitz.Rect(m.left, m.top, r.width - m.right, r.height - m.bottom)
            else:  # auto
                content = _auto_content_rect(page, req.threshold)
                if content is None:
                    results.append({"page": p, "cropbox": list(page.cropbox), "skipped": "blank page"})
                    continue
                pad = max(0.0, req.padding)
                vis = fitz.Rect(content.x0 - pad, content.y0 - pad, content.x1 + pad, content.y1 + pad)
            cb = _set_visible_crop(page, vis)
            results.append({"page": p, "cropbox": list(cb), "width": page.rect.width, "height": page.rect.height})
    except HTTPException:
        doc.close()
        raise
    _save(doc, path)
    return {"status": "ok", "pages": results}


class ResizeRequest(PagesRequest):
    size: Literal["letter", "legal", "a4", "a3", "a5", "tabloid", "custom"] = "letter"
    width: Optional[float] = None
    height: Optional[float] = None
    match_orientation: bool = True  # landscape source -> landscape target
    scale_content: bool = True  # False: keep content at 100% and center it


@router.post("/{doc_id}/organize/resize")
async def resize_pages(doc_id: str, req: ResizeRequest):
    """Change page size. Content is scaled to fit (keeping proportions) or centered at 100%.

    Implementation: each target page is rebuilt as a new page that shows the
    original page as a Form XObject (vector content preserved, text still
    selectable). Annotations on resized pages are re-created by insert_pdf where
    possible; see the module report for limits."""
    if req.size == "custom":
        if not req.width or not req.height or req.width < 36 or req.height < 36:
            raise HTTPException(status_code=400, detail="custom size needs width and height >= 36")
        tw, th = req.width, req.height
    else:
        tw, th = PAPER_SIZES[req.size]

    doc, path = _open(doc_id)
    pages = _check_pages(doc, req.pages)
    toc = doc.get_toc(simple=False)
    src = fitz.open(stream=doc.tobytes(), filetype="pdf")
    snapshot(doc_id, f"Resize {len(pages)} page(s)")
    for p in sorted(pages):
        sp = src[p]
        sw, sh = sp.rect.width, sp.rect.height  # visible size
        w, h = tw, th
        if req.match_orientation and (sw > sh) != (w > h):
            w, h = h, w
        newp = doc.new_page(pno=p, width=w, height=h)
        if req.scale_content:
            target = newp.rect
        else:
            x0 = (w - sw) / 2
            y0 = (h - sh) / 2
            target = fitz.Rect(x0, y0, x0 + sw, y0 + sh)
        newp.show_pdf_page(target, src, p, keep_proportion=True)
        doc.delete_page(p + 1)
    src.close()
    try:
        doc.set_toc(toc)
    except Exception:
        pass
    _save(doc, path)
    return {"status": "ok", "width": tw, "height": th}


class SplitRequest(BaseModel):
    mode: Literal["every_n", "bookmarks", "size"]
    n: int = 1
    level: int = 1  # bookmarks mode: outline level to split at
    max_mb: float = 10.0  # size mode
    download: bool = False  # True -> return a zip instead of new document ids


def _split_chunks(doc: fitz.Document, req: SplitRequest) -> list[tuple[str, list[int]]]:
    n = len(doc)
    if req.mode == "every_n":
        if req.n < 1:
            raise HTTPException(status_code=400, detail="n must be >= 1")
        return [(f"pages_{s + 1}-{min(s + req.n, n)}", list(range(s, min(s + req.n, n)))) for s in range(0, n, req.n)]

    if req.mode == "bookmarks":
        starts: list[tuple[int, str]] = []
        for lvl, title, pg in doc.get_toc(simple=True):
            if lvl == req.level and 1 <= pg <= n:
                if not starts or starts[-1][0] != pg - 1:
                    starts.append((pg - 1, title))
        if not starts:
            raise HTTPException(status_code=400, detail=f"No bookmarks at level {req.level}")
        starts.sort()
        chunks = []
        if starts[0][0] > 0:
            chunks.append(("front_matter", list(range(0, starts[0][0]))))
        for i, (s, title) in enumerate(starts):
            e = starts[i + 1][0] if i + 1 < len(starts) else n
            if e > s:
                chunks.append((title, list(range(s, e))))
        return chunks

    # size
    limit = req.max_mb * 1024 * 1024
    if limit < 1024:
        raise HTTPException(status_code=400, detail="max_mb too small")
    chunks = []
    cur: list[int] = []
    for p in range(n):
        trial = cur + [p]
        tmp = fitz.open()
        tmp.insert_pdf(doc, from_page=trial[0], to_page=trial[-1])
        size = len(tmp.tobytes(garbage=3, deflate=True))
        tmp.close()
        if size > limit and cur:
            chunks.append((f"part_{len(chunks) + 1}", cur))
            cur = [p]
        else:
            cur = trial
    if cur:
        chunks.append((f"part_{len(chunks) + 1}", cur))
    return chunks


@router.post("/{doc_id}/organize/split")
async def split_document(doc_id: str, req: SplitRequest):
    """Split into several new documents. The source document is not modified."""
    doc, _ = _open(doc_id)
    try:
        chunks = _split_chunks(doc, req)
    except HTTPException:
        doc.close()
        raise

    parts = []
    zbuf = io.BytesIO()
    zf = zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) if req.download else None
    for idx, (name, pages) in enumerate(chunks):
        part = fitz.open()
        part.insert_pdf(doc, from_page=pages[0], to_page=pages[-1], annots=True)
        safe = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip() or f"part_{idx + 1}"
        filename = f"{idx + 1:02d}_{safe[:60]}.pdf"
        if zf is not None:
            zf.writestr(filename, part.tobytes(garbage=3, deflate=True))
            parts.append({"filename": filename, "pages": pages})
        else:
            new_id = _new_doc_dir(part)
            parts.append({
                "id": new_id,
                "filename": filename,
                "pages": pages,
                "page_count": len(pages),
                "size": (_upload_dir() / new_id / "original.pdf").stat().st_size,
            })
        part.close()
    doc.close()
    if zf is not None:
        zf.close()
        return Response(
            content=zbuf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="split.zip"'},
        )
    return {"status": "ok", "documents": parts}


# ─── Headers / footers / page numbers / Bates ────────────────────────────────


class HeaderFooterRequest(BaseModel):
    header_left: str = ""
    header_center: str = ""
    header_right: str = ""
    footer_left: str = ""
    footer_center: str = ""
    footer_right: str = ""
    font: str = "helv"
    font_size: float = 10
    color: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    margin_top: float = 36
    margin_bottom: float = 36
    margin_left: float = 72
    margin_right: float = 72
    start_number: int = 1
    skip_first: bool = False
    pages: Optional[list[int]] = None  # restrict to these pages (0-based); default all
    bates_prefix: str = ""
    bates_suffix: str = ""
    bates_digits: int = 6
    bates_start: int = 1
    date_format: str = "%m/%d/%Y"


def format_token_text(template: str, *, n: int, total: int, bates: str, date: str) -> str:
    """Expand {n} {total} {bates} {date}. Unknown {tokens} are left as-is."""
    return (
        template.replace("{n}", str(n))
        .replace("{total}", str(total))
        .replace("{bates}", bates)
        .replace("{date}", date)
    )


def _plan_header_footer(req: HeaderFooterRequest, page_count: int) -> list[dict]:
    """Which pages get stamped and with which values. Pure: no PDF needed."""
    targets = sorted(set(req.pages)) if req.pages is not None else list(range(page_count))
    targets = [p for p in targets if 0 <= p < page_count]
    if req.skip_first and targets:
        targets = targets[1:]
    total = req.start_number + len(targets) - 1
    date = datetime.now().strftime(req.date_format)
    plan = []
    for i, p in enumerate(targets):
        n = req.start_number + i
        bates = f"{req.bates_prefix}{str(req.bates_start + i).zfill(max(1, req.bates_digits))}{req.bates_suffix}"
        texts = {}
        for slot in ("header_left", "header_center", "header_right", "footer_left", "footer_center", "footer_right"):
            tpl = getattr(req, slot)
            if tpl:
                texts[slot] = format_token_text(tpl, n=n, total=total, bates=bates, date=date)
        plan.append({"page": p, "n": n, "total": total, "bates": bates, "texts": texts})
    return plan


def _validate_hf(req: HeaderFooterRequest):
    if req.font not in BASE14_FONTS:
        raise HTTPException(status_code=400, detail=f"font must be one of {sorted(BASE14_FONTS)}")
    if not (4 <= req.font_size <= 72):
        raise HTTPException(status_code=400, detail="font_size must be 4..72")
    if len(req.color) != 3 or any(c < 0 or c > 1 for c in req.color):
        raise HTTPException(status_code=400, detail="color must be [r,g,b] in 0..1")
    if not (1 <= req.bates_digits <= 12):
        raise HTTPException(status_code=400, detail="bates_digits must be 1..12")
    try:
        datetime.now().strftime(req.date_format)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date_format")


def _stamp_header_footer(doc: fitz.Document, req: HeaderFooterRequest) -> list[dict]:
    plan = _plan_header_footer(req, len(doc))
    fs = req.font_size
    for item in plan:
        page = doc[item["page"]]
        W, H = page.rect.width, page.rect.height  # visible size
        for slot, text in item["texts"].items():
            tw = fitz.get_text_length(text, fontname=req.font, fontsize=fs)
            if slot.startswith("header"):
                y = req.margin_top + fs * 0.8  # baseline: top margin to cap-height
            else:
                y = H - req.margin_bottom
            if slot.endswith("left"):
                x = req.margin_left
            elif slot.endswith("center"):
                x = (W - tw) / 2
            else:
                x = W - req.margin_right - tw
            pt = _vis_point_to_page(page, (x, y))
            page.insert_text(pt, text, fontsize=fs, fontname=req.font, color=req.color, rotate=page.rotation)
    return plan


@router.post("/{doc_id}/organize/header-footer/preview")
async def header_footer_preview(doc_id: str, req: HeaderFooterRequest):
    _validate_hf(req)
    doc, _ = _open(doc_id)
    n = len(doc)
    doc.close()
    plan = _plan_header_footer(req, n)
    return {"pages": plan[:5], "stamped_count": len(plan)}


@router.post("/{doc_id}/organize/header-footer")
async def add_header_footer(doc_id: str, req: HeaderFooterRequest):
    _validate_hf(req)
    doc, path = _open(doc_id)
    snapshot(doc_id, "Add header/footer")
    plan = _stamp_header_footer(doc, req)
    _save(doc, path)
    return {"status": "ok", "stamped_count": len(plan), "first": plan[0] if plan else None}


Position = Literal["top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right"]


def _slot_for(position: str) -> str:
    v, h = position.split("-")
    return f"{'header' if v == 'top' else 'footer'}_{h}"


class PageNumbersRequest(BaseModel):
    format: str = "Page {n} of {total}"
    position: Position = "bottom-center"
    font: str = "helv"
    font_size: float = 10
    color: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    margin_top: float = 36
    margin_bottom: float = 36
    margin_left: float = 72
    margin_right: float = 72
    start_number: int = 1
    skip_first: bool = False
    pages: Optional[list[int]] = None


@router.post("/{doc_id}/organize/page-numbers")
async def add_page_numbers(doc_id: str, req: PageNumbersRequest):
    hf = HeaderFooterRequest(**req.model_dump(exclude={"format", "position"}), **{_slot_for(req.position): req.format})
    return await add_header_footer(doc_id, hf)


class BatesRequest(BaseModel):
    prefix: str = ""
    suffix: str = ""
    digits: int = 6
    start: int = 1
    position: Position = "bottom-right"
    font: str = "helv"
    font_size: float = 10
    color: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    margin_top: float = 36
    margin_bottom: float = 36
    margin_left: float = 72
    margin_right: float = 72
    skip_first: bool = False
    pages: Optional[list[int]] = None


@router.post("/{doc_id}/organize/bates")
async def add_bates(doc_id: str, req: BatesRequest):
    data = req.model_dump(exclude={"prefix", "suffix", "digits", "start", "position"})
    hf = HeaderFooterRequest(
        **data,
        bates_prefix=req.prefix,
        bates_suffix=req.suffix,
        bates_digits=req.digits,
        bates_start=req.start,
        **{_slot_for(req.position): "{bates}"},
    )
    result = await add_header_footer(doc_id, hf)
    first = result.get("first")
    return {**result, "first_bates": first["bates"] if first else None}


# ─── Bookmarks / outline ─────────────────────────────────────────────────────


def _bookmarks_payload(doc: fitz.Document) -> list[dict]:
    return [
        {"index": i, "level": lvl, "title": title, "page": pg - 1}
        for i, (lvl, title, pg, *_rest) in enumerate(doc.get_toc(simple=False))
    ]


def _validate_toc_levels(toc: list) -> None:
    prev = 0
    for i, item in enumerate(toc):
        lvl = item[0]
        if lvl < 1 or (i == 0 and lvl != 1) or lvl > prev + 1:
            raise HTTPException(status_code=400, detail=f"Bad outline level at item {i}: levels must start at 1 and increase by at most 1")
        prev = lvl


def _apply_toc(doc: fitz.Document, toc: list) -> None:
    _validate_toc_levels(toc)
    n = len(doc)
    for item in toc:
        if not (-1 <= item[2] - 1 < n) or item[2] == 0:
            raise HTTPException(status_code=400, detail=f"Bookmark page out of range: {item[2] - 1}")
    doc.set_toc(toc)


@router.get("/{doc_id}/organize/bookmarks")
async def list_bookmarks(doc_id: str):
    doc, _ = _open(doc_id)
    items = _bookmarks_payload(doc)
    doc.close()
    return {"bookmarks": items}


class BookmarkItem(BaseModel):
    level: int = 1
    title: str
    page: int  # 0-based target page


class ReplaceBookmarksRequest(BaseModel):
    bookmarks: list[BookmarkItem]


@router.put("/{doc_id}/organize/bookmarks")
async def replace_bookmarks(doc_id: str, req: ReplaceBookmarksRequest):
    doc, path = _open(doc_id)
    toc = [[b.level, b.title, b.page + 1] for b in req.bookmarks]
    try:
        _validate_toc_levels(toc)
        snapshot(doc_id, "Replace bookmarks")
        _apply_toc(doc, toc)
    except HTTPException:
        doc.close()
        raise
    items = _bookmarks_payload(doc)
    _save(doc, path)
    return {"status": "ok", "bookmarks": items}


class AddBookmarkRequest(BookmarkItem):
    index: Optional[int] = None  # insert position in the flat list; default append


@router.post("/{doc_id}/organize/bookmarks")
async def add_bookmark(doc_id: str, req: AddBookmarkRequest):
    doc, path = _open(doc_id)
    toc = doc.get_toc(simple=False)
    idx = len(toc) if req.index is None else max(0, min(req.index, len(toc)))
    toc.insert(idx, [req.level, req.title, req.page + 1])
    try:
        _check_page(doc, req.page)
        _validate_toc_levels(toc)
        snapshot(doc_id, f"Add bookmark '{req.title}'")
        _apply_toc(doc, toc)
    except HTTPException:
        doc.close()
        raise
    items = _bookmarks_payload(doc)
    _save(doc, path)
    return {"status": "ok", "bookmarks": items}


class UpdateBookmarkRequest(BaseModel):
    title: Optional[str] = None
    page: Optional[int] = None
    level: Optional[int] = None


@router.patch("/{doc_id}/organize/bookmarks/{index}")
async def update_bookmark(doc_id: str, index: int, req: UpdateBookmarkRequest):
    doc, path = _open(doc_id)
    toc = doc.get_toc(simple=False)
    if index < 0 or index >= len(toc):
        doc.close()
        raise HTTPException(status_code=404, detail="Bookmark not found")
    item = list(toc[index])
    if req.title is not None:
        item[1] = req.title
    if req.level is not None:
        item[0] = req.level
    if req.page is not None:
        try:
            _check_page(doc, req.page)
        except HTTPException:
            doc.close()
            raise
        item = [item[0], item[1], req.page + 1]  # drop old destination details
    toc[index] = item
    try:
        _validate_toc_levels(toc)
        snapshot(doc_id, "Edit bookmark")
        _apply_toc(doc, toc)
    except HTTPException:
        doc.close()
        raise
    items = _bookmarks_payload(doc)
    _save(doc, path)
    return {"status": "ok", "bookmarks": items}


@router.delete("/{doc_id}/organize/bookmarks/{index}")
async def delete_bookmark(doc_id: str, index: int):
    """Delete a bookmark and all its children."""
    doc, path = _open(doc_id)
    toc = doc.get_toc(simple=False)
    if index < 0 or index >= len(toc):
        doc.close()
        raise HTTPException(status_code=404, detail="Bookmark not found")
    lvl = toc[index][0]
    end = index + 1
    while end < len(toc) and toc[end][0] > lvl:
        end += 1
    removed = end - index
    del toc[index:end]
    snapshot(doc_id, "Delete bookmark")
    doc.set_toc(toc)
    items = _bookmarks_payload(doc)
    _save(doc, path)
    return {"status": "ok", "removed": removed, "bookmarks": items}


# ─── Comments / markup (real PDF annotations) ────────────────────────────────

_SKIP_TYPES = {fitz.PDF_ANNOT_LINK, fitz.PDF_ANNOT_WIDGET, fitz.PDF_ANNOT_POPUP}
_TEXT_MARKUP = {
    "highlight": "add_highlight_annot",
    "underline": "add_underline_annot",
    "strikeout": "add_strikeout_annot",
    "squiggly": "add_squiggly_annot",
}
REVIEW_STATES = {"Accepted", "Rejected", "Cancelled", "Completed", "None"}

CommentType = Literal[
    "note", "highlight", "underline", "strikeout", "squiggly",
    "freetext", "callout", "rect", "ellipse", "line", "arrow",
    "polygon", "polyline", "stamp", "ink",
]


class CommentCreate(BaseModel):
    page: int
    type: CommentType
    text: str = ""
    author: str = "User"
    subject: Optional[str] = None
    # geometry — all in visible page points (see module docstring)
    rect: Optional[list[float]] = None  # note (top-left used), freetext/callout box, rect, ellipse, stamp
    quads: Optional[list[list[float]]] = None  # text markup: list of [x0,y0,x1,y1]
    area: Optional[list[float]] = None  # text markup: mark all words inside this box
    search: Optional[str] = None  # text markup: mark every occurrence of this text
    points: Optional[list[list[float]]] = None  # line/arrow (2 points), polygon/polyline, ink
    callout: Optional[list[list[float]]] = None  # callout: 2 or 3 points, first = arrow tip
    # style
    color: Optional[list[float]] = None  # stroke / text color, rgb 0..1
    fill: Optional[list[float]] = None
    opacity: Optional[float] = None
    width: float = 1.5
    font_size: float = 12
    stamp: str = "Approved"
    icon: str = "Note"


def _annot_status(doc: fitz.Document, states: list[fitz.Annot]) -> Optional[str]:
    if not states:
        return None
    last = max(states, key=lambda a: a.xref)
    raw = doc.xref_get_key(last.xref, "State")
    return raw[1].strip("()") if raw[0] != "null" else None


def _annot_kind(annot: fitz.Annot) -> str:
    t = annot.type[0]
    mapping = {
        fitz.PDF_ANNOT_TEXT: "note",
        fitz.PDF_ANNOT_HIGHLIGHT: "highlight",
        fitz.PDF_ANNOT_UNDERLINE: "underline",
        fitz.PDF_ANNOT_STRIKE_OUT: "strikeout",
        fitz.PDF_ANNOT_SQUIGGLY: "squiggly",
        fitz.PDF_ANNOT_FREE_TEXT: "freetext",
        fitz.PDF_ANNOT_SQUARE: "rect",
        fitz.PDF_ANNOT_CIRCLE: "ellipse",
        fitz.PDF_ANNOT_LINE: "line",
        fitz.PDF_ANNOT_POLYGON: "polygon",
        fitz.PDF_ANNOT_POLY_LINE: "polyline",
        fitz.PDF_ANNOT_STAMP: "stamp",
        fitz.PDF_ANNOT_INK: "ink",
    }
    kind = mapping.get(t, annot.type[1].lower())
    if kind == "line":
        le = annot.line_ends
        if le and (le[0] or le[1]):
            kind = "arrow"
    return kind


def _is_state_annot(doc: fitz.Document, annot: fitz.Annot) -> bool:
    return doc.xref_get_key(annot.xref, "StateModel")[0] != "null"


def _annot_dict(doc: fitz.Document, page: fitz.Page, annot: fitz.Annot) -> dict:
    info = annot.info
    colors = annot.colors or {}
    return {
        "id": annot.xref,
        "page": page.number,
        "type": _annot_kind(annot),
        "pdf_subtype": annot.type[1],
        "author": info.get("title", ""),
        "contents": info.get("content", ""),
        "subject": info.get("subject", ""),
        "created": _parse_pdf_date(info.get("creationDate", "")),
        "modified": _parse_pdf_date(info.get("modDate", "")),
        "color": list(colors.get("stroke") or []) or None,
        "fill": list(colors.get("fill") or []) or None,
        "opacity": annot.opacity if annot.opacity is not None and annot.opacity >= 0 else 1.0,
        "rect": [round(v, 2) for v in _page_rect_to_vis(page, annot.rect)],
        "in_reply_to": annot.irt_xref or None,
    }


def _collect_page_annots(doc: fitz.Document, page: fitz.Page) -> list[fitz.Annot]:
    return [a for a in page.annots() if a.type[0] not in _SKIP_TYPES]


def _find_annot(doc: fitz.Document, xref: int) -> tuple[fitz.Page, fitz.Annot]:
    for page in doc:
        for a in page.annots():
            if a.xref == xref:
                return page, a
    raise HTTPException(status_code=404, detail="Comment not found")


def _root_of(xref: int, irt: dict[int, int]) -> int:
    seen = set()
    while xref in irt and irt[xref] and xref not in seen:
        seen.add(xref)
        xref = irt[xref]
    return xref


@router.get("/{doc_id}/organize/comments")
async def list_comments(doc_id: str):
    """All comments, threaded. Replies are nested under their root comment;
    review-state annotations are folded into `status`."""
    doc, _ = _open(doc_id)
    roots: list[dict] = []
    for page in doc:
        annots = _collect_page_annots(doc, page)
        by_xref = {a.xref: a for a in annots}
        irt = {a.xref: (a.irt_xref or 0) for a in annots}
        states: dict[int, list] = {}
        replies: dict[int, list] = {}
        page_roots = []
        for a in annots:
            parent = irt[a.xref]
            if parent and _is_state_annot(doc, a):
                states.setdefault(parent, []).append(a)
            elif parent and parent in by_xref:
                replies.setdefault(_root_of(a.xref, irt), []).append(_annot_dict(doc, page, a))
            else:
                page_roots.append(a)
        for a in page_roots:
            d = _annot_dict(doc, page, a)
            d["status"] = _annot_status(doc, states.get(a.xref, []))
            d["replies"] = sorted(replies.get(a.xref, []), key=lambda r: (r["created"] or "", r["id"]))
            roots.append(d)
    doc.close()
    return {"comments": roots, "count": len(roots)}


def _words_in_area(page: fitz.Page, area: fitz.Rect) -> list[fitz.Quad]:
    lines: dict[tuple[int, int], fitz.Rect] = {}
    for x0, y0, x1, y1, _w, b, ln, _wn in page.get_text("words"):
        r = fitz.Rect(x0, y0, x1, y1)
        c = fitz.Point((x0 + x1) / 2, (y0 + y1) / 2)
        if c in area:
            key = (b, ln)
            lines[key] = lines[key] | r if key in lines else r
    return [lines[k].quad for k in sorted(lines)]


def _style(annot: fitz.Annot, *, color=None, fill=None, opacity=None, width=None):
    if color is not None or fill is not None:
        colors = {}
        if color is not None:
            colors["stroke"] = color
        if fill is not None:
            colors["fill"] = fill
        annot.set_colors(**colors)
    if opacity is not None:
        annot.set_opacity(max(0.0, min(1.0, opacity)))
    if width is not None and annot.type[0] not in (fitz.PDF_ANNOT_TEXT, fitz.PDF_ANNOT_STAMP,
                                                   fitz.PDF_ANNOT_HIGHLIGHT, fitz.PDF_ANNOT_UNDERLINE,
                                                   fitz.PDF_ANNOT_STRIKE_OUT, fitz.PDF_ANNOT_SQUIGGLY):
        annot.set_border(width=width)


def _need(v, msg):
    if not v:
        raise HTTPException(status_code=400, detail=msg)
    return v


def _create_annot(page: fitz.Page, req: CommentCreate) -> fitz.Annot:
    t = req.type
    color = req.color

    if t in _TEXT_MARKUP:
        quads: list = []
        if req.quads:
            quads = [_vis_rect_to_page(page, q).quad for q in req.quads]
        elif req.area:
            quads = _words_in_area(page, _vis_rect_to_page(page, req.area))
        elif req.search:
            quads = page.search_for(req.search, quads=True)
        if not quads:
            raise HTTPException(status_code=400, detail="No text found to mark up")
        annot = getattr(page, _TEXT_MARKUP[t])(quads)
        default = {"highlight": [1, 0.92, 0.23], "underline": [0, 0.5, 0], "strikeout": [0.85, 0, 0], "squiggly": [0.2, 0.4, 1]}[t]
        _style(annot, color=color or default, opacity=req.opacity)
        return annot

    if t == "note":
        r = _need(req.rect or (req.points[0] if req.points else None), "note needs rect or points")
        pt = _vis_point_to_page(page, (r[0], r[1]))
        annot = page.add_text_annot(pt, req.text, icon=req.icon or "Note")
        _style(annot, color=color or [1, 0.85, 0.2], opacity=req.opacity)
        return annot

    if t in ("freetext", "callout"):
        r = _vis_rect_to_page(page, _need(req.rect, f"{t} needs rect"))
        callout = None
        if t == "callout":
            pts = _need(req.callout, "callout needs callout points")
            if len(pts) not in (2, 3):
                raise HTTPException(status_code=400, detail="callout needs 2 or 3 points")
            callout = [_vis_point_to_page(page, p) for p in pts]
        annot = page.add_freetext_annot(
            r, req.text or " ",
            fontsize=req.font_size,
            text_color=color or [0, 0, 0],
            fill_color=req.fill,
            # NB: PyMuPDF 1.27 rejects border_color unless rich_text=True, so the
            # callout leader line/border are drawn in the text color instead.
            border_width=req.width if t == "callout" else 0,
            callout=callout,
            line_end=fitz.PDF_ANNOT_LE_OPEN_ARROW,
            opacity=1 if req.opacity is None else req.opacity,
            rotate=page.rotation,
        )
        return annot

    if t in ("rect", "ellipse"):
        r = _vis_rect_to_page(page, _need(req.rect, f"{t} needs rect"))
        annot = page.add_rect_annot(r) if t == "rect" else page.add_circle_annot(r)
        _style(annot, color=color or [0.85, 0, 0], fill=req.fill, opacity=req.opacity, width=req.width)
        return annot

    if t in ("line", "arrow"):
        pts = _need(req.points, f"{t} needs 2 points")
        if len(pts) != 2:
            raise HTTPException(status_code=400, detail=f"{t} needs exactly 2 points")
        annot = page.add_line_annot(_vis_point_to_page(page, pts[0]), _vis_point_to_page(page, pts[1]))
        if t == "arrow":
            annot.set_line_ends(fitz.PDF_ANNOT_LE_NONE, fitz.PDF_ANNOT_LE_OPEN_ARROW)
        _style(annot, color=color or [0.85, 0, 0], opacity=req.opacity, width=req.width)
        return annot

    if t in ("polygon", "polyline"):
        pts = _need(req.points, f"{t} needs points")
        if len(pts) < (3 if t == "polygon" else 2):
            raise HTTPException(status_code=400, detail="not enough points")
        conv = [_vis_point_to_page(page, p) for p in pts]
        annot = page.add_polygon_annot(conv) if t == "polygon" else page.add_polyline_annot(conv)
        _style(annot, color=color or [0.85, 0, 0], fill=req.fill if t == "polygon" else None,
               opacity=req.opacity, width=req.width)
        return annot

    if t == "ink":
        pts = _need(req.points, "ink needs points")
        annot = page.add_ink_annot([[tuple(_vis_point_to_page(page, p)) for p in pts]])
        _style(annot, color=color or [0, 0, 0], opacity=req.opacity, width=req.width)
        return annot

    if t == "stamp":
        r = _vis_rect_to_page(page, _need(req.rect, "stamp needs rect"))
        if req.stamp not in STAMP_NAMES:
            raise HTTPException(status_code=400, detail=f"stamp must be one of {sorted(STAMP_NAMES)}")
        annot = page.add_stamp_annot(r, stamp=STAMP_NAMES[req.stamp])
        if req.opacity is not None:
            annot.set_opacity(req.opacity)
        return annot

    raise HTTPException(status_code=400, detail=f"Unsupported type {t}")


def _finish(annot: fitz.Annot, *, author: str, content: Optional[str], subject: Optional[str], new: bool):
    now = _pdf_date()
    info = {"title": author, "modDate": now}
    if content is not None:
        info["content"] = content
    if subject is not None:
        info["subject"] = subject
    if new:
        info["creationDate"] = now
        # /NM (unique name) is already assigned by PyMuPDF on creation.
    annot.set_info(**info)
    annot.update()


@router.post("/{doc_id}/organize/comments")
async def create_comment(doc_id: str, req: CommentCreate):
    doc, path = _open(doc_id)
    try:
        page = _check_page(doc, req.page)
        snapshot(doc_id, f"Add {req.type} comment")
        annot = _create_annot(page, req)
    except HTTPException:
        doc.close()
        raise
    subject = req.subject or {"note": "Sticky Note", "freetext": "Text Box", "callout": "Callout"}.get(req.type, req.type.capitalize())
    # FreeText shows its contents on the page; everything else keeps text as the comment body.
    _finish(annot, author=req.author, content=req.text, subject=subject, new=True)
    if req.type in ("freetext", "callout"):
        annot.update(text_color=req.color or [0, 0, 0], fill_color=req.fill)
    result = _annot_dict(doc, page, annot)
    _save(doc, path)
    return {"status": "ok", "comment": result}


class ReplyRequest(BaseModel):
    text: str
    author: str = "User"


@router.post("/{doc_id}/organize/comments/{xref}/reply")
async def reply_comment(doc_id: str, xref: int, req: ReplyRequest):
    """Acrobat-style reply: a Text annotation whose /IRT points at the parent."""
    doc, path = _open(doc_id)
    try:
        page, parent = _find_annot(doc, xref)
    except HTTPException:
        doc.close()
        raise
    snapshot(doc_id, "Reply to comment")
    reply = page.add_text_annot(parent.rect.tl, req.text, icon="Comment")
    reply.set_irt_xref(parent.xref)
    _finish(reply, author=req.author, content=req.text, subject="Reply", new=True)
    result = _annot_dict(doc, page, reply)
    _save(doc, path)
    return {"status": "ok", "reply": result}


class StatusRequest(BaseModel):
    status: str
    author: str = "User"


@router.post("/{doc_id}/organize/comments/{xref}/status")
async def set_comment_status(doc_id: str, xref: int, req: StatusRequest):
    """Acrobat review state: a hidden Text annot with /IRT, /StateModel (Review), /State (...)."""
    if req.status not in REVIEW_STATES:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(REVIEW_STATES)}")
    doc, path = _open(doc_id)
    try:
        page, parent = _find_annot(doc, xref)
    except HTTPException:
        doc.close()
        raise
    snapshot(doc_id, f"Set comment status {req.status}")
    st = page.add_text_annot(parent.rect.tl, "", icon="Comment")
    st.set_irt_xref(parent.xref)
    _finish(st, author=req.author, content=f"{req.status} set by {req.author}", subject="Status", new=True)
    doc.xref_set_key(st.xref, "StateModel", "(Review)")
    doc.xref_set_key(st.xref, "State", f"({req.status})")
    st.set_flags(fitz.PDF_ANNOT_IS_HIDDEN | fitz.PDF_ANNOT_IS_NO_ZOOM | fitz.PDF_ANNOT_IS_NO_ROTATE | fitz.PDF_ANNOT_IS_PRINT)
    _save(doc, path)
    return {"status": "ok", "state": req.status}


class CommentUpdate(BaseModel):
    text: Optional[str] = None
    author: Optional[str] = None
    subject: Optional[str] = None
    color: Optional[list[float]] = None
    fill: Optional[list[float]] = None
    opacity: Optional[float] = None
    width: Optional[float] = None


@router.patch("/{doc_id}/organize/comments/{xref}")
async def update_comment(doc_id: str, xref: int, req: CommentUpdate):
    doc, path = _open(doc_id)
    try:
        page, annot = _find_annot(doc, xref)
    except HTTPException:
        doc.close()
        raise
    snapshot(doc_id, "Edit comment")
    is_freetext = annot.type[0] == fitz.PDF_ANNOT_FREE_TEXT
    info = annot.info
    if is_freetext:
        _style(annot, opacity=req.opacity, width=req.width)
    else:
        _style(annot, color=req.color, fill=req.fill, opacity=req.opacity, width=req.width)
    _finish(
        annot,
        author=req.author if req.author is not None else info.get("title", ""),
        content=req.text,
        subject=req.subject,
        new=False,
    )
    if is_freetext and (req.color is not None or req.fill is not None or req.text is not None):
        kw = {}
        if req.color is not None:
            kw["text_color"] = req.color
        if req.fill is not None:
            kw["fill_color"] = req.fill
        annot.update(**kw)
    result = _annot_dict(doc, page, annot)
    _save(doc, path)
    return {"status": "ok", "comment": result}


@router.delete("/{doc_id}/organize/comments/{xref}")
async def delete_comment(doc_id: str, xref: int):
    """Delete a comment together with its replies and status annotations."""
    doc, path = _open(doc_id)
    try:
        page, annot = _find_annot(doc, xref)
    except HTTPException:
        doc.close()
        raise
    annots = _collect_page_annots(doc, page)
    irt = {a.xref: (a.irt_xref or 0) for a in annots}
    doomed = {xref}
    changed = True
    while changed:
        changed = False
        for x, parent in irt.items():
            if parent in doomed and x not in doomed:
                doomed.add(x)
                changed = True
    snapshot(doc_id, "Delete comment")
    deleted = 0
    # Deleting an annot invalidates other Python Annot handles on the page, so
    # re-fetch each one by xref right before deleting it (replies first).
    for x in sorted(doomed, key=lambda v: v == xref):
        target = next((a for a in page.annots() if a.xref == x), None)
        if target is not None:
            page.delete_annot(target)
            deleted += 1
    _save(doc, path)
    return {"status": "ok", "deleted": deleted}


@router.get("/{doc_id}/organize/stamps")
async def list_stamps(doc_id: str):
    _doc_path(doc_id)
    return {"stamps": sorted(STAMP_NAMES)}
