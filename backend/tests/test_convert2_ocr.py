"""convert2: OCR language management + real "Edit scanned text" (inpainting).

Every assertion is made on the real resulting PDF re-opened with PyMuPDF.
Nothing here touches the network: the tessdata download is monkeypatched.
"""

import asyncio
import io
import uuid

import fitz
import numpy as np
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image, ImageDraw, ImageFont

from backend import advanced_ops
from backend.features import convert, text_edit

HAS_ENG = "eng" in convert._available_languages()
needs_tesseract = pytest.mark.skipif(not HAS_ENG, reason="tesseract eng data not installed")

PAPER = (246, 240, 222)  # cream "paper" so background estimation is non-trivial


@pytest.fixture
def store(tmp_path, monkeypatch):
    up = tmp_path / "uploads"
    up.mkdir()
    monkeypatch.setattr(advanced_ops, "UPLOAD_DIR", up)
    return up


@pytest_asyncio.fixture
async def cl(store):
    app = FastAPI()
    app.include_router(convert.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def put_pdf(store, doc) -> str:
    doc_id = str(uuid.uuid4())
    (store / doc_id).mkdir()
    doc.save(str(store / doc_id / "original.pdf"))
    doc.close()
    return doc_id


def _font(size):
    for p in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default(size=size)


def scanned_pdf(fmt="PNG"):
    img = Image.new("RGB", (1700, 2200), PAPER)
    d = ImageDraw.Draw(img)
    f = _font(60)
    d.text((150, 200), "Quarterly Invoice Number 4821", fill=(20, 20, 20), font=f)
    d.text((150, 400), "The quick brown fox jumps", fill=(20, 20, 20), font=f)
    b = io.BytesIO()
    img.save(b, fmt, **({"quality": 95} if fmt == "JPEG" else {}))
    doc = fitz.open()
    p = doc.new_page(width=612, height=792)
    p.insert_image(p.rect, stream=b.getvalue())
    return doc


async def run_job(cl, doc_id, body):
    r = await cl.post(f"/api/pdf/{doc_id}/ocr", json=body)
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    for _ in range(600):
        j = (await cl.get(f"/api/pdf/ocr/jobs/{jid}")).json()
        if j["status"] in ("done", "error"):
            return j
        await asyncio.sleep(0.1)
    raise AssertionError("timeout")


def page_image(doc, page):
    """The page's scan as a numpy array plus its placement matrix."""
    info = [i for i in page.get_image_info(xrefs=True) if i["xref"]][0]
    pix = fitz.Pixmap(doc, info["xref"])
    if pix.n not in (1, 3):
        pix = fitz.Pixmap(fitz.csRGB, pix)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return arr.astype(int), fitz.Matrix(info["transform"])


def pixels_under(arr, mat, rect):
    inv = ~mat
    H, W = arr.shape[:2]
    a, b = rect.tl * inv, rect.br * inv
    return arr[int(a.y * H):int(b.y * H), int(a.x * W):int(b.x * W)]


# ─── languages ───────────────────────────────────────────────────────────────


async def test_languages_listing_has_installable_and_multi_validation(cl, store):
    r = (await cl.get("/api/pdf/ocr/languages")).json()
    assert set(r) >= {"languages", "installable", "names"}
    inst = {x["code"] for x in r["installable"]}
    assert "spa" in inst or "spa" in r["languages"]
    assert not (inst & set(r["languages"]))  # installed langs are not offered again
    assert "osd" not in r["languages"]
    doc_id = put_pdf(store, scanned_pdf())
    bad = await cl.post(f"/api/pdf/{doc_id}/ocr", json={"language": "eng+zzz"})
    assert bad.status_code == 400 and "zzz" in bad.json()["detail"]


async def test_install_language_downloads_into_user_dir_mocked(cl, store, tmp_path, monkeypatch):
    user = tmp_path / "tessdata"
    monkeypatch.setenv("PDF_EDITOR_TESSDATA_DIR", str(user))
    calls = []

    def fake_fetch(code):
        calls.append(code)
        return b"TESSDATA-FAKE" + b"\0" * 4096

    monkeypatch.setattr(convert, "_fetch_traineddata", fake_fetch)
    assert "spa" not in convert._available_languages()

    r = await cl.post("/api/pdf/ocr/languages/install", json={"code": "spa"})
    assert r.status_code == 200, r.text
    assert calls == ["spa"]
    assert (user / "spa.traineddata").read_bytes().startswith(b"TESSDATA-FAKE")
    assert "spa" in r.json()["languages"]
    langs = (await cl.get("/api/pdf/ocr/languages")).json()
    assert "spa" in langs["languages"] and "spa" not in {x["code"] for x in langs["installable"]}
    if HAS_ENG:
        # system eng is linked into the user dir so ONE dir serves "eng+spa"
        assert (user / "eng.traineddata").exists()
        assert convert._tessdata("eng+spa") == str(user)
        assert convert._validate_language("eng+spa") == "eng+spa"

    # second install is a no-op; unknown codes and HTML error pages are rejected
    again = (await cl.post("/api/pdf/ocr/languages/install", json={"code": "spa"})).json()
    assert again["already_installed"] is True and calls == ["spa"]
    assert (await cl.post("/api/pdf/ocr/languages/install", json={"code": "../etc"})).status_code == 400
    monkeypatch.setattr(convert, "_fetch_traineddata", lambda c: b"<!DOCTYPE html>" + b" " * 2000)
    assert (await cl.post("/api/pdf/ocr/languages/install", json={"code": "fra"})).status_code == 502
    assert not (user / "fra.traineddata").exists()


def test_tessdata_prefix_is_honoured(tmp_path, monkeypatch):
    td = tmp_path / "prefix"
    td.mkdir()
    (td / "xyz.traineddata").write_bytes(b"x" * 2048)
    monkeypatch.setenv("TESSDATA_PREFIX", str(td))
    monkeypatch.setenv("PDF_EDITOR_TESSDATA_DIR", str(tmp_path / "nouser"))
    assert convert._system_tessdata_dirs()[0] == str(td)
    assert "xyz" in convert._available_languages()
    assert convert._tessdata("xyz") == str(td)


# ─── editable OCR ────────────────────────────────────────────────────────────


@needs_tesseract
@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
async def test_editable_ocr_inpaints_scan_and_places_editable_text(cl, store, fmt):
    doc_id = put_pdf(store, scanned_pdf(fmt))
    job = await run_job(cl, doc_id, {"language": "eng", "mode": "editable"})
    assert job["status"] == "done", job
    assert job["result"]["words_added"] >= 8

    doc = fitz.open(str(store / doc_id / "original.pdf"))
    page = doc[0]
    # 1. the Edit Text tool sees real, visible, editable lines
    blocks = text_edit.extract_blocks(page)
    lines = [l.text for b in blocks for l in b.lines]
    assert any("Quarterly Invoice Number 4821" in t for t in lines), lines
    assert any("quick brown fox" in t for t in lines), lines
    traces = page.get_texttrace()
    assert traces and all(t["type"] == 0 for t in traces)
    assert all(max(t["color"]) < 0.35 for t in traces)  # ink colour sampled from the scan

    # 2. font size matched to the scan: 60px glyphs at 200dpi ~= 21.6pt
    sizes = [t["size"] for t in traces]
    assert all(17 < s < 26 for s in sizes), sizes

    # 3. the glyph pixels are gone from the scan: under each word only background remains
    arr, mat = page_image(doc, page)
    for word in ("Quarterly", "4821", "brown"):
        hit = page.search_for(word)[0]
        sub = pixels_under(arr, mat, hit)
        assert sub.size
        dark = (sub.mean(axis=2) < 150).mean()
        assert dark < 0.01, (word, dark)
        med = np.median(sub.reshape(-1, sub.shape[2]), axis=0)
        assert np.abs(med - np.array(PAPER[: sub.shape[2]])).max() < 12, med

    # 4. the rendered page still reads the same: text pixels where the scan had them
    rend = page.get_pixmap(dpi=72, colorspace=fitz.csGRAY)
    g = np.frombuffer(rend.samples, dtype=np.uint8).reshape(rend.height, rend.width)
    word_box = page.search_for("Quarterly")[0]
    assert (g[int(word_box.y0):int(word_box.y1), int(word_box.x0):int(word_box.x1)] < 128).mean() > 0.05
    doc.close()


@needs_tesseract
async def test_searchable_stays_default_and_leaves_scan_untouched(cl, store):
    doc_id = put_pdf(store, scanned_pdf())
    before = fitz.open(str(store / doc_id / "original.pdf"))
    arr0, _ = page_image(before, before[0])
    before.close()
    job = await run_job(cl, doc_id, {"language": "eng"})
    assert job["status"] == "done"
    doc = fitz.open(str(store / doc_id / "original.pdf"))
    arr1, _ = page_image(doc, doc[0])
    assert np.array_equal(arr0, arr1)
    assert all(t["type"] == 3 for t in doc[0].get_texttrace())
