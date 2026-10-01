"""Tests for backend/features/forms.py (fill + author interactive forms).

Every test builds a real PDF with PyMuPDF, drives the HTTP API, then re-opens
the saved file and inspects the actual PDF objects/content.
"""

import io
import uuid
from pathlib import Path

import fitz
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend import advanced_ops
from backend.features import forms


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(forms, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(advanced_ops, "UPLOAD_DIR", tmp_path)
    return tmp_path


@pytest_asyncio.fixture
async def fclient(upload_dir):
    app = FastAPI()
    app.include_router(forms.router)
    app.include_router(advanced_ops.router)  # for /undo
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        yield ac


def store(upload_dir: Path, doc: fitz.Document) -> str:
    doc_id = str(uuid.uuid4())
    d = upload_dir / doc_id
    d.mkdir()
    doc.save(str(d / "original.pdf"))
    doc.close()
    return doc_id


def reopen(upload_dir: Path, doc_id: str) -> fitz.Document:
    return fitz.open(str(upload_dir / doc_id / "original.pdf"))


def widgets_by_name(doc: fitz.Document) -> dict:
    out = {}
    for page in doc:
        for w in page.widgets():
            w._keep_page = page
            out.setdefault(w.field_name, []).append(w)
    return out


def as_state(doc, xref):
    return doc.xref_get_key(xref, "AS")[1]


def native_form() -> fitz.Document:
    """A form built the way other tools build them, including a proper
    Acrobat-style radio group (parent field + kids with distinct export values)."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 60), "Application form", fontsize=14)

    def add(t, name, rect, **kw):
        w = fitz.Widget()
        w.field_type = t
        w.field_name = name
        w.rect = fitz.Rect(rect)
        for k, v in kw.items():
            setattr(w, k, v)
        return page.add_widget(w)

    add(fitz.PDF_WIDGET_TYPE_TEXT, "full_name", (72, 80, 300, 100), field_flags=forms.FF_REQUIRED,
        text_fontsize=11)
    add(fitz.PDF_WIDGET_TYPE_CHECKBOX, "subscribe", (72, 110, 86, 124), field_value=False)
    add(fitz.PDF_WIDGET_TYPE_COMBOBOX, "country", (72, 130, 250, 150), choice_values=["US", "CA", "MX"],
        field_value="US")
    add(fitz.PDF_WIDGET_TYPE_LISTBOX, "size", (72, 160, 250, 210), choice_values=["S", "M", "L"])
    add(fitz.PDF_WIDGET_TYPE_SIGNATURE, "sig", (72, 400, 300, 440))
    # radio group built by hand
    k1 = add(fitz.PDF_WIDGET_TYPE_RADIOBUTTON, "tmp1", (72, 220, 86, 234), field_value=False).xref
    k2 = add(fitz.PDF_WIDGET_TYPE_RADIOBUTTON, "tmp2", (100, 220, 114, 234), field_value=False).xref
    par = doc.get_new_xref()
    doc.update_object(par, f"<< /FT /Btn /Ff 49152 /T (plan) /V /Off /Kids [{k1} 0 R {k2} 0 R] >>")
    forms._rewrite_radio_kid(doc, k1, par, "basic", page.xref)
    forms._rewrite_radio_kid(doc, k2, par, "pro", page.xref)
    _h, fields = forms._acroform_fields(doc)
    forms._set_acroform_fields(doc, [f for f in fields if f not in (k1, k2)] + [par])
    return doc


def flat_form() -> fitz.Document:
    """A non-interactive form: labels, underscores, boxes, a rule line, checkboxes."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 60), "Patient Intake", fontsize=16)
    page.insert_text((72, 100), "Name:", fontsize=11)                       # label + blank space
    page.insert_text((72, 140), "Address: ______________________", fontsize=11)  # underscores
    page.insert_text((72, 180), "Notes", fontsize=11)
    page.draw_rect(fitz.Rect(72, 185, 400, 245), color=(0, 0, 0), width=0.8)  # empty box -> multiline
    page.draw_rect(fitz.Rect(72, 270, 84, 282), color=(0, 0, 0), width=0.8)   # checkbox
    page.insert_text((90, 280), "Subscribe to newsletter", fontsize=11)
    page.draw_line(fitz.Point(72, 360), fitz.Point(280, 360), color=(0, 0, 0), width=0.8)  # sign line
    page.insert_text((72, 374), "Signature", fontsize=9)
    page.insert_text((72, 420), "Date: 2024-01-01", fontsize=11)  # label already filled -> no field
    return doc


# ─── Listing ─────────────────────────────────────────────────────────────────


async def test_list_fields_reports_all_types(fclient, upload_dir):
    doc_id = store(upload_dir, native_form())
    r = await fclient.get(f"/api/pdf/{doc_id}/form-fields")
    assert r.status_code == 200
    body = r.json()
    by = {}
    for f in body["fields"]:
        by.setdefault(f["name"], []).append(f)
    assert by["full_name"][0]["type"] == "text"
    assert by["full_name"][0]["required"] is True
    assert by["full_name"][0]["rect"] == [72, 80, 300, 100]
    assert by["subscribe"][0]["type"] == "checkbox" and by["subscribe"][0]["value"] is False
    assert by["country"][0]["type"] == "combo" and by["country"][0]["options"] == ["US", "CA", "MX"]
    assert by["country"][0]["value"] == "US"
    assert by["size"][0]["type"] == "list"
    assert by["sig"][0]["type"] == "signature"
    plan = by["plan"]
    assert len(plan) == 2 and {p["export_value"] for p in plan} == {"basic", "pro"}
    assert plan[0]["options"] == ["basic", "pro"] and plan[0]["value"] is None
    assert body["is_form"] is True and body["pages"][0]["width"] == 612

    r = await fclient.get(f"/api/pdf/{doc_id}/form-fields", params={"page": 5})
    assert r.status_code == 400


async def test_bad_ids(fclient, upload_dir):
    assert (await fclient.get("/api/pdf/not-a-uuid/form-fields")).status_code == 400
    assert (await fclient.get(f"/api/pdf/{uuid.uuid4()}/form-fields")).status_code == 404


# ─── Filling ─────────────────────────────────────────────────────────────────


async def test_fill_every_type(fclient, upload_dir):
    doc_id = store(upload_dir, native_form())
    r = await fclient.post(f"/api/pdf/{doc_id}/form-fields/fill", json={"values": {
        "full_name": "Ada Lovelace",
        "subscribe": True,
        "plan": "pro",
        "country": "CA",
        "size": "M",
        "nope": "x",
    }})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["filled"]) == {"full_name", "subscribe", "plan", "country", "size"}
    assert body["errors"] == {"nope": "no such field"}

    doc = reopen(upload_dir, doc_id)
    w = widgets_by_name(doc)
    assert w["full_name"][0].field_value == "Ada Lovelace"
    assert as_state(doc, w["subscribe"][0].xref) == "/Yes"
    states = {x.on_state(): as_state(doc, x.xref) for x in w["plan"]}
    assert states == {"basic": "/Off", "pro": "/pro"}
    par = forms._parent_xref(doc, w["plan"][0].xref)
    assert doc.xref_get_key(par, "V") == ("name", "/pro")
    assert w["country"][0].field_value == "CA"
    assert w["size"][0].field_value == "M"
    # Text actually rendered in the appearance stream:
    ap = forms._ref_xref(doc.xref_get_key(w["full_name"][0].xref, "AP/N")[1])
    assert b"Ada Lovelace" in doc.xref_stream(ap)
    doc.close()

    # switch radio, uncheck box
    r = await fclient.post(f"/api/pdf/{doc_id}/form-fields/fill",
                           json={"values": {"plan": "basic", "subscribe": False}})
    assert r.status_code == 200
    doc = reopen(upload_dir, doc_id)
    w = widgets_by_name(doc)
    assert {x.on_state(): as_state(doc, x.xref) for x in w["plan"]} == {"basic": "/basic", "pro": "/Off"}
    assert as_state(doc, w["subscribe"][0].xref) == "/Off"
    doc.close()


async def test_fill_rejects_invalid_values(fclient, upload_dir):
    doc_id = store(upload_dir, native_form())
    r = await fclient.post(f"/api/pdf/{doc_id}/form-fields/fill", json={"values": {
        "country": "FR", "plan": "gold", "sig": "x", "full_name": "ok"}})
    assert r.status_code == 200
    errs = r.json()["errors"]
    assert set(errs) == {"country", "plan", "sig"}
    r = await fclient.post(f"/api/pdf/{doc_id}/form-fields/fill", json={"values": {"country": "FR"}})
    assert r.status_code == 400
    doc = reopen(upload_dir, doc_id)
    assert widgets_by_name(doc)["country"][0].field_value == "US"
    doc.close()


# ─── Creating ────────────────────────────────────────────────────────────────


async def test_create_every_field_type(fclient, upload_dir):
    base = fitz.open()
    base.new_page(width=612, height=792)
    doc_id = store(upload_dir, base)
    url = f"/api/pdf/{doc_id}/form-fields"

    specs = [
        {"page": 0, "type": "text", "rect": [72, 72, 300, 92], "name": "email", "value": "a@b.co",
         "font_size": 10, "required": True, "tooltip": "Your email"},
        {"page": 0, "type": "checkbox", "rect": [72, 100, 86, 114], "name": "agree", "export_value": "Agree",
         "value": True},
        {"page": 0, "type": "radio", "rect": [72, 130, 86, 144], "name": "color", "export_value": "red"},
        {"page": 0, "type": "radio", "rect": [100, 130, 114, 144], "name": "color", "export_value": "blue",
         "value": True},
        {"page": 0, "type": "combo", "rect": [72, 160, 250, 180], "name": "state", "options": ["NC", "SC"],
         "value": "SC"},
        {"page": 0, "type": "list", "rect": [72, 190, 250, 240], "name": "pets", "options": ["cat", "dog"]},
        {"page": 0, "type": "signature", "rect": [72, 300, 300, 340], "name": "signhere"},
        {"page": 0, "type": "text", "rect": [72, 400, 300, 480], "multiline": True, "max_len": 200},
    ]
    for s in specs:
        r = await fclient.post(url, json=s)
        assert r.status_code == 200, (s, r.text)
        assert r.json()["field"]["type"] == s["type"]

    doc = reopen(upload_dir, doc_id)
    w = widgets_by_name(doc)
    assert w["email"][0].field_value == "a@b.co"
    assert w["email"][0].field_flags & forms.FF_REQUIRED
    assert w["email"][0].text_fontsize == 10
    assert w["email"][0].field_label == "Your email"
    assert w["agree"][0].on_state() == "Agree" and as_state(doc, w["agree"][0].xref) == "/Agree"
    # radio: ONE field with two kids, distinct export values, blue selected
    kids = w["color"]
    assert len(kids) == 2
    par = {forms._parent_xref(doc, k.xref) for k in kids}
    assert len(par) == 1 and 0 not in par
    par = par.pop()
    assert doc.xref_get_key(par, "T") == ("string", "color")
    assert all(doc.xref_get_key(k.xref, "T")[0] == "null" for k in kids)
    _h, fields = forms._acroform_fields(doc)
    assert par in fields and not any(k.xref in fields for k in kids)
    assert {k.on_state(): as_state(doc, k.xref) for k in kids} == {"red": "/Off", "blue": "/blue"}
    assert w["state"][0].field_value == "SC" and w["state"][0].choice_values == ["NC", "SC"]
    assert w["pets"][0].field_type == fitz.PDF_WIDGET_TYPE_LISTBOX
    assert w["signhere"][0].field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE
    auto = w["text"][0]
    assert auto.field_flags & forms.FF_MULTILINE and auto.text_maxlen == 200
    doc.close()

    # The authored radio group behaves as a group when filled.
    r = await fclient.post(f"{url}/fill", json={"values": {"color": "red"}})
    assert r.status_code == 200
    doc = reopen(upload_dir, doc_id)
    assert {k.on_state(): as_state(doc, k.xref) for k in widgets_by_name(doc)["color"]} == {
        "red": "/red", "blue": "/Off"}
    doc.close()

    # Duplicate names and bad input are rejected
    assert (await fclient.post(url, json={**specs[0]})).status_code == 409
    assert (await fclient.post(url, json={"page": 0, "type": "radio", "rect": [1, 1, 20, 20],
                                          "name": "color", "export_value": "red"})).status_code == 409
    assert (await fclient.post(url, json={"page": 3, "type": "text", "rect": [1, 1, 50, 20]})).status_code == 400
    assert (await fclient.post(url, json={"page": 0, "type": "combo", "rect": [1, 1, 50, 20]})).status_code == 400
    assert (await fclient.post(url, json={"page": 0, "type": "bogus", "rect": [1, 1, 50, 20]})).status_code == 400


async def test_create_on_rotated_page_uses_display_coords(fclient, upload_dir):
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.set_rotation(90)
    doc_id = store(upload_dir, doc)
    r = await fclient.post(f"/api/pdf/{doc_id}/form-fields",
                           json={"page": 0, "type": "text", "rect": [100, 50, 300, 70], "name": "t"})
    assert r.status_code == 200
    f = r.json()["field"]
    assert f["rect"] == [100, 50, 300, 70]
    assert f["pdf_rect"] != f["rect"]  # stored in unrotated space
    d = reopen(upload_dir, doc_id)
    pg = d[0]
    w = next(pg.widgets())
    assert (w.rect * pg.rotation_matrix).normalize() == fitz.Rect(100, 50, 300, 70)
    d.close()


# ─── Editing / deleting ──────────────────────────────────────────────────────


async def test_update_field_properties(fclient, upload_dir):
    doc_id = store(upload_dir, native_form())
    fields = (await fclient.get(f"/api/pdf/{doc_id}/form-fields")).json()["fields"]
    fid = {f["name"]: f["id"] for f in fields if f["type"] != "radio"}
    radio = [f for f in fields if f["name"] == "plan"]

    r = await fclient.patch(f"/api/pdf/{doc_id}/form-fields/{fid['full_name']}", json={
        "name": "applicant", "rect": [80, 90, 350, 115], "required": False, "readonly": True,
        "font_size": 14, "tooltip": "Applicant name", "value": "Grace"})
    assert r.status_code == 200, r.text
    f = r.json()["field"]
    assert f["name"] == "applicant" and f["rect"] == [80, 90, 350, 115]
    assert f["required"] is False and f["readonly"] is True and f["value"] == "Grace"

    r = await fclient.patch(f"/api/pdf/{doc_id}/form-fields/{fid['country']}",
                            json={"options": ["DE", "FR"], "editable": True})
    assert r.status_code == 200
    r = await fclient.patch(f"/api/pdf/{doc_id}/form-fields/{fid['subscribe']}", json={"export_value": "On"})
    assert r.status_code == 200
    # move one radio kid + rename its export; rename the group
    r = await fclient.patch(f"/api/pdf/{doc_id}/form-fields/{radio[1]['id']}",
                            json={"rect": [200, 300, 220, 320], "export_value": "premium", "name": "tier"})
    assert r.status_code == 200, r.text
    assert r.json()["field"]["rect"] == [200, 300, 220, 320]
    # renaming to an existing name conflicts
    r = await fclient.patch(f"/api/pdf/{doc_id}/form-fields/{fid['size']}", json={"name": "tier"})
    assert r.status_code == 409

    doc = reopen(upload_dir, doc_id)
    w = widgets_by_name(doc)
    a = w["applicant"][0]
    assert a.rect == fitz.Rect(80, 90, 350, 115)
    assert a.field_value == "Grace" and a.text_fontsize == 14 and a.field_label == "Applicant name"
    assert a.field_flags & forms.FF_READONLY and not a.field_flags & forms.FF_REQUIRED
    assert w["country"][0].choice_values == ["DE", "FR"]
    assert w["country"][0].field_flags & forms.FF_EDIT
    assert w["subscribe"][0].on_state() == "On"
    assert "plan" not in w and len(w["tier"]) == 2
    assert {k.on_state() for k in w["tier"]} == {"basic", "premium"}
    moved = [k for k in w["tier"] if k.on_state() == "premium"][0]
    assert moved.rect == fitz.Rect(200, 300, 220, 320)
    doc.close()

    # readonly field refuses normal fill
    r = await fclient.post(f"/api/pdf/{doc_id}/form-fields/fill", json={"values": {"applicant": "X", "size": "S"}})
    assert r.json()["errors"] == {"applicant": "field is read-only"}


async def test_delete_field_and_radio_kid(fclient, upload_dir):
    doc_id = store(upload_dir, native_form())
    fields = (await fclient.get(f"/api/pdf/{doc_id}/form-fields")).json()["fields"]
    text_id = [f["id"] for f in fields if f["name"] == "full_name"][0]
    radio_ids = [f["id"] for f in fields if f["name"] == "plan"]

    r = await fclient.delete(f"/api/pdf/{doc_id}/form-fields/{text_id}")
    assert r.status_code == 200 and r.json()["deleted"] == [text_id]
    r = await fclient.delete(f"/api/pdf/{doc_id}/form-fields/{radio_ids[0]}")
    assert r.status_code == 200

    doc = reopen(upload_dir, doc_id)
    w = widgets_by_name(doc)
    assert "full_name" not in w
    _h, fl = forms._acroform_fields(doc)
    assert text_id not in fl
    par = forms._parent_xref(doc, w["plan"][0].xref)
    assert doc.xref_get_key(par, "Kids")[1].replace(" ", "") == f"[{radio_ids[1]}0R]"
    doc.close()

    r = await fclient.delete(f"/api/pdf/{doc_id}/form-fields/{radio_ids[1]}", params={"whole_field": True})
    assert r.status_code == 200
    doc = reopen(upload_dir, doc_id)
    assert "plan" not in widgets_by_name(doc)
    _h, fl = forms._acroform_fields(doc)
    assert par not in fl
    doc.close()
    assert (await fclient.delete(f"/api/pdf/{doc_id}/form-fields/999999")).status_code == 404


# ─── Flatten ─────────────────────────────────────────────────────────────────


async def test_flatten_bakes_values_into_content(fclient, upload_dir):
    doc_id = store(upload_dir, native_form())
    await fclient.post(f"/api/pdf/{doc_id}/form-fields/fill",
                       json={"values": {"full_name": "Katherine Johnson", "country": "MX"}})
    doc = reopen(upload_dir, doc_id)
    assert len(list(doc[0].widgets())) == 7
    assert b"Katherine" not in doc[0].read_contents()  # value lives only in the widget so far
    doc.close()

    r = await fclient.post(f"/api/pdf/{doc_id}/form-fields/flatten")
    assert r.status_code == 200 and r.json()["flattened"] == 7

    doc = reopen(upload_dir, doc_id)
    assert list(doc[0].widgets()) == []
    assert not doc.is_form_pdf
    text = doc[0].get_text()
    assert "Katherine Johnson" in text and "MX" in text
    doc.close()
    assert (await fclient.post(f"/api/pdf/{doc_id}/form-fields/flatten")).status_code == 400


# ─── Auto-detect ─────────────────────────────────────────────────────────────


async def test_detect_fields_on_flat_form(fclient, upload_dir):
    doc_id = store(upload_dir, flat_form())
    url = f"/api/pdf/{doc_id}/form-fields/detect"

    dry = (await fclient.post(url, json={"dry_run": True})).json()
    assert dry["created"] == [] and dry["count"] == len(dry["candidates"])
    doc = reopen(upload_dir, doc_id)
    assert list(doc[0].widgets()) == []  # dry run did not modify
    doc.close()

    r = await fclient.post(url, json={})
    assert r.status_code == 200
    created = r.json()["created"]
    by_src = {}
    for c in created:
        by_src.setdefault(c["source"], []).append(c)

    # "Name:" + blank space -> text field to the right of the label, same row
    lab = by_src["label"]
    assert len(lab) == 1 and lab[0]["name"] == "Name" and lab[0]["type"] == "text"
    assert lab[0]["rect"][0] > 100 and lab[0]["rect"][1] < 100 < lab[0]["rect"][3]
    # underscores after "Address:"
    und = by_src["underscore"]
    assert len(und) == 1 and und[0]["name"] == "Address"
    # empty box -> multiline text; small square -> checkbox
    boxes = {c["type"]: c for c in by_src["box"]}
    assert boxes["text"]["rect"][0] >= 72 and boxes["text"]["rect"][3] <= 245
    assert boxes["checkbox"]["name"].startswith("Subscribe")
    # rule line with "Signature" below it
    assert by_src["line"][0]["name"] == "Signature"
    # "Date: 2024-01-01" is already filled -> no field
    assert not any(c["name"].startswith("Date") for c in created)

    doc = reopen(upload_dir, doc_id)
    w = widgets_by_name(doc)
    assert set(w) == {c["name"] for c in created}
    assert w[boxes["checkbox"]["name"]][0].field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX
    notes = w[boxes["text"]["name"]][0]
    assert notes.field_flags & forms.FF_MULTILINE
    doc.close()

    # Running again finds nothing new (existing widgets suppress duplicates).
    again = (await fclient.post(url, json={})).json()
    assert again["created"] == []


# ─── Export / import ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("fmt", ["json", "fdf", "xfdf"])
async def test_export_import_roundtrip(fclient, upload_dir, fmt):
    src = store(upload_dir, native_form())
    dst = store(upload_dir, native_form())
    values = {"full_name": "Émilie (du) Châtelet", "subscribe": True, "plan": "pro", "country": "CA", "size": "L"}
    assert (await fclient.post(f"/api/pdf/{src}/form-fields/fill", json={"values": values})).status_code == 200

    r = await fclient.get(f"/api/pdf/{src}/form-fields/export", params={"format": fmt})
    assert r.status_code == 200
    data = r.text if fmt != "fdf" else r.content.decode("latin-1")
    if fmt == "json":
        assert r.json()["fields"]["subscribe"] is True
    elif fmt == "fdf":
        assert data.startswith("%FDF-1.2") and "/V /pro" in data
    else:
        assert "<xfdf" in data and 'name="plan"' in data

    r = await fclient.post(f"/api/pdf/{dst}/form-fields/import", json={"format": fmt, "data": data})
    assert r.status_code == 200, r.text
    assert set(r.json()["filled"]) >= set(values)

    doc = reopen(upload_dir, dst)
    w = widgets_by_name(doc)
    assert w["full_name"][0].field_value == values["full_name"]
    assert as_state(doc, w["subscribe"][0].xref) == "/Yes"
    assert {k.on_state(): as_state(doc, k.xref) for k in w["plan"]} == {"basic": "/Off", "pro": "/pro"}
    assert w["country"][0].field_value == "CA" and w["size"][0].field_value == "L"
    doc.close()


async def test_import_rejects_garbage(fclient, upload_dir):
    doc_id = store(upload_dir, native_form())
    url = f"/api/pdf/{doc_id}/form-fields/import"
    assert (await fclient.post(url, json={"format": "json", "data": "{nope"})).status_code == 400
    assert (await fclient.post(url, json={"format": "xfdf", "data": "<xfdf"})).status_code == 400
    assert (await fclient.post(url, json={"format": "fdf", "data": "%FDF-1.2 nothing"})).status_code == 400
    assert (await fclient.post(url, json={"format": "csv", "data": "a,b"})).status_code == 400


# ─── Undo integration ────────────────────────────────────────────────────────


async def test_mutations_snapshot_for_undo(fclient, upload_dir):
    base = fitz.open()
    base.new_page()
    doc_id = store(upload_dir, base)
    r = await fclient.post(f"/api/pdf/{doc_id}/form-fields",
                           json={"page": 0, "type": "text", "rect": [72, 72, 200, 92], "name": "x"})
    assert r.status_code == 200
    doc = reopen(upload_dir, doc_id)
    assert "x" in widgets_by_name(doc)
    doc.close()

    r = await fclient.post(f"/api/pdf/{doc_id}/undo")
    assert r.status_code == 200, r.text
    doc = reopen(upload_dir, doc_id)
    assert widgets_by_name(doc) == {}
    doc.close()

    # a failed mutation must not push a history entry
    hist_before = (await fclient.get(f"/api/pdf/{doc_id}/history")).json()
    r = await fclient.post(f"/api/pdf/{doc_id}/form-fields",
                           json={"page": 9, "type": "text", "rect": [72, 72, 200, 92]})
    assert r.status_code == 400
    assert (await fclient.get(f"/api/pdf/{doc_id}/history")).json() == hist_before
