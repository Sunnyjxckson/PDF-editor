"""
Document Intelligence — deep PDF analysis using PyMuPDF.

Provides structural analysis, text intelligence, table extraction,
form detection, entity extraction, and language detection for uploaded PDFs.
"""

import json
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

UPLOAD_DIR = Path("uploads")


def _get_doc_path(doc_id: str) -> Path:
    path = UPLOAD_DIR / doc_id / "original.pdf"
    if not path.exists():
        raise FileNotFoundError(f"Document {doc_id} not found")
    return path


def _get_analysis_path(doc_id: str) -> Path:
    return UPLOAD_DIR / doc_id / "analysis.json"


def load_analysis(doc_id: str) -> dict | None:
    path = _get_analysis_path(doc_id)
    if path.exists():
        return json.loads(path.read_text())
    return None


def save_analysis(doc_id: str, analysis: dict):
    path = _get_analysis_path(doc_id)
    path.write_text(json.dumps(analysis, default=str))


# ── Structural Analysis ────────────────────────────────────────────────────


def _detect_headings(blocks: list[dict], avg_font_size: float) -> list[dict]:
    """Detect headings using font size, boldness, and formatting heuristics."""
    headings = []
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span["text"].strip()
                if not text:
                    continue
                size = span["size"]
                flags = span["flags"]
                is_bold = bool(flags & 2**4)  # bit 4 = bold
                is_large = size >= avg_font_size + 2
                is_allcaps = text == text.upper() and len(text) > 3 and text.isalpha()

                level = 0
                if is_large and is_bold:
                    level = 1
                elif is_large:
                    level = 2
                elif is_bold and is_allcaps:
                    level = 2
                elif is_bold:
                    level = 3
                elif is_allcaps:
                    level = 4

                if level > 0:
                    headings.append({
                        "text": text,
                        "level": level,
                        "font_size": size,
                        "bold": is_bold,
                        "bbox": list(span["bbox"]),
                    })
    return headings


def _classify_blocks(blocks: list[dict], avg_font_size: float) -> list[dict]:
    """Classify text blocks into paragraphs, headings, lists, etc."""
    classified = []
    for block in blocks:
        if block.get("type") == 1:
            classified.append({
                "type": "image",
                "bbox": list(block["bbox"]),
                "width": block.get("width", 0),
                "height": block.get("height", 0),
            })
            continue
        if block.get("type") != 0:
            continue

        full_text = ""
        spans_info = []
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                full_text += span["text"]
                spans_info.append({
                    "text": span["text"],
                    "size": span["size"],
                    "flags": span["flags"],
                })

        text = full_text.strip()
        if not text:
            continue

        # Detect lists (starts with bullet, dash, number+period)
        is_list = bool(re.match(r'^[\u2022\u2023\u25E6\u2043\-\*]\s', text) or
                       re.match(r'^\d+[\.\)]\s', text) or
                       re.match(r'^[a-zA-Z][\.\)]\s', text))

        # Detect headings
        max_size = max((s["size"] for s in spans_info), default=avg_font_size)
        any_bold = any(s["flags"] & 2**4 for s in spans_info)
        is_heading = max_size >= avg_font_size + 2 or (any_bold and len(text) < 100)

        block_type = "heading" if is_heading else ("list_item" if is_list else "paragraph")

        classified.append({
            "type": block_type,
            "text": text,
            "bbox": list(block["bbox"]),
            "font_size": max_size,
            "bold": any_bold,
        })

    return classified


def _compute_avg_font_size(doc: fitz.Document) -> float:
    """Compute the average font size across the document."""
    sizes = []
    for page in doc:
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if span["text"].strip():
                        sizes.append(span["size"])
    return statistics.mean(sizes) if sizes else 12.0


def analyze_structure(doc_id: str) -> dict:
    """Analyze document structure: headings, paragraphs, lists, images, links."""
    file_path = _get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    avg_font_size = _compute_avg_font_size(doc)

    toc = doc.get_toc()
    pages_structure = []

    for page_num, page in enumerate(doc):
        blocks = page.get_text("dict")["blocks"]
        headings = _detect_headings(blocks, avg_font_size)
        classified = _classify_blocks(blocks, avg_font_size)
        images = page.get_images(full=True)
        links = page.get_links()

        pages_structure.append({
            "page": page_num,
            "width": page.rect.width,
            "height": page.rect.height,
            "headings": headings,
            "blocks": classified,
            "image_count": len(images),
            "images": [
                {
                    "xref": img[0],
                    "width": img[2],
                    "height": img[3],
                }
                for img in images
            ],
            "link_count": len(links),
            "links": [
                {
                    "type": link.get("kind", 0),
                    "uri": link.get("uri", ""),
                    "bbox": list(link.get("from", fitz.Rect()).irect) if "from" in link else [],
                }
                for link in links
            ],
        })

    doc.close()

    # Build section hierarchy from headings
    sections = []
    for ps in pages_structure:
        for h in ps["headings"]:
            sections.append({
                "title": h["text"],
                "level": h["level"],
                "page": ps["page"],
            })

    return {
        "table_of_contents": [{"level": t[0], "title": t[1], "page": t[2]} for t in toc],
        "sections": sections,
        "pages": pages_structure,
        "avg_font_size": round(avg_font_size, 1),
    }


# ── Table Intelligence ─────────────────────────────────────────────────────


def extract_tables(doc_id: str) -> list[dict]:
    """Extract tables from all pages using PyMuPDF's table detection."""
    file_path = _get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    all_tables = []

    for page_num, page in enumerate(doc):
        try:
            tables = page.find_tables()
        except Exception:
            continue

        for table_idx, table in enumerate(tables):
            try:
                data = table.extract()
            except Exception:
                continue

            if not data or len(data) < 2:
                continue

            # First row as headers, rest as data rows
            headers = [str(cell) if cell else f"col_{i}" for i, cell in enumerate(data[0])]
            rows_as_dicts = []
            for row in data[1:]:
                row_dict = {}
                for i, cell in enumerate(row):
                    key = headers[i] if i < len(headers) else f"col_{i}"
                    row_dict[key] = str(cell) if cell else ""
                rows_as_dicts.append(row_dict)

            all_tables.append({
                "page": page_num,
                "table_index": table_idx,
                "bbox": list(table.bbox) if hasattr(table, "bbox") else [],
                "headers": headers,
                "row_count": len(data) - 1,
                "col_count": len(headers),
                "rows": rows_as_dicts,
                "raw": data,
            })

    doc.close()
    return all_tables


def query_table(tables: list[dict], query: str) -> str:
    """Answer a natural-language question about table data (simple heuristic)."""
    query_lower = query.lower()

    # Try to find a "total" or "sum" request
    sum_match = re.search(r'(?:total|sum|add up)\s+(?:of\s+)?(?:the\s+)?["\']?(.+?)["\']?\s*(?:column|col)?', query_lower)
    if sum_match:
        target_col = sum_match.group(1).strip()
        for table in tables:
            for header in table["headers"]:
                if target_col in header.lower():
                    values = []
                    for row in table["rows"]:
                        val = row.get(header, "")
                        # Strip currency symbols and commas
                        cleaned = re.sub(r'[,$\u20ac\u00a3%]', '', val).strip()
                        try:
                            values.append(float(cleaned))
                        except ValueError:
                            pass
                    if values:
                        total = sum(values)
                        return f"The total of '{header}' is {total:,.2f} (from {len(values)} values on page {table['page'] + 1})."
        return "Could not find a matching numeric column to sum."

    # Try to find a specific value lookup
    for table in tables:
        for row in table["rows"]:
            for key, val in row.items():
                if query_lower in str(val).lower() or query_lower in key.lower():
                    return f"Found in table on page {table['page'] + 1}: {dict(row)}"

    if tables:
        summary_parts = []
        for t in tables:
            summary_parts.append(
                f"Page {t['page'] + 1}: {t['row_count']} rows x {t['col_count']} cols, headers: {t['headers']}"
            )
        return "Tables found:\n" + "\n".join(summary_parts)

    return "No tables found in the document."


def tables_to_csv(table: dict) -> str:
    """Convert a single extracted table to CSV format."""
    lines = [",".join(f'"{h}"' for h in table["headers"])]
    for row in table["rows"]:
        cells = [f'"{row.get(h, "")}"' for h in table["headers"]]
        lines.append(",".join(cells))
    return "\n".join(lines)


# ── Entity Extraction ──────────────────────────────────────────────────────

_ENTITY_PATTERNS = {
    "emails": r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    "phones": r"[\+]?[(]?[0-9]{1,4}[)]?[-\s\./0-9]{7,}",
    "dates": r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{2,4}\b",
    "urls": r"https?://[^\s<>\"']+",
    "monetary": r"[\$\u20ac\u00a3]\s?\d[\d,]*\.?\d*\b|\b\d[\d,]*\.\d{2}\b(?:\s?(?:USD|EUR|GBP|dollars?|euros?))?",
    "ssns": r"\b\d{3}-\d{2}-\d{4}\b",
}


def extract_entities(doc_id: str) -> dict[str, list[dict]]:
    """Extract named entities: emails, phones, dates, monetary amounts, urls, people, organizations."""
    file_path = _get_doc_path(doc_id)
    doc = fitz.open(str(file_path))

    entities: dict[str, list[dict]] = {k: [] for k in _ENTITY_PATTERNS}
    entities["people"] = []
    entities["organizations"] = []
    seen: dict[str, set] = {k: set() for k in entities}

    for page_num, page in enumerate(doc):
        text = page.get_text()

        # Regex-based entity extraction
        for entity_type, pattern in _ENTITY_PATTERNS.items():
            for m in re.finditer(pattern, text):
                value = m.group().strip()
                if entity_type == "phones" and len(value) < 7:
                    continue
                if value not in seen[entity_type]:
                    seen[entity_type].add(value)
                    entities[entity_type].append({
                        "value": value,
                        "page": page_num,
                    })

        # Simple people/org detection heuristics
        # Capitalized multi-word sequences that look like names (2-3 capitalized words)
        for m in re.finditer(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b', text):
            name = m.group(1)
            # Filter out common non-name patterns
            if name not in seen["people"] and not re.match(
                r'^(?:The |This |That |These |Those |January|February|March|April|May|June|July|August|September|October|November|December)',
                name
            ):
                seen["people"].add(name)
                entities["people"].append({"value": name, "page": page_num})

        # Organizations: words ending in common suffixes
        for m in re.finditer(
            r'\b([A-Z][\w&\-]*(?:\s+[A-Z][\w&\-]*)*\s+(?:Inc|LLC|Ltd|Corp|Corporation|Company|Co|Group|Foundation|Institute|Association|University|Bank|Partners)\.?)\b',
            text
        ):
            org = m.group(1)
            if org not in seen["organizations"]:
                seen["organizations"].add(org)
                entities["organizations"].append({"value": org, "page": page_num})

    doc.close()
    return entities


# ── Text Intelligence ──────────────────────────────────────────────────────


def analyze_document(doc_id: str) -> dict:
    """Full document analysis: structure, key topics, entities, summary."""
    structure = analyze_structure(doc_id)
    entities = extract_entities(doc_id)
    tables = extract_tables(doc_id)

    # Build a summary from the document text
    file_path = _get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    all_text = ""
    page_count = len(doc)
    for page in doc:
        all_text += page.get_text() + "\n"
    doc.close()

    # Extract key topics via word frequency (simple TF approach)
    words = re.findall(r'\b[a-zA-Z]{4,}\b', all_text.lower())
    # Filter common stop words
    stop_words = {
        "this", "that", "with", "from", "have", "been", "were", "will", "would",
        "could", "should", "their", "there", "they", "them", "than", "then",
        "also", "each", "which", "when", "what", "where", "about", "into",
        "more", "some", "such", "only", "over", "other", "after", "before",
        "between", "through", "under", "these", "those", "does", "done",
        "being", "very", "just", "because", "most", "many", "much", "your",
        "page", "document",
    }
    filtered = [w for w in words if w not in stop_words]
    word_freq = Counter(filtered)
    key_topics = [{"word": w, "count": c} for w, c in word_freq.most_common(20)]

    # Extractive summary: pick key sentences
    sentences = [s.strip() for s in re.split(r'[.!?]+', all_text.replace("\n", " ")) if len(s.strip()) > 20]
    if len(sentences) <= 5:
        summary = ". ".join(sentences) + "." if sentences else "No extractable text."
    else:
        indices = [0, len(sentences) // 4, len(sentences) // 2, 3 * len(sentences) // 4, -1]
        summary = ". ".join(sentences[i] for i in indices) + "."

    analysis = {
        "doc_id": doc_id,
        "page_count": page_count,
        "character_count": len(all_text),
        "word_count": len(all_text.split()),
        "structure": structure,
        "key_topics": key_topics,
        "entities": entities,
        "table_count": len(tables),
        "tables": tables,
        "summary": summary,
    }

    # Cache the analysis
    save_analysis(doc_id, analysis)
    return analysis


def find_similar_sections(doc_id: str, query: str) -> list[dict]:
    """Find sections of the document relevant to a query using keyword matching."""
    file_path = _get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    query_words = set(query.lower().split())
    results = []

    for page_num, page in enumerate(doc):
        blocks = page.get_text("blocks")
        for block in blocks:
            if block[6] != 0:  # not a text block
                continue
            text = block[4]
            text_lower = text.lower()
            # Score by number of query words present
            score = sum(1 for w in query_words if w in text_lower)
            if score > 0:
                results.append({
                    "page": page_num,
                    "text": text.strip()[:500],
                    "bbox": list(block[:4]),
                    "relevance_score": score / len(query_words) if query_words else 0,
                })

    doc.close()
    results.sort(key=lambda x: x["relevance_score"], reverse=True)
    return results[:20]


def answer_question(doc_id: str, question: str) -> dict:
    """Answer a question about the document using full text as context."""
    file_path = _get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    all_text = ""
    for page in doc:
        all_text += page.get_text() + "\n"
    doc.close()

    # Find relevant sections
    relevant = find_similar_sections(doc_id, question)

    # Check if question is about tables
    tables = extract_tables(doc_id)
    table_answer = None
    if tables and re.search(r'table|total|sum|row|column|data', question.lower()):
        table_answer = query_table(tables, question)

    # Check for entity-related questions
    entities = None
    q_lower = question.lower()
    if re.search(r'email|phone|date|url|link|name|person|organization|company|amount|money', q_lower):
        entities = extract_entities(doc_id)

    return {
        "question": question,
        "relevant_sections": relevant[:10],
        "table_answer": table_answer,
        "entities": entities,
        "full_text_length": len(all_text),
        "context_snippet": all_text[:5000],
    }


def compare_pages(doc_id: str, page1: int, page2: int) -> dict:
    """Compare content between two pages."""
    file_path = _get_doc_path(doc_id)
    doc = fitz.open(str(file_path))

    if page1 < 0 or page1 >= len(doc) or page2 < 0 or page2 >= len(doc):
        doc.close()
        return {"error": f"Invalid page numbers. Document has {len(doc)} pages."}

    text1 = doc[page1].get_text()
    text2 = doc[page2].get_text()
    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())

    common = words1 & words2
    only_p1 = words1 - words2
    only_p2 = words2 - words1

    similarity = len(common) / max(len(words1 | words2), 1)

    doc.close()
    return {
        "page1": page1,
        "page2": page2,
        "page1_word_count": len(text1.split()),
        "page2_word_count": len(text2.split()),
        "common_words": len(common),
        "unique_to_page1": len(only_p1),
        "unique_to_page2": len(only_p2),
        "similarity": round(similarity, 3),
        "page1_preview": text1[:500],
        "page2_preview": text2[:500],
    }


def detect_language(doc_id: str) -> dict:
    """Detect the document's primary language using character frequency analysis."""
    file_path = _get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    all_text = ""
    for page in doc:
        all_text += page.get_text()
    doc.close()

    if not all_text.strip():
        return {"language": "unknown", "confidence": 0, "reason": "No text found"}

    # Simple heuristic-based language detection using common word patterns
    text_lower = all_text.lower()
    words = re.findall(r'\b[a-z]+\b', text_lower)
    word_set = set(words)

    language_indicators = {
        "english": {"the", "and", "is", "in", "to", "of", "for", "that", "with", "this", "are", "was", "have", "from"},
        "spanish": {"el", "la", "de", "en", "que", "los", "del", "las", "por", "con", "una", "para", "como", "pero"},
        "french": {"le", "la", "de", "et", "les", "des", "en", "une", "est", "que", "dans", "pour", "pas", "avec"},
        "german": {"der", "die", "und", "den", "das", "ist", "ein", "eine", "nicht", "auf", "mit", "sich", "auch", "von"},
        "portuguese": {"de", "que", "em", "para", "com", "uma", "por", "mais", "como", "dos", "das", "foi", "ser", "tem"},
        "italian": {"di", "che", "il", "per", "una", "con", "del", "della", "sono", "anche", "questo", "come", "non", "nel"},
    }

    # Check for non-Latin scripts
    cjk_chars = len(re.findall(r'[\u4e00-\u9fff]', all_text))
    arabic_chars = len(re.findall(r'[\u0600-\u06FF]', all_text))
    cyrillic_chars = len(re.findall(r'[\u0400-\u04FF]', all_text))
    total_chars = len(all_text)

    if cjk_chars > total_chars * 0.1:
        return {"language": "chinese/japanese/korean", "confidence": 0.8, "script": "CJK"}
    if arabic_chars > total_chars * 0.1:
        return {"language": "arabic", "confidence": 0.8, "script": "Arabic"}
    if cyrillic_chars > total_chars * 0.1:
        return {"language": "russian/cyrillic", "confidence": 0.8, "script": "Cyrillic"}

    scores = {}
    for lang, indicators in language_indicators.items():
        matches = word_set & indicators
        scores[lang] = len(matches) / len(indicators)

    if not scores or max(scores.values()) == 0:
        return {"language": "unknown", "confidence": 0, "reason": "Could not determine language"}

    best_lang = max(scores, key=scores.get)
    confidence = round(scores[best_lang], 2)

    return {
        "language": best_lang,
        "confidence": confidence,
        "scores": {k: round(v, 2) for k, v in sorted(scores.items(), key=lambda x: -x[1])},
    }


# ── Form Detection ─────────────────────────────────────────────────────────


def detect_forms(doc_id: str) -> dict:
    """Detect form fields in PDFs: text fields, checkboxes, signatures, etc."""
    file_path = _get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    form_fields = []

    for page_num, page in enumerate(doc):
        # Check for PDF form widgets
        widgets = page.widgets()
        if widgets:
            for widget in widgets:
                field_info = {
                    "page": page_num,
                    "field_name": widget.field_name or "",
                    "field_type": _widget_type_name(widget.field_type),
                    "field_value": widget.field_value or "",
                    "bbox": list(widget.rect),
                    "is_read_only": bool(widget.field_flags & 1),
                }
                if widget.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                    field_info["checked"] = widget.field_value == "Yes"
                elif widget.field_type == fitz.PDF_WIDGET_TYPE_COMBOBOX:
                    field_info["options"] = widget.choice_values or []
                elif widget.field_type == fitz.PDF_WIDGET_TYPE_LISTBOX:
                    field_info["options"] = widget.choice_values or []

                form_fields.append(field_info)

        # Heuristic detection of form-like patterns in non-interactive PDFs
        text = page.get_text()
        # Look for label: _______ patterns (underline fields)
        for m in re.finditer(r'([A-Za-z\s]+):\s*_{3,}', text):
            form_fields.append({
                "page": page_num,
                "field_name": m.group(1).strip(),
                "field_type": "text_underline",
                "field_value": "",
                "detected": "heuristic",
            })
        # Look for [ ] or [x] checkbox patterns
        for m in re.finditer(r'\[([xX\s])\]\s*(.+?)(?:\n|$)', text):
            form_fields.append({
                "page": page_num,
                "field_name": m.group(2).strip(),
                "field_type": "checkbox_text",
                "checked": m.group(1).strip().lower() == "x",
                "detected": "heuristic",
            })

    doc.close()

    # Extract form data as key-value pairs
    form_data = {}
    for field in form_fields:
        name = field.get("field_name", "")
        if name:
            form_data[name] = field.get("field_value", field.get("checked", ""))

    return {
        "has_forms": len(form_fields) > 0,
        "field_count": len(form_fields),
        "fields": form_fields,
        "form_data": form_data,
    }


def fill_form_field(doc_id: str, field_name: str, value: str) -> dict:
    """Fill a form field in the PDF."""
    file_path = _get_doc_path(doc_id)
    doc = fitz.open(str(file_path))
    filled = False

    for page in doc:
        widgets = page.widgets()
        if not widgets:
            continue
        for widget in widgets:
            if widget.field_name == field_name:
                widget.field_value = value
                widget.update()
                filled = True

    if filled:
        import os
        out_path = str(file_path) + ".tmp"
        doc.save(out_path)
        doc.close()
        os.replace(out_path, str(file_path))
        return {"status": "ok", "field": field_name, "value": value}

    doc.close()
    return {"status": "error", "message": f"Field '{field_name}' not found"}


def _widget_type_name(widget_type: int) -> str:
    type_map = {
        fitz.PDF_WIDGET_TYPE_BUTTON: "button",
        fitz.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
        fitz.PDF_WIDGET_TYPE_COMBOBOX: "combobox",
        fitz.PDF_WIDGET_TYPE_LISTBOX: "listbox",
        fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "radiobutton",
        fitz.PDF_WIDGET_TYPE_SIGNATURE: "signature",
        fitz.PDF_WIDGET_TYPE_TEXT: "text",
    }
    return type_map.get(widget_type, "unknown")


# ── Extract Key Info (convenience wrapper) ─────────────────────────────────


def extract_key_info(doc_id: str) -> dict:
    """Extract key entities: people, organizations, dates, monetary amounts, addresses."""
    entities = extract_entities(doc_id)
    return {
        "people": entities.get("people", []),
        "organizations": entities.get("organizations", []),
        "dates": entities.get("dates", []),
        "monetary": entities.get("monetary", []),
        "emails": entities.get("emails", []),
        "phones": entities.get("phones", []),
        "urls": entities.get("urls", []),
    }


# ── Full Auto-Analysis (triggered on upload) ───────────────────────────────


def run_full_analysis(doc_id: str) -> dict:
    """Run the complete document analysis and cache results. Called on upload."""
    analysis = analyze_document(doc_id)

    # Add form detection
    forms = detect_forms(doc_id)
    analysis["forms"] = forms

    # Add language detection
    language = detect_language(doc_id)
    analysis["language"] = language

    # Save the full analysis
    save_analysis(doc_id, analysis)
    return analysis
