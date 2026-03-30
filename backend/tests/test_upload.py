"""Tests for PDF upload, info, and delete endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_upload_pdf_success(client: AsyncClient, test_pdf_bytes: bytes):
    """Uploading a valid PDF returns an id, filename, page_count, and metadata."""
    resp = await client.post(
        "/api/pdf/upload",
        files={"file": ("sample.pdf", test_pdf_bytes, "application/pdf")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["filename"] == "sample.pdf"
    assert data["page_count"] == 3
    assert "metadata" in data

    # cleanup
    await client.delete(f"/api/pdf/{data['id']}")


@pytest.mark.asyncio
async def test_upload_rejects_non_pdf(client: AsyncClient):
    """Uploading a non-PDF file should be rejected with 400."""
    resp = await client.post(
        "/api/pdf/upload",
        files={"file": ("readme.txt", b"not a pdf", "text/plain")},
    )
    assert resp.status_code == 400
    assert "PDF" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_document_info_after_upload(
    client: AsyncClient, uploaded_doc_id: str
):
    """GET /api/pdf/{id}/info returns page dimensions, count, and metadata."""
    resp = await client.get(f"/api/pdf/{uploaded_doc_id}/info")
    assert resp.status_code == 200
    info = resp.json()
    assert info["id"] == uploaded_doc_id
    assert info["page_count"] == 3
    assert len(info["pages"]) == 3
    # Each page entry should carry index, width, height, rotation
    first = info["pages"][0]
    assert first["index"] == 0
    assert first["width"] > 0
    assert first["height"] > 0
    assert "rotation" in first


@pytest.mark.asyncio
async def test_delete_document(client: AsyncClient, test_pdf_bytes: bytes):
    """DELETE /api/pdf/{id} removes the document."""
    resp = await client.post(
        "/api/pdf/upload",
        files={"file": ("del.pdf", test_pdf_bytes, "application/pdf")},
    )
    doc_id = resp.json()["id"]

    del_resp = await client.delete(f"/api/pdf/{doc_id}")
    assert del_resp.status_code == 200

    # Subsequent info request should 404
    info_resp = await client.get(f"/api/pdf/{doc_id}/info")
    assert info_resp.status_code == 404
