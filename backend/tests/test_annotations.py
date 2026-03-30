"""Tests for the per-page annotation storage endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_save_and_load_annotations(
    client: AsyncClient, uploaded_doc_id: str
):
    """POST then GET annotations for a page round-trips the data."""
    annotations = [
        {"type": "rect", "left": 10, "top": 20, "width": 100, "height": 50, "fill": "red"},
        {"type": "circle", "left": 200, "top": 300, "radius": 25, "fill": "blue"},
    ]

    save_resp = await client.post(
        f"/api/pdf/{uploaded_doc_id}/annotations/0",
        json=annotations,
    )
    assert save_resp.status_code == 200
    assert save_resp.json()["status"] == "ok"

    load_resp = await client.get(
        f"/api/pdf/{uploaded_doc_id}/annotations/0"
    )
    assert load_resp.status_code == 200
    loaded = load_resp.json()
    assert len(loaded) == 2
    assert loaded[0]["type"] == "rect"
    assert loaded[1]["type"] == "circle"


@pytest.mark.asyncio
async def test_annotations_nonexistent_page_returns_empty(
    client: AsyncClient, uploaded_doc_id: str
):
    """GET annotations for a page that has never been annotated returns []."""
    resp = await client.get(
        f"/api/pdf/{uploaded_doc_id}/annotations/99"
    )
    assert resp.status_code == 200
    assert resp.json() == []
