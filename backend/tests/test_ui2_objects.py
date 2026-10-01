"""ui2: z-order preserving object moves, batch (one undo step), arrange, duplicate.

Every test builds a real PDF, calls the endpoint, then re-opens the saved file
with PyMuPDF and checks the actual content / rendered pixels.
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


def _png(w, h, color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(objects, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(adv, "UPLOAD_DIR", tmp_path)
    return tmp_path


@pytest_asyncio.fixture
async def client(store):
    app = FastAPI()
    app.include_router(objects.router)
    app.include_router(adv.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def _save(store, doc) -> str:
    did = str(uuid.uuid4())
    (store / did).mkdir()
    doc.save(str(store / did / "original.pdf"))
    doc.close()
    return did


def _open(store, did) -> fitz.Document:
    return fitz.open(str(store / did / "original.pdf"))


def _dark_pixels(store, did, rect, page=0) -> int:
    """Count near-black pixels (text) inside rect of the rendered page (72 dpi)."""
    d = _open(store, did)
    pix = d[page].get_pixmap(dpi=72)
    n = 0
    for y in range(int(rect[1]), int(rect[3])):
        for x in range(int(rect[0]), int(rect[2])):
            r, g, b = pix.pixel(x, y)[:3]
            if r < 90 and g < 90 and b < 90:
                n += 1
    d.close()
    return n


def _pixel(store, did, x, y, page=0):
    d = _open(store, did)
    px = d[page].get_pixmap(dpi=72).pixel(int(x), int(y))
    d.close()
    return px


async def _objects(c, did, page=0):
    r = await c.get(f"/api/pdf/{did}/objects/{page}")
    assert r.status_code == 200, r.text
    return r.json()


# ─── Z-order ──────────────────────────────────────────────────────────────────

TEXT_RECT = (110, 120, 260, 160)  # where the overlapping text lands after the moves below


def _text_over(page):
    page.insert_text((115, 150), "OVERLAP", fontsize=30, color=(0, 0, 0))


async def test_form_xobject_image_move_stays_below_text(client, store):
    """An image drawn from inside a Form XObject keeps its place UNDER the text."""
    src = fitz.open()
    sp = src.new_page(width=300, height=300)
    sp.insert_image(fitz.Rect(50, 50, 250, 150), stream=_png(20, 10, (255, 0, 0)))
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.show_pdf_page(fitz.Rect(50, 50, 350, 350), src, 0)  # image lands at 100,100,300,200
    _text_over(page)
    did = _save(store, doc)
    assert _dark_pixels(store, did, TEXT_RECT) > 50  # text visible on the image before

    img = (await _objects(client, did))["images"][0]
    assert img["bbox"] == [100, 100, 300, 200] and img["method"] == "redact"
    r = await client.post(
        f"/api/pdf/{did}/objects/image/move",
        json={"page": 0, "xref": img["xref"], "bbox": img["bbox"], "new_bbox": [90, 110, 290, 210]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["method"] == "form"
    d = _open(store, did)
    infos = d[0].get_image_info()
    d.close()
    assert [[round(v) for v in i["bbox"]] for i in infos] == [[90, 110, 290, 210]]
    assert _pixel(store, did, 95, 205)[:3] == (255, 0, 0)  # image really moved
    assert _pixel(store, did, 295, 105)[:3] == (255, 255, 255)
    # the text is still drawn on top of the moved image
    assert _dark_pixels(store, did, TEXT_RECT) > 50


async def test_form_image_move_outside_form_clip_falls_back(client, store):
    """Moving outside the form's BBox would clip it away: legacy re-place instead."""
    src = fitz.open()
    sp = src.new_page(width=200, height=100)
    sp.insert_image(sp.rect, stream=_png(20, 10, (255, 0, 0)))
    doc = fitz.open()
    page = doc.new_page()
    page.show_pdf_page(fitz.Rect(100, 100, 300, 200), src, 0)
    did = _save(store, doc)
    img = (await _objects(client, did))["images"][0]
    r = await client.post(
        f"/api/pdf/{did}/objects/image/move",
        json={"page": 0, "xref": img["xref"], "bbox": img["bbox"], "new_bbox": [300, 400, 400, 450]},
    )
    assert r.status_code == 200 and r.json()["method"] == "redact"
    assert _pixel(store, did, 350, 425)[:3] == (255, 0, 0)


async def test_stream_image_move_stays_below_text(client, store):
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(100, 100, 300, 200), stream=_png(20, 10, (255, 0, 0)))
    _text_over(page)
    did = _save(store, doc)
    img = (await _objects(client, did))["images"][0]
    r = await client.post(
        f"/api/pdf/{did}/objects/image/move",
        json={"page": 0, "xref": img["xref"], "bbox": img["bbox"], "new_bbox": [90, 110, 290, 210]},
    )
    assert r.status_code == 200 and r.json()["method"] == "stream"
    assert _dark_pixels(store, did, TEXT_RECT) > 50


def _vector_doc():
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    sh = page.new_shape()
    sh.draw_rect(fitz.Rect(100, 100, 300, 200))
    sh.finish(color=None, fill=(0, 0, 1))
    sh.commit()
    _text_over(page)
    return doc


async def test_vector_move_stays_below_text(client, store):
    did = _save(store, _vector_doc())
    drw = (await _objects(client, did))["drawings"][0]
    r = await client.post(
        f"/api/pdf/{did}/objects/drawing/move",
        json={"page": 0, "index": drw["index"], "bbox": drw["bbox"], "new_bbox": [90, 110, 290, 210]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["method"] == "stream"
    d = _open(store, did)
    paths = d[0].get_drawings()
    d.close()
    assert len(paths) == 1
    assert [round(v) for v in paths[0]["rect"]] == [90, 110, 290, 210]
    assert paths[0]["fill"] == (0.0, 0.0, 1.0)
    assert _pixel(store, did, 95, 205)[:3] == (0, 0, 255)
    assert _dark_pixels(store, did, TEXT_RECT) > 50


async def test_state_ops_inside_a_moved_path_still_apply_to_later_paths(client, store):
    """`0 0 1 RG` set inside path 1 must still colour path 2 after path 1 is wrapped."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    data = (
        b"1 0 0 RG 4 w\n"
        b"100 692 m 0 0 1 RG 200 682 l S\n"  # path 1 (y 100..110), sets blue mid-path
        b"100 342 m 200 342 l S\n"  # path 2 (y=450) inherits blue
    )
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, data)
    doc.xref_set_key(page.xref, "Contents", f"{xref} 0 R")
    did = _save(store, doc)
    drw = (await _objects(client, did))["drawings"]
    first = [o for o in drw if abs(o["bbox"][1] - 100) < 1][0]
    r = await client.post(
        f"/api/pdf/{did}/objects/drawing/move",
        json={"page": 0, "index": first["index"], "bbox": first["bbox"], "new_bbox": [300, 150, 400, 160]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["method"] == "stream"
    d = _open(store, did)
    paths = sorted(d[0].get_drawings(), key=lambda p: p["rect"].y0)
    d.close()
    assert len(paths) == 2
    assert [round(v) for v in paths[0]["rect"]] == [300, 150, 400, 160]
    assert paths[0]["color"] == (0.0, 0.0, 1.0)
    assert abs(paths[1]["rect"].y0 - 450) < 1
    assert paths[1]["color"] == (0.0, 0.0, 1.0)  # still blue: the state op was re-emitted


# ─── Batch: one snapshot, one undo step ───────────────────────────────────────


def _two_objects_doc():
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(100, 100, 200, 150), stream=_png(10, 5, (255, 0, 0)))
    sh = page.new_shape()
    sh.draw_rect(fitz.Rect(300, 300, 400, 350))
    sh.finish(color=(0, 0, 0), fill=(0, 1, 0), width=2)
    sh.commit()
    page.insert_text((100, 500), "Keep me", fontsize=12)
    return doc


async def test_batch_move_two_objects_is_one_undo_step(client, store):
    did = _save(store, _two_objects_doc())
    objs = await _objects(client, did)
    img, drw = objs["images"][0], objs["drawings"][0]
    r = await client.post(
        f"/api/pdf/{did}/objects/batch",
        json={
            "page": 0,
            "ops": [
                {"op": "move", "kind": "image", "xref": img["xref"], "bbox": img["bbox"], "new_bbox": [110, 120, 210, 170]},
                {"op": "move", "kind": "drawing", "index": drw["index"], "bbox": drw["bbox"], "new_bbox": [310, 320, 410, 370]},
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert [x["method"] for x in r.json()["results"]] == ["stream", "stream"]
    d = _open(store, did)
    assert [[round(v) for v in i["bbox"]] for i in d[0].get_image_info()] == [[110, 120, 210, 170]]
    assert [round(v) for v in d[0].get_drawings()[0]["rect"]] == [310, 320, 410, 370]
    assert "Keep me" in d[0].get_text()
    d.close()

    hist = (await client.get(f"/api/pdf/{did}/history")).json()
    assert len(hist["versions"]) == 1  # ONE snapshot for the whole batch

    assert (await client.post(f"/api/pdf/{did}/undo")).status_code == 200
    d = _open(store, did)
    assert [[round(v) for v in i["bbox"]] for i in d[0].get_image_info()] == [[100, 100, 200, 150]]
    assert [round(v) for v in d[0].get_drawings()[0]["rect"]] == [300, 300, 400, 350]
    d.close()


async def test_batch_stale_target_changes_nothing(client, store):
    did = _save(store, _two_objects_doc())
    before = (store / did / "original.pdf").read_bytes()
    objs = await _objects(client, did)
    img = objs["images"][0]
    r = await client.post(
        f"/api/pdf/{did}/objects/batch",
        json={
            "page": 0,
            "ops": [
                {"op": "move", "kind": "image", "xref": img["xref"], "bbox": img["bbox"], "new_bbox": [0, 0, 100, 50]},
                {"op": "delete", "kind": "drawing", "index": 0, "bbox": [1, 1, 2, 2]},
            ],
        },
    )
    assert r.status_code == 409
    assert (store / did / "original.pdf").read_bytes() == before
    assert not (store / did / "history.json").exists()


async def test_batch_delete_and_duplicate(client, store):
    did = _save(store, _two_objects_doc())
    objs = await _objects(client, did)
    img, drw = objs["images"][0], objs["drawings"][0]
    r = await client.post(
        f"/api/pdf/{did}/objects/batch",
        json={
            "page": 0,
            "ops": [
                {"op": "duplicate", "kind": "image", "xref": img["xref"], "bbox": img["bbox"], "dx": 10, "dy": 10},
                {"op": "delete", "kind": "drawing", "index": drw["index"], "bbox": drw["bbox"]},
            ],
        },
    )
    assert r.status_code == 200, r.text
    d = _open(store, did)
    boxes = sorted([round(v) for v in i["bbox"]] for i in d[0].get_image_info())
    assert boxes == [[100, 100, 200, 150], [110, 110, 210, 160]]
    assert d[0].get_drawings() == []
    d.close()


async def test_duplicate_drawing(client, store):
    did = _save(store, _two_objects_doc())
    drw = (await _objects(client, did))["drawings"][0]
    r = await client.post(
        f"/api/pdf/{did}/objects/batch",
        json={"page": 0, "ops": [{"op": "duplicate", "kind": "drawing", "index": drw["index"], "bbox": drw["bbox"], "dx": 20, "dy": 0}]},
    )
    assert r.status_code == 200, r.text
    d = _open(store, did)
    rects = sorted([round(v) for v in p["rect"]] for p in d[0].get_drawings())
    d.close()
    assert rects == [[300, 300, 400, 350], [320, 300, 420, 350]]


# ─── Bring to front / send to back ────────────────────────────────────────────


def _overlap_doc():
    """Green rect, then red image on top, overlapping at (150..200, 150..200)."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    sh = page.new_shape()
    sh.draw_rect(fitz.Rect(100, 100, 200, 200))
    sh.finish(color=None, fill=(0, 1, 0))
    sh.commit()
    page.insert_image(fitz.Rect(150, 150, 250, 250), stream=_png(10, 10, (255, 0, 0)))
    return doc


async def test_arrange_front_and_back(client, store):
    did = _save(store, _overlap_doc())
    assert _pixel(store, did, 175, 175)[:3] == (255, 0, 0)  # image on top initially
    objs = await _objects(client, did)
    drw, img = objs["drawings"][0], objs["images"][0]

    r = await client.post(
        f"/api/pdf/{did}/objects/arrange",
        json={"page": 0, "kind": "drawing", "index": drw["index"], "bbox": drw["bbox"], "where": "front"},
    )
    assert r.status_code == 200, r.text
    assert _pixel(store, did, 175, 175)[:3] == (0, 255, 0)  # rect now above the image
    assert _pixel(store, did, 225, 225)[:3] == (255, 0, 0)

    objs = await _objects(client, did)
    img = objs["images"][0]
    r = await client.post(
        f"/api/pdf/{did}/objects/arrange",
        json={"page": 0, "kind": "image", "xref": img["xref"], "bbox": img["bbox"], "where": "front"},
    )
    assert r.status_code == 200 and r.json()["method"] == "stream"
    assert _pixel(store, did, 175, 175)[:3] == (255, 0, 0)

    r = await client.post(
        f"/api/pdf/{did}/objects/arrange",
        json={"page": 0, "kind": "image", "xref": img["xref"], "bbox": img["bbox"], "where": "back"},
    )
    assert r.status_code == 200
    assert _pixel(store, did, 175, 175)[:3] == (0, 255, 0)
    d = _open(store, did)
    assert [[round(v) for v in i["bbox"]] for i in d[0].get_image_info()] == [[150, 150, 250, 250]]
    d.close()

    bad = await client.post(
        f"/api/pdf/{did}/objects/arrange",
        json={"page": 0, "kind": "image", "xref": img["xref"], "where": "sideways"},
    )
    assert bad.status_code == 400


# ─── Scanner units ────────────────────────────────────────────────────────────


def test_scan_paths_geometry_and_flags():
    data = (
        b"q 2 0 0 2 0 0 cm 10 10 m 20 10 l S Q\n"
        b"0 0 50 50 re W n\n"
        b"(fake 1 1 m 2 2 l S) Tj\n"
        b"5 5 m 1 0 0 1 3 3 cm 6 6 l f\n"
    )
    segs = objects._scan_paths(data)
    assert [s["op"] for s in segs] == ["S", "n", "f"]
    assert [(p.x, p.y) for p in segs[0]["points"]] == [(20, 20), (40, 20)]  # CTM applied
    assert segs[1]["clip"] and not segs[0]["clip"]
    assert segs[2]["bad"]  # cm inside a path is never wrapped
    assert data[segs[0]["start"] : segs[0]["end"]] == b"10 10 m 20 10 l S"


def test_q_balance():
    assert objects._q_balance(b"q q Q") == 1
    assert objects._q_balance(b"q (q) Q") == 0
