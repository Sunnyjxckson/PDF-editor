"""Tests for backend/features/objects.py (image & vector object editing).

Every test builds a real PDF with PyMuPDF, runs the endpoint, then re-opens the
saved file and inspects the actual PDF content (placements, pixels, paths, text).
"""

import io
import uuid

import fitz
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image

import backend.advanced_ops as adv
from backend.features import objects

TEXT_ABOVE = "Text above the picture"
TEXT_BELOW = "Caption below the picture"


def _png(w, h, color, alpha=None) -> bytes:
    mode = "RGBA" if alpha is not None else "RGB"
    c = tuple(color) + ((alpha,) if alpha is not None else ())
    img = Image.new(mode, (w, h), c)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _quadrant_png() -> bytes:
    """40x20 image: left half red, right half blue."""
    img = Image.new("RGB", (40, 20), (255, 0, 0))
    for x in range(20, 40):
        for y in range(20):
            img.putpixel((x, y), (0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _make_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 150), TEXT_ABOVE, fontsize=12)
    page.insert_image(fitz.Rect(100, 200, 300, 300), stream=_quadrant_png())
    page.insert_text((100, 320), TEXT_BELOW, fontsize=12)
    page.insert_image(fitz.Rect(350, 200, 450, 300), stream=_png(10, 10, (0, 255, 0)))
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(100, 500, 200, 560))
    shape.finish(color=(1, 0, 0), fill=(0, 0, 1), width=2)
    shape.draw_line((100, 580), (200, 580))  # touches nothing else -> separate group
    shape.finish(color=(0, 0, 0), width=1)
    shape.commit()
    page.insert_text((300, 540), "Vector caption", fontsize=12)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(objects, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(adv, "UPLOAD_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def doc_id(store) -> str:
    did = str(uuid.uuid4())
    (store / did).mkdir()
    (store / did / "original.pdf").write_bytes(_make_pdf())
    return did


@pytest_asyncio.fixture
async def oclient(store):
    app = FastAPI()
    app.include_router(objects.router)
    app.include_router(adv.router)  # for undo
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def _open(store, did) -> fitz.Document:
    return fitz.open(str(store / did / "original.pdf"))


def _infos(store, did, page=0):
    d = _open(store, did)
    out = d[page].get_image_info(xrefs=True)
    text = d[page].get_text()
    d.close()
    return out, text


def _pixel(store, did, x, y, page=0):
    """RGB of the rendered page at PDF point (x, y)."""
    d = _open(store, did)
    pix = d[page].get_pixmap(dpi=72)
    px = pix.pixel(int(x), int(y))
    d.close()
    return px


async def _objects(c, did, page=0):
    r = await c.get(f"/api/pdf/{did}/objects/{page}")
    assert r.status_code == 200, r.text
    return r.json()


# ─── Listing ──────────────────────────────────────────────────────────────────


async def test_list_objects(oclient, doc_id):
    data = await _objects(oclient, doc_id)
    assert data["page_width"] == 612 and data["page_height"] == 792
    imgs = data["images"]
    assert len(imgs) == 2
    assert imgs[0]["bbox"] == [100, 200, 300, 300]
    assert imgs[0]["width"] == 40 and imgs[0]["height"] == 20
    assert imgs[0]["method"] == "stream" and imgs[0]["editable"]
    drw = data["drawings"]
    assert len(drw) == 2  # rect and the separate line
    boxes = sorted(tuple(round(v) for v in d["bbox"]) for d in drw)
    assert boxes[0][:2] == (100, 500)
    assert any(abs(d["bbox"][1] - 580) < 2 for d in drw)


async def test_list_invalid(oclient, doc_id):
    assert (await oclient.get(f"/api/pdf/not-a-uuid/objects/0")).status_code == 400
    assert (await oclient.get(f"/api/pdf/{uuid.uuid4()}/objects/0")).status_code == 404
    assert (await oclient.get(f"/api/pdf/{doc_id}/objects/7")).status_code == 400


async def test_drawing_grouping_merges_touching_paths(oclient, store):
    doc = fitz.open()
    page = doc.new_page()
    s = page.new_shape()
    # a box made of 4 separate lines + a dot inside -> one object
    for a, b in [((50, 50), (150, 50)), ((150, 50), (150, 150)), ((150, 150), (50, 150)), ((50, 150), (50, 50))]:
        s.draw_line(a, b)
        s.finish(color=(0, 0, 0), width=1)
    s.draw_circle((100, 100), 3)
    s.finish(fill=(1, 0, 0), color=None)
    s.draw_rect(fitz.Rect(300, 300, 320, 320))  # far away -> separate
    s.finish(color=(0, 0, 1))
    s.commit()
    did = str(uuid.uuid4())
    (store / did).mkdir()
    doc.save(str(store / did / "original.pdf"))
    data = await _objects(oclient, did)
    assert len(data["drawings"]) == 2
    big = max(data["drawings"], key=lambda d: d["path_count"])
    assert big["path_count"] == 5


# ─── Image move / resize ──────────────────────────────────────────────────────


async def test_move_resize_image_keeps_text_and_other_image(oclient, store, doc_id):
    r = await oclient.post(
        f"/api/pdf/{doc_id}/objects/image/move",
        json={"page": 0, "xref": (await _objects(oclient, doc_id))["images"][0]["xref"],
              "occurrence": 0, "bbox": [100, 200, 300, 300], "new_bbox": [120, 400, 220, 450]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["method"] == "stream"
    infos, text = _infos(store, doc_id)
    bboxes = sorted([tuple(round(v, 2) for v in i["bbox"]) for i in infos])
    assert (120, 400, 220, 450) in bboxes
    assert (100, 200, 300, 300) not in bboxes
    assert (350, 200, 450, 300) in bboxes  # other image untouched
    assert len(infos) == 2  # moved, not duplicated
    assert TEXT_ABOVE in text and TEXT_BELOW in text
    # rendered: old area is now white, new area shows red (left half of image)
    assert _pixel(store, doc_id, 150, 250) == (255, 255, 255)
    assert _pixel(store, doc_id, 140, 425) == (255, 0, 0)
    assert _pixel(store, doc_id, 200, 425) == (0, 0, 255)


async def test_move_stale_bbox_conflict(oclient, doc_id):
    xref = (await _objects(oclient, doc_id))["images"][0]["xref"]
    r = await oclient.post(
        f"/api/pdf/{doc_id}/objects/image/move",
        json={"page": 0, "xref": xref, "occurrence": 0, "bbox": [0, 0, 10, 10], "new_bbox": [0, 0, 50, 50]},
    )
    assert r.status_code == 409


async def test_move_then_undo_restores(oclient, store, doc_id):
    xref = (await _objects(oclient, doc_id))["images"][0]["xref"]
    await oclient.post(
        f"/api/pdf/{doc_id}/objects/image/move",
        json={"page": 0, "xref": xref, "new_bbox": [10, 10, 60, 60]},
    )
    assert (await oclient.post(f"/api/pdf/{doc_id}/undo")).status_code == 200
    infos, _ = _infos(store, doc_id)
    assert [round(v) for v in infos[0]["bbox"]] == [100, 200, 300, 300]


async def test_move_twice_and_same_xref_two_placements(oclient, store):
    """Same image xref drawn twice: moving occurrence 1 must leave occurrence 0."""
    doc = fitz.open()
    page = doc.new_page()
    xref = page.insert_image(fitz.Rect(50, 50, 150, 150), stream=_png(8, 8, (255, 0, 0)))
    page.insert_image(fitz.Rect(300, 50, 400, 150), xref=xref)
    did = str(uuid.uuid4())
    (store / did).mkdir()
    doc.save(str(store / did / "original.pdf"))
    for nb in ([300, 300, 350, 350], [310, 400, 410, 500]):
        cur = [i for i in (await _objects(oclient, did))["images"] if i["occurrence"] == 1][0]
        r = await oclient.post(
            f"/api/pdf/{did}/objects/image/move",
            json={"page": 0, "xref": xref, "occurrence": 1, "bbox": cur["bbox"], "new_bbox": nb},
        )
        assert r.status_code == 200, r.text
    infos, _ = _infos(store, did)
    boxes = sorted(tuple(round(v) for v in i["bbox"]) for i in infos)
    assert boxes == [(50, 50, 150, 150), (310, 400, 410, 500)]


# ─── Rotate ───────────────────────────────────────────────────────────────────


async def test_rotate_image_90(oclient, store, doc_id):
    xref = (await _objects(oclient, doc_id))["images"][0]["xref"]
    r = await oclient.post(
        f"/api/pdf/{doc_id}/objects/image/rotate",
        json={"page": 0, "xref": xref, "angle": 90},
    )
    assert r.status_code == 200, r.text
    infos, text = _infos(store, doc_id)
    bb = [round(v, 2) for v in infos[0]["bbox"]]
    # 200x100 box about centre (200,250) -> 100x200 box
    assert bb == [150, 150, 250, 350]
    assert TEXT_ABOVE in text
    # clockwise: the left (red) half ends up on top, blue at the bottom
    assert _pixel(store, doc_id, 200, 190) == (255, 0, 0)
    assert _pixel(store, doc_id, 200, 330) == (0, 0, 255)


async def test_rotate_bad_angle(oclient, doc_id):
    xref = (await _objects(oclient, doc_id))["images"][0]["xref"]
    r = await oclient.post(f"/api/pdf/{doc_id}/objects/image/rotate", json={"page": 0, "xref": xref, "angle": 45})
    assert r.status_code == 400


# ─── Delete ───────────────────────────────────────────────────────────────────


async def test_delete_image_removes_bytes_keeps_text(oclient, store, doc_id):
    imgs = (await _objects(oclient, doc_id))["images"]
    xref = imgs[0]["xref"]
    r = await oclient.post(
        f"/api/pdf/{doc_id}/objects/image/delete",
        json={"page": 0, "xref": xref, "occurrence": 0, "bbox": imgs[0]["bbox"]},
    )
    assert r.status_code == 200, r.text
    infos, text = _infos(store, doc_id)
    assert len(infos) == 1
    assert [round(v) for v in infos[0]["bbox"]] == [350, 200, 450, 300]
    assert TEXT_ABOVE in text and TEXT_BELOW in text
    d = _open(store, doc_id)
    assert all(img[0] != xref for img in d[0].get_images(full=True))
    # object garbage-collected: no remaining image object has the 40x20 size
    sizes = [(d.xref_get_key(x, "Width")[1], d.xref_get_key(x, "Height")[1])
             for x in range(1, d.xref_length()) if d.xref_is_image(x)]
    d.close()
    assert ("40", "20") not in sizes
    assert _pixel(store, doc_id, 150, 250) == (255, 255, 255)


# ─── Crop ─────────────────────────────────────────────────────────────────────


async def test_crop_image_keeps_only_right_half(oclient, store, doc_id):
    xref = (await _objects(oclient, doc_id))["images"][0]["xref"]
    r = await oclient.post(
        f"/api/pdf/{doc_id}/objects/image/crop",
        json={"page": 0, "xref": xref, "crop_bbox": [200, 180, 320, 320]},  # clamps to image
    )
    assert r.status_code == 200, r.text
    assert r.json()["pixels"] == [20, 20]
    infos, text = _infos(store, doc_id)
    boxes = sorted(tuple(round(v, 2) for v in i["bbox"]) for i in infos)
    assert (200, 200, 300, 300) in boxes
    cropped = [i for i in infos if round(i["bbox"][0]) == 200][0]
    assert (cropped["width"], cropped["height"]) == (20, 20)
    assert _pixel(store, doc_id, 150, 250) == (255, 255, 255)  # left half gone
    assert _pixel(store, doc_id, 250, 250) == (0, 0, 255)
    assert TEXT_BELOW in text
    # the extracted cropped image is entirely blue
    d = _open(store, doc_id)
    pix = fitz.Pixmap(d, cropped["xref"])
    assert pix.pixel(0, 0)[:3] == (0, 0, 255) and pix.pixel(19, 19)[:3] == (0, 0, 255)
    d.close()


async def test_crop_outside_rejected(oclient, doc_id):
    xref = (await _objects(oclient, doc_id))["images"][0]["xref"]
    r = await oclient.post(
        f"/api/pdf/{doc_id}/objects/image/crop",
        json={"page": 0, "xref": xref, "crop_bbox": [500, 500, 600, 600]},
    )
    assert r.status_code == 400


# ─── Replace ──────────────────────────────────────────────────────────────────


async def test_replace_single_placement(oclient, store, doc_id):
    xref = (await _objects(oclient, doc_id))["images"][0]["xref"]
    r = await oclient.post(
        f"/api/pdf/{doc_id}/objects/image/replace",
        data={"page": "0", "xref": str(xref), "occurrence": "0", "scope": "placement"},
        files={"file": ("y.png", _png(16, 16, (255, 255, 0)), "image/png")},
    )
    assert r.status_code == 200, r.text
    infos, text = _infos(store, doc_id)
    assert len(infos) == 2
    first = infos[0]
    assert [round(v) for v in first["bbox"]] == [100, 200, 300, 300]  # same z-order & box
    assert (first["width"], first["height"]) == (16, 16)
    assert _pixel(store, doc_id, 150, 250) == (255, 255, 0)
    assert TEXT_ABOVE in text


async def test_replace_all_uses_replace_image(oclient, store, doc_id):
    xref = (await _objects(oclient, doc_id))["images"][1]["xref"]
    r = await oclient.post(
        f"/api/pdf/{doc_id}/objects/image/replace",
        data={"page": "0", "xref": str(xref), "scope": "all"},
        files={"file": ("m.png", _png(4, 4, (255, 0, 255)), "image/png")},
    )
    assert r.status_code == 200 and r.json()["method"] == "replace_image"
    assert _pixel(store, doc_id, 400, 250) == (255, 0, 255)


async def test_replace_rejects_garbage(oclient, doc_id):
    xref = (await _objects(oclient, doc_id))["images"][0]["xref"]
    r = await oclient.post(
        f"/api/pdf/{doc_id}/objects/image/replace",
        data={"page": "0", "xref": str(xref)},
        files={"file": ("x.png", b"not an image", "image/png")},
    )
    assert r.status_code == 400


# ─── Extract ──────────────────────────────────────────────────────────────────


async def test_extract_original_and_png(oclient, doc_id):
    xref = (await _objects(oclient, doc_id))["images"][0]["xref"]
    r = await oclient.get(f"/api/pdf/{doc_id}/objects/image/{xref}/extract")
    assert r.status_code == 200
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (40, 20)
    r2 = await oclient.get(f"/api/pdf/{doc_id}/objects/image/{xref}/extract?format=png")
    assert r2.headers["content-type"] == "image/png"
    img2 = Image.open(io.BytesIO(r2.content)).convert("RGB")
    assert img2.getpixel((0, 0)) == (255, 0, 0) and img2.getpixel((39, 0)) == (0, 0, 255)
    assert (await oclient.get(f"/api/pdf/{doc_id}/objects/image/1/extract")).status_code == 404


# ─── Insert ───────────────────────────────────────────────────────────────────


async def test_insert_image_with_transparency(oclient, store, doc_id):
    r = await oclient.post(
        f"/api/pdf/{doc_id}/objects/image/insert",
        data={"page": "0", "x0": "400", "y0": "600", "x1": "500", "y1": "700", "keep_proportion": "false"},
        files={"file": ("a.png", _png(10, 10, (0, 128, 0), alpha=255), "image/png")},
    )
    assert r.status_code == 200, r.text
    infos, _ = _infos(store, doc_id)
    assert len(infos) == 3
    assert [round(v) for v in infos[-1]["bbox"]] == [400, 600, 500, 700]
    assert infos[-1]["has-mask"]
    assert _pixel(store, doc_id, 450, 650) == (0, 128, 0)


# ─── Shapes ───────────────────────────────────────────────────────────────────


async def test_add_shapes_are_vector_not_annotations(oclient, store, doc_id):
    base = len(_open(store, doc_id)[0].get_drawings())
    calls = [
        {"type": "rect", "rect": [50, 650, 150, 700], "stroke_color": [0, 0, 1], "fill_color": [1, 1, 0], "width": 3},
        {"type": "ellipse", "rect": [200, 650, 300, 700], "stroke_color": [1, 0, 0], "width": 1},
        {"type": "line", "start": [320, 650], "end": [400, 700], "stroke_color": [0, 0, 0], "width": 2, "dashed": True},
        {"type": "arrow", "start": [420, 650], "end": [550, 650], "stroke_color": [255, 0, 0], "width": 2},
    ]
    for body in calls:
        r = await oclient.post(f"/api/pdf/{doc_id}/objects/shape", json={"page": 0, **body})
        assert r.status_code == 200, r.text
    d = _open(store, doc_id)
    page = d[0]
    assert list(page.annots()) == []
    paths = page.get_drawings()
    new = paths[base:]
    assert len(new) >= 5  # rect, ellipse, line, arrow shaft, arrow head
    rect = [p for p in new if p["rect"] == fitz.Rect(50, 650, 150, 700)][0]
    assert rect["fill"] == (1.0, 1.0, 0.0) and rect["color"] == (0.0, 0.0, 1.0) and rect["width"] == 3
    ellipse = [p for p in new if abs(p["rect"].x0 - 200) < 0.5][0]
    assert any(it[0] == "c" for it in ellipse["items"])  # bezier curves
    line = [p for p in new if abs(p["rect"].x0 - 320) < 0.5][0]
    assert line["dashes"] != "[] 0"
    head = [p for p in new if p.get("fill") == (1.0, 0.0, 0.0)]
    assert head and abs(head[0]["rect"].x1 - 550) < 0.5  # arrow tip reaches end point
    d.close()


async def test_shape_validation(oclient, doc_id):
    bad = [
        {"page": 0, "type": "star", "rect": [0, 0, 10, 10]},
        {"page": 0, "type": "rect"},
        {"page": 0, "type": "line", "start": [1, 1], "end": [1, 1]},
        {"page": 0, "type": "rect", "rect": [0, 0, 10, 10], "stroke_color": None},
    ]
    for body in bad:
        assert (await oclient.post(f"/api/pdf/{doc_id}/objects/shape", json=body)).status_code == 400


# ─── Vector objects ───────────────────────────────────────────────────────────


async def test_move_drawing_group(oclient, store, doc_id):
    drw = (await _objects(oclient, doc_id))["drawings"]
    rect_obj = [d for d in drw if abs(d["bbox"][1] - 500) < 2][0]
    new = [400, 600, 450, 630]
    r = await oclient.post(
        f"/api/pdf/{doc_id}/objects/drawing/move",
        json={"page": 0, "index": rect_obj["index"], "bbox": rect_obj["bbox"], "new_bbox": new},
    )
    assert r.status_code == 200, r.text
    d = _open(store, doc_id)
    paths = d[0].get_drawings()
    text = d[0].get_text()
    d.close()
    assert not any(abs(p["rect"].x0 - 100) < 2 and abs(p["rect"].y0 - 500) < 2 for p in paths)
    moved = [p for p in paths if p.get("fill") == (0.0, 0.0, 1.0)]
    assert len(moved) == 1
    mr = moved[0]["rect"]
    assert abs(mr.x0 - 400) < 2 and abs(mr.y0 - 600) < 2 and abs(mr.x1 - 450) < 2 and abs(mr.y1 - 630) < 2
    assert moved[0]["color"] == (1.0, 0.0, 0.0)
    assert any(abs(p["rect"].y0 - 580) < 1 for p in paths)  # the separate line survives
    assert "Vector caption" in text and TEXT_BELOW in text


async def test_delete_drawing_group_keeps_text_images(oclient, store, doc_id):
    drw = (await _objects(oclient, doc_id))["drawings"]
    rect_obj = [d for d in drw if abs(d["bbox"][1] - 500) < 2][0]
    r = await oclient.post(
        f"/api/pdf/{doc_id}/objects/drawing/delete",
        json={"page": 0, "index": rect_obj["index"], "bbox": rect_obj["bbox"]},
    )
    assert r.status_code == 200, r.text
    d = _open(store, doc_id)
    paths = d[0].get_drawings()
    assert len(paths) == 1 and abs(paths[0]["rect"].y0 - 580) < 1
    assert len(d[0].get_image_info()) == 2
    assert "Vector caption" in d[0].get_text()
    d.close()


async def test_drawing_stale_conflict(oclient, doc_id):
    r = await oclient.post(
        f"/api/pdf/{doc_id}/objects/drawing/delete",
        json={"page": 0, "index": 0, "bbox": [1, 1, 2, 2]},
    )
    assert r.status_code == 409


# ─── Tokenizer ────────────────────────────────────────────────────────────────


def test_tokenizer_ignores_strings_and_inline_images():
    data = (
        b"BT (fake /Im1 Do inside string \\) still) Tj ET\n"
        b"% /Im1 Do in a comment\n"
        b"BI /W 2 /H 1 /CS /G /BPC 8 ID \x00/Im1 Do\x01 EI\n"
        b"q 1 0 0 1 0 0 cm /Im#31 Do Q /Fm0 Do"
    )
    ops = objects._find_do_ops(data)
    assert [name for _, _, name in ops] == ["Im1", "Fm0"]
    s, e, _ = ops[0]
    assert data[s:e] == b"/Im#31 Do"


# ─── Fallback: image drawn from inside a Form XObject ─────────────────────────


async def test_form_xobject_image_uses_redact_fallback(oclient, store):
    src = fitz.open()
    sp = src.new_page(width=200, height=100)
    sp.insert_image(fitz.Rect(0, 0, 200, 100), stream=_png(20, 10, (255, 0, 0)))
    doc = fitz.open()
    page = doc.new_page()
    page.show_pdf_page(fitz.Rect(100, 100, 300, 200), src, 0)  # wraps image in a Form XObject
    page.insert_text((100, 250), "Survives fallback", fontsize=12)
    did = str(uuid.uuid4())
    (store / did).mkdir()
    doc.save(str(store / did / "original.pdf"))

    img = (await _objects(oclient, did))["images"][0]
    assert img["method"] == "redact"
    r = await oclient.post(
        f"/api/pdf/{did}/objects/image/move",
        json={"page": 0, "xref": img["xref"], "bbox": img["bbox"], "new_bbox": [300, 400, 400, 450]},
    )
    assert r.status_code == 200 and r.json()["method"] == "redact"
    infos, text = _infos(store, did)
    assert [[round(v) for v in i["bbox"]] for i in infos] == [[300, 400, 400, 450]]
    assert "Survives fallback" in text
    assert _pixel(store, did, 350, 425) == (255, 0, 0)
    assert _pixel(store, did, 200, 150) == (255, 255, 255)


def test_same_xref_multiple_resource_names_is_stream_addressable():
    doc = fitz.open()
    page = doc.new_page()
    xref = page.insert_image(fitz.Rect(50, 50, 150, 150), stream=_png(8, 8, (255, 0, 0)))
    page.insert_image(fitz.Rect(300, 50, 400, 150), xref=xref)  # registered as a 2nd name
    pls = objects._image_placements(page)
    assert [p["method"] for p in pls] == ["stream", "stream"]
    assert len(pls[0]["names"]) == 2


async def test_rotated_page_uses_display_coordinates(oclient, store):
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_image(fitz.Rect(50, 50, 150, 100), stream=_png(8, 8, (255, 0, 0)))
    page.set_rotation(90)
    did = str(uuid.uuid4())
    (store / did).mkdir()
    doc.save(str(store / did / "original.pdf"))

    data = await _objects(oclient, did)
    assert (data["page_width"], data["page_height"], data["rotation"]) == (800, 600, 90)
    img = data["images"][0]
    # the listed bbox is where the image actually renders on the rotated page
    x0, y0, x1, y1 = img["bbox"]
    assert _pixel(store, did, (x0 + x1) / 2, (y0 + y1) / 2) == (255, 0, 0)

    r = await oclient.post(
        f"/api/pdf/{did}/objects/image/move",
        json={"page": 0, "xref": img["xref"], "bbox": img["bbox"], "new_bbox": [500, 300, 600, 400]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["bbox"] == [500, 300, 600, 400]
    assert _pixel(store, did, 550, 350) == (255, 0, 0)
    assert _pixel(store, did, (x0 + x1) / 2, (y0 + y1) / 2) == (255, 255, 255)
    assert (await _objects(oclient, did))["images"][0]["bbox"] == [500, 300, 600, 400]

    r = await oclient.post(
        f"/api/pdf/{did}/objects/shape",
        json={"page": 0, "type": "rect", "rect": [100, 400, 200, 500], "stroke_color": None, "fill_color": [0, 0, 1]},
    )
    assert r.status_code == 200, r.text
    assert _pixel(store, did, 150, 450) == (0, 0, 255)
