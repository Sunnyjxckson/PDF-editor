"""Bookmark placement, nesting and drag-reorder (organize2). Asserts on the saved outline."""

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


def store(upload_dir, toc, pages=10) -> str:
    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"p{i}")
    if toc:
        doc.set_toc(toc)
    doc_id = str(uuid.uuid4())
    (upload_dir / doc_id).mkdir()
    (upload_dir / doc_id / "original.pdf").write_bytes(doc.tobytes())
    return doc_id


def outline(upload_dir, doc_id):
    d = fitz.open(str(upload_dir / doc_id / "original.pdf"))
    t = [tuple(x[:3]) for x in d.get_toc()]
    d.close()
    return t


TOC = [[1, "Intro", 1], [2, "Intro.a", 2], [2, "Intro.b", 3], [1, "Chapter 5", 6], [2, "5.1", 7], [1, "Appendix", 9]]


async def test_add_without_position_is_top_level_in_page_order(c, upload_dir):
    doc_id = store(upload_dir, TOC)
    # page index 3 (=page 4) lies between Intro (1) and Chapter 5 (6); the last
    # item is a level-2 child, and the old code nested/appended wrongly.
    r = await c.post(f"{P}/{doc_id}/organize/bookmarks", json={"title": "Chapter 2", "page": 3})
    assert r.status_code == 200, r.text
    assert outline(upload_dir, doc_id) == [
        (1, "Intro", 1), (2, "Intro.a", 2), (2, "Intro.b", 3),
        (1, "Chapter 2", 4),
        (1, "Chapter 5", 6), (2, "5.1", 7), (1, "Appendix", 9)]
    assert r.json()["index"] == 3
    # same page as Intro -> after Intro (and after Intro's children)
    await c.post(f"{P}/{doc_id}/organize/bookmarks", json={"title": "Cover", "page": 0})
    # after everything, even though the last flat item is top-level already
    await c.post(f"{P}/{doc_id}/organize/bookmarks", json={"title": "Index", "page": 9})
    # same page as an existing one -> after it
    await c.post(f"{P}/{doc_id}/organize/bookmarks", json={"title": "Chapter 5 cont", "page": 5})
    t = outline(upload_dir, doc_id)
    assert [x for x in t if x[0] == 1] == [
        (1, "Intro", 1), (1, "Cover", 1), (1, "Chapter 2", 4), (1, "Chapter 5", 6),
        (1, "Chapter 5 cont", 6), (1, "Appendix", 9), (1, "Index", 10)]
    # children of Chapter 5 stay with Chapter 5
    assert t[1:4] == [(2, "Intro.a", 2), (2, "Intro.b", 3), (1, "Cover", 1)]
    i5 = t.index((1, "Chapter 5", 6))
    assert t[i5 + 1] == (2, "5.1", 7)


async def test_add_child_in_page_order(c, upload_dir):
    doc_id = store(upload_dir, TOC)
    r = await c.post(f"{P}/{doc_id}/organize/bookmarks", json={"title": "Intro.0", "page": 0, "parent": 0})
    assert r.status_code == 200
    r = await c.post(f"{P}/{doc_id}/organize/bookmarks", json={"title": "Intro.c", "page": 4, "parent": 0})
    assert outline(upload_dir, doc_id)[:5] == [
        (1, "Intro", 1), (2, "Intro.0", 1), (2, "Intro.a", 2), (2, "Intro.b", 3), (2, "Intro.c", 5)]
    assert (await c.post(f"{P}/{doc_id}/organize/bookmarks", json={"title": "x", "page": 0, "parent": 99})).status_code == 404


async def test_indent_outdent_carry_children(c, upload_dir):
    doc_id = store(upload_dir, TOC)
    # nest Chapter 5 (with child 5.1) under Intro
    r = await c.post(f"{P}/{doc_id}/organize/bookmarks/3/indent")
    assert r.status_code == 200, r.text
    assert outline(upload_dir, doc_id) == [
        (1, "Intro", 1), (2, "Intro.a", 2), (2, "Intro.b", 3), (2, "Chapter 5", 6), (3, "5.1", 7), (1, "Appendix", 9)]
    # un-nest it again: next sibling of its parent, child still attached
    r = await c.post(f"{P}/{doc_id}/organize/bookmarks/3/outdent")
    assert r.status_code == 200
    assert outline(upload_dir, doc_id) == [tuple(x) for x in TOC]
    # un-nest a middle child: it follows its old parent's subtree
    r = await c.post(f"{P}/{doc_id}/organize/bookmarks/1/outdent")
    assert outline(upload_dir, doc_id)[:3] == [(1, "Intro", 1), (2, "Intro.b", 3), (1, "Intro.a", 2)]
    assert (await c.post(f"{P}/{doc_id}/organize/bookmarks/0/indent")).status_code == 400
    assert (await c.post(f"{P}/{doc_id}/organize/bookmarks/0/outdent")).status_code == 400
    # PATCH level change also carries children (old code produced an invalid outline -> 400)
    doc_id2 = store(upload_dir, TOC)
    r = await c.patch(f"{P}/{doc_id2}/organize/bookmarks/3", json={"level": 2})
    assert r.status_code == 200
    assert outline(upload_dir, doc_id2)[3:5] == [(2, "Chapter 5", 6), (3, "5.1", 7)]


async def test_drag_reorder_moves_subtree_and_keeps_destinations(c, upload_dir):
    toc = [list(x) for x in TOC]
    doc_id = store(upload_dir, toc)
    d = fitz.open(str(upload_dir / doc_id / "original.pdf"))
    full = d.get_toc(simple=False)
    full[3][3]["color"] = (1.0, 0.0, 0.0)  # styled bookmark must survive the move
    full[3][3]["bold"] = True
    d.set_toc(full)
    d.saveIncr()
    d.close()

    # drag Chapter 5 (and 5.1) before Intro
    r = await c.post(f"{P}/{doc_id}/organize/bookmarks/3/move", json={"target": 0, "position": "before"})
    assert r.status_code == 200, r.text
    assert r.json()["index"] == 0
    assert outline(upload_dir, doc_id) == [
        (1, "Chapter 5", 6), (2, "5.1", 7), (1, "Intro", 1), (2, "Intro.a", 2), (2, "Intro.b", 3), (1, "Appendix", 9)]
    d = fitz.open(str(upload_dir / doc_id / "original.pdf"))
    moved = d.get_toc(simple=False)[0]
    assert moved[3].get("color") == pytest.approx((1.0, 0.0, 0.0)) and moved[3].get("bold")
    d.close()
    # drop Appendix inside Intro (last child)
    r = await c.post(f"{P}/{doc_id}/organize/bookmarks/5/move", json={"target": 2, "position": "inside"})
    assert outline(upload_dir, doc_id)[2:6] == [(1, "Intro", 1), (2, "Intro.a", 2), (2, "Intro.b", 3), (2, "Appendix", 9)]
    # drop 5.1 after Intro.a
    r = await c.post(f"{P}/{doc_id}/organize/bookmarks/1/move", json={"target": 3, "position": "after"})
    assert outline(upload_dir, doc_id) == [
        (1, "Chapter 5", 6), (1, "Intro", 1), (2, "Intro.a", 2), (2, "5.1", 7), (2, "Intro.b", 3), (2, "Appendix", 9)]
    # cannot drop a bookmark into its own subtree
    bad = await c.post(f"{P}/{doc_id}/organize/bookmarks/1/move", json={"target": 2, "position": "inside"})
    assert bad.status_code == 400
