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
   space below the block, then shrinks the font in small steps (to 85%),
   then pushes the following blocks of the same column down (re-inserted
   glyph-exact) when the page has room. If none of that works nothing is
   written: 422 ``{code: "overflow", needed_height, available_height,
   fit_size, ...}``; the client then resends with ``overflow: "shrink"``
   (shrink below 85% until it fits) or ``overflow: "allow"`` (write at the
   requested size and overlap).
5. Spacing: horizontal scaling (Tz) is recovered from the glyph box height vs
   the reported size, character spacing (Tc) and word spacing (Tw) from the
   measured glyph advances vs the font's natural advances. Re-inserted text
   reproduces them (glyph-by-glyph placement + a horizontal-scale morph).
6. When the embedded (subset) font lacks some glyphs of the new text, those
   glyphs alone fall back to the Base-14 equivalent; the rest keep using the
   embedded font (``FontStack``).

Coordinates
-----------
Everything crossing this API is in PDF points, top-left origin, in the
*visible* page space (``page.rect`` — rotation already applied, i.e. the space
of the rendered page image). PyMuPDF text extraction / insertion works in the
unrotated space; conversion happens here with ``page.rotation_matrix``.
Text at any angle (rotated pages, vertical labels, slanted stamps) is handled
by laying it out in a local horizontal frame and writing it back with a
rotation ``morph``. Removal of slanted text uses per-glyph point quads (MuPDF
tests a redaction quad by its bounding box, so one rotated band would also
hit neighbours).

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
    def is_right(self) -> bool:
        """Text runs along a page axis (0/90/180/270)."""
        return abs(self.angle / 90.0 - round(self.angle / 90.0)) < 1e-6

    def quad_local(self, q) -> fitz.Rect:
        pts = [self.pt_local(p) for p in (q.ul, q.ur, q.ll, q.lr)]
        return fitz.Rect(min(p.x for p in pts), min(p.y for p in pts),
                         max(p.x for p in pts), max(p.y for p in pts))

    @property
    def morph(self):
        if abs(self.angle) < 1e-6:
            return None
        # TextWriter applies ``morph`` in PDF (y-up) orientation, so the
        # visual rotation is the inverse of the y-down matrix ``to_page``.
        return (self.pivot, self.to_local)

    def morph_scaled(self, hscale: float):
        """Morph for text compressed/expanded horizontally by ``hscale`` (Tz)."""
        if abs(hscale - 1.0) < 1e-6:
            return self.morph
        # x-scaling commutes with the y-flip, so it composes before the rotation
        return (self.pivot, fitz.Matrix(hscale, 0, 0, 1, 0, 0) * self.to_local)


ANGLE_TOL = 0.5  # degrees


def _dir_angle(d) -> float:
    """Angle (degrees, y-down page space) of a line direction. Snapped to the
    nearest multiple of 90 when within ``ANGLE_TOL``; any other angle is
    returned as measured."""
    ang = math.degrees(math.atan2(d[1], d[0]))
    snapped = round(ang / 90.0) * 90.0
    if abs(ang - snapped) <= ANGLE_TOL:
        return snapped % 360.0
    return round(ang % 360.0, 4)


def _same_angle(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return False
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d) <= ANGLE_TOL


# ─── Extraction model ───────────────────────────────────────────────────────


@dataclass
class Char:
    c: str
    origin: fitz.Point  # page coords
    bbox: fitz.Rect     # page coords (axis-aligned box of the glyph)
    quad: Optional[fitz.Quad] = None  # page coords, follows the text direction


@dataclass
class Span:
    id: str
    font: str
    size: float  # true (vertical) font size; MuPDF reports sqrt(sx*sy)
    color: int
    flags: int
    chars: list[Char]
    bbox: fitz.Rect
    origin: fitz.Point
    hscale: float = 1.0  # horizontal scaling (Tz / 100), 1 = none

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


def _span_scale(sp: dict, ldir, size: float) -> tuple[float, float]:
    """(true size, hscale) of a raw span.

    MuPDF reports ``size`` as sqrt(sx*sy) of the text matrix, while the glyph
    boxes are ``(ascender - descender) * sy`` tall. So sy (the real font size)
    comes from the box height, and Tz = sx/sy = (size/sy)**2."""
    ang = _dir_angle(ldir)
    k = float(sp.get("ascender", 0.0)) - float(sp.get("descender", 0.0))
    if size <= 0 or k < 0.3 or abs(ang / 90.0 - round(ang / 90.0)) > 1e-6:
        return size, 1.0
    fr = Frame(pivot=fitz.Point(sp["origin"]), angle=ang)
    h = fr.rect_local(fitz.Rect(sp["bbox"])).height
    if h <= 0:
        return size, 1.0
    v = h / k
    a = size / v
    if abs(a - 1.0) <= 0.01 or not (0.3 < a < 3.0):
        return size, 1.0
    return v, a * a


def _raw_lines(bi: int, b: dict) -> list[Line]:
    lines = []
    for li, ln in enumerate(b.get("lines", [])):
        spans = []
        ldir = tuple(ln.get("dir", (1, 0)))
        for si, sp in enumerate(ln.get("spans", [])):
            chars = []
            for ch in sp.get("chars", []):
                try:
                    q = fitz.recover_char_quad(ldir, sp, ch)
                except Exception:
                    q = fitz.Rect(ch["bbox"]).quad
                chars.append(Char(c=ch["c"], origin=fitz.Point(ch["origin"]), bbox=fitz.Rect(ch["bbox"]), quad=q))
            if not chars:
                continue
            size, hscale = _span_scale(sp, ldir, float(sp.get("size", 11)))
            spans.append(Span(
                id=f"b{bi}.l{li}.s{si}", font=sp.get("font", ""), size=size,
                color=int(sp.get("color", 0)), flags=int(sp.get("flags", 0)), chars=chars,
                bbox=fitz.Rect(sp["bbox"]), origin=fitz.Point(sp["origin"]), hscale=hscale,
            ))
        lines.append(Line(id=f"b{bi}.l{li}", bbox=fitz.Rect(ln["bbox"]),
                          dir=ldir, spans=spans))
    return lines


def _chars_of(obj) -> list[Char]:
    if isinstance(obj, Char):
        return [obj]
    if isinstance(obj, Span):
        return obj.chars
    if isinstance(obj, Line):
        return [c for s in obj.spans for c in s.chars]
    return [c for ln in obj.lines for s in ln.spans for c in s.chars]


def _lrect(fr: "Frame", obj) -> fitz.Rect:
    """Box of a Block/Line/Span/Char in ``fr``'s local frame. For right-angle
    frames this is the exact transform of the axis-aligned bbox; for slanted
    text the union of the per-glyph quads (the bbox would be far too big)."""
    if fr.is_right:
        return fr.rect_local(obj.bbox)
    r = fitz.Rect()
    for c in _chars_of(obj):
        r |= fr.quad_local(c.quad if c.quad is not None else fitz.Rect(c.bbox).quad)
    return r if not r.is_empty else fr.rect_local(obj.bbox)


def _starts_new_paragraph(prev: Line, cur: Line, pitch: Optional[float]) -> bool:
    """Should ``cur`` start a new paragraph after ``prev`` (same raw block)?"""
    a_prev, a_cur = _dir_angle(prev.dir), _dir_angle(cur.dir)
    if not _same_angle(a_prev, a_cur):
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
                fr = Frame(pivot=fitz.Point(cur[-1].origin), angle=_dir_angle(cur[-1].dir))
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
    """Frame of a paragraph; None only if its lines run in different directions."""
    ang = _dir_angle(blk.lines[0].dir)
    for ln in blk.lines:
        if not _same_angle(_dir_angle(ln.dir), ang):
            return None
    return Frame(pivot=fitz.Point(blk.lines[0].origin), angle=ang)


def _line_frame(ln: Line) -> Frame:
    return Frame(pivot=fitz.Point(ln.origin), angle=_dir_angle(ln.dir))


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
        r = _lrect(fr, ln)
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


def _has_ink(font: fitz.Font, ch: str) -> bool:
    """Does the glyph draw anything? Subsets made by some producers (MuPDF's
    own ``subset_fonts`` among them) keep the cmap and widths of every glyph
    but empty the outlines of unused ones, so has_glyph() is not enough."""
    try:
        scratch = fitz.open()
        pg = scratch.new_page(width=24, height=24)
        tw = fitz.TextWriter(pg.rect)
        tw.append((4, 18), ch, font=font, fontsize=16)
        tw.write_text(pg)
        ok = not pg.get_pixmap(alpha=False).is_unicolor
        scratch.close()
        return ok
    except Exception:
        return True


def _glyph_ok(font: fitz.Font, ch: str) -> bool:
    if ch.isspace():
        return True
    cp = ord(ch)
    # some broken subsets map a code point to an empty glyph
    if not font.has_glyph(cp) or font.glyph_advance(cp) <= 0:
        return False
    ink = getattr(font, "_te_ink", None)  # set on subset fonts only
    if ink is None:
        return True
    if ch not in ink:
        ink[ch] = _has_ink(font, ch)
    return ink[ch]


class FontStack:
    """A primary (embedded, possibly subset) font plus a fallback used only for
    the glyphs the primary lacks. Duck-types the parts of ``fitz.Font`` the
    layout code uses; ``_segments`` splits text into single-font pieces."""

    def __init__(self, primary: fitz.Font, fallback: fitz.Font):
        self.primary = primary
        self.fallback = fallback

    def font_for(self, ch: str) -> fitz.Font:
        return self.primary if _glyph_ok(self.primary, ch) else self.fallback

    def segments(self, text: str) -> list[tuple[str, fitz.Font]]:
        out: list[tuple[str, fitz.Font]] = []
        for ch in text:
            f = out[-1][1] if (ch.isspace() and out) else self.font_for(ch)
            if out and out[-1][1] is f:
                out[-1] = (out[-1][0] + ch, f)
            else:
                out.append((ch, f))
        return out

    def text_length(self, text: str, fontsize: float = 11) -> float:
        return sum(f.text_length(t, fontsize=fontsize) for t, f in self.segments(text))

    def glyph_advance(self, cp: int) -> float:
        return self.font_for(chr(cp)).glyph_advance(cp)

    def has_glyph(self, cp: int) -> int:
        return self.primary.has_glyph(cp) or self.fallback.has_glyph(cp)

    @property
    def ascender(self) -> float:
        return self.primary.ascender

    @property
    def descender(self) -> float:
        return self.primary.descender


def _segments(font, text: str) -> list[tuple[str, fitz.Font]]:
    if isinstance(font, FontStack):
        return font.segments(text)
    return [(text, font)]


@dataclass
class FontChoice:
    font: Union[fitz.Font, FontStack]
    source: str  # "embedded:<name>" | "base14:<code>"
    fallback: Optional[str] = None  # source of the per-glyph fallback, if any


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
                if re.match(r"^[A-Z]{6}\+", name or ""):
                    font._te_ink = {}  # subset: verify outlines before trusting a glyph
        except Exception:
            font = None
        self._cache[xref] = font
        return font

    @staticmethod
    def covers(font, text: str) -> bool:
        if isinstance(font, FontStack):
            return all(_glyph_ok(font.primary, ch) or _glyph_ok(font.fallback, ch) for ch in set(text))
        return all(_glyph_ok(font, ch) for ch in set(text))

    def _partial(self, partial: Optional[tuple], text: str, fb: FontChoice) -> Optional[FontChoice]:
        """Embedded font for the glyphs it has + ``fb`` for the rest, if that
        actually covers the text (otherwise mixing fonts buys nothing)."""
        if partial is None:
            return None
        font, src = partial
        chars = {ch for ch in text if not ch.isspace()}
        have = {ch for ch in chars if _glyph_ok(font, ch)}
        missing = chars - have
        if not have or not missing or not self.covers(fb.font, "".join(missing)):
            return None
        return FontChoice(FontStack(font, fb.font), src, fallback=fb.source)

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

        own_partial = sib_partial = None
        if not generic_override:
            # (a) the span's own font, if style is unchanged
            if want_bold == o_bold and want_italic == o_italic:
                target = _norm_font(span_font)
                for xref, basefont, _fk, _st in self.entries:
                    if _norm_font(basefont) == target:
                        f = self._load(xref)
                        if f is None:
                            continue
                        if self.covers(f, text):
                            return FontChoice(f, f"embedded:{_strip_subset(basefont)}")
                        own_partial = own_partial or (f, f"embedded:{_strip_subset(basefont)}")
            # (b) a sibling weight/style (or another subset) of the same family
            fk = _family_key(span_font)
            if fk:
                for xref, basefont, efk, (efam, ebold, eitalic) in self.entries:
                    if efk == fk and ebold == want_bold and eitalic == want_italic:
                        f = self._load(xref)
                        if f is None:
                            continue
                        if self.covers(f, text):
                            return FontChoice(f, f"embedded:{_strip_subset(basefont)}")
                        sib_partial = sib_partial or (f, f"embedded:{_strip_subset(basefont)}")
        # (c) Base-14 equivalent — for the whole run, or (per-glyph) only for
        # the glyphs an embedded subset is missing
        fb = self.base14(want_fam, want_bold, want_italic)
        return self._partial(own_partial, text, fb) or self._partial(sib_partial, text, fb) or fb


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


def _remove_runs(page: fitz.Page, rects: list) -> None:
    """Remove the text under ``rects`` (unrotated page coords) only.

    An item is a Rect (axis-aligned band) or a list of Quads (point quads
    along slanted text, written as one annotation's /QuadPoints)."""
    items = []
    for r in rects:
        if isinstance(r, list):
            if r:
                items.append(r)
        elif r and not r.is_empty:
            items.append(r)
    if not items:
        return
    doc = page.parent
    # Stash redact annots placed by other tools so we do not burn them in.
    stashed = []
    for annot in list(page.annots(types=[fitz.PDF_ANNOT_REDACT]) or []):
        stashed.append((fitz.Rect(annot.rect), doc.xref_object(annot.xref, compressed=False)))
        page.delete_annot(annot)
    tm = page.transformation_matrix
    for r in items:
        if not isinstance(r, list):
            page.add_redact_annot(r, fill=False)
            continue
        box = fitz.Rect()
        for q in r:
            box |= q.rect
        a = page.add_redact_annot(box, fill=False)
        pts = " ".join(f"{(p * tm).x:.4f} {(p * tm).y:.4f}" for q in r for p in (q.ul, q.ur, q.ll, q.lr))
        doc.xref_set_key(a.xref, "QuadPoints", f"[{pts}]")
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


def _span_band(span: Span, fr: Frame, from_char: int = 0):
    """What to redact to remove ``span``: a thin x-height band (Rect) for text
    along a page axis; for slanted text a tiny axis-aligned quad at the middle
    of each glyph's x-height (MuPDF only tests a quad's bounding box, so a
    rotated band would also catch glyphs of the neighbouring lines)."""
    if fr.is_right:
        loc = [(fr.pt_local(c.origin), fr.rect_local(c.bbox)) for c in span.chars[from_char:]]
        band = _band_rect_local(loc, span.size)
        return fr.rect_page(band) if band is not None else None
    quads = []
    for c in span.chars[from_char:]:
        lr = _lrect(fr, c)
        o = fr.pt_local(c.origin)
        e = max(0.05, min(0.08 * span.size, lr.width / 4))
        p = fr.pt_page(fitz.Point((lr.x0 + lr.x1) / 2, o.y - 0.33 * span.size))
        quads.append(fitz.Rect(p.x - e, p.y - e, p.x + e, p.y + e).quad)
    return quads or None


# ─── Writing ────────────────────────────────────────────────────────────────


@dataclass
class Run:
    """One piece of text to write, in a frame's local coordinates."""
    origin: fitz.Point
    text: str
    font: Union[fitz.Font, FontStack]
    size: float
    rgb: tuple
    hscale: float = 1.0  # Tz / 100
    tc: float = 0.0      # extra advance after every glyph (points, final)
    tw: float = 0.0      # extra advance after every space (points, final)


def _adv(font, text: str, size: float, hscale: float = 1.0, tc: float = 0.0, tw: float = 0.0) -> float:
    """Advance width of ``text`` as it will be written (Tz, Tc, Tw applied)."""
    if not text:
        return 0.0
    return hscale * font.text_length(text, fontsize=size) + tc * len(text) + tw * text.count(" ")


def _write(page: fitz.Page, fr: Frame, runs: list):
    """runs: Run objects (or (local origin, text, font, size, rgb) tuples), in
    reading order.

    Consecutive runs of one colour and horizontal scale share a TextWriter; a
    change starts a new one. Writing strictly in order keeps the content
    stream in reading order, so copy/paste and search see the words in the
    right sequence. Runs with char/word spacing are placed glyph by glyph."""
    tw, key = None, None

    def flush():
        if tw is not None:
            tw.write_text(page, color=key[0], morph=fr.morph_scaled(key[1]))

    for r in runs:
        if not isinstance(r, Run):
            r = Run(*r)
        if not r.text:
            continue
        k = (r.rgb, round(r.hscale, 5))
        if tw is None or k != key:
            flush()
            tw, key = fitz.TextWriter(page.rect), k
        _append_run(tw, fr, r)
    flush()


def _append_run(tw: fitz.TextWriter, fr: Frame, r: Run) -> None:
    hs = r.hscale if r.hscale > 0 else 1.0
    px = fr.pivot.x

    def put(x: float, text: str, font: fitz.Font):
        # the writer's morph scales x by ``hs`` around the pivot: pre-divide
        tw.append(fitz.Point(px + (x - px) / hs, r.origin.y), text, font=font, fontsize=r.size)

    x = r.origin.x
    per_glyph = abs(r.tc) > 1e-6 or abs(r.tw) > 1e-6
    for text, font in _segments(r.font, r.text):
        if not per_glyph:
            put(x, text, font)
            x += hs * font.text_length(text, fontsize=r.size)
            continue
        for ch in text:
            if not ch.isspace():
                put(x, ch, font)
            x += _adv(font, ch, r.size, hs, r.tc, r.tw)


def _reinsert_span_exact(span: Span, fr: Frame, font, delta: fitz.Point,
                         runs: list, rgb: Optional[tuple] = None):
    """Re-insert every glyph at its original origin (+delta): keeps kerning,
    tracking, horizontal scaling and justification spacing exactly."""
    color = rgb if rgb is not None else _int_to_rgb(span.color)
    for ch in span.chars:
        if ch.c.isspace():
            continue
        o = fr.pt_local(ch.origin) + delta
        runs.append(Run(o, ch.c, font, span.size, color, hscale=span.hscale))


# ─── Spacing (Tc / Tw) measurement ──────────────────────────────────────────

# fonts whose metrics the Base-14 substitutes share, so measuring against the
# substitute is meaningful even when the original is not embedded
_METRIC_COMPAT = ("helvetica", "arial", "times", "courier", "liberation", "nimbus", "arimo",
                  "tinos", "cousine")


def _measure_spacing(spans: list[Span], fr: Frame, resolver: "FontResolver") -> tuple[float, float]:
    """(tc, tw) in ems: extra advance per glyph and per space beyond the
    font's natural advance (x hscale), from consecutive glyph origins.
    Medians make it robust against kerning; tiny values snap to 0."""
    d_chars: list[float] = []
    d_spaces: list[float] = []
    for sp in spans:
        if len(sp.chars) < 2 or sp.size <= 0:
            continue
        choice = resolver.resolve(sp.font, sp.flags, sp.text)
        if not (choice.source.startswith("embedded:") or
                any(h in _norm_font(sp.font) for h in _METRIC_COMPAT)):
            continue  # substitute metrics would masquerade as spacing
        xs = [fr.pt_local(c.origin).x for c in sp.chars]
        for i in range(len(sp.chars) - 1):
            ch = sp.chars[i].c
            base = choice.font.primary if isinstance(choice.font, FontStack) else choice.font
            if ch == " " and not base.has_glyph(32):
                continue
            nat = sp.hscale * choice.font.glyph_advance(ord(ch)) * sp.size
            d = (xs[i + 1] - xs[i] - nat) / sp.size
            (d_spaces if ch == " " else d_chars).append(d)

    def median(v):
        v = sorted(v)
        n = len(v)
        return 0.0 if not n else (v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2)

    tc = median(d_chars) if len(d_chars) >= 3 else 0.0
    tw = (median(d_spaces) - tc) if d_spaces else 0.0
    tc = 0.0 if abs(tc) < 0.01 else max(-0.2, min(1.0, tc))
    tw = 0.0 if abs(tw) < 0.04 else max(-0.3, min(2.0, tw))
    return tc, tw


# ─── Paragraph layout ───────────────────────────────────────────────────────


def _free_bottom(blocks: list[Block], blk: Block, fr: Frame, page_bottom_local: float) -> float:
    """Lowest local y the block may grow to without hitting another block."""
    me = _lrect(fr, blk)
    limit = page_bottom_local
    for other in blocks:
        if other.id == blk.id:
            continue
        r = _lrect(fr, other)
        if r.x1 <= me.x0 or r.x0 >= me.x1:
            continue  # different column
        if r.y0 >= me.y1 - 0.5:
            limit = min(limit, r.y0 - 1.0)
    return max(limit, me.y1)


def _free_right(blocks: list[Block], blk: Block, fr: Frame, page_right_local: float) -> float:
    """Rightmost local x a single-line block may grow to without hitting a neighbour."""
    me = _lrect(fr, blk)
    limit = page_right_local
    for other in blocks:
        if other.id == blk.id:
            continue
        r = _lrect(fr, other)
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
    font: Union[fitz.Font, FontStack]
    size: float
    rgb: tuple
    hscale: float = 1.0
    tc: float = 0.0  # ems
    tw: float = 0.0  # ems

    def run_args(self, f: float = 1.0) -> dict:
        return {"hscale": self.hscale, "tc": self.tc * self.size * f, "tw": self.tw * self.size * f}

    def adv(self, text: str, f: float = 1.0) -> float:
        return _adv(self.font, text, self.size * f, **self.run_args(f))

    def width(self, f: float = 1.0) -> float:
        return self.adv(self.text, f)

    def space(self, f: float = 1.0) -> float:
        return self.adv(" ", f)

    def with_text(self, text: str) -> "_Item":
        return _Item(text, self.font, self.size, self.rgb, self.hscale, self.tc, self.tw)

    def style_key(self) -> tuple:
        return (id(self.font), self.size, self.rgb, self.hscale, self.tc, self.tw)


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
                while n > 1 and it.adv(rest[:n], f) > width + 0.01:
                    n -= 1
                piece = it.with_text(rest[:n])
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


class OverflowError422(Exception):
    """Raised before anything is written when an edit cannot fit."""

    def __init__(self, info: dict):
        super().__init__("overflow")
        self.info = info


FOOTER_ZONE = 0.07   # bottom fraction of the page whose blocks are never pushed
PUSH_MARGIN = 18.0   # pushed text must stay this far above the page edge
MIN_FIT_SCALE = 0.25  # "shrink to fit" never goes below 25% ...
MIN_FIT_SIZE = 3.0    # ... or 3pt


def _plan_push(blocks: list[Block], blk: Block, fr: Frame, x0: float, x1: float,
               old_bottom: float, delta: float, page_local: fitz.Rect) -> Optional[list[Block]]:
    """Blocks to move down by ``delta`` (local y) so the grown paragraph fits,
    or None when there is no room on the page.

    Followers are the blocks below the paragraph in its column (horizontal
    overlap), closed transitively (a pushed block pushes what is under it).
    Blocks in the footer zone are obstacles, never followers. Every follower
    must keep clear of all other blocks and of the page bottom margin."""
    zone_top = page_local.y1 - FOOTER_ZONE * page_local.height
    rects = {b.id: _lrect(fr, b) for b in blocks if b.id != blk.id}
    same_dir = {b.id for b in blocks if b.id != blk.id and (bf := _block_frame(b)) is not None
                and _same_angle(bf.angle, fr.angle)}
    followers: dict[str, fitz.Rect] = {}
    spans_x = [(x0, x1, old_bottom)]
    changed = True
    while changed:
        changed = False
        for bid, r in rects.items():
            if bid in followers or r.y0 >= zone_top:
                continue
            for sx0, sx1, sy in spans_x:
                if r.x1 > sx0 + 0.5 and r.x0 < sx1 - 0.5 and r.y0 >= sy - 0.5:
                    if bid not in same_dir:
                        return None  # a block we cannot re-insert would be overrun
                    followers[bid] = r
                    spans_x.append((r.x0, r.x1, r.y1))
                    changed = True
                    break
    if not followers:
        return None
    bottom_limit = page_local.y1 - PUSH_MARGIN
    grown = fitz.Rect(x0, old_bottom - 0.5, x1, old_bottom + delta)
    if grown.y1 > bottom_limit:
        return None
    for oid, o in rects.items():
        if oid not in followers and o.y0 >= old_bottom - 0.5:
            inter = grown & o
            if not inter.is_empty and inter.height >= 0.5 and inter.width >= 0.5:
                return None
    for bid, r in followers.items():
        moved = fitz.Rect(r.x0, r.y0 + delta, r.x1, r.y1 + delta)
        if moved.y1 > max(bottom_limit, r.y1):
            return None
        for oid, o in rects.items():
            if oid in followers:
                continue
            if (moved & o).is_empty or (moved & o).height < 0.5 or (moved & o).width < 0.5:
                continue
            if not (r & o).is_empty and (r & o).height >= 0.5:
                continue  # already overlapping before: not made worse by us
            return None
    return [b for b in blocks if b.id in followers]


def _edit_paragraph(page, blocks, blk: Block, fr: Frame, resolver: "FontResolver", text: Optional[str],
                    fam, size_o, rgb_o, bold_o, italic_o, align_o, overflow_mode: str = "auto",
                    before_write=None) -> dict:
    lls = _local_lines(blk, fr)
    local_bbox = _lrect(fr, blk)
    dom = _dominant_span(blk.spans)
    orig = _paragraph_tokens(lls, local_bbox.x1)
    new_text = _tokens_text(orig) if text is None else text
    toks = _restyle_tokens(orig, new_text)
    align = align_o or _detect_align(lls, 0, 0)

    # resolve one font per original style (checked against all its new words)
    groups: dict[tuple, list[str]] = {}
    for w, sp in toks:
        if w != NL:
            sp = sp or dom
            groups.setdefault((sp.font, sp.flags), []).append(w)
    fonts: dict[tuple, FontChoice] = {}
    spacing: dict[tuple, tuple[float, float]] = {}
    for (fname, flags), words in groups.items():
        fonts[(fname, flags)] = resolver.resolve(fname, flags, "".join(words), family=fam,
                                                 bold=bold_o, italic=italic_o)
        tc, tw = _measure_spacing([s for s in blk.spans if (s.font, s.flags) == (fname, flags)], fr, resolver)
        if align == "justify":
            tw = 0.0  # justified gaps are recomputed by the layout
        spacing[(fname, flags)] = (tc, tw)
    items: list[Optional[_Item]] = []
    for w, sp in toks:
        if w == NL:
            items.append(None)
            continue
        sp = sp or dom
        tc, tw = spacing[(sp.font, sp.flags)]
        items.append(_Item(w, fonts[(sp.font, sp.flags)].font, size_o or sp.size,
                           rgb_o or _int_to_rgb(sp.color), sp.hscale, tc, tw))

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

    def fits(b: float) -> bool:
        return b <= limit + 0.5

    f = 1.0
    lines, baselines, bottom = layout(f)
    full_bottom = bottom  # at the requested size
    pushed: list[Block] = []
    push_delta = 0.0
    overlap = False
    if not fits(bottom):
        # 1. shrink a little (to 85%)
        while f > MIN_SHRINK + 1e-6:
            f = max(MIN_SHRINK, f - 0.025)
            lines, baselines, bottom = layout(f)
            if fits(bottom):
                break
    if not fits(bottom):
        plan = None
        if overflow_mode == "auto":
            # 2. keep the 85% size and push the blocks below down by what is
            # still missing (if the page has room)
            push_delta = bottom - local_bbox.y1
            plan = _plan_push(blocks, blk, fr, x0, x0 + width, local_bbox.y1, push_delta, page_local)
        if plan:
            pushed = plan
        else:
            push_delta = 0.0
            # largest scale that fits without pushing
            fit_f, fit_b = None, None
            g = MIN_SHRINK
            min_f = max(MIN_FIT_SCALE, MIN_FIT_SIZE / base_size if base_size else MIN_FIT_SCALE)
            while g > min_f + 1e-9:
                g = max(min_f, round(g - 0.01, 4))
                ls, bs, b = layout(g)
                if fits(b):
                    fit_f, fit_b = g, (ls, bs, b)
                    break
            if overflow_mode == "shrink" and fit_b is not None:
                f = fit_f
                lines, baselines, bottom = fit_b
            elif overflow_mode == "allow":
                f = 1.0
                lines, baselines, bottom = layout(f)
                overlap = True
            else:
                raise OverflowError422({
                    "code": "overflow",
                    "message": "The edited text does not fit: there is no room below to push the "
                               "following text down",
                    "needed_height": round(full_bottom - top, 2),
                    "available_height": round(limit - top, 2),
                    "requested_size": round(base_size, 2),
                    "fit_size": round(base_size * fit_f, 2) if fit_f is not None else None,
                    "fit_scale": round(fit_f, 3) if fit_f is not None else None,
                })

    if before_write is not None:
        before_write()

    bands = [b for l in blk.lines for s in l.spans if (b := _span_band(s, fr)) is not None]
    push_frames = []
    if pushed:
        vec = fr.pt_page(fr.pivot + (0, push_delta)) - fr.pivot  # page-space vector
        for pb in pushed:
            for pl in pb.lines:
                lf = _line_frame(pl)
                push_frames.append((pl, lf, lf.pt_local(lf.pivot + vec) - lf.pivot))
                bands += [b for s in pl.spans if (b := _span_band(s, lf)) is not None]
    _remove_runs(page, bands)
    runs: list[Run] = []
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
                runs.append(Run(fitz.Point(x, y), it.text, it.font, it.size * f, it.rgb, **it.run_args(f)))
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
            while j + 1 < len(ln_items) and ln_items[j + 1].style_key() == ln_items[i].style_key():
                j += 1
                txt += " " + ln_items[j].text
            it = ln_items[i]
            runs.append(Run(fitz.Point(x, y), txt, it.font, it.size * f, it.rgb, **it.run_args(f)))
            x += it.adv(txt, f)
            if j + 1 < len(ln_items):
                x += ln_items[j].space(f)
            i = j + 1
    _write(page, fr, runs)
    for pl, lf, lvec in push_frames:
        pruns: list = []
        for s in pl.spans:
            _reinsert_span_exact(s, lf, resolver.resolve(s.font, s.flags, s.text).font, lvec, pruns)
        _write(page, lf, pruns)
    used = {fc.source for fc in fonts.values()} | {fc.fallback for fc in fonts.values() if fc.fallback}
    return {
        "font": fonts[(dom.font, dom.flags)].source if (dom.font, dom.flags) in fonts else sorted(used)[0],
        "fonts": sorted(used),
        "font_size": round(base_size * f, 2),
        "requested_size": round(base_size, 2),
        "scale": round(f, 3),
        "lines": len(lines),
        # overflow: the text extends past the space it had (it pushed the
        # following text down, or overlaps it); overlap: it covers other text
        "overflow": not fits(bottom),
        "overlap": overlap,
        "align": align,
        "pushed": len(pushed),
        "push_distance": round(push_delta, 2) if pushed else 0.0,
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
    # paragraph edits that do not fit: "auto" = shrink to 85% / push the text
    # below down, else 422; "shrink" = shrink as far as needed; "allow" =
    # write at the requested size and let it overlap
    overflow: Literal["auto", "shrink", "allow"] = "auto"


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
        "hscale": round(sp.hscale, 4),
    }


def _box_json(page: fitz.Page, fr: Frame, obj) -> dict:
    """The text's own (possibly rotated) box in visible space: top-left corner
    (x, y), width/height along/across the text direction, and the clockwise
    angle — i.e. CSS ``transform: rotate(angle deg)`` with origin top-left."""
    lr = _lrect(fr, obj)
    tl = fr.pt_page(lr.tl) * page.rotation_matrix
    return {"x": round(tl.x, 3), "y": round(tl.y, 3), "w": round(lr.width, 3), "h": round(lr.height, 3),
            "angle": round((fr.angle + page.rotation) % 360, 4)}


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
        out.update(reason="Lines of this paragraph run in different directions; edit them one line at a time",
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
            angle=round((fr.angle + page_rot) % 360, 4),
            box=_box_json(page, fr, blk),
        )
    lines = []
    for ln in blk.lines:
        lf = _line_frame(ln)
        lines.append({
            "id": ln.id,
            "bbox": _vis(page, ln.bbox),
            "text": ln.text,
            "style": _style_dict(_dominant_span(ln.spans)),
            "editable": True,
            "angle": round((lf.angle + page_rot) % 360, 4),
            "box": _box_json(page, lf, ln),
            "spans": [{"id": sp.id, "bbox": _vis(page, sp.bbox), "text": sp.text,
                       "box": _box_json(page, lf, sp), **_style_dict(sp)}
                      for sp in ln.spans],
        })
    out["lines"] = lines
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
    try:
        if req.target.kind == "block":
            fr = _block_frame(blk)
            if fr is None:
                raise HTTPException(status_code=422, detail="Lines of this paragraph run in different "
                                                            "directions; edit them one line at a time")
            try:
                result.update(_edit_paragraph(page, blocks, blk, fr, resolver, text,
                                              fam, size_o, rgb_o, bold_o, italic_o, req.align,
                                              overflow_mode=req.overflow,
                                              before_write=lambda: snapshot(doc_id, "Edit text block")))
            except OverflowError422 as e:
                raise HTTPException(status_code=422, detail=e.info)

        elif req.target.kind == "line":
            fr = _line_frame(ln)
            dom = _dominant_span(ln.spans)
            if text is not None and "\n" in text:
                text = text.replace("\n", " ")
            if text is None and not has_style:
                # alignment-only change on a single line is a no-op
                doc.close()
                return {"status": "ok", "kind": "line", "changed": False}
            snapshot(doc_id, "Edit text line")
            size = size_o or dom.size
            rgb = rgb_o or _int_to_rgb(dom.color)
            # restyle keeps the text (mixed styles collapse to the override)
            new_text = ln.text if text is None else text
            choice = resolver.resolve(dom.font, dom.flags, new_text, family=fam, bold=bold_o, italic=italic_o)
            tc, tw = _measure_spacing(ln.spans, fr, resolver)
            origin = fr.pt_local(ln.origin)
            _remove_runs(page, [b for s in ln.spans if (b := _span_band(s, fr)) is not None])
            _write(page, fr, [Run(origin, new_text, choice.font, size, rgb, dom.hscale, tc * size, tw * size)])
            result.update(font=choice.source, font_size=round(size, 2), fallback=choice.fallback)

        else:  # span: rewrite this span and shift the rest of the line
            fr = _line_frame(ln)
            snapshot(doc_id, "Edit text")
            new_text = sp.text if text is None else text.replace("\n", " ")
            size = size_o or sp.size
            rgb = rgb_o or _int_to_rgb(sp.color)
            choice = resolver.resolve(sp.font, sp.flags, new_text, family=fam, bold=bold_o, italic=italic_o)
            tc, tw = _measure_spacing([s for s in ln.spans if (s.font, s.flags) == (sp.font, sp.flags)], fr, resolver)
            tc, tw = tc * size, tw * size
            idx = ln.spans.index(sp)
            following = ln.spans[idx + 1:]
            o_local = fr.pt_local(sp.origin)
            old_end = _lrect(fr, sp).x1
            # trailing whitespace advances are not in the glyph bbox; keep them.
            # The last glyph's Tc is not in the bbox either.
            new_width = _adv(choice.font, new_text, size, sp.hscale, tc, tw) - (tc if new_text else 0.0)
            new_end = o_local.x + new_width
            shift = fitz.Point(new_end - old_end, 0)
            bands = [_span_band(sp, fr)] + [_span_band(s, fr) for s in following]
            _remove_runs(page, [b for b in bands if b is not None])
            runs = [Run(o_local, new_text, choice.font, size, rgb, sp.hscale, tc, tw)]
            for s in following:
                fch = resolver.resolve(s.font, s.flags, s.text)
                _reinsert_span_exact(s, fr, fch.font, shift, runs)
            _write(page, fr, runs)
            result.update(font=choice.source, font_size=round(size, 2), shifted=round(shift.x, 3),
                          fallback=choice.fallback)
    except BaseException:
        if not doc.is_closed:
            doc.close()
        raise

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

    frames = [_line_frame(l) for l in lines]

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
        b = _span_band(s, _line_frame(l))
        if b is not None:
            bands.append(b)
    snapshot(doc_id, "Delete text")
    _remove_runs(page, bands)
    _save_in_place(doc, path)
    return {"status": "ok", "deleted": req.target.kind}
