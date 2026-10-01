"""Resize keeps annotations and links (organize2). Asserts on the real saved PDF."""

from __future__ import annotations

import uuid
from pathlib import Path

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


def store(upload_dir: Path, doc) -> str:
    doc_id = str(uuid.uuid4())
    (upload_dir / doc_id).mkdir()
    (upload_dir / doc_id / "original.pdf").write_bytes(doc.tobytes())
    return doc_id


def reopen(upload_dir, doc_id) -> fitz.Document:
    return fitz.open(str(upload_dir / doc_id / "original.pdf"))


def annots(doc, pno):
    page = doc[pno]
    _KEEP.append(page)
    return list(page.annots())


def word_rect(page, word) -> fitz.Rect:
    hits = [fitz.Rect(w[:4]) for w in page.get_text("words") if w[4] == word]
    assert hits, f"{word!r} not on page: {page.get_text()!r}"
    return hits[0]


def words_under(page, rect) -> list[str]:
    out = []
    for w in page.get_text("words"):
        ctr = fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
        if ctr in rect:
            out.append(w[4])
    return out


def annotated_pdf(rotate: int = 0) -> fitz.Document:
    doc = fitz.open()
    for _ in range(2):
        doc.new_page(width=612, height=792)
    p0, p1 = doc[0], doc[1]
    _KEEP.extend([p0, p1])
    p0.insert_text((72, 100), "HIGHLIGHTME and other words", fontsize=14)
    p0.insert_text((72, 300), "LINKWORD target", fontsize=14)
    p0.insert_text((300, 500), "NOTEHERE", fontsize=14)
    p1.insert_text((72, 100), "second page", fontsize=14)
    p1.insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(72, 80, 200, 110), "page": 0, "to": fitz.Point(72, 100)})

    hl = p0.add_highlight_annot(p0.search_for("HIGHLIGHTME", quads=True))
    hl.set_info(content="hl comment")
    hl.update()
    ink = p0.add_ink_annot([[(100, 600), (150, 650), (200, 600)]])
    ink.update()
    note = p0.add_text_annot(word_rect(p0, "NOTEHERE").tl, "sticky body")
    note.update()
    lr = word_rect(p0, "LINKWORD")
    p0.insert_link({"kind": fitz.LINK_URI, "from": lr, "uri": "https://example.com/x"})
    p0.insert_link({"kind": fitz.LINK_GOTO, "from": word_rect(p0, "target"), "page": 1, "to": fitz.Point(0, 0)})
    if rotate:
        p0.set_rotation(rotate)
    return doc


@pytest.mark.parametrize("rotate", [0, 90])
async def test_resize_keeps_and_transforms_annotations_and_links(c, upload_dir, rotate):
    doc_id = store(upload_dir, annotated_pdf(rotate))
    before = reopen(upload_dir, doc_id)
    kinds_before = sorted(a.type[1] for a in annots(before, 0))
    ink_vis_before = [fitz.Point(pt) * before[0].rotation_matrix for pt in annots(before, 0)[1].vertices[0]]
    before.close()

    r = await c.post(f"{P}/{doc_id}/organize/resize", json={"pages": [0], "size": "tabloid"})
    assert r.status_code == 200, r.text

    d = reopen(upload_dir, doc_id)
    page = d[0]
    assert page.rotation == rotate  # visible orientation preserved
    exp = (1224, 792) if rotate else (792, 1224)
    assert (round(page.rect.width), round(page.rect.height)) == exp

    got = annots(d, 0)
    assert sorted(a.type[1] for a in got) == kinds_before  # nothing dropped
    by = {a.type[1]: a for a in got}

    # highlight: quads still sit exactly on the highlighted word in the scaled content
    hl = by["Highlight"]
    v = hl.vertices
    quad_rects = [fitz.Quad(v[i:i + 4]).rect for i in range(0, len(v), 4)]
    assert any(words_under(page, q) == ["HIGHLIGHTME"] for q in quad_rects), (quad_rects, page.get_text("words"))
    assert hl.info["content"] == "hl comment"
    assert hl.rect.contains(word_rect(page, "HIGHLIGHTME"))

    # note: still next to its word, contents kept
    note = by["Text"]
    nr = word_rect(page, "NOTEHERE")
    assert note.info["content"] == "sticky body"
    # the icon is a fixed 16pt NoZoom box; on a rotated page MuPDF anchors it at the
    # visible top-left, so allow one icon height of slack
    assert abs(note.rect.x0 - nr.x0) < 3 and abs(note.rect.y0 - nr.y0) < 17

    # ink: scaled by the same factor as the content
    s = 792 / 612  # min(target_w / visible_w, target_h / visible_h) in both orientations
    ink_after = by["Ink"].vertices[0]
    assert len(ink_after) == 3
    d01_before = abs(ink_vis_before[2].x - ink_vis_before[0].x) + abs(ink_vis_before[2].y - ink_vis_before[0].y)
    d01_after = abs(ink_after[2][0] - ink_after[0][0]) + abs(ink_after[2][1] - ink_after[0][1])
    assert d01_after == pytest.approx(d01_before * s, rel=0.02)

    # links: URI and internal still work and cover their words
    links = page.get_links()  # "from" is in visible (rotated) space

    uri = [lk for lk in links if lk["kind"] == fitz.LINK_URI][0]
    assert uri["uri"] == "https://example.com/x"
    assert words_under(page, uri["from"] * page.derotation_matrix) == ["LINKWORD"]
    goto = [lk for lk in links if lk["kind"] == fitz.LINK_GOTO][0]
    assert goto["page"] == 1 and words_under(page, goto["from"] * page.derotation_matrix) == ["target"]
    # a link on ANOTHER page that points at the resized page is still valid
    back = [lk for lk in d[1].get_links() if lk["kind"] == fitz.LINK_GOTO]
    assert back and back[0]["page"] == 0
    d.close()


async def test_resize_centered_without_scaling_keeps_annot_offset(c, upload_dir):
    doc_id = store(upload_dir, annotated_pdf())
    b = reopen(upload_dir, doc_id)
    x0_before = [a for a in annots(b, 0) if a.type[1] == "Highlight"][0].rect.x0
    b.close()
    r = await c.post(f"{P}/{doc_id}/organize/resize", json={"pages": [0], "size": "tabloid", "scale_content": False})
    assert r.status_code == 200
    d = reopen(upload_dir, doc_id)
    hl = [a for a in annots(d, 0) if a.type[1] == "Highlight"][0]
    v = hl.vertices
    assert words_under(d[0], fitz.Quad(v[0:4]).rect) == ["HIGHLIGHTME"]
    assert hl.rect.x0 == pytest.approx(x0_before + (792 - 612) / 2, abs=0.5)
    d.close()
