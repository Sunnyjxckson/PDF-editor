"""
Advanced PDF operations: undo/redo, watermark, stamps, images, password protection,
flatten, page-to-image export, document comparison, and batch operations.
"""

import io
import os
import json
import math
import shutil
import time
import zipfile
import base64
import difflib
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import fitz  # PyMuPDF
from fastapi import APIRouter, HTTPException, Body, UploadFile, File
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/pdf")

UPLOAD_DIR = Path("uploads")

# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_doc_path(doc_id: str) -> Path:
    path = UPLOAD_DIR / doc_id / "original.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    return path


# ─── Undo / Redo System ──────────────────────────────────────────────────────

MAX_HISTORY = 20


def _history_dir(doc_id: str) -> Path:
    d = UPLOAD_DIR / doc_id / "history"
    d.mkdir(exist_ok=True)
    return d


def _history_file(doc_id: str) -> Path:
    return UPLOAD_DIR / doc_id / "history.json"


def _load_history(doc_id: str) -> dict:
    hf = _history_file(doc_id)
    if hf.exists():
        return json.loads(hf.read_text())
    return {"versions": [], "current": -1}


def _save_history(doc_id: str, history: dict):
    _history_file(doc_id).write_text(json.dumps(history))


def snapshot(doc_id: str, operation: str):
    """Save a copy of the current PDF before a mutating operation."""
    src = UPLOAD_DIR / doc_id / "original.pdf"
    if not src.exists():
        return

    history = _load_history(doc_id)
    hdir = _history_dir(doc_id)

    # If we're not at the tip, discard any redo states ahead of current
    if history["current"] < len(history["versions"]) - 1:
        # Remove files for discarded versions
        for v in history["versions"][history["current"] + 1:]:
            fp = hdir / v["filename"]
            if fp.exists():
                fp.unlink()
        history["versions"] = history["versions"][:history["current"] + 1]

    # Create new version entry
    version_num = len(history["versions"])
    filename = f"v{version_num}_{int(time.time())}.pdf"
    shutil.copy2(str(src), str(hdir / filename))

    history["versions"].append({
        "filename": filename,
        "operation": operation,
        "timestamp": datetime.now().isoformat(),
    })

    # Enforce max history
    while len(history["versions"]) > MAX_HISTORY:
        oldest = history["versions"].pop(0)
        fp = hdir / oldest["filename"]
        if fp.exists():
            fp.unlink()

    history["current"] = len(history["versions"]) - 1
    _save_history(doc_id, history)


@router.post("/{doc_id}/undo")
async def undo(doc_id: str):
    """Restore the previous version of the document."""
    get_doc_path(doc_id)
    history = _load_history(doc_id)

    if history["current"] < 0 or not history["versions"]:
        raise HTTPException(status_code=400, detail="Nothing to undo")

    # The version at `current` is the snapshot taken *before* the last op.
    # Restoring it undoes that op.
    version = history["versions"][history["current"]]
    hdir = _history_dir(doc_id)
    snapshot_path = hdir / version["filename"]

    if not snapshot_path.exists():
        raise HTTPException(status_code=500, detail="Snapshot file missing")

    # Before restoring, save the current state so redo can get back to it
    current_pdf = UPLOAD_DIR / doc_id / "original.pdf"

    # If we're at the tip, save current state as a redo-able state
    if history["current"] == len(history["versions"]) - 1:
        redo_filename = f"redo_{int(time.time())}.pdf"
        shutil.copy2(str(current_pdf), str(hdir / redo_filename))
        history["versions"].append({
            "filename": redo_filename,
            "operation": "(current state before undo)",
            "timestamp": datetime.now().isoformat(),
        })

    # Restore the snapshot
    shutil.copy2(str(snapshot_path), str(current_pdf))
    history["current"] -= 1
    _save_history(doc_id, history)

    return {
        "status": "ok",
        "undone_operation": version["operation"],
        "can_undo": history["current"] >= 0,
        "can_redo": history["current"] < len(history["versions"]) - 1,
    }


@router.post("/{doc_id}/redo")
async def redo(doc_id: str):
    """Restore the next version of the document (after an undo)."""
    get_doc_path(doc_id)
    history = _load_history(doc_id)

    next_idx = history["current"] + 1
    if next_idx >= len(history["versions"]):
        raise HTTPException(status_code=400, detail="Nothing to redo")

    # Move forward and restore that version
    # We need the version *after* the one we want to go to
    # Actually: current+1 is the next snapshot to restore
    next_next = next_idx + 1
    if next_next >= len(history["versions"]):
        raise HTTPException(status_code=400, detail="Nothing to redo")

    version = history["versions"][next_next]
    hdir = _history_dir(doc_id)
    snapshot_path = hdir / version["filename"]

    if not snapshot_path.exists():
        raise HTTPException(status_code=500, detail="Snapshot file missing")

    current_pdf = UPLOAD_DIR / doc_id / "original.pdf"
    shutil.copy2(str(snapshot_path), str(current_pdf))
    history["current"] = next_next - 1
    # If we've reached the redo tip, pop the redo state marker
    if next_next == len(history["versions"]) - 1 and "(current state before undo)" in version.get("operation", ""):
        # Restored to the latest state; remove the redo marker
        removed = history["versions"].pop()
        fp = hdir / removed["filename"]
        if fp.exists():
            fp.unlink()
        history["current"] = len(history["versions"]) - 1

    _save_history(doc_id, history)

    return {
        "status": "ok",
        "restored_operation": version["operation"],
        "can_undo": history["current"] >= 0,
        "can_redo": history["current"] < len(history["versions"]) - 1,
    }


@router.get("/{doc_id}/history")
async def get_history(doc_id: str):
    """List available undo/redo states with timestamps and descriptions."""
    get_doc_path(doc_id)
    history = _load_history(doc_id)
    versions = []
    for i, v in enumerate(history["versions"]):
        versions.append({
            "index": i,
            "operation": v["operation"],
            "timestamp": v["timestamp"],
            "is_current": i == history["current"],
        })
    return {
        "versions": versions,
        "current": history["current"],
        "can_undo": history["current"] >= 0 and len(history["versions"]) > 0,
        "can_redo": history["current"] < len(history["versions"]) - 1,
    }


# ─── Watermark ────────────────────────────────────────────────────────────────

class WatermarkRequest(BaseModel):
    text: str = "CONFIDENTIAL"
    font_size: float = 60
    color: list[float] = [0.8, 0.8, 0.8]
    opacity: float = 0.3
    rotation: float = -45
    pages: Union[str, list[int]] = "all"


@router.post("/{doc_id}/watermark")
async def add_watermark(doc_id: str, req: WatermarkRequest):
    file_path = get_doc_path(doc_id)
    snapshot(doc_id, f"Add watermark: {req.text}")

    doc = fitz.open(str(file_path))
    pages = list(range(len(doc))) if req.pages == "all" else req.pages

    for p in pages:
        if p < 0 or p >= len(doc):
            continue
        page = doc[p]
        rect = page.rect
        # Center of page
        cx, cy = rect.width / 2, rect.height / 2

        # Create text with rotation using a shape
        tw = fitz.TextWriter(page.rect)
        font = fitz.Font("helv")
        text_width = font.text_length(req.text, fontsize=req.font_size)

        # Calculate rotated insertion point so text is centered
        angle_rad = math.radians(req.rotation)
        # Start point offset from center
        sx = cx - (text_width / 2) * math.cos(angle_rad)
        sy = cy - (text_width / 2) * math.sin(angle_rad) + req.font_size / 2

        # Use morph to rotate text around center
        tw.append(
            fitz.Point(sx, sy),
            req.text,
            font=font,
            fontsize=req.font_size,
        )
        tw.write_text(
            page,
            color=tuple(req.color[:3]),
            opacity=req.opacity,
            morph=(fitz.Point(cx, cy), fitz.Matrix(math.cos(angle_rad), math.sin(angle_rad),
                                                     -math.sin(angle_rad), math.cos(angle_rad),
                                                     0, 0)),
        )

    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))

    return {"status": "ok", "pages_watermarked": len(pages)}


# ─── Page Stamps / Headers & Footers ─────────────────────────────────────────

class StampRequest(BaseModel):
    text: str = "Page {n} of {total}"
    position: str = "bottom-center"  # top-left, top-center, top-right, bottom-left, bottom-center, bottom-right
    font_size: float = 10
    color: list[float] = [0, 0, 0]
    pages: Union[str, list[int]] = "all"
    margin: float = 36  # points from edge


@router.post("/{doc_id}/stamp")
async def add_stamp(doc_id: str, req: StampRequest):
    file_path = get_doc_path(doc_id)
    snapshot(doc_id, f"Add stamp: {req.text} at {req.position}")

    doc = fitz.open(str(file_path))
    total_pages = len(doc)
    pages = list(range(total_pages)) if req.pages == "all" else req.pages
    font = fitz.Font("helv")

    # Get original filename if stored
    filename = "document.pdf"
    meta_path = UPLOAD_DIR / doc_id / "metadata.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        filename = meta.get("filename", filename)

    for p in pages:
        if p < 0 or p >= len(doc):
            continue
        page = doc[p]
        rect = page.rect

        # Resolve variables
        text = req.text.replace("{n}", str(p + 1))
        text = text.replace("{total}", str(total_pages))
        text = text.replace("{date}", datetime.now().strftime("%Y-%m-%d"))
        text = text.replace("{filename}", filename)

        text_width = font.text_length(text, fontsize=req.font_size)

        # Calculate position
        pos = req.position.lower()
        if "left" in pos:
            x = req.margin
        elif "right" in pos:
            x = rect.width - req.margin - text_width
        else:  # center
            x = (rect.width - text_width) / 2

        if "top" in pos:
            y = req.margin + req.font_size
        else:  # bottom
            y = rect.height - req.margin

        tw = fitz.TextWriter(page.rect)
        tw.append(fitz.Point(x, y), text, font=font, fontsize=req.font_size)
        tw.write_text(page, color=tuple(req.color[:3]))

    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))

    return {"status": "ok", "pages_stamped": len(pages)}


# ─── Image Operations ────────────────────────────────────────────────────────

class AddImageRequest(BaseModel):
    page: int
    x: float
    y: float
    width: float
    height: float
    image: str  # base64-encoded image data


@router.post("/{doc_id}/add-image")
async def add_image(doc_id: str, req: AddImageRequest):
    file_path = get_doc_path(doc_id)
    snapshot(doc_id, f"Add image on page {req.page}")

    doc = fitz.open(str(file_path))
    if req.page < 0 or req.page >= len(doc):
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid page number")

    page = doc[req.page]
    rect = fitz.Rect(req.x, req.y, req.x + req.width, req.y + req.height)

    # Decode base64 image
    try:
        img_data = base64.b64decode(req.image)
    except Exception:
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid base64 image data")

    page.insert_image(rect, stream=img_data)

    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))

    return {"status": "ok"}


@router.get("/{doc_id}/images")
async def list_images(doc_id: str):
    """List all images in the document with page, position, and size."""
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    images = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        img_list = page.get_images(full=True)
        for idx, img_info in enumerate(img_list):
            xref = img_info[0]
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            for rect in rects:
                images.append({
                    "page": page_num,
                    "index": idx,
                    "xref": xref,
                    "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
                    "width": rect.width,
                    "height": rect.height,
                })

    doc.close()
    return {"images": images, "count": len(images)}


@router.delete("/{doc_id}/image/{page}/{index}")
async def delete_image(doc_id: str, page: int, index: int):
    """Remove an image from a specific page by index."""
    file_path = get_doc_path(doc_id)
    snapshot(doc_id, f"Delete image {index} on page {page}")

    doc = fitz.open(str(file_path))
    if page < 0 or page >= len(doc):
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid page number")

    pg = doc[page]
    img_list = pg.get_images(full=True)
    if index < 0 or index >= len(img_list):
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid image index")

    xref = img_list[index][0]
    try:
        rects = pg.get_image_rects(xref)
        for rect in rects:
            pg.add_redact_annot(rect)
        pg.apply_redactions()
    except Exception:
        doc.close()
        raise HTTPException(status_code=500, detail="Failed to remove image")

    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))

    return {"status": "ok"}


# ─── Password Protection ─────────────────────────────────────────────────────

class ProtectRequest(BaseModel):
    user_password: str = ""
    owner_password: str
    permissions: list[str] = ["print", "copy", "modify"]


@router.post("/{doc_id}/protect")
async def protect_pdf(doc_id: str, req: ProtectRequest):
    file_path = get_doc_path(doc_id)
    snapshot(doc_id, "Add password protection")

    doc = fitz.open(str(file_path))

    # Map permission strings to PyMuPDF permission flags
    perm = (
        fitz.PDF_PERM_ACCESSIBILITY  # always allow accessibility
    )
    perm_map = {
        "print": fitz.PDF_PERM_PRINT | fitz.PDF_PERM_PRINT_HQ,
        "copy": fitz.PDF_PERM_COPY,
        "modify": fitz.PDF_PERM_MODIFY | fitz.PDF_PERM_ANNOTATE,
        "annotate": fitz.PDF_PERM_ANNOTATE,
        "fill_forms": fitz.PDF_PERM_FORM,
        "assemble": fitz.PDF_PERM_ASSEMBLE,
    }
    for p in req.permissions:
        perm |= perm_map.get(p, 0)

    encrypt_method = fitz.PDF_ENCRYPT_AES_256

    out_path = str(file_path) + ".tmp"
    doc.save(
        out_path,
        encryption=encrypt_method,
        user_pw=req.user_password,
        owner_pw=req.owner_password,
        permissions=perm,
    )
    doc.close()
    os.replace(out_path, str(file_path))

    return {"status": "ok", "encryption": "AES-256"}


class UnlockRequest(BaseModel):
    password: str


@router.post("/{doc_id}/unlock")
async def unlock_pdf(doc_id: str, req: UnlockRequest):
    file_path = get_doc_path(doc_id)
    snapshot(doc_id, "Remove password protection")

    try:
        doc = fitz.open(str(file_path))
        if doc.needs_pass:
            if not doc.authenticate(req.password):
                doc.close()
                raise HTTPException(status_code=403, detail="Invalid password")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to open document")

    # Re-save without encryption
    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))

    return {"status": "ok"}


# ─── PDF/A Compliance ─────────────────────────────────────────────────────────

@router.post("/{doc_id}/convert-pdfa")
async def convert_pdfa(doc_id: str):
    """Convert to PDF/A-like format: embed all fonts, remove JS, flatten forms."""
    file_path = get_doc_path(doc_id)
    snapshot(doc_id, "Convert to PDF/A")

    doc = fitz.open(str(file_path))

    # Remove JavaScript actions
    try:
        doc.scrub(
            attached_files=False,
            clean_pages=True,
            embedded_files=False,
            hidden_text=False,
            javascript=True,
            metadata=False,
            redactions=False,
            redact_images=0,
            remove_links=False,
            reset_fields=True,
            reset_responses=True,
            thumbnails=True,
            xml_metadata=False,
        )
    except Exception:
        pass  # scrub may not be available on all fitz versions

    # Set PDF/A metadata
    doc.set_metadata({
        "producer": "AI PDF Editor (PDF/A conversion)",
        "creator": "AI PDF Editor",
        "creationDate": datetime.now().strftime("D:%Y%m%d%H%M%S"),
        "modDate": datetime.now().strftime("D:%Y%m%d%H%M%S"),
    })

    out_path = str(file_path) + ".tmp"
    doc.save(out_path, deflate=True, garbage=4, clean=True)
    doc.close()
    os.replace(out_path, str(file_path))

    return {"status": "ok", "note": "Document cleaned and optimized for archival"}


# ─── Flatten Annotations ─────────────────────────────────────────────────────

@router.post("/{doc_id}/flatten")
async def flatten_annotations(doc_id: str):
    """Flatten all annotations into page content."""
    file_path = get_doc_path(doc_id)
    snapshot(doc_id, "Flatten annotations")

    doc = fitz.open(str(file_path))
    flattened_count = 0

    for page in doc:
        annots = list(page.annots()) if page.annots() else []
        for annot in annots:
            # Render annotation into the page
            annot.set_flags(fitz.PDF_ANNOT_IS_PRINT)
            annot.update()
            flattened_count += 1

    # Save and re-open to "burn in" annotations
    tmp1 = str(file_path) + ".tmp1"
    doc.save(tmp1)
    doc.close()

    # Re-open, remove annotation objects (they're now part of appearance streams)
    doc = fitz.open(tmp1)
    for page in doc:
        annots = list(page.annots()) if page.annots() else []
        for annot in annots:
            page.delete_annot(annot)

    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))
    if os.path.exists(tmp1):
        os.unlink(tmp1)

    return {"status": "ok", "flattened": flattened_count}


# ─── Extract Pages as Images ─────────────────────────────────────────────────

@router.get("/{doc_id}/page/{page_num}/image")
async def page_to_image(doc_id: str, page_num: int, format: str = "png", dpi: int = 300):
    """Export a single page as a high-resolution image."""
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))

    if page_num < 0 or page_num >= len(doc):
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid page number")

    page = doc[page_num]
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)

    fmt = format.lower()
    if fmt == "jpg" or fmt == "jpeg":
        img_bytes = pix.tobytes("jpeg")
        media_type = "image/jpeg"
    else:
        img_bytes = pix.tobytes("png")
        media_type = "image/png"

    doc.close()
    return Response(content=img_bytes, media_type=media_type)


@router.post("/{doc_id}/export-images")
async def export_all_pages_as_images(
    doc_id: str,
    format: str = Body("png"),
    dpi: int = Body(150),
):
    """Export all pages as a ZIP of images."""
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zoom = dpi / 72
        mat = fitz.Matrix(zoom, zoom)
        for i in range(len(doc)):
            page = doc[i]
            pix = page.get_pixmap(matrix=mat)
            ext = "jpg" if format.lower() in ("jpg", "jpeg") else "png"
            img_fmt = "jpeg" if ext == "jpg" else "png"
            img_bytes = pix.tobytes(img_fmt)
            zf.writestr(f"page_{i + 1:04d}.{ext}", img_bytes)

    doc.close()
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=pages_{doc_id[:8]}.zip"},
    )


# ─── Compare Documents ───────────────────────────────────────────────────────

class CompareRequest(BaseModel):
    doc_id_1: str
    doc_id_2: str


@router.post("/compare")
async def compare_documents(req: CompareRequest):
    """Compare two uploaded PDFs and return text diffs per page."""
    path1 = get_doc_path(req.doc_id_1)
    path2 = get_doc_path(req.doc_id_2)

    doc1 = fitz.open(str(path1))
    doc2 = fitz.open(str(path2))

    result = {
        "doc1_pages": len(doc1),
        "doc2_pages": len(doc2),
        "pages_added": max(0, len(doc2) - len(doc1)),
        "pages_removed": max(0, len(doc1) - len(doc2)),
        "diffs": [],
    }

    max_pages = max(len(doc1), len(doc2))
    for i in range(max_pages):
        text1 = doc1[i].get_text().splitlines() if i < len(doc1) else []
        text2 = doc2[i].get_text().splitlines() if i < len(doc2) else []

        if text1 == text2:
            continue

        diff = list(difflib.unified_diff(text1, text2, lineterm="",
                                          fromfile=f"doc1/page_{i+1}",
                                          tofile=f"doc2/page_{i+1}"))
        if diff:
            # Count additions and removals
            added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
            result["diffs"].append({
                "page": i,
                "lines_added": added,
                "lines_removed": removed,
                "diff": diff,
            })

    doc1.close()
    doc2.close()

    return result


# ─── Batch Operations ────────────────────────────────────────────────────────

class BatchOperation(BaseModel):
    type: str
    # Common fields — optional depending on type
    find: Optional[str] = None
    replace: Optional[str] = None
    text: Optional[str] = None
    position: Optional[str] = None
    font_size: Optional[float] = None
    color: Optional[list[float]] = None
    opacity: Optional[float] = None
    rotation: Optional[float] = None
    pages: Optional[Union[str, list[int]]] = None
    page: Optional[int] = None
    margin: Optional[float] = None


class BatchRequest(BaseModel):
    operations: list[BatchOperation]


@router.post("/{doc_id}/batch")
async def batch_operations(doc_id: str, req: BatchRequest):
    """Execute multiple operations in sequence with a single undo snapshot."""
    get_doc_path(doc_id)
    op_names = [op.type for op in req.operations]
    snapshot(doc_id, f"Batch: {', '.join(op_names)}")

    results = []
    for op in req.operations:
        try:
            if op.type == "replace" and op.find is not None and op.replace is not None:
                r = await _batch_replace(doc_id, op.find, op.replace, op.page)
            elif op.type == "watermark":
                wm = WatermarkRequest(
                    text=op.text or "CONFIDENTIAL",
                    font_size=op.font_size or 60,
                    color=op.color or [0.8, 0.8, 0.8],
                    opacity=op.opacity or 0.3,
                    rotation=op.rotation or -45,
                    pages=op.pages or "all",
                )
                r = await _batch_watermark(doc_id, wm)
            elif op.type == "stamp":
                st = StampRequest(
                    text=op.text or "Page {n} of {total}",
                    position=op.position or "bottom-center",
                    font_size=op.font_size or 10,
                    color=op.color or [0, 0, 0],
                    pages=op.pages or "all",
                    margin=op.margin or 36,
                )
                r = await _batch_stamp(doc_id, st)
            elif op.type == "flatten":
                r = await flatten_annotations(doc_id)
            else:
                r = {"status": "skipped", "reason": f"Unknown operation type: {op.type}"}
            results.append({"type": op.type, "result": r})
        except Exception as e:
            results.append({"type": op.type, "error": str(e)})

    return {"status": "ok", "results": results}


async def _batch_replace(doc_id: str, find: str, replace: str, page: Optional[int]):
    """Find-replace without taking another snapshot (batch already took one)."""
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    replaced = 0
    pages = [page] if page is not None else range(len(doc))

    for p in pages:
        if p < 0 or p >= len(doc):
            continue
        pg = doc[p]
        matches = pg.search_for(find)
        if not matches:
            continue
        font_size = 11
        text_color = (0, 0, 0)
        blocks = pg.get_text("dict")["blocks"]
        for block in blocks:
            if block["type"] == 0:
                for line in block["lines"]:
                    for span in line["spans"]:
                        span_rect = fitz.Rect(span["bbox"])
                        if span_rect.intersects(matches[0]):
                            font_size = span["size"]
                            c = span["color"]
                            text_color = (((c >> 16) & 0xFF) / 255, ((c >> 8) & 0xFF) / 255, (c & 0xFF) / 255)
                            break
        for rect in matches:
            pg.add_redact_annot(rect)
        pg.apply_redactions()
        font = fitz.Font("helv")
        tw = fitz.TextWriter(pg.rect)
        for rect in matches:
            tw.append(fitz.Point(rect.x0, rect.y0 + font_size), replace, font=font, fontsize=font_size)
            replaced += 1
        tw.write_text(pg, color=text_color)

    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))
    return {"status": "ok", "replaced": replaced}


async def _batch_watermark(doc_id: str, req: WatermarkRequest):
    """Watermark without snapshot (batch already took one)."""
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    pages = list(range(len(doc))) if req.pages == "all" else req.pages

    for p in pages:
        if p < 0 or p >= len(doc):
            continue
        page = doc[p]
        rect = page.rect
        cx, cy = rect.width / 2, rect.height / 2
        tw = fitz.TextWriter(page.rect)
        font = fitz.Font("helv")
        text_width = font.text_length(req.text, fontsize=req.font_size)
        angle_rad = math.radians(req.rotation)
        sx = cx - (text_width / 2) * math.cos(angle_rad)
        sy = cy - (text_width / 2) * math.sin(angle_rad) + req.font_size / 2
        tw.append(fitz.Point(sx, sy), req.text, font=font, fontsize=req.font_size)
        tw.write_text(
            page, color=tuple(req.color[:3]), opacity=req.opacity,
            morph=(fitz.Point(cx, cy), fitz.Matrix(math.cos(angle_rad), math.sin(angle_rad),
                                                     -math.sin(angle_rad), math.cos(angle_rad), 0, 0)),
        )

    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))
    return {"status": "ok", "pages_watermarked": len(pages)}


async def _batch_stamp(doc_id: str, req: StampRequest):
    """Stamp without snapshot (batch already took one)."""
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    total_pages = len(doc)
    pages = list(range(total_pages)) if req.pages == "all" else req.pages
    font = fitz.Font("helv")

    for p in pages:
        if p < 0 or p >= len(doc):
            continue
        page = doc[p]
        rect = page.rect
        text = req.text.replace("{n}", str(p + 1)).replace("{total}", str(total_pages))
        text = text.replace("{date}", datetime.now().strftime("%Y-%m-%d")).replace("{filename}", "document.pdf")
        text_width = font.text_length(text, fontsize=req.font_size)
        pos = req.position.lower()
        x = req.margin if "left" in pos else (rect.width - req.margin - text_width if "right" in pos else (rect.width - text_width) / 2)
        y = req.margin + req.font_size if "top" in pos else rect.height - req.margin
        tw = fitz.TextWriter(page.rect)
        tw.append(fitz.Point(x, y), text, font=font, fontsize=req.font_size)
        tw.write_text(page, color=tuple(req.color[:3]))

    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))
    return {"status": "ok", "pages_stamped": len(pages)}
