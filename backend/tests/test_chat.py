"""Tests for the chat interface endpoint.

The chat endpoint tries the AI engine first, but falls back to
regex-based intent parsing when no Anthropic API key is configured.
These tests exercise the fallback parser.
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_chat_replace_command(
    client: AsyncClient, uploaded_doc_id: str
):
    """Chat message 'replace Hello with Goodbye' should mutate the PDF."""
    resp = await client.post(
        f"/api/pdf/{uploaded_doc_id}/chat",
        json={"message": "replace \"Hello\" with \"Goodbye\"", "current_page": 0},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["changed"] is True
    assert "Goodbye" in data["response"]

    # Confirm the replacement in the actual PDF
    find_resp = await client.post(
        f"/api/pdf/{uploaded_doc_id}/find",
        json={"find_text": "Goodbye"},
    )
    assert find_resp.json()["count"] >= 1


@pytest.mark.asyncio
async def test_chat_page_count_query(
    client: AsyncClient, uploaded_doc_id: str
):
    """Asking 'how many pages' should return a non-mutating answer."""
    resp = await client.post(
        f"/api/pdf/{uploaded_doc_id}/chat",
        json={"message": "how many pages does this document have?", "current_page": 0},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["changed"] is False
    assert "3" in data["response"]


@pytest.mark.asyncio
async def test_chat_unknown_command(
    client: AsyncClient, uploaded_doc_id: str
):
    """An unrecognizable message should return a help response without crashing."""
    resp = await client.post(
        f"/api/pdf/{uploaded_doc_id}/chat",
        json={"message": "xyzzy plugh", "current_page": 0},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["changed"] is False
    # The fallback should suggest available commands
    assert "response" in data


@pytest.mark.asyncio
async def test_chat_word_count(
    client: AsyncClient, uploaded_doc_id: str
):
    """Asking for word count should return statistics."""
    resp = await client.post(
        f"/api/pdf/{uploaded_doc_id}/chat",
        json={"message": "word count", "current_page": 0},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["changed"] is False
    assert "Words" in data["response"] or "words" in data["response"].lower()
