"""
Layout-aware text replacement for PDFs.

Fixes three critical bugs in naive PyMuPDF text replacement:

1. **Wrong font weight** -- Reads the original span's `flags` field and maps
   to the correct base-14 variant (e.g. Helvetica, NOT Helvetica-Bold).
2. **Overlap** -- Measures replacement text width *before* inserting.  If it's
   wider than the original bbox, the font size is scaled down to fit (to a
   configurable floor).
3. **Line drift** -- Redacts only the exact span bbox (not the whole line) and
   reinserts at the span's *baseline origin* (not bbox top).

Public API
----------
- ``get_span_info(page, search_text)`` -- find spans + formatting
- ``smart_replace(page, find, replace, target_rect=None)`` -- single-page replace
- ``smart_replace_on_page(page, find, replace)`` -- thin wrapper kept for compat
- ``smart_replace_in_doc(path, find, replace, scope)`` -- whole-doc replace
- ``validate_replacement(page, expected, bbox)`` -- post-edit sanity check

Internal helpers are prefixed with ``_`` and also exported for use by main.py's
edit_text endpoint (``_find_matching_spans``, ``_resolve_font_code``,
``_extract_span_style``, ``_compute_replacement_size``, ``hex_color_to_rgb``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF


# ---------------------------------------------------------------------------
# Font mapping
# ---------------------------------------------------------------------------
# PyMuPDF's get_text("dict") returns internal font names like
# "BCDEEE+Helvetica-Light".  We strip the subset prefix (everything before
# "+") then map to the base-14 short codes that fitz.Font() accepts.

_FONT_FAMILY_MAP: dict[str, str] = {
    # Helvetica / Sans-serif
    "helvetica": "helv",
    "nimbussansregular": "helv",
    "nimbussansbold": "hebo",
    "nimbussansitalic": "heit",
    "nimbussansbolditalic": "hebi",
    "arial": "helv",
    "arialboldmt": "hebo",
    "arialitalicmt": "heit",
    "arialbolditalimt": "hebi",
    "arialmt": "helv",
    # Times / Serif
    "timesroman": "tiro",
    "timesnewromanpsmt": "tiro",
    "timesnewromanpsboldmt": "tibo",
    "timesnewromanpsitalicmt": "tiit",
    "timesnewromanpsbolditalicmt": "tibi",
    "nimbusromanregular": "tiro",
    "nimbusromanbold": "tibo",
    "nimbusromanitalic": "tiit",
    "nimbusromanbolditalic": "tibi",
    # Courier / Monospace
    "courier": "cour",
    "courierneweepsmt": "cour",
    "nimbusmonopsregular": "cour",
    "nimbusmonopsbold": "cobo",
    "nimbusmonopsitalic": "coit",
    "nimbusmonopsbolditalic": "cobi",
}

# Bold/italic variant codes for each base family
_FAMILY_VARIANTS: dict[str, dict[str, str]] = {
    "helv": {"bold": "hebo", "italic": "heit", "bold_italic": "hebi"},
    "tiro": {"bold": "tibo", "italic": "tiit", "bold_italic": "tibi"},
    "cour": {"bold": "cobo", "italic": "coit", "bold_italic": "cobi"},
}

# Minimum font size -- below this text is unreadable
MIN_FONT_SIZE = 6.0

# Maximum shrink ratio (0.7 = won't go below 70% of original size)
MAX_SHRINK_RATIO = 0.7


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SpanStyle:
    """Formatting extracted from an original text span."""
    font_name: str       # PDF internal font name (e.g. "NimbusSans-Regular")
    font_code: str       # fitz.Font() short code (e.g. "helv")
    font_size: float
    color: tuple[float, float, float]  # (r, g, b) in 0..1
    flags: int
    is_bold: bool
    is_italic: bool
    origin: tuple[float, float]  # baseline origin point (x, y)
    bbox: tuple[float, float, float, float]


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

def hex_color_to_rgb(color_int: int) -> tuple[float, float, float]:
    """Convert integer color from PyMuPDF to (r, g, b) floats 0..1."""
    if isinstance(color_int, int):
        r = ((color_int >> 16) & 0xFF) / 255.0
        g = ((color_int >> 8) & 0xFF) / 255.0
        b = (color_int & 0xFF) / 255.0
        return (r, g, b)
    return (0.0, 0.0, 0.0)


# Alias expected by the spec
int_color_to_rgb = hex_color_to_rgb


# ---------------------------------------------------------------------------
# Font resolution
# ---------------------------------------------------------------------------

def _strip_subset_prefix(font_name: str) -> str:
    """Strip the subset prefix (e.g. 'BCDEEE+Helvetica' -> 'Helvetica')."""
    if "+" in font_name:
        font_name = font_name.split("+", 1)[1]
    return font_name


def _resolve_font_code(font_name: str, flags: int) -> str:
    """Map a PDF font name + flags to a fitz.Font() short code.

    FIX #1 (bold bug):  We decode the flags bitmask to determine bold/italic
    and pick the correct base-14 variant.  Previously this defaulted to
    Helvetica-Bold.
    """
    clean_name = _strip_subset_prefix(font_name)
    # Normalize: lowercase, strip spaces/hyphens/underscores
    normalized = re.sub(r"[\s\-_]+", "", clean_name.lower())

    # Decode flags -- PyMuPDF flag bits:
    #   bit 0 = superscript, bit 1 = italic, bit 2 = serif,
    #   bit 3 = monospaced, bit 4 = bold
    is_bold = bool(flags & (1 << 4))
    is_italic = bool(flags & (1 << 1))

    # Try exact lookup first
    if normalized in _FONT_FAMILY_MAP:
        return _FONT_FAMILY_MAP[normalized]

    # Substring-based family detection
    base_code = "helv"  # default sans-serif
    if any(s in normalized for s in ("times", "roman", "serif", "garamond", "georgia")):
        base_code = "tiro"
    elif any(s in normalized for s in ("courier", "mono", "consol", "menlo")):
        base_code = "cour"

    # Also check the monospaced bit
    if bool(flags & (1 << 3)):
        base_code = "cour"

    # Pick the right weight/style variant -- THIS is the core bold-bug fix
    if is_bold and is_italic:
        variant = "bold_italic"
    elif is_bold:
        variant = "bold"
    elif is_italic:
        variant = "italic"
    else:
        return base_code  # regular weight -- NOT bold

    variants = _FAMILY_VARIANTS.get(base_code, _FAMILY_VARIANTS["helv"])
    return variants.get(variant, base_code)


def map_to_base14(font_name: str, is_bold: bool, is_italic: bool) -> str:
    """Map font name + explicit bold/italic bools to a base-14 font name.

    This returns the *display name* (e.g. "Helvetica"), not the short code.
    Useful when callers already decoded the flags themselves.
    """
    name_lower = _strip_subset_prefix(font_name).lower()

    if any(x in name_lower for x in ("courier", "mono", "consolas", "menlo")):
        if is_bold and is_italic:
            return "Courier-BoldOblique"
        elif is_bold:
            return "Courier-Bold"
        elif is_italic:
            return "Courier-Oblique"
        return "Courier"
    elif any(x in name_lower for x in ("times", "serif", "garamond", "georgia")):
        if is_bold and is_italic:
            return "Times-BoldItalic"
        elif is_bold:
            return "Times-Bold"
        elif is_italic:
            return "Times-Italic"
        return "Times-Roman"
    else:
        if is_bold and is_italic:
            return "Helvetica-BoldOblique"
        elif is_bold:
            return "Helvetica-Bold"
        elif is_italic:
            return "Helvetica-Oblique"
        return "Helvetica"


# ---------------------------------------------------------------------------
# Span extraction
# ---------------------------------------------------------------------------

def _extract_span_style(span: dict) -> SpanStyle:
    """Extract formatting information from a PyMuPDF text span dict."""
    flags = span.get("flags", 0)
    font_name = span.get("font", "Helvetica")
    font_code = _resolve_font_code(font_name, flags)
    color = hex_color_to_rgb(span.get("color", 0))
    # "origin" is the baseline start point -- critical for FIX #3
    origin = span.get("origin", (span["bbox"][0], span["bbox"][3]))

    return SpanStyle(
        font_name=font_name,
        font_code=font_code,
        font_size=span["size"],
        color=color,
        flags=flags,
        is_bold=bool(flags & (1 << 4)),
        is_italic=bool(flags & (1 << 1)),
        origin=origin,
        bbox=tuple(span["bbox"]),
    )


def _find_matching_spans(page, search_rect: fitz.Rect) -> list[SpanStyle]:
    """Find all text spans that intersect a search rectangle."""
    styles: list[SpanStyle] = []
    blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
    for block in blocks:
        if block.get("type", 0) != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                span_rect = fitz.Rect(span["bbox"])
                if span_rect.intersects(search_rect) and span["text"].strip():
                    styles.append(_extract_span_style(span))
    return styles


def get_span_info(page, search_text: str) -> list[dict]:
    """Find all spans containing *search_text* and return their exact formatting.

    Returns a list of dicts with keys: text, font, size, color, flags,
    is_bold, is_italic, bbox, origin, line_bbox, ascender, descender.
    """
    matches: list[dict] = []
    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

    for block in text_dict["blocks"]:
        if block.get("type", 0) != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                if search_text in span["text"]:
                    flags = span["flags"]
                    matches.append({
                        "text": span["text"],
                        "font": span["font"],
                        "size": span["size"],
                        "color": span["color"],
                        "flags": flags,
                        "is_bold": bool(flags & (1 << 4)),
                        "is_italic": bool(flags & (1 << 1)),
                        "is_monospaced": bool(flags & (1 << 3)),
                        "bbox": fitz.Rect(span["bbox"]),
                        "origin": span.get("origin"),
                        "line_bbox": fitz.Rect(line["bbox"]),
                        "ascender": span.get("ascender", 0),
                        "descender": span.get("descender", 0),
                    })
    return matches


# ---------------------------------------------------------------------------
# Width measurement
# ---------------------------------------------------------------------------

def measure_text_width(text: str, font_code: str, fontsize: float) -> float:
    """Measure how wide *text* will be at *fontsize* using a base-14 font.

    Uses fitz.Font().text_length() which is exact for base-14 fonts.
    """
    font = fitz.Font(font_code)
    return font.text_length(text, fontsize=fontsize)


def _compute_replacement_size(
    font: fitz.Font,
    old_text: str,
    new_text: str,
    original_size: float,
    original_width: float,
) -> tuple[float, list[str]]:
    """Compute the font size for replacement text to fit the original width.

    FIX #2 (overlap bug):  We measure BEFORE inserting.  If the new text is
    wider, we scale down.  We won't shrink below MAX_SHRINK_RATIO of the
    original size or MIN_FONT_SIZE.

    Returns (adjusted_size, warnings).
    """
    warnings: list[str] = []
    if not new_text:
        return original_size, warnings

    new_width = font.text_length(new_text, fontsize=original_size)

    # 5% tolerance -- minor overflow is less visible than unnecessary shrink
    if new_width <= original_width * 1.05:
        return original_size, warnings

    # Scale down to fit
    ratio = original_width / new_width
    min_allowed = max(original_size * MAX_SHRINK_RATIO, MIN_FONT_SIZE)
    adjusted = original_size * ratio

    if adjusted < min_allowed:
        adjusted = min_allowed
        warnings.append(
            f"Text '{new_text}' is much longer than '{old_text}'. "
            f"Scaled from {original_size:.1f}pt to {adjusted:.1f}pt to fit. "
            f"May still extend slightly beyond original bounds."
        )

    return adjusted, warnings


# ---------------------------------------------------------------------------
# Core replacement
# ---------------------------------------------------------------------------

def smart_replace(
    page,
    find_text: str,
    replace_text: str,
    target_rect: Optional[fitz.Rect] = None,
) -> dict:
    """Replace text on a single page while preserving exact formatting and position.

    Fixes all three bugs:
    - FIX #1: Reads span flags to pick correct font weight variant
    - FIX #2: Measures width and scales down if replacement is wider
    - FIX #3: Redacts only exact span bbox; reinserts at baseline origin

    Args:
        page: A fitz.Page object.
        find_text: Text to search for.
        replace_text: Replacement text.
        target_rect: If set, only replace within this PDF-coordinate rectangle.

    Returns:
        {"replaced": bool, "count": int, "warnings": list[str], "details": list[dict]}
    """
    all_warnings: list[str] = []
    details: list[dict] = []

    spans = get_span_info(page, find_text)

    if target_rect:
        spans = [s for s in spans if s["bbox"].intersects(target_rect)]

    if not spans:
        return {"replaced": False, "count": 0, "warnings": [], "details": []}

    # ── Phase 1: Collect formatting + add redaction annotations ──
    # We gather everything BEFORE applying redactions because apply_redactions
    # mutates the page's internal structure.
    span_data: list[dict] = []
    for span_info in spans:
        original_bbox = span_info["bbox"]
        original_text = span_info["text"]
        original_size = span_info["size"]
        original_color = int_color_to_rgb(span_info["color"])
        flags = span_info["flags"]
        is_bold = span_info["is_bold"]
        is_italic = span_info["is_italic"]

        # FIX #1: Correct font weight via flags
        font_code = _resolve_font_code(span_info["font"], flags)
        font = fitz.Font(font_code)

        # Build the new text (handle partial span replacement)
        new_full_text = original_text.replace(find_text, replace_text)

        # FIX #2: Measure before insert to prevent overlap
        adjusted_size, warnings = _compute_replacement_size(
            font, find_text, new_full_text, original_size, original_bbox.width,
        )
        all_warnings.extend(warnings)

        # FIX #3: Use baseline origin, not bbox top
        if span_info["origin"]:
            insert_x = original_bbox.x0
            insert_y = span_info["origin"][1]  # baseline y
        else:
            # Fallback: approximate baseline from bbox bottom minus descender
            insert_x = original_bbox.x0
            insert_y = original_bbox.y1 - (adjusted_size * 0.15)

        span_data.append({
            "original_bbox": original_bbox,
            "original_text": original_text,
            "new_full_text": new_full_text,
            "font_code": font_code,
            "font": font,
            "original_size": original_size,
            "adjusted_size": adjusted_size,
            "color": original_color,
            "insert_x": insert_x,
            "insert_y": insert_y,
        })

        # FIX #3: Redact ONLY the exact span bbox -- not the whole line
        page.add_redact_annot(original_bbox, fill=(1, 1, 1))

    # Apply ALL redactions at once (avoids coordinate shifts between individual redactions)
    # images=PDF_REDACT_IMAGE_NONE so we don't destroy nearby images
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    # ── Phase 2: Re-insert replacement text at saved positions ──
    for sd in span_data:
        tw = fitz.TextWriter(page.rect)
        tw.append(
            fitz.Point(sd["insert_x"], sd["insert_y"]),
            sd["new_full_text"],
            font=sd["font"],
            fontsize=sd["adjusted_size"],
        )
        tw.write_text(page, color=sd["color"])

        details.append({
            "page": page.number,
            "original": sd["original_text"],
            "replacement": sd["new_full_text"],
            "font": sd["font_code"],
            "original_size": sd["original_size"],
            "adjusted_size": round(sd["adjusted_size"], 1),
            "was_scaled": sd["adjusted_size"] != sd["original_size"],
            "bbox": list(sd["original_bbox"]),
        })

    return {
        "replaced": True,
        "count": len(span_data),
        "warnings": all_warnings,
        "details": details,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_replacement(page, expected_text: str, bbox: fitz.Rect) -> dict:
    """Verify a replacement actually landed correctly.

    Returns {"valid": bool, "issues": list[str], "extracted_text": str}.
    """
    found_text = page.get_text("text", clip=bbox).strip()

    issues: list[str] = []
    if expected_text not in found_text:
        issues.append("replacement_text_not_found_in_region")

    # Check if text extends beyond the original bbox
    new_spans = get_span_info(page, expected_text)
    for span in new_spans:
        if span["bbox"].x1 > bbox.x1 + 2:  # 2pt tolerance
            issues.append("text_extends_beyond_original_bounds")
            break

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "extracted_text": found_text,
    }


# ---------------------------------------------------------------------------
# Compat wrapper (used by existing callers)
# ---------------------------------------------------------------------------

def smart_replace_on_page(
    page,
    find_text: str,
    replace_text: str,
) -> int:
    """Replace all occurrences of *find_text* on a single page.

    Returns the number of replacements made.  This is a thin wrapper around
    smart_replace() kept for backward compatibility.
    """
    result = smart_replace(page, find_text, replace_text)
    return result["count"]


# ---------------------------------------------------------------------------
# Document-level replacement
# ---------------------------------------------------------------------------

def smart_replace_in_doc(
    file_path: Path,
    find_text: str,
    replace_text: str,
    scope: int | str = "all",
) -> tuple[int, list[int]]:
    """Replace text across a PDF document with layout-aware formatting.

    Args:
        file_path: Path to the PDF file (modified in-place).
        find_text: Text to find.
        replace_text: Replacement text.
        scope: ``"all"`` for every page, or a 0-indexed page number.

    Returns:
        ``(total_replaced, list_of_1indexed_page_numbers_with_hits)``
    """
    doc = fitz.open(str(file_path))
    replaced = 0
    page_hits: list[int] = []

    if isinstance(scope, str) and scope != "all":
        try:
            scope = int(scope)
        except (ValueError, TypeError):
            scope = "all"

    pages_to_scan = [scope] if isinstance(scope, int) else range(len(doc))
    for p in pages_to_scan:
        if p < 0 or p >= len(doc):
            continue
        count = smart_replace_on_page(doc[p], find_text, replace_text)
        if count > 0:
            replaced += count
            page_hits.append(p + 1)

    if replaced > 0:
        out_path = str(file_path) + ".tmp"
        doc.save(out_path)
        doc.close()
        os.replace(out_path, str(file_path))
    else:
        doc.close()

    return replaced, page_hits
