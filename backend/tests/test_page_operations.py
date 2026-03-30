"""Tests for page-level operations: rotate, delete, reorder, split."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_rotate_page(client: AsyncClient, uploaded_doc_id: str):
    """PATCH /api/pdf/{id}/edit with type=rotate applies rotation."""
    resp = await client.patch(
        f"/api/pdf/{uploaded_doc_id}/edit",
        json={"page": 0, "type": "rotate", "rotation": 90},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["type"] == "rotate"

    # Confirm via info
    info = (await client.get(f"/api/pdf/{uploaded_doc_id}/info")).json()
    assert info["pages"][0]["rotation"] == 90


@pytest.mark.asyncio
async def test_delete_middle_page(client: AsyncClient, uploaded_doc_id: str):
    """Deleting the middle page (index 1) reduces page count to 2."""
    resp = await client.patch(
        f"/api/pdf/{uploaded_doc_id}/edit",
        json={"page": 1, "type": "delete"},
    )
    assert resp.status_code == 200

    info = (await client.get(f"/api/pdf/{uploaded_doc_id}/info")).json()
    assert info["page_count"] == 2


@pytest.mark.asyncio
async def test_delete_last_remaining_page_of_multipage(
    client: AsyncClient, uploaded_doc_id: str
):
    """Deleting the last page of a 3-page doc should leave 2 pages."""
    resp = await client.patch(
        f"/api/pdf/{uploaded_doc_id}/edit",
        json={"page": 2, "type": "delete"},
    )
    assert resp.status_code == 200

    info = (await client.get(f"/api/pdf/{uploaded_doc_id}/info")).json()
    assert info["page_count"] == 2


@pytest.mark.asyncio
async def test_invalid_page_number(
    client: AsyncClient, uploaded_doc_id: str
):
    """Operating on a page index beyond the document should return 400."""
    resp = await client.patch(
        f"/api/pdf/{uploaded_doc_id}/edit",
        json={"page": 99, "type": "rotate", "rotation": 90},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reorder_pages(client: AsyncClient, uploaded_doc_id: str):
    """POST /api/pdf/{id}/reorder reverses page order."""
    resp = await client.post(
        f"/api/pdf/{uploaded_doc_id}/reorder",
        json={"page_order": [2, 1, 0]},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # After reorder, what was page 2 ("Page Three") is now page 0
    text_resp = await client.get(
        f"/api/pdf/{uploaded_doc_id}/text", params={"page_num": 0}
    )
    blocks = text_resp.json()[0]["blocks"]
    combined = " ".join(b["text"] for b in blocks)
    assert "Page Three" in combined


@pytest.mark.asyncio
async def test_reorder_invalid(client: AsyncClient, uploaded_doc_id: str):
    """Reorder with an invalid permutation should fail with 400."""
    resp = await client.post(
        f"/api/pdf/{uploaded_doc_id}/reorder",
        json={"page_order": [0, 0, 0]},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_split_pdf(client: AsyncClient, uploaded_doc_id: str):
    """POST /api/pdf/{id}/split creates new documents from page ranges."""
    resp = await client.post(
        f"/api/pdf/{uploaded_doc_id}/split",
        json={"page_ranges": [[0, 0], [1, 2]]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert len(data["documents"]) == 2

    # First split doc should have 1 page
    first_id = data["documents"][0]
    info = (await client.get(f"/api/pdf/{first_id}/info")).json()
    assert info["page_count"] == 1

    # Second split doc should have 2 pages
    second_id = data["documents"][1]
    info2 = (await client.get(f"/api/pdf/{second_id}/info")).json()
    assert info2["page_count"] == 2

    # cleanup split docs
    await client.delete(f"/api/pdf/{first_id}")
    await client.delete(f"/api/pdf/{second_id}")
