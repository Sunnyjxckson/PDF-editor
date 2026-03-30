"""
AI Engine — Claude-powered natural language interface for PDF operations.

Replaces the regex-based intent parser with real LLM intelligence using
Anthropic's Claude API with tool use (function calling).
"""

import os
import re
import json
import logging
from pathlib import Path
from typing import Any

import fitz

logger = logging.getLogger(__name__)

# ── Try to import anthropic SDK ──────────────────────────────────────────────
try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False
    anthropic = None  # type: ignore


# ── PDF operation functions (standalone, importable) ─────────────────────────

def get_doc_path(doc_id: str) -> Path:
    """Resolve document path. Raises FileNotFoundError if missing."""
    path = Path("uploads") / doc_id / "original.pdf"
    if not path.exists():
        raise FileNotFoundError(f"Document {doc_id} not found")
    return path


def hex_color_to_rgb(color_int: int) -> tuple:
    r = ((color_int >> 16) & 0xFF) / 255.0
    g = ((color_int >> 8) & 0xFF) / 255.0
    b = (color_int & 0xFF) / 255.0
    return (r, g, b)


def get_full_text(doc_id: str) -> dict[int, str]:
    """Get text for all pages. Returns {page_index: text}."""
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    pages = {}
    for i, page in enumerate(doc):
        pages[i] = page.get_text()
    doc.close()
    return pages


def op_get_page_count(doc_id: str) -> dict:
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    count = len(doc)
    doc.close()
    return {"response": f"This document has {count} pages.", "changed": False}


def op_query_count(doc_id: str, term: str) -> dict:
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    total = 0
    page_hits = {}
    for i, page in enumerate(doc):
        matches = page.search_for(term)
        if matches:
            total += len(matches)
            page_hits[i + 1] = len(matches)
    doc.close()
    if total == 0:
        return {"response": f'I couldn\'t find "{term}" anywhere in the document.', "changed": False}
    pages_str = ", ".join(f"page {p} ({c}x)" for p, c in page_hits.items())
    return {
        "response": f'Found **{total}** instance{"s" if total != 1 else ""} of "{term}" on {pages_str}.',
        "changed": False,
    }


def op_replace_text(doc_id: str, find_str: str, replace_str: str, scope: str | int = "all") -> dict:
    from backend.smart_replace import smart_replace_in_doc

    file_path = get_doc_path(doc_id)
    replaced, page_hits = smart_replace_in_doc(file_path, find_str, replace_str, scope)

    if replaced == 0:
        return {"response": f'I couldn\'t find "{find_str}" in the document. No changes made.', "changed": False}
    pages_str = ", ".join(str(p) for p in page_hits)
    return {
        "response": f'Replaced **{replaced}** instance{"s" if replaced != 1 else ""} of "{find_str}" with "{replace_str}" on page{"s" if len(page_hits) > 1 else ""} {pages_str}.',
        "changed": True,
    }


def op_delete_text(doc_id: str, find_str: str) -> dict:
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    deleted = 0
    page_hits = []
    for i, page in enumerate(doc):
        matches = page.search_for(find_str)
        if matches:
            for rect in matches:
                page.add_redact_annot(rect)
            page.apply_redactions()
            deleted += len(matches)
            page_hits.append(i + 1)
    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))

    if deleted == 0:
        return {"response": f'I couldn\'t find "{find_str}" in the document.', "changed": False}
    return {
        "response": f'Removed **{deleted}** instance{"s" if deleted != 1 else ""} of "{find_str}" from page{"s" if len(page_hits) > 1 else ""} {", ".join(str(p) for p in page_hits)}.',
        "changed": True,
    }


def op_delete_pages(doc_id: str, start: int, end: int) -> dict:
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    total = len(doc)
    if start < 0 or end >= total:
        doc.close()
        return {"response": f"Invalid page range. Document has {total} pages.", "changed": False}
    pages_to_delete = list(range(start, end + 1))
    pages_to_keep = [i for i in range(total) if i not in pages_to_delete]
    if not pages_to_keep:
        doc.close()
        return {"response": "Can't delete all pages.", "changed": False}
    doc.select(pages_to_keep)
    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))
    count = len(pages_to_delete)
    return {
        "response": f'Deleted {"page" if count == 1 else "pages"} {start + 1}{"–" + str(end + 1) if end != start else ""}. Document now has {len(pages_to_keep)} pages.',
        "changed": True,
        "page_count_changed": True,
    }


def op_rotate_page(doc_id: str, page_num: int) -> dict:
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    if page_num < 0 or page_num >= len(doc):
        doc.close()
        return {"response": f"Page {page_num + 1} doesn't exist.", "changed": False}
    page = doc[page_num]
    current_rot = page.rotation
    new_rot = (current_rot + 90) % 360
    page.set_rotation(new_rot)
    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))
    return {"response": f"Rotated page {page_num + 1} to {new_rot} degrees.", "changed": True}


def op_extract_data(doc_id: str, extract_type: str) -> dict:
    pages_text = get_full_text(doc_id)
    all_text = "\n".join(pages_text.values())

    if extract_type == "emails":
        items = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", all_text)
    elif extract_type == "phones":
        items = re.findall(r"[\+]?[(]?[0-9]{1,4}[)]?[-\s\./0-9]{7,}", all_text)
        items = [p.strip() for p in items if len(p.strip()) >= 7]
    elif extract_type == "dates":
        items = re.findall(
            r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? \d{4}\b",
            all_text,
        )
    elif extract_type == "urls":
        items = re.findall(r"https?://[^\s<>\"']+", all_text)
    else:
        items = []

    items = list(dict.fromkeys(items))
    if not items:
        return {"response": f"No {extract_type} found in the document.", "changed": False}
    items_list = "\n".join(f"  {i + 1}. {item}" for i, item in enumerate(items))
    return {
        "response": f"Found **{len(items)}** {extract_type}:\n\n{items_list}",
        "changed": False,
    }


def op_redact_pattern(doc_id: str, pattern_type: str) -> dict:
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    redacted = 0
    regex_map = {
        "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
        "phone": r"[\+]?[(]?[0-9]{1,4}[)]?[-\s\./0-9]{7,}",
        "email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        "date": r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
    }
    regex = regex_map.get(pattern_type)
    if not regex:
        doc.close()
        return {"response": f"Unknown pattern type: {pattern_type}", "changed": False}

    for page in doc:
        text = page.get_text()
        for m in re.finditer(regex, text):
            matches = page.search_for(m.group())
            for rect in matches:
                page.add_redact_annot(rect, fill=(0, 0, 0))
                redacted += 1
        page.apply_redactions()

    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))

    if redacted == 0:
        return {"response": f"No {pattern_type} patterns found to redact.", "changed": False}
    return {
        "response": f'Redacted **{redacted}** {pattern_type} pattern{"s" if redacted != 1 else ""}. The content has been permanently blacked out.',
        "changed": True,
    }


def op_add_text(doc_id: str, text: str, page_num: int, x: float = 72, y: float = 72, font_size: float = 12) -> dict:
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    if page_num < 0 or page_num >= len(doc):
        doc.close()
        return {"response": f"Page {page_num + 1} doesn't exist.", "changed": False}
    page = doc[page_num]
    tw = fitz.TextWriter(page.rect)
    font = fitz.Font("helv")
    tw.append(fitz.Point(x, y), text, font=font, fontsize=font_size)
    tw.write_text(page, color=(0, 0, 0))
    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))
    return {"response": f'Added text "{text}" to page {page_num + 1}.', "changed": True}


def op_search_text(doc_id: str, query: str) -> dict:
    return op_query_count(doc_id, query)


def op_get_word_count(doc_id: str) -> dict:
    pages_text = get_full_text(doc_id)
    all_text = " ".join(pages_text.values())
    words = len(all_text.split())
    chars = len(all_text)
    return {
        "response": f"**Document statistics:**\n- Words: {words:,}\n- Characters: {chars:,}\n- Pages: {len(pages_text)}",
        "changed": False,
    }


def op_summarize(doc_id: str) -> dict:
    pages_text = get_full_text(doc_id)
    all_text = " ".join(pages_text.values())
    sentences = [s.strip() for s in all_text.replace("\n", " ").split(".") if s.strip() and len(s.strip()) > 10]
    if len(sentences) <= 3:
        summary = ". ".join(sentences) + "." if sentences else "No text found."
    else:
        picks = [sentences[0], sentences[len(sentences) // 3], sentences[2 * len(sentences) // 3], sentences[-1]]
        summary = ". ".join(picks) + "."
    return {"response": f"**Summary:**\n\n{summary}", "changed": False}


def op_get_page_text(doc_id: str, page_number: int) -> dict:
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    if page_number < 0 or page_number >= len(doc):
        doc.close()
        return {"response": f"Page {page_number + 1} doesn't exist.", "changed": False}
    text = doc[page_number].get_text()
    doc.close()
    if not text.strip():
        return {"response": f"Page {page_number + 1} contains no extractable text.", "changed": False}
    return {"response": f"**Page {page_number + 1} text:**\n\n{text}", "changed": False}


def op_reorder_pages(doc_id: str, new_order: list[int]) -> dict:
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    total = len(doc)
    if sorted(new_order) != list(range(total)):
        doc.close()
        return {"response": f"Invalid page order. Must be a permutation of 0-{total - 1}.", "changed": False}
    doc.select(new_order)
    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))
    display_order = [str(p + 1) for p in new_order]
    return {"response": f"Pages reordered to: {', '.join(display_order)}.", "changed": True}


def op_split_document(doc_id: str, page_ranges: list[list[int]]) -> dict:
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    total = len(doc)
    new_doc_ids = []

    for page_range in page_ranges:
        if len(page_range) != 2:
            doc.close()
            return {"response": "Each range must be [start, end] (0-indexed).", "changed": False}
        start, end = page_range
        if start < 0 or end >= total or start > end:
            doc.close()
            return {"response": f"Invalid range [{start}, {end}]. Document has {total} pages.", "changed": False}

        import uuid
        new_id = str(uuid.uuid4())
        new_dir = Path("uploads") / new_id
        new_dir.mkdir(parents=True, exist_ok=True)

        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=start, to_page=end)
        new_doc.save(str(new_dir / "original.pdf"))
        new_doc.close()
        new_doc_ids.append(new_id)

    doc.close()
    ranges_str = ", ".join(f"pages {r[0]+1}-{r[1]+1}" for r in page_ranges)
    return {
        "response": f"Split document into {len(new_doc_ids)} parts ({ranges_str}). New document IDs: {', '.join(new_doc_ids)}",
        "changed": False,
        "new_doc_ids": new_doc_ids,
    }


def op_merge_documents(doc_id: str, other_doc_ids: list[str]) -> dict:
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))

    for other_id in other_doc_ids:
        other_path = get_doc_path(other_id)
        other_doc = fitz.open(str(other_path))
        doc.insert_pdf(other_doc)
        other_doc.close()

    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))
    return {
        "response": f"Merged {len(other_doc_ids)} document(s) into the current document.",
        "changed": True,
        "page_count_changed": True,
    }


def op_add_highlight(doc_id: str, page_num: int, rect: list[float], color: list[float] | None = None) -> dict:
    file_path = get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    if page_num < 0 or page_num >= len(doc):
        doc.close()
        return {"response": f"Page {page_num + 1} doesn't exist.", "changed": False}
    page = doc[page_num]
    r = fitz.Rect(rect)
    annot = page.add_highlight_annot(r)
    if color:
        annot.set_colors(stroke=color)
        annot.update()
    out_path = str(file_path) + ".tmp"
    doc.save(out_path)
    doc.close()
    os.replace(out_path, str(file_path))
    return {"response": f"Added highlight on page {page_num + 1}.", "changed": True}


# ── Tool definitions for Claude API ──────────────────────────────────────────

TOOLS = [
    {
        "name": "replace_text",
        "description": "Find and replace text in the PDF. Use scope='all' for all pages or a 0-indexed page number for a specific page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "find": {"type": "string", "description": "Text to find"},
                "replace": {"type": "string", "description": "Text to replace with"},
                "scope": {"type": "string", "description": "'all' for all pages, or a 0-indexed page number (e.g. '0' for page 1)", "default": "all"},
            },
            "required": ["find", "replace"],
        },
    },
    {
        "name": "delete_text",
        "description": "Delete all instances of text from the document.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to delete"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "delete_pages",
        "description": "Delete a range of pages (0-indexed). Both start and end are inclusive.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "integer", "description": "Start page index (0-indexed)"},
                "end": {"type": "integer", "description": "End page index (0-indexed, inclusive)"},
            },
            "required": ["start", "end"],
        },
    },
    {
        "name": "rotate_page",
        "description": "Rotate a page 90 degrees clockwise.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_number": {"type": "integer", "description": "Page index (0-indexed)"},
            },
            "required": ["page_number"],
        },
    },
    {
        "name": "add_text",
        "description": "Add new text to a page at specified coordinates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to add"},
                "page": {"type": "integer", "description": "Page index (0-indexed)"},
                "x": {"type": "number", "description": "X coordinate (default 72)", "default": 72},
                "y": {"type": "number", "description": "Y coordinate (default 72)", "default": 72},
                "font_size": {"type": "number", "description": "Font size (default 12)", "default": 12},
            },
            "required": ["text", "page"],
        },
    },
    {
        "name": "extract_data",
        "description": "Extract structured data from the document: emails, phones, dates, or urls.",
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["emails", "phones", "dates", "urls"], "description": "Type of data to extract"},
            },
            "required": ["type"],
        },
    },
    {
        "name": "redact_pattern",
        "description": "Permanently redact (black out) all instances of a pattern type. WARNING: This is irreversible.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern_type": {"type": "string", "enum": ["ssn", "phone", "email", "date"], "description": "Type of pattern to redact"},
            },
            "required": ["pattern_type"],
        },
    },
    {
        "name": "search_text",
        "description": "Search for text and count occurrences across all pages.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to search for"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_page_count",
        "description": "Get the total number of pages in the document.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_word_count",
        "description": "Get word count, character count, and page count for the document.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "summarize_document",
        "description": "Generate an extractive summary of the document content.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_page_text",
        "description": "Get the full text content of a specific page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_number": {"type": "integer", "description": "Page index (0-indexed)"},
            },
            "required": ["page_number"],
        },
    },
    {
        "name": "add_highlight",
        "description": "Add a highlight annotation to a rectangular region on a page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page": {"type": "integer", "description": "Page index (0-indexed)"},
                "rect": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Rectangle as [x0, y0, x1, y1]",
                },
                "color": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "RGB color as [r, g, b] with values 0-1. Default is yellow.",
                },
            },
            "required": ["page", "rect"],
        },
    },
    {
        "name": "merge_documents",
        "description": "Merge other uploaded PDFs into the current document.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of document IDs to merge into this document",
                },
            },
            "required": ["doc_ids"],
        },
    },
    {
        "name": "split_document",
        "description": "Split the document into multiple PDFs by page ranges.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_ranges": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "description": "List of [start, end] page ranges (0-indexed, inclusive)",
                },
            },
            "required": ["page_ranges"],
        },
    },
    {
        "name": "reorder_pages",
        "description": "Reorder pages in the document. Provide the new order as a list of 0-indexed page numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "new_order": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "New page order as list of 0-indexed page numbers (must be a permutation)",
                },
            },
            "required": ["new_order"],
        },
    },
]


SYSTEM_PROMPT = """\
You are an intelligent PDF editing assistant embedded inside an AI PDF Editor application. \
Users interact with you through a chat panel while viewing their PDF document.

Your capabilities:
- You can read, search, and analyze the document text provided to you
- You can execute PDF operations using the tools available to you
- You can chain multiple operations in a single response when the user requests it
- You understand natural language and can infer intent even from vague requests

Important guidelines:
1. **Page numbering**: Users speak in 1-indexed pages ("page 1" = first page), but the tools use 0-indexed. Always convert: user's page N → tool's page N-1.
2. **Destructive operations**: For delete, redact, or irreversible actions, briefly confirm what you're about to do in your response. If the user already specified exactly what they want, proceed.
3. **Be concise**: Give short, direct responses. Don't over-explain unless the user asks.
4. **Document context**: You receive the current page text and document metadata. Use this to answer questions about document content directly without needing a tool call.
5. **Multi-step tasks**: If a request involves multiple operations, execute them in sequence using multiple tool calls.
6. **Error handling**: If a tool returns an error, explain what went wrong in plain language.
7. **Questions about content**: When users ask about what's in the document (e.g., "what is this about?", "who wrote this?"), answer directly from the document text provided — don't call a tool unless you need text from a different page.
8. **Format responses**: Use **bold** for emphasis. Keep responses scannable.
"""


def _build_document_context(doc_id: str, current_page: int, page_text: str, page_count: int) -> str:
    """Build document context string to include in the LLM request."""
    context_parts = [
        f"Document info: {page_count} pages total. User is viewing page {current_page + 1}.",
        f"\n--- Current page ({current_page + 1}) text ---\n{page_text[:4000]}" if page_text.strip() else f"\nPage {current_page + 1} has no extractable text.",
    ]

    # For short documents, include full text
    if page_count <= 20:
        try:
            pages_text = get_full_text(doc_id)
            full_parts = []
            for i in range(page_count):
                pg_text = pages_text.get(i, "").strip()
                if pg_text and i != current_page:
                    full_parts.append(f"\n--- Page {i + 1} text ---\n{pg_text[:2000]}")
            if full_parts:
                context_parts.append("\nOther pages:" + "".join(full_parts))
        except Exception:
            pass
    else:
        # For long documents, include first line of each page as outline
        try:
            pages_text = get_full_text(doc_id)
            outline_parts = []
            for i in range(page_count):
                pg_text = pages_text.get(i, "").strip()
                if pg_text:
                    first_line = pg_text.split("\n")[0][:100]
                    outline_parts.append(f"  Page {i + 1}: {first_line}")
            if outline_parts:
                context_parts.append("\nDocument outline (first line of each page):\n" + "\n".join(outline_parts))
        except Exception:
            pass

    return "\n".join(context_parts)


def _execute_tool(doc_id: str, tool_name: str, tool_input: dict, current_page: int) -> dict:
    """Execute a tool call and return the result."""
    try:
        if tool_name == "replace_text":
            scope = tool_input.get("scope", "all")
            if scope != "all":
                try:
                    scope = int(scope)
                except (ValueError, TypeError):
                    scope = "all"
            return op_replace_text(doc_id, tool_input["find"], tool_input["replace"], scope)

        elif tool_name == "delete_text":
            return op_delete_text(doc_id, tool_input["text"])

        elif tool_name == "delete_pages":
            return op_delete_pages(doc_id, tool_input["start"], tool_input["end"])

        elif tool_name == "rotate_page":
            return op_rotate_page(doc_id, tool_input["page_number"])

        elif tool_name == "add_text":
            return op_add_text(
                doc_id,
                tool_input["text"],
                tool_input["page"],
                tool_input.get("x", 72),
                tool_input.get("y", 72),
                tool_input.get("font_size", 12),
            )

        elif tool_name == "extract_data":
            return op_extract_data(doc_id, tool_input["type"])

        elif tool_name == "redact_pattern":
            return op_redact_pattern(doc_id, tool_input["pattern_type"])

        elif tool_name == "search_text":
            return op_search_text(doc_id, tool_input["query"])

        elif tool_name == "get_page_count":
            return op_get_page_count(doc_id)

        elif tool_name == "get_word_count":
            return op_get_word_count(doc_id)

        elif tool_name == "summarize_document":
            return op_summarize(doc_id)

        elif tool_name == "get_page_text":
            return op_get_page_text(doc_id, tool_input["page_number"])

        elif tool_name == "add_highlight":
            return op_add_highlight(
                doc_id,
                tool_input["page"],
                tool_input["rect"],
                tool_input.get("color"),
            )

        elif tool_name == "merge_documents":
            return op_merge_documents(doc_id, tool_input["doc_ids"])

        elif tool_name == "split_document":
            return op_split_document(doc_id, tool_input["page_ranges"])

        elif tool_name == "reorder_pages":
            return op_reorder_pages(doc_id, tool_input["new_order"])

        else:
            return {"response": f"Unknown tool: {tool_name}", "changed": False}

    except FileNotFoundError as e:
        return {"response": str(e), "changed": False}
    except Exception as e:
        logger.exception(f"Tool execution error: {tool_name}")
        return {"response": f"Error executing {tool_name}: {str(e)}", "changed": False}


def _convert_chat_history(history: list[dict]) -> list[dict]:
    """Convert internal chat history format to Anthropic API messages format."""
    messages = []
    for entry in history[-20:]:  # Keep last 20 messages for context window
        if entry["role"] == "user":
            messages.append({"role": "user", "content": entry["content"]})
        elif entry["role"] == "assistant":
            messages.append({"role": "assistant", "content": entry["content"]})
    return messages


async def understand_and_execute(
    message: str,
    doc_id: str,
    current_page: int,
    page_text: str,
    page_count: int,
    chat_history: list[dict],
) -> dict:
    """
    Use Claude API to understand the user's message and execute PDF operations.

    Returns: {response: str, changed: bool, page_count_changed: bool|None, intent: dict}
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not HAS_ANTHROPIC:
        return {
            "response": "The `anthropic` Python package is not installed. Install it with `pip install anthropic` to enable AI chat.",
            "changed": False,
            "intent": {"action": "error", "reason": "missing_sdk"},
        }

    if not api_key:
        return {
            "response": "No API key configured. Set the `ANTHROPIC_API_KEY` environment variable to enable AI-powered chat. Falling back to basic command parsing.",
            "changed": False,
            "intent": {"action": "error", "reason": "no_api_key"},
        }

    client = anthropic.Anthropic(api_key=api_key)
    doc_context = _build_document_context(doc_id, current_page, page_text, page_count)

    # Build messages: history + current message with document context
    messages = _convert_chat_history(chat_history)
    user_content = f"{message}\n\n[Document context]\n{doc_context}"
    messages.append({"role": "user", "content": user_content})

    overall_changed = False
    page_count_changed = False
    tool_results_text = []
    intent_info: dict[str, Any] = {"action": "ai_chat"}
    max_iterations = 10  # Safety limit for tool-use loops

    for _ in range(max_iterations):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
        except anthropic.AuthenticationError:
            return {
                "response": "Invalid API key. Please check your `ANTHROPIC_API_KEY` environment variable.",
                "changed": False,
                "intent": {"action": "error", "reason": "auth_failed"},
            }
        except anthropic.RateLimitError:
            return {
                "response": "Rate limit exceeded. Please try again in a moment.",
                "changed": False,
                "intent": {"action": "error", "reason": "rate_limit"},
            }
        except Exception as e:
            logger.exception("Claude API error")
            return {
                "response": f"AI service error: {str(e)}",
                "changed": False,
                "intent": {"action": "error", "reason": "api_error"},
            }

        # Process response blocks
        assistant_text_parts = []
        tool_use_blocks = []

        for block in response.content:
            if block.type == "text":
                assistant_text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_use_blocks.append(block)

        # If no tool calls, we're done
        if not tool_use_blocks:
            final_text = "\n".join(assistant_text_parts)
            if tool_results_text:
                # Append accumulated tool result summaries if the model didn't restate them
                pass
            return {
                "response": final_text,
                "changed": overall_changed,
                "page_count_changed": page_count_changed or None,
                "intent": intent_info,
            }

        # Append the assistant's full response (text + tool_use blocks) to messages
        messages.append({"role": "assistant", "content": response.content})

        # Execute each tool and collect results
        tool_result_blocks = []
        for tool_block in tool_use_blocks:
            result = _execute_tool(doc_id, tool_block.name, tool_block.input, current_page)

            if result.get("changed"):
                overall_changed = True
            if result.get("page_count_changed"):
                page_count_changed = True

            intent_info["action"] = tool_block.name
            intent_info["input"] = tool_block.input
            tool_results_text.append(result["response"])

            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "content": result["response"],
            })

        messages.append({"role": "user", "content": tool_result_blocks})
        # Loop continues — Claude may want to call more tools or produce final text

    # If we exhausted iterations, return what we have
    return {
        "response": "\n".join(tool_results_text) if tool_results_text else "I completed the operations but ran into complexity. Please check the document.",
        "changed": overall_changed,
        "page_count_changed": page_count_changed or None,
        "intent": intent_info,
    }
