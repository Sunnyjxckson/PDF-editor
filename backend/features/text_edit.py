"""
True in-place text editing (the Acrobat "Edit PDF" text tool).

Endpoints (all under /api/pdf):

    GET  /{doc_id}/text-edit/page/{page_num}   editable blocks/lines/spans
    POST /{doc_id}/text-edit/edit              replace text and/or restyle
    POST /{doc_id}/text-edit/move              move a block/line
    POST /{doc_id}/text-edit/delete            delete a block/line/span

How an edit works
-----------------
1. The target (block / line / span) is located in ``page.get_text("rawdict")``
   by its positional id and cross-checked against the bbox the client saw, so
   a stale UI can never edit the wrong text (409 instead).
2. The original glyphs are removed with a *redaction* that touches text only:
   ``apply_redactions(images=PDF_REDACT_IMAGE_NONE,
   graphics=PDF_REDACT_LINE_ART_NONE)`` with ``fill=False``. The redaction
   rect is a thin band through the x-height of each line, so glyphs of the
   lines above/below (whose boxes overlap vertically) are not hit, and
   backgrounds, images and vector art underneath survive untouched.
   Redact annotations that some *other* tool left pending on the page are
   stashed and restored, so they are not burned in by our apply.
3. The text is re-inserted with ``TextWriter`` using the closest font:
     a. the document's own embedded font (``doc.extract_font``) when it has
        glyphs for every character of the new text,
     b. another embedded font of the same family with the requested
        bold/italic weight (e.g. ``Arial-BoldMT`` when toggling bold on
        ``ArialMT``),
     c. otherwise the Base-14 equivalent (helv/tiro/cour + bold/italic).
   Size and colour are preserved unless overridden.
4. Paragraph (block) edits are re-laid-out by our own word wrapper inside the
   original block width, at the original first baseline, line pitch and
   alignment. If the new text needs more height it first grows into free
   space below the block, then shrinks the font in small steps (to 85%).

Coordinates
-----------
Everything crossing this API is in PDF points, top-left origin, in the
*visible* page space (``page.rect`` — rotation already applied, i.e. the space
of the rendered page image). PyMuPDF text extraction / insertion works in the
unrotated space; conversion happens here with ``page.rotation_matrix``.
Text whose direction is a multiple of 90 degrees (rotated pages, vertical
labels) is handled by laying it out in a local horizontal frame and writing it
back with a rotation ``morph``. Text at other angles is reported as
``editable: false``.

Every mutating route calls ``snapshot()`` first so undo/redo works.
"""

from __future__ import annotations

import math
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional, Union

import fitz  # PyMuPDF
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import advanced_ops
from backend.advanced_ops import snapshot

router = APIRouter(prefix="/api/pdf")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# rawdict without ligature preservation: "fi" comes back as two editable chars.
_TEXT_FLAGS = fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP

MAX_TEXT_LEN = 20000
MIN_SHRINK = 0.85  # never shrink an edited paragraph below 85% of its size

_BASE14 = {
    ("sans", False, False): "helv",
    ("sans", True, False): "hebo",
    ("sans", False, True): "heit",
    ("sans", True, True): "hebi",
    ("serif", False, False): "tiro",
    ("serif", True, False): "tibo",
    ("serif", False, True): "tiit",
    ("serif", True, True): "tibi",
    ("mono", False, False): "cour",
    ("mono", True, False): "cobo",
    ("mono", False, True): "coit",
    ("mono", True, True): "cobi",
}

_SERIF_HINTS = ("times", "serif", "roman", "georgia", "garamond", "minion", "cambria",
                "palatino", "book", "baskerville", "caslon", "bodoni", "century", "tiro")
_MONO_HINTS = ("courier", "mono", "consol", "menlo", "typewriter", "code", "cour")
_BOLD_HINTS = ("bold", "black", "heavy", "semibold", "demibold", "demi", "extrabold", "ultrabold")
_ITALIC_HINTS = ("italic", "oblique", "slanted", "inclined")
_STYLE_WORDS = re.compile(
    r"(bold|black|heavy|semibold|demibold|demi|extrabold|ultrabold|italic|oblique|"
    r"regular|roman|book|medium|light|thin|normal|plain|mt|ps|std|pro)",
)


# ─── Storage helpers (same layout as main.py / advanced_ops) ─────────────────


def _doc_path(doc_id: str) -> Path:
    if not _UUID_RE.match(doc_id or ""):
        raise HTTPException(status_code=400, detail="Invalid document ID")
    path = Path(advanced_ops.UPLOAD_DIR) / doc_id / "original.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    return path


def _open(doc_id: str) -> tuple[fitz.Document, Path]:
    path = _doc_path(doc_id)
    try:
        doc = fitz.open(str(path))
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to open document")
    if doc.needs_pass:
        doc.close()
        raise HTTPException(status_code=423, detail="Document is password-protected. Unlock it first.")
    return doc, path


def _save_in_place(doc: fitz.Document, path: Path) -> None:
    tmp = str(path) + f".{uuid.uuid4().hex}.tmp"
    try:
        # garbage=3 drops the pre-edit content stream so the old text does not
        # linger as an orphaned object.
        doc.save(tmp, garbage=3, deflate=True)
    finally:
        doc.close()
    os.replace(tmp, str(path))


def _get_page(doc: fitz.Document, page_num: int) -> fitz.Page:
    if page_num < 0 or page_num >= doc.page_count:
        doc.close()
        raise HTTPException(status_code=400, detail="Invalid page number")
    return doc[page_num]


# ─── Colour / font naming helpers ────────────────────────────────────────────


def _int_to_rgb(c: int) -> tuple[float, float, float]:
    return (((c >> 16) & 255) / 255.0, ((c >> 8) & 255) / 255.0, (c & 255) / 255.0)


def _int_to_hex(c: int) -> str:
    return f"#{(c >> 16) & 255:02x}{(c >> 8) & 255:02x}{c & 255:02x}"


def _parse_color(value: Union[str, list[float], None]) -> Optional[tuple]:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip().lstrip("#")
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
            raise HTTPException(status_code=400, detail=f"Invalid color: {value}")
        return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    if len(value) != 3:
        raise HTTPException(status_code=400, detail="Color must be [r, g, b]")
    vals = [float(v) for v in value]
    if any(v > 1.0 for v in vals):
        vals = [v / 255.0 for v in vals]
    return tuple(max(0.0, min(1.0, v)) for v in vals)


def _strip_subset(name: str) -> str:
    return re.sub(r"^[A-Z]{6}\+", "", name or "")


def _norm_font(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _strip_subset(name).lower())


def _family_key(name: str) -> str:
    n = _norm_font(name)
    n = _STYLE_WORDS.sub("", n)
    return n


def classify_font(name: str, flags: int = 0) -> tuple[str, bool, bool]:
    """(generic family, bold, italic) from a font name and span flags."""
    n = _strip_subset(name).lower()
    bold = bool(flags & 16) or any(h in n for h in _BOLD_HINTS)
    italic = bool(flags & 2) or any(h in n for h in _ITALIC_HINTS)
    if bool(flags & 8) or any(h in n for h in _MONO_HINTS):
        fam = "mono"
    elif "sans" in n or "helv" in n or "arial" in n:
        fam = "sans"
    elif any(h in n for h in _SERIF_HINTS):
        fam = "serif"
    elif flags & 4:
        fam = "serif"
    else:
        fam = "sans"
    return fam, bold, italic


# ─── Geometry: local horizontal frame per line ──────────────────────────────


@dataclass
class Frame:
    """Maps page (unrotated) coords to a local frame where the text runs +x."""
    pivot: fitz.Point
    angle: float  # degrees, text direction in page space

    @property
    def to_local(self) -> fitz.Matrix:
        return fitz.Matrix(-self.angle)

    @property
    def to_page(self) -> fitz.Matrix:
        return fitz.Matrix(self.angle)

    def pt_local(self, p) -> fitz.Point:
        p = fitz.Point(p)
        return (p - self.pivot) * self.to_local + self.pivot

    def pt_page(self, p) -> fitz.Point:
        p = fitz.Point(p)
        return (p - self.pivot) * self.to_page + self.pivot

    def rect_local(self, r) -> fitz.Rect:
        r = fitz.Rect(r)
        pts = [self.pt_local(q) for q in (r.tl, r.tr, r.bl, r.br)]
        return fitz.Rect(min(p.x for p in pts), min(p.y for p in pts),
                         max(p.x for p in pts), max(p.y for p in pts))

    def rect_page(self, r) -> fitz.Rect:
        r = fitz.Rect(r)
        pts = [self.pt_page(q) for q in (r.tl, r.tr, r.bl, r.br)]
        return fitz.Rect(min(p.x for p in pts), min(p.y for p in pts),
                         max(p.x for p in pts), max(p.y for p in pts))

    @property
    def morph(self):
        if abs(self.angle) < 1e-6:
            return None
        # TextWriter applies ``morph`` in PDF (y-up) orientation, so the
        # visual rotation is the inverse of the y-down matrix ``to_page``.
        return (self.pivot, self.to_local)


def _dir_angle(d) -> Optional[float]:
    """Angle of a line direction if it is (within 0.5 deg) a multiple of 90."""
    ang = math.degrees(math.atan2(d[1], d[0]))
    snapped = round(ang / 90.0) * 90.0
    if abs(ang - snapped) > 0.5:
        return None
    return snapped % 360.0


# ─── Extraction model ───────────────────────────────────────────────────────


@dataclass
class Char:
    c: str
    origin: fitz.Point  # page coords
    bbox: fitz.Rect     # page coords


@dataclass
class Span:
    id: str
    font: str
    size: float
    color: int
    flags: int
    chars: list[Char]
    bbox: fitz.Rect
    origin: fitz.Point

    @property
    def text(self) -> str:
        return "".join(ch.c for ch in self.chars)

    @property
    def style(self) -> tuple[str, bool, bool]:
        return classify_font(self.font, self.flags)


@dataclass
class Line:
    id: str
    bbox: fitz.Rect
    dir: tuple
    spans: list[Span]

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def origin(self) -> fitz.Point:
        return self.spans[0].origin if self.spans else self.bbox.bl


@dataclass
class Block:
    id: str
    bbox: fitz.Rect
    lines: list[Line] = field(default_factory=list)

    @property
    def spans(self) -> list[Span]:
        return [s for ln in self.lines for s in ln.spans]

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)


def _raw_lines(bi: int, b: dict) -> list[Line]:
    lines = []
    for li, ln in enumerate(b.get("lines", [])):
        spans = []
        for si, sp in enumerate(ln.get("spans", [])):
            chars = [Char(c=ch["c"], origin=fitz.Point(ch["origin"]), bbox=fitz.Rect(ch["bbox"]))
                     for ch in sp.get("chars", [])]
            if not chars:
                continue
            spans.append(Span(
                id=f"b{bi}.l{li}.s{si}", font=sp.get("font", ""), size=float(sp.get("size", 11)),
                color=int(sp.get("color", 0)), flags=int(sp.get("flags", 0)), chars=chars,
                bbox=fitz.Rect(sp["bbox"]), origin=fitz.Point(sp["origin"]),
            ))
        lines.append(Line(id=f"b{bi}.l{li}", bbox=fitz.Rect(ln["bbox"]),
                          dir=tuple(ln.get("dir", (1, 0))), spans=spans))
    return lines


def _starts_new_paragraph(prev: Line, cur: Line, pitch: Optional[float]) -> bool:
    """Should ``cur`` start a new paragraph after ``prev`` (same raw block)?"""
    a_prev, a_cur = _dir_angle(prev.dir), _dir_angle(cur.dir)
    if a_prev is None or a_cur is None or a_prev != a_cur:
        return True
    fr = Frame(pivot=fitz.Point(prev.origin), angle=a_prev)
    d = fr.pt_local(cur.origin).y - fr.pt_local(prev.origin).y
    dp, dc = _dominant_span(prev.spans), _dominant_span(cur.spans)
    size = max(dp.size, dc.size)
    if d <= size * 0.3:  # same visual line / moving up: a separate column or cell
        return True
    if abs(dp.size - dc.size) > 0.15 * size:
        return True
    if pitch is not None:
        if d > pitch * 1.35 + 0.5:
            return True
    elif d > size * 1.8:
        return True
    # a whole line in a different face (e.g. a bold heading above body text)
    if len({(s.font, s.flags) for s in prev.spans if s.text.strip()}) == 1 and \
            len({(s.font, s.flags) for s in cur.spans if s.text.strip()}) == 1 and \
            (dp.font, dp.flags) != (dc.font, dc.flags):
        return True
    return False


def extract_blocks(page: fitz.Page) -> list[Block]:
    """Editable paragraphs. PyMuPDF raw blocks are split further into real
    paragraphs (blank lines, a gap larger than the line pitch, a size or face
    change), because a raw block can be a whole page of text."""
    raw = page.get_text("rawdict", flags=_TEXT_FLAGS)
    blocks: list[Block] = []
    for bi, b in enumerate(raw.get("blocks", [])):
        if b.get("type") != 0:
            continue
        groups: list[list[Line]] = []
        cur: list[Line] = []
        pitch: Optional[float] = None
        for ln in _raw_lines(bi, b):
            if not ln.spans or not ln.text.strip():
                if cur:  # blank line ends a paragraph
                    groups.append(cur)
                    cur, pitch = [], None
                continue
            if cur and _starts_new_paragraph(cur[-1], ln, pitch):
                groups.append(cur)
                cur, pitch = [], None
            if cur and pitch is None:
                fr = Frame(pivot=fitz.Point(cur[-1].origin), angle=_dir_angle(cur[-1].dir) or 0.0)
                pitch = fr.pt_local(ln.origin).y - fr.pt_local(cur[-1].origin).y
            cur.append(ln)
        if cur:
            groups.append(cur)
        for pi, g in enumerate(groups):
            bbox = fitz.Rect()
            for ln in g:
                bbox |= ln.bbox
            blocks.append(Block(id=f"b{bi}p{pi}", bbox=bbox, lines=g))
    return blocks


def _dominant_span(spans: list[Span]) -> Span:
    weights: dict[tuple, int] = {}
    first: dict[tuple, Span] = {}
    for s in spans:
        k = (s.font, round(s.size, 1), s.color, s.flags)
        weights[k] = weights.get(k, 0) + len(s.text.strip())
        first.setdefault(k, s)
    best = max(weights, key=lambda k: weights[k])
    return first[best]


def _block_frame(blk: Block) -> Optional[Frame]:
    ang = _dir_angle(blk.lines[0].dir)
    if ang is None:
        return None
    for ln in blk.lines:
        a = _dir_angle(ln.dir)
        if a is None or abs(a - ang) > 1e-6:
            return None
    return Frame(pivot=fitz.Point(blk.lines[0].origin), angle=ang)


def _line_frame(ln: Line) -> Optional[Frame]:
    ang = _dir_angle(ln.dir)
    if ang is None:
        return None
    return Frame(pivot=fitz.Point(ln.origin), angle=ang)


@dataclass
class LocalLine:
    """A line with geometry in its block's local horizontal frame."""
    line: Line
    x0: float
    x1: float
    top: float
    baseline: float


def _local_lines(blk: Block, fr: Frame) -> list[LocalLine]:
    out = []
    for ln in blk.lines:
        r = fr.rect_local(ln.bbox)
        o = fr.pt_local(ln.origin)
        out.append(LocalLine(line=ln, x0=r.x0, x1=r.x1, top=r.y0, baseline=o.y))
    return out


def _detect_align(lls: list[LocalLine], width_left: float, width_right: float) -> str:
    if len(lls) < 2:
        return "left"
    tol = 1.5
    lefts = [l.x0 for l in lls]
    rights = [l.x1 for l in lls]
    centers = [(l.x0 + l.x1) / 2 for l in lls]
    left_aligned = max(lefts) - min(lefts) <= tol
    right_aligned = max(rights) - min(rights) <= tol
    center_aligned = max(centers) - min(centers) <= tol
    if left_aligned and len(lls) >= 3 and max(rights[:-1]) - min(rights[:-1]) <= tol:
        return "justify"
    if left_aligned:
        return "left"
    if center_aligned:
        return "center"
    if right_aligned:
        return "right"
    return "left"


# ─── Fonts ──────────────────────────────────────────────────────────────────


@dataclass
class FontChoice:
    font: fitz.Font
    source: str  # "embedded:<name>" | "base14:<code>"


class FontResolver:
    """Finds the closest usable font, preferring the document's own fonts."""

    def __init__(self, doc: fitz.Document, page: fitz.Page):
        self.doc = doc
        self.entries = []  # (xref, basefont, family_key, (fam, bold, italic))
        seen = set()
        for f in page.get_fonts(full=True):
            xref, basefont = f[0], f[3]
            if xref in seen or not xref:
                continue
            seen.add(xref)
            self.entries.append((xref, basefont, _family_key(basefont), classify_font(basefont)))
        self._cache: dict[int, Optional[fitz.Font]] = {}
        self._b14: dict[str, fitz.Font] = {}

    def _load(self, xref: int) -> Optional[fitz.Font]:
        if xref in self._cache:
            return self._cache[xref]
        font = None
        try:
            name, ext, ftype, buf = self.doc.extract_font(xref)
            if buf and ext not in ("n/a", "") and ftype != "Type3":
                font = fitz.Font(fontbuffer=buf)
        except Exception:
            font = None
        self._cache[xref] = font
        return font

    @staticmethod
    def covers(font: fitz.Font, text: str) -> bool:
        for ch in set(text):
            if ch.isspace():
                continue
            if not font.has_glyph(ord(ch)):
                return False
            # some broken subsets map a code point to an empty glyph
            if font.glyph_advance(ord(ch)) <= 0:
                return False
        return True

    def base14(self, fam: str, bold: bool, italic: bool) -> FontChoice:
        code = _BASE14[(fam, bold, italic)]
        if code not in self._b14:
            self._b14[code] = fitz.Font(code)
        return FontChoice(self._b14[code], f"base14:{code}")

    def resolve(self, span_font: str, span_flags: int, text: str, *, family: Optional[str] = None,
                bold: Optional[bool] = None, italic: Optional[bool] = None) -> FontChoice:
        o_fam, o_bold, o_italic = classify_font(span_font, span_flags)
        want_bold = o_bold if bold is None else bold
        want_italic = o_italic if italic is None else italic
        generic_override = family not in (None, "original")
        want_fam = family if generic_override else o_fam

        if not generic_override:
            # (a) the span's own font, if style is unchanged
            if want_bold == o_bold and want_italic == o_italic:
                target = _norm_font(span_font)
                for xref, basefont, _fk, _st in self.entries:
                    if _norm_font(basefont) == target:
                        f = self._load(xref)
                        if f is not None and self.covers(f, text):
                            return FontChoice(f, f"embedded:{_strip_subset(basefont)}")
            # (b) a sibling weight/style of the same family embedded in the doc
            fk = _family_key(span_font)
            if fk:
                for xref, basefont, efk, (efam, ebold, eitalic) in self.entries:
                    if efk == fk and ebold == want_bold and eitalic == want_italic:
                        f = self._load(xref)
                        if f is not None and self.covers(f, text):
                            return FontChoice(f, f"embedded:{_strip_subset(basefont)}")
        # (c) Base-14 equivalent
        return self.base14(want_fam, want_bold, want_italic)


# ─── Redaction (text-only removal) ──────────────────────────────────────────


def _band_rect_local(chars_local: list[tuple[fitz.Point, fitz.Rect]], size: float) -> Optional[fitz.Rect]:
    """Thin band through the x-height of a run of chars (local frame)."""
    vis = [(o, b) for o, b in chars_local]
    if not vis:
        return None
    x0 = min(b.x0 for _, b in vis)
    x1 = max(b.x1 for _, b in vis)
    base = vis[0][0].y
    eps = min(0.6, max(0.05, (x1 - x0) * 0.02))
    if x1 - x0 <= 2 * eps:
        eps = 0
    return fitz.Rect(x0 + eps, base - size * 0.45, x1 - eps, base - size * 0.2)


def _remove_runs(page: fitz.Page, rects: list[fitz.Rect]) -> None:
    """Remove the text under ``rects`` (unrotated page coords) only."""
    rects = [r for r in rects if r and not r.is_empty]
    if not rects:
        return
    doc = page.parent
    # Stash redact annots placed by other tools so we do not burn them in.
    stashed = []
    for annot in list(page.annots(types=[fitz.PDF_ANNOT_REDACT]) or []):
        stashed.append((fitz.Rect(annot.rect), doc.xref_object(annot.xref, compressed=False)))
        page.delete_annot(annot)
    for r in rects:
        page.add_redact_annot(r, fill=False)
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_NONE,
        graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        text=fitz.PDF_REDACT_TEXT_REMOVE,
    )
    for rect, obj in stashed:
        a = page.add_redact_annot(rect)
        try:
            doc.update_object(a.xref, obj)
        except Exception:
            pass


def _span_band(span: Span, fr: Frame, from_char: int = 0) -> Optional[fitz.Rect]:
    loc = [(fr.pt_local(c.origin), fr.rect_local(c.bbox)) for c in span.chars[from_char:]]
    band = _band_rect_local(loc, span.size)
    return fr.rect_page(band) if band is not None else None


# ─── Writing ────────────────────────────────────────────────────────────────


def _write(page: fitz.Page, fr: Frame, runs: list[tuple[fitz.Point, str, fitz.Font, float, tuple]]):
    """runs: (local origin, text, font, size, rgb), in reading order.

    Consecutive runs of one colour share a TextWriter; a colour change starts a
    new one. Writing strictly in order keeps the content stream in reading
    order, so copy/paste and search see the words in the right sequence."""
    tw, cur_rgb = None, None

    def flush():
        if tw is not None:
            tw.write_text(page, color=cur_rgb, morph=fr.morph)

    for origin, text, font, size, rgb in runs:
        if not text:
            continue
        if tw is None or rgb != cur_rgb:
            flush()
            tw, cur_rgb = fitz.TextWriter(page.rect), rgb
        tw.append(fitz.Point(origin), text, font=font, fontsize=size)
    flush()


def _reinsert_span_exact(span: Span, fr: Frame, font: fitz.Font, delta: fitz.Point,
                         runs: list, rgb: Optional[tuple] = None):
    """Re-insert every glyph at its original origin (+delta): keeps kerning,
    tracking and justification spacing exactly."""
    color = rgb if rgb is not None else _int_to_rgb(span.color)
    for ch in span.chars:
        if ch.c.isspace():
            continue
        o = fr.pt_local(ch.origin) + delta
        runs.append((o, ch.c, font, span.size, color))


# ─── Paragraph layout ───────────────────────────────────────────────────────


def _free_bottom(blocks: list[Block], blk: Block, fr: Frame, page_bottom_local: float) -> float:
    """Lowest local y the block may grow to without hitting another block."""
    me = fr.rect_local(blk.bbox)
    limit = page_bottom_local
    for other in blocks:
        if other.id == blk.id:
            continue
        r = fr.rect_local(other.bbox)
        if r.x1 <= me.x0 or r.x0 >= me.x1:
            continue  # different column
        if r.y0 >= me.y1 - 0.5:
            limit = min(limit, r.y0 - 1.0)
    return max(limit, me.y1)


def _free_right(blocks: list[Block], blk: Block, fr: Frame, page_right_local: float) -> float:
    """Rightmost local x a single-line block may grow to without hitting a neighbour."""
    me = fr.rect_local(blk.bbox)
    limit = page_right_local
    for other in blocks:
        if other.id == blk.id:
            continue
        r = fr.rect_local(other.bbox)
        if r.y1 <= me.y0 + 0.5 or r.y0 >= me.y1 - 0.5:
            continue  # different row
        if r.x0 >= me.x1 - 0.5:
            limit = min(limit, r.x0 - 4.0)
    return max(limit, me.x1)


# ─── Target lookup ──────────────────────────────────────────────────────────


def _iou(a: fitz.Rect, b: fitz.Rect) -> float:
    inter = fitz.Rect(a) & fitz.Rect(b)
    if inter.is_empty:
        return 0.0
    ia = inter.width * inter.height
    ua = a.width * a.height + b.width * b.height - ia
    return ia / ua if ua > 0 else 0.0


def _items(blocks: list[Block], kind: str):
    for blk in blocks:
        if kind == "block":
            yield blk.id, blk, blk, None, None
            continue
        for ln in blk.lines:
            if kind == "line":
                yield ln.id, blk, ln, ln, None
                continue
            for sp in ln.spans:
                yield sp.id, blk, sp, ln, sp


def _locate(blocks, kind: str, tid: Optional[str], bbox_unrot: Optional[fitz.Rect]):
    """Returns (block, line|None, span|None). Raises 404/409."""
    found = None
    for iid, blk, obj, ln, sp in _items(blocks, kind):
        if tid and iid == tid:
            found = (blk, ln, sp, obj)
            break
    if found and bbox_unrot is not None and _iou(found[3].bbox, bbox_unrot) < 0.5:
        found = None  # ids shifted since the client loaded the page
    if found is None and bbox_unrot is not None:
        best, best_iou = None, 0.0
        for iid, blk, obj, ln, sp in _items(blocks, kind):
            v = _iou(obj.bbox, bbox_unrot)
            if v > best_iou:
                best, best_iou = (blk, ln, sp, obj), v
        if best is not None and best_iou >= 0.5:
            found = best
    if found is None:
        if tid and bbox_unrot is None:
            raise HTTPException(status_code=404, detail="Text element not found")
        raise HTTPException(status_code=409, detail="Text on this page changed; reload and try again")
    return found[0], found[1], found[2]


# ─── Rich paragraph re-layout ───────────────────────────────────────────────


def _soft_breaks(lls: list[LocalLine], block_x1: float) -> list[bool]:
    """soft[i] is True when the break after line i is a word-wrap."""
    if not lls:
        return []
    width = max(block_x1 - min(l.x0 for l in lls), 1.0)
    out = []
    for prev, cur in zip(lls, lls[1:]):
        nxt = cur.line.text.strip()
        first_word = nxt.split(" ")[0] if nxt else ""
        approx_word = fitz.get_text_length(first_word + " ", fontname="helv",
                                           fontsize=cur.line.spans[0].size)
        room = block_x1 - prev.x1
        soft = (room < approx_word) and ((prev.x1 - prev.x0) >= 0.6 * width) \
            and not prev.line.text.rstrip().endswith("-")
        out.append(soft)
    out.append(False)
    return out


NL = "\n"


def _paragraph_tokens(lls: list[LocalLine], block_x1: float) -> list[tuple[str, Optional[Span]]]:
    """Words of a paragraph, each tagged with the span it came from; hard line
    breaks appear as ("\n", None)."""
    soft = _soft_breaks(lls, block_x1)
    toks: list[tuple[str, Optional[Span]]] = []
    for i, ll in enumerate(lls):
        word, wspan = "", None
        for sp in ll.line.spans:
            for ch in sp.chars:
                if ch.c.isspace():
                    if word:
                        toks.append((word, wspan))
                    word, wspan = "", None
                else:
                    if not word:
                        wspan = sp
                    word += ch.c
        if word:
            toks.append((word, wspan))
        if i < len(lls) - 1 and not soft[i]:
            toks.append((NL, None))
    return toks


def _tokens_text(toks) -> str:
    out, line = [], []
    for t, _ in toks:
        if t == NL:
            out.append(" ".join(line))
            line = []
        else:
            line.append(t)
    out.append(" ".join(line))
    return "\n".join(out)


def _restyle_tokens(orig, new_text: str):
    """Map each word of ``new_text`` to the span style of the original word
    it replaces (word-level diff), so inline bold/italic/colour survive edits."""
    import difflib

    new: list[str] = []
    for i, para in enumerate(new_text.split(NL)):
        if i:
            new.append(NL)
        new.extend(w for w in para.split(" ") if w != "")
    o_words = [t for t, _ in orig]
    styled = [sp for _, sp in orig]

    def style_near(idx: int) -> Optional[Span]:
        for j in list(range(idx, -1, -1)) + list(range(idx + 1, len(styled))):
            if 0 <= j < len(styled) and styled[j] is not None:
                return styled[j]
        return None

    out: list[tuple[str, Optional[Span]]] = []
    sm = difflib.SequenceMatcher(None, o_words, new, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        for k, j in enumerate(range(j1, j2)):
            w = new[j]
            if w == NL:
                out.append((NL, None))
            elif tag == "equal":
                out.append((w, styled[i1 + k]))
            elif tag == "replace":
                out.append((w, style_near(min(i1 + k, i2 - 1))))
            else:  # insert: inherit from the preceding original word
                out.append((w, style_near(i1 - 1 if i1 > 0 else 0)))
    return out


@dataclass
class _Item:
    text: str
    font: fitz.Font
    size: float
    rgb: tuple

    def width(self, f: float = 1.0) -> float:
        return self.font.text_length(self.text, fontsize=self.size * f)

    def space(self, f: float = 1.0) -> float:
        return self.font.text_length(" ", fontsize=self.size * f)


def _wrap_items(items: list[Optional[_Item]], width: float, f: float) -> list[tuple[list[_Item], bool]]:
    """Greedy wrap of styled words; None = hard break. Returns (line, para_end)."""
    lines: list[tuple[list[_Item], bool]] = []
    cur: list[_Item] = []
    cur_w = 0.0
    for it in items:
        if it is None:
            lines.append((cur, True))
            cur, cur_w = [], 0.0
            continue
        w = it.width(f)
        add = w if not cur else cur[-1].space(f) + w
        if cur and cur_w + add > width + 0.01:
            lines.append((cur, False))
            cur, cur_w, add = [], 0.0, w
        if not cur and w > width + 0.01:
            # single word wider than the box: hard-split by characters
            rest = it.text
            while rest:
                n = len(rest)
                while n > 1 and it.font.text_length(rest[:n], fontsize=it.size * f) > width + 0.01:
                    n -= 1
                piece = _Item(rest[:n], it.font, it.size, it.rgb)
                rest = rest[n:]
                if rest:
                    lines.append(([piece], False))
                else:
                    cur, cur_w = [piece], piece.width(f)
            continue
        cur.append(it)
        cur_w += add
    lines.append((cur, True))
    return lines


def _edit_paragraph(page, blocks, blk: Block, fr: Frame, resolver: "FontResolver", text: Optional[str],
                    fam, size_o, rgb_o, bold_o, italic_o, align_o) -> dict:
    lls = _local_lines(blk, fr)
    local_bbox = fr.rect_local(blk.bbox)
    dom = _dominant_span(blk.spans)
    orig = _paragraph_tokens(lls, local_bbox.x1)
    new_text = _tokens_text(orig) if text is None else text
    toks = _restyle_tokens(orig, new_text)

    # resolve one font per original style (checked against all its new words)
    groups: dict[tuple, list[str]] = {}
    for w, sp in toks:
        if w != NL:
            sp = sp or dom
            groups.setdefault((sp.font, sp.flags), []).append(w)
    fonts: dict[tuple, FontChoice] = {}
    for (fname, flags), words in groups.items():
        fonts[(fname, flags)] = resolver.resolve(fname, flags, "".join(words), family=fam,
                                                 bold=bold_o, italic=italic_o)
    items: list[Optional[_Item]] = []
    for w, sp in toks:
        if w == NL:
            items.append(None)
            continue
        sp = sp or dom
        items.append(_Item(w, fonts[(sp.font, sp.flags)].font, size_o or sp.size,
                           rgb_o or _int_to_rgb(sp.color)))

    x0 = min(l.x0 for l in lls)
    width = max(local_bbox.x1 - x0, 1.0)
    base_size = size_o or dom.size
    asc_ratio = (lls[0].baseline - lls[0].top) / dom.size if dom.size else 0.8
    top = lls[0].top
    if len(lls) > 1:
        pitch_ratio = (lls[-1].baseline - lls[0].baseline) / (len(lls) - 1) / dom.size
    else:
        dfont = fonts[(dom.font, dom.flags)].font if (dom.font, dom.flags) in fonts else fitz.Font("helv")
        pitch_ratio = max(1.15, dfont.ascender - dfont.descender)
    pitch_ratio = max(pitch_ratio, 0.8)
    align = align_o or _detect_align(lls, 0, 0)
    page_local = fr.rect_local(page.rect)
    if len(lls) == 1 and align == "left":
        # A one-line block behaves like Acrobat point text: when the new text is
        # longer it grows to the right (up to the neighbour or the mirrored left
        # margin) instead of wrapping and shrinking inside its old tight width.
        right_margin = max(18.0, x0 - page_local.x0)
        width = max(width, _free_right(blocks, blk, fr, page_local.x1 - right_margin) - x0)
    page_bottom = page_local.y1 - 18
    limit = max(_free_bottom(blocks, blk, fr, page_bottom), local_bbox.y1)

    def layout(f: float):
        lines = _wrap_items(items, width, f)
        baselines = []
        y = None
        for ln_items, _ in lines:
            lsize = max((it.size for it in ln_items), default=base_size) * f
            y = top + asc_ratio * lsize if y is None else y + pitch_ratio * lsize
            baselines.append(y)
        last = lines[-1][0]
        desc = max((-it.font.descender * it.size * f for it in last), default=0.25 * base_size * f)
        return lines, baselines, baselines[-1] + desc

    f = 1.0
    lines, baselines, bottom = layout(f)
    if bottom > limit + 0.5:
        while f > MIN_SHRINK + 1e-6:
            f = max(MIN_SHRINK, f - 0.025)
            lines, baselines, bottom = layout(f)
            if bottom <= limit + 0.5:
                break
    overflow = bottom > limit + 0.5

    _remove_runs(page, [b for l in blk.lines for s in l.spans if (b := _span_band(s, fr)) is not None])
    runs = []
    for (ln_items, para_end), y in zip(lines, baselines):
        if not ln_items:
            continue
        words_w = sum(it.width(f) for it in ln_items)
        spaces = [ln_items[i - 1].space(f) for i in range(1, len(ln_items))]
        line_w = words_w + sum(spaces)
        if align == "justify" and not para_end and len(ln_items) > 1:
            gap = (width - words_w) / (len(ln_items) - 1)
            x = x0
            for it in ln_items:
                runs.append((fitz.Point(x, y), it.text, it.font, it.size * f, it.rgb))
                x += it.width(f) + gap
            continue
        if align == "center":
            x = x0 + (width - line_w) / 2
        elif align == "right":
            x = x0 + width - line_w
        else:
            x = x0
        # merge consecutive same-style words into one run (keeps real spaces)
        i = 0
        while i < len(ln_items):
            j = i
            txt = ln_items[i].text
            while j + 1 < len(ln_items) and (ln_items[j + 1].font, ln_items[j + 1].size, ln_items[j + 1].rgb) == \
                    (ln_items[i].font, ln_items[i].size, ln_items[i].rgb):
                j += 1
                txt += " " + ln_items[j].text
            it = ln_items[i]
            runs.append((fitz.Point(x, y), txt, it.font, it.size * f, it.rgb))
            x += it.font.text_length(txt, fontsize=it.size * f)
            if j + 1 < len(ln_items):
                x += ln_items[j].space(f)
            i = j + 1
    _write(page, fr, runs)
    used = {fc.source for fc in fonts.values()}
    return {
        "font": fonts[(dom.font, dom.flags)].source if (dom.font, dom.flags) in fonts else sorted(used)[0],
        "fonts": sorted(used),
        "font_size": round(base_size * f, 2),
        "requested_size": round(base_size, 2),
        "scale": round(f, 3),
        "lines": len(lines),
        "overflow": overflow,
        "align": align,
    }


# ─── API models ─────────────────────────────────────────────────────────────


class Target(BaseModel):
    kind: Literal["block", "line", "span"]
    id: Optional[str] = None
    bbox: Optional[list[float]] = Field(default=None, description="visible-space PDF points")


class StyleOverride(BaseModel):
    family: Optional[Literal["original", "sans", "serif", "mono"]] = None
    size: Optional[float] = Field(default=None, gt=0, le=500)
    color: Optional[Union[str, list[float]]] = None
    bold: Optional[bool] = None
    italic: Optional[bool] = None


class EditRequest(BaseModel):
    page: int
    target: Target
    text: Optional[str] = None
    style: Optional[StyleOverride] = None
    align: Optional[Literal["left", "center", "right", "justify"]] = None


class MoveRequest(BaseModel):
    page: int
    target: Target
    dx: float = 0.0  # visible-space points
    dy: float = 0.0


class DeleteRequest(BaseModel):
    page: int
    target: Target


# ─── Serialisation ──────────────────────────────────────────────────────────


def _vis(page: fitz.Page, r: fitz.Rect) -> list[float]:
    v = fitz.Rect(r) * page.rotation_matrix
    v.normalize()
    return [round(v.x0, 3), round(v.y0, 3), round(v.x1, 3), round(v.y1, 3)]


def _style_dict(sp: Span) -> dict:
    fam, bold, italic = sp.style
    return {
        "font": _strip_subset(sp.font),
        "family": fam,
        "size": round(sp.size, 2),
        "color": _int_to_hex(sp.color),
        "bold": bold,
        "italic": italic,
        "flags": sp.flags,
    }


def _block_json(page: fitz.Page, blk: Block, page_rot: int) -> dict:
    fr = _block_frame(blk)
    dom = _dominant_span(blk.spans)
    styles = {(s.font, round(s.size, 1), s.color, s.flags) for s in blk.spans if s.text.strip()}
    out = {
        "id": blk.id,
        "bbox": _vis(page, blk.bbox),
        "text": blk.text,
        "style": _style_dict(dom),
        "mixed_styles": len(styles) > 1,
        "editable": fr is not None,
        # angle the text is drawn at in the *visible* page (0 = normal reading)
        "angle": None,
    }
    if fr is None:
        out.update(reason="Text at a non-right angle cannot be edited in place",
                   paragraph_text=blk.text, align="left", line_height=1.2)
    else:
        lls = _local_lines(blk, fr)
        bx1 = fr.rect_local(blk.bbox).x1
        pitch = None
        if len(lls) > 1:
            pitch = (lls[-1].baseline - lls[0].baseline) / (len(lls) - 1)
        out.update(
            paragraph_text=_tokens_text(_paragraph_tokens(lls, bx1)),
            align=_detect_align(lls, 0, 0),
            line_height=round(pitch / dom.size, 3) if pitch and dom.size else 1.2,
            angle=(fr.angle + page_rot) % 360,
        )
    out["lines"] = [{
        "id": ln.id,
        "bbox": _vis(page, ln.bbox),
        "text": ln.text,
        "style": _style_dict(_dominant_span(ln.spans)),
        "spans": [{"id": sp.id, "bbox": _vis(page, sp.bbox), "text": sp.text, **_style_dict(sp)}
                  for sp in ln.spans],
    } for ln in blk.lines]
    return out


def _unrot_rect(page: fitz.Page, bbox: Optional[list[float]]) -> Optional[fitz.Rect]:
    if bbox is None:
        return None
    if len(bbox) != 4:
        raise HTTPException(status_code=400, detail="bbox must be [x0, y0, x1, y1]")
    r = fitz.Rect(bbox) * page.derotation_matrix
    r.normalize()
    return r


# ─── Routes ─────────────────────────────────────────────────────────────────


@router.get("/{doc_id}/text-edit/page/{page_num}")
async def get_editable_text(doc_id: str, page_num: int):
    doc, _ = _open(doc_id)
    try:
        page = _get_page(doc, page_num)
        blocks = extract_blocks(page)
        rot = page.rotation
        fonts = []
        for f in page.get_fonts(full=True):
            fonts.append({"xref": f[0], "name": _strip_subset(f[3]), "type": f[2],
                          "embedded": f[1] not in ("n/a", ""), "subset": bool(re.match(r"^[A-Z]{6}\+", f[3] or ""))})
        return {
            "page": page_num,
            "width": page.rect.width,
            "height": page.rect.height,
            "rotation": rot,
            "blocks": [_block_json(page, b, rot) for b in blocks],
            "fonts": fonts,
        }
    finally:
        if not doc.is_closed:
            doc.close()


def _style_values(style: Optional[StyleOverride]):
    if style is None:
        return None, None, None, None, None
    return style.family, style.size, _parse_color(style.color), style.bold, style.italic


@router.post("/{doc_id}/text-edit/edit")
async def edit_text_in_place(doc_id: str, req: EditRequest):
    if req.text is not None and len(req.text) > MAX_TEXT_LEN:
        raise HTTPException(status_code=413, detail="Text too long")
    has_style = req.style is not None and any(
        v is not None for v in (req.style.family, req.style.size, req.style.color, req.style.bold, req.style.italic))
    if req.text is None and not has_style and req.align is None:
        raise HTTPException(status_code=400, detail="Nothing to change: provide text, style or align")

    doc, path = _open(doc_id)
    page = _get_page(doc, req.page)
    blocks = extract_blocks(page)
    blk, ln, sp = _locate(blocks, req.target.kind, req.target.id, _unrot_rect(page, req.target.bbox))
    fam, size_o, rgb_o, bold_o, italic_o = _style_values(req.style)
    text = None if req.text is None else req.text.replace("\r\n", "\n").replace("\r", "\n")

    resolver = FontResolver(doc, page)
    result: dict = {"status": "ok", "kind": req.target.kind}

    if req.target.kind == "block":
        fr = _block_frame(blk)
        if fr is None:
            doc.close()
            raise HTTPException(status_code=422, detail="Text at a non-right angle cannot be edited in place")
        snapshot(doc_id, "Edit text block")
        result.update(_edit_paragraph(page, blocks, blk, fr, resolver, text,
                                      fam, size_o, rgb_o, bold_o, italic_o, req.align))

    elif req.target.kind == "line":
        fr = _line_frame(ln)
        if fr is None:
            doc.close()
            raise HTTPException(status_code=422, detail="Text at a non-right angle cannot be edited in place")
        snapshot(doc_id, "Edit text line")
        dom = _dominant_span(ln.spans)
        if text is not None and "\n" in text:
            text = text.replace("\n", " ")
        if text is None and not has_style:
            # alignment-only change on a single line is a no-op
            doc.close()
            return {"status": "ok", "kind": "line", "changed": False}
        size = size_o or dom.size
        rgb = rgb_o or _int_to_rgb(dom.color)
        if text is None:
            # restyle every span, keep their text (mixed styles collapse to override)
            new_text = ln.text
        else:
            new_text = text
        choice = resolver.resolve(dom.font, dom.flags, new_text, family=fam, bold=bold_o, italic=italic_o)
        origin = fr.pt_local(ln.origin)
        _remove_runs(page, [b for s in ln.spans if (b := _span_band(s, fr)) is not None])
        _write(page, fr, [(origin, new_text, choice.font, size, rgb)])
        result.update(font=choice.source, font_size=round(size, 2))

    else:  # span: rewrite this span and shift the rest of the line
        fr = _line_frame(ln)
        if fr is None:
            doc.close()
            raise HTTPException(status_code=422, detail="Text at a non-right angle cannot be edited in place")
        snapshot(doc_id, "Edit text")
        new_text = sp.text if text is None else text.replace("\n", " ")
        size = size_o or sp.size
        rgb = rgb_o or _int_to_rgb(sp.color)
        choice = resolver.resolve(sp.font, sp.flags, new_text, family=fam, bold=bold_o, italic=italic_o)
        idx = ln.spans.index(sp)
        following = ln.spans[idx + 1:]
        o_local = fr.pt_local(sp.origin)
        old_end = fr.rect_local(sp.bbox).x1
        # trailing whitespace advances are not in the glyph bbox; keep them
        new_width = choice.font.text_length(new_text, fontsize=size)
        new_end = o_local.x + new_width
        shift = fitz.Point(new_end - old_end, 0)
        bands = [_span_band(sp, fr)] + [_span_band(s, fr) for s in following]
        _remove_runs(page, [b for b in bands if b is not None])
        runs = [(o_local, new_text, choice.font, size, rgb)]
        for s in following:
            fch = resolver.resolve(s.font, s.flags, s.text)
            _reinsert_span_exact(s, fr, fch.font, shift, runs)
        _write(page, fr, runs)
        result.update(font=choice.source, font_size=round(size, 2), shifted=round(shift.x, 3))

    _save_in_place(doc, path)
    return result


@router.post("/{doc_id}/text-edit/move")
async def move_text(doc_id: str, req: MoveRequest):
    if req.target.kind == "span":
        raise HTTPException(status_code=400, detail="Move a line or a block, not a span")
    doc, path = _open(doc_id)
    page = _get_page(doc, req.page)
    blocks = extract_blocks(page)
    blk, ln, _ = _locate(blocks, req.target.kind, req.target.id, _unrot_rect(page, req.target.bbox))
    lines = blk.lines if req.target.kind == "block" else [ln]

    # visible-space vector -> unrotated page vector
    dm = page.derotation_matrix
    vec = fitz.Point(req.dx, req.dy) * dm - fitz.Point(0, 0) * dm
    moved = fitz.Rect()
    for l in lines:
        moved |= fitz.Rect(l.bbox) + (vec.x, vec.y, vec.x, vec.y)
    if not moved.intersects(page.rect * page.derotation_matrix if page.rotation else page.rect):
        doc.close()
        raise HTTPException(status_code=400, detail="Destination is outside the page")

    frames = []
    for l in lines:
        fr = _line_frame(l)
        if fr is None:
            doc.close()
            raise HTTPException(status_code=422, detail="Text at a non-right angle cannot be moved")
        frames.append(fr)

    snapshot(doc_id, "Move text")
    resolver = FontResolver(doc, page)
    bands = [b for l, fr in zip(lines, frames) for s in l.spans if (b := _span_band(s, fr)) is not None]
    _remove_runs(page, bands)
    for l, fr in zip(lines, frames):
        local_vec = fr.pt_local(fr.pivot + vec) - fr.pivot
        runs = []
        for s in l.spans:
            ch = resolver.resolve(s.font, s.flags, s.text)
            _reinsert_span_exact(s, fr, ch.font, local_vec, runs)
        _write(page, fr, runs)
    _save_in_place(doc, path)
    return {"status": "ok", "moved": len(lines), "dx": req.dx, "dy": req.dy}


@router.post("/{doc_id}/text-edit/delete")
async def delete_text(doc_id: str, req: DeleteRequest):
    doc, path = _open(doc_id)
    page = _get_page(doc, req.page)
    blocks = extract_blocks(page)
    blk, ln, sp = _locate(blocks, req.target.kind, req.target.id, _unrot_rect(page, req.target.bbox))
    if req.target.kind == "block":
        spans_lines = [(s, l) for l in blk.lines for s in l.spans]
    elif req.target.kind == "line":
        spans_lines = [(s, ln) for s in ln.spans]
    else:
        spans_lines = [(sp, ln)]
    bands = []
    for s, l in spans_lines:
        fr = _line_frame(l)
        if fr is None:
            doc.close()
            raise HTTPException(status_code=422, detail="Text at a non-right angle cannot be deleted in place")
        b = _span_band(s, fr)
        if b is not None:
            bands.append(b)
    snapshot(doc_id, "Delete text")
    _remove_runs(page, bands)
    _save_in_place(doc, path)
    return {"status": "ok", "deleted": req.target.kind}
