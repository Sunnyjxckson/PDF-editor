"""Tests for backend/features/organize.py, run against real PDFs built in-test.

The router is mounted on a private FastAPI app (plus advanced_ops for undo),
with advanced_ops.UPLOAD_DIR pointed at a temp dir. Every test re-opens the
stored PDF and asserts on its actual content, not just on status codes.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path

import fitz
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend import advanced_ops
from backend.features.organize import router as organize_router

P = "/api/pdf"


# ─── fixtures / helpers ──────────────────────────────────────────────────────


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


def make_pdf(texts: list[str], width=612, height=792, toc=None) -> fitz.Document:
    doc = fitz.open()
    for t in texts:
        page = doc.new_page(width=width, height=height)
        page.insert_text((72, 100), t, fontsize=14)
    if toc:
        doc.set_toc(toc)
    return doc


def store(upload_dir: Path, doc: fitz.Document | bytes) -> str:
    doc_id = str(uuid.uuid4())
    d = upload_dir / doc_id
    d.mkdir()
    (d / "original.pdf").write_bytes(doc if isinstance(doc, bytes) else doc.tobytes())
    return doc_id


def reopen(upload_dir: Path, doc_id: str) -> fitz.Document:
    return fitz.open(str(upload_dir / doc_id / "original.pdf"))


def page_texts(doc: fitz.Document) -> list[str]:
    return [p.get_text().strip() for p in doc]


def vis_words(page: fitz.Page) -> list[tuple[str, fitz.Rect]]:
    """Words with rects in VISIBLE (rotated, top-left) page points."""
    out = []
    for w in page.get_text("words"):
        r = fitz.Rect(w[:4]) * page.rotation_matrix
        r.normalize()
        out.append((w[4], r))
    return out


_LIVE_PAGES: list = []


def annots(doc: fitz.Document, pno: int) -> list:
    """List a page's annots. PyMuPDF annots hold only a weak ref to their page;
    if the page object is garbage-collected first, touching annot.rect segfaults,
    so keep the page alive."""
    page = doc[pno]
    _LIVE_PAGES.append(page)
    return list(page.annots())


def marked_text(page: fitz.Page, annot) -> str:
    """Words whose centre lies inside a text-markup annot's quads (not its padded /Rect)."""
    v = annot.vertices
    quads = [fitz.Quad(v[i:i + 4]).rect for i in range(0, len(v), 4)]
    out = []
    for w in page.get_text("words"):
        ctr = fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
        if any(ctr in q for q in quads):
            out.append(w[4])
    return " ".join(out)


def history_ops(upload_dir: Path, doc_id: str) -> list[str]:
    hf = upload_dir / doc_id / "history.json"
    if not hf.exists():
        return []
    return [v["operation"] for v in json.loads(hf.read_text())["versions"]]


# ─── validation ──────────────────────────────────────────────────────────────


async def test_rejects_bad_and_missing_doc_ids(c):
    r = await c.post(f"{P}/..%2F..%2Fetc/organize/rotate", json={"pages": [0]})
    assert r.status_code in (400, 404)
    r = await c.post(f"{P}/not-a-uuid/organize/rotate", json={"pages": [0]})
    assert r.status_code == 400
    r = await c.post(f"{P}/{uuid.uuid4()}/organize/rotate", json={"pages": [0]})
    assert r.status_code == 404


# ─── insert blank / insert from file ─────────────────────────────────────────


async def test_insert_blank_neighbor_letter_a4_and_undo(c, upload_dir):
    doc_id = store(upload_dir, make_pdf(["A", "B"], width=400, height=500))

    r = await c.post(f"{P}/{doc_id}/organize/insert-blank", json={"position": 1, "size": "neighbor"})
    assert r.status_code == 200, r.text
    r = await c.post(f"{P}/{doc_id}/organize/insert-blank", json={"position": 0, "size": "a4"})
    assert r.status_code == 200
    r = await c.post(f"{P}/{doc_id}/organize/insert-blank",
                     json={"position": 4, "size": "letter", "landscape": True})
    assert r.status_code == 200
    assert r.json()["page_count"] == 5

    d = reopen(upload_dir, doc_id)
    assert page_texts(d) == ["", "A", "", "B", ""]
    sizes = [(round(p.rect.width), round(p.rect.height)) for p in d]
    assert sizes == [(595, 842), (400, 500), (400, 500), (400, 500), (792, 612)]
    d.close()

    assert history_ops(upload_dir, doc_id)[0].startswith("Insert 1 blank")
    r = await c.post(f"{P}/{doc_id}/undo")
    assert r.status_code == 200
    d = reopen(upload_dir, doc_id)
    assert len(d) == 4  # last insert undone
    d.close()

    r = await c.post(f"{P}/{doc_id}/organize/insert-blank", json={"position": 9})
    assert r.status_code == 400


async def test_insert_pages_from_uploaded_file_with_range(c, upload_dir):
    doc_id = store(upload_dir, make_pdf(["A", "B"]))
    other = make_pdf(["X1", "X2", "X3", "X4"])
    other[2].add_text_annot((50, 50), "carried note")
    files = {"file": ("o.pdf", other.tobytes(), "application/pdf")}
    r = await c.post(f"{P}/{doc_id}/organize/insert-file",
                     data={"position": "1", "page_from": "1", "page_to": "2"}, files=files)
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 2

    d = reopen(upload_dir, doc_id)
    assert page_texts(d) == ["A", "X2", "X3", "B"]
    assert [a.info["content"] for a in annots(d, 2)] == ["carried note"]
    d.close()

    # by reference to another stored doc, full range
    src_id = store(upload_dir, make_pdf(["S1", "S2"]))
    r = await c.post(f"{P}/{doc_id}/organize/insert-file", data={"position": "4", "source_doc_id": src_id})
    assert r.status_code == 200
    d = reopen(upload_dir, doc_id)
    assert page_texts(d) == ["A", "X2", "X3", "B", "S1", "S2"]
    d.close()

    r = await c.post(f"{P}/{doc_id}/organize/insert-file",
                     data={"position": "0", "page_from": "3", "page_to": "9"}, files=files)
    assert r.status_code == 400


# ─── extract / duplicate / rotate / delete ───────────────────────────────────


async def test_extract_returns_pdf_and_optionally_deletes(c, upload_dir):
    doc_id = store(upload_dir, make_pdf(["P0", "P1", "P2", "P3"]))
    r = await c.post(f"{P}/{doc_id}/organize/extract", json={"pages": [2, 0], "filename": "out"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert 'filename="out.pdf"' in r.headers["content-disposition"]
    out = fitz.open(stream=r.content, filetype="pdf")
    assert page_texts(out) == ["P2", "P0"]
    d = reopen(upload_dir, doc_id)
    assert len(d) == 4  # source untouched
    d.close()

    r = await c.post(f"{P}/{doc_id}/organize/extract", json={"pages": [1, 3], "delete_after": True})
    assert r.status_code == 200
    assert page_texts(fitz.open(stream=r.content, filetype="pdf")) == ["P1", "P3"]
    d = reopen(upload_dir, doc_id)
    assert page_texts(d) == ["P0", "P2"]
    d.close()


async def test_duplicate_rotate_delete_multiple(c, upload_dir):
    doc_id = store(upload_dir, make_pdf(["P0", "P1", "P2"]))
    r = await c.post(f"{P}/{doc_id}/organize/duplicate", json={"pages": [0, 2]})
    assert r.status_code == 200 and r.json()["page_count"] == 5
    d = reopen(upload_dir, doc_id)
    assert page_texts(d) == ["P0", "P0", "P1", "P2", "P2"]
    d.close()

    r = await c.post(f"{P}/{doc_id}/organize/rotate", json={"pages": [1, 3], "angle": 90})
    assert r.status_code == 200
    r = await c.post(f"{P}/{doc_id}/organize/rotate", json={"pages": [3], "angle": 90})
    r = await c.post(f"{P}/{doc_id}/organize/rotate", json={"pages": [4], "angle": 270, "relative": False})
    d = reopen(upload_dir, doc_id)
    assert [p.rotation for p in d] == [0, 90, 0, 180, 270]
    d.close()
    assert (await c.post(f"{P}/{doc_id}/organize/rotate", json={"pages": [0], "angle": 45})).status_code == 400

    r = await c.post(f"{P}/{doc_id}/organize/delete", json={"pages": [4, 0, 2]})
    assert r.status_code == 200 and r.json()["page_count"] == 2
    d = reopen(upload_dir, doc_id)
    assert page_texts(d) == ["P0", "P2"]
    d.close()
    r = await c.post(f"{P}/{doc_id}/organize/delete", json={"pages": [0, 1]})
    assert r.status_code == 400
    r = await c.post(f"{P}/{doc_id}/organize/delete", json={"pages": [7]})
    assert r.status_code == 400


# ─── crop / resize ───────────────────────────────────────────────────────────


async def test_crop_box_margins_reset(c, upload_dir):
    doc_id = store(upload_dir, make_pdf(["P0", "P1"]))
    r = await c.post(f"{P}/{doc_id}/organize/crop", json={"pages": [0], "mode": "box", "box": [50, 60, 350, 460]})
    assert r.status_code == 200, r.text
    r = await c.post(f"{P}/{doc_id}/organize/crop",
                     json={"pages": [1], "mode": "margins", "margins": {"top": 10, "right": 20, "bottom": 30, "left": 40}})
    assert r.status_code == 200
    d = reopen(upload_dir, doc_id)
    assert tuple(round(v) for v in d[0].rect) == (0, 0, 300, 400)
    # cropbox in PDF (bottom-left) space: y = 792-460 .. 792-60
    assert tuple(round(v) for v in d[0].cropbox) == (50, 60, 350, 460)  # PyMuPDF reports top-left
    assert (round(d[1].rect.width), round(d[1].rect.height)) == (552, 752)
    d.close()

    r = await c.post(f"{P}/{doc_id}/organize/crop", json={"pages": [0, 1], "mode": "reset"})
    d = reopen(upload_dir, doc_id)
    assert [(round(p.rect.width), round(p.rect.height)) for p in d] == [(612, 792), (612, 792)]
    d.close()


async def test_auto_crop_removes_white_margins(c, upload_dir):
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.draw_rect(fitz.Rect(200, 300, 400, 500), color=(0, 0, 0), fill=(0, 0, 0))
    blank = doc.new_page(width=612, height=792)  # noqa: F841
    doc_id = store(upload_dir, doc)
    r = await c.post(f"{P}/{doc_id}/organize/crop", json={"pages": [0, 1], "mode": "auto", "padding": 10})
    assert r.status_code == 200, r.text
    assert r.json()["pages"][1]["skipped"] == "blank page"
    d = reopen(upload_dir, doc_id)
    cb = d[0].cropbox
    assert abs(cb.x0 - 190) <= 2 and abs(cb.y0 - 290) <= 2
    assert abs(cb.x1 - 410) <= 2 and abs(cb.y1 - 510) <= 2
    assert d[1].rect == fitz.Rect(0, 0, 612, 792)
    d.close()


async def test_crop_on_rotated_page_uses_visible_coordinates(c, upload_dir):
    doc = make_pdf(["P0"])
    doc[0].set_rotation(90)  # visible page is 792 wide x 612 tall
    doc_id = store(upload_dir, doc)
    r = await c.post(f"{P}/{doc_id}/organize/crop", json={"pages": [0], "mode": "box", "box": [0, 0, 400, 300]})
    assert r.status_code == 200, r.text
    d = reopen(upload_dir, doc_id)
    assert (round(d[0].rect.width), round(d[0].rect.height)) == (400, 300)
    assert d[0].rotation == 90
    d.close()


async def test_resize_scales_content_and_keeps_text(c, upload_dir):
    doc_id = store(upload_dir, make_pdf(["Alpha", "Beta"], width=612, height=792))
    r = await c.post(f"{P}/{doc_id}/organize/resize", json={"pages": [1], "size": "a4"})
    assert r.status_code == 200, r.text
    d = reopen(upload_dir, doc_id)
    assert (round(d[0].rect.width), round(d[0].rect.height)) == (612, 792)
    assert (round(d[1].rect.width), round(d[1].rect.height)) == (595, 842)
    assert page_texts(d) == ["Alpha", "Beta"]  # vector content kept, still text
    # content scaled by 595.276/612 and vertically centred
    (word, rect), = vis_words(d[1])
    assert word == "Beta" and rect.x0 < 72  # 72 * 0.9727 = 70.0
    d.close()

    r = await c.post(f"{P}/{doc_id}/organize/resize",
                     json={"pages": [0], "size": "custom", "width": 1224, "height": 1584, "scale_content": False})
    d = reopen(upload_dir, doc_id)
    (word, rect), = vis_words(d[0])
    assert word == "Alpha" and abs(rect.x0 - (306 + 72)) < 1  # centred at 100%
    d.close()


# ─── split ───────────────────────────────────────────────────────────────────


async def test_split_every_n_creates_new_docs(c, upload_dir):
    doc_id = store(upload_dir, make_pdf([f"P{i}" for i in range(5)]))
    r = await c.post(f"{P}/{doc_id}/organize/split", json={"mode": "every_n", "n": 2})
    assert r.status_code == 200, r.text
    docs = r.json()["documents"]
    assert [x["pages"] for x in docs] == [[0, 1], [2, 3], [4]]
    assert page_texts(reopen(upload_dir, docs[1]["id"])) == ["P2", "P3"]
    assert len(reopen(upload_dir, doc_id)) == 5


async def test_split_by_bookmarks_and_zip_download(c, upload_dir):
    toc = [[1, "Intro", 2], [2, "Sub", 3], [1, "Body", 4]]
    doc_id = store(upload_dir, make_pdf([f"P{i}" for i in range(6)], toc=toc))
    r = await c.post(f"{P}/{doc_id}/organize/split", json={"mode": "bookmarks", "level": 1, "download": True})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = sorted(zf.namelist())
    assert names == ["01_front_matter.pdf", "02_Intro.pdf", "03_Body.pdf"]
    assert page_texts(fitz.open(stream=zf.read("02_Intro.pdf"), filetype="pdf")) == ["P1", "P2"]
    assert page_texts(fitz.open(stream=zf.read("03_Body.pdf"), filetype="pdf")) == ["P3", "P4", "P5"]

    r = await c.post(f"{P}/{doc_id}/organize/split", json={"mode": "bookmarks", "level": 3})
    assert r.status_code == 400


async def test_split_by_size(c, upload_dir):
    doc = fitz.open()
    import os
    for i in range(4):
        page = doc.new_page()
        # ~60KB of incompressible image data per page
        raw = bytearray(os.urandom(150 * 150 * 3))
        pix = fitz.Pixmap(fitz.csRGB, 150, 150, raw, False)
        page.insert_image(fitz.Rect(0, 0, 300, 300), pixmap=pix)
        page.insert_text((72, 400), f"P{i}")
    doc_id = store(upload_dir, doc)
    r = await c.post(f"{P}/{doc_id}/organize/split", json={"mode": "size", "max_mb": 0.12})
    assert r.status_code == 200, r.text
    docs = r.json()["documents"]
    assert len(docs) >= 2
    assert sum(len(x["pages"]) for x in docs) == 4
    for x in docs:
        if len(x["pages"]) > 1:
            assert x["size"] <= 0.12 * 1024 * 1024


# ─── headers / footers / page numbers / Bates ────────────────────────────────


async def test_page_numbers_skip_first_and_start_number(c, upload_dir):
    doc_id = store(upload_dir, make_pdf(["Cover", "A", "B", "C"]))
    r = await c.post(f"{P}/{doc_id}/organize/page-numbers", json={
        "format": "Page {n} of {total}", "position": "bottom-center",
        "skip_first": True, "start_number": 1, "font_size": 10,
    })
    assert r.status_code == 200, r.text
    assert r.json()["stamped_count"] == 3
    d = reopen(upload_dir, doc_id)
    assert "Page" not in d[0].get_text()
    for i, expect in [(1, "Page 1 of 3"), (2, "Page 2 of 3"), (3, "Page 3 of 3")]:
        assert expect in d[i].get_text()
        words = [r for w, r in vis_words(d[i]) if w == "Page"]
        assert words and words[0].y1 > 792 - 36 - 12 and words[0].y1 <= 792 - 30
        line = fitz.Rect()
        for w, rr in vis_words(d[i]):
            if rr.y0 > 700:
                line |= rr
        assert abs((line.x0 + line.x1) / 2 - 306) < 3  # centred
    d.close()
    assert history_ops(upload_dir, doc_id) == ["Add header/footer"]


async def test_page_numbers_on_rotated_page_land_at_visible_bottom(c, upload_dir):
    doc = make_pdf(["A"])
    doc[0].set_rotation(90)
    doc_id = store(upload_dir, doc)
    r = await c.post(f"{P}/{doc_id}/organize/page-numbers", json={"format": "{n}", "position": "bottom-right", "start_number": 7})
    assert r.status_code == 200
    d = reopen(upload_dir, doc_id)
    hits = [rr for w, rr in vis_words(d[0]) if w == "7"]
    assert hits, d[0].get_text()
    r7 = hits[0]
    assert r7.y1 > 612 - 50 and r7.x1 > 792 - 72 - 2 and r7.x1 <= 792 - 70
    assert r7.width < r7.height * 2  # upright single glyph, not sideways strip
    d.close()


async def test_header_footer_all_slots_and_tokens(c, upload_dir):
    doc_id = store(upload_dir, make_pdf(["A", "B"]))
    body = {
        "header_left": "CONFIDENTIAL", "header_right": "{date}",
        "footer_left": "Doc {bates}", "footer_right": "{n}/{total}",
        "bates_prefix": "ACME-", "bates_digits": 4, "bates_start": 10,
        "date_format": "%Y", "font": "tibo", "color": [1, 0, 0],
    }
    r = await c.post(f"{P}/{doc_id}/organize/header-footer/preview", json=body)
    assert r.status_code == 200
    prev = r.json()["pages"]
    assert prev[1]["texts"]["footer_left"] == "Doc ACME-0011"
    assert len(reopen(upload_dir, doc_id)[0].get_text()) < 5  # preview does not write

    r = await c.post(f"{P}/{doc_id}/organize/header-footer", json=body)
    assert r.status_code == 200
    d = reopen(upload_dir, doc_id)
    t1 = d[1].get_text()
    assert "CONFIDENTIAL" in t1 and "Doc ACME-0011" in t1 and "2/2" in t1
    spans = [s for b in d[0].get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]]
    conf = [s for s in spans if s["text"] == "CONFIDENTIAL"][0]
    assert conf["color"] == 0xFF0000 and "Bold" in conf["font"]
    assert conf["bbox"][1] < 60  # header near top
    d.close()

    r = await c.post(f"{P}/{doc_id}/organize/header-footer", json={**body, "font": "comic-sans"})
    assert r.status_code == 400


async def test_bates_numbering(c, upload_dir):
    doc_id = store(upload_dir, make_pdf(["A", "B", "C"]))
    r = await c.post(f"{P}/{doc_id}/organize/bates", json={"prefix": "SMITH", "digits": 6, "start": 100})
    assert r.status_code == 200, r.text
    assert r.json()["first_bates"] == "SMITH000100"
    d = reopen(upload_dir, doc_id)
    for i in range(3):
        hits = [rr for w, rr in vis_words(d[i]) if w == f"SMITH{100 + i:06d}"]
        assert hits and hits[0].x1 > 612 - 75 and hits[0].y1 > 740
    d.close()


# ─── bookmarks ───────────────────────────────────────────────────────────────


async def test_bookmarks_crud(c, upload_dir):
    doc_id = store(upload_dir, make_pdf(["A", "B", "C"], toc=[[1, "One", 1], [2, "One.a", 2]]))
    r = await c.get(f"{P}/{doc_id}/organize/bookmarks")
    assert r.json()["bookmarks"] == [
        {"index": 0, "level": 1, "title": "One", "page": 0},
        {"index": 1, "level": 2, "title": "One.a", "page": 1},
    ]
    r = await c.post(f"{P}/{doc_id}/organize/bookmarks", json={"title": "Two", "page": 2})
    assert r.status_code == 200
    r = await c.patch(f"{P}/{doc_id}/organize/bookmarks/1", json={"title": "Renamed", "page": 2})
    assert r.status_code == 200
    d = reopen(upload_dir, doc_id)
    assert [t[:3] for t in d.get_toc()] == [[1, "One", 1], [2, "Renamed", 3], [1, "Two", 3]]
    d.close()

    r = await c.delete(f"{P}/{doc_id}/organize/bookmarks/0")
    assert r.json()["removed"] == 2  # parent + child
    d = reopen(upload_dir, doc_id)
    assert [t[:3] for t in d.get_toc()] == [[1, "Two", 3]]
    d.close()

    r = await c.put(f"{P}/{doc_id}/organize/bookmarks", json={"bookmarks": [
        {"level": 1, "title": "X", "page": 0}, {"level": 2, "title": "Y", "page": 1}]})
    assert r.status_code == 200
    assert [t[:3] for t in reopen(upload_dir, doc_id).get_toc()] == [[1, "X", 1], [2, "Y", 2]]

    bad = await c.put(f"{P}/{doc_id}/organize/bookmarks", json={"bookmarks": [{"level": 2, "title": "Z", "page": 0}]})
    assert bad.status_code == 400
    bad = await c.post(f"{P}/{doc_id}/organize/bookmarks", json={"title": "Z", "page": 99})
    assert bad.status_code == 400
    assert (await c.delete(f"{P}/{doc_id}/organize/bookmarks/9")).status_code == 404


# ─── comments / markup ───────────────────────────────────────────────────────


async def test_text_markup_over_quads_area_and_search(c, upload_dir):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "The quick brown fox jumps", fontsize=14)
    page.insert_text((72, 200), "second line of text", fontsize=14)
    doc_id = store(upload_dir, doc)

    words = {w[4]: fitz.Rect(w[:4]) for w in page.get_text("words")}
    q = words["quick"] | words["brown"]
    r = await c.post(f"{P}/{doc_id}/organize/comments", json={
        "page": 0, "type": "highlight", "quads": [list(q)], "text": "check this", "author": "Ann"})
    assert r.status_code == 200, r.text
    r = await c.post(f"{P}/{doc_id}/organize/comments", json={"page": 0, "type": "strikeout", "search": "fox"})
    assert r.status_code == 200
    r = await c.post(f"{P}/{doc_id}/organize/comments",
                     json={"page": 0, "type": "underline", "area": [60, 180, 400, 210]})
    assert r.status_code == 200
    r = await c.post(f"{P}/{doc_id}/organize/comments", json={"page": 0, "type": "squiggly", "search": "jumps"})
    assert r.status_code == 200
    r = await c.post(f"{P}/{doc_id}/organize/comments", json={"page": 0, "type": "highlight", "search": "zebra"})
    assert r.status_code == 400

    d = reopen(upload_dir, doc_id)
    by_type = {a.type[1]: a for a in annots(d, 0)}
    assert set(by_type) == {"Highlight", "StrikeOut", "Underline", "Squiggly"}
    hl = by_type["Highlight"]
    assert hl.info["title"] == "Ann" and hl.info["content"] == "check this"
    # the highlighted text is exactly the selected words
    pg = d[0]
    assert marked_text(pg, hl) == "quick brown"
    assert marked_text(pg, by_type["StrikeOut"]) == "fox"
    assert marked_text(pg, by_type["Underline"]) == "second line of text"
    assert marked_text(pg, by_type["Squiggly"]) == "jumps"
    assert len(hl.vertices) == 4
    d.close()


async def test_shapes_notes_callout_stamp_ink(c, upload_dir):
    doc_id = store(upload_dir, make_pdf(["A"]))
    bodies = [
        {"type": "note", "rect": [100, 100, 120, 120], "text": "sticky"},
        {"type": "freetext", "rect": [100, 150, 300, 180], "text": "typed words", "color": [0, 0, 1]},
        {"type": "callout", "rect": [300, 300, 450, 340], "text": "look here", "callout": [[200, 400], [300, 320]]},
        {"type": "rect", "rect": [50, 500, 150, 560], "color": [0, 1, 0], "fill": [1, 1, 0], "width": 3},
        {"type": "ellipse", "rect": [200, 500, 300, 560]},
        {"type": "line", "points": [[50, 600], [200, 600]]},
        {"type": "arrow", "points": [[50, 650], [200, 700]]},
        {"type": "polygon", "points": [[300, 600], [400, 600], [350, 700]]},
        {"type": "ink", "points": [[400, 400], [420, 420], [440, 400]]},
        {"type": "stamp", "rect": [400, 50, 560, 100], "stamp": "Approved"},
    ]
    for b in bodies:
        r = await c.post(f"{P}/{doc_id}/organize/comments", json={"page": 0, "author": "Bob", "opacity": 0.8, **b})
        assert r.status_code == 200, (b["type"], r.text)

    d = reopen(upload_dir, doc_id)
    kinds = sorted(a.type[1] for a in annots(d, 0))
    assert kinds == sorted(["Text", "FreeText", "FreeText", "Square", "Circle", "Line", "Line",
                            "Polygon", "Ink", "Stamp"])
    for a in annots(d, 0):
        assert a.info["title"] == "Bob"
        assert a.info["creationDate"].startswith("D:")
    sq = [a for a in annots(d, 0) if a.type[1] == "Square"][0]
    assert sq.colors["stroke"] == [0.0, 1.0, 0.0] and sq.colors["fill"] == [1.0, 1.0, 0.0]
    assert sq.border["width"] == 3 and abs(sq.opacity - 0.8) < 1e-6
    assert tuple(round(v) for v in sq.rect) == (49, 499, 151, 561) or sq.rect.contains(fitz.Rect(50, 500, 150, 560))
    lines = [a for a in annots(d, 0) if a.type[1] == "Line"]
    assert sorted(tuple(a.line_ends) for a in lines) == [(0, 0), (0, fitz.PDF_ANNOT_LE_OPEN_ARROW)]
    callout = [a for a in annots(d, 0) if a.info["content"] == "look here"][0]
    assert "/FreeTextCallout" in d.xref_object(callout.xref)
    assert "/CL" in d.xref_object(callout.xref)
    d.close()

    r = await c.get(f"{P}/{doc_id}/organize/comments")
    types = sorted(x["type"] for x in r.json()["comments"])
    assert types == sorted(["note", "freetext", "freetext", "rect", "ellipse", "line", "arrow",
                            "polygon", "ink", "stamp"])

    r = await c.post(f"{P}/{doc_id}/organize/comments", json={"page": 0, "type": "stamp", "rect": [0, 0, 9, 9], "stamp": "Nope"})
    assert r.status_code == 400
    r = await c.post(f"{P}/{doc_id}/organize/comments", json={"page": 5, "type": "note", "rect": [0, 0, 9, 9]})
    assert r.status_code == 400


async def test_comment_on_rotated_page_lands_where_drawn(c, upload_dir):
    doc = make_pdf(["A"])
    doc[0].set_rotation(90)
    doc_id = store(upload_dir, doc)
    r = await c.post(f"{P}/{doc_id}/organize/comments",
                     json={"page": 0, "type": "rect", "rect": [600, 50, 700, 150]})
    assert r.status_code == 200
    assert r.json()["comment"]["rect"][0] == pytest.approx(600, abs=3)
    assert r.json()["comment"]["rect"][1] == pytest.approx(50, abs=3)
    d = reopen(upload_dir, doc_id)
    a = annots(d, 0)[0]
    vis = a.rect * d[0].rotation_matrix
    vis.normalize()
    assert vis.x0 == pytest.approx(600, abs=3) and vis.y1 == pytest.approx(150, abs=3)
    d.close()


async def test_comment_thread_reply_status_edit_delete(c, upload_dir):
    doc_id = store(upload_dir, make_pdf(["A", "B"]))
    r = await c.post(f"{P}/{doc_id}/organize/comments",
                     json={"page": 1, "type": "note", "rect": [100, 100, 120, 120], "text": "root", "author": "Ann"})
    root = r.json()["comment"]["id"]
    other = (await c.post(f"{P}/{doc_id}/organize/comments",
                          json={"page": 1, "type": "rect", "rect": [10, 10, 50, 50]})).json()["comment"]["id"]

    r = await c.post(f"{P}/{doc_id}/organize/comments/{root}/reply", json={"text": "agree", "author": "Bob"})
    assert r.status_code == 200
    reply_id = r.json()["reply"]["id"]
    r = await c.post(f"{P}/{doc_id}/organize/comments/{root}/status", json={"status": "Accepted", "author": "Bob"})
    assert r.status_code == 200
    assert (await c.post(f"{P}/{doc_id}/organize/comments/{root}/status", json={"status": "Meh"})).status_code == 400

    # Real PDF structure Acrobat reads: /IRT on reply, /StateModel + /State on status
    d = reopen(upload_dir, doc_id)
    by = {a.xref: a for a in annots(d, 1)}
    assert by[reply_id].irt_xref == root
    state = [a for a in by.values() if d.xref_get_key(a.xref, "StateModel")[0] != "null"][0]
    assert d.xref_get_key(state.xref, "State")[1].strip("()") == "Accepted"
    assert state.irt_xref == root
    d.close()

    r = await c.get(f"{P}/{doc_id}/organize/comments")
    comments = r.json()["comments"]
    assert len(comments) == 2  # replies and states are nested, not top level
    rc = [x for x in comments if x["id"] == root][0]
    assert rc["author"] == "Ann" and rc["contents"] == "root" and rc["page"] == 1
    assert rc["status"] == "Accepted"
    assert [(x["author"], x["contents"]) for x in rc["replies"]] == [("Bob", "agree")]
    assert rc["created"] and rc["created"].endswith("Z")

    r = await c.patch(f"{P}/{doc_id}/organize/comments/{other}",
                      json={"text": "edited", "color": [0, 0, 1], "opacity": 0.4})
    assert r.status_code == 200
    d = reopen(upload_dir, doc_id)
    a = [a for a in annots(d, 1) if a.xref == other][0]
    assert a.info["content"] == "edited" and a.colors["stroke"] == [0.0, 0.0, 1.0]
    assert abs(a.opacity - 0.4) < 1e-6
    d.close()

    r = await c.delete(f"{P}/{doc_id}/organize/comments/{root}")
    assert r.json()["deleted"] == 3  # root + reply + status
    d = reopen(upload_dir, doc_id)
    assert [a.xref for a in annots(d, 1)] == [other]
    d.close()
    assert (await c.delete(f"{P}/{doc_id}/organize/comments/{root}")).status_code == 404
    ops = history_ops(upload_dir, doc_id)
    assert ops[-1] == "Delete comment" and "Reply to comment" in ops


async def test_lists_existing_acrobat_style_comments(c, upload_dir):
    """Comments made by another tool (not us) appear with author/date/content."""
    doc = make_pdf(["A"])
    a = doc[0].add_text_annot((80, 80), "from acrobat")
    a.set_info(title="Carol", creationDate="D:20250102030405+02'00'")
    a.update()
    doc_id = store(upload_dir, doc)
    r = await c.get(f"{P}/{doc_id}/organize/comments")
    (cm,) = r.json()["comments"]
    assert cm["author"] == "Carol" and cm["contents"] == "from acrobat"
    assert cm["created"] == "2025-01-02T03:04:05+02:00"


async def test_stamps_listing(c, upload_dir):
    doc_id = store(upload_dir, make_pdf(["A"]))
    r = await c.get(f"{P}/{doc_id}/organize/stamps")
    assert "Approved" in r.json()["stamps"] and "Confidential" in r.json()["stamps"]
