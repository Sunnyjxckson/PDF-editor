"""textedit2: overflow push / 422, arbitrary-angle text, Tc/Tw/Tz spacing,
per-glyph font fallback. Every test re-opens the saved PDF with fitz."""

import math
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

LONG = ("The quick brown fox jumps over the lazy dog while the cat watches from the window "
        "and the bird sings a song about the morning sun. ")


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


def pdf_bytes(upload_dir, doc_id) -> bytes:
    return (upload_dir / doc_id / "original.pdf").read_bytes()


def reopen(upload_dir, doc_id) -> fitz.Document:
    return fitz.open(str(upload_dir / doc_id / "original.pdf"))


def spans_of(page):
    return [s for b in page.get_text("dict")["blocks"] for l in b.get("lines", [])
            for s in l["spans"] if s["text"].strip()]


def lines_of(page):
    return [l for b in page.get_text("dict")["blocks"] for l in b.get("lines", [])
            if "".join(s["text"] for s in l["spans"]).strip()]


def line_text(l):
    return "".join(s["text"] for s in l["spans"])


async def page_json(c, doc_id, page=0):
    r = await c.get(f"/api/pdf/{doc_id}/text-edit/page/{page}")
    assert r.status_code == 200, r.text
    return r.json()


def block_with(data, needle):
    for b in data["blocks"]:
        if needle in b["text"]:
            return b
    raise AssertionError(f"no block with {needle!r}: {[b['text'] for b in data['blocks']]}")


def line_with(data, needle):
    for b in data["blocks"]:
        for l in b["lines"]:
            if needle in l["text"]:
                return l
    raise AssertionError(needle)


async def edit(c, doc_id, target, **kw):
    return await c.post(f"/api/pdf/{doc_id}/text-edit/edit", json={"page": 0, "target": target, **kw})


def para_doc(para_top: float, follower_y: float, page_h: float = 792):
    """A wrapped Times paragraph (220pt wide) and a one-line block under it."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=page_h)
    tw = fitz.TextWriter(page.rect)
    tw.fill_textbox(fitz.Rect(72, para_top, 292, para_top + 120), LONG, font=fitz.Font("tiro"), fontsize=11)
    tw.write_text(page)
    page.insert_text((72, follower_y), "Follower block stays readable", fontname="helv", fontsize=10)
    return doc


def follower_spans(page):
    return [s for s in spans_of(page) if "Follower" in s["text"] or "readable" in s["text"]]


# ─── 1. overflow: push, 422, shrink, allow ─────────────────────────────────


async def test_overflow_pushes_following_block_down_glyph_exact(c, upload_dir):
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    tw = fitz.TextWriter(page.rect)
    tw.fill_textbox(fitz.Rect(72, 72, 292, 192), LONG, font=fitz.Font("tiro"), fontsize=11)
    tw.write_text(page)
    para_bottom = page.get_text("dict")["blocks"][0]["bbox"][3]
    page.insert_text((72, para_bottom + 16), "Follower block stays readable", fontname="helv", fontsize=10)
    doc_id = store(upload_dir, doc)
    before = follower_spans(reopen(upload_dir, doc_id)[0])
    b = block_with(await page_json(c, doc_id), "quick brown")
    new = LONG * 3
    r = await edit(c, doc_id, {"kind": "block", "id": b["id"], "bbox": b["bbox"]}, text=new)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pushed"] == 1 and body["push_distance"] > 20 and body["overlap"] is False

    pg = reopen(upload_dir, doc_id)[0]
    after = follower_spans(pg)
    assert "".join(s["text"] for s in after) == "".join(s["text"] for s in before)
    # glyph-exact: same x, moved straight down by push_distance
    assert after[0]["origin"][0] == pytest.approx(before[0]["origin"][0], abs=0.05)
    assert after[0]["origin"][1] - before[0]["origin"][1] == pytest.approx(body["push_distance"], abs=0.3)
    assert after[0]["size"] == pytest.approx(10, abs=0.01)
    edited = [s for s in spans_of(pg) if s not in after]
    assert " ".join(" ".join(s["text"] for s in edited).split()) == " ".join(new.split())
    # nothing overlaps: the whole edited paragraph ends above the follower
    assert max(s["bbox"][3] for s in edited) <= min(s["bbox"][1] for s in after) + 0.5
    assert pg.get_text().count("Follower") == 1


async def test_overflow_without_room_returns_422_and_writes_nothing(c, upload_dir):
    # paragraph near the bottom; follower at the very bottom: no room to push
    doc_id = store(upload_dir, para_doc(560, 760))
    data = await page_json(c, doc_id)
    b = block_with(data, "quick brown")
    before = pdf_bytes(upload_dir, doc_id)
    r = await edit(c, doc_id, {"kind": "block", "id": b["id"], "bbox": b["bbox"]}, text=LONG * 8)
    assert r.status_code == 422, r.text
    d = r.json()["detail"]
    assert d["code"] == "overflow"
    assert d["needed_height"] > d["available_height"] > 0
    assert d["requested_size"] == pytest.approx(11)
    assert d["fit_size"] is not None and d["fit_size"] < 11 * 0.85
    assert pdf_bytes(upload_dir, doc_id) == before  # nothing written
    assert (await c.post(f"/api/pdf/{doc_id}/undo")).status_code != 200  # and no snapshot taken


async def test_overflow_shrink_mode_writes_at_the_offered_fit_size(c, upload_dir):
    doc_id = store(upload_dir, para_doc(560, 760))
    b = block_with(await page_json(c, doc_id), "quick brown")
    tgt = {"kind": "block", "id": b["id"], "bbox": b["bbox"]}
    r = await edit(c, doc_id, tgt, text=LONG * 8)
    fit = r.json()["detail"]["fit_size"]
    fol_before = follower_spans(reopen(upload_dir, doc_id)[0])
    r = await edit(c, doc_id, tgt, text=LONG * 8, overflow="shrink")
    assert r.status_code == 200, r.text
    assert r.json()["font_size"] == pytest.approx(fit, abs=0.01)
    pg = reopen(upload_dir, doc_id)[0]
    fol = follower_spans(pg)
    assert fol[0]["origin"] == pytest.approx(fol_before[0]["origin"], abs=0.05)  # not moved
    edited = [s for s in spans_of(pg) if s not in fol]
    assert all(s["size"] == pytest.approx(fit, abs=0.05) for s in edited)
    assert max(s["bbox"][3] for s in edited) <= fol[0]["bbox"][1] + 0.5  # no overlap
    assert " ".join(" ".join(s["text"] for s in edited).split()) == " ".join((LONG * 8).split())


async def test_overflow_allow_mode_overlaps_at_requested_size(c, upload_dir):
    doc_id = store(upload_dir, para_doc(560, 760))
    b = block_with(await page_json(c, doc_id), "quick brown")
    r = await edit(c, doc_id, {"kind": "block", "id": b["id"], "bbox": b["bbox"]},
                   text=LONG * 8, overflow="allow")
    assert r.status_code == 200, r.text
    assert r.json()["overlap"] is True and r.json()["font_size"] == pytest.approx(11)
    pg = reopen(upload_dir, doc_id)[0]
    fol = follower_spans(pg)
    edited = [s for s in spans_of(pg) if s not in fol]
    assert all(s["size"] == pytest.approx(11, abs=0.05) for s in edited)
    assert max(s["bbox"][3] for s in edited) > fol[0]["bbox"][1]  # it does overlap, as asked


# ─── 2. arbitrary-angle text ────────────────────────────────────────────────


def slanted_doc(angle=30.0):
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    tw = fitz.TextWriter(page.rect)
    piv = fitz.Point(200, 300)
    f = fitz.Font("helv")
    tw.append(piv, "Slanted stamp text", font=f, fontsize=14)
    tw.append(piv + (0, 17), "second slanted line", font=f, fontsize=14)
    tw.write_text(page, morph=(piv, fitz.Matrix(-angle)), color=(0.7, 0, 0))
    # inside the slanted lines' axis-aligned bbox, but not touching the glyphs
    page.insert_text((200, 380), "Keep me", fontname="helv", fontsize=10)
    page.insert_text((300, 300), "Keep too", fontname="helv", fontsize=10)
    return doc


def _dir_deg(l):
    return math.degrees(math.atan2(l["dir"][1], l["dir"][0]))


async def test_slanted_block_is_editable_and_rewritten_at_its_angle(c, upload_dir):
    doc = slanted_doc(30)
    orig = [l for l in lines_of(doc[0]) if "Slanted" in line_text(l)][0]
    # precondition: the neighbours sit inside the slanted line's axis bbox
    assert fitz.Rect(orig["bbox"]).intersects(fitz.Rect(300, 290, 340, 302))
    doc_id = store(upload_dir, doc)
    data = await page_json(c, doc_id)
    b = block_with(data, "Slanted")
    assert b["editable"] is True and b["angle"] == pytest.approx(30, abs=0.1)
    assert b["box"]["w"] == pytest.approx(fitz.get_text_length("second slanted line", "helv", 14), abs=1.5)
    assert b["box"]["angle"] == pytest.approx(30, abs=0.1)
    r = await edit(c, doc_id, {"kind": "block", "id": b["id"], "bbox": b["bbox"]},
                   text="Edited slanted\nstamp lines")
    assert r.status_code == 200, r.text
    pg = reopen(upload_dir, doc_id)[0]
    t = pg.get_text()
    assert "Slanted stamp" not in t and "second slanted" not in t
    assert "Keep me" in t and "Keep too" in t  # neighbours under the bbox survive
    new = [l for l in lines_of(pg) if "Edited" in line_text(l)]
    assert new and _dir_deg(new[0]) == pytest.approx(_dir_deg(orig), abs=0.2)
    assert new[0]["spans"][0]["origin"] == pytest.approx(orig["spans"][0]["origin"], abs=0.6)
    assert new[0]["spans"][0]["color"] == orig["spans"][0]["color"]
    second = [l for l in lines_of(pg) if "stamp lines" in line_text(l)][0]
    assert _dir_deg(second) == pytest.approx(30, abs=0.2)


async def test_slanted_line_delete_and_move(c, upload_dir):
    doc_id = store(upload_dir, slanted_doc(-40))
    data = await page_json(c, doc_id)
    ln = line_with(data, "second slanted")
    assert ln["angle"] == pytest.approx(320, abs=0.1)
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/delete", json={"page": 0, "target": {"kind": "line", "id": ln["id"]}})
    assert r.status_code == 200, r.text
    t = reopen(upload_dir, doc_id)[0].get_text()
    assert "second slanted" not in t and "Slanted stamp text" in t and "Keep me" in t and "Keep too" in t
    data = await page_json(c, doc_id)
    ln = line_with(data, "Slanted stamp")
    r = await c.post(f"/api/pdf/{doc_id}/text-edit/move", json={"page": 0, "target": {"kind": "line", "id": ln["id"]},
                                                                  "dx": 50, "dy": 10})
    assert r.status_code == 200, r.text
    s = [s for s in spans_of(reopen(upload_dir, doc_id)[0]) if "Slanted" in s["text"]][0]
    assert s["origin"] == pytest.approx((250, 310), abs=0.6)


# ─── 3. letter spacing / word spacing / horizontal scale ────────────────────


def _append_stream(page, content: str):
    doc = page.parent
    x = doc.get_new_xref()
    doc.update_object(x, "<<>>")
    doc.update_stream(x, content.encode())
    cs = page.get_contents()
    doc.xref_set_key(page.xref, "Contents", "[" + " ".join(f"{c} 0 R" for c in cs + [x]) + "]")


def spaced_doc(tc=2.0, tz=80, tw=0.0):
    """Target line and a reference line already holding the new text, both
    drawn with the same Tc/Tz/Tw via raw content."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 40), "x", fontname="helv", fontsize=8)  # registers /helv
    for y, text in ((600, "SPACED HEADING TEXT"), (300, "SPACED EDITED WORDS NOW")):
        _append_stream(page, f"q BT /helv 12 Tf {tc} Tc {tz} Tz {tw} Tw 1 0 0 1 72 {y} Tm ({text}) Tj ET Q")
    return doc


def _line_width(page, needle):
    l = [l for l in lines_of(page) if needle in line_text(l)][0]
    return l["bbox"][2] - l["bbox"][0], l


@pytest.mark.parametrize("kind", ["block", "line"])
async def test_letter_spacing_and_horizontal_scale_are_reproduced(c, upload_dir, kind):
    doc_id = store(upload_dir, spaced_doc(tc=2.0, tz=80))
    data = await page_json(c, doc_id)
    ln = line_with(data, "HEADING")
    # Tz is reported as horizontal scale, the size is the real font size
    assert ln["style"]["size"] == pytest.approx(12, abs=0.05)
    assert ln["style"]["hscale"] == pytest.approx(0.8, abs=0.01)
    b = block_with(data, "HEADING")
    tgt = {"kind": kind, "id": (b if kind == "block" else ln)["id"]}
    r = await edit(c, doc_id, tgt, text="SPACED EDITED WORDS NOW")
    assert r.status_code == 200, r.text
    pg = reopen(upload_dir, doc_id)[0]
    lines = [l for l in lines_of(pg) if "EDITED" in line_text(l)]
    assert len(lines) == 2, [line_text(l) for l in lines]
    edited = [l for l in lines if l["bbox"][1] < 250][0]  # y-down: the old y=600 line
    ref = [l for l in lines if l is not edited][0]
    we, wr = edited["bbox"][2] - edited["bbox"][0], ref["bbox"][2] - ref["bbox"][0]
    assert we / wr == pytest.approx(1.0, abs=0.03), (we, wr)
    # glyph shapes are compressed too (Tz), not only spread apart
    assert edited["spans"][0]["size"] == pytest.approx(ref["spans"][0]["size"], abs=0.1)
    assert edited["spans"][0]["origin"][0] == pytest.approx(72, abs=0.3)


async def test_word_spacing_is_reproduced(c, upload_dir):
    doc_id = store(upload_dir, spaced_doc(tc=0, tz=100, tw=6))
    data = await page_json(c, doc_id)
    ln = line_with(data, "HEADING")
    r = await edit(c, doc_id, {"kind": "line", "id": ln["id"]}, text="SPACED EDITED WORDS NOW")
    assert r.status_code == 200, r.text
    pg = reopen(upload_dir, doc_id)[0]
    lines = [l for l in lines_of(pg) if "EDITED" in line_text(l)]
    edited = [l for l in lines if l["bbox"][1] < 250][0]
    ref = [l for l in lines if l is not edited][0]
    we, wr = edited["bbox"][2] - edited["bbox"][0], ref["bbox"][2] - ref["bbox"][0]
    assert we / wr == pytest.approx(1.0, abs=0.03), (we, wr)


async def test_plain_text_gets_no_spurious_spacing(upload_dir):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Plain text with ordinary spacing.", fontname="helv", fontsize=12)
    doc = fitz.open("pdf", doc.tobytes())
    page = doc[0]
    blk = te.extract_blocks(page)[0]
    fr = te._block_frame(blk)
    assert blk.spans[0].hscale == 1.0 and blk.spans[0].size == pytest.approx(12)
    assert te._measure_spacing(blk.spans, fr, te.FontResolver(doc, page)) == (0.0, 0.0)


# ─── 4. per-glyph font fallback ─────────────────────────────────────────────


def _chars_by_basefont(page) -> dict:
    """char -> basefont names that draw it, from the content stream's Tf/TJ
    sequence (extraction reports both fonts as NimbusSans-Regular)."""
    import re
    page.clean_contents()
    doc = page.parent
    stream = b"".join(doc.xref_stream(x) for x in page.get_contents()).decode("latin-1")
    res = {f[4]: f[3] for f in page.get_fonts(full=True)}
    glyph_fonts = []
    for name, hexs in re.findall(r"/(\w+) [\d.]+ Tf[^\[]*\[<([0-9A-Fa-f]+)>\]TJ", stream):
        glyph_fonts += [res[name]] * (len(hexs) // 4)
    chars = [ch["c"] for b in page.get_text("rawdict")["blocks"] for l in b["lines"]
             for sp in l["spans"] for ch in sp["chars"]]
    assert len(chars) == len(glyph_fonts), (chars, glyph_fonts)
    out: dict = {}
    for ch, f in zip(chars, glyph_fonts):
        out.setdefault(ch, set()).add(f)
    return out


def subset_doc():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="EmbH", fontbuffer=fitz.Font("helv").buffer)
    page.insert_text((72, 100), "Hello world", fontname="EmbH", fontsize=12)
    doc.subset_fonts()
    return fitz.open("pdf", doc.tobytes())


async def test_subset_font_keeps_its_glyphs_and_falls_back_per_glyph(c, upload_dir):
    doc = subset_doc()
    sub_name = doc[0].get_fonts(full=True)[0][3]
    assert sub_name.endswith("+Nimbus Sans Regular")  # really a subset
    doc_id = store(upload_dir, doc)
    data = await page_json(c, doc_id)
    ln = line_with(data, "Hello")
    r = await edit(c, doc_id, {"kind": "line", "id": ln["id"]}, text="Hello Zebra world")
    assert r.status_code == 200, r.text
    assert r.json()["font"].startswith("embedded:") and r.json()["fallback"] == "base14:helv"
    pg = reopen(upload_dir, doc_id)[0]
    assert "Hello Zebra world" in " ".join(pg.get_text().split())
    by_char = _chars_by_basefont(pg)
    # glyphs the subset has stay in the embedded Nimbus font ...
    for ch in "Helowrd":
        assert by_char[ch] == {"Nimbus Sans Regular"}, (ch, by_char)
    # ... only Z, b, a (missing from the subset) come from the Base-14 fallback
    for ch in "Zba":
        assert by_char[ch] == {"Helvetica"}, (ch, by_char)
    # the missing glyphs are really drawn (the fallback has ink there)
    words = [w for w in pg.get_text("words") if w[4] == "Zebra"]
    assert words
    pix = pg.get_pixmap(clip=fitz.Rect(words[0][:4]), dpi=144)
    assert not pix.is_unicolor


async def test_resolver_returns_font_stack_only_when_fallback_covers(upload_dir):
    doc = subset_doc()
    page = doc[0]
    res = te.FontResolver(doc, page)
    sp = te.extract_blocks(page)[0].spans[0]
    whole = res.resolve(sp.font, sp.flags, "Hello world")
    assert whole.fallback is None and not isinstance(whole.font, te.FontStack)
    mixed = res.resolve(sp.font, sp.flags, "Hello Zebra")
    assert isinstance(mixed.font, te.FontStack) and mixed.fallback == "base14:helv"
    segs = te._segments(mixed.font, "Hello Zebra")
    assert "".join(t for t, _ in segs) == "Hello Zebra"
    assert segs[0][1] is mixed.font.primary and segs[1] == ("Z", mixed.font.fallback)
