"""Tests for backend/features/text_edit.py, run against real generated PDFs.

The router is mounted on a private FastAPI app (plus advanced_ops for undo),
with advanced_ops.UPLOAD_DIR pointed at a temp dir. Every test re-opens the
saved PDF and inspects its actual text / fonts / pixels.
"""

import uuid
from pathlib import Path

import fitz
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend import advanced_ops
from backend.features import text_edit as te
from backend.features.text_edit import router as text_edit_router

PARA = ("The quick brown fox jumps over the lazy dog while the cat watches "
        "from the window and the bird sings a song about the morning sun.")


# ─── fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(advanced_ops, "UPLOAD_DIR", tmp_path)
    return tmp_path


@pytest_asyncio.fixture
async def c(upload_dir):
    app = FastAPI()
    app.include_router(text_edit_router)
    app.include_router(advanced_ops.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield client


def store(upload_dir: Path, doc: fitz.Document) -> str:
    doc_id = str(uuid.uuid4())
    d = upload_dir / doc_id
    d.mkdir()
    (d / "original.pdf").write_bytes(doc.tobytes())
    return doc_id


def reopen(upload_dir, doc_id) -> fitz.Document:
    return fitz.open(str(upload_dir / doc_id / "original.pdf"))


def spans_of(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                if s["text"].strip():
                    out.append(s)
    return out


def find_span(page, needle):
    for s in spans_of(page):
        if needle in s["text"]:
            return s
    return None


async def blocks(c, doc_id, page=0):
    r = await c.get(f"/api/pdf/{doc_id}/text-edit/page/{page}")
    assert r.status_code == 200, r.text
    return r.json()


def block_with(data, needle):
    for b in data["blocks"]:
        if needle in b["text"]:
            return b
    raise AssertionError(f"no block with {needle!r}")


def line_with(data, needle):
    for b in data["blocks"]:
        for l in b["lines"]:
            if needle in l["text"]:
                return l
    raise AssertionError(f"no line with {needle!r}")


def span_with(data, needle):
    for b in data["blocks"]:
        for l in b["lines"]:
            for s in l["spans"]:
                if needle in s["text"]:
                    return s
    raise AssertionError(f"no span with {needle!r}")


def three_lines_doc(background=True, embed=False):
    """Three tightly spaced lines (14pt leading at 12pt) over a yellow fill."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    if background:
        page.draw_rect(fitz.Rect(60, 60, 400, 200), color=None, fill=(1, 1, 0))
    kw = {"fontname": "helv"}
    if embed:
        page.insert_font(fontname="EmbH", fontbuffer=fitz.Font("helv").buffer)
        kw = {"fontname": "EmbH"}
    page.insert_text((72, 100), "Above line stays", fontsize=12, **kw)
    page.insert_text((72, 114), "Target line here", fontsize=12, color=(0.8, 0, 0), **kw)
    page.insert_text((72, 128), "Below line stays", fontsize=12, **kw)
    return doc


def paragraph_doc(next_block_gap=None):
    """A wrapped paragraph in a 220pt-wide box; optionally a block below it."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    rect = fitz.Rect(72, 72, 292, 200)
    tw = fitz.TextWriter(page.rect)
    tw.fill_textbox(rect, PARA, font=fitz.Font("tiro"), fontsize=11)
    tw.write_text(page, color=(0, 0, 0.6))
    if next_block_gap is not None:
        tb = page.get_text("dict")["blocks"][0]["bbox"]
        page.insert_text((72, tb[3] + next_block_gap + 10), "NEXT BLOCK BELOW", fontname="helv", fontsize=10)
    return doc


# ─── extraction ─────────────────────────────────────────────────────────────


async def test_extract_reports_geometry_font_and_style(c, upload_dir):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Bold heading", fontname="hebo", fontsize=18, color=(0, 0, 1))
    page.insert_text((72, 200), "Italic body", fontname="tiit", fontsize=10)
    page.insert_text((72, 300), "Mono code", fontname="cour", fontsize=9)
    doc_id = store(upload_dir, doc)

    data = await blocks(c, doc_id)
    assert data["width"] == 595 and data["height"] == 842 and data["rotation"] == 0
    h = block_with(data, "Bold heading")
    assert h["style"]["bold"] is True and h["style"]["italic"] is False
    assert h["style"]["size"] == 18 and h["style"]["color"] == "#0000ff"
    assert h["style"]["family"] == "sans" and h["editable"] is True
    x0, y0, x1, y1 = h["bbox"]
    assert x0 == pytest.approx(72, abs=0.5) and y1 > 95 and y0 < 90
    i = block_with(data, "Italic body")
    assert i["style"]["italic"] is True and i["style"]["family"] == "serif"
    m = block_with(data, "Mono code")
    assert m["style"]["family"] == "mono"
    # hierarchical ids
    assert h["id"] == "b0p0"
    assert h["lines"][0]["id"].startswith("b0.l")
    assert h["lines"][0]["spans"][0]["id"].startswith(h["lines"][0]["id"] + ".s")


async def test_extract_joins_soft_wrapped_paragraph(c, upload_dir):
    doc_id = store(upload_dir, paragraph_doc())
    data = await blocks(c, doc_id)
    b = block_with(data, "quick brown")
    assert len(b["lines"]) >= 3
    assert "\n" in b["text"]
    assert "\n" not in b["paragraph_text"]
    assert b["paragraph_text"].split() == PARA.split()


# ─── line / span edits ──────────────────────────────────────────────────────


async def test_edit_line_replaces_only_that_line_and_keeps_style_and_background(c, upload_dir):
    doc_id = store(upload_dir, three_lines_doc())
    data = await blocks(c, doc_id)
    ln = line_with(data, "Target line")
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "line", "id": ln["id"], "bbox": ln["bbox"]},
        "text": "Edited line text"})
    assert r.status_code == 200, r.text

    page = reopen(upload_dir, doc_id)[0]
    text = page.get_text()
    assert "Target line here" not in text
    assert "Edited line text" in text
    # neighbours (whose boxes overlap the target vertically) survive intact
    assert "Above line stays" in text and "Below line stays" in text
    s = find_span(page, "Edited line text")
    assert s["size"] == pytest.approx(12, abs=0.05)
    assert s["color"] == 0xCC0000
    assert s["origin"][0] == pytest.approx(72, abs=0.5)
    assert s["origin"][1] == pytest.approx(114, abs=0.5)
    # no white box painted: the yellow background under the line is intact
    pix = page.get_pixmap(clip=fitz.Rect(160, 105, 300, 118))
    assert pix.pixel(pix.width - 2, 2) == (255, 255, 0)


async def test_edit_span_shifts_following_spans_on_the_line(c, upload_dir):
    doc = fitz.open()
    page = doc.new_page()
    tw = fitz.TextWriter(page.rect)
    _, end = tw.append((72, 100), "Price: ", font=fitz.Font("helv"), fontsize=12)
    tw.write_text(page)
    tw2 = fitz.TextWriter(page.rect)
    _, end2 = tw2.append(end, "$10", font=fitz.Font("hebo"), fontsize=12)
    tw2.write_text(page, color=(1, 0, 0))
    tw3 = fitz.TextWriter(page.rect)
    tw3.append(end2, " per unit", font=fitz.Font("helv"), fontsize=12)
    tw3.write_text(page)
    doc_id = store(upload_dir, doc)

    data = await blocks(c, doc_id)
    sp = span_with(data, "$10")
    assert sp["bold"] is True
    old_unit = span_with(data, "per unit")
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "span", "id": sp["id"], "bbox": sp["bbox"]},
        "text": "$1,250.00"})
    assert r.status_code == 200, r.text
    page = reopen(upload_dir, doc_id)[0]
    txt = " ".join(page.get_text().split())
    assert "Price: $1,250.00 per unit" in txt
    new = find_span(page, "1,250.00")
    assert new["color"] == 0xFF0000 and "Bold" in new["font"]
    unit = find_span(page, "per unit")
    widened = fitz.get_text_length("$1,250.00", "hebo", 12) - fitz.get_text_length("$10", "hebo", 12)
    assert unit["bbox"][0] == pytest.approx(old_unit["bbox"][0] + widened, abs=1.0)
    assert unit["bbox"][0] >= new["bbox"][2] - 0.5  # no overlap


async def test_restyle_line_bold_italic_size_color(c, upload_dir):
    doc_id = store(upload_dir, three_lines_doc())
    data = await blocks(c, doc_id)
    ln = line_with(data, "Target line")
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "line", "id": ln["id"], "bbox": ln["bbox"]},
        "style": {"bold": True, "italic": True, "size": 13, "color": "#00ff00", "family": "serif"}})
    assert r.status_code == 200, r.text
    assert r.json()["font"] == "base14:tibi"
    page = reopen(upload_dir, doc_id)[0]
    s = find_span(page, "Target line here")
    assert s is not None
    assert s["size"] == pytest.approx(13, abs=0.05)
    assert s["color"] == 0x00FF00
    assert s["flags"] & 16 and s["flags"] & 2  # bold + italic
    assert "Times" in s["font"] or "NimbusRoman" in s["font"]


# ─── fonts ──────────────────────────────────────────────────────────────────


async def test_reuses_embedded_font_when_it_covers_the_glyphs(c, upload_dir):
    doc_id = store(upload_dir, three_lines_doc(embed=True))
    data = await blocks(c, doc_id)
    ln = line_with(data, "Target line")
    assert "Nimbus" in ln["style"]["font"]
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "line", "id": ln["id"]}, "text": "Embedded reuse"})
    assert r.status_code == 200, r.text
    assert r.json()["font"].startswith("embedded:Nimbus Sans")
    page = reopen(upload_dir, doc_id)[0]
    s = find_span(page, "Embedded reuse")
    assert s["font"].startswith("NimbusSans")  # not a Base-14 'Helvetica' substitute


async def test_falls_back_to_base14_when_embedded_font_lacks_glyphs(upload_dir):
    doc = three_lines_doc(embed=True)
    doc = fitz.open("pdf", doc.tobytes())
    page = doc[0]
    res = te.FontResolver(doc, page)
    span_font = [s for s in spans_of(page) if "Target" in s["text"]][0]
    ok = res.resolve(span_font["font"], span_font["flags"], "Plain ASCII")
    assert ok.source.startswith("embedded:")
    cjk = res.resolve(span_font["font"], span_font["flags"], "Plain 中文")
    assert cjk.source == "base14:helv"


async def test_bold_toggle_prefers_sibling_embedded_weight(c, upload_dir):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="R", fontbuffer=fitz.Font("helv").buffer)
    page.insert_font(fontname="B", fontbuffer=fitz.Font("hebo").buffer)
    page.insert_text((72, 100), "Regular words", fontname="R", fontsize=12)
    page.insert_text((72, 200), "Bold words", fontname="B", fontsize=12)
    doc_id = store(upload_dir, doc)
    data = await blocks(c, doc_id)
    ln = line_with(data, "Regular words")
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "line", "id": ln["id"]}, "style": {"bold": True}})
    assert r.status_code == 200, r.text
    assert r.json()["font"] == "embedded:Nimbus Sans Bold"
    s = find_span(reopen(upload_dir, doc_id)[0], "Regular words")
    assert s["font"] == "NimbusSans-Bold"


# ─── paragraph reflow ───────────────────────────────────────────────────────


async def test_paragraph_edit_reflows_within_original_width(c, upload_dir):
    doc_id = store(upload_dir, paragraph_doc())
    data = await blocks(c, doc_id)
    b = block_with(data, "quick brown")
    x0, y0, x1, y1 = b["bbox"]
    first_baseline = find_span(reopen(upload_dir, doc_id)[0], "quick")["origin"][1]
    new = PARA + " Then everyone went home happy and rested until the next bright day began."
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "block", "id": b["id"], "bbox": b["bbox"]}, "text": new})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["font_size"] == pytest.approx(11)  # free space below: no shrink
    assert body["lines"] > len(b["lines"]) and body["overflow"] is False

    page = reopen(upload_dir, doc_id)[0]
    assert " ".join(page.get_text().split()) == " ".join(new.split())
    ss = spans_of(page)
    assert all(s["bbox"][2] <= x1 + 0.6 for s in ss), "text escaped the block width"
    assert min(s["bbox"][0] for s in ss) == pytest.approx(x0, abs=0.6)
    assert min(s["origin"][1] for s in ss) == pytest.approx(first_baseline, abs=0.3)
    assert all(s["size"] == pytest.approx(11, abs=0.05) and s["color"] == 0x000099 for s in ss)
    assert all("Times" in s["font"] or "Nimbus" in s["font"] for s in ss)


async def test_paragraph_overflow_shrinks_font_slightly_and_avoids_next_block(c, upload_dir):
    doc_id = store(upload_dir, paragraph_doc(next_block_gap=2))
    data = await blocks(c, doc_id)
    b = block_with(data, "quick brown")
    nxt_before = block_with(data, "NEXT BLOCK")["bbox"]
    new = PARA + " And one more short clause."
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "block", "id": b["id"]}, "text": new})
    assert r.status_code == 200, r.text
    body = r.json()
    assert 11 * 0.85 - 0.01 <= body["font_size"] < 11
    page = reopen(upload_dir, doc_id)[0]
    assert "NEXT BLOCK BELOW" in page.get_text()
    edited = [s for s in spans_of(page) if "NEXT" not in s["text"]]
    if not body["overflow"]:
        assert max(s["bbox"][3] for s in edited) <= nxt_before[1] + 1.0
    assert " ".join(" ".join(s["text"] for s in edited).split()) == " ".join(new.split())


async def test_block_restyle_keeps_text_and_changes_size_color(c, upload_dir):
    doc_id = store(upload_dir, paragraph_doc())
    data = await blocks(c, doc_id)
    b = block_with(data, "quick brown")
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "block", "id": b["id"]},
        "style": {"size": 9, "color": [255, 0, 0]}})
    assert r.status_code == 200, r.text
    page = reopen(upload_dir, doc_id)[0]
    assert " ".join(page.get_text().split()) == " ".join(PARA.split())
    assert all(s["size"] == pytest.approx(9, abs=0.05) and s["color"] == 0xFF0000 for s in spans_of(page))


async def test_center_alignment_is_preserved(c, upload_dir):
    doc = fitz.open()
    page = doc.new_page()
    tw = fitz.TextWriter(page.rect)
    tw.fill_textbox(fitz.Rect(100, 100, 400, 300), "A centered title line\nsecond short\nthird one here",
                    font=fitz.Font("helv"), fontsize=12, align=fitz.TEXT_ALIGN_CENTER)
    tw.write_text(page)
    doc_id = store(upload_dir, doc)
    data = await blocks(c, doc_id)
    b = block_with(data, "centered")
    assert b["align"] == "center"
    cx = (b["bbox"][0] + b["bbox"][2]) / 2
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "block", "id": b["id"]}, "text": "New\nlines are centered"})
    assert r.status_code == 200, r.text
    for s in spans_of(reopen(upload_dir, doc_id)[0]):
        assert (s["bbox"][0] + s["bbox"][2]) / 2 == pytest.approx(cx, abs=1.0)


def test_wrap_items_marks_paragraph_ends_and_splits_long_words():
    f = fitz.Font("helv")
    mk = lambda w: te._Item(w, f, 10, (0, 0, 0))
    width = fitz.get_text_length("aaa bbb", "helv", 10) + 0.1
    out = te._wrap_items([mk("aaa"), mk("bbb"), mk("ccc"), None, mk("ddd")], width, 1.0)
    assert [([i.text for i in l], end) for l, end in out] == [
        (["aaa", "bbb"], False), (["ccc"], True), (["ddd"], True)]
    long = te._wrap_items([mk("W" * 40)], 50, 1.0)
    assert all(sum(i.width() for i in l) <= 50.01 for l, _ in long)
    assert "".join(i.text for l, _ in long for i in l) == "W" * 40


# ─── move / delete ──────────────────────────────────────────────────────────


async def test_move_block_keeps_glyph_positions_relative(c, upload_dir):
    doc_id = store(upload_dir, paragraph_doc())
    before = sorted((s["text"], s["origin"]) for s in spans_of(reopen(upload_dir, doc_id)[0]))
    data = await blocks(c, doc_id)
    b = block_with(data, "quick brown")
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/move", json={
        "page": 0, "target": {"kind": "block", "id": b["id"], "bbox": b["bbox"]}, "dx": 50, "dy": 300})
    assert r.status_code == 200, r.text
    page = reopen(upload_dir, doc_id)[0]
    assert page.get_text("text", clip=fitz.Rect(b["bbox"])).strip() == ""
    words_before = fitz.open("pdf", paragraph_doc().tobytes())[0].get_text("words")
    words_after = page.get_text("words")
    assert [w[4] for w in words_after] == [w[4] for w in words_before]
    for wb, wa in zip(words_before, words_after):
        assert wa[0] == pytest.approx(wb[0] + 50, abs=0.3)
        assert wa[3] == pytest.approx(wb[3] + 300, abs=0.3)
    assert all(s["color"] == 0x000099 for s in spans_of(page))


async def test_delete_block_keeps_image_and_other_text(c, upload_dir):
    doc = fitz.open()
    page = doc.new_page()
    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
    pm.set_rect(pm.irect, (0, 128, 255))
    page.insert_image(fitz.Rect(70, 80, 300, 120), pixmap=pm)
    page.insert_text((72, 100), "Delete me please", fontname="helv", fontsize=12)
    page.insert_text((72, 300), "Keep me", fontname="helv", fontsize=12)
    doc_id = store(upload_dir, doc)
    data = await blocks(c, doc_id)
    b = block_with(data, "Delete me")
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/delete", json={
        "page": 0, "target": {"kind": "block", "id": b["id"], "bbox": b["bbox"]}})
    assert r.status_code == 200, r.text
    page = reopen(upload_dir, doc_id)[0]
    assert "Delete me" not in page.get_text()
    assert "Keep me" in page.get_text()
    assert len(page.get_images()) == 1
    pix = page.get_pixmap(clip=fitz.Rect(150, 95, 151, 96))
    assert pix.pixel(0, 0) == (0, 128, 255)


# ─── safety: stale targets, foreign redactions, undo, errors ────────────────


async def test_stale_bbox_is_rejected_with_409(c, upload_dir):
    doc_id = store(upload_dir, three_lines_doc())
    data = await blocks(c, doc_id)
    ln = line_with(data, "Target line")
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "line", "id": ln["id"], "bbox": [400, 600, 500, 620]},
        "text": "x"})
    assert r.status_code == 409
    assert "Target line here" in reopen(upload_dir, doc_id)[0].get_text()


async def test_bbox_alone_finds_target_when_ids_shift(c, upload_dir):
    doc_id = store(upload_dir, three_lines_doc())
    data = await blocks(c, doc_id)
    ln = line_with(data, "Below line")
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "line", "id": "b999.l0", "bbox": ln["bbox"]}, "text": "Found by box"})
    assert r.status_code == 200, r.text
    t = reopen(upload_dir, doc_id)[0].get_text()
    assert "Found by box" in t and "Below line stays" not in t and "Target line here" in t


async def test_pending_redaction_marks_from_other_tools_are_not_burned_in(c, upload_dir):
    doc = three_lines_doc()
    page = doc[0]
    page.insert_text((72, 400), "SECRET pending", fontname="helv", fontsize=12)
    page.add_redact_annot(fitz.Rect(70, 388, 200, 404), text="X", fill=(0, 0, 0))
    doc_id = store(upload_dir, doc)
    data = await blocks(c, doc_id)
    ln = line_with(data, "Target line")
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "line", "id": ln["id"]}, "text": "Changed"})
    assert r.status_code == 200, r.text
    page = reopen(upload_dir, doc_id)[0]
    assert "SECRET pending" in page.get_text()  # not burned in
    annots = list(page.annots(types=[fitz.PDF_ANNOT_REDACT]))
    assert len(annots) == 1
    assert annots[0].rect == fitz.Rect(70, 388, 200, 404)
    assert annots[0].colors["fill"] == pytest.approx([0, 0, 0])


async def test_undo_restores_original_text(c, upload_dir):
    doc_id = store(upload_dir, three_lines_doc())
    data = await blocks(c, doc_id)
    ln = line_with(data, "Target line")
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "line", "id": ln["id"]}, "text": "Changed"})
    assert r.status_code == 200
    assert "Changed" in reopen(upload_dir, doc_id)[0].get_text()
    u = await c.post(f"/api/pdf/{doc_id}/undo")
    assert u.status_code == 200, u.text
    t = reopen(upload_dir, doc_id)[0].get_text()
    assert "Target line here" in t and "Changed" not in t


async def test_error_cases(c, upload_dir):
    doc_id = store(upload_dir, three_lines_doc())
    assert (await c.get("/api/pdf/not-a-uuid/text-edit/page/0")).status_code == 400
    assert (await c.get(f"/api/pdf/{uuid.uuid4()}/text-edit/page/0")).status_code == 404
    assert (await c.get(f"/api/pdf/{doc_id}/text-edit/page/7")).status_code == 400
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "line", "id": "b0.l0"}})
    assert r.status_code == 400  # nothing to change
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "line", "id": "b42.l0"}, "text": "x"})
    assert r.status_code == 404
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/move", json={
        "page": 0, "target": {"kind": "span", "id": "b0.l0.s0"}, "dx": 1})
    assert r.status_code == 400


# ─── rotation ───────────────────────────────────────────────────────────────


async def test_rotated_page_bbox_is_visible_space_and_edit_works(c, upload_dir):
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 100), "Rotated page text", fontname="helv", fontsize=12)
    page.set_rotation(90)
    doc_id = store(upload_dir, doc)
    data = await blocks(c, doc_id)
    assert data["rotation"] == 90 and data["width"] == 792 and data["height"] == 612
    ln = line_with(data, "Rotated page")
    # visible-space box lies inside the visible page
    x0, y0, x1, y1 = ln["bbox"]
    assert 0 <= x0 < x1 <= 792 and 0 <= y0 < y1 <= 612
    assert (y1 - y0) > (x1 - x0)  # rotated 90: tall and narrow on screen
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "line", "id": ln["id"], "bbox": ln["bbox"]}, "text": "Still works"})
    assert r.status_code == 200, r.text
    page = reopen(upload_dir, doc_id)[0]
    assert "Still works" in page.get_text() and "Rotated page text" not in page.get_text()
    s = find_span(page, "Still works")
    assert s["origin"] == pytest.approx((72, 100), abs=0.5)


@pytest.mark.parametrize("rot", [90, 180, 270])
async def test_vertical_text_is_edited_along_its_direction(c, upload_dir, rot):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((300, 400), "Vertical label", fontname="helv", fontsize=12, rotate=rot)
    page.insert_text((72, 72), "Horizontal", fontname="helv", fontsize=12)
    doc_id = store(upload_dir, doc)
    data = await blocks(c, doc_id)
    b = block_with(data, "Vertical label")
    assert b["editable"] is True and b["angle"] == (360 - rot) % 360
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "block", "id": b["id"]}, "text": "New vertical"})
    assert r.status_code == 200, r.text
    page = reopen(upload_dir, doc_id)[0]
    d = page.get_text("dict")
    lines = [l for bl in d["blocks"] for l in bl.get("lines", [])
             if "New vertical" in "".join(s["text"] for s in l["spans"])]
    assert lines, page.get_text()
    old_dir = [l for bl in fitz.open("pdf", doc.tobytes())[0].get_text("dict")["blocks"]
               for l in bl.get("lines", []) if "Vertical" in l["spans"][0]["text"]][0]["dir"]
    assert lines[0]["dir"] == pytest.approx(old_dir, abs=1e-3)
    assert lines[0]["spans"][0]["origin"] == pytest.approx((300, 400), abs=0.6)
    assert "Horizontal" in page.get_text() and "Vertical label" not in page.get_text()


# ─── inline styles / paragraph segmentation ─────────────────────────────────


def _mixed_paragraph_doc():
    """One wrapped paragraph whose words 'IMPORTANT' are bold red."""
    doc = fitz.open()
    page = doc.new_page()
    reg, bold = fitz.Font("helv"), fitz.Font("hebo")
    words = ("This is a long paragraph with one IMPORTANT word in it that keeps "
             "going for a while so that it wraps across several lines nicely").split()
    x, y, x_max = 72.0, 100.0, 260.0
    for w in words:
        f = bold if w == "IMPORTANT" else reg
        wl = f.text_length(w, fontsize=11)
        if x + wl > x_max:
            x, y = 72.0, y + 13.2
        tw = fitz.TextWriter(page.rect)
        tw.append((x, y), w + " ", font=f, fontsize=11)
        tw.write_text(page, color=(1, 0, 0) if w == "IMPORTANT" else (0, 0, 0))
        x += f.text_length(w + " ", fontsize=11)
    return doc


async def test_paragraph_edit_preserves_inline_bold_and_colour(c, upload_dir):
    doc_id = store(upload_dir, _mixed_paragraph_doc())
    data = await blocks(c, doc_id)
    b = block_with(data, "IMPORTANT")
    assert b["mixed_styles"] is True
    assert len(b["lines"]) >= 3
    new = b["paragraph_text"].replace("long paragraph", "much longer edited paragraph")
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "block", "id": b["id"], "bbox": b["bbox"]}, "text": new})
    assert r.status_code == 200, r.text
    page = reopen(upload_dir, doc_id)[0]
    assert " ".join(page.get_text().split()) == " ".join(new.split())
    imp = find_span(page, "IMPORTANT")
    assert imp["color"] == 0xFF0000 and "Bold" in imp["font"]
    edited = find_span(page, "edited")
    assert edited["color"] == 0 and "Bold" not in edited["font"]
    assert all(s["bbox"][2] <= b["bbox"][2] + 0.6 for s in spans_of(page))


async def test_raw_block_is_split_into_paragraphs(c, upload_dir):
    doc = fitz.open()
    page = doc.new_page()
    tw = fitz.TextWriter(page.rect)
    tw.fill_textbox(fitz.Rect(72, 72, 400, 400),
                    "First paragraph line one\nfirst paragraph line two\n\nSecond paragraph here",
                    font=fitz.Font("helv"), fontsize=11)
    tw.write_text(page)
    doc_id = store(upload_dir, doc)
    raw_blocks = [b for b in fitz.open("pdf", doc.tobytes())[0].get_text("dict")["blocks"] if b["type"] == 0]
    data = await blocks(c, doc_id)
    texts = [b["text"] for b in data["blocks"]]
    assert len(data["blocks"]) == 2, (len(raw_blocks), texts)
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={
        "page": 0, "target": {"kind": "block", "id": data["blocks"][1]["id"]}, "text": "Replaced second"})
    assert r.status_code == 200, r.text
    t = reopen(upload_dir, doc_id)[0].get_text()
    assert "First paragraph line one" in t and "first paragraph line two" in t
    assert "Replaced second" in t and "Second paragraph here" not in t


async def test_move_vertical_line(c, upload_dir):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((300, 400), "Side label", fontname="helv", fontsize=12, rotate=90)
    doc_id = store(upload_dir, doc)
    data = await blocks(c, doc_id)
    ln = line_with(data, "Side label")
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/move", json={
        "page": 0, "target": {"kind": "line", "id": ln["id"]}, "dx": -100, "dy": 20})
    assert r.status_code == 200, r.text
    s = find_span(reopen(upload_dir, doc_id)[0], "Side label")
    assert s["origin"] == pytest.approx((200, 420), abs=0.6)


async def test_single_line_block_grows_right_instead_of_shrinking(c, upload_dir):
    """A one-line sentence made longer keeps its size and stays on one line
    (Acrobat point-text behaviour) as long as the page has room to the right,
    and it never runs into a neighbour on the same row."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Revenue was 4200 dollars.", fontname="helv", fontsize=12)
    page.insert_text((72, 150), "Next paragraph further below.", fontname="helv", fontsize=12)
    page.insert_text((72, 200), "Cell A", fontname="helv", fontsize=10)
    page.insert_text((150, 200), "Cell B", fontname="helv", fontsize=10)
    doc_id = store(upload_dir, doc)

    data = await blocks(c, doc_id)
    b = block_with(data, "Revenue")
    new = "Revenue was 5300 dollars this quarter, up 26 percent on last year."
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit",
                     json={"page": 0, "target": {"kind": "block", "id": b["id"], "bbox": b["bbox"]}, "text": new})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["lines"] == 1 and res["scale"] == 1.0 and res["overflow"] is False
    pg = reopen(upload_dir, doc_id)[0]
    hit = [s for s in spans_of(pg) if "5300" in s["text"]]
    assert hit and hit[0]["size"] == pytest.approx(12, abs=0.01)
    assert hit[0]["bbox"][2] < pg.rect.width - 72 + 0.5

    # a row neighbour still caps the width: Cell A must not run into Cell B
    data = await blocks(c, doc_id)
    a = block_with(data, "Cell A")
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/edit",
                     json={"page": 0, "target": {"kind": "block", "id": a["id"], "bbox": a["bbox"]},
                           "text": "Cell A has much longer content now"})
    assert r.status_code == 200, r.text
    pg = reopen(upload_dir, doc_id)[0]
    a_spans = [s for s in spans_of(pg) if s["text"].startswith("Cell A") or "longer" in s["text"] or "content" in s["text"]]
    assert all(s["bbox"][2] <= 150 for s in a_spans), [(s["text"], s["bbox"]) for s in a_spans]
