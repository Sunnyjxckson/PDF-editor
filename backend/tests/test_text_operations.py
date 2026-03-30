"""Tests for text extraction, find, replace, and add-text endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_extract_text_blocks(
    client: AsyncClient, uploaded_doc_id: str
):
    """GET /api/pdf/{id}/text returns text blocks per page."""
    resp = await client.get(f"/api/pdf/{uploaded_doc_id}/text")
    assert resp.status_code == 200
    pages = resp.json()
    assert len(pages) == 3  # all three pages returned

    # Page 0 should contain "Hello World"
    page0_texts = [b["text"] for b in pages[0]["blocks"]]
    assert any("Hello" in t for t in page0_texts)

    # Page 1 should contain the email address
    page1_texts = [b["text"] for b in pages[1]["blocks"]]
    combined = " ".join(page1_texts)
    assert "test@example.com" in combined


@pytest.mark.asyncio
async def test_extract_text_single_page(
    client: AsyncClient, uploaded_doc_id: str
):
    """Requesting text for a specific page returns only that page."""
    resp = await client.get(
        f"/api/pdf/{uploaded_doc_id}/text", params={"page_num": 2}
    )
    assert resp.status_code == 200
    pages = resp.json()
    assert len(pages) == 1
    assert pages[0]["page"] == 2


@pytest.mark.asyncio
async def test_find_text(client: AsyncClient, uploaded_doc_id: str):
    """POST /api/pdf/{id}/find locates text and returns bounding boxes."""
    resp = await client.post(
        f"/api/pdf/{uploaded_doc_id}/find",
        json={"find_text": "Hello"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    assert len(data["matches"]) >= 1
    match = data["matches"][0]
    assert match["page"] == 0
    assert len(match["bbox"]) == 4


@pytest.mark.asyncio
async def test_find_text_no_results(
    client: AsyncClient, uploaded_doc_id: str
):
    """Searching for a term that does not exist returns zero matches."""
    resp = await client.post(
        f"/api/pdf/{uploaded_doc_id}/find",
        json={"find_text": "ZZZZNONEXISTENTZZZZ"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["matches"] == []


@pytest.mark.asyncio
async def test_replace_text(client: AsyncClient, uploaded_doc_id: str):
    """POST /api/pdf/{id}/replace substitutes text and reports count."""
    resp = await client.post(
        f"/api/pdf/{uploaded_doc_id}/replace",
        json={"find_text": "Hello", "replace_text": "Goodbye"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["replaced"] >= 1

    # Verify the replacement took effect
    find_resp = await client.post(
        f"/api/pdf/{uploaded_doc_id}/find",
        json={"find_text": "Goodbye"},
    )
    assert find_resp.json()["count"] >= 1


@pytest.mark.asyncio
async def test_add_text(client: AsyncClient, uploaded_doc_id: str):
    """POST /api/pdf/{id}/text/add inserts new text on a page."""
    resp = await client.post(
        f"/api/pdf/{uploaded_doc_id}/text/add",
        json={"page": 0, "x": 100, "y": 200, "text": "Injected Text", "font_size": 14},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # The new text should be discoverable via find
    find_resp = await client.post(
        f"/api/pdf/{uploaded_doc_id}/find",
        json={"find_text": "Injected"},
    )
    assert find_resp.json()["count"] >= 1
