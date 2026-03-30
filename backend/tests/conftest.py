"""
Shared fixtures for backend test suite.

Creates a test PDF with 3 pages, an async httpx test client,
and a convenience fixture that uploads the PDF and returns the doc_id.
"""

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import fitz  # PyMuPDF
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so `backend.*` imports resolve.
# ---------------------------------------------------------------------------
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Create a backend package __init__.py if it does not exist (needed for
# `from backend import ...` to work when the repo has no __init__.py).
# ---------------------------------------------------------------------------
_backend_init = Path(__file__).resolve().parents[1] / "__init__.py"
if not _backend_init.exists():
    _backend_init.write_text("")

# ---------------------------------------------------------------------------
# Import the FastAPI app *after* path setup.
# ---------------------------------------------------------------------------
from backend.main import app  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_pdf_bytes() -> bytes:
    """Build a 3-page PDF in memory and return the raw bytes.

    Page 0: "Hello World"
    Page 1: "Page Two Content test@example.com"
    Page 2: "Page Three"
    """
    doc = fitz.open()  # new empty PDF

    page_texts = [
        "Hello World",
        "Page Two Content test@example.com",
        "Page Three",
    ]

    for text in page_texts:
        page = doc.new_page(width=612, height=792)  # US Letter
        tw = fitz.TextWriter(page.rect)
        font = fitz.Font("helv")
        tw.append(fitz.Point(72, 72), text, font=font, fontsize=12)
        tw.write_text(page, color=(0, 0, 0))

    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


@pytest_asyncio.fixture
async def client():
    """Async httpx test client bound to the FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest_asyncio.fixture
async def uploaded_doc_id(client: AsyncClient, test_pdf_bytes: bytes) -> str:
    """Upload the test PDF and return the document id.

    The document is cleaned up after the test via the DELETE endpoint.
    """
    resp = await client.post(
        "/api/pdf/upload",
        files={"file": ("test.pdf", test_pdf_bytes, "application/pdf")},
    )
    assert resp.status_code == 200
    doc_id = resp.json()["id"]
    yield doc_id
    # cleanup
    await client.delete(f"/api/pdf/{doc_id}")
