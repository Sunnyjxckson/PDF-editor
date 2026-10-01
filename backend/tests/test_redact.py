"""Tests for backend/features/redact.py — run against real generated PDFs.

The router is mounted on a private FastAPI app (plus advanced_ops for undo),
with advanced_ops.UPLOAD_DIR pointed at a temp dir.
"""

import io
import re
import uuid
from pathlib import Path

import fitz
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend import advanced_ops
from backend.features.redact import router as redact_router

SECRET = "123-45-6789"


# ─── fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(advanced_ops, "UPLOAD_DIR", tmp_path)
    return tmp_path


@pytest_asyncio.fixture
async def rc(upload_dir):
    app = FastAPI()
    app.include_router(redact_router)
    app.include_router(advanced_ops.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def store(upload_dir: Path, doc: fitz.Document | bytes) -> str:
    doc_id = str(uuid.uuid4())
    d = upload_dir / doc_id
    d.mkdir()
    data = doc if isinstance(doc, bytes) else doc.tobytes()
    (d / "original.pdf").write_bytes(data)
    return doc_id


def path_of(upload_dir: Path, doc_id: str) -> Path:
    return upload_dir / doc_id / "original.pdf"


def reopen(upload_dir, doc_id) -> fitz.Document:
    return fitz.open(str(path_of(upload_dir, doc_id)))


def all_object_bytes(doc: fitz.Document) -> bytes:
    """Every object dictionary and every decompressed stream in the file."""
    out = []
    for x in range(1, doc.xref_length()):
        try:
            out.append(doc.xref_object(x, compressed=False).encode("latin-1", "replace"))
        except Exception:
            pass
        try:
            s = doc.xref_stream(x)
            if s:
                out.append(s)
        except Exception:
            pass
    return b"\n".join(out)


def secret_in_raw(doc: fitz.Document, raw_file: bytes, secret: str) -> bool:
    blob = all_object_bytes(doc) + raw_file
    hex_up = secret.encode().hex().upper().encode()
    hex_lo = secret.encode().hex().encode()
    # Base-14 text written by insert_text is hex-encoded 2-byte? check both widths.
    hex2 = "".join(f"00{ord(c):02x}" for c in secret).encode()
    return any(n in blob for n in (secret.encode(), hex_up, hex_lo, hex2, hex2.upper()))


def sample_doc() -> fitz.Document:
    doc = fitz.open()
    p = doc.new_page(width=612, height=792)
    p.insert_text((72, 100), "Name: John Smith", fontsize=12)
    p.insert_text((72, 130), f"SSN {SECRET} on file", fontsize=12)
    p.insert_text((72, 160), "Public paragraph stays.", fontsize=12)
    p2 = doc.new_page(width=612, height=792)
    p2.insert_text((72, 100), "Contact jane.doe@example.com or (704) 555-1234", fontsize=12)
    return doc


# ─── true redaction ──────────────────────────────────────────────────────────


async def test_apply_removes_text_from_extraction_and_raw_content(rc, upload_dir):
    src = sample_doc()
    # Precondition: the secret is really in the raw content before redaction,
    # so the "absent afterwards" assertion below is meaningful.
    raw_before = src.tobytes(deflate=False)
    assert secret_in_raw(src, raw_before, SECRET)
    rect = list(src[0].search_for(SECRET)[0])
    doc_id = store(upload_dir, src)

    r = await rc.post(f"/api/pdf/{doc_id}/redact/mark", json={"areas": [{"page": 0, "rect": rect}]})
    assert r.status_code == 200, r.text
    # Marked but not applied: text still present.
    d = reopen(upload_dir, doc_id)
    assert SECRET in d[0].get_text()
    d.close()

    r = await rc.post(f"/api/pdf/{doc_id}/redact/apply", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == 1 and body["verified"] is True and body["removed_chars"] >= 9

    raw = path_of(upload_dir, doc_id).read_bytes()
    d = reopen(upload_dir, doc_id)
    text = d[0].get_text()
    assert SECRET not in text
    assert "6789" not in text
    assert "Name: John Smith" in text and "Public paragraph stays." in text
    assert not secret_in_raw(d, raw, SECRET)
    assert not secret_in_raw(d, d.tobytes(deflate=False, expand=255), SECRET)
    # No pending redact annots remain; area is filled black.
    assert list(d[0].annots(types=[fitz.PDF_ANNOT_REDACT])) == []
    r_ = fitz.Rect(rect)
    pix = d[0].get_pixmap(clip=r_)
    cx, cy = pix.width // 2, pix.height // 2
    assert pix.pixel(cx, cy)[:3] == (0, 0, 0)
    d.close()


async def test_custom_fill_and_overlay_text(rc, upload_dir):
    src = sample_doc()
    rect = list(src[0].search_for(SECRET)[0] + (-2, -2, 60, 2))
    doc_id = store(upload_dir, src)
    r = await rc.post(
        f"/api/pdf/{doc_id}/redact/apply",
        json={"areas": [{"page": 0, "rect": rect}], "fill_color": "#ff0000",
              "overlay_text": "REDACTED", "overlay_font_size": 8},
    )
    assert r.status_code == 200, r.text
    d = reopen(upload_dir, doc_id)
    text = d[0].get_text()
    assert SECRET not in text
    assert "REDACTED" in text
    pix = d[0].get_pixmap(clip=fitz.Rect(rect))
    # top-left corner region is fill, not overlay glyphs
    assert pix.pixel(1, 1)[:3] == (255, 0, 0)
    d.close()


async def test_redaction_removes_image_pixels_and_vector_art(rc, upload_dir):
    doc = fitz.open()
    page = doc.new_page(width=400, height=400)
    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), False)
    pm.set_rect(pm.irect, (0, 0, 255))
    page.insert_image(fitz.Rect(50, 50, 150, 150), pixmap=pm)
    page.draw_rect(fitz.Rect(220, 220, 260, 260), color=(1, 0, 0), fill=(0, 1, 0))
    page.draw_rect(fitz.Rect(300, 50, 350, 100), color=(0, 0, 1), fill=(0, 0, 1))  # outside
    doc_id = store(upload_dir, doc)
    assert len(page.get_drawings()) == 2

    r = await rc.post(
        f"/api/pdf/{doc_id}/redact/apply",
        json={"areas": [{"page": 0, "rect": [40, 40, 100, 100]}, {"page": 0, "rect": [210, 210, 270, 270]}],
              "fill_color": [1, 1, 1], "graphics": "covered", "images": "pixels"},
    )
    assert r.status_code == 200, r.text
    d = reopen(upload_dir, doc_id)
    pg = d[0]
    # Vector art fully covered by the area is gone; the other shape remains.
    # (Redaction fills are themselves drawn as white rects, so ignore those.)
    drawings = [dr for dr in pg.get_drawings() if dr.get("fill") != (1.0, 1.0, 1.0)]
    assert len(drawings) == 1
    assert fitz.Rect(drawings[0]["rect"]).intersects(fitz.Rect(300, 50, 350, 100))
    assert not any(dr.get("fill") == (0.0, 1.0, 0.0) for dr in pg.get_drawings())
    # Image pixels under the area are blanked in the image data itself.
    imgs = pg.get_images(full=True)
    assert len(imgs) == 1
    ipix = fitz.Pixmap(d, imgs[0][0])
    if ipix.n > 3:
        ipix = fitz.Pixmap(fitz.csRGB, ipix)
    # image spans 50..150 pts over 100 px → area 50..100pt = px 0..50
    assert ipix.pixel(10, 10)[:3] != (0, 0, 255)
    assert ipix.pixel(90, 90)[:3] == (0, 0, 255)  # outside area untouched
    d.close()


async def test_image_remove_mode_deletes_image(rc, upload_dir):
    doc = fitz.open()
    page = doc.new_page(width=400, height=400)
    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
    pm.set_rect(pm.irect, (0, 200, 0))
    page.insert_image(fitz.Rect(50, 50, 150, 150), pixmap=pm)
    doc_id = store(upload_dir, doc)
    r = await rc.post(f"/api/pdf/{doc_id}/redact/apply",
                      json={"areas": [{"page": 0, "rect": [60, 60, 70, 70]}], "images": "remove"})
    assert r.status_code == 200
    d = reopen(upload_dir, doc_id)
    assert d[0].get_images() == []
    d.close()


async def test_marks_lifecycle(rc, upload_dir):
    doc_id = store(upload_dir, sample_doc())
    r = await rc.post(f"/api/pdf/{doc_id}/redact/mark", json={
        "areas": [{"page": 0, "rect": [70, 85, 200, 105]}, {"page": 1, "rect": [70, 85, 200, 105]}],
        "overlay_text": "PII", "label": "manual", "fill_color": "#112233",
    })
    assert r.status_code == 200, r.text
    marks = (await rc.get(f"/api/pdf/{doc_id}/redact/marks")).json()["marks"]
    assert len(marks) == 2
    assert marks[0]["label"] == "manual"
    assert marks[0]["overlay_text"] == "PII"
    assert marks[0]["fill_color"] == "#112233"
    assert marks[0]["rect"] == pytest.approx([70, 85, 200, 105], abs=0.5)

    r = await rc.delete(f"/api/pdf/{doc_id}/redact/marks/{marks[0]['page']}/{marks[0]['xref']}")
    assert r.status_code == 200
    assert (await rc.get(f"/api/pdf/{doc_id}/redact/marks")).json()["count"] == 1
    assert (await rc.delete(f"/api/pdf/{doc_id}/redact/marks/0/99999")).status_code == 404
    r = await rc.delete(f"/api/pdf/{doc_id}/redact/marks")
    assert r.json()["removed"] == 1
    d = reopen(upload_dir, doc_id)
    assert "John Smith" in d[0].get_text()  # nothing was burned in
    d.close()
    assert (await rc.post(f"/api/pdf/{doc_id}/redact/apply", json={})).status_code == 400


async def test_apply_restricted_to_pages(rc, upload_dir):
    doc_id = store(upload_dir, sample_doc())
    await rc.post(f"/api/pdf/{doc_id}/redact/mark", json={
        "areas": [{"page": 0, "rect": [70, 85, 300, 105]}, {"page": 1, "rect": [70, 85, 300, 105]}]})
    r = await rc.post(f"/api/pdf/{doc_id}/redact/apply", json={"pages": [1]})
    assert r.json()["pages_affected"] == [1]
    d = reopen(upload_dir, doc_id)
    assert "John Smith" in d[0].get_text()
    assert "jane.doe" not in d[1].get_text()
    assert len(list(d[0].annots(types=[fitz.PDF_ANNOT_REDACT]))) == 1  # still pending
    d.close()


async def test_rotated_page_uses_visible_coordinates(rc, upload_dir):
    src = sample_doc()
    src[0].set_rotation(90)
    doc_id = store(upload_dir, src)
    words = (await rc.get(f"/api/pdf/{doc_id}/redact/words/0")).json()
    assert words["width"] == pytest.approx(792) and words["height"] == pytest.approx(612)
    w = next(w for w in words["words"] if w["text"] == SECRET)
    # Visible rect of horizontal text on a 90°-rotated page is taller than wide.
    x0, y0, x1, y1 = w["rect"]
    assert (y1 - y0) > (x1 - x0)
    r = await rc.post(f"/api/pdf/{doc_id}/redact/apply", json={"areas": [{"page": 0, "rect": w["rect"]}]})
    assert r.status_code == 200 and r.json()["verified"]
    d = reopen(upload_dir, doc_id)
    t = d[0].get_text()
    assert SECRET not in t and "SSN" in t and "on file" in t
    d.close()


async def test_undo_restores_after_apply(rc, upload_dir):
    doc_id = store(upload_dir, sample_doc())
    await rc.post(f"/api/pdf/{doc_id}/redact/apply", json={"areas": [{"page": 0, "rect": [60, 115, 300, 135]}]})
    d = reopen(upload_dir, doc_id)
    assert SECRET not in d[0].get_text()
    d.close()
    r = await rc.post(f"/api/pdf/{doc_id}/undo")
    assert r.status_code == 200, r.text
    d = reopen(upload_dir, doc_id)
    assert SECRET in d[0].get_text()
    d.close()


# ─── search & redact ─────────────────────────────────────────────────────────


def pii_doc() -> fitz.Document:
    doc = fitz.open()
    p = doc.new_page(width=612, height=792)
    lines = [
        "SSN valid 123-45-6789 and invalid 000-12-3456",
        "Card good 4111 1111 1111 1111 bad 4111 1111 1111 1112",
        "Call (704) 555-1234 or 704.555.9876 today",
        "Mail bob@corp.io now",
        "Due 03/15/2024 or March 5, 2025 or 2024-01-31",
        "Total $1,234.56 and 500 USD",
        "Ship to 1600 Pennsylvania Avenue, Washington, DC 20500",
        "The word cat and category and CAT.",
    ]
    for i, line in enumerate(lines):
        p.insert_text((40, 60 + i * 30), line, fontsize=11)
    p2 = doc.new_page()
    p2.insert_text((40, 60), "second page cat 222-33-4444", fontsize=11)
    return doc


async def _search(rc, doc_id, **kw):
    r = await rc.post(f"/api/pdf/{doc_id}/redact/search", json=kw)
    assert r.status_code == 200, r.text
    return r.json()["matches"]


async def test_search_presets(rc, upload_dir):
    doc_id = store(upload_dir, pii_doc())
    texts = lambda ms: [m["text"] for m in ms]  # noqa: E731

    ssn = texts(await _search(rc, doc_id, presets=["ssn"]))
    assert "123-45-6789" in ssn and "222-33-4444" in ssn and "000-12-3456" not in ssn

    cc = texts(await _search(rc, doc_id, presets=["credit_card"]))
    assert cc == ["4111 1111 1111 1111"]

    ph = texts(await _search(rc, doc_id, presets=["phone"]))
    assert "(704) 555-1234" in ph and "704.555.9876" in ph
    assert not any("6789" in p for p in ph)

    assert texts(await _search(rc, doc_id, presets=["email"])) == ["bob@corp.io"]

    dates = texts(await _search(rc, doc_id, presets=["date"]))
    assert {"03/15/2024", "March 5, 2025", "2024-01-31"} <= set(dates)

    money = texts(await _search(rc, doc_id, presets=["money"]))
    assert "$1,234.56" in money and "500 USD" in money

    addr = texts(await _search(rc, doc_id, presets=["address"]))
    assert any(a.startswith("1600 Pennsylvania Avenue") and "DC 20500" in a for a in addr)

    presets = (await rc.get("/api/pdf/redact/presets")).json()["presets"]
    assert {p["id"] for p in presets} == {"ssn", "phone", "email", "credit_card", "date", "money", "address"}


async def test_search_text_regex_wholeword_and_rects(rc, upload_dir):
    src = pii_doc()
    expected = src[0].search_for("bob@corp.io")[0]
    doc_id = store(upload_dir, src)

    ms = await _search(rc, doc_id, query="cat")
    assert len(ms) == 4  # cat, CAT (case-insensitive), category, page-2 cat
    ms = await _search(rc, doc_id, query="cat", whole_word=True)
    assert len(ms) == 3 and {m["page"] for m in ms} == {0, 1}
    ms = await _search(rc, doc_id, query="cat", whole_word=True, case_sensitive=True)
    assert len(ms) == 2
    ms = await _search(rc, doc_id, query=r"bob@\w+\.io", mode="regex")
    assert len(ms) == 1
    assert ms[0]["rects"][0] == pytest.approx(list(expected), abs=1.0)
    assert "Mail" in ms[0]["context"]
    ms = await _search(rc, doc_id, query="cat", pages=[1])
    assert [m["page"] for m in ms] == [1]

    r = await rc.post(f"/api/pdf/{doc_id}/redact/search", json={"query": "(", "mode": "regex"})
    assert r.status_code == 400
    r = await rc.post(f"/api/pdf/{doc_id}/redact/search", json={"presets": ["nope"]})
    assert r.status_code == 400
    r = await rc.post(f"/api/pdf/{doc_id}/redact/search", json={})
    assert r.status_code == 400


async def test_search_then_apply_selected(rc, upload_dir):
    doc_id = store(upload_dir, pii_doc())
    ms = await _search(rc, doc_id, presets=["ssn", "email"])
    selected = [m for m in ms if m["text"] != "222-33-4444"]  # user unticks one
    areas = [{"page": m["page"], "rect": r} for m in selected for r in m["rects"]]
    r = await rc.post(f"/api/pdf/{doc_id}/redact/mark", json={"areas": areas, "overlay_text": "X"})
    assert r.status_code == 200
    r = await rc.post(f"/api/pdf/{doc_id}/redact/apply", json={})
    assert r.json()["verified"]
    d = reopen(upload_dir, doc_id)
    t0, t1 = d[0].get_text(), d[1].get_text()
    assert "123-45-6789" not in t0 and "bob@corp.io" not in t0
    assert "000-12-3456" in t0  # not selected (invalid SSN, never matched)
    assert "222-33-4444" in t1  # unticked by the user
    assert "Mail" in t0 and "now" in t0
    d.close()


# ─── sanitize ────────────────────────────────────────────────────────────────


def dirty_doc() -> fitz.Document:
    doc = fitz.open()
    p = doc.new_page(width=612, height=792)
    p.insert_text((72, 72), "Visible body text", fontsize=12)
    p.insert_text((72, 120), "INVISIBLESECRET", fontsize=12, render_mode=3)
    p.insert_text((72, 160), "WHITESECRET", fontsize=12, color=(1, 1, 1))
    p.add_text_annot((400, 400), "reviewer comment: lowball them")
    p.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 60, 200, 80), "uri": "https://example.com"})
    w = fitz.Widget()
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.field_name = "ssn_field"
    w.field_value = "987-65-4321"
    w.rect = fitz.Rect(72, 300, 250, 320)
    p.add_widget(w)
    p.add_redact_annot(fitz.Rect(72, 200, 150, 220), text="KEEP", fill=(1, 0, 0))  # pending user mark
    doc.set_metadata({"author": "Jane Insider", "title": "Secret Plan", "subject": "M&A"})
    doc.set_xml_metadata('<x:xmpmeta xmlns:x="adobe:ns:meta/">XMPSECRET</x:xmpmeta>')
    js = doc.get_new_xref()
    doc.update_object(js, "<</S/JavaScript/JS(app.alert('pwned'))>>")
    cat = doc.pdf_catalog()
    doc.xref_set_key(cat, "Names", f"<</JavaScript <</Names [(a) {js} 0 R]>> >>")
    doc.embfile_add("payload.txt", b"attached secret")  # joins the same /Names dict
    doc.xref_set_key(cat, "OpenAction", f"{js} 0 R")
    return doc


async def test_sanitize_removes_hidden_information(rc, upload_dir):
    doc_id = store(upload_dir, dirty_doc())
    audit = (await rc.get(f"/api/pdf/{doc_id}/security/audit")).json()
    assert audit["metadata"]["author"] == "Jane Insider"
    assert audit["has_xmp_metadata"]
    assert audit["embedded_files"] == ["payload.txt"]
    assert audit["javascript_objects"] >= 1
    assert audit["annotation_count"] == 1 and audit["pending_redactions"] == 1
    assert audit["links"] == 1
    assert audit["form_fields"] == 1 and audit["form_fields_filled"] == 1
    reasons = {(h["reason"], h["text"]) for h in audit["hidden_text"]}
    assert ("invisible", "INVISIBLESECRET") in reasons and ("white", "WHITESECRET") in reasons

    r = await rc.post(f"/api/pdf/{doc_id}/security/sanitize", json={})
    assert r.status_code == 200, r.text
    after = r.json()["after"]
    assert after["metadata"] == {}
    assert not after["has_xmp_metadata"]
    assert after["embedded_files"] == []
    assert after["javascript_objects"] == 0
    assert after["annotation_count"] == 0
    assert after["pending_redactions"] == 1  # user's marks are preserved
    marks = (await rc.get(f"/api/pdf/{doc_id}/redact/marks")).json()["marks"]
    assert marks[0]["overlay_text"] == "KEEP" and marks[0]["fill_color"] == "#ff0000"
    assert marks[0]["rect"] == pytest.approx([72, 200, 150, 220], abs=0.5)
    assert after["links"] == 0
    assert after["form_fields_filled"] == 0
    assert after["hidden_text_count"] == 0

    raw = path_of(upload_dir, doc_id).read_bytes()
    d = reopen(upload_dir, doc_id)
    text = d[0].get_text()
    assert "Visible body text" in text
    assert "INVISIBLESECRET" not in text and "WHITESECRET" not in text
    assert d.embfile_count() == 0
    blob = all_object_bytes(d) + raw
    for s in (b"INVISIBLESECRET", b"WHITESECRET", b"XMPSECRET", b"Jane Insider", b"attached secret", b"pwned", b"lowball"):
        assert s not in blob, s
    d.close()


async def test_sanitize_respects_options(rc, upload_dir):
    doc_id = store(upload_dir, dirty_doc())
    r = await rc.post(f"/api/pdf/{doc_id}/security/sanitize", json={
        "metadata": False, "xmp_metadata": False, "embedded_files": False, "javascript": False,
        "hidden_text": True, "white_text": False, "annotations": False, "form_data": False,
        "links": False, "thumbnails": False,
    })
    after = r.json()["after"]
    assert after["metadata"]["author"] == "Jane Insider"
    assert after["embedded_files"] == ["payload.txt"]
    assert after["annotation_count"] == 1 and after["links"] == 1
    d = reopen(upload_dir, doc_id)
    t = d[0].get_text()
    assert "INVISIBLESECRET" not in t and "WHITESECRET" in t
    d.close()


# ─── protect / unlock ────────────────────────────────────────────────────────


async def test_protect_download_aes256(rc, upload_dir):
    doc_id = store(upload_dir, sample_doc())
    r = await rc.post(f"/api/pdf/{doc_id}/security/protect", json={
        "user_password": "openme", "owner_password": "boss", "permissions": ["print"]})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    enc = fitz.open("pdf", r.content)
    assert enc.needs_pass
    assert enc.authenticate("wrong") == 0
    assert enc.authenticate("openme")
    assert "AES" in enc.metadata["encryption"] and "256" in enc.metadata["encryption"]
    perms = enc.permissions
    assert perms & fitz.PDF_PERM_PRINT
    assert not perms & fitz.PDF_PERM_COPY
    assert not perms & fitz.PDF_PERM_MODIFY
    assert "John Smith" in enc[0].get_text()
    # Working copy untouched.
    d = reopen(upload_dir, doc_id)
    assert not d.needs_pass and not d.metadata.get("encryption")
    d.close()


async def test_protect_working_copy_and_unlock(rc, upload_dir):
    doc_id = store(upload_dir, sample_doc())
    r = await rc.post(f"/api/pdf/{doc_id}/security/protect", json={
        "user_password": "u", "owner_password": "o", "apply_to_document": True})
    assert r.status_code == 400  # would make the editor unable to render
    r = await rc.post(f"/api/pdf/{doc_id}/security/protect", json={"owner_password": ""})
    assert r.status_code == 400
    r = await rc.post(f"/api/pdf/{doc_id}/security/protect", json={
        "owner_password": "boss", "permissions": ["print"], "apply_to_document": True})
    assert r.status_code == 200, r.text
    d = reopen(upload_dir, doc_id)
    assert not d.needs_pass and "AES" in d.metadata["encryption"]
    assert not d.permissions & fitz.PDF_PERM_COPY
    d.close()
    assert (await rc.get(f"/api/pdf/{doc_id}/security/audit")).json()["encrypted"] is True

    assert (await rc.post(f"/api/pdf/{doc_id}/security/unlock", json={"password": "nope"})).status_code == 403
    r = await rc.post(f"/api/pdf/{doc_id}/security/unlock", json={"password": "boss"})
    assert r.status_code == 200
    d = reopen(upload_dir, doc_id)
    assert not d.metadata.get("encryption")
    assert d.permissions & fitz.PDF_PERM_COPY
    d.close()
    assert (await rc.post(f"/api/pdf/{doc_id}/security/unlock", json={"password": "boss"})).status_code == 400


async def test_open_password_document_is_locked_until_unlocked(rc, upload_dir):
    data = sample_doc().tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="own", user_pw="usr")
    doc_id = store(upload_dir, data)
    assert (await rc.post(f"/api/pdf/{doc_id}/redact/search", json={"presets": ["ssn"]})).status_code == 423
    assert (await rc.get(f"/api/pdf/{doc_id}/security/audit")).json()["needs_password"] is True
    r = await rc.post(f"/api/pdf/{doc_id}/security/unlock", json={"password": "usr"})
    assert r.status_code == 403 and "permissions password" in r.json()["detail"]
    r = await rc.post(f"/api/pdf/{doc_id}/security/unlock", json={"password": "own"})
    assert r.status_code == 200
    d = reopen(upload_dir, doc_id)
    assert not d.needs_pass and SECRET in d[0].get_text()
    d.close()


# ─── validation ──────────────────────────────────────────────────────────────


async def test_validation_errors(rc, upload_dir):
    assert (await rc.get("/api/pdf/../etc/redact/marks")).status_code in (400, 404)
    assert (await rc.get("/api/pdf/not-a-uuid/redact/marks")).status_code == 400
    assert (await rc.get(f"/api/pdf/{uuid.uuid4()}/redact/marks")).status_code == 404
    doc_id = store(upload_dir, sample_doc())
    r = await rc.post(f"/api/pdf/{doc_id}/redact/mark", json={"areas": [{"page": 9, "rect": [0, 0, 10, 10]}]})
    assert r.status_code == 400
    r = await rc.post(f"/api/pdf/{doc_id}/redact/mark", json={"areas": [{"page": 0, "rect": [0, 0, 0, 0]}]})
    assert r.status_code == 400
    r = await rc.post(f"/api/pdf/{doc_id}/redact/mark", json={"areas": [{"page": 0, "rect": [1, 1, 20, 20]}], "fill_color": "#zzz"})
    assert r.status_code == 400
    assert (await rc.get(f"/api/pdf/{doc_id}/redact/words/5")).status_code == 400
    # Failed requests must not have mutated or snapshotted the document.
    assert not (upload_dir / doc_id / "history.json").exists()
