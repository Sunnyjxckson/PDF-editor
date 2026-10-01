"""
OCR, conversion & optimization feature router.

Endpoints (all under /api/pdf):

  GET  /ocr/languages                 installed tesseract languages (+ installable ones)
  POST /ocr/languages/install         download {code}.traineddata (tessdata_fast) into
                                      the user tessdata dir; only on an explicit UI click
  GET  /convert/capabilities          optional converters present (Microsoft Word, OCR)
  GET  /{doc_id}/ocr/detect           per-page scan detection
  POST /{doc_id}/ocr                  start an OCR job (returns job_id)
  GET  /ocr/jobs/{job_id}             poll OCR job progress / result
  GET  /{doc_id}/export/{fmt}         docx | txt | md | html | png | jpg | xlsx | csv
                                      (html: ?layout=positioned|reflow)
  POST /create                        images / text / markdown / docx / pdf -> new document
                                      (docx_engine=builtin|word)
  POST /{doc_id}/compress             optimize with a preset, report before/after sizes

Coordinates: everything here is in PDF points with PyMuPDF's top-left origin
(the same space ``page.get_text("words")`` returns).  Nothing in this module
accepts screen pixels.

Every mutating endpoint calls ``snapshot(doc_id, operation)`` before it writes,
so the shared undo/redo stack works.
"""

from __future__ import annotations

import asyncio
import html as _html
import io
import os
import re
import shutil
import statistics
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Literal, Optional

import fitz  # PyMuPDF
from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend import advanced_ops
from backend.advanced_ops import snapshot

router = APIRouter(prefix="/api/pdf")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_CREATE_BYTES = int(os.environ.get("MAX_FILE_SIZE_MB", "50")) * 1024 * 1024

# Per-document locks so an OCR job and a compress call never write the same
# file at the same time.
_doc_locks: dict[str, threading.Lock] = {}
_doc_locks_guard = threading.Lock()


def _lock_for(doc_id: str) -> threading.Lock:
    with _doc_locks_guard:
        lk = _doc_locks.get(doc_id)
        if lk is None:
            lk = _doc_locks[doc_id] = threading.Lock()
        return lk


def _upload_dir() -> Path:
    # Read at call time so it always matches the directory snapshot() uses.
    return Path(advanced_ops.UPLOAD_DIR)


def _doc_path(doc_id: str) -> Path:
    if not _UUID_RE.match(doc_id):
        raise HTTPException(status_code=400, detail="Invalid document ID")
    p = _upload_dir() / doc_id / "original.pdf"
    if not p.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    return p


def _open(doc_id: str) -> tuple[fitz.Document, Path]:
    p = _doc_path(doc_id)
    try:
        return fitz.open(str(p)), p
    except Exception as e:  # pragma: no cover - corrupt file
        raise HTTPException(status_code=422, detail=f"Cannot open PDF: {e}")


def _atomic_save(doc: fitz.Document, path: Path, **kw) -> None:
    tmp = str(path) + ".convert.tmp"
    doc.save(tmp, **kw)
    os.replace(tmp, str(path))


def _download(content: bytes, media_type: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


# ═════════════════════════════════════════════════════════════════════════════
#  OCR
# ═════════════════════════════════════════════════════════════════════════════

_LANG_RE = re.compile(r"^[A-Za-z_]{3,20}(\+[A-Za-z_]{3,20}){0,4}$")
_CJK_LANGS = {"chi_sim", "chi_tra", "jpn", "kor", "chi_sim_vert", "chi_tra_vert", "jpn_vert", "kor_vert"}


# Directory where languages installed from the UI are stored.  Tesseract reads
# ONE tessdata directory per run, so this directory is kept a superset of the
# system one (system *.traineddata are symlinked in on install) and is
# preferred whenever it holds every requested language.
_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _user_tessdata_dir() -> Path:
    env = os.environ.get("PDF_EDITOR_TESSDATA_DIR")
    return Path(env) if env else _BACKEND_DIR / "tessdata"


def _system_tessdata_dirs() -> list[str]:
    """Tessdata directories that ship with the host install, best first.

    TESSDATA_PREFIX (when set) wins, exactly as it does for the tesseract CLI.
    """
    out: list[str] = []
    env = os.environ.get("TESSDATA_PREFIX")
    if env and Path(env).is_dir():
        out.append(env)
    try:
        td = fitz.get_tessdata()
        if td and Path(td).is_dir():
            out.append(td)
    except Exception:
        pass
    for cand in ("/opt/homebrew/share/tessdata", "/usr/local/share/tessdata",
                 "/usr/share/tesseract-ocr/5/tessdata", "/usr/share/tesseract-ocr/4.00/tessdata",
                 "/usr/share/tessdata"):
        if Path(cand).is_dir():
            out.append(cand)
    seen, uniq = set(), []
    for d in out:
        k = os.path.realpath(d)
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    return uniq


def _tessdata_dirs() -> list[str]:
    """Every tessdata directory we know about (user dir first if it has data)."""
    dirs = _system_tessdata_dirs()
    ud = _user_tessdata_dir()
    if ud.is_dir() and any(ud.glob("*.traineddata")):
        dirs = [str(ud)] + [d for d in dirs if os.path.realpath(d) != os.path.realpath(ud)]
    return dirs


def _langs_in(d: str) -> set[str]:
    return {p.stem for p in Path(d).glob("*.traineddata") if p.exists()}


def _tessdata(language: Optional[str] = None) -> Optional[str]:
    """Pick the tessdata directory to hand tesseract.

    With ``language`` (e.g. "eng+spa") the first directory that holds every
    requested language wins.  If they are split across directories (system
    eng + user-installed spa), the user directory is completed with symlinks
    so a single directory serves the whole request.
    """
    dirs = _tessdata_dirs()
    if not dirs:
        return None
    if not language:
        return dirs[0]
    need = set(language.split("+"))
    for d in dirs:
        if need <= _langs_in(d):
            return d
    ud = _user_tessdata_dir()
    if ud.is_dir():
        _link_system_languages(ud)
        if need <= _langs_in(str(ud)):
            return str(ud)
    return dirs[0]


def _link_system_languages(ud: Path) -> None:
    """Make ``ud`` a superset of the system tessdata (symlink, copy fallback)."""
    for d in _system_tessdata_dirs():
        if os.path.realpath(d) == os.path.realpath(ud):
            continue
        for f in Path(d).glob("*.traineddata"):
            dst = ud / f.name
            if dst.exists():
                continue
            try:
                os.symlink(f, dst)
            except OSError:
                try:
                    shutil.copyfile(f, dst)
                except OSError:
                    pass


def _available_languages() -> list[str]:
    langs: set[str] = set()
    for d in _tessdata_dirs():
        langs |= _langs_in(d)
    return sorted(l for l in langs if l not in ("osd", "snum", "equ"))


# Languages offered for one-click install (codes from tesseract-ocr/tessdata_fast).
INSTALLABLE_LANGUAGES: dict[str, str] = {
    "afr": "Afrikaans", "ara": "Arabic", "aze": "Azerbaijani", "bel": "Belarusian",
    "ben": "Bengali", "bul": "Bulgarian", "cat": "Catalan", "ces": "Czech",
    "chi_sim": "Chinese (Simplified)", "chi_tra": "Chinese (Traditional)", "dan": "Danish",
    "deu": "German", "ell": "Greek", "eng": "English", "est": "Estonian", "eus": "Basque",
    "fas": "Persian", "fin": "Finnish", "fra": "French", "gle": "Irish", "glg": "Galician",
    "heb": "Hebrew", "hin": "Hindi", "hrv": "Croatian", "hun": "Hungarian", "ind": "Indonesian",
    "isl": "Icelandic", "ita": "Italian", "jpn": "Japanese", "kor": "Korean", "lat": "Latin",
    "lav": "Latvian", "lit": "Lithuanian", "msa": "Malay", "nld": "Dutch", "nor": "Norwegian",
    "pol": "Polish", "por": "Portuguese", "ron": "Romanian", "rus": "Russian", "slk": "Slovak",
    "slv": "Slovenian", "spa": "Spanish", "sqi": "Albanian", "srp": "Serbian", "swa": "Swahili",
    "swe": "Swedish", "tam": "Tamil", "tel": "Telugu", "tha": "Thai", "tur": "Turkish",
    "ukr": "Ukrainian", "urd": "Urdu", "vie": "Vietnamese",
}
TESSDATA_FAST_URL = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/{code}.traineddata"
MAX_TRAINEDDATA_BYTES = 40 * 1024 * 1024  # largest tessdata_fast model is ~3 MB; generous cap


def _fetch_traineddata(code: str) -> bytes:
    """Download one model from the official tessdata_fast repo (mocked in tests)."""
    import httpx

    url = TESSDATA_FAST_URL.format(code=code)
    buf = bytearray()
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Download failed ({r.status_code}) for {code}")
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > MAX_TRAINEDDATA_BYTES:
                raise HTTPException(status_code=502, detail="Downloaded language file is too large")
    return bytes(buf)


def install_language(code: str) -> dict:
    if code not in INSTALLABLE_LANGUAGES:
        raise HTTPException(status_code=400, detail=f"Unknown language code: {code}")
    if code in _available_languages():
        return {"installed": code, "already_installed": True, "languages": _available_languages()}
    data = _fetch_traineddata(code)
    # A traineddata file is a tesseract archive; reject HTML error pages etc.
    if len(data) < 1024 or data[:15].lower().startswith((b"<!doctype", b"<html")):
        raise HTTPException(status_code=502, detail=f"Downloaded file for {code} is not a tesseract model")
    ud = _user_tessdata_dir()
    ud.mkdir(parents=True, exist_ok=True)
    tmp = ud / f".{code}.traineddata.part"
    tmp.write_bytes(data)
    os.replace(tmp, ud / f"{code}.traineddata")
    _link_system_languages(ud)
    return {"installed": code, "already_installed": False, "bytes": len(data),
            "languages": _available_languages()}


def _validate_language(language: str) -> str:
    if not _LANG_RE.match(language):
        raise HTTPException(status_code=400, detail="Invalid language code")
    avail = set(_available_languages())
    missing = [l for l in language.split("+") if l not in avail]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Tesseract language(s) not installed: {', '.join(missing)}. "
                   f"Installed: {', '.join(sorted(avail)) or 'none'}",
        )
    return language


def _page_scan_info(page: fitz.Page) -> dict:
    """Classify a page: visible text chars, invisible (OCR) chars, image coverage."""
    visible = 0
    invisible = 0
    try:
        for span in page.get_texttrace():
            n = sum(1 for c in span["chars"] if chr(c[0]).strip())
            if span.get("type") == 3:
                invisible += n
            else:
                visible += n
    except Exception:
        visible = len(page.get_text().strip())

    page_area = abs(page.rect) or 1.0
    covered = fitz.Rect()
    img_area = 0.0
    try:
        for info in page.get_image_info():
            r = fitz.Rect(info["bbox"]) & page.rect
            if not r.is_empty:
                img_area += abs(r)
                covered |= r
    except Exception:
        pass
    coverage = min(1.0, img_area / page_area)
    is_scanned = coverage >= 0.5 and visible < 50
    return {
        "page": page.number,
        "text_chars": visible,
        "ocr_text_chars": invisible,
        "image_coverage": round(coverage, 3),
        "is_scanned": is_scanned,
        "has_ocr_layer": invisible > 0,
        "needs_ocr": is_scanned and invisible == 0,
    }


@router.get("/ocr/languages")
async def ocr_languages():
    installed = _available_languages()
    return {
        "languages": installed,
        "tessdata": bool(_tessdata()),
        "installable": [{"code": c, "name": n} for c, n in sorted(INSTALLABLE_LANGUAGES.items(), key=lambda kv: kv[1])
                        if c not in installed],
        "names": {c: INSTALLABLE_LANGUAGES.get(c, c) for c in installed},
    }


class InstallLanguageRequest(BaseModel):
    code: str


@router.post("/ocr/languages/install")
async def ocr_install_language(req: InstallLanguageRequest):
    """Download ``{code}.traineddata`` from tesseract-ocr/tessdata_fast into the
    user tessdata dir.  Only ever called from the UI's explicit Install button."""
    return await asyncio.to_thread(install_language, req.code.strip())


@router.get("/{doc_id}/ocr/detect")
async def ocr_detect(doc_id: str):
    doc, _ = _open(doc_id)
    try:
        pages = [_page_scan_info(p) for p in doc]
    finally:
        doc.close()
    return {
        "pages": pages,
        "scanned_pages": [p["page"] for p in pages if p["is_scanned"]],
        "needs_ocr": [p["page"] for p in pages if p["needs_ocr"]],
    }


def _sample_colors(pix: fitz.Pixmap, rect: fitz.Rect, scale: float) -> tuple[tuple, tuple]:
    """Return (background_rgb, foreground_rgb) in 0..1 floats for a word box.

    Background = median of the box border pixels, foreground = mean of the
    darkest-contrast 15% of pixels inside the box.
    """
    x0 = max(0, int(rect.x0 * scale)); y0 = max(0, int(rect.y0 * scale))
    x1 = min(pix.width - 1, int(rect.x1 * scale)); y1 = min(pix.height - 1, int(rect.y1 * scale))
    if x1 <= x0 or y1 <= y0:
        return (1.0, 1.0, 1.0), (0.0, 0.0, 0.0)

    def px(x, y):
        v = pix.pixel(x, y)
        return v[:3] if len(v) >= 3 else (v[0], v[0], v[0])

    border = []
    stepx = max(1, (x1 - x0) // 20); stepy = max(1, (y1 - y0) // 8)
    for x in range(x0, x1 + 1, stepx):
        border.append(px(x, y0)); border.append(px(x, y1))
    for y in range(y0, y1 + 1, stepy):
        border.append(px(x0, y)); border.append(px(x1, y))
    bg = tuple(statistics.median(c[i] for c in border) for i in range(3))
    inner = [px(x, y) for x in range(x0, x1 + 1, stepx) for y in range(y0, y1 + 1, stepy)]
    inner.sort(key=lambda c: -sum(abs(c[i] - bg[i]) for i in range(3)))
    top = inner[: max(1, len(inner) * 15 // 100)]
    fg = tuple(sum(c[i] for c in top) / len(top) for i in range(3))
    return tuple(v / 255 for v in bg), tuple(v / 255 for v in fg)


def _ocr_words(page: fitz.Page, language: str, dpi: int, tessdata: Optional[str]) -> list[tuple]:
    """OCR the page (no modification) and return PyMuPDF-style word tuples."""
    kw = {"full": True, "language": language, "dpi": dpi}
    if tessdata:
        kw["tessdata"] = tessdata
    tp = page.get_textpage_ocr(**kw)
    return page.get_text("words", textpage=tp)


def _pil_from_xref(doc: fitz.Document, xref: int):
    """Decode an image XObject into a Pillow image (L or RGB), or None."""
    from PIL import Image

    try:
        pix = fitz.Pixmap(doc, xref)
    except Exception:
        return None
    if pix.alpha:
        pix = fitz.Pixmap(pix, 0)
    if pix.colorspace is None or pix.n not in (1, 3):
        try:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        except Exception:
            return None
    mode = "L" if pix.n == 1 else "RGB"
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples)


def _word_pixel_box(r: fitz.Rect, inv: fitz.Matrix, w: int, h: int) -> tuple[int, int, int, int]:
    """Page rect -> integer pixel box in an image placed with matrix ``~inv``."""
    pts = [p * inv for p in (r.tl, r.tr, r.bl, r.br)]
    xs = [p.x * w for p in pts]
    ys = [p.y * h for p in pts]
    return (int(max(0, min(xs))), int(max(0, min(ys))),
            int(min(w, max(xs) + 0.999)), int(min(h, max(ys) + 0.999)))


def _ink_stats(arr, box, bg):
    """(ink colour 0..1, ink-pixel fraction, first ink row, baseline row) inside box.

    ``arr`` is an HxWxC float array; rows are relative to the box top.
    """
    import numpy as np

    x0, y0, x1, y1 = box
    sub = arr[y0:y1, x0:x1]
    if sub.size == 0:
        return (0.0, 0.0, 0.0), 0.0, None, None
    diff = np.abs(sub - np.asarray(bg, dtype=float)).sum(axis=2)
    thr = max(60.0, float(diff.max()) * 0.45)
    ink = diff >= thr
    frac = float(ink.mean())
    if not ink.any():
        return (0.0, 0.0, 0.0), 0.0, None, None
    col = tuple(float(v) / 255 for v in sub[ink].mean(axis=0))
    if len(col) == 1:
        col = (col[0], col[0], col[0])
    rows = ink.sum(axis=1)
    nz = np.nonzero(rows)[0]
    top = int(nz[0])
    # Baseline: last row that still carries a solid share of the ink (rows
    # below it are descenders only).
    strong = np.nonzero(rows >= max(1, rows.max() * 0.3))[0]
    base = int(strong[-1]) + 1
    return col, frac, top, base


def _inpaint_words(page: fitz.Page, rects: list[fitz.Rect]) -> dict[int, dict]:
    """Erase the scanned glyphs under each OCR word box in the page image(s).

    For every word lying on a raster image: background = median of a ring of
    pixels just outside the box, the box is filled with it and the edge is
    feathered, then the image XObject is rewritten with page.replace_image.
    Returns {word index: {"color", "ink", "top", "base"}} measured on the
    ORIGINAL pixels (page units for top/base); words not on an image are absent.
    """
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    doc = page.parent
    out: dict[int, dict] = {}
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        return out
    done_xrefs: set[int] = set()
    for info in sorted(infos, key=lambda i: -abs(fitz.Rect(i["bbox"]))):
        xref = info.get("xref") or 0
        if xref <= 0 or xref in done_xrefs or info.get("has-mask"):
            continue
        bbox = fitz.Rect(info["bbox"])
        idx = [i for i, r in enumerate(rects) if i not in out
               and fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2) in bbox]
        if not idx:
            continue
        mat = fitz.Matrix(info["transform"])
        if abs(mat.a * mat.d - mat.b * mat.c) < 1e-9:
            continue
        inv = ~mat
        img = _pil_from_xref(doc, xref)
        if img is None:
            continue
        W, H = img.size
        arr = np.asarray(img, dtype=float)
        if arr.ndim == 2:
            arr = arr[:, :, None]
        fill = img.copy()
        mask = Image.new("L", img.size, 0)
        fd = ImageDraw.Draw(fill)
        md = ImageDraw.Draw(mask)
        px_per_pt = max(W / max(bbox.width, 1e-6), H / max(bbox.height, 1e-6))
        feather = max(1.0, 0.6 * px_per_pt)
        for i in idx:
            box = _word_pixel_box(rects[i], inv, W, H)
            bx0, by0, bx1, by1 = box
            if bx1 - bx0 < 2 or by1 - by0 < 2:
                continue
            hpx = by1 - by0
            pad = max(2, int(round(hpx * 0.12)))
            ring = max(2, int(round(hpx * 0.15)))
            ox0, oy0 = max(0, bx0 - pad - ring), max(0, by0 - pad - ring)
            ox1, oy1 = min(W, bx1 + pad + ring), min(H, by1 + pad + ring)
            ix0, iy0 = max(0, bx0 - pad), max(0, by0 - pad)
            ix1, iy1 = min(W, bx1 + pad), min(H, by1 + pad)
            outer = arr[oy0:oy1, ox0:ox1].reshape(-1, arr.shape[2])
            sel = np.ones((oy1 - oy0, ox1 - ox0), dtype=bool)
            sel[iy0 - oy0:iy1 - oy0, ix0 - ox0:ix1 - ox0] = False
            ring_px = arr[oy0:oy1, ox0:ox1][sel]
            if ring_px.size == 0:
                ring_px = outer
            bg = tuple(float(v) for v in np.median(ring_px, axis=0))
            col, frac, top, base = _ink_stats(arr, (ix0, iy0, ix1, iy1), bg)
            fill_val = int(round(bg[0])) if img.mode == "L" else tuple(int(round(v)) for v in bg[:3])
            fd.rectangle([ix0, iy0, ix1 - 1, iy1 - 1], fill=fill_val)
            md.rectangle([ix0, iy0, ix1 - 1, iy1 - 1], fill=255)
            meas = {"color": col, "ink": frac, "bg": tuple(v / 255 for v in bg)}
            if top is not None:
                # rows -> page y (axis-aligned images; good enough for scans)
                to_pt = lambda row: (fitz.Point(0, (iy0 + row) / H) * mat).y
                meas["top"] = to_pt(top)
                meas["base"] = to_pt(base)
            out[i] = meas
        # Feather: grow the mask a little then blur so the patch edge blends.
        r = int(round(feather))
        mask = mask.filter(ImageFilter.MaxFilter(2 * r + 1)).filter(ImageFilter.GaussianBlur(feather))
        # Keep the inner area fully replaced (blur would let ink bleed back).
        hard = Image.new("L", img.size, 0)
        hd = ImageDraw.Draw(hard)
        for i in idx:
            if i in out:
                bx0, by0, bx1, by1 = _word_pixel_box(rects[i], inv, W, H)
                hd.rectangle([bx0 - 1, by0 - 1, bx1, by1], fill=255)
        mask = Image.fromarray(np.maximum(np.asarray(mask), np.asarray(hard)))
        # fill outside the hard boxes only differs within the feather band
        result = Image.composite(fill, img, mask)
        if info.get("bpc") == 1 and img.mode == "L":
            # bilevel scans (CCITT/JBIG2) stay bilevel so the file does not balloon
            result = result.point(lambda v: 255 if v >= 128 else 0).convert("1")
        buf = io.BytesIO()
        flt = doc.xref_get_key(xref, "Filter")[1] or ""
        if "DCT" in flt:
            result.save(buf, "JPEG", quality=92)
        else:
            result.save(buf, "PNG", optimize=False)
        page.replace_image(xref, stream=buf.getvalue())
        done_xrefs.add(xref)
    return out


def _ocr_page(page: fitz.Page, language: str, dpi: int, mode: str, tessdata: Optional[str]) -> int:
    """OCR one page in place and add a text layer aligned to the scan.

    mode="searchable": invisible text (render mode 3) over the untouched image —
                       Acrobat's "Recognize Text / Searchable Image".
    mode="editable":   Acrobat's "Edit scanned text": the glyph pixels under
                       each recognized word are inpainted out of the page image
                       (background estimated from the box border, feathered),
                       and real text in a size / weight / colour matched to the
                       scan is placed there, so the Edit Text tool can change it.
    Returns the number of words added.
    """
    rotation = page.rotation
    if rotation:
        page.set_rotation(0)  # work in unrotated space; restored below
    try:
        words = _ocr_words(page, language, dpi, tessdata)
        if not words:
            return 0

        # Do not duplicate text the page already has natively.
        native = [fitz.Rect(w[:4]) for w in page.get_text("words")]
        keep = []
        for w in words:
            text = w[4]
            r = fitz.Rect(w[:4])
            if not text.strip() or r.is_empty or r.height < 1 or r.width < 0.5:
                continue
            center = fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
            if any(center in nr for nr in native):
                continue
            keep.append(w)
        if not keep:
            return 0

        use_cjk = any(l in _CJK_LANGS for l in language.split("+"))
        fontname = "china-s" if use_cjk else "helv"
        font = fitz.Font("cjk") if use_cjk else fitz.Font("helv")
        bold_font = font if use_cjk else fitz.Font("hebo")
        asc, desc = font.ascender, font.descender
        span_h = (asc - desc) or 1.0
        rects = [fitz.Rect(w[:4]) for w in keep]

        shape = page.new_shape()
        added = 0
        if mode != "editable":
            for w, r in zip(keep, rects):
                text = w[4]
                fs = max(1.0, r.height / span_h)
                baseline = fitz.Point(r.x0, r.y1 + desc * fs)
                natural = font.text_length(text, fontsize=fs) or 1.0
                sx = max(0.2, min(5.0, r.width / natural))
                shape.insert_text(baseline, text, fontsize=fs, fontname=fontname,
                                  render_mode=3, morph=(baseline, fitz.Matrix(sx, 1)))
                added += 1
            if added:
                shape.commit(overlay=True)
            return added

        # ── editable: inpaint the scan, then place matched real text ──
        meas = _inpaint_words(page, rects)

        # Per OCR line: one font size and baseline (from measured ink), so the
        # words of a line share a baseline like real typeset text does.
        cap = 0.72 if not use_cjk else 0.88  # Helvetica cap/ascender height per em
        lines: dict[tuple, list[int]] = {}
        for i, w in enumerate(keep):
            lines.setdefault((w[5], w[6]), []).append(i)
        inks = [m["ink"] for m in meas.values() if m.get("ink")]
        med_ink = statistics.median(inks) if inks else 0.0
        fallback_pix = None
        meas_fg = (0.0, 0.0, 0.0)
        for key, idxs in lines.items():
            tops = [meas[i]["top"] for i in idxs if "top" in meas.get(i, {})]
            bases = [meas[i]["base"] for i in idxs if "base" in meas.get(i, {})]
            if tops and bases:
                base_y = statistics.median(bases)
                fs = max(1.0, (base_y - min(tops)) / cap)
            else:
                r0 = rects[idxs[0]]
                fs = max(1.0, r0.height / span_h)
                base_y = r0.y1 + desc * fs
            idxs.sort(key=lambda i: rects[i].x0)
            on_img = [i for i in idxs if i in meas]
            for i in idxs:
                if i in meas:
                    continue
                # Not on a raster image: cover with the sampled background instead.
                if fallback_pix is None:
                    fallback_pix = page.get_pixmap(dpi=150, colorspace=fitz.csRGB, alpha=False)
                bg, fg = _sample_colors(fallback_pix, rects[i], 150 / 72)
                meas_fg = fg
                shape.draw_rect(rects[i] + (-0.5, -0.5, 0.5, 0.5))
                shape.finish(color=None, fill=bg, width=0)
            if on_img:
                cols = [meas[i]["color"] for i in on_img]
                fg = tuple(statistics.median(c[k] for c in cols) for k in range(3))
                n_bold = sum(1 for i in on_img if med_ink > 0 and len(inks) >= 3
                             and meas[i]["ink"] > med_ink * 1.45)
                bold = (not use_cjk) and n_bold * 2 > len(on_img)
            else:
                fg, bold = meas_fg, False
            # One span per OCR line (words joined by spaces) so the Edit Text
            # tool sees a normal editable line; a single horizontal scale makes
            # it span the same width as the scanned line.
            text = " ".join(keep[i][4] for i in idxs)
            x0 = rects[idxs[0]].x0
            width = rects[idxs[-1]].x1 - x0
            f = bold_font if bold else font
            fname = "hebo" if bold else fontname
            baseline = fitz.Point(x0, base_y)
            natural = f.text_length(text, fontsize=fs) or 1.0
            ratio = width / natural
            if 0.98 <= ratio <= 1.02:
                shape.insert_text(baseline, text, fontsize=fs, fontname=fname, color=fg, render_mode=0)
            else:
                sx = max(0.6, min(1.6, ratio))
                shape.insert_text(baseline, text, fontsize=fs, fontname=fname, color=fg,
                                  render_mode=0, morph=(baseline, fitz.Matrix(sx, 1)))
            added += len(idxs)
        if added:
            shape.commit(overlay=True)
        return added
    finally:
        if rotation:
            page.set_rotation(rotation)


class OCRRequest(BaseModel):
    language: str = "eng"
    pages: Optional[list[int]] = None  # None = auto (pages that need OCR)
    dpi: int = Field(300, ge=72, le=600)
    mode: Literal["searchable", "editable"] = "searchable"
    force: bool = False  # OCR even if the page already has text / an OCR layer


_jobs: dict[str, dict] = {}
_jobs_guard = threading.Lock()


def _set_job(job_id: str, **kw):
    with _jobs_guard:
        _jobs[job_id].update(kw)


def run_ocr(doc_id: str, req: OCRRequest, job_id: Optional[str] = None) -> dict:
    """Run OCR synchronously. Used by the job thread (and directly by tests)."""
    lock = _lock_for(doc_id)
    with lock:
        path = _doc_path(doc_id)
        doc = fitz.open(str(path))
        try:
            infos = [_page_scan_info(p) for p in doc]
            if req.pages is None:
                targets = [i["page"] for i in infos if (i["is_scanned"] if req.force else i["needs_ocr"])]
            else:
                bad = [p for p in req.pages if p < 0 or p >= len(doc)]
                if bad:
                    raise HTTPException(status_code=400, detail=f"Invalid page(s): {bad}")
                targets = sorted(set(req.pages))
                if not req.force:
                    targets = [p for p in targets if not infos[p]["has_ocr_layer"]]

            result = {"pages_processed": [], "words_added": 0, "skipped": [], "total": len(targets)}
            if job_id:
                _set_job(job_id, total=len(targets), done=0, status="running")
            if not targets:
                return result

            snapshot(doc_id, f"OCR ({req.mode}, {req.language})")
            td = _tessdata(req.language)
            for n, pno in enumerate(targets):
                added = _ocr_page(doc[pno], req.language, req.dpi, req.mode, td)
                result["words_added"] += added
                (result["pages_processed"] if added else result["skipped"]).append(pno)
                if job_id:
                    _set_job(job_id, done=n + 1, current_page=pno)
            _atomic_save(doc, path, garbage=3, deflate=True)
            return result
        finally:
            doc.close()


def _job_runner(job_id: str, doc_id: str, req: OCRRequest):
    try:
        res = run_ocr(doc_id, req, job_id)
        _set_job(job_id, status="done", result=res, finished=time.time())
    except HTTPException as e:
        _set_job(job_id, status="error", error=str(e.detail), finished=time.time())
    except Exception as e:  # pragma: no cover - tesseract failures etc.
        _set_job(job_id, status="error", error=str(e), finished=time.time())


@router.post("/{doc_id}/ocr")
async def start_ocr(doc_id: str, req: OCRRequest = Body(default_factory=OCRRequest)):
    _doc_path(doc_id)
    _validate_language(req.language)
    if not _tessdata():
        raise HTTPException(status_code=503, detail="Tesseract language data not found on server")
    job_id = uuid.uuid4().hex
    with _jobs_guard:
        # drop finished jobs older than an hour
        now = time.time()
        for k in [k for k, v in _jobs.items() if v.get("finished") and now - v["finished"] > 3600]:
            del _jobs[k]
        _jobs[job_id] = {"job_id": job_id, "doc_id": doc_id, "status": "queued",
                         "done": 0, "total": None, "created": now}
    threading.Thread(target=_job_runner, args=(job_id, doc_id, req), daemon=True).start()
    return {"job_id": job_id}


@router.get("/ocr/jobs/{job_id}")
async def ocr_job(job_id: str):
    with _jobs_guard:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        out = dict(job)
    total = out.get("total")
    out["progress"] = 1.0 if out["status"] == "done" else (
        (out.get("done", 0) / total) if total else 0.0)
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Export
# ═════════════════════════════════════════════════════════════════════════════

def _page_text(page: fitz.Page) -> str:
    return page.get_text("text", sort=True)


def export_txt(doc: fitz.Document) -> bytes:
    # Form feed between pages, the pdftotext convention.
    return "\f".join(_page_text(p).rstrip() + "\n" for p in doc).encode("utf-8")


def _md_escape(s: str) -> str:
    return re.sub(r"([\\`*_\[\]])", r"\\\1", s)


_BULLET_RE = re.compile(r"^\s*([•●▪–\-\*·])\s+")
_NUM_RE = re.compile(r"^\s*(\d{1,3})[.)]\s+")


def _table_to_md(rows: list[list]) -> str:
    rows = [[("" if c is None else str(c)).replace("\n", " ").replace("|", "\\|").strip() for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _find_tables(page: fitz.Page) -> list:
    try:
        return list(page.find_tables().tables)
    except Exception:
        return []


def export_markdown(doc: fitz.Document) -> str:
    # Body font size = most common size by character count across the doc.
    sizes: dict[float, int] = {}
    for page in doc:
        for b in page.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                for s in l["spans"]:
                    k = round(s["size"], 1)
                    sizes[k] = sizes.get(k, 0) + len(s["text"].strip())
    body = max(sizes, key=sizes.get) if sizes else 11.0

    parts: list[str] = []
    for page in doc:
        tables = _find_tables(page)
        tbl_rects = [fitz.Rect(t.bbox) for t in tables]
        items: list[tuple[float, float, str]] = []  # (y, x, markdown)
        for t in tables:
            md = _table_to_md(t.extract())
            if md:
                items.append((t.bbox[1], t.bbox[0], md))

        for b in page.get_text("dict", sort=True)["blocks"]:
            if b.get("type") != 0:
                continue
            br = fitz.Rect(b["bbox"])
            if any(abs(br & tr) > 0.5 * abs(br) for tr in tbl_rects if abs(br)):
                continue
            lines_md = []
            max_size = 0.0
            for l in b["lines"]:
                segs = []
                for s in l["spans"]:
                    txt = s["text"]
                    if not txt.strip():
                        segs.append(txt)
                        continue
                    max_size = max(max_size, s["size"])
                    esc = _md_escape(txt.strip())
                    lead = " " if txt[:1].isspace() else ""
                    trail = " " if txt[-1:].isspace() else ""
                    bold = bool(s["flags"] & 16) or "bold" in s["font"].lower()
                    ital = bool(s["flags"] & 2) or "italic" in s["font"].lower() or "oblique" in s["font"].lower()
                    mono = bool(s["flags"] & 8) or "mono" in s["font"].lower() or "courier" in s["font"].lower()
                    if mono:
                        esc = f"`{txt.strip()}`"
                    elif bold and ital:
                        esc = f"***{esc}***"
                    elif bold:
                        esc = f"**{esc}**"
                    elif ital:
                        esc = f"*{esc}*"
                    segs.append(lead + esc + trail)
                line = re.sub(r"\s+", " ", "".join(segs)).strip()
                if line:
                    lines_md.append(line)
            if not lines_md:
                continue
            ratio = max_size / body if body else 1
            plain = re.sub(r"[*`]", "", " ".join(lines_md))
            if ratio >= 1.15 and len(plain) < 200:
                level = 1 if ratio >= 1.6 else 2 if ratio >= 1.3 else 3
                heading = re.sub(r"^\*+|\*+$", "", " ".join(lines_md)).strip()
                items.append((b["bbox"][1], b["bbox"][0], "#" * level + " " + heading))
                continue
            out_lines = []
            para: list[str] = []
            for ln in lines_md:
                raw = re.sub(r"^[*`]+", "", ln)
                m_b = _BULLET_RE.match(raw)
                m_n = _NUM_RE.match(raw)
                if m_b or m_n:
                    if para:
                        out_lines.append(" ".join(para)); para = []
                    if m_b:
                        out_lines.append("- " + _BULLET_RE.sub("", raw, count=1))
                    else:
                        out_lines.append(f"{m_n.group(1)}. " + _NUM_RE.sub("", raw, count=1))
                elif out_lines and out_lines[-1].startswith(("- ",)) and not para:
                    out_lines[-1] += " " + ln  # wrapped list item
                else:
                    para.append(ln)
            if para:
                out_lines.append(" ".join(para))
            items.append((b["bbox"][1], b["bbox"][0], "\n".join(out_lines)))
        items.sort(key=lambda t: (round(t[0], 0), t[1]))
        page_md = "\n\n".join(i[2] for i in items)
        parts.append(page_md)
    return "\n\n---\n\n".join(p for p in parts if p.strip()).strip() + "\n"


def export_html(doc: fitz.Document, title: str) -> str:
    pages = []
    for page in doc:
        pages.append(page.get_text("html"))
    css = ("body{background:#e5e7eb;margin:0;padding:24px;font-family:sans-serif}"
           "body>div{background:#fff;margin:0 auto 24px auto;box-shadow:0 1px 4px rgba(0,0,0,.2);"
           "position:relative;overflow:hidden}")
    return (f"<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>{_html.escape(title)}</title>"
            f"<style>{css}</style></head><body>\n" + "\n".join(pages) + "\n</body></html>\n")


def _span_html(s: dict) -> str:
    txt = _html.escape(s["text"])
    if not s["text"].strip():
        return txt
    font = s["font"].lower()
    bold = bool(s["flags"] & 16) or "bold" in font
    ital = bool(s["flags"] & 2) or "italic" in font or "oblique" in font
    mono = bool(s["flags"] & 8) or "mono" in font or "courier" in font
    if mono:
        txt = f"<code>{txt}</code>"
    if bold:
        txt = f"<strong>{txt}</strong>"
    if ital:
        txt = f"<em>{txt}</em>"
    return txt


def export_html_semantic(doc: fitz.Document, title: str) -> str:
    """Reflowable HTML: headings, paragraphs, lists, tables and images (data URIs)
    in reading order, with no absolute positioning — suitable for re-use,
    screen readers and small screens."""
    import base64

    sizes: dict[float, int] = {}
    for page in doc:
        for b in page.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                for sp in l["spans"]:
                    k = round(sp["size"], 1)
                    sizes[k] = sizes.get(k, 0) + len(sp["text"].strip())
    body_size = max(sizes, key=sizes.get) if sizes else 11.0

    parts: list[str] = []
    for page in doc:
        tables = _find_tables(page)
        tbl_rects = [fitz.Rect(t.bbox) for t in tables]
        items: list[tuple[float, float, str, str]] = []  # (y, x, kind, html)
        for t in tables:
            rows = [r for r in t.extract() if any(c for c in r)]
            if not rows:
                continue
            h = ["<table>"]
            for ri, r in enumerate(rows):
                tag = "th" if ri == 0 else "td"
                cells = "".join(f"<{tag}>{_html.escape(('' if c is None else str(c)).replace(chr(10), ' ').strip())}</{tag}>"
                                for c in r)
                h.append(f"<tr>{cells}</tr>")
            h.append("</table>")
            items.append((t.bbox[1], t.bbox[0], "table", "".join(h)))

        for b in page.get_text("dict", sort=True)["blocks"]:
            br = fitz.Rect(b["bbox"])
            if b.get("type") == 1:
                data = b.get("image")
                if not data or br.width < 4 or br.height < 4:
                    continue
                ext = (b.get("ext") or "png").lower()
                if ext not in ("png", "jpeg", "jpg", "gif"):
                    try:
                        data = fitz.Pixmap(data).tobytes("png"); ext = "png"
                    except Exception:
                        continue
                mime = "jpeg" if ext in ("jpg", "jpeg") else ext
                uri = f"data:image/{mime};base64," + base64.b64encode(data).decode("ascii")
                items.append((br.y0, br.x0, "img",
                              f'<figure><img src="{uri}" alt="" width="{round(br.width)}" '
                              f'height="{round(br.height)}"/></figure>'))
                continue
            if b.get("type") != 0:
                continue
            if any(abs(br & tr) > 0.5 * abs(br) for tr in tbl_rects if abs(br)):
                continue
            lines: list[str] = []
            max_size = 0.0
            for l in b["lines"]:
                segs = []
                for sp in l["spans"]:
                    if sp["text"].strip():
                        max_size = max(max_size, sp["size"])
                    segs.append(_span_html(sp))
                line = re.sub(r"\s+", " ", "".join(segs)).strip()
                if line:
                    lines.append(line)
            if not lines:
                continue
            plain = re.sub(r"<[^>]+>", "", " ".join(lines))
            ratio = max_size / body_size if body_size else 1
            if ratio >= 1.15 and len(plain) < 200:
                level = 1 if ratio >= 1.6 else 2 if ratio >= 1.3 else 3
                inner = re.sub(r"</?strong>", "", " ".join(lines))
                items.append((br.y0, br.x0, "h", f"<h{level}>{inner}</h{level}>"))
                continue
            para: list[str] = []
            y = br.y0
            for ln in lines:
                raw = _html.unescape(re.sub(r"<[^>]+>", "", ln))
                m_b = _BULLET_RE.match(raw)
                m_n = _NUM_RE.match(raw)
                if m_b or m_n:
                    if para:
                        items.append((y, br.x0, "p", "<p>" + " ".join(para) + "</p>")); para = []
                    marker = (m_b or m_n).group(0)
                    # strip the marker from the html version of the line
                    esc_marker = _html.escape(marker.strip())
                    li = re.sub(r"^((?:<[^>]+>)*)\s*" + re.escape(esc_marker) + r"\s*", r"\1", ln, count=1)
                    items.append((y, br.x0, "ul" if m_b else "ol", f"<li>{li}</li>"))
                    y += 0.01
                elif items and items[-1][2] in ("ul", "ol") and not para and items[-1][0] >= br.y0:
                    yy, xx, kind, h = items[-1]
                    items[-1] = (yy, xx, kind, h[:-5] + " " + ln + "</li>")  # wrapped list item
                else:
                    para.append(ln)
            if para:
                items.append((y, br.x0, "p", "<p>" + " ".join(para) + "</p>"))
        items.sort(key=lambda t: (round(t[0], 0), t[1]))
        out: list[str] = []
        open_list: Optional[str] = None
        for _y, _x, kind, h in items:
            if kind in ("ul", "ol"):
                if open_list != kind:
                    if open_list:
                        out.append(f"</{open_list}>")
                    out.append(f"<{kind}>")
                    open_list = kind
                out.append(h)
                continue
            if open_list:
                out.append(f"</{open_list}>")
                open_list = None
            out.append(h)
        if open_list:
            out.append(f"</{open_list}>")
        if out:
            parts.append(f'<section class="page" aria-label="Page {page.number + 1}">\n' + "\n".join(out) + "\n</section>")
    css = ("body{max-width:46rem;margin:2rem auto;padding:0 1rem;font-family:system-ui,sans-serif;line-height:1.5;color:#111}"
           "img{max-width:100%;height:auto}figure{margin:1rem 0}"
           "table{border-collapse:collapse;margin:1rem 0}td,th{border:1px solid #999;padding:.25rem .5rem;text-align:left}"
           "th{background:#f3f4f6}section.page+section.page{border-top:1px solid #ddd;margin-top:2rem;padding-top:1rem}"
           "code{font-family:ui-monospace,monospace}")
    return (f'<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{_html.escape(title)}</title><style>{css}</style></head>\n<body>\n<main>\n"
            + "\n".join(parts) + "\n</main>\n</body></html>\n")


def export_images_zip(doc: fitz.Document, fmt: str, dpi: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            if fmt == "jpg":
                data = pix.tobytes("jpeg", jpg_quality=90)
            else:
                data = pix.tobytes("png")
            zf.writestr(f"page_{page.number + 1:04d}.{fmt}", data)
    return buf.getvalue()


def _all_tables(doc: fitz.Document) -> list[tuple[int, int, list[list]]]:
    out = []
    for page in doc:
        for ti, t in enumerate(_find_tables(page)):
            rows = t.extract()
            if rows and any(any(c for c in r) for r in rows):
                out.append((page.number, ti, rows))
    return out


def _coerce_cell(v):
    if v is None:
        return None
    s = str(v).strip()
    if re.fullmatch(r"-?\d+", s) and len(s) < 16 and not (len(s) > 1 and s.startswith("0")):
        return int(s)
    if re.fullmatch(r"-?\d*\.\d+", s):
        try:
            return float(s)
        except ValueError:
            pass
    return s


def export_xlsx(doc: fitz.Document) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    tables = _all_tables(doc)
    if not tables:
        raise HTTPException(status_code=422, detail="No tables detected in this document")
    wb = Workbook()
    wb.remove(wb.active)
    for pno, ti, rows in tables:
        ws = wb.create_sheet(title=f"Page {pno + 1} Table {ti + 1}"[:31])
        for r in rows:
            ws.append([_coerce_cell(c) for c in r])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for col in ws.columns:
            width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            ws.column_dimensions[col[0].column_letter].width = min(60, max(8, width + 2))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_csv_zip(doc: fitz.Document) -> bytes:
    import csv

    tables = _all_tables(doc)
    if not tables:
        raise HTTPException(status_code=422, detail="No tables detected in this document")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for pno, ti, rows in tables:
            s = io.StringIO()
            csv.writer(s).writerows([["" if c is None else c for c in r] for r in rows])
            zf.writestr(f"page_{pno + 1}_table_{ti + 1}.csv", s.getvalue())
    return buf.getvalue()


def export_docx(pdf_path: Path) -> bytes:
    from pdf2docx import Converter

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.docx"
        cv = Converter(str(pdf_path))
        try:
            cv.convert(str(out))
        finally:
            cv.close()
        return out.read_bytes()


ExportFormat = Literal["docx", "txt", "md", "html", "png", "jpg", "xlsx", "csv"]

_MEDIA = {
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "txt": ("text/plain; charset=utf-8", "txt"),
    "md": ("text/markdown; charset=utf-8", "md"),
    "html": ("text/html; charset=utf-8", "html"),
    "png": ("application/zip", "png.zip"),
    "jpg": ("application/zip", "jpg.zip"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "csv": ("application/zip", "csv.zip"),
}


@router.get("/{doc_id}/export/{fmt}")
async def export_document(doc_id: str, fmt: ExportFormat, dpi: int = 150, filename: str = "document",
                          layout: Literal["positioned", "reflow"] = "positioned"):
    path = _doc_path(doc_id)
    dpi = max(36, min(600, dpi))
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", os.path.splitext(os.path.basename(filename))[0])[:80] or "document"
    media, ext = _MEDIA[fmt]

    def work() -> bytes:
        with _lock_for(doc_id):
            if fmt == "docx":
                return export_docx(path)
            doc = fitz.open(str(path))
            try:
                if fmt == "txt":
                    return export_txt(doc)
                if fmt == "md":
                    return export_markdown(doc).encode("utf-8")
                if fmt == "html":
                    if layout == "reflow":
                        return export_html_semantic(doc, base).encode("utf-8")
                    return export_html(doc, base).encode("utf-8")
                if fmt in ("png", "jpg"):
                    return export_images_zip(doc, fmt, dpi)
                if fmt == "xlsx":
                    return export_xlsx(doc)
                return export_csv_zip(doc)
            finally:
                doc.close()

    data = await asyncio.to_thread(work)
    return _download(data, media, f"{base}.{ext}")


# ═════════════════════════════════════════════════════════════════════════════
#  Create PDF from files
# ═════════════════════════════════════════════════════════════════════════════

_PAGE_SIZES = {"letter": fitz.paper_rect("letter"), "a4": fitz.paper_rect("a4")}
_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
_TEXT_EXT = {".txt", ".text"}
_MD_EXT = {".md", ".markdown"}

_STORY_CSS = """
body { font-family: sans-serif; font-size: 11pt; line-height: 1.35; color: #111; }
h1 { font-size: 22pt; margin: 0 0 8pt 0; } h2 { font-size: 17pt; margin: 10pt 0 6pt 0; }
h3 { font-size: 14pt; margin: 8pt 0 4pt 0; } h4,h5,h6 { font-size: 12pt; }
p { margin: 0 0 6pt 0; } pre, code { font-family: monospace; font-size: 9.5pt; }
pre { background: #f3f4f6; padding: 6pt; }
table { border-collapse: collapse; } td, th { border: 1px solid #999; padding: 3pt 5pt; }
blockquote { margin-left: 18pt; color: #444; }
"""


def _inline_md(s: str) -> str:
    s = _html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", s)
    s = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"<b>{m.group(1) or m.group(2)}</b>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)|(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)",
               lambda m: f"<i>{m.group(1) or m.group(2)}</i>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', s)
    return s


def markdown_to_html(md: str) -> str:
    """Small, dependency-free Markdown -> HTML (headings, lists, code, tables, quotes)."""
    out: list[str] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    para: list[str] = []

    def flush():
        if para:
            out.append("<p>" + _inline_md(" ".join(para)) + "</p>")
            para.clear()

    while i < len(lines):
        ln = lines[i]
        st = ln.strip()
        if st.startswith("```"):
            flush()
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i]); i += 1
            out.append("<pre>" + _html.escape("\n".join(code)) + "</pre>")
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", st)
        if m:
            flush()
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline_md(m.group(2).strip())}</h{n}>")
            i += 1; continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", st):
            flush(); out.append("<hr/>"); i += 1; continue
        if st.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not re.match(r"^\|?\s*:?-{2,}", lines[i].strip()):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            html_rows = []
            for ri, r in enumerate(rows):
                tag = "th" if ri == 0 else "td"
                html_rows.append("<tr>" + "".join(f"<{tag}>{_inline_md(c)}</{tag}>" for c in r) + "</tr>")
            out.append("<table>" + "".join(html_rows) + "</table>")
            continue
        if re.match(r"^\s*([-*+]|\d+[.)])\s+", ln):
            flush()
            ordered = bool(re.match(r"^\s*\d+[.)]", ln))
            tag = "ol" if ordered else "ul"
            items = []
            while i < len(lines) and re.match(r"^\s*([-*+]|\d+[.)])\s+", lines[i]):
                items.append("<li>" + _inline_md(re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", lines[i])) + "</li>")
                i += 1
            out.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue
        if st.startswith(">"):
            flush()
            q = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                q.append(lines[i].strip()[1:].strip()); i += 1
            out.append("<blockquote>" + _inline_md(" ".join(q)) + "</blockquote>")
            continue
        if not st:
            flush(); i += 1; continue
        para.append(st)
        i += 1
    flush()
    return "\n".join(out)


def text_to_html(text: str) -> str:
    return "\n".join(f"<p>{_html.escape(p) if p.strip() else '&#160;'}</p>"
                     for p in text.replace("\r\n", "\n").split("\n"))


def html_to_pdf(html_body: str, page_rect: fitz.Rect) -> fitz.Document:
    story = fitz.Story(html=html_body, user_css=_STORY_CSS)
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    where = page_rect + (54, 54, -54, -54)
    more = 1
    while more:
        dev = writer.begin_page(page_rect)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return fitz.open("pdf", buf.getvalue())


# ─── DOCX -> PDF ─────────────────────────────────────────────────────────────
#
# python-docx walks the package; each body block becomes HTML that fitz.Story
# lays out.  Page size/margins come from the first section, inline images
# are pulled from the package into a fitz.Archive, headers/footers are laid
# out separately on every page (with PAGE / NUMPAGES fields substituted),
# hard page breaks start a new page, and table borders / cell shading are
# carried as inline CSS.

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_PAGE_FIELD = "\u0001PAGE\u0001"
_NUMPAGES_FIELD = "\u0001NUMPAGES\u0001"
_PAGE_BREAK = "<!--PAGEBREAK-->"

_SERIF_FONTS = ("times", "georgia", "cambria", "garamond", "book antiqua", "palatino", "baskerville",
                "minion", "century", "bookman", "charter", "constantia", "serif")
_MONO_FONTS = ("courier", "consolas", "menlo", "monaco", "lucida console", "source code", "mono")


def _css_font_family(name: Optional[str]) -> str:
    n = (name or "").lower()
    if any(k in n for k in _MONO_FONTS):
        return "monospace"
    if any(k in n for k in _SERIF_FONTS):
        return "serif"
    return "sans-serif"


def _w(tag: str) -> str:
    return f"{{{_W_NS}}}{tag}"


def _wval(el, tag: str, attr: str = "val") -> Optional[str]:
    if el is None:
        return None
    c = el.find(_w(tag))
    return None if c is None else c.get(_w(attr))


def _border_css(b) -> Optional[str]:
    """<w:top w:val="single" w:sz="8" w:color="FF0000"/> -> CSS border value."""
    if b is None:
        return None
    val = b.get(_w("val")) or "single"
    if val in ("nil", "none"):
        return "none"
    sz = b.get(_w("sz"))
    width = max(0.5, int(sz) / 8) if sz and sz.isdigit() else 0.5
    color = b.get(_w("color")) or "000000"
    color = "#000000" if color == "auto" else f"#{color}"
    style = "double" if val == "double" else "dashed" if "dash" in val else "dotted" if "dot" in val else "solid"
    if style == "double":
        width = max(width, 2.25)
    return f"{width:.2f}pt {style} {color}"


class _DocxRenderer:
    def __init__(self, data: bytes, archive: "fitz.Archive"):
        import docx

        self.d = docx.Document(io.BytesIO(data))
        self.archive = archive
        self.n_images = 0
        self.numfmt_cache: dict[tuple[str, str], str] = {}
        self.default_size = 11.0
        self.default_font = "Calibri"
        try:
            st = self.d.styles["Normal"]
            if st.font.size:
                self.default_size = st.font.size.pt
            if st.font.name:
                self.default_font = st.font.name
        except Exception:
            pass

    # ── styles
    def _style_attr(self, style, attr):
        seen = 0
        while style is not None and seen < 10:
            v = getattr(style.font, attr, None)
            if v is not None:
                return v
            style = style.base_style
            seen += 1
        return None

    def _list_kind(self, p) -> Optional[tuple[str, int]]:
        """('ul'|'ol', level) for list paragraphs, else None."""
        ppr = p._p.pPr
        num_id, ilvl = None, 0
        if ppr is not None and ppr.numPr is not None:
            if ppr.numPr.numId is not None:
                num_id = str(ppr.numPr.numId.val)
            if ppr.numPr.ilvl is not None:
                ilvl = int(ppr.numPr.ilvl.val)
        sname = (p.style.name if p.style is not None else "") or ""
        if num_id is None or num_id == "0":
            if "List Number" in sname:
                return "ol", 0
            if "List" in sname and "Paragraph" not in sname:
                return "ul", 0
            return None
        return self._num_fmt(num_id, ilvl), ilvl

    def _num_fmt(self, num_id: str, ilvl: int) -> str:
        key = (num_id, str(ilvl))
        if key in self.numfmt_cache:
            return self.numfmt_cache[key]
        kind = "ul"
        try:
            numbering = self.d.part.numbering_part.element
            num = next((n for n in numbering.findall(_w("num")) if n.get(_w("numId")) == num_id), None)
            abs_id = _wval(num, "abstractNumId")
            absn = next((a for a in numbering.findall(_w("abstractNum")) if a.get(_w("abstractNumId")) == abs_id), None)
            if absn is not None:
                lvl = next((l for l in absn.findall(_w("lvl")) if l.get(_w("ilvl")) == str(ilvl)), None)
                fmt = _wval(lvl, "numFmt")
                kind = "ul" if fmt in (None, "bullet", "none") else "ol"
        except Exception:
            pass
        self.numfmt_cache[key] = kind
        return kind

    # ── inline content
    def _image_html(self, el, part) -> str:
        blip = el.find(".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip")
        if blip is None:
            return ""
        rid = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
        try:
            img_part = part.related_parts[rid]
            blob = img_part.blob
        except Exception:
            return ""
        ext = os.path.splitext(str(getattr(img_part, "partname", "")))[1].lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".bmp"):
            try:  # emf/wmf/tiff etc. -> PNG via Pillow when it can read it
                from PIL import Image
                im = Image.open(io.BytesIO(blob)); im.load()
                b = io.BytesIO(); im.convert("RGBA").save(b, "PNG"); blob = b.getvalue(); ext = ".png"
            except Exception:
                return ""
        name = f"docx_img{self.n_images}{ext}"
        self.n_images += 1
        self.archive.add(blob, name)
        ext_el = el.find(".//{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}extent")
        size = ""
        if ext_el is not None:
            try:
                wpt = int(ext_el.get("cx")) / 12700
                hpt = int(ext_el.get("cy")) / 12700
                size = f' width="{wpt:.1f}" height="{hpt:.1f}"'
            except (TypeError, ValueError):
                pass
        return f'<img src="{name}"{size}/>'

    def _run_html(self, r_el, p, part) -> str:
        from docx.text.run import Run

        run = Run(r_el, p)
        out = []
        for child in r_el:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "t":
                out.append(_html.escape(child.text or ""))
            elif tag == "tab":
                out.append("&#160;&#160;&#160;&#160;")
            elif tag in ("br", "cr"):
                if child.get(_w("type")) == "page":
                    out.append(_PAGE_BREAK)
                else:
                    out.append("<br/>")
            elif tag in ("drawing", "pict", "object"):
                out.append(self._image_html(child, part))
        txt = "".join(out)
        if not txt:
            return ""
        if _PAGE_BREAK in txt and txt.strip() == _PAGE_BREAK:
            return txt
        style = []
        f = run.font
        if f.size:
            style.append(f"font-size:{f.size.pt:g}pt")
        if f.name:
            style.append(f"font-family:{_css_font_family(f.name)}")
        try:
            if f.color is not None and f.color.type is not None and f.color.rgb is not None:
                style.append(f"color:#{f.color.rgb}")
        except Exception:
            pass
        try:
            if f.highlight_color is not None:
                hl = {3: "#00ff00", 7: "#ffff00", 4: "#ff00ff", 5: "#0000ff", 6: "#ff0000", 16: "#c0c0c0"}
                c = hl.get(int(f.highlight_color))
                if c:
                    style.append(f"background-color:{c}")
        except Exception:
            pass
        if style:
            txt = f'<span style="{";".join(style)}">{txt}</span>'
        if run.bold:
            txt = f"<b>{txt}</b>"
        if run.italic:
            txt = f"<i>{txt}</i>"
        if run.underline:
            txt = f"<u>{txt}</u>"
        if f.strike:
            txt = f"<s>{txt}</s>"
        if f.superscript:
            txt = f"<sup>{txt}</sup>"
        if f.subscript:
            txt = f"<sub>{txt}</sub>"
        return txt

    def _inline_html(self, container, p, part) -> str:
        """Runs, hyperlinks and fields (PAGE / NUMPAGES) of a paragraph."""
        out = []
        field_instr: Optional[str] = None
        in_result = False
        skip_result = False
        for child in container:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "r":
                fc = child.find(_w("fldChar"))
                if fc is not None:
                    t = fc.get(_w("fldCharType"))
                    if t == "begin":
                        field_instr, in_result, skip_result = "", False, False
                    elif t == "separate":
                        in_result = True
                        ins = (field_instr or "").strip().upper()
                        if ins.startswith("PAGE"):
                            out.append(_PAGE_FIELD); skip_result = True
                        elif ins.startswith("NUMPAGES"):
                            out.append(_NUMPAGES_FIELD); skip_result = True
                    elif t == "end":
                        ins = (field_instr or "").strip().upper()
                        if not in_result:  # field without a cached result
                            if ins.startswith("PAGE"):
                                out.append(_PAGE_FIELD)
                            elif ins.startswith("NUMPAGES"):
                                out.append(_NUMPAGES_FIELD)
                        field_instr, in_result, skip_result = None, False, False
                    continue
                it = child.find(_w("instrText"))
                if it is not None and field_instr is not None and not in_result:
                    field_instr += it.text or ""
                    continue
                if skip_result:
                    continue
                out.append(self._run_html(child, p, part))
            elif tag == "hyperlink":
                out.append(self._inline_html(child, p, part))
            elif tag == "fldSimple":
                ins = (child.get(_w("instr")) or "").strip().upper()
                if ins.startswith("PAGE"):
                    out.append(_PAGE_FIELD)
                elif ins.startswith("NUMPAGES"):
                    out.append(_NUMPAGES_FIELD)
                else:
                    out.append(self._inline_html(child, p, part))
            elif tag in ("smartTag", "ins", "sdt", "sdtContent", "customXml"):
                out.append(self._inline_html(child, p, part))
        return "".join(out)

    def _para_style(self, p) -> str:
        css = []
        pf = p.paragraph_format
        al = p.alignment if p.alignment is not None else (p.style.paragraph_format.alignment if p.style is not None else None)
        if al is not None:
            m = {0: "left", 1: "center", 2: "right", 3: "justify"}.get(int(al))
            if m:
                css.append(f"text-align:{m}")
        try:
            if pf.left_indent:
                css.append(f"margin-left:{pf.left_indent.pt:.1f}pt")
            if pf.first_line_indent:
                css.append(f"text-indent:{pf.first_line_indent.pt:.1f}pt")
            sb = pf.space_before if pf.space_before is not None else (p.style.paragraph_format.space_before if p.style is not None else None)
            sa = pf.space_after if pf.space_after is not None else (p.style.paragraph_format.space_after if p.style is not None else None)
            if sb is not None:
                css.append(f"margin-top:{sb.pt:.1f}pt")
            if sa is not None:
                css.append(f"margin-bottom:{sa.pt:.1f}pt")
        except Exception:
            pass
        size = self._style_attr(p.style, "size")
        if size is not None:
            css.append(f"font-size:{size.pt:g}pt")
        fname = self._style_attr(p.style, "name")
        if fname:
            css.append(f"font-family:{_css_font_family(fname)}")
        shd = p._p.pPr.find(_w("shd")) if p._p.pPr is not None else None
        if shd is not None and (shd.get(_w("fill")) or "auto") not in ("auto", ""):
            css.append(f"background-color:#{shd.get(_w('fill'))}")
        return ";".join(css)

    def paragraph_html(self, p, part) -> tuple[str, Optional[tuple[str, int]]]:
        style = (p.style.name if p.style is not None else "") or ""
        body = self._inline_html(p._p, p, part)
        css = self._para_style(p)
        st = f' style="{css}"' if css else ""
        lst = self._list_kind(p)
        if lst:
            return f"<li{st}>{body or '&#160;'}</li>", lst
        m = re.match(r"Heading (\d)", style)
        if style == "Title":
            return f"<h1{st}>{body}</h1>", None
        if m:
            n = min(6, max(1, int(m.group(1))))
            return f"<h{n}{st}>{body}</h{n}>", None
        return f"<p{st}>{body or '&#160;'}</p>", None

    # ── tables
    def _table_borders(self, tbl_el) -> dict[str, Optional[str]]:
        borders: dict[str, Optional[str]] = {}

        def read(tblpr):
            if tblpr is None:
                return
            tb = tblpr.find(_w("tblBorders"))
            if tb is None:
                return
            for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
                b = tb.find(_w(side))
                if b is None and side == "left":
                    b = tb.find(_w("start"))
                if b is None and side == "right":
                    b = tb.find(_w("end"))
                if b is not None:
                    borders[side] = _border_css(b)

        tblpr = tbl_el.find(_w("tblPr"))
        sid = _wval(tblpr, "tblStyle")
        if sid:
            try:
                st = self.d.styles.element.get_by_id(sid)
                chain = []
                while st is not None and len(chain) < 10:
                    chain.append(st)
                    base = _wval(st, "basedOn")
                    st = self.d.styles.element.get_by_id(base) if base else None
                for s_el in reversed(chain):
                    read(s_el.find(_w("tblPr")))
            except Exception:
                pass
        read(tblpr)
        return borders

    def table_html(self, tbl_el, part) -> str:
        from docx.table import Table

        tbl = Table(tbl_el, self.d)
        borders = self._table_borders(tbl_el)
        rows = tbl_el.findall(_w("tr"))
        # rowspan bookkeeping for vMerge
        grid: list[list[dict]] = []
        for r_el in rows:
            row = []
            col = 0
            for tc in r_el.findall(_w("tc")):
                tcpr = tc.find(_w("tcPr"))
                span = int(_wval(tcpr, "gridSpan") or 1)
                vm = tcpr.find(_w("vMerge")) if tcpr is not None else None
                vstate = None if vm is None else (vm.get(_w("val")) or "continue")
                row.append({"tc": tc, "col": col, "span": span, "vmerge": vstate, "rowspan": 1})
                col += span
            grid.append(row)
        for ri, row in enumerate(grid):
            for cell in row:
                if cell["vmerge"] == "restart":
                    n = 1
                    for rj in range(ri + 1, len(grid)):
                        nxt = next((c for c in grid[rj] if c["col"] == cell["col"]), None)
                        if nxt is not None and nxt["vmerge"] == "continue":
                            n += 1
                        else:
                            break
                    cell["rowspan"] = n
        n_rows = len(grid)
        out = ['<table style="border-collapse:collapse;width:100%">']
        for ri, row in enumerate(grid):
            out.append("<tr>")
            last_col = max((c["col"] + c["span"] for c in row), default=0)
            for cell in row:
                if cell["vmerge"] == "continue":
                    continue
                tc = cell["tc"]
                tcpr = tc.find(_w("tcPr"))
                css = ["padding:2pt 4pt", "vertical-align:top"]
                sides = {
                    "top": borders.get("top") if ri == 0 else borders.get("insideH"),
                    "bottom": borders.get("bottom") if ri + cell["rowspan"] >= n_rows else borders.get("insideH"),
                    "left": borders.get("left") if cell["col"] == 0 else borders.get("insideV"),
                    "right": borders.get("right") if cell["col"] + cell["span"] >= last_col else borders.get("insideV"),
                }
                tcb = tcpr.find(_w("tcBorders")) if tcpr is not None else None
                if tcb is not None:
                    for side in ("top", "left", "bottom", "right"):
                        b = tcb.find(_w(side))
                        if b is not None:
                            sides[side] = _border_css(b)
                for side, v in sides.items():
                    if v and v != "none":
                        css.append(f"border-{side}:{v}")
                shd = tcpr.find(_w("shd")) if tcpr is not None else None
                if shd is not None and (shd.get(_w("fill")) or "auto") not in ("auto", ""):
                    css.append(f"background-color:#{shd.get(_w('fill'))}")
                tcw = tcpr.find(_w("tcW")) if tcpr is not None else None
                if tcw is not None and tcw.get(_w("type")) == "dxa" and (tcw.get(_w("w")) or "").isdigit():
                    css.append(f"width:{int(tcw.get(_w('w'))) / 20:.1f}pt")
                attrs = ""
                if cell["span"] > 1:
                    attrs += f' colspan="{cell["span"]}"'
                if cell["rowspan"] > 1:
                    attrs += f' rowspan="{cell["rowspan"]}"'
                inner = self.blocks_html(tc, part, cell_mode=True)
                out.append(f'<td{attrs} style="{";".join(css)}">{inner}</td>')
            out.append("</tr>")
        out.append("</table>")
        return "".join(out)

    # ── block containers (body, cell, header, footer)
    def blocks_html(self, container_el, part, cell_mode: bool = False) -> str:
        from docx.text.paragraph import Paragraph

        out: list[str] = []
        list_stack: list[tuple[str, int]] = []

        def close_lists(to_level: int = -1):
            while list_stack and list_stack[-1][1] > to_level:
                out.append(f"</{list_stack.pop()[0]}>")

        for child in container_el.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                p = Paragraph(child, self.d)
                html_, lst = self.paragraph_html(p, part)
                if lst:
                    kind, lvl = lst
                    close_lists(lvl)
                    if not list_stack or list_stack[-1][1] < lvl or list_stack[-1][0] != kind:
                        if list_stack and list_stack[-1][1] == lvl:
                            out.append(f"</{list_stack.pop()[0]}>")
                        out.append(f"<{kind}>")
                        list_stack.append((kind, lvl))
                    out.append(html_)
                else:
                    close_lists()
                    if cell_mode:
                        html_ = html_.replace("<p>", '<p style="margin:0">', 1)
                    out.append(html_)
            elif tag == "tbl":
                close_lists()
                out.append(self.table_html(child, part))
            elif tag == "sdt":
                content = child.find(_w("sdtContent"))
                if content is not None:
                    close_lists()
                    out.append(self.blocks_html(content, part, cell_mode))
        close_lists()
        return "\n".join(out)

    def header_footer_html(self, hf) -> str:
        try:
            if hf is None or hf.is_linked_to_previous and not hf._has_definition:
                return ""
            return self.blocks_html(hf._element, hf.part)
        except Exception:
            return ""


class DocxLayout:
    """Everything needed to paginate one DOCX."""

    def __init__(self, data: bytes):
        self.archive = fitz.Archive()
        r = _DocxRenderer(data, self.archive)
        sec = r.d.sections[0] if r.d.sections else None

        def pt(v, default):
            try:
                return float(v.pt) if v is not None else default
            except Exception:
                return default

        self.width = pt(sec.page_width if sec else None, 612.0)
        self.height = pt(sec.page_height if sec else None, 792.0)
        self.margins = (pt(sec.left_margin if sec else None, 72.0), pt(sec.top_margin if sec else None, 72.0),
                        pt(sec.right_margin if sec else None, 72.0), pt(sec.bottom_margin if sec else None, 72.0))
        self.header_dist = pt(sec.header_distance if sec else None, 36.0)
        self.footer_dist = pt(sec.footer_distance if sec else None, 36.0)
        self.body_chunks = r.blocks_html(r.d.element.body, r.d.part).split(_PAGE_BREAK)
        self.header = r.header_footer_html(sec.header) if sec else ""
        self.footer = r.header_footer_html(sec.footer) if sec else ""
        self.first_header = self.first_footer = None
        if sec is not None and sec.different_first_page_header_footer:
            self.first_header = r.header_footer_html(sec.first_page_header)
            self.first_footer = r.header_footer_html(sec.first_page_header and sec.first_page_footer)
        self.css = (_STORY_CSS + f"\nbody {{ font-size: {r.default_size:g}pt; "
                    f"font-family: {_css_font_family(r.default_font)}; margin: 0; padding: 0; }}\n"
                    "ul, ol { margin-top: 0; margin-bottom: 6pt; }\n"
                    "p { margin: 0 0 6pt 0; }\n")


def _render_docx_layout(lay: DocxLayout, total_pages: Optional[int] = None) -> bytes:
    page_rect = fitz.Rect(0, 0, lay.width, lay.height)
    l, t, r, b = lay.margins
    body_rect = fitz.Rect(l, t, lay.width - r, lay.height - b)
    if body_rect.width < 72 or body_rect.height < 72:
        body_rect = page_rect + (36, 36, -36, -36)
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    pno = 0

    def deco(html_: Optional[str], top: bool, dev):
        if not html_ or not html_.strip():
            return
        html_ = html_.replace(_PAGE_FIELD, str(pno)).replace(_NUMPAGES_FIELD, str(total_pages or pno))
        avail = fitz.Rect(l, 0, lay.width - r, lay.height)
        if top:
            area = fitz.Rect(l, lay.header_dist, lay.width - r, max(lay.header_dist + 12, t))
            st = fitz.Story(html=html_, user_css=lay.css, archive=lay.archive)
            st.place(area)
            st.draw(dev)
        else:
            probe = fitz.Story(html=html_, user_css=lay.css, archive=lay.archive)
            _, filled = probe.place(fitz.Rect(avail.x0, 0, avail.x1, lay.height))
            h = fitz.Rect(filled).height if filled else 14
            y1 = lay.height - lay.footer_dist
            area = fitz.Rect(l, y1 - h - 1, lay.width - r, y1 + 1)
            st = fitz.Story(html=html_, user_css=lay.css, archive=lay.archive)
            st.place(area)
            st.draw(dev)

    for chunk in lay.body_chunks:
        if not chunk.strip() and pno > 0:
            continue
        chunk = chunk.replace(_PAGE_FIELD, "").replace(_NUMPAGES_FIELD, "")
        story = fitz.Story(html=chunk or "<p>&#160;</p>", user_css=lay.css, archive=lay.archive)
        more = 1
        while more:
            pno += 1
            dev = writer.begin_page(page_rect)
            more, _ = story.place(body_rect)
            story.draw(dev)
            first = pno == 1 and lay.first_header is not None
            deco(lay.first_header if first else lay.header, True, dev)
            deco(lay.first_footer if first else lay.footer, False, dev)
            writer.end_page()
    writer.close()
    return buf.getvalue()


def docx_to_pdf(data: bytes) -> fitz.Document:
    lay = DocxLayout(data)
    out = _render_docx_layout(lay)
    if _NUMPAGES_FIELD in (lay.header or "") + (lay.footer or "") + (lay.first_header or "") + (lay.first_footer or ""):
        n = fitz.open("pdf", out).page_count
        out = _render_docx_layout(lay, total_pages=n)
    return fitz.open("pdf", out)


def docx_to_html(data: bytes) -> str:
    """Body of a DOCX as HTML (images omitted: they live in the Story archive)."""
    lay = DocxLayout(data)
    return "\n".join(lay.body_chunks)


# ─── Optional high-fidelity path through Microsoft Word (macOS) ──────────────

WORD_APP = Path("/Applications/Microsoft Word.app")


def word_available() -> bool:
    import sys

    return sys.platform == "darwin" and WORD_APP.exists() and shutil.which("osascript") is not None


def _osascript_word_to_pdf(src: Path, dst: Path) -> None:
    import subprocess

    script = (
        'on run argv\n'
        '  set inFile to POSIX file (item 1 of argv)\n'
        '  set outFile to (item 2 of argv)\n'
        '  tell application "Microsoft Word"\n'
        '    open inFile\n'
        '    set theDoc to active document\n'
        '    save as theDoc file name outFile file format format PDF\n'
        '    close theDoc saving no\n'
        '  end tell\n'
        'end run\n'
    )
    res = subprocess.run(["osascript", "-e", script, str(src), str(dst)],
                         capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        raise HTTPException(status_code=422, detail=f"Microsoft Word conversion failed: {res.stderr.strip()[:300]}")


def docx_to_pdf_word(data: bytes) -> fitz.Document:
    """High fidelity: let Microsoft Word itself export the PDF."""
    if not word_available():
        raise HTTPException(status_code=400, detail="Microsoft Word is not installed on the server")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.docx"
        dst = Path(td) / "out.pdf"
        src.write_bytes(data)
        _osascript_word_to_pdf(src, dst)
        if not dst.exists():
            raise HTTPException(status_code=422, detail="Microsoft Word did not produce a PDF")
        return fitz.open("pdf", dst.read_bytes())


def _image_page(dst: fitz.Document, data: bytes, page_size: str, margin: float):
    try:
        img_doc = fitz.open(stream=data)  # format sniffed from content
        pix_w, pix_h = img_doc[0].rect.width, img_doc[0].rect.height
        img_doc.close()
    except Exception:
        # Formats MuPDF can't read directly (e.g. some WEBP) -> Pillow -> PNG
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.load()
        if im.mode not in ("RGB", "L", "RGBA"):
            im = im.convert("RGB")
        b = io.BytesIO(); im.save(b, "PNG"); data = b.getvalue()
        pix_w, pix_h = im.size
    if page_size == "fit":
        page = dst.new_page(width=pix_w, height=pix_h)
        page.insert_image(page.rect, stream=data)
        return
    base = _PAGE_SIZES[page_size]
    w, h = base.width, base.height
    if pix_w > pix_h:
        w, h = h, w  # landscape page for landscape images
    page = dst.new_page(width=w, height=h)
    page.insert_image(fitz.Rect(margin, margin, w - margin, h - margin), stream=data, keep_proportion=True)


def create_pdf_from_files(files: list[tuple[str, bytes]], page_size: str = "letter",
                          margin: float = 36, docx_engine: str = "builtin") -> fitz.Document:
    if page_size not in ("letter", "a4", "fit"):
        raise HTTPException(status_code=400, detail="page_size must be letter, a4 or fit")
    if docx_engine not in ("builtin", "word"):
        raise HTTPException(status_code=400, detail="docx_engine must be builtin or word")
    out = fitz.open()
    text_rect = _PAGE_SIZES["a4" if page_size == "a4" else "letter"]
    skipped = []
    for name, data in files:
        ext = os.path.splitext(name.lower())[1]
        try:
            if ext in _IMG_EXT:
                _image_page(out, data, page_size, margin)
            elif ext == ".pdf":
                src = fitz.open("pdf", data)
                out.insert_pdf(src)
                src.close()
            elif ext in _MD_EXT:
                src = html_to_pdf(markdown_to_html(data.decode("utf-8", "replace")), text_rect)
                out.insert_pdf(src); src.close()
            elif ext in _TEXT_EXT:
                src = html_to_pdf(text_to_html(data.decode("utf-8", "replace")), text_rect)
                out.insert_pdf(src); src.close()
            elif ext == ".docx":
                src = docx_to_pdf_word(data) if docx_engine == "word" else docx_to_pdf(data)
                out.insert_pdf(src); src.close()
            else:
                skipped.append(name)
        except HTTPException:
            raise
        except Exception as e:
            out.close()
            raise HTTPException(status_code=422, detail=f"Could not convert {name}: {e}")
    if skipped:
        out.close()
        raise HTTPException(status_code=400, detail=f"Unsupported file type(s): {', '.join(skipped)}")
    if len(out) == 0:
        out.close()
        raise HTTPException(status_code=400, detail="No pages were produced")
    return out


@router.get("/convert/capabilities")
async def convert_capabilities():
    """What optional converters this server offers (checked, not assumed)."""
    return {"word": word_available(), "ocr": bool(_tessdata()), "languages": _available_languages()}


@router.post("/create")
async def create_pdf(
    files: list[UploadFile] = File(...),
    page_size: str = Form("letter"),
    filename: str = Form(""),
    docx_engine: str = Form("builtin"),
):
    """Combine images / text / markdown / docx / pdf files (in order) into a new document."""
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    payload = []
    total = 0
    for f in files:
        data = await f.read()
        total += len(data)
        if total > MAX_CREATE_BYTES:
            raise HTTPException(status_code=413, detail="Uploaded files are too large")
        payload.append((os.path.basename(f.filename or "file"), data))

    doc = await asyncio.to_thread(create_pdf_from_files, payload, page_size, 36, docx_engine)
    doc_id = str(uuid.uuid4())
    d = _upload_dir() / doc_id
    d.mkdir(parents=True)
    try:
        doc.set_metadata({**(doc.metadata or {}), "producer": "AI PDF Editor", "creator": "AI PDF Editor"})
        doc.save(str(d / "original.pdf"), garbage=3, deflate=True)
        (d / "annotations.json").write_text("{}")
        page_count = len(doc)
        metadata = doc.metadata
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    finally:
        doc.close()
    name = filename.strip() or (os.path.splitext(payload[0][0])[0] + ".pdf")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return {"id": doc_id, "filename": os.path.basename(name), "page_count": page_count, "metadata": metadata}


# ═════════════════════════════════════════════════════════════════════════════
#  Compress / optimize
# ═════════════════════════════════════════════════════════════════════════════

PRESETS = {
    "high": {"dpi": 300, "quality": 90, "subset_fonts": True},
    "balanced": {"dpi": 150, "quality": 75, "subset_fonts": True},
    "smallest": {"dpi": 96, "quality": 50, "subset_fonts": True},
}


class CompressRequest(BaseModel):
    preset: Literal["high", "balanced", "smallest"] = "balanced"
    target_dpi: Optional[int] = Field(None, ge=36, le=600)
    quality: Optional[int] = Field(None, ge=10, le=95)
    dry_run: bool = False


def _image_effective_dpi(doc: fitz.Document, xref: int, w: int, h: int) -> float:
    """Highest DPI at which this image is displayed anywhere in the document."""
    best = 0.0
    for page in doc:
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            continue
        for r in rects:
            if r.width <= 0 or r.height <= 0:
                continue
            best = max(best, w / (r.width / 72), h / (r.height / 72))
    return best


def _rewrite_images(doc: fitz.Document, target_dpi: int, quality: int) -> dict:
    from PIL import Image

    seen = set()
    smask_xrefs = set()
    for page in doc:
        for img in page.get_images(full=True):
            if img[1]:
                smask_xrefs.add(img[1])
    stats = {"images_total": 0, "images_rewritten": 0, "image_bytes_before": 0, "image_bytes_after": 0}
    for page in doc:
        for img in page.get_images(full=True):
            xref, smask = img[0], img[1]
            if xref in seen or xref in smask_xrefs:
                continue
            seen.add(xref)
            stats["images_total"] += 1
            if smask:
                continue  # keep transparency intact
            try:
                if doc.xref_get_key(xref, "ImageMask")[1] == "true":
                    continue
                orig_len = len(doc.xref_stream_raw(xref) or b"")
                pix = fitz.Pixmap(doc, xref)
            except Exception:
                continue
            stats["image_bytes_before"] += orig_len
            if pix.alpha:
                pix = fitz.Pixmap(pix, 0)
            if pix.colorspace is None or pix.n not in (1, 3):
                try:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                except Exception:
                    stats["image_bytes_after"] += orig_len
                    continue
            mode = "L" if pix.n == 1 else "RGB"
            im = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
            eff = _image_effective_dpi(doc, xref, pix.width, pix.height)
            if eff > target_dpi * 1.05:
                f = target_dpi / eff
                new_size = (max(1, round(pix.width * f)), max(1, round(pix.height * f)))
                im = im.resize(new_size, Image.LANCZOS)
            b = io.BytesIO()
            im.save(b, "JPEG", quality=quality, optimize=True)
            new = b.getvalue()
            if len(new) < orig_len * 0.95:
                page.replace_image(xref, stream=new)
                stats["images_rewritten"] += 1
                stats["image_bytes_after"] += len(new)
            else:
                stats["image_bytes_after"] += orig_len
    return stats


def compress_pdf_bytes(src: bytes, preset: str, target_dpi: Optional[int] = None,
                       quality: Optional[int] = None) -> tuple[bytes, dict]:
    cfg = dict(PRESETS[preset])
    if target_dpi:
        cfg["dpi"] = target_dpi
    if quality:
        cfg["quality"] = quality
    doc = fitz.open("pdf", src)
    try:
        stats = _rewrite_images(doc, cfg["dpi"], cfg["quality"])
        fonts_subset = False
        if cfg["subset_fonts"]:
            try:
                doc.subset_fonts()
                fonts_subset = True
            except Exception:
                fonts_subset = False
        out = doc.tobytes(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True,
                          clean=True, use_objstms=1)
    finally:
        doc.close()
    stats.update({"fonts_subset": fonts_subset, "target_dpi": cfg["dpi"], "quality": cfg["quality"]})
    return out, stats


@router.post("/{doc_id}/compress")
async def compress(doc_id: str, req: CompressRequest = Body(default_factory=CompressRequest)):
    path = _doc_path(doc_id)

    def work():
        with _lock_for(doc_id):
            before_bytes = path.read_bytes()
            out, stats = compress_pdf_bytes(before_bytes, req.preset, req.target_dpi, req.quality)
            before, after = len(before_bytes), len(out)
            applied = False
            if not req.dry_run and after < before:
                snapshot(doc_id, f"Compress ({req.preset})")
                tmp = str(path) + ".convert.tmp"
                Path(tmp).write_bytes(out)
                os.replace(tmp, str(path))
                applied = True
            return {
                "preset": req.preset,
                "before_bytes": before,
                "after_bytes": after if applied or req.dry_run else before,
                "optimized_bytes": after,
                "saved_bytes": max(0, before - after),
                "saved_percent": round(max(0, before - after) * 100 / before, 1) if before else 0.0,
                "applied": applied,
                "dry_run": req.dry_run,
                **stats,
            }

    return await asyncio.to_thread(work)
