"""
AI engine: a Claude agent that can read, cite, see and *edit* the open PDF.

Design
------
* The model is given ~45 tools. Every tool that touches the PDF calls the real
  feature route in-process (``httpx.AsyncClient`` over ``ASGITransport`` against
  ``backend.main.app``), so there is exactly one implementation of each PDF
  operation, and the signed-document guard / validation / locking of those
  routes applies to the agent too.
* Every mutating tool takes ONE labelled undo snapshot (``advanced_ops.snapshot``,
  "AI: <what>") before it runs. The snapshots the underlying routes take are
  coalesced into it, so a batch tool such as ``rewrite_paragraphs`` (used for
  translation) is a single undo step. A tool that turns out not to change the
  file leaves no history entry behind. Redaction is the exception: the redact
  route purges undo history on purpose (snapshots would keep redacted content),
  so the agent never re-snapshots around it.
* A 409 ``signed_document`` from the guard is turned into a "ask the user"
  result plus a ``needs_confirmation`` event; the request is only resent with
  ``X-Allow-Break-Signature: 1`` when the user confirmed in the UI.
* Grounding: the whole document text (page-tagged) goes into a cached system
  block for normal documents; long documents get an outline + retrieved pages
  and must use ``search_document`` / ``read_pages``. Answers cite ``[p. N]`` or
  ``[p. N "quote"]``; citations are resolved to page rectangles for the viewer.
* Vision: ``view_page`` returns the rendered page as an image block, and a
  scanned current page is attached automatically.
* ``run_agent`` is an async generator of plain-dict events (see EVENT TYPES);
  the router turns them into SSE. ``understand_and_execute`` keeps the old
  non-streaming contract used by main.py's /chat.

EVENT TYPES (``type`` field)
  start {run_id, model} | text {delta} | tool_start {id, name, input, label}
  tool_result {id, name, ok, summary, changed}
  redaction_review {items:[{id,page,text,category,reason,rects}], count}
  file {filename, mime, content} | download {format, url, label}
  needs_confirmation {reason, message} | setup_required {reason, message}
  error {message, code} | done {response, changed, changes, citations,
  page_count_changed, new_page_count, undo_steps, stopped}
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import json
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import fitz

logger = logging.getLogger("pdf-editor.ai")

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:  # pragma: no cover - the venv ships the SDK
    HAS_ANTHROPIC = False
    anthropic = None  # type: ignore


# ─── Configuration ───────────────────────────────────────────────────────────

MODELS: list[dict] = [
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "note": "Fast and capable (default)"},
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "note": "Most capable, slower"},
    {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5", "note": "Fastest, simple tasks"},
]
MODEL_IDS = {m["id"] for m in MODELS}
DEFAULT_MODEL = "claude-sonnet-5-5"

MAX_ITERATIONS = int(os.environ.get("AI_MAX_ITERATIONS", "25"))
MAX_TOKENS = int(os.environ.get("AI_MAX_TOKENS", "32000"))
# Documents whose text is under this many characters go into the prompt whole
# (cached); longer ones are chunked by page and retrieved.
FULL_TEXT_CHAR_LIMIT = int(os.environ.get("AI_FULL_TEXT_CHARS", "400000"))
RETRIEVED_PAGES = 8
TOOL_RESULT_CHAR_LIMIT = 40000
OCR_TIMEOUT_S = float(os.environ.get("AI_OCR_TIMEOUT", "240"))

_runtime: dict[str, Optional[str]] = {"api_key": None, "model": None}
_histories: dict[str, list[dict]] = {}
_cancelled: set[str] = set()

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SIGNED_HEADER = "X-Allow-Break-Signature"


def get_api_key() -> str:
    """Runtime key (pasted in the UI) wins over the environment/.env key."""
    return (_runtime.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "") or "").strip()


def set_api_key(key: str, persist: bool = False) -> None:
    """Store the key server-side. It is never logged or returned to a client."""
    key = (key or "").strip()
    _runtime["api_key"] = key or None
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
    else:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    if persist and key:
        _persist_key(key)


def _persist_key(key: str) -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    lines = [ln for ln in lines if not ln.strip().startswith("ANTHROPIC_API_KEY=")]
    lines.append(f"ANTHROPIC_API_KEY={key}")
    env_path.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass


def get_model() -> str:
    m = _runtime.get("model") or os.environ.get("AI_MODEL", "").strip() or DEFAULT_MODEL
    return m


def set_model(model: str) -> None:
    if model not in MODEL_IDS:
        raise ValueError(f"Unknown model {model!r}")
    _runtime["model"] = model


def ai_status() -> dict:
    key = get_api_key()
    return {
        "sdk_installed": HAS_ANTHROPIC,
        "api_key_set": bool(key),
        "key_source": ("runtime" if _runtime.get("api_key") else ("env" if key else None)),
        "ai_available": HAS_ANTHROPIC and bool(key),
        "model": get_model(),
        "models": MODELS,
    }


def _effort_for(model: str) -> Optional[str]:
    if model.startswith("claude-haiku"):
        return None  # effort is not supported on Haiku 4.5
    env = os.environ.get("AI_EFFORT", "").strip()
    if env in ("low", "medium", "high", "xhigh", "max"):
        return env
    return "high" if model.startswith("claude-opus") else "medium"


def _fallbacks_enabled(model: str) -> bool:
    if os.environ.get("AI_FALLBACKS", "1") != "1":
        return False
    return model in ("claude-sonnet-5-5", "claude-opus-5-5")


# ─── Document helpers (read-only) ────────────────────────────────────────────

def _upload_dir() -> Path:
    from backend import advanced_ops
    return Path(advanced_ops.UPLOAD_DIR)


def doc_path(doc_id: str) -> Path:
    if not _UUID_RE.match(doc_id or ""):
        raise FileNotFoundError("Invalid document id")
    p = _upload_dir() / doc_id / "original.pdf"
    if not p.exists():
        raise FileNotFoundError(f"Document {doc_id} not found")
    return p


def page_texts(doc_id: str) -> list[str]:
    with fitz.open(str(doc_path(doc_id))) as doc:
        return [p.get_text("text", sort=True) for p in doc]


def _file_hash(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def render_page_png(doc_id: str, page_index: int, max_px: int = 1568) -> bytes:
    with fitz.open(str(doc_path(doc_id))) as doc:
        if not 0 <= page_index < len(doc):
            raise ValueError(f"Page {page_index + 1} does not exist")
        page = doc[page_index]
        longest = max(page.rect.width, page.rect.height) or 1
        zoom = min(2.0, max_px / longest)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png")


def page_looks_scanned(doc_id: str, page_index: int) -> bool:
    with fitz.open(str(doc_path(doc_id))) as doc:
        if not 0 <= page_index < len(doc):
            return False
        page = doc[page_index]
        text = page.get_text("text").strip()
        return len(text) < 40 and (bool(page.get_images()) or bool(page.get_drawings()))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'-]+")


def retrieve_pages(texts: list[str], query: str, k: int = RETRIEVED_PAGES) -> list[int]:
    """Rank pages by a small TF-IDF score against the query (0-based indices)."""
    import math
    q_terms = [w.lower() for w in _WORD_RE.findall(query or "") if len(w) > 2]
    if not q_terms:
        return list(range(min(k, len(texts))))
    page_terms = [[w.lower() for w in _WORD_RE.findall(t)] for t in texts]
    n = len(texts) or 1
    df = {t: sum(1 for pt in page_terms if t in set(pt)) for t in set(q_terms)}
    scores = []
    for i, pt in enumerate(page_terms):
        if not pt:
            scores.append((0.0, i))
            continue
        counts: dict[str, int] = {}
        for w in pt:
            counts[w] = counts.get(w, 0) + 1
        s = 0.0
        for t in q_terms:
            if counts.get(t):
                s += (1 + math.log(counts[t])) * math.log(1 + n / (1 + df.get(t, 0)))
        scores.append((s, i))
    scores.sort(key=lambda x: (-x[0], x[1]))
    return sorted(i for s, i in scores[:k] if s > 0) or list(range(min(k, len(texts))))


def build_document_block(doc_id: str, texts: list[str], question: str) -> tuple[str, bool]:
    """Return (document context text, is_full_text)."""
    total = sum(len(t) for t in texts)
    if total <= FULL_TEXT_CHAR_LIMIT:
        body = "\n".join(f'<page number="{i + 1}">\n{t.strip()}\n</page>' for i, t in enumerate(texts))
        return (f"<document pages=\"{len(texts)}\" mode=\"full_text\">\n{body}\n</document>", True)
    outline = "\n".join(
        f"  p. {i + 1}: {(t.strip().splitlines() or [''])[0][:100]}" for i, t in enumerate(texts)
    )
    picked = retrieve_pages(texts, question)
    body = "\n".join(f'<page number="{i + 1}">\n{texts[i].strip()}\n</page>' for i in picked)
    return (
        f"<document pages=\"{len(texts)}\" mode=\"retrieved\">\n"
        f"This document is long; only the pages most relevant to the latest question are included "
        f"below. Use search_document and read_pages to read anything else before answering.\n"
        f"<outline>\n{outline}\n</outline>\n{body}\n</document>",
        False,
    )


# ─── Citations ───────────────────────────────────────────────────────────────

CITATION_RE = re.compile(
    r"\[p\.\s*(\d{1,5})(?:\s*[:,]?\s*[\"“]([^\"”\]]{2,300})[\"”])?\s*\]"
)


def parse_citations(text: str) -> list[dict]:
    out = []
    for m in CITATION_RE.finditer(text or ""):
        out.append({"page": int(m.group(1)), "quote": (m.group(2) or "").strip() or None,
                    "start": m.start(), "end": m.end(), "marker": m.group(0)})
    return out


def _search_rects(page: fitz.Page, quote: str) -> list[list[float]]:
    def visible(r: fitz.Rect) -> list[float]:
        vr = r * page.rotation_matrix
        return [round(v, 2) for v in (vr.x0, vr.y0, vr.x1, vr.y1)]

    for candidate in (quote, " ".join(quote.split()[:8]), " ".join(quote.split()[:4])):
        candidate = candidate.strip()
        if len(candidate) < 2:
            continue
        hits = page.search_for(candidate)
        if hits:
            return [visible(r) for r in hits[:8]]
    return []


def locate_quote(doc_id: str, page: int, quote: str) -> dict:
    """1-based page + exact quote -> visible-space rects for the viewer highlight."""
    with fitz.open(str(doc_path(doc_id))) as doc:
        if not 1 <= page <= len(doc):
            return {"page": page, "rects": [], "valid": False}
        return {"page": page, "rects": _search_rects(doc[page - 1], quote) if quote else [], "valid": True}


def resolve_citations(doc_id: str, text: str) -> list[dict]:
    """Citations in `text`, each with 1-based page and highlight rects."""
    cites = parse_citations(text)
    if not cites:
        return []
    try:
        doc = fitz.open(str(doc_path(doc_id)))
    except FileNotFoundError:
        return []
    out, seen = [], set()
    with doc:
        for c in cites:
            key = (c["page"], c["quote"])
            if key in seen:
                continue
            seen.add(key)
            rects: list[list[float]] = []
            valid = 1 <= c["page"] <= len(doc)
            if valid and c["quote"]:
                rects = _search_rects(doc[c["page"] - 1], c["quote"])
            out.append({"page": c["page"], "quote": c["quote"], "rects": rects, "valid": valid,
                        "marker": c["marker"]})
    return out


# ─── In-process route calls ──────────────────────────────────────────────────

def _app():
    mod = sys.modules.get("backend.main")
    if mod is None or not hasattr(mod, "app"):
        import importlib
        mod = importlib.import_module("backend.main")
    return mod.app


class RouteError(Exception):
    def __init__(self, status: int, detail: Any, code: Optional[str] = None):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail, self.code = status, detail, code


@dataclass
class AgentCtx:
    doc_id: str
    http: Any
    allow_break_signature: bool = False
    reference_doc_ids: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    page_count_changed: bool = False
    current_page: int = 0
    _snap: Optional[str] = None  # filename of the active AI snapshot

    async def call(self, method: str, path: str, json_body: Any = None,
                   params: Optional[dict] = None, raw: bool = False) -> Any:
        headers = {SIGNED_HEADER: "1"} if self.allow_break_signature else {}
        resp = await self.http.request(method, path, json=json_body, params=params, headers=headers)
        if self._snap:
            _drop_after(self.doc_id, self._snap)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = {"detail": resp.text[:500]}
            code = body.get("code") if isinstance(body, dict) else None
            detail = body.get("detail", body) if isinstance(body, dict) else body
            raise RouteError(resp.status_code, detail, code)
        if raw:
            return resp
        ctype = resp.headers.get("content-type", "")
        return resp.json() if "json" in ctype else resp.text


def _history_api():
    from backend import advanced_ops
    return advanced_ops


def _drop_after(doc_id: str, keep_filename: str) -> None:
    """Remove history entries newer than our AI snapshot (route snapshots)."""
    ao = _history_api()
    try:
        hist = ao._load_history(doc_id)
    except Exception:
        return
    names = [v.get("filename") for v in hist.get("versions", [])]
    if keep_filename not in names:
        return
    idx = names.index(keep_filename)
    extra = hist["versions"][idx + 1:]
    if not extra:
        return
    hdir = ao._history_dir(doc_id)
    for v in extra:
        fp = hdir / v["filename"]
        if fp.exists():
            fp.unlink()
    hist["versions"] = hist["versions"][: idx + 1]
    hist["current"] = idx
    ao._save_history(doc_id, hist)


def _drop_including(doc_id: str, filename: str) -> None:
    ao = _history_api()
    hist = ao._load_history(doc_id)
    names = [v.get("filename") for v in hist.get("versions", [])]
    if filename not in names:
        return
    idx = names.index(filename)
    _drop_after(doc_id, filename)
    hist = ao._load_history(doc_id)
    v = hist["versions"].pop(idx)
    fp = ao._history_dir(doc_id) / v["filename"]
    if fp.exists():
        fp.unlink()
    hist["current"] = len(hist["versions"]) - 1
    ao._save_history(doc_id, hist)


async def run_mutation(ctx: AgentCtx, label: str, fn: Callable[[], Awaitable[Any]],
                       take_snapshot: bool = True) -> tuple[Any, bool]:
    """Run fn under one labelled undo snapshot. Returns (result, changed)."""
    ao = _history_api()
    path = doc_path(ctx.doc_id)
    before = _file_hash(path)
    if take_snapshot:
        ao.snapshot(ctx.doc_id, f"AI: {label}")
        hist = ao._load_history(ctx.doc_id)
        ctx._snap = hist["versions"][-1]["filename"] if hist.get("versions") else None
    try:
        result = await fn()
    finally:
        snap, ctx._snap = ctx._snap, None
        changed = path.exists() and _file_hash(path) != before
        if snap:
            _drop_after(ctx.doc_id, snap)
            if not changed:
                _drop_including(ctx.doc_id, snap)
    return result, changed


# ─── Tool registry ───────────────────────────────────────────────────────────

@dataclass
class ToolOut:
    content: Any                      # str or list of content blocks
    summary: str
    ok: bool = True
    changed: bool = False
    page_count_changed: bool = False
    code: Optional[str] = None


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict
    handler: Callable[[AgentCtx, dict], Awaitable[ToolOut]]
    mutating: bool = False
    snapshot: bool = True
    label: Optional[Callable[[dict], str]] = None


TOOLS_REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, description: str, properties: dict, required: Optional[list] = None,
         mutating: bool = False, snapshot: bool = True, label=None):
    def deco(fn):
        TOOLS_REGISTRY[name] = ToolSpec(
            name=name, description=description,
            schema={"type": "object", "properties": properties, "required": required or []},
            handler=fn, mutating=mutating, snapshot=snapshot, label=label,
        )
        return fn
    return deco


def tool_definitions() -> list[dict]:
    """Deterministic tool list (stable order keeps the prompt cache warm)."""
    return [{"name": s.name, "description": s.description, "input_schema": s.schema}
            for s in TOOLS_REGISTRY.values()]


def _p0(page: Any) -> int:
    """1-based page (model-facing) -> 0-based (routes)."""
    return int(page) - 1


def _j(obj: Any) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) > TOOL_RESULT_CHAR_LIMIT:
        s = s[:TOOL_RESULT_CHAR_LIMIT] + ' ... [truncated: ask for fewer pages/items]'
    return s


PAGE = {"type": "integer", "description": "1-based page number"}
PAGES = {"type": "array", "items": {"type": "integer"}, "description": "1-based page numbers"}
RECT = {"type": "array", "items": {"type": "number"}, "description": "[x0,y0,x1,y1] in PDF points, top-left origin"}
STYLE = {
    "type": "object",
    "description": "Optional style override; omit fields to keep the original",
    "properties": {
        "family": {"type": "string", "enum": ["original", "sans", "serif", "mono"]},
        "size": {"type": "number"},
        "color": {"type": "string", "description": "#rrggbb"},
        "bold": {"type": "boolean"},
        "italic": {"type": "boolean"},
    },
}
TARGET = {
    "paragraph_id": {"type": "string", "description": "Paragraph id from list_paragraphs/find_paragraph"},
    "match_text": {"type": "string", "description": "Some of the paragraph's current text, used to find it"},
}


# ── Read tools ──

@tool("get_document_info", "Page count, page sizes and metadata of the open document.", {})
async def t_info(ctx: AgentCtx, inp: dict) -> ToolOut:
    info = await ctx.call("GET", f"/api/pdf/{ctx.doc_id}/info")
    return ToolOut(_j(info), f"{info.get('page_count', '?')} pages")


@tool("search_document", "Find every occurrence of words/phrases in the document. Returns page and a snippet "
      "for each hit. Use this for long documents before answering.",
      {"query": {"type": "string"}, "max_results": {"type": "integer"}}, ["query"])
async def t_search(ctx: AgentCtx, inp: dict) -> ToolOut:
    q = _norm(inp["query"])
    limit = int(inp.get("max_results") or 40)
    hits = []
    for i, t in enumerate(page_texts(ctx.doc_id)):
        flat = re.sub(r"\s+", " ", t)
        low = flat.lower()
        start = 0
        while q and len(hits) < limit:
            k = low.find(q, start)
            if k < 0:
                break
            hits.append({"page": i + 1, "snippet": flat[max(0, k - 80): k + len(q) + 80]})
            start = k + len(q)
    if not hits:
        texts = page_texts(ctx.doc_id)
        ranked = retrieve_pages(texts, inp["query"], 5)
        return ToolOut(_j({"exact_hits": [], "related_pages": [i + 1 for i in ranked]}),
                       "no exact hits")
    return ToolOut(_j({"hits": hits, "count": len(hits)}), f"{len(hits)} hits")


@tool("read_pages", "Read the full text of specific pages.", {"pages": PAGES}, ["pages"])
async def t_read(ctx: AgentCtx, inp: dict) -> ToolOut:
    texts = page_texts(ctx.doc_id)
    out = []
    for p in inp["pages"][:30]:
        if 1 <= int(p) <= len(texts):
            out.append(f'<page number="{p}">\n{texts[int(p) - 1].strip()}\n</page>')
        else:
            out.append(f'<page number="{p}" error="does not exist"/>')
    return ToolOut("\n".join(out)[:TOOL_RESULT_CHAR_LIMIT], f"read {len(out)} page(s)")


@tool("view_page", "Look at a rendered image of a page. Use for scanned pages, charts, images, signatures, "
      "layout questions, or to place form fields on a flat form.", {"page": PAGE}, ["page"])
async def t_view(ctx: AgentCtx, inp: dict) -> ToolOut:
    png = await asyncio.to_thread(render_page_png, ctx.doc_id, _p0(inp["page"]))
    with fitz.open(str(doc_path(ctx.doc_id))) as doc:
        r = doc[_p0(inp["page"])].rect
    return ToolOut([
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.standard_b64encode(png).decode()}},
        {"type": "text", "text": f"Page {inp['page']} rendered. Page size {r.width:.0f}x{r.height:.0f} pt; "
                                 f"image pixels map linearly to those points (top-left origin)."},
    ], f"viewed page {inp['page']}")


def _block_view(b: dict, page: int) -> dict:
    return {
        "paragraph_id": b.get("id"), "page": page, "bbox": b.get("bbox"),
        "text": b.get("paragraph_text") or b.get("text"), "style": b.get("style"),
        "align": b.get("align"), "editable": b.get("editable", True),
        **({"reason": b["reason"]} if b.get("reason") else {}),
    }


async def _page_blocks(ctx: AgentCtx, page1: int) -> list[dict]:
    data = await ctx.call("GET", f"/api/pdf/{ctx.doc_id}/text-edit/page/{_p0(page1)}")
    return data.get("blocks", [])


@tool("list_paragraphs", "List the editable text paragraphs on a page with ids, boxes, text and style.",
      {"page": PAGE}, ["page"])
async def t_list_paras(ctx: AgentCtx, inp: dict) -> ToolOut:
    blocks = await _page_blocks(ctx, int(inp["page"]))
    return ToolOut(_j([_block_view(b, int(inp["page"])) for b in blocks]), f"{len(blocks)} paragraphs")


def _text_score(query: str, text: str) -> float:
    q, t = _norm(query), _norm(text)
    if not q or not t:
        return 0.0
    if q in t:
        return 1.0 + min(len(q) / max(len(t), 1), 1.0) * 0.5
    sm = difflib.SequenceMatcher(None, q, t[: max(len(q) * 3, 400)])
    return sm.ratio() if sm.quick_ratio() > 0.3 else 0.0


async def find_paragraphs(ctx: AgentCtx, query: str, page: Optional[int] = None, top: int = 3) -> list[dict]:
    pages = [int(page)] if page else list(range(1, len(page_texts(ctx.doc_id)) + 1))[:300]
    scored = []
    for p in pages:
        try:
            blocks = await _page_blocks(ctx, p)
        except RouteError:
            continue
        for b in blocks:
            s = _text_score(query, b.get("paragraph_text") or b.get("text") or "")
            if s > 0.45:
                scored.append((s, _block_view(b, p)))
    scored.sort(key=lambda x: -x[0])
    return [dict(v, score=round(s, 3)) for s, v in scored[:top]]


@tool("find_paragraph", "Find the paragraph(s) whose text best matches the given content (exact or fuzzy). "
      "Returns page, paragraph_id, bbox and current text. Use before editing in place.",
      {"text": {"type": "string"}, "page": PAGE}, ["text"])
async def t_find_para(ctx: AgentCtx, inp: dict) -> ToolOut:
    found = await find_paragraphs(ctx, inp["text"], inp.get("page"))
    return ToolOut(_j(found), f"{len(found)} match(es)")


@tool("list_objects", "List images and vector drawings on a page (for moving or deleting them).",
      {"page": PAGE}, ["page"])
async def t_objects(ctx: AgentCtx, inp: dict) -> ToolOut:
    data = await ctx.call("GET", f"/api/pdf/{ctx.doc_id}/objects/{_p0(inp['page'])}")
    return ToolOut(_j(data), "listed objects")


@tool("list_form_fields", "List the interactive form fields (name, type, value, options, page, rect).", {})
async def t_fields(ctx: AgentCtx, inp: dict) -> ToolOut:
    data = await ctx.call("GET", f"/api/pdf/{ctx.doc_id}/form-fields")
    fields = data.get("fields", data) if isinstance(data, dict) else data
    return ToolOut(_j(data), f"{len(fields) if isinstance(fields, list) else '?'} fields")


@tool("list_bookmarks", "List the document bookmarks (outline).", {})
async def t_bookmarks(ctx: AgentCtx, inp: dict) -> ToolOut:
    return ToolOut(_j(await ctx.call("GET", f"/api/pdf/{ctx.doc_id}/organize/bookmarks")), "listed bookmarks")


@tool("list_comments", "List comments/annotations with their replies and status.", {})
async def t_comments(ctx: AgentCtx, inp: dict) -> ToolOut:
    return ToolOut(_j(await ctx.call("GET", f"/api/pdf/{ctx.doc_id}/organize/comments")), "listed comments")


@tool("extract_tables", "Extract tables (rows/cells) from the document, optionally from one page.",
      {"page": PAGE})
async def t_tables(ctx: AgentCtx, inp: dict) -> ToolOut:
    params = {"page": _p0(inp["page"])} if inp.get("page") else None
    data = await ctx.call("GET", f"/api/pdf/{ctx.doc_id}/tables", params=params)
    return ToolOut(_j(data), f"{data.get('count', 0) if isinstance(data, dict) else '?'} table(s)")


@tool("security_audit", "Audit the document for hidden text, metadata, JavaScript, embedded files, etc.", {})
async def t_audit(ctx: AgentCtx, inp: dict) -> ToolOut:
    return ToolOut(_j(await ctx.call("GET", f"/api/pdf/{ctx.doc_id}/security/audit")), "audited")


@tool("get_edit_history", "List the undo history of the document.", {})
async def t_history(ctx: AgentCtx, inp: dict) -> ToolOut:
    return ToolOut(_j(await ctx.call("GET", f"/api/pdf/{ctx.doc_id}/history")), "history")


def _check_ref(ctx: AgentCtx, doc_id: str) -> None:
    if doc_id not in ctx.reference_doc_ids:
        raise ValueError("Only reference documents attached by the user in this request can be read")


@tool("read_reference_document", "Read the text of a reference PDF the user attached (e.g. source data for "
      "form filling, or the other version for comparison).",
      {"doc_id": {"type": "string"}, "pages": PAGES}, ["doc_id"])
async def t_read_ref(ctx: AgentCtx, inp: dict) -> ToolOut:
    _check_ref(ctx, inp["doc_id"])
    texts = page_texts(inp["doc_id"])
    pages = inp.get("pages") or list(range(1, len(texts) + 1))
    body = "\n".join(f'<page number="{p}">\n{texts[p - 1].strip()}\n</page>'
                     for p in pages if 1 <= p <= len(texts))
    return ToolOut(body[:TOOL_RESULT_CHAR_LIMIT], f"read reference ({len(texts)} pages)")


@tool("compare_with_reference", "Diff the open document against an attached reference PDF, page by page.",
      {"doc_id": {"type": "string"}}, ["doc_id"])
async def t_compare(ctx: AgentCtx, inp: dict) -> ToolOut:
    _check_ref(ctx, inp["doc_id"])
    data = await ctx.call("POST", "/api/pdf/compare", {"doc_id_1": inp["doc_id"], "doc_id_2": ctx.doc_id})
    return ToolOut(_j(data), "compared documents")


EXPORT_FORMATS = ["docx", "txt", "md", "html", "png", "jpg", "xlsx", "csv"]


@tool("export_document", "Offer the user a download of the document converted to another format.",
      {"format": {"type": "string", "enum": EXPORT_FORMATS}}, ["format"])
async def t_export(ctx: AgentCtx, inp: dict) -> ToolOut:
    fmt = inp["format"]
    url = f"/api/pdf/{ctx.doc_id}/export/{fmt}"
    resp = await ctx.call("GET", url, raw=True)  # validates that the conversion works
    ctx.events.append({"type": "download", "format": fmt, "url": url,
                       "label": f"Download as {fmt.upper()}", "bytes": len(resp.content)})
    return ToolOut(f"A {fmt.upper()} download link is now shown to the user.", f"prepared {fmt} export")


@tool("create_download", "Give the user a file you produced (e.g. extracted table data as CSV or JSON).",
      {"filename": {"type": "string"}, "format": {"type": "string", "enum": ["csv", "json", "txt", "md"]},
       "content": {"type": "string"}}, ["filename", "format", "content"])
async def t_download(ctx: AgentCtx, inp: dict) -> ToolOut:
    mime = {"csv": "text/csv", "json": "application/json", "txt": "text/plain", "md": "text/markdown"}[inp["format"]]
    if inp["format"] == "json":
        try:
            json.loads(inp["content"])
        except ValueError as e:
            return ToolOut(f"content is not valid JSON: {e}", "invalid JSON", ok=False)
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", inp["filename"])[:80] or "data"
    if not name.lower().endswith("." + inp["format"]):
        name += "." + inp["format"]
    ctx.events.append({"type": "file", "filename": name, "mime": mime, "content": inp["content"]})
    return ToolOut(f"The file {name} is now offered to the user for download.", f"created {name}")


@tool("find_redaction_candidates", "Find text to redact with built-in detectors (presets) and/or a text or "
      "regex query. Read-only: returns matches with ids and rects.",
      {"presets": {"type": "array", "items": {"type": "string", "enum": [
          "ssn", "phone", "email", "credit_card", "date", "money", "address"]}},
       "query": {"type": "string"}, "regex": {"type": "boolean"}, "pages": PAGES})
async def t_redact_find(ctx: AgentCtx, inp: dict) -> ToolOut:
    body = {"presets": inp.get("presets") or [], "query": inp.get("query"),
            "mode": "regex" if inp.get("regex") else "text"}
    if inp.get("pages"):
        body["pages"] = [_p0(p) for p in inp["pages"]]
    data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/redact/search", body)
    for m in data.get("matches", []):
        m["page"] = m["page"] + 1
    return ToolOut(_j(data), f"{data.get('count', 0)} candidate(s)")


@tool("propose_redactions", "Smart redaction step 1: propose sensitive items YOU identified (names, account "
      "numbers, addresses, medical or confidential details, anything beyond the regex presets) plus optional "
      "presets. Each item is located in the PDF and shown to the user as a review list. Nothing is redacted "
      "until the user approves in the review list.",
      {"items": {"type": "array", "items": {"type": "object", "properties": {
          "page": PAGE, "text": {"type": "string", "description": "exact text as it appears"},
          "category": {"type": "string"}, "reason": {"type": "string"}}, "required": ["page", "text"]}},
       "presets": {"type": "array", "items": {"type": "string"}}},
      ["items"])
async def t_propose(ctx: AgentCtx, inp: dict) -> ToolOut:
    review, missing = [], []
    for it in inp.get("items", [])[:300]:
        try:
            data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/redact/search",
                                  {"query": it["text"], "mode": "text", "pages": [_p0(it["page"])]})
        except RouteError:
            data = {"matches": []}
        ms = data.get("matches", [])
        if not ms:  # the model may have the page wrong: search everywhere
            try:
                data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/redact/search",
                                      {"query": it["text"], "mode": "text"})
                ms = data.get("matches", [])
            except RouteError:
                ms = []
        if not ms:
            missing.append(it["text"])
        for m in ms:
            review.append({"id": m["id"], "page": m["page"] + 1, "page_index": m["page"], "text": m["text"],
                           "rects": m["rects"],
                           "category": it.get("category") or "sensitive", "reason": it.get("reason") or "",
                           "context": m.get("context", "")})
    if inp.get("presets"):
        try:
            data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/redact/search",
                                  {"presets": inp["presets"], "mode": "text"})
            for m in data.get("matches", []):
                review.append({"id": m["id"], "page": m["page"] + 1, "page_index": m["page"], "text": m["text"],
                               "rects": m["rects"], "category": m.get("kind", "pattern"), "reason": f"Matches {m.get('kind')} pattern",
                               "context": m.get("context", "")})
        except RouteError as e:
            missing.append(f"presets failed: {e.detail}")
    dedup = {}
    for r in review:
        dedup.setdefault(r["id"], r)
    items = list(dedup.values())
    ctx.events.append({"type": "redaction_review", "items": items, "count": len(items)})
    return ToolOut(_j({"proposed": len(items), "not_found": missing,
                       "note": "Shown to the user for review. Do NOT call apply_redactions unless the user "
                               "explicitly asks you to apply without review."}),
                   f"proposed {len(items)} redaction(s)")


# ── Mutating tools ──

def _style_body(style: Optional[dict]) -> Optional[dict]:
    if not style:
        return None
    return {k: v for k, v in style.items() if k in ("family", "size", "color", "bold", "italic") and v is not None}


def _iou(a: list, b: list) -> float:
    if not a or not b:
        return 0.0
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def locate_block(blocks: list[dict], paragraph_id: Optional[str] = None, bbox: Optional[list] = None,
                 match_text: Optional[str] = None) -> Optional[dict]:
    if paragraph_id:
        for b in blocks:
            if b.get("id") == paragraph_id and (not bbox or _iou(b.get("bbox"), bbox) > 0.5):
                return b
    if bbox:
        best = max(blocks, key=lambda b: _iou(b.get("bbox"), bbox), default=None)
        if best is not None and _iou(best.get("bbox"), bbox) > 0.3:
            if not match_text or _text_score(match_text, best.get("paragraph_text") or best.get("text") or "") > 0.3:
                return best
    if match_text:
        scored = [(_text_score(match_text, b.get("paragraph_text") or b.get("text") or ""), b) for b in blocks]
        scored = [x for x in scored if x[0] > 0.45]
        if scored:
            return max(scored, key=lambda x: x[0])[1]
    return None


async def _resolve_target(ctx: AgentCtx, page: int, inp: dict) -> dict:
    blocks = await _page_blocks(ctx, page)
    b = locate_block(blocks, inp.get("paragraph_id"), inp.get("bbox"), inp.get("match_text"))
    if b is None:
        raise ValueError(f"No paragraph on page {page} matches; call list_paragraphs or find_paragraph first")
    if b.get("editable") is False:
        raise ValueError(f"That paragraph cannot be edited in place: {b.get('reason', 'unsupported')}")
    return b


async def _edit_one(ctx: AgentCtx, page: int, ed: dict) -> dict:
    b = await _resolve_target(ctx, page, ed)
    body = {"page": _p0(page), "target": {"kind": "block", "id": b["id"], "bbox": b["bbox"]}}
    if ed.get("new_text") is not None:
        body["text"] = ed["new_text"]
    st = _style_body(ed.get("style"))
    if st:
        body["style"] = st
    if ed.get("align"):
        body["align"] = ed["align"]
    await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/text-edit/edit", body)
    return {"page": page, "before": (b.get("paragraph_text") or b.get("text") or "")[:300],
            "after": (ed.get("new_text") or "")[:300]}


EDIT_PROPS = {"page": PAGE, **TARGET, "new_text": {"type": "string", "description": "Full replacement text"},
              "style": STYLE, "align": {"type": "string", "enum": ["left", "center", "right", "justify"]}}


@tool("edit_paragraph", "Rewrite and/or restyle ONE paragraph in place, keeping its font, size, colour and "
      "position (true in-place PDF text editing). Identify it by paragraph_id or match_text.",
      EDIT_PROPS, ["page"], mutating=True, label=lambda i: f"edit paragraph on p. {i.get('page')}")
async def t_edit(ctx: AgentCtx, inp: dict) -> ToolOut:
    r = await _edit_one(ctx, int(inp["page"]), inp)
    return ToolOut(_j({"edited": r}), f"edited a paragraph on page {inp['page']}")


@tool("rewrite_paragraphs", "Edit MANY paragraphs in place as ONE undo step (translation, tone changes, "
      "grammar fixes across pages). Each edit identifies a paragraph by paragraph_id (from list_paragraphs) "
      "and/or match_text. Layout, fonts and positions are preserved.",
      {"label": {"type": "string", "description": "Short description for the undo history"},
       "edits": {"type": "array", "items": {"type": "object", "properties": EDIT_PROPS, "required": ["page"]}}},
      ["edits"], mutating=True, label=lambda i: i.get("label") or f"rewrite {len(i.get('edits', []))} paragraphs")
async def t_rewrite_many(ctx: AgentCtx, inp: dict) -> ToolOut:
    # Resolve every target against the ORIGINAL layout first (ids may shift once a
    # paragraph is re-laid-out), then re-locate each one by bbox + text at apply time.
    resolved = []
    cache: dict[int, list[dict]] = {}
    for ed in inp.get("edits", [])[:400]:
        p = int(ed["page"])
        if p not in cache:
            cache[p] = await _page_blocks(ctx, p)
        b = locate_block(cache[p], ed.get("paragraph_id"), ed.get("bbox"), ed.get("match_text"))
        resolved.append((p, ed, b))
    done, failed = [], []
    for p, ed, b in resolved:
        if b is None:
            failed.append({"page": p, "target": ed.get("paragraph_id") or ed.get("match_text"), "error": "not found"})
            continue
        ed2 = dict(ed, paragraph_id=b.get("id"), bbox=b.get("bbox"),
                   match_text=ed.get("match_text") or (b.get("paragraph_text") or b.get("text") or "")[:200])
        try:
            done.append(await _edit_one(ctx, p, ed2))
        except (RouteError, ValueError) as e:
            failed.append({"page": p, "target": ed.get("paragraph_id"), "error": str(getattr(e, "detail", e))})
    return ToolOut(_j({"edited": len(done), "failed": failed}),
                   f"rewrote {len(done)} paragraph(s)" + (f", {len(failed)} failed" if failed else ""),
                   ok=bool(done) or not failed)


@tool("delete_paragraph", "Delete a paragraph of text in place.", {"page": PAGE, **TARGET}, ["page"],
      mutating=True, label=lambda i: f"delete paragraph on p. {i.get('page')}")
async def t_delete_para(ctx: AgentCtx, inp: dict) -> ToolOut:
    b = await _resolve_target(ctx, int(inp["page"]), inp)
    await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/text-edit/delete",
                   {"page": _p0(inp["page"]), "target": {"kind": "block", "id": b["id"], "bbox": b["bbox"]}})
    return ToolOut(_j({"deleted": (b.get("paragraph_text") or "")[:200]}), f"deleted a paragraph on page {inp['page']}")


@tool("move_paragraph", "Move a paragraph by dx/dy points (positive dy = down).",
      {"page": PAGE, **TARGET, "dx": {"type": "number"}, "dy": {"type": "number"}}, ["page"],
      mutating=True, label=lambda i: f"move paragraph on p. {i.get('page')}")
async def t_move_para(ctx: AgentCtx, inp: dict) -> ToolOut:
    b = await _resolve_target(ctx, int(inp["page"]), inp)
    await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/text-edit/move",
                   {"page": _p0(inp["page"]), "target": {"kind": "block", "id": b["id"], "bbox": b["bbox"]},
                    "dx": inp.get("dx", 0), "dy": inp.get("dy", 0)})
    return ToolOut("moved", f"moved a paragraph on page {inp['page']}")


@tool("replace_text", "Find and replace a word or phrase everywhere (or on one page), preserving style.",
      {"find": {"type": "string"}, "replace": {"type": "string"}, "page": PAGE,
       "match_case": {"type": "boolean"}}, ["find", "replace"],
      mutating=True, label=lambda i: f'replace "{i.get("find")}"')
async def t_replace(ctx: AgentCtx, inp: dict) -> ToolOut:
    body = {"find_text": inp["find"], "replace_text": inp["replace"], "match_case": bool(inp.get("match_case"))}
    if inp.get("page"):
        body["page"] = _p0(inp["page"])
    data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/replace", body)
    return ToolOut(_j(data), f'replaced "{inp["find"]}" -> "{inp["replace"]}"')


@tool("add_text", "Add new text at a position on a page.",
      {"page": PAGE, "x": {"type": "number"}, "y": {"type": "number"}, "text": {"type": "string"},
       "font_size": {"type": "number"}, "color": {"type": "array", "items": {"type": "number"},
                                                 "description": "[r,g,b] 0-1"}},
      ["page", "x", "y", "text"], mutating=True, label=lambda i: f"add text on p. {i.get('page')}")
async def t_add_text(ctx: AgentCtx, inp: dict) -> ToolOut:
    body = {"page": _p0(inp["page"]), "x": inp["x"], "y": inp["y"], "text": inp["text"],
            "font_size": inp.get("font_size", 12)}
    if inp.get("color"):
        body["color"] = inp["color"]
    await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/text/add", body)
    return ToolOut("added", f"added text on page {inp['page']}")


OBJ_PROPS = {"page": PAGE, "kind": {"type": "string", "enum": ["image", "drawing"]},
             "xref": {"type": "integer", "description": "image xref (from list_objects)"},
             "occurrence": {"type": "integer"}, "index": {"type": "integer", "description": "drawing index"},
             "bbox": RECT}


@tool("move_object", "Move/resize an image or drawing to new_bbox (from list_objects ids).",
      {**OBJ_PROPS, "new_bbox": RECT}, ["page", "kind", "new_bbox"],
      mutating=True, label=lambda i: f"move {i.get('kind')} on p. {i.get('page')}")
async def t_move_obj(ctx: AgentCtx, inp: dict) -> ToolOut:
    if inp["kind"] == "image":
        body = {"page": _p0(inp["page"]), "xref": inp["xref"], "occurrence": inp.get("occurrence", 0),
                "bbox": inp.get("bbox"), "new_bbox": inp["new_bbox"]}
        await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/objects/image/move", body)
    else:
        body = {"page": _p0(inp["page"]), "index": inp["index"], "bbox": inp.get("bbox"), "new_bbox": inp["new_bbox"]}
        await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/objects/drawing/move", body)
    return ToolOut("moved", f"moved {inp['kind']} on page {inp['page']}")


@tool("delete_object", "Delete an image or drawing (from list_objects ids).", OBJ_PROPS, ["page", "kind"],
      mutating=True, label=lambda i: f"delete {i.get('kind')} on p. {i.get('page')}")
async def t_del_obj(ctx: AgentCtx, inp: dict) -> ToolOut:
    if inp["kind"] == "image":
        await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/objects/image/delete",
                       {"page": _p0(inp["page"]), "xref": inp["xref"], "occurrence": inp.get("occurrence", 0),
                        "bbox": inp.get("bbox")})
    else:
        await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/objects/drawing/delete",
                       {"page": _p0(inp["page"]), "index": inp["index"], "bbox": inp.get("bbox")})
    return ToolOut("deleted", f"deleted {inp['kind']} on page {inp['page']}")


@tool("fill_form", "Fill form fields by field name. Checkboxes take true/false; radio groups and choice "
      "fields take an option/export value.",
      {"values": {"type": "object", "description": "{field_name: value}"}}, ["values"],
      mutating=True, label=lambda i: f"fill {len(i.get('values') or {})} form field(s)")
async def t_fill(ctx: AgentCtx, inp: dict) -> ToolOut:
    data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/form-fields/fill", {"values": inp["values"]})
    return ToolOut(_j(data), f"filled {len(data.get('filled', []))} field(s)"
                   + (f", {len(data.get('errors') or [])} error(s)" if data.get("errors") else ""))


@tool("create_form_field", "Create an interactive form field on a page.",
      {"page": PAGE, "type": {"type": "string", "enum": ["text", "checkbox", "radio", "combobox", "listbox",
                                                         "signature", "button"]},
       "rect": RECT, "name": {"type": "string"}, "value": {"type": "string"},
       "options": {"type": "array", "items": {"type": "string"}}, "multiline": {"type": "boolean"},
       "required": {"type": "boolean"}, "tooltip": {"type": "string"}},
      ["page", "type", "rect"], mutating=True, label=lambda i: f"add {i.get('type')} field")
async def t_create_field(ctx: AgentCtx, inp: dict) -> ToolOut:
    body = {k: v for k, v in inp.items() if v is not None}
    body["page"] = _p0(inp["page"])
    data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/form-fields", body)
    return ToolOut(_j(data), f"created {inp['type']} field {(data.get('field') or {}).get('name', '')}")


@tool("detect_form_fields", "Turn a flat (non-interactive) form into a fillable one: detects blanks, boxes "
      "and checkboxes and creates fields.",
      {"pages": PAGES, "types": {"type": "array", "items": {"type": "string", "enum": ["text", "checkbox"]}}},
      mutating=True, label=lambda i: "auto-create form fields")
async def t_detect(ctx: AgentCtx, inp: dict) -> ToolOut:
    body: dict = {}
    if inp.get("pages"):
        body["pages"] = [_p0(p) for p in inp["pages"]]
    if inp.get("types"):
        body["types"] = inp["types"]
    data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/form-fields/detect", body)
    n = len(data.get("created", [])) if isinstance(data, dict) else 0
    return ToolOut(_j(data), f"created {n} field(s)")


@tool("apply_redactions", "IRREVERSIBLY redact (remove) content. Only when the user explicitly asked to "
      "apply without review. Give areas (page + rect from find_redaction_candidates) and/or presets/query to "
      "redact every match. This also clears the undo history (by design).",
      {"areas": {"type": "array", "items": {"type": "object", "properties": {"page": PAGE, "rect": RECT},
                                             "required": ["page", "rect"]}},
       "presets": {"type": "array", "items": {"type": "string"}}, "query": {"type": "string"},
       "overlay_text": {"type": "string"}},
      mutating=True, snapshot=False, label=lambda i: "apply redactions")
async def t_apply_redact(ctx: AgentCtx, inp: dict) -> ToolOut:
    areas = [{"page": _p0(a["page"]), "rect": a["rect"]} for a in inp.get("areas") or []]
    if inp.get("presets") or inp.get("query"):
        data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/redact/search",
                              {"presets": inp.get("presets") or [], "query": inp.get("query"), "mode": "text"})
        for m in data.get("matches", []):
            areas += [{"page": m["page"], "rect": r} for r in m["rects"]]
    if not areas:
        return ToolOut("Nothing matched; nothing was redacted.", "nothing to redact", ok=False)
    body = {"areas": areas}
    if inp.get("overlay_text"):
        body["overlay_text"] = inp["overlay_text"]
    data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/redact/apply", body)
    return ToolOut(_j(data), f"redacted {len(areas)} area(s)")


@tool("run_ocr", "Run OCR so scanned pages get selectable/searchable text.",
      {"pages": PAGES, "language": {"type": "string", "description": "tesseract code, e.g. eng, deu, fra"},
       "mode": {"type": "string", "enum": ["searchable", "editable"]}},
      mutating=True, label=lambda i: "OCR")
async def t_ocr(ctx: AgentCtx, inp: dict) -> ToolOut:
    body: dict = {"language": inp.get("language") or "eng", "mode": inp.get("mode") or "searchable"}
    if inp.get("pages"):
        body["pages"] = [_p0(p) for p in inp["pages"]]
    job = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/ocr", body)
    job_id = job.get("job_id")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + OCR_TIMEOUT_S
    status: dict = {}
    while loop.time() < deadline:
        status = await ctx.call("GET", f"/api/pdf/ocr/jobs/{job_id}")
        if status.get("status") in ("done", "error"):
            break
        await asyncio.sleep(0.5)
    if status.get("status") == "error":
        return ToolOut(f"OCR failed: {status.get('error')}", "OCR failed", ok=False)
    if status.get("status") != "done":
        return ToolOut("OCR is still running in the background.", "OCR still running", ok=False)
    return ToolOut(_j(status.get("result")), "OCR complete")


@tool("compress_document", "Reduce file size.", {"preset": {"type": "string", "enum": ["high", "balanced", "smallest"]}},
      mutating=True, label=lambda i: f"compress ({i.get('preset', 'balanced')})")
async def t_compress(ctx: AgentCtx, inp: dict) -> ToolOut:
    data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/compress", {"preset": inp.get("preset") or "balanced"})
    return ToolOut(_j(data), "compressed")


@tool("rotate_pages", "Rotate pages by 90/180/270 degrees clockwise.",
      {"pages": PAGES, "angle": {"type": "integer", "enum": [90, 180, 270]}}, ["pages"],
      mutating=True, label=lambda i: f"rotate {len(i.get('pages', []))} page(s)")
async def t_rotate(ctx: AgentCtx, inp: dict) -> ToolOut:
    await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/organize/rotate",
                   {"pages": [_p0(p) for p in inp["pages"]], "angle": inp.get("angle", 90), "relative": True})
    return ToolOut("rotated", f"rotated page(s) {', '.join(map(str, inp['pages']))}")


@tool("delete_pages", "Delete pages.", {"pages": PAGES}, ["pages"], mutating=True,
      label=lambda i: f"delete page(s) {', '.join(map(str, i.get('pages', [])))}")
async def t_del_pages(ctx: AgentCtx, inp: dict) -> ToolOut:
    await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/organize/delete", {"pages": [_p0(p) for p in inp["pages"]]})
    return ToolOut("deleted", f"deleted page(s) {', '.join(map(str, inp['pages']))}", page_count_changed=True)


@tool("insert_blank_pages", "Insert blank pages so the first new page becomes page `position` (1-based; "
      "page_count+1 appends).", {"position": PAGE, "count": {"type": "integer"},
                                 "size": {"type": "string", "enum": ["neighbor", "letter", "legal", "a4", "a3", "a5", "tabloid"]}},
      ["position"], mutating=True, label=lambda i: "insert blank page(s)")
async def t_insert_blank(ctx: AgentCtx, inp: dict) -> ToolOut:
    await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/organize/insert-blank",
                   {"position": _p0(inp["position"]), "count": inp.get("count", 1), "size": inp.get("size", "neighbor")})
    return ToolOut("inserted", f"inserted {inp.get('count', 1)} blank page(s)", page_count_changed=True)


@tool("duplicate_pages", "Duplicate pages (copies go right after the originals).", {"pages": PAGES}, ["pages"],
      mutating=True, label=lambda i: "duplicate page(s)")
async def t_dup(ctx: AgentCtx, inp: dict) -> ToolOut:
    await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/organize/duplicate", {"pages": [_p0(p) for p in inp["pages"]]})
    return ToolOut("duplicated", "duplicated page(s)", page_count_changed=True)


@tool("reorder_pages", "Reorder pages. `order` is the full new order as 1-based page numbers.",
      {"order": PAGES}, ["order"], mutating=True, label=lambda i: "reorder pages")
async def t_reorder(ctx: AgentCtx, inp: dict) -> ToolOut:
    await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/reorder", {"page_order": [_p0(p) for p in inp["order"]]})
    return ToolOut("reordered", "reordered pages")


POSITIONS = ["top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right"]


@tool("add_page_numbers", "Stamp page numbers. format may use {n} and {total}.",
      {"format": {"type": "string"}, "position": {"type": "string", "enum": POSITIONS},
       "start_number": {"type": "integer"}, "skip_first": {"type": "boolean"}, "font_size": {"type": "number"},
       "pages": PAGES}, mutating=True, label=lambda i: "add page numbers")
async def t_page_numbers(ctx: AgentCtx, inp: dict) -> ToolOut:
    body = {k: v for k, v in inp.items() if k != "pages" and v is not None}
    if inp.get("pages"):
        body["pages"] = [_p0(p) for p in inp["pages"]]
    data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/organize/page-numbers", body)
    return ToolOut(_j(data), "added page numbers")


@tool("add_header_footer", "Add headers/footers. Text may use {n}, {total}, {date}.",
      {k: {"type": "string"} for k in ("header_left", "header_center", "header_right",
                                       "footer_left", "footer_center", "footer_right")} |
      {"font_size": {"type": "number"}, "skip_first": {"type": "boolean"}, "pages": PAGES},
      mutating=True, label=lambda i: "add header/footer")
async def t_header_footer(ctx: AgentCtx, inp: dict) -> ToolOut:
    body = {k: v for k, v in inp.items() if k != "pages" and v is not None}
    if inp.get("pages"):
        body["pages"] = [_p0(p) for p in inp["pages"]]
    data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/organize/header-footer", body)
    return ToolOut(_j(data), "added header/footer")


@tool("add_bates_numbers", "Add Bates numbering.",
      {"prefix": {"type": "string"}, "suffix": {"type": "string"}, "digits": {"type": "integer"},
       "start": {"type": "integer"}, "position": {"type": "string", "enum": POSITIONS}},
      mutating=True, label=lambda i: "add Bates numbers")
async def t_bates(ctx: AgentCtx, inp: dict) -> ToolOut:
    data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/organize/bates", {k: v for k, v in inp.items() if v is not None})
    return ToolOut(_j(data), "added Bates numbers")


@tool("add_bookmark", "Add a bookmark pointing at a page.",
      {"title": {"type": "string"}, "page": PAGE, "level": {"type": "integer"}}, ["title", "page"],
      mutating=True, label=lambda i: f'add bookmark "{i.get("title")}"')
async def t_add_bm(ctx: AgentCtx, inp: dict) -> ToolOut:
    data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/organize/bookmarks",
                          {"title": inp["title"], "page": _p0(inp["page"]), "level": inp.get("level", 1)})
    return ToolOut(_j(data), f'added bookmark "{inp["title"]}"')


@tool("delete_bookmark", "Delete the bookmark at `index` (from list_bookmarks).", {"index": {"type": "integer"}},
      ["index"], mutating=True, label=lambda i: "delete bookmark")
async def t_del_bm(ctx: AgentCtx, inp: dict) -> ToolOut:
    await ctx.call("DELETE", f"/api/pdf/{ctx.doc_id}/organize/bookmarks/{int(inp['index'])}")
    return ToolOut("deleted", "deleted bookmark")


@tool("add_comment", "Add a comment or markup: note, highlight/underline/strikeout (give `search` text to "
      "mark), freetext, rect, stamp...",
      {"page": PAGE, "type": {"type": "string", "enum": ["note", "highlight", "underline", "strikeout", "squiggly",
                                                         "freetext", "rect", "ellipse", "stamp"]},
       "text": {"type": "string", "description": "comment body"}, "search": {"type": "string"},
       "rect": RECT, "color": {"type": "array", "items": {"type": "number"}}, "stamp": {"type": "string"}},
      ["page", "type"], mutating=True, label=lambda i: f"add {i.get('type')} on p. {i.get('page')}")
async def t_comment(ctx: AgentCtx, inp: dict) -> ToolOut:
    body = {k: v for k, v in inp.items() if v is not None}
    body["page"] = _p0(inp["page"])
    body.setdefault("author", "AI Assistant")
    data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/organize/comments", body)
    return ToolOut(_j(data), f"added {inp['type']} on page {inp['page']}")


@tool("add_watermark", "Add a text watermark.",
      {"text": {"type": "string"}, "opacity": {"type": "number"}, "font_size": {"type": "number"},
       "rotation": {"type": "number"}, "pages": PAGES}, ["text"],
      mutating=True, label=lambda i: f'watermark "{i.get("text")}"')
async def t_watermark(ctx: AgentCtx, inp: dict) -> ToolOut:
    body = {k: v for k, v in inp.items() if k != "pages" and v is not None}
    body["pages"] = [_p0(p) for p in inp["pages"]] if inp.get("pages") else "all"
    data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/watermark", body)
    return ToolOut(_j(data), f'added watermark "{inp["text"]}"')


@tool("protect_document", "Restrict permissions with an owner password (applied to the working copy). "
      "Only use passwords the user typed in this conversation.",
      {"owner_password": {"type": "string"}, "permissions": {"type": "array", "items": {"type": "string",
       "enum": ["print", "copy", "modify", "annotate", "fill_forms"]}}}, ["owner_password"],
      mutating=True, label=lambda i: "protect document")
async def t_protect(ctx: AgentCtx, inp: dict) -> ToolOut:
    body = {"owner_password": inp["owner_password"], "apply_to_document": True}
    if inp.get("permissions") is not None:
        body["permissions"] = inp["permissions"]
    await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/security/protect", body)
    return ToolOut("protected with AES-256", "protected document")


@tool("sanitize_document", "Remove hidden information: metadata, JavaScript, embedded files, hidden text...",
      {k: {"type": "boolean"} for k in ("metadata", "javascript", "embedded_files", "hidden_text", "links",
                                        "annotations", "form_data")},
      mutating=True, label=lambda i: "sanitize document")
async def t_sanitize(ctx: AgentCtx, inp: dict) -> ToolOut:
    data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/security/sanitize", {k: v for k, v in inp.items()})
    return ToolOut(_j(data), "sanitized document")


@tool("undo", "Undo the last change(s) to the document.", {"steps": {"type": "integer"}})
async def t_undo(ctx: AgentCtx, inp: dict) -> ToolOut:
    n, out = max(1, int(inp.get("steps") or 1)), []
    for _ in range(n):
        try:
            out.append((await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/undo")).get("undone_operation"))
        except RouteError as e:
            out.append(f"stopped: {e.detail}")
            break
    return ToolOut(_j({"undone": out}), f"undid {len([o for o in out if o and not str(o).startswith('stopped')])} step(s)",
                   changed=True, page_count_changed=True)


@tool("redo", "Redo the last undone change.", {})
async def t_redo(ctx: AgentCtx, inp: dict) -> ToolOut:
    data = await ctx.call("POST", f"/api/pdf/{ctx.doc_id}/redo")
    return ToolOut(_j(data), "redid 1 step", changed=True, page_count_changed=True)


async def execute_tool(ctx: AgentCtx, name: str, inp: dict) -> ToolOut:
    """Dispatch one tool call. Never raises: errors become an error ToolOut."""
    spec = TOOLS_REGISTRY.get(name)
    if spec is None:
        return ToolOut(f"Unknown tool {name}", "unknown tool", ok=False)
    if not isinstance(inp, dict):
        return ToolOut("Tool input must be an object", "bad input", ok=False)
    try:
        if spec.mutating:
            label = spec.label(inp) if spec.label else name
            out, changed = await run_mutation(ctx, label, lambda: spec.handler(ctx, inp), take_snapshot=spec.snapshot)
            out.changed = changed
            if changed:
                ctx.changes.append({"tool": name, "summary": out.summary, "undoable": spec.snapshot})
                if out.page_count_changed:
                    ctx.page_count_changed = True
            return out
        out = await spec.handler(ctx, inp)
        if out.changed:  # undo/redo
            ctx.changes.append({"tool": name, "summary": out.summary, "undoable": False})
            ctx.page_count_changed = ctx.page_count_changed or out.page_count_changed
        return out
    except RouteError as e:
        if e.status == 409 and (e.code == "signed_document" or "signed" in json.dumps(e.detail).lower()):
            msg = ("This PDF is digitally signed; this change would invalidate the signature. "
                   "Nothing was changed. Ask the user whether to proceed anyway (the app shows a "
                   "'Proceed anyway' button); do not retry on your own.")
            ctx.events.append({"type": "needs_confirmation", "reason": "signed_document",
                               "message": str(e.detail) if not isinstance(e.detail, dict) else e.detail.get("detail", msg)})
            return ToolOut(msg, "blocked: document is signed", ok=False, code="signed_document")
        detail = e.detail if isinstance(e.detail, str) else json.dumps(e.detail)[:800]
        return ToolOut(f"Error {e.status}: {detail}", f"failed: {detail[:120]}", ok=False)
    except (ValueError, KeyError, TypeError, FileNotFoundError) as e:
        return ToolOut(f"Error: {e}", f"failed: {str(e)[:120]}", ok=False)
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("AI tool %s crashed", name)
        return ToolOut(f"Internal error in {name}: {type(e).__name__}", "internal error", ok=False)


# ─── Prompts ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are the AI assistant inside a full PDF editor. The user is looking at their PDF next to this chat. \
You can read the whole document, look at rendered pages, and change the actual PDF through tools that call \
the editor's real features (in-place text editing, objects, forms, redaction, OCR, conversion, page \
organisation, page numbers/headers/Bates, bookmarks, comments, watermark, protection, compression, undo).

Answering questions
- Ground every factual statement about the document in its text. Cite the page right after the claim as \
[p. N], or [p. N "short exact quote"] when a specific passage supports it (quote 3-12 words copied exactly \
from the page so the viewer can highlight it). Never invent page numbers. If the document does not say, say so.
- For long documents (mode="retrieved") use search_document / read_pages before answering.
- Use view_page for scanned pages, charts, figures, images, signatures and layout questions.

Editing
- Page numbers in tools are 1-based, exactly as the user says them.
- To change wording, use edit_paragraph (one paragraph) or rewrite_paragraphs (many; one undo step) so the \
text is rewritten IN PLACE with the original font and position. Locate paragraphs with find_paragraph or \
list_paragraphs first. Use replace_text only for short word/phrase substitutions.
- Translating a document: list_paragraphs for each page, then rewrite_paragraphs page by page with the \
translated text of every editable paragraph; keep numbers, names and formatting.
- Smart redaction: read the document, identify sensitive information (personal names, addresses, IDs, \
account/card numbers, phone numbers, emails, dates of birth, medical, financial or confidential business \
details), then call propose_redactions. The user reviews and applies them in the app. Only call \
apply_redactions if the user explicitly says to redact without review.
- Form filling: call list_form_fields, map the user's profile or the reference document to field names, \
then fill_form. Never guess values you do not have; list what is missing. For flat forms, use \
detect_form_fields (and view_page + create_form_field for anything it misses).
- Every change you make is undoable. After changing the document, end with a short bulleted summary of \
exactly what changed and tell the user they can undo it.
- If a tool reports the document is digitally signed, stop and ask the user; never work around it.
- Ask before destructive actions the user did not clearly request (deleting pages, applying redactions).
- Do not reveal passwords in your replies.

Style: concise, scannable, markdown. Use **bold** sparingly. No preamble.
"""

ACTION_PROMPTS: dict[str, str] = {
    "summarize_short": "Summarize this document in 2-3 sentences. Cite pages.",
    "summarize_detailed": "Write a detailed summary of this document with a heading per major section, "
                          "key facts, figures, dates and obligations. Cite pages.",
    "summarize_bullets": "Summarize this document as 5-10 concise bullet points. Cite pages.",
    "explain_selection": "Explain the selected text in plain language: what it means, why it matters in this "
                         "document, and any terms a non-expert would not know. Cite pages.",
    "rewrite_selection": "Rewrite the selected paragraph to be clearer and more polished, and apply it IN PLACE "
                         "in the PDF (find_paragraph with the selected text, then edit_paragraph).",
    "shorten_selection": "Shorten the selected paragraph by about 30-40% while keeping its meaning, and apply it "
                         "IN PLACE in the PDF (find_paragraph, then edit_paragraph).",
    "fix_grammar_selection": "Fix grammar, spelling and punctuation in the selected paragraph without changing its "
                             "meaning, and apply it IN PLACE (find_paragraph, then edit_paragraph). If nothing "
                             "needs fixing, say so and change nothing.",
    "fix_grammar_page": "Fix grammar, spelling and punctuation in every paragraph on the current page, IN PLACE, "
                        "as one rewrite_paragraphs call. Only include paragraphs that actually change.",
    "translate_document": "Translate the entire document into {language}, preserving layout: for each page, "
                          "list_paragraphs and then rewrite_paragraphs with the translation of every editable "
                          "paragraph (label it 'Translate to {language}').",
    "smart_redact": "Find all personal and confidential information in this document that should be redacted "
                    "before sharing it externally, and propose it for review with propose_redactions (include the "
                    "relevant presets too). Briefly explain what you found by category.",
    "autofill_profile": "Fill this form from my saved profile (in the request). Map profile entries to the form "
                        "fields, fill everything you can with fill_form, and list fields you could not fill.",
    "autofill_reference": "Fill this form using the information in the attached reference document. Read it, "
                          "map values to the form fields, fill_form, and list anything missing.",
    "generate_fields": "This is a flat form. Make it fillable: run detect_form_fields, then view the pages and "
                       "create any missing fields with sensible names. Summarize the fields you created.",
    "compare_reference": "Compare this document with the attached reference document. Explain the meaningful "
                         "differences (added, removed, changed terms, numbers and dates), grouped by importance, "
                         "with page citations.",
    "extract_tables_csv": "Extract all tables in this document and give them to me as a CSV download "
                          "(create_download). If there are several tables, separate them with a blank line and a "
                          "'# Table on page N' line.",
    "extract_data_json": "Extract the key structured data in this document (parties, dates, amounts, "
                         "identifiers, line items, contact details) as JSON and give it to me as a download "
                         "(create_download).",
}


def action_prompt(action: str, options: Optional[dict] = None) -> str:
    tmpl = ACTION_PROMPTS.get(action)
    if tmpl is None:
        raise ValueError(f"Unknown action {action!r}")
    opts = {"language": "English", **(options or {})}
    return tmpl.format(**{k: str(v) for k, v in opts.items()})


# ─── The agent loop ──────────────────────────────────────────────────────────

def _make_client():
    return anthropic.AsyncAnthropic(api_key=get_api_key(), max_retries=2)


def _blk(b: Any, key: str, default=None):
    return b.get(key, default) if isinstance(b, dict) else getattr(b, key, default)


_fallbacks_rejected = False


async def _call_model(client, params: dict, on_text: Callable[[str], Awaitable[None]], model: str):
    """One streamed model turn. Emits text deltas, returns the final message.

    Server-side refusal fallbacks are requested by default; if the API rejects
    the parameter (400 mentioning it, raised before any token streams) the turn
    is retried once without it and fallbacks stay off for this process."""
    global _fallbacks_rejected
    use_fb = _fallbacks_enabled(model) and not _fallbacks_rejected
    try:
        return await _stream_once(client, params, on_text, use_fb)
    except Exception as e:
        if use_fb and HAS_ANTHROPIC and isinstance(e, anthropic.BadRequestError) and "fallback" in str(e).lower():
            _fallbacks_rejected = True
            logger.warning("Refusal fallbacks rejected by the API; continuing without them")
            return await _stream_once(client, params, on_text, False)
        raise


async def _stream_once(client, params: dict, on_text, use_fallbacks: bool):
    if use_fallbacks:
        cm = client.beta.messages.stream(**params, betas=["server-side-fallback-2026-07-01"],
                                         extra_body={"fallbacks": "default"})
    else:
        cm = client.messages.stream(**params)
    async with cm as stream:
        async for event in stream:
            if getattr(event, "type", None) == "content_block_delta":
                delta = getattr(event, "delta", None)
                if getattr(delta, "type", None) == "text_delta":
                    await on_text(delta.text)
        return await stream.get_final_message()


def _history_messages(history: list[dict]) -> list[dict]:
    msgs = []
    for e in history[-20:]:
        if e.get("role") in ("user", "assistant") and isinstance(e.get("content"), str) and e["content"].strip():
            if msgs and msgs[-1]["role"] == e["role"]:
                msgs[-1]["content"] += "\n\n" + e["content"]
            else:
                msgs.append({"role": e["role"], "content": e["content"]})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def build_user_content(doc_id: str, message: str, current_page: int, page_count: int,
                       selection_text: Optional[str], region: Optional[dict], profile: Optional[dict],
                       reference_doc_ids: list[str], attach_image: bool) -> list[dict]:
    parts = [f"<context>\nThe user is viewing page {current_page + 1} of {page_count}."]
    if region:
        parts.append(f"The user selected a region on page {region.get('page', current_page) + 1} "
                     f"(x={region.get('x', 0):.0f}, y={region.get('y', 0):.0f}, w={region.get('width', 0):.0f}, "
                     f"h={region.get('height', 0):.0f} pt).")
    if selection_text:
        parts.append(f'<selected_text page="{(region or {}).get("page", current_page) + 1}">\n'
                     f'{selection_text[:8000]}\n</selected_text>')
    if profile:
        parts.append("<user_profile note=\"Provided by the user from their browser for form filling only\">\n"
                     + json.dumps(profile, ensure_ascii=False)[:8000] + "\n</user_profile>")
    for rid in reference_doc_ids:
        try:
            n = len(page_texts(rid))
            parts.append(f'<reference_document doc_id="{rid}" pages="{n}"/> (read it with read_reference_document)')
        except FileNotFoundError:
            pass
    parts.append("</context>")
    content: list[dict] = []
    if attach_image:
        try:
            png = render_page_png(doc_id, current_page)
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": base64.standard_b64encode(png).decode()}})
            parts.append(f"(Page {current_page + 1} has little extractable text, so its image is attached.)")
        except Exception:
            pass
    content.append({"type": "text", "text": "\n".join(parts) + "\n\n" + message})
    return content


def register_purge_hook() -> None:
    """Redaction purges must also drop chat history that quotes redacted text."""
    try:
        from backend import advanced_ops
        hook = getattr(advanced_ops, "register_purge_hook", None)
        if hook:
            hook(lambda doc_id: _histories.pop(doc_id, None))
    except Exception:  # pragma: no cover
        pass


async def run_agent(doc_id: str, message: str, *, current_page: int = 0,
                    selection_text: Optional[str] = None, region: Optional[dict] = None,
                    profile: Optional[dict] = None, reference_doc_ids: Optional[list[str]] = None,
                    allow_break_signature: bool = False, history: Optional[list[dict]] = None,
                    model: Optional[str] = None, run_id: Optional[str] = None,
                    record_history: bool = True) -> AsyncIterator[dict]:
    """Run one user turn of the agent. Yields event dicts (see module doc)."""
    run_id = run_id or uuid.uuid4().hex
    model = model or get_model()
    yield {"type": "start", "run_id": run_id, "model": model}

    if not HAS_ANTHROPIC:
        yield {"type": "setup_required", "reason": "missing_sdk",
               "message": "The anthropic Python package is not installed on the server."}
        return
    if not get_api_key():
        yield {"type": "setup_required", "reason": "no_api_key",
               "message": "Add an Anthropic API key to use the AI assistant."}
        return

    try:
        texts = page_texts(doc_id)
    except FileNotFoundError as e:
        yield {"type": "error", "code": "not_found", "message": str(e)}
        return
    page_count = len(texts)
    current_page = max(0, min(int(current_page or 0), max(page_count - 1, 0)))
    refs = [r for r in (reference_doc_ids or []) if _UUID_RE.match(r or "")]
    hist = _histories.setdefault(doc_id, []) if history is None else history

    doc_block, _full = build_document_block(doc_id, texts, message)
    system = [
        {"type": "text", "text": SYSTEM_PROMPT},
        {"type": "text", "text": doc_block, "cache_control": {"type": "ephemeral"}},
    ]
    attach = page_count > 0 and page_looks_scanned(doc_id, current_page)
    messages = _history_messages(hist)
    messages.append({"role": "user", "content": build_user_content(
        doc_id, message, current_page, page_count, selection_text, region, profile, refs, attach)})

    params_base = {"model": model, "max_tokens": MAX_TOKENS, "system": system,
                   "tools": tool_definitions()}
    effort = _effort_for(model)
    if effort:
        params_base["output_config"] = {"effort": effort}

    from httpx import ASGITransport, AsyncClient
    client = _make_client()
    final_text: list[str] = []
    queue: list[dict] = []

    async def on_text(t: str):
        final_text.append(t)
        queue.append({"type": "text", "delta": t})

    stopped = False
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://ai.internal",
                           timeout=600) as http:
        ctx = AgentCtx(doc_id=doc_id, http=http, allow_break_signature=allow_break_signature,
                       reference_doc_ids=refs, current_page=current_page)
        for _ in range(MAX_ITERATIONS):
            if run_id in _cancelled:
                stopped = True
                break
            task = asyncio.ensure_future(_call_model(client, dict(params_base, messages=messages), on_text, model))
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=0.05)
                    while queue:
                        yield queue.pop(0)
                    if run_id in _cancelled:
                        task.cancel()
                        stopped = True
                        break
                if stopped:
                    break
                resp = task.result()
            except Exception as e:  # API errors
                while queue:
                    yield queue.pop(0)
                yield _api_error_event(e)
                break
            while queue:
                yield queue.pop(0)

            stop_reason = _blk(resp, "stop_reason")
            content = list(_blk(resp, "content", []) or [])
            if stop_reason == "refusal":
                det = _blk(resp, "stop_details")
                yield {"type": "error", "code": "refusal",
                       "message": "The model declined this request" +
                                  (f" ({_blk(det, 'category')})" if det and _blk(det, 'category') else "") + "."}
                break
            tool_uses = [b for b in content if _blk(b, "type") == "tool_use"]
            if stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": content})
                continue
            if stop_reason != "tool_use" or not tool_uses:
                if stop_reason == "max_tokens":
                    yield {"type": "error", "code": "max_tokens", "message": "The response was cut off (too long)."}
                break

            messages.append({"role": "assistant", "content": content})
            results = []
            for tu in tool_uses:
                name, inp, tid = _blk(tu, "name"), _blk(tu, "input") or {}, _blk(tu, "id")
                spec = TOOLS_REGISTRY.get(name)
                label = (spec.label(inp) if spec and spec.label and isinstance(inp, dict) else name)
                yield {"type": "tool_start", "id": tid, "name": name, "input": _safe_input(name, inp), "label": label}
                if run_id in _cancelled:
                    out = ToolOut("Stopped by the user before this ran.", "skipped (stopped)", ok=False)
                else:
                    out = await execute_tool(ctx, name, inp)
                while ctx.events:
                    yield ctx.events.pop(0)
                yield {"type": "tool_result", "id": tid, "name": name, "ok": out.ok,
                       "summary": out.summary, "changed": out.changed, **({"code": out.code} if out.code else {})}
                results.append({"type": "tool_result", "tool_use_id": tid, "content": out.content,
                                **({"is_error": True} if not out.ok else {})})
            messages.append({"role": "user", "content": results})
            if final_text and not final_text[-1].endswith("\n"):
                # keep text written before and after the tool calls apart
                final_text.append("\n\n")
                yield {"type": "text", "delta": "\n\n"}
            if run_id in _cancelled:
                stopped = True
                break
        _cancelled.discard(run_id)

        response = "".join(final_text).strip()
        if stopped:
            response = (response + "\n\n_(Stopped.)_").strip()
        if ctx.changes and not response:
            response = "Done:\n" + "\n".join(f"- {c['summary']}" for c in ctx.changes)
        citations = resolve_citations(doc_id, response)
        new_count = None
        if ctx.page_count_changed or ctx.changes:
            try:
                new_count = len(page_texts(doc_id))
            except FileNotFoundError:
                pass
        if record_history:
            hist.append({"role": "user", "content": message})
            hist.append({"role": "assistant", "content": response or "(no reply)"})
            del hist[:-40]
        yield {
            "type": "done", "run_id": run_id, "response": response,
            "changed": bool(ctx.changes), "changes": ctx.changes,
            "undo_steps": sum(1 for c in ctx.changes if c.get("undoable")),
            "citations": citations, "page_count_changed": ctx.page_count_changed or (new_count is not None and new_count != page_count),
            "new_page_count": new_count, "stopped": stopped,
        }


def _safe_input(name: str, inp: Any) -> Any:
    """Tool input as shown in the UI: no passwords, long text trimmed."""
    if not isinstance(inp, dict):
        return inp
    out = {}
    for k, v in inp.items():
        if "password" in k:
            out[k] = "••••"
        elif isinstance(v, str) and len(v) > 200:
            out[k] = v[:200] + "…"
        elif k == "edits" and isinstance(v, list):
            out[k] = f"{len(v)} edit(s)"
        elif k == "content":
            out[k] = f"{len(str(v))} chars"
        else:
            out[k] = v
    return out


def _api_error_event(e: Exception) -> dict:
    if HAS_ANTHROPIC:
        if isinstance(e, anthropic.AuthenticationError):
            return {"type": "setup_required", "reason": "auth_failed",
                    "message": "The Anthropic API key was rejected. Paste a valid key."}
        if isinstance(e, anthropic.PermissionDeniedError):
            return {"type": "error", "code": "permission", "message": "This API key cannot use the selected model."}
        if isinstance(e, anthropic.NotFoundError):
            return {"type": "error", "code": "model_not_found", "message": "The selected model is not available to this key."}
        if isinstance(e, anthropic.RateLimitError):
            return {"type": "error", "code": "rate_limit", "message": "Rate limited by the AI service. Try again shortly."}
        if isinstance(e, anthropic.BadRequestError) and "credit balance" in str(e).lower():
            return {"type": "error", "code": "billing",
                    "message": "Your Anthropic account is out of credits. Add credits in the Anthropic Console "
                               "(Plans & Billing), then try again."}
        if isinstance(e, anthropic.BadRequestError):
            return {"type": "error", "code": "bad_request", "message": f"The AI service rejected the request: {e.message}"}
        if isinstance(e, anthropic.APIStatusError):
            return {"type": "error", "code": "api_error", "message": f"AI service error ({e.status_code})."}
        if isinstance(e, anthropic.APIConnectionError):
            return {"type": "error", "code": "network", "message": "Could not reach the AI service."}
    logger.error("AI agent error: %s", type(e).__name__)
    return {"type": "error", "code": "internal", "message": f"AI error: {type(e).__name__}"}


def cancel_run(run_id: str) -> None:
    _cancelled.add(run_id)


async def run_agent_collect(doc_id: str, message: str, **kw) -> dict:
    """Non-streaming wrapper: run the agent and return the done payload + extras."""
    events = []
    async for ev in run_agent(doc_id, message, **kw):
        events.append(ev)
    done = next((e for e in reversed(events) if e["type"] == "done"), None)
    setup = next((e for e in events if e["type"] == "setup_required"), None)
    errors = [e for e in events if e["type"] == "error"]
    result = dict(done or {"type": "done", "response": "", "changed": False, "changes": [], "citations": []})
    if setup:
        result["setup_required"] = setup
        result["response"] = setup["message"]
    if errors and not result.get("response"):
        result["response"] = errors[0]["message"]
    result["errors"] = errors
    result["events"] = [e for e in events if e["type"] in (
        "redaction_review", "file", "download", "needs_confirmation", "tool_result")]
    return result


async def understand_and_execute(message: str, doc_id: str, current_page: int, page_text: str,
                                 page_count: int, chat_history: list[dict]) -> dict:
    """Legacy contract for main.py's /chat endpoint."""
    if not HAS_ANTHROPIC:
        return {"response": "The anthropic package is not installed.", "changed": False,
                "intent": {"action": "error", "reason": "missing_sdk"}}
    if not get_api_key():
        return {"response": "No API key configured.", "changed": False,
                "intent": {"action": "error", "reason": "no_api_key"}}
    res = await run_agent_collect(doc_id, message, current_page=current_page, history=list(chat_history),
                                  record_history=False)
    if res.get("setup_required"):
        return {"response": res["response"], "changed": False,
                "intent": {"action": "error", "reason": res["setup_required"]["reason"]}}
    return {
        "response": res.get("response", ""), "changed": res.get("changed", False),
        "page_count_changed": res.get("page_count_changed") or None,
        "intent": {"action": "ai_agent", "changes": res.get("changes", [])},
    }


# The AI router (backend.features.ai.router) is mounted explicitly by main.py.


register_purge_hook()
