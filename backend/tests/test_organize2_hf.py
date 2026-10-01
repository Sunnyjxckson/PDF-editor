"""Removable header/footer runs (organize2): add -> list -> update -> remove, plus
removal of Acrobat-made pagination artifacts. Every assertion re-opens the PDF."""

from __future__ import annotations

import re
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


def body_pdf(n=3) -> fitz.Document:
    doc = fitz.open()
    for i in range(n):
        p = doc.new_page()
        p.insert_text((72, 120), f"Body text of page {i + 1}", fontsize=12)
        p.insert_text((72, 400), f"Middle paragraph {i + 1} alpha beta", fontsize=11)
    return doc


def store(upload_dir: Path, doc) -> str:
    doc_id = str(uuid.uuid4())
    (upload_dir / doc_id).mkdir()
    (upload_dir / doc_id / "original.pdf").write_bytes(doc.tobytes())
    return doc_id


def reopen(upload_dir, doc_id) -> fitz.Document:
    return fitz.open(str(upload_dir / doc_id / "original.pdf"))


def texts(doc) -> list[str]:
    return [p.get_text() for p in doc]


async def test_add_update_remove_leaves_body_text_identical(c, upload_dir):
    doc_id = store(upload_dir, body_pdf(3))
    orig = texts(reopen(upload_dir, doc_id))

    r = await c.post(f"{P}/{doc_id}/organize/header-footer",
                     json={"header_center": "CONFIDENTIAL DRAFT", "footer_right": "Page {n} of {total}"})
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    r2 = await c.post(f"{P}/{doc_id}/organize/bates", json={"prefix": "ACME", "digits": 4, "position": "bottom-left"})
    bates_rid = r2.json()["run_id"]

    d = reopen(upload_dir, doc_id)
    assert "CONFIDENTIAL DRAFT" in d[1].get_text() and "Page 2 of 3" in d[1].get_text()
    assert "ACME0002" in d[1].get_text()
    # real marked-content structure, like Acrobat's
    raw = b"".join(d.xref_stream(x) for x in d[0].get_contents())
    assert re.search(rb"/Artifact <</Type /Pagination /Subtype /Header /Attached \[/Top\] /PylorRun /" + rid.encode() + rb">> BDC", raw)
    assert re.search(rb"/Subtype /Footer .*?/PylorRun /" + rid.encode(), raw)
    assert raw.count(b"EMC") == 3  # run 1 header + footer, Bates footer
    d.close()

    runs = (await c.get(f"{P}/{doc_id}/organize/header-footer/runs")).json()["runs"]
    by = {x["id"]: x for x in runs}
    assert set(by) == {rid, bates_rid}
    assert by[rid]["pages"] == [0, 1, 2] and by[rid]["subtypes"] == ["Footer", "Header"]
    assert by[rid]["settings"]["header_center"] == "CONFIDENTIAL DRAFT"
    assert by[bates_rid]["kind"] == "bates" and by[bates_rid]["editable"]

    # Update: change text and font size of run 1; Bates untouched
    new = {**by[rid]["settings"], "header_center": "FINAL", "footer_right": "{n}", "font_size": 14}
    r = await c.put(f"{P}/{doc_id}/organize/header-footer/runs/{rid}", json=new)
    assert r.status_code == 200, r.text
    d = reopen(upload_dir, doc_id)
    t = d[2].get_text()
    assert "FINAL" in t and "CONFIDENTIAL" not in t and "Page 3 of 3" not in t and "ACME0003" in t
    spans = [s for b in d[0].get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]]
    assert [round(s["size"]) for s in spans if s["text"] == "FINAL"] == [14]
    d.close()

    # Remove run 1, then Bates: body text exactly as before
    r = await c.delete(f"{P}/{doc_id}/organize/header-footer/runs/{rid}")
    assert r.status_code == 200 and r.json()["removed"] == 6
    d = reopen(upload_dir, doc_id)
    assert "FINAL" not in d[0].get_text() and "ACME0001" in d[0].get_text()
    d.close()
    r = await c.delete(f"{P}/{doc_id}/organize/header-footer/runs/{bates_rid}")
    assert r.status_code == 200
    d = reopen(upload_dir, doc_id)
    assert texts(d) == orig
    assert d.xref_get_key(d.pdf_catalog(), f"PylorHFRuns/{rid}")[0] == "null"
    d.close()
    assert (await c.get(f"{P}/{doc_id}/organize/header-footer/runs")).json()["runs"] == []
    assert (await c.delete(f"{P}/{doc_id}/organize/header-footer/runs/{rid}")).status_code == 404
    assert (await c.delete(f"{P}/{doc_id}/organize/header-footer/runs/..%2Fx")).status_code in (400, 404)

    # undo restores the Bates run
    await c.post(f"{P}/{doc_id}/undo")
    assert "ACME0001" in reopen(upload_dir, doc_id)[0].get_text()


async def test_page_numbers_run_on_rotated_page_removable(c, upload_dir):
    doc = body_pdf(2)
    doc[1].set_rotation(90)
    doc_id = store(upload_dir, doc)
    orig = texts(reopen(upload_dir, doc_id))
    r = await c.post(f"{P}/{doc_id}/organize/page-numbers", json={"format": "- {n} -"})
    rid = r.json()["run_id"]
    assert "- 2 -" in reopen(upload_dir, doc_id)[1].get_text()
    runs = (await c.get(f"{P}/{doc_id}/organize/header-footer/runs")).json()["runs"]
    assert runs[0]["kind"] == "page-numbers"
    await c.delete(f"{P}/{doc_id}/organize/header-footer/runs/{rid}")
    assert texts(reopen(upload_dir, doc_id)) == orig


def acrobat_like_pdf() -> fitz.Document:
    """Pages carrying header/footer exactly as Adobe Acrobat writes them:
    page 0: inline /Artifact /Pagination BDC..EMC + page PieceInfo/ADBE_CompoundType;
    page 1: a Form XObject tagged PieceInfo/ADBE_CompoundType /Private /Footer, invoked
            inside a named-property artifact (/Artifact /MC0 BDC);
    page 2: a /Pagination /Watermark artifact, which is NOT a header/footer."""
    doc = body_pdf(3)
    # page 0 -- inline property list
    p = doc[0]
    before = set(p.get_contents())
    p.insert_text((250, 40), "ACROBAT HEADER", fontsize=9)
    x = [v for v in p.get_contents() if v not in before][0]
    doc.update_stream(x, b"/Artifact <</Attached [/Top]/Subtype /Header /Type /Pagination >>BDC \n"
                      + doc.xref_stream(x) + b"EMC \n")
    doc.xref_set_key(p.xref, "PieceInfo",
                     "<</ADBE_CompoundType<</DocSettings 1 0 R/LastModified(D:20240101)/Private/Header>>>>")
    # page 1 -- Form XObject footer
    p = doc[1]
    form = fitz.open()
    fp = form.new_page(width=612, height=792)
    fp.insert_text((250, 770), "ACROBAT FOOTER", fontsize=9)
    p.show_pdf_page(p.rect, form, 0)  # creates a Form XObject and a "q /fzFrm0 Do Q" stream
    xo_name, xo_xref = None, None
    for item in p.get_xobjects():
        if item[2] == 0:  # the form invoked by the page itself
            xo_xref, xo_name = item[0], item[1]
    doc.xref_set_key(xo_xref, "PieceInfo",
                     "<</ADBE_CompoundType<</LastModified(D:20240101)/Private/Footer>>>>")
    t, v = doc.xref_get_key(p.xref, "Resources")
    res_xref = int(v.split()[0]) if t == "xref" else p.xref
    key = "Properties/MC0" if t == "xref" else "Resources/Properties/MC0"
    doc.xref_set_key(res_xref, key, "<</Attached [/Bottom]/Subtype /Footer /Type /Pagination>>")
    last = p.get_contents()[-1]
    doc.update_stream(last, b"/Artifact /MC0 BDC\n" + doc.xref_stream(last) + b"\nEMC\n")
    assert xo_name
    # page 2 -- watermark (must survive)
    p = doc[2]
    before = set(p.get_contents())
    p.insert_text((200, 300), "WATERMARK", fontsize=30)
    x = [v for v in p.get_contents() if v not in before][0]
    doc.update_stream(x, b"/Artifact <</Subtype /Watermark /Type /Pagination >>BDC\n"
                      + doc.xref_stream(x) + b"EMC\n")
    return doc


async def test_detect_and_remove_acrobat_headers_footers(c, upload_dir):
    clean = texts(body_pdf(3))
    doc_id = store(upload_dir, acrobat_like_pdf())
    d = reopen(upload_dir, doc_id)
    assert "ACROBAT HEADER" in d[0].get_text() and "ACROBAT FOOTER" in d[1].get_text()
    d.close()

    runs = (await c.get(f"{P}/{doc_id}/organize/header-footer/runs")).json()["runs"]
    assert len(runs) == 1
    ext = runs[0]
    assert ext["id"] == "acrobat" and ext["source"] == "external" and not ext["editable"]
    assert ext["pages"] == [0, 1] and ext["subtypes"] == ["Footer", "Header"]

    r = await c.delete(f"{P}/{doc_id}/organize/header-footer/runs/acrobat")
    assert r.status_code == 200, r.text
    d = reopen(upload_dir, doc_id)
    assert "ACROBAT" not in d[0].get_text() and "ACROBAT" not in d[1].get_text()
    assert d[0].get_text() == clean[0] and d[1].get_text() == clean[1]
    assert "WATERMARK" in d[2].get_text()  # watermark artifact is not a header/footer
    assert d.xref_get_key(d[0].xref, "PieceInfo/ADBE_CompoundType")[0] == "null"
    d.close()
    assert (await c.get(f"{P}/{doc_id}/organize/header-footer/runs")).json()["runs"] == []


async def test_update_acrobat_run_replaces_it_with_editable_run(c, upload_dir):
    doc_id = store(upload_dir, acrobat_like_pdf())
    r = await c.put(f"{P}/{doc_id}/organize/header-footer/runs/acrobat", json={"footer_center": "Ours {n}"})
    assert r.status_code == 200, r.text
    new_id = r.json()["run_id"]
    assert re.fullmatch(r"r[0-9a-f]{8}", new_id)
    d = reopen(upload_dir, doc_id)
    assert "ACROBAT" not in d[0].get_text() + d[1].get_text()
    assert "Ours 2" in d[1].get_text()
    d.close()
    runs = (await c.get(f"{P}/{doc_id}/organize/header-footer/runs")).json()["runs"]
    assert [x["id"] for x in runs] == [new_id]


async def test_remove_all_runs(c, upload_dir):
    doc_id = store(upload_dir, acrobat_like_pdf())
    await c.post(f"{P}/{doc_id}/organize/page-numbers", json={"format": "{n}"})
    r = await c.delete(f"{P}/{doc_id}/organize/header-footer/runs")
    assert r.status_code == 200
    d = reopen(upload_dir, doc_id)
    assert texts(d)[:2] == texts(body_pdf(3))[:2]
    d.close()


async def test_acrobat_tagged_xobject_without_artifact_wrapper_is_removed(c, upload_dir):
    doc = body_pdf(1)
    clean = doc[0].get_text()
    p = doc[0]
    form = fitz.open()
    form.new_page(width=612, height=792).insert_text((250, 30), "XOBJ HEADER", fontsize=9)
    p.show_pdf_page(p.rect, form, 0)
    xo_xref = [it[0] for it in p.get_xobjects() if it[2] == 0][0]  # invoked by the page itself
    doc.xref_set_key(xo_xref, "PieceInfo", "<</ADBE_CompoundType<</Private/Header>>>>")
    doc_id = store(upload_dir, doc)
    assert "XOBJ HEADER" in reopen(upload_dir, doc_id)[0].get_text()
    runs = (await c.get(f"{P}/{doc_id}/organize/header-footer/runs")).json()["runs"]
    assert [(x["id"], x["subtypes"]) for x in runs] == [("acrobat", ["Header"])]
    await c.delete(f"{P}/{doc_id}/organize/header-footer/runs/acrobat")
    assert reopen(upload_dir, doc_id)[0].get_text() == clean


def test_lexer_skips_strings_and_inline_images():
    from backend.features.organize import _cs_tokens
    data = b"BT (EMC \\) BDC) Tj ET BI /W 1 /H 1 ID \x00EMC\x01 EI q /Artifact <</Type /Pagination>> BDC EMC Q"
    ops = [data[s:e] for k, s, e in _cs_tokens(data) if k == "op"]
    assert ops.count(b"EMC") == 1 and ops.count(b"BDC") == 1
