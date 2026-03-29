import os
import io
import json
import uuid
import shutil
import base64
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
from fastapi import FastAPI, UploadFile, File, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

app = FastAPI(title="AI PDF Editor API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)


# ─── Models ─────────────────────────────────────────────────────────────────

class TextEditRequest(BaseModel):
    page: int
    bbox: list[float]  # [x0, y0, x1, y1] in PDF points
    new_text: str
    font_size: Optional[float] = None
    font_name: Optional[str] = None
    color: Optional[list[float]] = None  # [r, g, b] 0-1


class AnnotationData(BaseModel):
    page: int
    annotations: list[dict]  # list of fabric.js JSON objects


class HighlightRequest(BaseModel):
    page: int
    rects: list[list[float]]  # list of [x0, y0, x1, y1]
    color: Optional[list[float]] = None  # [r, g, b] 0-1
    opacity: Optional[float] = 0.35


class DrawingRequest(BaseModel):
    page: int
    paths: list[dict]  # SVG path data with stroke info


class FindReplaceRequest(BaseModel):
    find_text: str
    replace_text: Optional[str] = None
    page: Optional[int] = None  # None = all pages
    match_case: bool = False


class PageOpRequest(BaseModel):
    page: int
    type: str  # rotate, delete
    rotation: Optional[int] = None


class ReorderRequest(BaseModel):
    page_order: list[int]


class SplitRequest(BaseModel):
    page_ranges: list[list[int]]


class MergeRequest(BaseModel):
    doc_ids: list[str]


class AddTextRequest(BaseModel):
    page: int
    x: float
    y: float
    text: str
    font_size: float = 12
    color: Optional[list[float]] = None


class MoveResizeRequest(BaseModel):
    page: int
    old_bbox: list[float]  # [x0, y0, x1, y1] original position in PDF points
    new_bbox: list[float]  # [x0, y0, x1, y1] new position in PDF points


class AIAssistRequest(BaseModel):
    page: int
    action: str  # summarize, rewrite, fix_grammar, extract_data, custom
    selected_text: Optional[str] = None
    prompt: Optional[str] = None


# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_doc_path(doc_id: str) -> Path:
    path = UPLOAD_DIR / doc_id / "original.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    return path


def get_annotations_path(doc_id: str) -> Path:
    return UPLOAD_DIR / doc_id / "annotations.json"


def load_annotations(doc_id: str) -> dict:
    path = get_annotations_path(doc_id)
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_annotations(doc_id: str, data: dict):
    path = get_annotations_path(doc_id)
    path.write_text(json.dumps(data))


def hex_color_to_rgb(color_int: int) -> tuple:
    """Convert integer color from PyMuPDF to (r, g, b) floats 0-1."""
    r = ((color_int >> 16) & 0xFF) / 255.0
    g = ((color_int >> 8) & 0xFF) / 255.0
    b = (color_int & 0xFF) / 255.0
    return (r, g, b)


# ─── Upload & Info ──────────────────────────────────────────────────────────

@app.post("/api/pdf/upload")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    doc_id = str(uuid.uuid4())
    doc_dir = UPLOAD_DIR / doc_id
    doc_dir.mkdir(parents=True)

    file_path = doc_dir / "original.pdf"
    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    # Initialize empty annotations
    save_annotations(doc_id, {})

    doc = fitz.open(str(file_path))
    page_count = len(doc)
    metadata = doc.metadata
    doc.close()

    return {
        "id": doc_id,
        "filename": file.filename,
        "page_count": page_count,
        "metadata": metadata,
    }


@app.get("/api/pdf/{doc_id}/info")
async def get_info(doc_id: str):
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    pages = []
    for i, page in enumerate(doc):
        pages.append({
            "index": i,
            "width": page.rect.width,
            "height": page.rect.height,
            "rotation": page.rotation,
        })
    info = {
        "id": doc_id,
        "page_count": len(doc),
        "metadata": doc.metadata,
        "pages": pages,
    }
    doc.close()
    return info


# ─── Rendering ──────────────────────────────────────────────────────────────

@app.get("/api/pdf/{doc_id}/page/{page_num}")
async def get_page(doc_id: str, page_num: int, dpi: int = 150):
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    if page_num < 0 or page_num >= len(doc):
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid page number")
    page = doc[page_num]
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("png")
    doc.close()
    return Response(content=img_bytes, media_type="image/png")


@app.get("/api/pdf/{doc_id}/thumbnail/{page_num}")
async def get_thumbnail(doc_id: str, page_num: int):
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    if page_num < 0 or page_num >= len(doc):
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid page number")
    page = doc[page_num]
    mat = fitz.Matrix(0.3, 0.3)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("png")
    doc.close()
    return Response(content=img_bytes, media_type="image/png")


# ─── Text Extraction ───────────────────────────────────────────────────────

@app.get("/api/pdf/{doc_id}/text")
async def get_text(doc_id: str, page_num: Optional[int] = None):
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    results = []
    pages = [page_num] if page_num is not None else range(len(doc))
    for p in pages:
        if p < 0 or p >= len(doc):
            continue
        page = doc[p]
        page_width = page.rect.width
        page_height = page.rect.height
        blocks = page.get_text("dict")["blocks"]
        text_blocks = []
        for block in blocks:
            if block["type"] == 0:
                for line in block["lines"]:
                    for span in line["spans"]:
                        if not span["text"].strip():
                            continue
                        text_blocks.append({
                            "text": span["text"],
                            "bbox": list(span["bbox"]),
                            "font": span["font"],
                            "size": span["size"],
                            "color": span["color"],
                            "flags": span["flags"],
                            "page": p,
                        })
        results.append({
            "page": p,
            "width": page_width,
            "height": page_height,
            "blocks": text_blocks,
        })
    doc.close()
    return results


# ─── Text Editing ───────────────────────────────────────────────────────────

@app.post("/api/pdf/{doc_id}/text/edit")
async def edit_text(doc_id: str, req: TextEditRequest):
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))

    if req.page < 0 or req.page >= len(doc):
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid page number")

    page = doc[req.page]
    rect = fitz.Rect(req.bbox[0], req.bbox[1], req.bbox[2], req.bbox[3])

    # Determine font properties from existing text at that location
    font_size = req.font_size or 11
    font_name = "helv"  # Default to Helvetica
    text_color = (0, 0, 0)

    if req.color:
        text_color = tuple(req.color[:3])

    # If no explicit font size, try to detect from existing content
    if not req.font_size:
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if block["type"] == 0:
                for line in block["lines"]:
                    for span in line["spans"]:
                        span_rect = fitz.Rect(span["bbox"])
                        if span_rect.intersects(rect):
                            font_size = span["size"]
                            text_color = hex_color_to_rgb(span["color"])
                            break

    # Redact the area and re-insert text
    page.add_redact_annot(rect)
    page.apply_redactions()

    # Insert new text with matched formatting
    tw = fitz.TextWriter(page.rect)
    font = fitz.Font(font_name)
    tw.append(fitz.Point(rect.x0, rect.y0 + font_size), req.new_text,
              font=font, fontsize=font_size)
    tw.write_text(page, color=text_color)

    # Save non-incrementally to avoid issues with redactions
    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))

    return {"status": "ok"}


@app.post("/api/pdf/{doc_id}/text/add")
async def add_text(doc_id: str, req: AddTextRequest):
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))

    if req.page < 0 or req.page >= len(doc):
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid page number")

    page = doc[req.page]
    color = tuple(req.color[:3]) if req.color else (0, 0, 0)

    tw = fitz.TextWriter(page.rect)
    font = fitz.Font("helv")
    tw.append(fitz.Point(req.x, req.y + req.font_size), req.text,
              font=font, fontsize=req.font_size)
    tw.write_text(page, color=color)

    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))

    return {"status": "ok"}


@app.post("/api/pdf/{doc_id}/text/move")
async def move_resize_text(doc_id: str, req: MoveResizeRequest):
    """Move or resize a text block: extract from old_bbox, redact, re-insert at new_bbox."""
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))

    if req.page < 0 or req.page >= len(doc):
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid page number")

    page = doc[req.page]
    old_rect = fitz.Rect(req.old_bbox)
    new_rect = fitz.Rect(req.new_bbox)

    # Collect all text spans that intersect the old rect
    spans_to_move = []
    blocks = page.get_text("dict")["blocks"]
    for block in blocks:
        if block["type"] == 0:
            for line in block["lines"]:
                for span in line["spans"]:
                    span_rect = fitz.Rect(span["bbox"])
                    if old_rect.intersects(span_rect) and span["text"].strip():
                        spans_to_move.append({
                            "text": span["text"],
                            "size": span["size"],
                            "color": hex_color_to_rgb(span["color"]),
                            "flags": span["flags"],
                            # Relative position within old_rect
                            "rel_x": (span["bbox"][0] - old_rect.x0) / max(old_rect.width, 1),
                            "rel_y": (span["bbox"][1] - old_rect.y0) / max(old_rect.height, 1),
                        })

    if not spans_to_move:
        # Maybe it's an image block — try to move image
        for block in blocks:
            if block["type"] == 1:  # image
                block_rect = fitz.Rect(block["bbox"])
                if old_rect.intersects(block_rect):
                    # Extract the image
                    img_list = page.get_images(full=True)
                    for img_info in img_list:
                        xref = img_info[0]
                        img_rects = page.get_image_rects(xref)
                        for ir in img_rects:
                            if old_rect.intersects(ir):
                                base_image = doc.extract_image(xref)
                                img_bytes = base_image["image"]
                                # Redact old position
                                page.add_redact_annot(ir)
                                page.apply_redactions()
                                # Insert at new position
                                page.insert_image(new_rect, stream=img_bytes)
                                out_path = str(file_path) + ".tmp"
                                doc.save(out_path)
                                doc.close()
                                os.replace(out_path, str(file_path))
                                return {"status": "ok", "moved": "image"}
        doc.close()
        return {"status": "ok", "moved": "nothing"}

    # Redact old area
    page.add_redact_annot(old_rect)
    page.apply_redactions()

    # Scale factor from old rect to new rect
    scale_x = new_rect.width / max(old_rect.width, 1)
    scale_y = new_rect.height / max(old_rect.height, 1)

    # Re-insert text at new positions
    font = fitz.Font("helv")
    tw = fitz.TextWriter(page.rect)
    for span in spans_to_move:
        new_x = new_rect.x0 + span["rel_x"] * new_rect.width
        new_y = new_rect.y0 + span["rel_y"] * new_rect.height
        new_size = span["size"] * min(scale_x, scale_y)
        new_size = max(4, min(new_size, 72))  # clamp font size
        tw.append(
            fitz.Point(new_x, new_y + new_size),
            span["text"],
            font=font,
            fontsize=new_size,
        )
    tw.write_text(page, color=spans_to_move[0]["color"] if spans_to_move else (0, 0, 0))

    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))

    return {"status": "ok", "moved": "text", "spans": len(spans_to_move)}


# ─── Find & Replace ────────────────────────────────────────────────────────

@app.post("/api/pdf/{doc_id}/find")
async def find_text(doc_id: str, req: FindReplaceRequest):
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    results = []
    flags = 0 if req.match_case else fitz.TEXT_PRESERVE_WHITESPACE

    pages = [req.page] if req.page is not None else range(len(doc))
    for p in pages:
        if p < 0 or p >= len(doc):
            continue
        page = doc[p]
        matches = page.search_for(req.find_text)
        for rect in matches:
            results.append({
                "page": p,
                "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
            })

    doc.close()
    return {"matches": results, "count": len(results)}


@app.post("/api/pdf/{doc_id}/replace")
async def replace_text(doc_id: str, req: FindReplaceRequest):
    if not req.replace_text and req.replace_text != "":
        raise HTTPException(status_code=400, detail="replace_text required")

    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    replaced = 0

    pages = [req.page] if req.page is not None else range(len(doc))
    for p in pages:
        if p < 0 or p >= len(doc):
            continue
        page = doc[p]
        matches = page.search_for(req.find_text)
        if not matches:
            continue

        # Get font info from first match
        font_size = 11
        text_color = (0, 0, 0)
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if block["type"] == 0:
                for line in block["lines"]:
                    for span in line["spans"]:
                        span_rect = fitz.Rect(span["bbox"])
                        if span_rect.intersects(matches[0]):
                            font_size = span["size"]
                            text_color = hex_color_to_rgb(span["color"])
                            break

        for rect in matches:
            page.add_redact_annot(rect)
        page.apply_redactions()

        font = fitz.Font("helv")
        tw = fitz.TextWriter(page.rect)
        for rect in matches:
            tw.append(fitz.Point(rect.x0, rect.y0 + font_size),
                      req.replace_text, font=font, fontsize=font_size)
            replaced += 1
        tw.write_text(page, color=text_color)

    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))

    return {"status": "ok", "replaced": replaced}


# ─── Highlights ─────────────────────────────────────────────────────────────

@app.post("/api/pdf/{doc_id}/highlight")
async def add_highlight(doc_id: str, req: HighlightRequest):
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))

    if req.page < 0 or req.page >= len(doc):
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid page number")

    page = doc[req.page]
    color = req.color or [1, 0.92, 0.23]  # yellow default

    for r in req.rects:
        rect = fitz.Rect(r[0], r[1], r[2], r[3])
        annot = page.add_highlight_annot(rect)
        annot.set_colors(stroke=color)
        annot.set_opacity(req.opacity or 0.35)
        annot.update()

    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))

    return {"status": "ok"}


# ─── Drawing (freehand paths burned into PDF) ──────────────────────────────

@app.post("/api/pdf/{doc_id}/draw")
async def add_drawing(doc_id: str, req: DrawingRequest):
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))

    if req.page < 0 or req.page >= len(doc):
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid page number")

    page = doc[req.page]

    for path_data in req.paths:
        points = path_data.get("points", [])
        if len(points) < 2:
            continue

        stroke_color = path_data.get("color", [0, 0, 0])
        stroke_width = path_data.get("width", 2)

        # Draw as ink annotation — expects list of list of (x, y) tuples
        point_list = [(p[0], p[1]) for p in points]
        annot = page.add_ink_annot([point_list])
        annot.set_border(width=stroke_width)
        annot.set_colors(stroke=stroke_color)
        annot.update()

    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))

    return {"status": "ok"}


# ─── Annotations (Fabric.js JSON storage per page) ─────────────────────────

@app.get("/api/pdf/{doc_id}/annotations/{page_num}")
async def get_page_annotations(doc_id: str, page_num: int):
    get_doc_path(doc_id)  # validates doc exists
    annots = load_annotations(doc_id)
    return annots.get(str(page_num), [])


@app.post("/api/pdf/{doc_id}/annotations/{page_num}")
async def save_page_annotations(doc_id: str, page_num: int, data: list = Body(...)):
    get_doc_path(doc_id)
    annots = load_annotations(doc_id)
    annots[str(page_num)] = data
    save_annotations(doc_id, annots)
    return {"status": "ok"}


# ─── Page Operations ───────────────────────────────────────────────────────

@app.patch("/api/pdf/{doc_id}/edit")
async def edit_pdf(doc_id: str, req: PageOpRequest):
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))

    if req.page < 0 or req.page >= len(doc):
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid page number")

    if req.type == "rotate" and req.rotation is not None:
        page = doc[req.page]
        page.set_rotation(req.rotation)
        out_path = str(file_path) + ".tmp"
        doc.save(out_path)
        doc.close()
        os.replace(out_path, str(file_path))

    elif req.type == "delete":
        doc.delete_page(req.page)
        out_path = str(file_path) + ".tmp"
        doc.save(out_path)
        doc.close()
        os.replace(out_path, str(file_path))
    else:
        doc.close()

    return {"status": "ok", "type": req.type}


@app.post("/api/pdf/{doc_id}/reorder")
async def reorder_pages(doc_id: str, req: ReorderRequest):
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    if sorted(req.page_order) != list(range(len(doc))):
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid page order")
    doc.select(req.page_order)
    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))
    return {"status": "ok"}


@app.post("/api/pdf/{doc_id}/split")
async def split_pdf(doc_id: str, req: SplitRequest):
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    result_ids = []
    for page_range in req.page_ranges:
        new_id = str(uuid.uuid4())
        new_dir = UPLOAD_DIR / new_id
        new_dir.mkdir(parents=True)
        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=page_range[0], to_page=page_range[1])
        new_doc.save(str(new_dir / "original.pdf"))
        new_doc.close()
        result_ids.append(new_id)
    doc.close()
    return {"status": "ok", "documents": result_ids}


@app.post("/api/pdf/{doc_id}/merge")
async def merge_pdfs(doc_id: str, req: MergeRequest):
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    for other_id in req.doc_ids:
        other_path = get_doc_path(other_id)
        other_doc = fitz.open(str(other_path))
        doc.insert_pdf(other_doc)
        other_doc.close()
    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))
    return {"status": "ok"}


# ─── Export (with annotations flattened) ────────────────────────────────────

@app.get("/api/pdf/{doc_id}/export")
async def export_pdf(doc_id: str, flatten: bool = True):
    file_path = get_doc_path(doc_id)

    if flatten:
        # Flatten all annotations into the PDF for a clean export
        doc = fitz.open(str(file_path))
        for page in doc:
            annots = list(page.annots()) if page.annots() else []
            for annot in annots:
                annot.set_flags(fitz.PDF_ANNOT_IS_PRINT)
                annot.update()
        export_path = str(file_path).replace("original.pdf", "export.pdf")
        doc.save(export_path)
        doc.close()
        return FileResponse(export_path, media_type="application/pdf", filename="edited.pdf")

    return FileResponse(str(file_path), media_type="application/pdf", filename="edited.pdf")


# ─── AI Assist ──────────────────────────────────────────────────────────────

@app.post("/api/pdf/{doc_id}/ai/assist")
async def ai_assist(doc_id: str, req: AIAssistRequest):
    """AI-powered text operations using the document content."""
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))

    if req.page < 0 or req.page >= len(doc):
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid page number")

    page = doc[req.page]
    page_text = page.get_text()
    doc.close()

    text_to_process = req.selected_text or page_text

    # Built-in AI operations (no external API needed)
    if req.action == "summarize":
        sentences = [s.strip() for s in text_to_process.replace("\n", " ").split(".") if s.strip()]
        if len(sentences) <= 3:
            summary = text_to_process
        else:
            # Simple extractive summary: first sentence, middle, last
            summary = ". ".join([sentences[0], sentences[len(sentences) // 2], sentences[-1]]) + "."
        return {"result": summary, "action": "summarize"}

    elif req.action == "fix_grammar":
        # Basic fixes: double spaces, capitalize after period, etc.
        import re
        text = text_to_process
        text = re.sub(r"  +", " ", text)  # double spaces
        text = re.sub(r"\.\s+([a-z])", lambda m: ". " + m.group(1).upper(), text)  # capitalize after period
        text = text[0].upper() + text[1:] if text else text  # capitalize first letter
        return {"result": text, "action": "fix_grammar"}

    elif req.action == "extract_data":
        # Extract emails, phone numbers, dates, URLs
        import re
        emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text_to_process)
        phones = re.findall(r"[\+]?[(]?[0-9]{1,4}[)]?[-\s\./0-9]{7,}", text_to_process)
        dates = re.findall(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? \d{4}\b", text_to_process)
        urls = re.findall(r"https?://[^\s<>\"']+", text_to_process)
        return {
            "result": {
                "emails": emails,
                "phones": phones,
                "dates": dates,
                "urls": urls,
            },
            "action": "extract_data",
        }

    elif req.action == "rewrite":
        # Simple rewrite: clean up whitespace and formatting
        lines = text_to_process.split("\n")
        cleaned = []
        for line in lines:
            line = line.strip()
            if line:
                cleaned.append(line)
        result = " ".join(cleaned)
        return {"result": result, "action": "rewrite"}

    elif req.action == "word_count":
        words = text_to_process.split()
        chars = len(text_to_process)
        lines = text_to_process.count("\n") + 1
        return {
            "result": {"words": len(words), "characters": chars, "lines": lines},
            "action": "word_count",
        }

    else:
        return {"result": text_to_process, "action": req.action, "note": "Unknown action, returned original text"}


# ─── Chat Interface ────────────────────────────────────────────────────────

import re as _re

# In-memory chat histories per document session
_chat_histories: dict[str, list[dict]] = {}


class ChatMessage(BaseModel):
    message: str
    current_page: int = 0


def _get_full_text(doc_id: str) -> dict[int, str]:
    """Get text for all pages."""
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    pages = {}
    for i, page in enumerate(doc):
        pages[i] = page.get_text()
    doc.close()
    return pages


def _parse_intent(message: str, page_text: str, page_count: int, current_page: int):
    """Rule-based intent parser — extracts structured commands from natural language."""
    msg = message.strip()
    msg_lower = msg.lower()

    # ── Query: word/phrase count ──
    count_match = _re.search(
        r"how many (?:times?|instances?|occurrences?).*?(?:does?|of|is)\s+[\"']?(.+?)[\"']?\s*(?:appear|occur|exist|show|\?|$)",
        msg_lower
    )
    if not count_match:
        count_match = _re.search(r"count\s+[\"']?(.+?)[\"']?\s*$", msg_lower)
    if count_match:
        term = count_match.group(1).strip().rstrip("?")
        return {"action": "query_count", "term": term}

    # ── Query: page count ──
    if _re.search(r"how many pages", msg_lower):
        return {"action": "query_pages"}

    # ── Find & Replace ──
    replace_patterns = [
        # Quoted: replace "old" with "new"
        r"(?:replace|change|swap|update)\s+[\"'](.+?)[\"']\s+(?:to|with|into)\s+[\"'](.+?)[\"']",
        # With trailing scope: replace X with Y everywhere
        r"(?:replace|change|swap|update)\s+(.+?)\s+(?:to|with|into)\s+(.+?)\s+(?:everywhere|throughout|across|on all pages|in the document|in this document)",
        # "the name/word/text X to Y"
        r"(?:replace|change|swap|update)\s+the\s+(?:name|word|text|phrase)\s+(.+?)\s+(?:to|with)\s+(.+?)$",
        # Simple: replace X with Y (to end of string)
        r"(?:replace|change|swap|update)\s+(.+?)\s+(?:to|with|into)\s+(.+?)$",
    ]
    for pat in replace_patterns:
        m = _re.search(pat, msg, _re.IGNORECASE)
        if m:
            find_str = m.group(1).strip().strip("\"'")
            repl_str = m.group(2).strip().strip("\"'.,")
            scope = "all"
            page_match = _re.search(r"(?:on|in)\s+page\s+(\d+)", msg_lower)
            if page_match:
                scope = int(page_match.group(1)) - 1
            return {"action": "replace", "find": find_str, "replace": repl_str, "scope": scope}

    # ── Delete text ──
    del_match = _re.search(
        r"(?:delete|remove|strip|erase)\s+(?:every\s+instance\s+of\s+|all\s+instances\s+of\s+|all\s+|the\s+(?:word|text|phrase)\s+)?[\"']?(.+?)[\"']?\s*(?:from|in|on|$)",
        msg, _re.IGNORECASE
    )
    if del_match and ("page" not in msg_lower.split("delete")[0] if "delete" in msg_lower else True):
        term = del_match.group(1).strip().rstrip(".")
        if term and len(term) < 200 and not _re.match(r"^pages?\s+\d", term, _re.IGNORECASE):
            return {"action": "delete_text", "find": term}

    # ── Delete pages ──
    del_pages = _re.search(
        r"(?:delete|remove)\s+pages?\s+(\d+)(?:\s*(?:through|to|-)\s*(\d+))?",
        msg_lower
    )
    if del_pages:
        start = int(del_pages.group(1)) - 1
        end = int(del_pages.group(2)) - 1 if del_pages.group(2) else start
        return {"action": "delete_pages", "start": start, "end": end}

    # ── Rotate page ──
    rotate_match = _re.search(r"rotate\s+(?:page\s+)?(\d+)?", msg_lower)
    if rotate_match:
        page = int(rotate_match.group(1)) - 1 if rotate_match.group(1) else current_page
        return {"action": "rotate_page", "page": page}

    if "rotate" in msg_lower and "this" in msg_lower:
        return {"action": "rotate_page", "page": current_page}

    # ── Extract data ──
    if _re.search(r"(?:extract|pull|find|list|get)\s+(?:all\s+)?(?:the\s+)?emails?", msg_lower):
        return {"action": "extract", "type": "emails"}
    if _re.search(r"(?:extract|pull|find|list|get)\s+(?:all\s+)?(?:the\s+)?phone", msg_lower):
        return {"action": "extract", "type": "phones"}
    if _re.search(r"(?:extract|pull|find|list|get)\s+(?:all\s+)?(?:the\s+)?dates?", msg_lower):
        return {"action": "extract", "type": "dates"}
    if _re.search(r"(?:extract|pull|find|list|get)\s+(?:all\s+)?(?:the\s+)?urls?|links?", msg_lower):
        return {"action": "extract", "type": "urls"}

    # ── Redact patterns ──
    if _re.search(r"redact\s+(?:all\s+)?(?:the\s+)?(?:ssn|social\s+security)", msg_lower):
        return {"action": "redact", "pattern": "ssn"}
    if _re.search(r"redact\s+(?:all\s+)?(?:the\s+)?(?:phone|email|date)", msg_lower):
        pattern_match = _re.search(r"(phone|email|date)", msg_lower)
        return {"action": "redact", "pattern": pattern_match.group(1) if pattern_match else "ssn"}

    # ── Add text ──
    add_match = _re.search(
        r"(?:add|insert|write|put)\s+(?:the\s+)?(?:text\s+)?[\"'](.+?)[\"']\s+(?:on|to|at)\s+(?:page\s+)?(\d+)?",
        msg, _re.IGNORECASE
    )
    if add_match:
        text = add_match.group(1)
        page = int(add_match.group(2)) - 1 if add_match.group(2) else current_page
        return {"action": "add_text", "text": text, "page": page}

    # ── Summarize ──
    if _re.search(r"summarize|summary|sum up|tldr|tl;dr", msg_lower):
        return {"action": "summarize"}

    # ── Word count ──
    if _re.search(r"word count|how many words|character count", msg_lower):
        return {"action": "word_count"}

    # ── Fallback: unknown ──
    return {"action": "unknown", "message": msg}


def _execute_intent(doc_id: str, intent: dict, current_page: int) -> dict:
    """Execute a parsed intent against the PDF and return a result."""
    action = intent["action"]
    file_path = get_doc_path(doc_id)

    if action == "query_pages":
        doc = fitz.open(str(file_path))
        count = len(doc)
        doc.close()
        return {"response": f"This document has {count} pages.", "changed": False}

    elif action == "query_count":
        term = intent["term"]
        doc = fitz.open(str(file_path))
        total = 0
        page_hits = {}
        for i, page in enumerate(doc):
            matches = page.search_for(term)
            if matches:
                total += len(matches)
                page_hits[i + 1] = len(matches)
        doc.close()
        if total == 0:
            return {"response": f'I couldn\'t find "{term}" anywhere in the document.', "changed": False}
        pages_str = ", ".join(f"page {p} ({c}x)" for p, c in page_hits.items())
        return {
            "response": f'Found **{total}** instance{"s" if total != 1 else ""} of "{term}" on {pages_str}.',
            "changed": False,
        }

    elif action == "replace":
        find_str = intent["find"]
        repl_str = intent["replace"]
        scope = intent.get("scope", "all")
        doc = fitz.open(str(file_path))
        replaced = 0
        page_hits = []

        pages_to_scan = [scope] if isinstance(scope, int) else range(len(doc))
        for p in pages_to_scan:
            if p < 0 or p >= len(doc):
                continue
            page = doc[p]
            matches = page.search_for(find_str)
            if not matches:
                continue
            # Get font info from first match
            font_size = 11
            text_color = (0, 0, 0)
            blocks = page.get_text("dict")["blocks"]
            for block in blocks:
                if block["type"] == 0:
                    for line in block["lines"]:
                        for span in line["spans"]:
                            span_rect = fitz.Rect(span["bbox"])
                            if span_rect.intersects(matches[0]):
                                font_size = span["size"]
                                text_color = hex_color_to_rgb(span["color"])
                                break

            for rect in matches:
                page.add_redact_annot(rect)
            page.apply_redactions()

            font = fitz.Font("helv")
            tw = fitz.TextWriter(page.rect)
            for rect in matches:
                tw.append(fitz.Point(rect.x0, rect.y0 + font_size),
                          repl_str, font=font, fontsize=font_size)
                replaced += 1
            tw.write_text(page, color=text_color)
            page_hits.append(p + 1)

        out_path = str(file_path) + ".tmp"
        doc.save(out_path)
        doc.close()
        os.replace(out_path, str(file_path))

        if replaced == 0:
            return {"response": f'I couldn\'t find "{find_str}" in the document. No changes made.', "changed": False}
        pages_str = ", ".join(str(p) for p in page_hits)
        return {
            "response": f'Replaced **{replaced}** instance{"s" if replaced != 1 else ""} of "{find_str}" with "{repl_str}" on page{"s" if len(page_hits) > 1 else ""} {pages_str}.',
            "changed": True,
        }

    elif action == "delete_text":
        find_str = intent["find"]
        doc = fitz.open(str(file_path))
        deleted = 0
        page_hits = []
        for i, page in enumerate(doc):
            matches = page.search_for(find_str)
            if matches:
                for rect in matches:
                    page.add_redact_annot(rect)
                page.apply_redactions()
                deleted += len(matches)
                page_hits.append(i + 1)
        out_path = str(file_path) + ".tmp"
        doc.save(out_path)
        doc.close()
        os.replace(out_path, str(file_path))

        if deleted == 0:
            return {"response": f'I couldn\'t find "{find_str}" in the document.', "changed": False}
        return {
            "response": f'Removed **{deleted}** instance{"s" if deleted != 1 else ""} of "{find_str}" from page{"s" if len(page_hits) > 1 else ""} {", ".join(str(p) for p in page_hits)}.',
            "changed": True,
        }

    elif action == "delete_pages":
        start = intent["start"]
        end = intent["end"]
        doc = fitz.open(str(file_path))
        total = len(doc)
        if start < 0 or end >= total:
            doc.close()
            return {"response": f"Invalid page range. Document has {total} pages.", "changed": False}
        pages_to_delete = list(range(start, end + 1))
        pages_to_keep = [i for i in range(total) if i not in pages_to_delete]
        if not pages_to_keep:
            doc.close()
            return {"response": "Can't delete all pages.", "changed": False}
        doc.select(pages_to_keep)
        out_path = str(file_path) + ".tmp"
        doc.save(out_path)
        doc.close()
        os.replace(out_path, str(file_path))
        count = len(pages_to_delete)
        return {
            "response": f'Deleted {"page" if count == 1 else "pages"} {start + 1}{"–" + str(end + 1) if end != start else ""}. Document now has {len(pages_to_keep)} pages.',
            "changed": True,
            "page_count_changed": True,
        }

    elif action == "rotate_page":
        page_num = intent["page"]
        doc = fitz.open(str(file_path))
        if page_num < 0 or page_num >= len(doc):
            doc.close()
            return {"response": f"Page {page_num + 1} doesn't exist.", "changed": False}
        page = doc[page_num]
        current_rot = page.rotation
        new_rot = (current_rot + 90) % 360
        page.set_rotation(new_rot)
        out_path = str(file_path) + ".tmp"
        doc.save(out_path)
        doc.close()
        os.replace(out_path, str(file_path))
        return {"response": f"Rotated page {page_num + 1} to {new_rot} degrees.", "changed": True}

    elif action == "extract":
        extract_type = intent["type"]
        pages_text = _get_full_text(doc_id)
        all_text = "\n".join(pages_text.values())

        if extract_type == "emails":
            items = _re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", all_text)
        elif extract_type == "phones":
            items = _re.findall(r"[\+]?[(]?[0-9]{1,4}[)]?[-\s\./0-9]{7,}", all_text)
            items = [p.strip() for p in items if len(p.strip()) >= 7]
        elif extract_type == "dates":
            items = _re.findall(
                r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? \d{4}\b",
                all_text
            )
        elif extract_type == "urls":
            items = _re.findall(r"https?://[^\s<>\"']+", all_text)
        else:
            items = []

        items = list(dict.fromkeys(items))  # deduplicate preserving order
        if not items:
            return {"response": f"No {extract_type} found in the document.", "changed": False}
        items_list = "\n".join(f"  {i + 1}. {item}" for i, item in enumerate(items))
        return {
            "response": f"Found **{len(items)}** {extract_type}:\n\n{items_list}",
            "changed": False,
        }

    elif action == "redact":
        pattern = intent["pattern"]
        doc = fitz.open(str(file_path))
        redacted = 0
        regex_map = {
            "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
            "phone": r"[\+]?[(]?[0-9]{1,4}[)]?[-\s\./0-9]{7,}",
            "email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
            "date": r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
        }
        regex = regex_map.get(pattern)
        if not regex:
            doc.close()
            return {"response": f"Unknown pattern type: {pattern}", "changed": False}

        for page in doc:
            text = page.get_text()
            for m in _re.finditer(regex, text):
                matches = page.search_for(m.group())
                for rect in matches:
                    page.add_redact_annot(rect, fill=(0, 0, 0))
                    redacted += 1
            page.apply_redactions()

        out_path = str(file_path) + ".tmp"
        doc.save(out_path)
        doc.close()
        os.replace(out_path, str(file_path))

        if redacted == 0:
            return {"response": f"No {pattern} patterns found to redact.", "changed": False}
        return {
            "response": f"Redacted **{redacted}** {pattern} pattern{"s" if redacted != 1 else ""}. The content has been permanently blacked out.",
            "changed": True,
        }

    elif action == "add_text":
        text = intent["text"]
        page_num = intent.get("page", current_page)
        doc = fitz.open(str(file_path))
        if page_num < 0 or page_num >= len(doc):
            doc.close()
            return {"response": f"Page {page_num + 1} doesn't exist.", "changed": False}
        page = doc[page_num]
        tw = fitz.TextWriter(page.rect)
        font = fitz.Font("helv")
        tw.append(fitz.Point(72, 72), text, font=font, fontsize=12)
        tw.write_text(page, color=(0, 0, 0))
        out_path = str(file_path) + ".tmp"
        doc.save(out_path)
        doc.close()
        os.replace(out_path, str(file_path))
        return {"response": f'Added text "{text}" to page {page_num + 1}.', "changed": True}

    elif action == "summarize":
        pages_text = _get_full_text(doc_id)
        all_text = " ".join(pages_text.values())
        sentences = [s.strip() for s in all_text.replace("\n", " ").split(".") if s.strip() and len(s.strip()) > 10]
        if len(sentences) <= 3:
            summary = ". ".join(sentences) + "." if sentences else "No text found."
        else:
            picks = [sentences[0], sentences[len(sentences) // 3], sentences[2 * len(sentences) // 3], sentences[-1]]
            summary = ". ".join(picks) + "."
        return {"response": f"**Summary:**\n\n{summary}", "changed": False}

    elif action == "word_count":
        pages_text = _get_full_text(doc_id)
        all_text = " ".join(pages_text.values())
        words = len(all_text.split())
        chars = len(all_text)
        return {
            "response": f"**Document statistics:**\n- Words: {words:,}\n- Characters: {chars:,}\n- Pages: {len(pages_text)}",
            "changed": False,
        }

    elif action == "unknown":
        return {
            "response": (
                "I'm not sure what you'd like me to do. Here are some things I can help with:\n\n"
                "- **Replace text:** \"Change [old] to [new]\"\n"
                "- **Delete text:** \"Remove all instances of [text]\"\n"
                "- **Delete pages:** \"Delete pages 3 through 5\"\n"
                "- **Rotate page:** \"Rotate page 2\"\n"
                "- **Extract data:** \"Pull all email addresses\"\n"
                "- **Redact:** \"Redact all phone numbers\"\n"
                "- **Count:** \"How many times does [word] appear?\"\n"
                "- **Summarize:** \"Summarize this document\"\n"
                "- **Word count:** \"How many words are in this document?\"\n"
                "- **Add text:** \"Add 'DRAFT' on page 1\""
            ),
            "changed": False,
        }

    return {"response": "Something went wrong.", "changed": False}


@app.post("/api/pdf/{doc_id}/chat")
async def chat(doc_id: str, msg: ChatMessage):
    """Process a natural language chat message and execute PDF commands."""
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    page_count = len(doc)
    current_text = doc[msg.current_page].get_text() if msg.current_page < len(doc) else ""
    doc.close()

    # Parse intent
    intent = _parse_intent(msg.message, current_text, page_count, msg.current_page)

    # Execute
    result = _execute_intent(doc_id, intent, msg.current_page)

    # Store in chat history
    if doc_id not in _chat_histories:
        _chat_histories[doc_id] = []
    _chat_histories[doc_id].append({"role": "user", "content": msg.message})
    _chat_histories[doc_id].append({"role": "assistant", "content": result["response"], "intent": intent})

    # Get updated page count if pages changed
    new_page_count = None
    if result.get("page_count_changed"):
        doc = fitz.open(str(file_path))
        new_page_count = len(doc)
        doc.close()

    return {
        "response": result["response"],
        "changed": result.get("changed", False),
        "intent": intent,
        "new_page_count": new_page_count,
    }


@app.get("/api/pdf/{doc_id}/chat/history")
async def get_chat_history(doc_id: str):
    get_doc_path(doc_id)
    return _chat_histories.get(doc_id, [])


@app.delete("/api/pdf/{doc_id}")
async def delete_document(doc_id: str):
    doc_dir = UPLOAD_DIR / doc_id
    if doc_dir.exists():
        shutil.rmtree(doc_dir)
    _chat_histories.pop(doc_id, None)
    return {"status": "ok"}
