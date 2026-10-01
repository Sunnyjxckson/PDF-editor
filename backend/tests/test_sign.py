"""Tests for backend/features/sign.py (visual Fill & Sign + pyHanko digital signatures).

The router is mounted on a private FastAPI app with UPLOAD_DIR redirected to a
tmp dir, so these tests never touch main.py or the real uploads folder.
"""

import base64
import io
import json
import os
import uuid
from pathlib import Path

import fitz
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image

from backend import advanced_ops
from backend.features import sign as sign_mod


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sign_mod, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(advanced_ops, "UPLOAD_DIR", tmp_path)
    return tmp_path


@pytest_asyncio.fixture
async def sclient(upload_dir):
    app = FastAPI()
    app.include_router(sign_mod.router)
    app.include_router(advanced_ops.router)  # for /undo
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        yield ac


def _make_doc(upload_dir: Path, rotation: int = 0, with_form: bool = False, with_annot: bool = False) -> str:
    doc_id = str(uuid.uuid4())
    d = upload_dir / doc_id
    d.mkdir()
    doc = fitz.open()
    for i in range(2):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), f"Agreement page {i + 1}", fontsize=12)
        if rotation:
            page.set_rotation(rotation)
    if with_form:
        w = fitz.Widget()
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        w.field_name = "client_name"
        w.field_value = "ACME Holdings"
        w.rect = fitz.Rect(72, 200, 300, 222)
        doc[0].add_widget(w)
    if with_annot:
        a = doc[0].add_freetext_annot(fitz.Rect(72, 300, 300, 330), "Reviewed by legal", fontsize=11)
        a.update()
    doc.save(str(d / "original.pdf"))
    doc.close()
    return doc_id


def _png_b64(w=300, h=100, bar_top=True) -> str:
    """Transparent PNG with an opaque dark bar in the TOP 30% (orientation marker)."""
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bar_h = int(h * 0.3)
    for x in range(w):
        for y in range(bar_h) if bar_top else range(h - bar_h, h):
            im.putpixel((x, y), (10, 10, 120, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _open(upload_dir: Path, doc_id: str) -> fitz.Document:
    return fitz.open(str(upload_dir / doc_id / "original.pdf"))


def _dark_bbox(page: fitz.Page):
    """Bounding box of dark pixels in a 72-dpi render (= visible-frame PDF points)."""
    pix = page.get_pixmap(dpi=72)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    return img.point(lambda v: 255 if v < 128 else 0).getbbox()


def _history_len(upload_dir: Path, doc_id: str) -> int:
    hf = upload_dir / doc_id / "history.json"
    return len(json.loads(hf.read_text())["versions"]) if hf.exists() else 0


async def _new_cert(sclient, **kw):
    body = {"name": "Jane Signer", "email": "jane@example.com", **kw}
    r = await sclient.post("/api/pdf/signing/certificates", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ─── Visual signature stamping ───────────────────────────────────────────────


async def test_stamp_inserts_transparent_image_at_rect(sclient, upload_dir):
    doc_id = _make_doc(upload_dir)
    rect = [100, 500, 250, 550]  # 150x50 box, same 3:1 aspect as the PNG
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/stamp",
                           json={"page": 0, "rect": rect, "image": "data:image/png;base64," + _png_b64()})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == {"image": 1}

    doc = _open(upload_dir, doc_id)
    imgs = doc[0].get_images(full=True)
    assert len(imgs) == 1
    xref, smask = imgs[0][0], imgs[0][1]
    assert smask != 0, "PNG alpha channel must survive as an SMask (transparent signature)"
    placed = doc[0].get_image_rects(xref)[0]
    for a, b in zip(placed, rect):
        assert abs(a - b) < 0.5
    # page 2 untouched, original text intact
    assert doc[1].get_images() == []
    assert "Agreement page 1" in doc[0].get_text()
    # rendered: the opaque bar sits in the top 30% of the rect, the rest is transparent
    x0, y0, x1, y1 = _dark_bbox_region(doc[0], rect)
    assert y0 <= rect[1] + 2 and y1 <= rect[1] + 0.3 * 50 + 2
    doc.close()
    assert _history_len(upload_dir, doc_id) == 1, "snapshot() must run before mutation"


def _dark_bbox_region(page, rect):
    clip = fitz.Rect(rect)
    pix = page.get_pixmap(dpi=72, clip=clip)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    bb = img.point(lambda v: 255 if v < 128 else 0).getbbox()
    assert bb, "nothing rendered inside the rect"
    return (bb[0] + clip.x0, bb[1] + clip.y0, bb[2] + clip.x0, bb[3] + clip.y0)


@pytest.mark.parametrize("rotation", [90, 180, 270])
async def test_stamp_on_rotated_page_lands_upright_in_visible_frame(sclient, upload_dir, rotation):
    doc_id = _make_doc(upload_dir, rotation=rotation)
    rect = [100, 100, 300, 200]  # visible frame (what the user sees)
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/stamp", json={"page": 0, "rect": rect, "image": _png_b64(200, 100)})
    assert r.status_code == 200, r.text
    doc = _open(upload_dir, doc_id)
    bb = _dark_bbox_region(doc[0], rect)
    # bar (top 30% of the image) must appear at the TOP of the visible rect, full width
    assert abs(bb[0] - 100) <= 2 and abs(bb[2] - 300) <= 2
    assert abs(bb[1] - 100) <= 2 and bb[3] <= 100 + 30 + 2
    doc.close()


async def test_apply_text_date_check_cross_are_real_pdf_content(sclient, upload_dir):
    doc_id = _make_doc(upload_dir)
    items = [
        {"type": "text", "page": 0, "rect": [72, 400, 300, 420], "text": "Jane Q. Public", "font_size": 14},
        {"type": "date", "page": 0, "rect": [72, 430, 200, 450], "text": "10/01/2026"},
        {"type": "check", "page": 1, "rect": [100, 100, 120, 120], "color": "#0000ff"},
        {"type": "cross", "page": 1, "rect": [150, 100, 170, 120]},
    ]
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/apply", json={"items": items})
    assert r.status_code == 200, r.text
    doc = _open(upload_dir, doc_id)
    words = doc[0].get_text("words")
    jane = [w for w in words if w[4] == "Jane"]
    assert jane, "typed text must be real, searchable page text"
    assert 72 <= jane[0][0] < 80 and 400 <= jane[0][1] and jane[0][3] <= 422
    assert any(w[4] == "10/01/2026" for w in words)
    drawings = doc[1].get_drawings()
    assert len(drawings) >= 2
    blue = [dr for dr in drawings if dr.get("color") and dr["color"][2] > 0.9 and dr["color"][0] < 0.1]
    assert blue and fitz.Rect(100, 100, 120, 120).contains(blue[0]["rect"])
    cross = [dr for dr in drawings if dr not in blue]
    assert cross and fitz.Rect(150, 100, 170, 120).contains(cross[0]["rect"])
    doc.close()


async def test_date_defaults_to_today(sclient, upload_dir):
    from datetime import datetime

    doc_id = _make_doc(upload_dir)
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/apply",
                           json={"items": [{"type": "date", "page": 0, "rect": [72, 430, 200, 450]}]})
    assert r.status_code == 200
    doc = _open(upload_dir, doc_id)
    assert datetime.now().strftime("%m/%d/%Y") in doc[0].get_text()
    doc.close()


async def test_lock_flattens_form_fields_and_annotations(sclient, upload_dir):
    doc_id = _make_doc(upload_dir, with_form=True, with_annot=True)
    doc = _open(upload_dir, doc_id)
    assert len(list(doc[0].widgets())) == 1 and len(list(doc[0].annots())) == 1
    doc.close()

    r = await sclient.post(f"/api/pdf/{doc_id}/sign/stamp",
                           json={"page": 0, "rect": [100, 600, 250, 650], "image": _png_b64(), "lock": True})
    assert r.status_code == 200, r.text
    assert r.json()["lock"] == {"annotations_flattened": 1, "fields_flattened": 1}
    doc = _open(upload_dir, doc_id)
    assert list(doc[0].widgets()) == [] and list(doc[0].annots()) == []
    text = doc[0].get_text()
    assert "ACME Holdings" in text, "field value must be burned into page content"
    assert "Reviewed by legal" in text, "annotation must be burned into page content"
    assert not doc.is_form_pdf
    doc.close()


async def test_lock_endpoint_alone(sclient, upload_dir):
    doc_id = _make_doc(upload_dir, with_form=True)
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/lock")
    assert r.status_code == 200 and r.json()["fields_flattened"] == 1
    doc = _open(upload_dir, doc_id)
    assert list(doc[0].widgets()) == [] and "ACME Holdings" in doc[0].get_text()
    doc.close()


async def test_undo_restores_pre_signature_state(sclient, upload_dir):
    doc_id = _make_doc(upload_dir)
    await sclient.post(f"/api/pdf/{doc_id}/sign/stamp", json={"page": 0, "rect": [100, 500, 250, 550], "image": _png_b64()})
    doc = _open(upload_dir, doc_id)
    assert len(doc[0].get_images()) == 1
    doc.close()
    r = await sclient.post(f"/api/pdf/{doc_id}/undo")
    assert r.status_code == 200, r.text
    doc = _open(upload_dir, doc_id)
    assert doc[0].get_images() == []
    doc.close()


@pytest.mark.parametrize(
    "body,status",
    [
        ({"items": [{"type": "image", "page": 5, "rect": [0, 0, 10, 10], "image": "AAAA"}]}, 400),
        ({"items": [{"type": "image", "page": 0, "rect": [10, 10, 100, 60], "image": "not-an-image!!"}]}, 400),
        ({"items": [{"type": "image", "page": 0, "rect": [5000, 5000, 5100, 5100], "image": "AAAA"}]}, 400),
        ({"items": [{"type": "text", "page": 0, "rect": [10, 10, 100, 30], "text": "   "}]}, 400),
        ({"items": [{"type": "text", "page": 0, "rect": [10, 10, 100, 30], "text": "x", "color": "red"}]}, 400),
        ({"items": []}, 422),
    ],
)
async def test_apply_rejects_bad_input_without_modifying(sclient, upload_dir, body, status):
    doc_id = _make_doc(upload_dir)
    before = (upload_dir / doc_id / "original.pdf").read_bytes()
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/apply", json=body)
    assert r.status_code == status, r.text
    assert (upload_dir / doc_id / "original.pdf").read_bytes() == before


async def test_bad_doc_id_rejected(sclient, upload_dir):
    r = await sclient.post("/api/pdf/..%2F..%2Fetc/sign/lock")
    assert r.status_code in (400, 404)
    r = await sclient.post("/api/pdf/not-a-uuid/sign/lock")
    assert r.status_code == 400


# ─── Certificates ────────────────────────────────────────────────────────────


async def test_certificate_generation_and_storage(sclient, upload_dir):
    info = await _new_cert(sclient, organization="Acme")
    d = upload_dir / "_certs" / info["cert_id"]
    assert (d / "key.pem").exists() and (d / "cert.pem").exists()
    assert oct(os.stat(d / "key.pem").st_mode & 0o777) == "0o600"
    from cryptography import x509

    cert = x509.load_pem_x509_certificate((d / "cert.pem").read_bytes())
    assert cert.subject.rfc4514_string().find("CN=Jane Signer") >= 0
    assert cert.issuer == cert.subject  # self-signed
    r = await sclient.get(f"/api/pdf/signing/certificates/{info['cert_id']}")
    assert r.json()["name"] == "Jane Signer" and r.json()["organization"] == "Acme"
    r = await sclient.get(f"/api/pdf/signing/certificates/{info['cert_id']}/download")
    assert r.status_code == 200 and r.content.startswith(b"-----BEGIN CERTIFICATE-----")
    r = await sclient.delete(f"/api/pdf/signing/certificates/{info['cert_id']}")
    assert r.status_code == 200 and not d.exists()


# ─── Digital signatures ──────────────────────────────────────────────────────


async def test_digital_sign_then_validate_intact(sclient, upload_dir):
    doc_id = _make_doc(upload_dir)
    cert = await _new_cert(sclient)
    rect = [72, 600, 272, 680]
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/digital", json={
        "cert_id": cert["cert_id"], "page": 1, "rect": rect, "image": _png_b64(),
        "reason": "I approve this agreement", "location": "Charlotte, NC",
    })
    assert r.status_code == 200, r.text
    assert r.json()["field_name"] == "Signature1"

    # Field exists on the requested page at the requested (top-left origin) rect
    doc = _open(upload_dir, doc_id)
    widgets = list(doc[1].widgets())
    assert len(widgets) == 1 and widgets[0].field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE
    assert widgets[0].is_signed
    for a, b in zip(widgets[0].rect, rect):
        assert abs(a - b) < 0.5
    doc.close()
    raw = (upload_dir / doc_id / "original.pdf").read_bytes()
    assert b"/ByteRange" in raw and (b"/adbe.pkcs7.detached" in raw or b"/ETSI.CAdES.detached" in raw)

    v = (await sclient.get(f"/api/pdf/{doc_id}/sign/validate")).json()
    assert v["signature_count"] == 1
    s = v["signatures"][0]
    assert s["signer_name"] == "Jane Signer" and s["signer_email"] == "jane@example.com"
    assert s["intact"] is True and s["valid"] is True
    assert s["issued_by_this_app"] is True and s["self_signed"] is True
    assert s["coverage"] == "entire_file" and s["modified_after_signing"] is False
    assert s["reason"] == "I approve this agreement" and s["location"] == "Charlotte, NC"
    assert s["signing_time"] and s["summary"].startswith("Valid")
    assert _history_len(upload_dir, doc_id) == 1


async def test_tampering_after_signature_is_detected(sclient, upload_dir):
    doc_id = _make_doc(upload_dir)
    cert = await _new_cert(sclient)
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/digital",
                           json={"cert_id": cert["cert_id"], "page": 0, "rect": [72, 600, 272, 680]})
    assert r.status_code == 200, r.text
    path = upload_dir / doc_id / "original.pdf"
    raw = bytearray(path.read_bytes())
    # Flip a byte inside the signed page content ("Agreement" -> "Bgreement")
    i = raw.find(b"Agreement")
    if i < 0:  # content stream is compressed: tamper by appending text through a full rewrite instead
        doc = fitz.open(str(path))
        doc[0].insert_text((72, 300), "Pay $1,000,000 instead", fontsize=12)
        tmp = str(path) + ".x"
        doc.save(tmp)
        doc.close()
        os.replace(tmp, path)
    else:
        raw[i] = ord("B")
        path.write_bytes(bytes(raw))
    v = (await sclient.get(f"/api/pdf/{doc_id}/sign/validate")).json()
    s = v["signatures"][0]
    assert s["intact"] is False or s["modified_after_signing"] is True
    assert s["summary"].startswith("INVALID") or "changed" in s["summary"]


async def test_incremental_change_after_signature_reported_as_modified(sclient, upload_dir):
    doc_id = _make_doc(upload_dir)
    cert = await _new_cert(sclient)
    await sclient.post(f"/api/pdf/{doc_id}/sign/digital",
                       json={"cert_id": cert["cert_id"], "page": 0, "rect": [72, 600, 272, 680]})
    path = upload_dir / doc_id / "original.pdf"
    doc = fitz.open(str(path))
    doc[0].insert_text((72, 300), "Added after signing", fontsize=12)
    doc.save(str(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    doc.close()
    s = (await sclient.get(f"/api/pdf/{doc_id}/sign/validate")).json()["signatures"][0]
    assert s["intact"] is True  # the signed revision itself is untouched...
    assert s["coverage"] != "entire_file"  # ...but more bytes were appended
    assert s["modified_after_signing"] is True
    assert "changed" in s["summary"] or s["summary"].startswith("INVALID")


async def test_visual_edits_refused_on_digitally_signed_doc(sclient, upload_dir):
    doc_id = _make_doc(upload_dir)
    cert = await _new_cert(sclient)
    await sclient.post(f"/api/pdf/{doc_id}/sign/digital",
                       json={"cert_id": cert["cert_id"], "page": 0, "rect": [72, 600, 272, 680]})
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/apply",
                           json={"items": [{"type": "check", "page": 0, "rect": [10, 10, 30, 30]}]})
    assert r.status_code == 409
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/lock")
    assert r.status_code == 409


async def test_second_signer_keeps_first_signature_valid(sclient, upload_dir):
    doc_id = _make_doc(upload_dir)
    a = await _new_cert(sclient, name="Alice")
    b = await _new_cert(sclient, name="Bob", email="bob@example.com")
    r1 = await sclient.post(f"/api/pdf/{doc_id}/sign/digital",
                            json={"cert_id": a["cert_id"], "page": 0, "rect": [72, 600, 272, 680]})
    r2 = await sclient.post(f"/api/pdf/{doc_id}/sign/digital",
                            json={"cert_id": b["cert_id"], "page": 0, "rect": [300, 600, 500, 680]})
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert r2.json()["field_name"] == "Signature2"
    sigs = (await sclient.get(f"/api/pdf/{doc_id}/sign/validate")).json()["signatures"]
    assert [s["signer_name"] for s in sigs] == ["Alice", "Bob"]
    assert all(s["intact"] and s["valid"] for s in sigs)
    assert sigs[1]["coverage"] == "entire_file"
    assert sigs[0]["coverage"] == "entire_revision"  # Bob's revision came later; allowed


async def test_certify_lock_flags_later_changes_invalid(sclient, upload_dir):
    doc_id = _make_doc(upload_dir)
    cert = await _new_cert(sclient)
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/digital",
                           json={"cert_id": cert["cert_id"], "page": 0, "rect": [72, 600, 272, 680], "lock": True})
    assert r.status_code == 200 and r.json()["certified"] is True
    s = (await sclient.get(f"/api/pdf/{doc_id}/sign/validate")).json()["signatures"][0]
    assert s["certified"] is True and s["summary"].startswith("Valid")
    path = upload_dir / doc_id / "original.pdf"
    doc = fitz.open(str(path))
    doc[0].insert_text((72, 300), "sneaky", fontsize=12)
    doc.save(str(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    doc.close()
    s = (await sclient.get(f"/api/pdf/{doc_id}/sign/validate")).json()["signatures"][0]
    assert s["modified_after_signing"] is True
    assert s["docmdp_ok"] is False and s["summary"].startswith("INVALID")


async def test_passphrase_protected_certificate(sclient, upload_dir):
    doc_id = _make_doc(upload_dir)
    cert = await _new_cert(sclient, passphrase="correct horse")
    key_pem = (upload_dir / "_certs" / cert["cert_id"] / "key.pem").read_bytes()
    assert b"ENCRYPTED" in key_pem
    body = {"cert_id": cert["cert_id"], "page": 0, "rect": [72, 600, 272, 680]}
    assert (await sclient.post(f"/api/pdf/{doc_id}/sign/digital", json=body)).status_code == 400
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/digital", json={**body, "passphrase": "wrong"})
    assert r.status_code == 403
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/digital", json={**body, "passphrase": "correct horse"})
    assert r.status_code == 200, r.text


async def test_sign_existing_empty_field_and_upload_validation(sclient, upload_dir):
    # Build a PDF with an empty signature field, as a sender would
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields

    doc_id = _make_doc(upload_dir)
    path = upload_dir / doc_id / "original.pdf"
    w = IncrementalPdfFileWriter(io.BytesIO(path.read_bytes()))
    fields.append_signature_field(w, fields.SigFieldSpec("ClientSignature", on_page=0, box=(72, 72, 272, 132)))
    out = io.BytesIO()
    w.write(out)
    path.write_bytes(out.getvalue())

    up = await sclient.post("/api/pdf/signing/validate",
                            files={"file": ("x.pdf", out.getvalue(), "application/pdf")})
    assert up.status_code == 200, up.text
    assert up.json()["signature_count"] == 0
    empty = up.json()["empty_signature_fields"]
    assert empty and empty[0]["field_name"] == "ClientSignature" and empty[0]["page"] == 0
    # 792 - 132 = 660 → top-left origin rect
    assert [round(v) for v in empty[0]["rect"]] == [72, 660, 272, 720]

    cert = await _new_cert(sclient)
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/digital",
                           json={"cert_id": cert["cert_id"], "field_name": "ClientSignature", "image": _png_b64()})
    assert r.status_code == 200, r.text
    signed = path.read_bytes()
    up = await sclient.post("/api/pdf/signing/validate",
                            files={"file": ("signed.pdf", signed, "application/pdf")})
    body = up.json()
    assert body["signature_count"] == 1 and body["empty_signature_fields"] == []
    assert body["signatures"][0]["field_name"] == "ClientSignature" and body["signatures"][0]["valid"]
    # signing the same field twice is refused
    r = await sclient.post(f"/api/pdf/{doc_id}/sign/digital",
                           json={"cert_id": cert["cert_id"], "field_name": "ClientSignature"})
    assert r.status_code == 409


async def test_validate_upload_rejects_non_pdf(sclient, upload_dir):
    r = await sclient.post("/api/pdf/signing/validate", files={"file": ("x.pdf", b"hello", "application/pdf")})
    assert r.status_code == 400
