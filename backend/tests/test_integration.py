"""
Integration tests for the wiring in backend/main.py: every feature router is
mounted on the real app, storage paths agree, and the advanced_ops endpoints
the new UI exposes (flatten, PDF/A, delete image) do not destroy content.
"""

import io
import os

import fitz
import pytest

from backend import advanced_ops
from backend import main as main_mod
from backend.main import app


def _pdf_bytes(build) -> bytes:
    doc = fitz.open()
    build(doc)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


async def _upload(client, data: bytes) -> str:
    r = await client.post("/api/pdf/upload", files={"file": ("t.pdf", data, "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _open(doc_id: str) -> fitz.Document:
    return fitz.open(str(main_mod.UPLOAD_DIR / doc_id / "original.pdf"))


def test_upload_dirs_agree():
    # snapshot()/undo and main's upload must resolve the same tree, otherwise
    # undo silently stops working whenever UPLOAD_DIR is set (docker, render).
    assert os.path.abspath(advanced_ops.UPLOAD_DIR) == os.path.abspath(main_mod.UPLOAD_DIR)


def test_every_feature_router_is_mounted():
    paths = {getattr(r, "path", "") for r in app.routes}
    expected = [
        "/api/pdf/{doc_id}/text-edit/page/{page_num}",
        "/api/pdf/{doc_id}/objects/{page_num}",
        "/api/pdf/{doc_id}/form-fields",
        "/api/pdf/{doc_id}/sign/apply",
        "/api/pdf/signing/certificates",
        "/api/pdf/{doc_id}/redact/apply",
        "/api/pdf/{doc_id}/security/protect",
        "/api/pdf/ocr/languages",
        "/api/pdf/create",
        "/api/pdf/{doc_id}/compress",
        "/api/pdf/{doc_id}/organize/comments",
        "/api/pdf/{doc_id}/organize/bookmarks",
        "/api/pdf/{doc_id}/undo",
    ]
    missing = [p for p in expected if p not in paths]
    assert not missing, missing


@pytest.mark.asyncio
async def test_literal_routes_not_captured_by_doc_routes(client):
    r = await client.get("/api/pdf/ocr/languages")
    assert r.status_code == 200 and "languages" in r.json()
    r = await client.get("/api/pdf/redact/presets")
    assert r.status_code == 200 and isinstance(r.json()["presets"], list)


@pytest.mark.asyncio
async def test_feature_edit_then_undo_through_main_app(client, uploaded_doc_id):
    doc_id = uploaded_doc_id
    r = await client.get(f"/api/pdf/{doc_id}/text-edit/page/0")
    assert r.status_code == 200, r.text
    block = r.json()["blocks"][0]
    r = await client.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "block", "id": block["id"], "bbox": block["bbox"]},
        "text": "Integrated edit",
    })
    assert r.status_code == 200, r.text
    with _open(doc_id) as d:
        # The paragraph reflows inside its original (narrow) width, so compare
        # with whitespace collapsed.
        assert "Integrated edit" in " ".join(d[0].get_text().split())
    r = await client.post(f"/api/pdf/{doc_id}/undo")
    assert r.status_code == 200, r.text
    with _open(doc_id) as d:
        assert "Hello World" in d[0].get_text()
        assert "Integrated edit" not in d[0].get_text()


@pytest.mark.asyncio
async def test_flatten_keeps_annotation_content_and_pending_marks(client):
    def build(doc):
        page = doc.new_page(width=300, height=300)
        page.insert_text((40, 60), "Visible words", fontsize=12)
        ink = page.add_ink_annot([[(50, 150), (250, 150), (250, 250)]])
        ink.set_colors(stroke=(1, 0, 0))
        ink.set_border(width=6)
        ink.update()
        note = page.add_freetext_annot(fitz.Rect(40, 80, 260, 110), "Typed comment", fontsize=11)
        note.update()
        page.add_redact_annot(fitz.Rect(30, 260, 120, 290))

    doc_id = await _upload(client, _pdf_bytes(build))
    try:
        r = await client.post(f"/api/pdf/{doc_id}/flatten")
        assert r.status_code == 200, r.text
        with _open(doc_id) as d:
            page = d[0]
            kinds = [a.type[1] for a in page.annots()]
            # The pending redaction mark is still a mark, nothing else is an annot.
            assert kinds == ["Redact"], kinds
            # The free-text comment became page text.
            assert "Typed comment" in page.get_text()
            # The red ink stroke is now drawn in the page content.
            pix = page.get_pixmap(annots=False)
            r_, g_, b_ = pix.pixel(150, 150)[:3]
            assert r_ > 200 and g_ < 80 and b_ < 80, (r_, g_, b_)
    finally:
        await client.delete(f"/api/pdf/{doc_id}")


@pytest.mark.asyncio
async def test_pdfa_keeps_filled_form_values(client):
    def build(doc):
        page = doc.new_page(width=300, height=300)
        w = fitz.Widget()
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        w.field_name = "name"
        w.rect = fitz.Rect(40, 40, 240, 70)
        w.field_value = "Ada Lovelace"
        page.add_widget(w)

    doc_id = await _upload(client, _pdf_bytes(build))
    try:
        r = await client.post(f"/api/pdf/{doc_id}/convert-pdfa")
        assert r.status_code == 200, r.text
        with _open(doc_id) as d:
            assert "Ada Lovelace" in d[0].get_text()
    finally:
        await client.delete(f"/api/pdf/{doc_id}")


@pytest.mark.asyncio
async def test_delete_image_keeps_text_and_pending_marks(client):
    def build(doc):
        page = doc.new_page(width=300, height=300)
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
        pix.set_rect(pix.irect, (0, 0, 255))
        page.insert_image(fitz.Rect(20, 20, 220, 220), pixmap=pix)
        page.insert_text((40, 120), "Caption over image", fontsize=14)
        page.add_redact_annot(fitz.Rect(20, 250, 120, 280))

    doc_id = await _upload(client, _pdf_bytes(build))
    try:
        r = await client.delete(f"/api/pdf/{doc_id}/image/0/0")
        assert r.status_code == 200, r.text
        with _open(doc_id) as d:
            page = d[0]
            assert page.get_images() == []
            assert "Caption over image" in page.get_text()
            assert [a.type[1] for a in page.annots()] == ["Redact"]
    finally:
        await client.delete(f"/api/pdf/{doc_id}")


@pytest.mark.asyncio
async def test_export_does_not_rewrite_a_signed_document(client):
    # Any full re-save changes the signed byte ranges and breaks the signature,
    # so /export must hand back the stored file byte-for-byte when SigFlags is set.
    def build(doc):
        page = doc.new_page(width=300, height=300)
        page.insert_text((40, 60), "Signed content", fontsize=12)
        page.add_highlight_annot(fitz.Rect(38, 48, 140, 64))
        w = fitz.Widget()
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        w.field_name = "f"
        w.rect = fitz.Rect(40, 100, 200, 120)
        page.add_widget(w)
        doc.xref_set_key(doc.pdf_catalog(), "AcroForm/SigFlags", "3")
        assert doc.get_sigflags() == 3

    doc_id = await _upload(client, _pdf_bytes(build))
    try:
        stored = (main_mod.UPLOAD_DIR / doc_id / "original.pdf").read_bytes()
        r = await client.get(f"/api/pdf/{doc_id}/export")
        assert r.status_code == 200
        assert r.content == stored
    finally:
        await client.delete(f"/api/pdf/{doc_id}")
