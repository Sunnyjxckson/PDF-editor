"""convert2: forms auto-detect fixes (table header rows, scanned pages, radio
groups, date fields) and multi-select list boxes.  Every test re-opens the
saved PDF with PyMuPDF and inspects the real widgets."""

import io
import uuid
from pathlib import Path

import fitz
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image, ImageDraw, ImageFont

from backend import advanced_ops
from backend.features import convert, forms

QA_PDF = Path("/private/tmp/claude-501/-Users-sunnyjackson-Pylor/39244b13-d9db-4e41-b44b-94c102173555/"
              "scratchpad/qa/test.pdf")
HAS_ENG = "eng" in convert._available_languages()


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(forms, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(advanced_ops, "UPLOAD_DIR", tmp_path)
    return tmp_path


@pytest_asyncio.fixture
async def fc(upload_dir):
    app = FastAPI()
    app.include_router(forms.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        yield ac


def store(upload_dir, doc) -> str:
    doc_id = str(uuid.uuid4())
    (upload_dir / doc_id).mkdir()
    doc.save(str(upload_dir / doc_id / "original.pdf"))
    doc.close()
    return doc_id


def reopen(upload_dir, doc_id):
    return fitz.open(str(upload_dir / doc_id / "original.pdf"))


def widgets(doc):
    out = []
    for p in doc:
        for w in p.widgets():
            w._keep_page = p
            out.append(w)
    return out


def table_page(doc=None):
    """The QA report's ruled table (header row Item/Qty/Price) on page 1."""
    doc = doc or fitz.open()
    p = doc.new_page()
    p.insert_text((72, 72), "Quarterly Report 2026", fontname="hebo", fontsize=20)
    for i in range(4):
        p.draw_line((72, 360 + i * 20), (372, 360 + i * 20))
    for x in (72, 172, 272, 372):
        p.draw_line((x, 360), (x, 420))
    for r, row in enumerate([("Item", "Qty", "Price"), ("Widget", "3", "9.99"), ("Gadget", "5", "4.50")]):
        for c, v in enumerate(row):
            p.insert_text((76 + c * 100, 375 + r * 20), v, fontname="helv", fontsize=10)
    return doc


HEADER = fitz.Rect(72, 340, 372, 380)  # header row plus the band just above it


def _assert_no_header_fields(doc, page_no):
    for w in widgets(doc):
        if w._keep_page.number != page_no:
            continue
        assert not (fitz.Rect(w.rect) & HEADER).get_area() > 0, (w.field_name, w.rect)
        assert "Item" not in w.field_name and "Qty" not in w.field_name, w.field_name


async def test_table_header_row_never_becomes_a_field_inline(fc, upload_dir):
    doc_id = store(upload_dir, table_page())
    r = await fc.post(f"/api/pdf/{doc_id}/form-fields/detect", json={"pages": [0]})
    assert r.status_code == 200
    assert not any("Item" in c["name"] for c in r.json()["created"]), r.json()
    _assert_no_header_fields(reopen(upload_dir, doc_id), 0)


@pytest.mark.skipif(not QA_PDF.exists(), reason="QA test.pdf not present")
async def test_table_header_row_regression_qa_pdf(fc, upload_dir):
    doc_id = store(upload_dir, fitz.open(str(QA_PDF)))
    r = await fc.post(f"/api/pdf/{doc_id}/form-fields/detect", json={"pages": [0]})
    assert r.status_code == 200
    assert all("Item_Qty_Price" not in c["name"] for c in r.json()["created"]), r.json()
    _assert_no_header_fields(reopen(upload_dir, doc_id), 0)


def vector_choice_form():
    doc = fitz.open()
    p = doc.new_page(width=612, height=792)
    p.insert_text((72, 60), "Registration", fontsize=16)
    # row radio group
    p.insert_text((72, 110), "Marital status:", fontsize=11)
    for x, lab in ((170, "Single"), (250, "Married"), (340, "Other")):
        p.draw_rect(fitz.Rect(x, 100, x + 11, 111), color=(0, 0, 0), width=0.8)
        p.insert_text((x + 15, 110), lab, fontsize=11)
    # column radio group
    p.insert_text((72, 150), "Preferred contact?", fontsize=11)
    for i, lab in enumerate(("Email", "Phone")):
        y = 160 + i * 18
        p.draw_rect(fitz.Rect(80, y, 91, y + 11), color=(0, 0, 0), width=0.8)
        p.insert_text((96, y + 10), lab, fontsize=11)
    # "check all that apply" stays checkboxes
    p.insert_text((72, 240), "Interests (check all that apply):", fontsize=11)
    for x, lab in ((260, "Music"), (340, "Sport")):
        p.draw_rect(fitz.Rect(x, 230, x + 11, 241), color=(0, 0, 0), width=0.8)
        p.insert_text((x + 15, 240), lab, fontsize=11)
    # date fields
    p.insert_text((72, 290), "Date of Birth:", fontsize=11)
    p.insert_text((72, 330), "Start date:", fontsize=11)
    p.draw_line((140, 332), (300, 332), color=(0, 0, 0), width=0.8)
    p.insert_text((72, 370), "Company:", fontsize=11)
    return doc


async def test_detect_radio_groups_and_date_fields(fc, upload_dir):
    doc_id = store(upload_dir, vector_choice_form())
    r = await fc.post(f"/api/pdf/{doc_id}/form-fields/detect", json={})
    assert r.status_code == 200, r.text
    created = r.json()["created"]

    doc = reopen(upload_dir, doc_id)
    ws = widgets(doc)
    by = {}
    for w in ws:
        by.setdefault(w.field_name, []).append(w)

    # row group: one radio field, three kids with the option labels as export values
    ms = by["Marital_status"]
    assert len(ms) == 3 and all(w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON for w in ms)
    assert sorted(forms._on_state(w) for w in ms) == ["Married", "Other", "Single"]
    par = {forms._parent_xref(doc, w.xref) for w in ms}
    assert len(par) == 1 and 0 not in par  # real group: shared parent field
    # column group
    pc = by["Preferred_contact"]
    assert sorted(forms._on_state(w) for w in pc) == ["Email", "Phone"]
    assert all(w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON for w in pc)
    # "check all that apply" -> independent checkboxes
    cbs = [w for w in ws if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX]
    assert {w.field_name for w in cbs} >= {"Music", "Sport"}

    # date fields carry Acrobat date format actions; plain text does not
    dates = [w for w in ws if w.field_type == fitz.PDF_WIDGET_TYPE_TEXT and forms._field_format(doc, w) == "date"]
    names = {w.field_name for w in dates}
    assert {"Date_of_Birth", "Start_date"} <= names, names
    js = doc.xref_get_key(dates[0].xref, "AA/F/JS")[1]
    assert 'AFDate_FormatEx("mm/dd/yyyy")' in js
    assert "AFDate_KeystrokeEx" in doc.xref_get_key(dates[0].xref, "AA/K/JS")[1]
    assert forms._field_format(doc, by["Company"][0]) is None
    doc.close()

    # the API reports them, and the radio group is fillable by export value
    lst = (await fc.get(f"/api/pdf/{doc_id}/form-fields")).json()["fields"]
    assert {f["name"]: f.get("format") for f in lst}["Date_of_Birth"] == "date"
    f = await fc.post(f"/api/pdf/{doc_id}/form-fields/fill",
                      json={"values": {"Marital_status": "Married", "Date_of_Birth": "1990-07-04"}})
    assert f.status_code == 200 and not f.json()["errors"], f.text
    doc = reopen(upload_dir, doc_id)
    by = {}
    for w in widgets(doc):
        by.setdefault(w.field_name, []).append(w)
    on = [forms._on_state(w) for w in by["Marital_status"] if forms._is_on(doc, w.xref)]
    assert on == ["Married"]
    assert by["Date_of_Birth"][0].field_value == "07/04/1990"  # ISO normalised to the field's format
    assert any(c.get("format") == "date" for c in created)


def scanned_form() -> fitz.Document:
    W, H = 1700, 2200  # 200 dpi letter
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 42)
    except Exception:
        f = ImageFont.load_default(size=42)
    d.text((150, 200), "Full Name:", fill=0, font=f)
    d.line((450, 248, 1300, 248), fill=0, width=3)
    d.text((150, 350), "Date of Birth:", fill=0, font=f)
    d.line((520, 398, 1000, 398), fill=0, width=3)
    d.text((150, 500), "Gender:", fill=0, font=f)
    d.rectangle((420, 502, 458, 540), outline=0, width=3)
    d.text((475, 500), "Male", fill=0, font=f)
    d.rectangle((650, 502, 688, 540), outline=0, width=3)
    d.text((705, 500), "Female", fill=0, font=f)
    d.rectangle((150, 652, 188, 690), outline=0, width=3)
    d.text((205, 650), "I agree to the terms", fill=0, font=f)
    d.rectangle((150, 800, 1300, 1000), outline=0, width=3)
    d.text((150, 745), "Comments", fill=0, font=f)
    b = io.BytesIO(); img.save(b, "PNG")
    doc = fitz.open()
    p = doc.new_page(width=612, height=792)
    p.insert_image(p.rect, stream=b.getvalue())
    return doc


@pytest.mark.skipif(not HAS_ENG, reason="tesseract eng data not installed")
async def test_detect_on_scanned_page_uses_ocr_and_raster_lines(fc, upload_dir):
    doc_id = store(upload_dir, scanned_form())
    r = await fc.post(f"/api/pdf/{doc_id}/form-fields/detect", json={})
    assert r.status_code == 200, r.text
    created = r.json()["created"]
    assert all(c["source"].startswith("scan") for c in created), created

    doc = reopen(upload_dir, doc_id)
    assert doc[0].get_text().strip() == ""  # detection did not add an OCR layer
    by = {}
    for w in widgets(doc):
        by.setdefault(w.field_name, []).append(w)
    s = 72 / 200
    # underline -> text field sitting on the line, named from the OCR'd label
    fn = by["Full_Name"][0]
    assert fn.field_type == fitz.PDF_WIDGET_TYPE_TEXT
    assert abs(fn.rect.x0 - 450 * s) < 4 and abs(fn.rect.x1 - 1300 * s) < 4 and abs(fn.rect.y1 - 248 * s) < 4
    dob = by["Date_of_Birth"][0]
    assert forms._field_format(doc, dob) == "date"
    # boxes in a row after "Gender:" -> radio group with OCR'd option labels
    g = by["Gender"]
    assert sorted(forms._on_state(w) for w in g) == ["Female", "Male"]
    assert all(w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON for w in g)
    box = min(g, key=lambda w: w.rect.x0).rect
    assert abs(box.x0 - 420 * s) < 3 and abs(box.y0 - 502 * s) < 3
    # lone box -> checkbox labelled by the text right of it
    agree = [n for n in by if "agree_to_the_terms" in n]
    assert agree and by[agree[0]][0].field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX
    # big empty box -> multiline text
    multi = [w for w in widgets(doc) if w.field_type == fitz.PDF_WIDGET_TYPE_TEXT and w.field_flags & forms.FF_MULTILINE]
    assert len(multi) == 1 and multi[0].rect.height > 60


def list_doc():
    doc = fitz.open()
    doc.new_page(width=612, height=792).insert_text((72, 60), "Lists", fontsize=12)
    return doc


async def test_multi_select_list_box(fc, upload_dir):
    doc_id = store(upload_dir, list_doc())
    r = await fc.post(f"/api/pdf/{doc_id}/form-fields", json={
        "page": 0, "type": "list", "rect": [72, 100, 250, 170], "name": "toppings",
        "options": ["Cheese", "Ham", "Olives", "Pepper"], "multi_select": True})
    assert r.status_code == 200, r.text
    assert r.json()["field"]["multi_select"] is True
    r = await fc.post(f"/api/pdf/{doc_id}/form-fields", json={
        "page": 0, "type": "list", "rect": [300, 100, 450, 170], "name": "size", "options": ["S", "M", "L"]})
    assert r.json()["field"]["multi_select"] is False

    f = await fc.post(f"/api/pdf/{doc_id}/form-fields/fill", json={"values": {"toppings": ["Cheese", "Olives"]}})
    assert f.status_code == 200 and f.json()["filled"] == ["toppings"], f.text
    bad = await fc.post(f"/api/pdf/{doc_id}/form-fields/fill", json={"values": {"size": ["S", "L"]}})
    assert bad.status_code == 400 and "only one" in bad.text

    doc = reopen(upload_dir, doc_id)
    w = next(w for w in widgets(doc) if w.field_name == "toppings")
    assert w.field_flags & forms.FF_MULTISELECT
    assert doc.xref_get_key(w.xref, "V")[1].replace(" ", "") == "[(Cheese)(Olives)]"
    assert doc.xref_get_key(w.xref, "I")[1].replace(" ", "") == "[02]"
    # the appearance highlights both selected rows
    ap = int(doc.xref_get_key(w.xref, "AP/N")[1].split()[0])
    assert doc.xref_stream(ap).count(b"0.6 0.75 0.95 rg") == 2
    doc.close()

    lst = {f["name"]: f for f in (await fc.get(f"/api/pdf/{doc_id}/form-fields")).json()["fields"]}
    assert lst["toppings"]["value"] == ["Cheese", "Olives"]
    exp = (await fc.get(f"/api/pdf/{doc_id}/form-fields/export?format=json")).json()
    assert exp["fields"]["toppings"] == ["Cheese", "Olives"]

    # turning multi-select off keeps only the first selection
    fid = lst["toppings"]["id"]
    u = await fc.patch(f"/api/pdf/{doc_id}/form-fields/{fid}", json={"multi_select": False})
    assert u.status_code == 200, u.text
    assert u.json()["field"]["multi_select"] is False and u.json()["field"]["value"] == "Cheese"
    doc = reopen(upload_dir, doc_id)
    w = next(w for w in widgets(doc) if w.field_name == "toppings")
    assert not w.field_flags & forms.FF_MULTISELECT
    assert doc.xref_get_key(w.xref, "V") == ("string", "Cheese")


def scanned_headings_form():
    """'Comments:' over a big box and a 'check all that apply' question over a
    column of boxes: neither heading has a fill-in blank to its right."""
    W, H = 1700, 2200
    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 40)
    d.text((150, 760), "Symptoms (check all that apply):", fill=0, font=f)
    for i, lab in enumerate(["Fever", "Cough"]):
        y = 830 + i * 70
        d.rectangle((200, y, 240, y + 40), outline=0, width=3)
        d.text((260, y - 2), lab, fill=0, font=f)
    d.text((150, 1000), "Comments:", fill=0, font=f)
    d.rectangle((150, 1060, 1500, 1300), outline=0, width=3)
    b = io.BytesIO()
    im.save(b, "PNG")
    doc = fitz.open()
    p = doc.new_page(width=612, height=792)
    p.insert_image(p.rect, stream=b.getvalue())
    return doc


@pytest.mark.skipif(not HAS_ENG, reason="tesseract eng data not installed")
async def test_scanned_heading_over_box_is_not_a_label_gap_field(fc, upload_dir):
    doc_id = store(upload_dir, scanned_headings_form())
    r = await fc.post(f"/api/pdf/{doc_id}/form-fields/detect", json={})
    assert r.status_code == 200, r.text
    doc = reopen(upload_dir, doc_id)
    ws = widgets(doc)
    texts = [w for w in ws if w.field_type == fitz.PDF_WIDGET_TYPE_TEXT]
    # only the big comments box; no single-line field beside either heading
    assert len(texts) == 1 and texts[0].rect.height > 60, [(w.field_name, w.rect) for w in texts]
    assert sum(w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX for w in ws) == 2
