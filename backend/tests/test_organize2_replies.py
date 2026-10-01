"""Comment replies are Acrobat-style thread members, not separate icons (organize2)."""

from __future__ import annotations

import uuid

import fitz
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend import advanced_ops
from backend.features.organize import router as organize_router

P = "/api/pdf"
_KEEP: list = []


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(advanced_ops, "UPLOAD_DIR", tmp_path)
    return tmp_path


@pytest_asyncio.fixture
async def c(upload_dir):
    app = FastAPI()
    app.include_router(organize_router)
    app.include_router(advanced_ops.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield client


def ink_pixels(page) -> int:
    pix = page.get_pixmap(dpi=72)
    s = pix.samples
    return sum(1 for i in range(0, len(s), pix.n) if s[i] < 250 or s[i + 1] < 250 or s[i + 2] < 250)


async def test_reply_is_threaded_hidden_and_not_drawn(c, upload_dir):
    doc = fitz.open()
    doc.new_page()
    doc_id = str(uuid.uuid4())
    (upload_dir / doc_id).mkdir()
    (upload_dir / doc_id / "original.pdf").write_bytes(doc.tobytes())

    r = await c.post(f"{P}/{doc_id}/organize/comments",
                     json={"page": 0, "type": "rect", "rect": [100, 100, 200, 160], "text": "root"})
    root = r.json()["comment"]["id"]
    d = fitz.open(str(upload_dir / doc_id / "original.pdf"))
    drawn_before = ink_pixels(d[0])
    d.close()

    r = await c.post(f"{P}/{doc_id}/organize/comments/{root}/reply", json={"text": "first reply", "author": "Bob"})
    assert r.status_code == 200
    rid = r.json()["reply"]["id"]
    r = await c.post(f"{P}/{doc_id}/organize/comments/{rid}/reply", json={"text": "reply to reply", "author": "Ann"})
    rid2 = r.json()["reply"]["id"]

    d = fitz.open(str(upload_dir / doc_id / "original.pdf"))
    page = d[0]
    _KEEP.append(page)
    for x, parent in ((rid, root), (rid2, rid)):
        assert d.xref_get_key(x, "IRT") == ("xref", f"{parent} 0 R")
        assert d.xref_get_key(x, "RT") == ("name", "/R")
        flags = int(d.xref_get_key(x, "F")[1])
        assert flags & fitz.PDF_ANNOT_IS_HIDDEN
        assert d.xref_get_key(x, "AP")[0] == "null"
        assert d.xref_get_key(x, "Rect") == d.xref_get_key(root, "Rect") or x == rid2
    # nothing new is painted on the page: no second icon next to the comment
    assert ink_pixels(page) == drawn_before
    d.close()

    # the API still threads them under the root
    comments = (await c.get(f"{P}/{doc_id}/organize/comments")).json()["comments"]
    assert len(comments) == 1
    assert [x["contents"] for x in comments[0]["replies"]] == ["first reply", "reply to reply"]
    # deleting the root removes the whole thread
    r = await c.delete(f"{P}/{doc_id}/organize/comments/{root}")
    assert r.json()["deleted"] == 3
