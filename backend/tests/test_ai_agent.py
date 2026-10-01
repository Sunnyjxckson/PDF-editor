"""AI assistant: tool dispatch (every tool, against the real PDF), undo
snapshots, signed-document guard, citations, the streamed agent loop (with a
mocked Anthropic client - no network), no-key setup state, routes."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from types import SimpleNamespace

import fitz
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend import advanced_ops, ai_engine
from backend.main import app

# ─── fixtures ────────────────────────────────────────────────────────────────


def _scan_png(lines: list[str]) -> bytes:
    """A 'scanned' page: text rendered to a bitmap (no text layer)."""
    src = fitz.open()
    p = src.new_page(width=612, height=792)
    y = 120
    for ln in lines:
        p.insert_text((72, y), ln, fontsize=28)
        y += 60
    png = p.get_pixmap(dpi=200).tobytes("png")
    src.close()
    return png


def build_rich_pdf() -> bytes:
    doc = fitz.open()
    p = doc.new_page(width=612, height=792)
    p.insert_text((72, 72), "Quarterly Report 2026", fontname="hebo", fontsize=20)
    p.insert_text((72, 110), "The total revenue was 4200 dollars this quarter.", fontname="helv", fontsize=12)
    p.insert_text((72, 170), "Contact: John Smith, SSN 123-45-6789, phone 704-555-1234.", fontname="helv", fontsize=11)
    p.insert_text((72, 230), "Courier mono line here.", fontname="cour", fontsize=11)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 40), False)
    pix.set_rect(pix.irect, (30, 120, 200))
    p.insert_image(fitz.Rect(72, 300, 272, 420), pixmap=pix)
    p.insert_text((300, 360), "Caption beside the image.", fontname="helv", fontsize=10)
    p.draw_rect(fitz.Rect(72, 460, 372, 520), color=(0, 0, 0))
    # page 2: scanned
    p2 = doc.new_page(width=612, height=792)
    p2.insert_image(p2.rect, stream=_scan_png(["Invoice Number 8841", "Scanned Customer Copy"]))
    # page 3: AcroForm
    p3 = doc.new_page(width=612, height=792)
    p3.insert_text((72, 80), "Name:", fontsize=11)
    w = fitz.Widget(); w.field_type = fitz.PDF_WIDGET_TYPE_TEXT; w.field_name = "full_name"
    w.rect = fitz.Rect(130, 65, 330, 85); p3.add_widget(w)
    w = fitz.Widget(); w.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX; w.field_name = "agree"
    w.rect = fitz.Rect(130, 105, 145, 120); p3.add_widget(w)
    # page 4: flat form
    p4 = doc.new_page(width=612, height=792)
    p4.insert_text((72, 100), "Employee name: ______________________________", fontsize=11)
    p4.insert_text((72, 140), "Address: ______________________________", fontsize=11)
    doc.set_toc([[1, "Report", 1], [1, "Scan", 2], [1, "Form", 3]])
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


RICH = build_rich_pdf()


async def _upload(client: AsyncClient, data: bytes) -> str:
    r = await client.post("/api/pdf/upload", files={"file": ("t.pdf", data, "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest_asyncio.fixture
async def rich(client: AsyncClient):
    doc_id = await _upload(client, RICH)
    yield doc_id
    await client.delete(f"/api/pdf/{doc_id}")


@pytest_asyncio.fixture
async def ref(client: AsyncClient):
    doc = fitz.open(stream=RICH, filetype="pdf")
    doc[0].insert_text((72, 600), "Added clause: payment due in 30 days.", fontsize=11)
    doc_id = await _upload(client, doc.tobytes())
    yield doc_id
    await client.delete(f"/api/pdf/{doc_id}")


def pdf_path(doc_id: str):
    return ai_engine.doc_path(doc_id)


def open_pdf(doc_id: str) -> fitz.Document:
    return fitz.open(str(pdf_path(doc_id)))


def sha(doc_id: str) -> str:
    return hashlib.sha1(pdf_path(doc_id).read_bytes()).hexdigest()


def history(doc_id: str) -> list[dict]:
    return advanced_ops._load_history(doc_id).get("versions", [])


def page_text(doc_id: str, i: int) -> str:
    with open_pdf(doc_id) as d:
        return d[i].get_text()


async def run_tool(doc_id: str, name: str, inp: dict, refs=(), allow=False):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://ai.internal") as http:
        ctx = ai_engine.AgentCtx(doc_id=doc_id, http=http, reference_doc_ids=list(refs),
                                 allow_break_signature=allow)
        out = await ai_engine.execute_tool(ctx, name, inp)
    return out, ctx


# ─── dispatch: read-only tools ───────────────────────────────────────────────

async def test_read_tools_dispatch(rich, ref):
    before = sha(rich)
    h0 = len(history(rich))

    out, _ = await run_tool(rich, "get_document_info", {})
    assert out.ok and "4 pages" in out.summary

    out, _ = await run_tool(rich, "search_document", {"query": "total revenue"})
    hits = json.loads(out.content)["hits"]
    assert out.ok and hits[0]["page"] == 1

    out, _ = await run_tool(rich, "read_pages", {"pages": [1, 9]})
    assert "Quarterly Report" in out.content and 'number="9" error' in out.content

    out, _ = await run_tool(rich, "view_page", {"page": 2})
    img = out.content[0]
    assert img["type"] == "image" and img["source"]["media_type"] == "image/png"
    pix = fitz.Pixmap(base64.b64decode(img["source"]["data"]))
    assert pix.width > 500

    out, _ = await run_tool(rich, "list_paragraphs", {"page": 1})
    paras = json.loads(out.content)
    assert any("4200" in (p["text"] or "") for p in paras) and all(p["paragraph_id"] for p in paras)

    out, _ = await run_tool(rich, "find_paragraph", {"text": "total revenue was 4200"})
    found = json.loads(out.content)
    assert found and found[0]["page"] == 1 and "4200" in found[0]["text"]

    out, _ = await run_tool(rich, "list_objects", {"page": 1})
    assert out.ok and "xref" in out.content

    out, _ = await run_tool(rich, "list_form_fields", {})
    assert out.ok and "full_name" in out.content

    for name in ("list_bookmarks", "list_comments", "security_audit", "get_edit_history"):
        out, _ = await run_tool(rich, name, {})
        assert out.ok, (name, out.content)
    out, _ = await run_tool(rich, "list_bookmarks", {})
    assert "Report" in out.content

    out, _ = await run_tool(rich, "extract_tables", {"page": 1})
    assert out.ok, out.content

    out, _ = await run_tool(rich, "read_reference_document", {"doc_id": ref}, refs=[ref])
    assert "payment due in 30 days" in out.content
    out, _ = await run_tool(rich, "read_reference_document", {"doc_id": ref})  # not attached
    assert not out.ok

    out, _ = await run_tool(rich, "compare_with_reference", {"doc_id": ref}, refs=[ref])
    assert out.ok and "payment due" in out.content

    out, ctx = await run_tool(rich, "export_document", {"format": "txt"})
    assert out.ok and ctx.events[0]["type"] == "download" and ctx.events[0]["url"].endswith("/export/txt")

    out, ctx = await run_tool(rich, "create_download", {"filename": "t", "format": "csv", "content": "a,b\n1,2"})
    assert out.ok and ctx.events[0] == {"type": "file", "filename": "t.csv", "mime": "text/csv", "content": "a,b\n1,2"}
    out, _ = await run_tool(rich, "create_download", {"filename": "t", "format": "json", "content": "{bad"})
    assert not out.ok

    out, _ = await run_tool(rich, "find_redaction_candidates", {"presets": ["ssn"]})
    data = json.loads(out.content)
    assert data["count"] == 1 and data["matches"][0]["page"] == 1

    out, ctx = await run_tool(rich, "propose_redactions",
                              {"items": [{"page": 1, "text": "John Smith", "category": "name"},
                                         {"page": 3, "text": "123-45-6789"}],  # wrong page on purpose
                               "presets": ["phone"]})
    review = ctx.events[0]
    assert review["type"] == "redaction_review"
    texts = {i["text"] for i in review["items"]}
    assert {"John Smith", "123-45-6789"} <= texts and any("704" in t for t in texts)
    assert all(i["page"] == 1 and i["page_index"] == 0 and i["rects"] for i in review["items"])

    # nothing above touched the file or the undo history
    assert sha(rich) == before and len(history(rich)) == h0


# ─── dispatch: mutating tools (each checked on the real PDF, then undone) ────

def _check_edit(d):
    t = d[0].get_text()
    return "9000" in t and "4200" not in t


MUTATIONS = [
    ("edit_paragraph", {"page": 1, "match_text": "total revenue was 4200",
                        "new_text": "The total revenue was 9000 dollars this quarter."}, _check_edit),
    ("rewrite_paragraphs", {"label": "Translate", "edits": [
        {"page": 1, "match_text": "Quarterly Report 2026", "new_text": "Rapport trimestriel 2026"},
        {"page": 1, "match_text": "Courier mono line", "new_text": "Ligne en police mono."}]},
     lambda d: "Rapport trimestriel" in d[0].get_text() and "Ligne en police mono" in d[0].get_text()),
    ("delete_paragraph", {"page": 1, "match_text": "Courier mono line here."},
     lambda d: "Courier mono" not in d[0].get_text()),
    ("move_paragraph", {"page": 1, "match_text": "Courier mono line here.", "dx": 0, "dy": 100},
     lambda d: d[0].search_for("Courier mono")[0].y0 > 300),
    ("replace_text", {"find": "Caption", "replace": "Legend"},
     lambda d: "Legend beside" in d[0].get_text() and "Caption" not in d[0].get_text()),
    ("add_text", {"page": 1, "x": 72, "y": 700, "text": "Added by AI"},
     lambda d: "Added by AI" in d[0].get_text()),
    ("move_object", {"page": 1, "kind": "image", "xref": "XREF", "new_bbox": [300, 500, 500, 620]},
     lambda d: d[0].get_image_rects(d[0].get_images()[0][0])[0].y0 > 490),
    ("delete_object", {"page": 1, "kind": "image", "xref": "XREF"},
     lambda d: not d[0].get_images()),
    ("fill_form", {"values": {"full_name": "Ada Lovelace", "agree": True}},
     lambda d: {w.field_name: w.field_value for w in d[2].widgets()}["full_name"] == "Ada Lovelace"),
    ("create_form_field", {"page": 4, "type": "text", "rect": [200, 300, 400, 320], "name": "ai_field"},
     lambda d: any(w.field_name == "ai_field" for w in d[3].widgets())),
    ("detect_form_fields", {"pages": [4]}, lambda d: len(list(d[3].widgets())) >= 2),
    ("compress_document", {"preset": "smallest"}, lambda d: len(d) == 4),
    ("rotate_pages", {"pages": [1], "angle": 90}, lambda d: d[0].rotation == 90),
    ("delete_pages", {"pages": [4]}, lambda d: len(d) == 3),
    ("insert_blank_pages", {"position": 2, "count": 1}, lambda d: len(d) == 5 and not d[1].get_text().strip()),
    ("duplicate_pages", {"pages": [1]}, lambda d: len(d) == 5 and "Quarterly" in d[1].get_text()),
    ("reorder_pages", {"order": [3, 1, 2, 4]}, lambda d: "Name:" in d[0].get_text() and "Quarterly" in d[1].get_text()),
    ("add_page_numbers", {"format": "Page {n} of {total}"}, lambda d: "Page 1 of 4" in d[0].get_text()),
    ("add_header_footer", {"header_center": "ACME CONFIDENTIAL"}, lambda d: "ACME CONFIDENTIAL" in d[0].get_text()),
    ("add_bates_numbers", {"prefix": "ABC", "digits": 6, "start": 1}, lambda d: "ABC000001" in d[0].get_text()),
    ("add_bookmark", {"title": "Appendix", "page": 4}, lambda d: ["Appendix", 4] in [[t[1], t[2]] for t in d.get_toc()]),
    ("delete_bookmark", {"index": 0}, lambda d: "Report" not in [t[1] for t in d.get_toc()]),
    ("add_comment", {"page": 1, "type": "highlight", "search": "total revenue", "text": "check"},
     lambda d: any(a.type[1] == "Highlight" for a in d[0].annots())),
    ("add_watermark", {"text": "DRAFT"}, lambda d: "DRAFT" in d[0].get_text()),
    ("protect_document", {"owner_password": "owner-pw-123"},
     lambda d: bool(d.metadata.get("encryption"))),
    ("sanitize_document", {"metadata": True}, lambda d: True),
    ("run_ocr", {"pages": [2]}, lambda d: "Invoice" in d[1].get_text()),
]


@pytest.mark.parametrize("name,inp,check", MUTATIONS, ids=[m[0] for m in MUTATIONS])
async def test_mutating_tool_dispatch_snapshots_and_undo(client, rich, name, inp, check):
    if inp.get("xref") == "XREF":
        with open_pdf(rich) as d:
            inp = dict(inp, xref=d[0].get_images()[0][0])
    if name == "sanitize_document":
        with fitz.open(str(pdf_path(rich))) as d:
            d.set_metadata({"author": "Secret Author", "title": "T"})
            d.saveIncr()
    before = sha(rich)
    h0 = history(rich)

    out, ctx = await run_tool(rich, name, inp)
    assert out.ok, out.content
    assert out.changed, f"{name} reported no change"
    with open_pdf(rich) as d:
        if name == "sanitize_document":
            assert "Secret Author" not in (d.metadata.get("author") or "")
        else:
            assert check(d), f"{name}: PDF does not show the change"
    assert ctx.changes and ctx.changes[0]["tool"] == name and ctx.changes[0]["undoable"]

    # exactly ONE new history entry, labelled as an AI change, holding the pre-op file
    h1 = history(rich)
    assert len(h1) == len(h0) + 1, [v["operation"] for v in h1]
    assert h1[-1]["operation"].startswith("AI: ")
    snap = advanced_ops._history_dir(rich) / h1[-1]["filename"]
    assert hashlib.sha1(snap.read_bytes()).hexdigest() == before

    r = await client.post(f"/api/pdf/{rich}/undo")
    assert r.status_code == 200 and r.json()["undone_operation"].startswith("AI: ")
    assert sha(rich) == before


async def test_failed_mutation_leaves_no_history(rich):
    before, h0 = sha(rich), len(history(rich))
    out, ctx = await run_tool(rich, "edit_paragraph", {"page": 1, "match_text": "no such paragraph xyz",
                                                       "new_text": "x"})
    assert not out.ok and not out.changed and not ctx.changes
    out, _ = await run_tool(rich, "delete_pages", {"pages": [99]})
    assert not out.ok
    assert sha(rich) == before and len(history(rich)) == h0


async def test_apply_redactions_tool_is_real_and_purges_history(rich):
    await run_tool(rich, "add_watermark", {"text": "DRAFT"})
    assert history(rich)
    out, ctx = await run_tool(rich, "apply_redactions", {"presets": ["ssn"], "query": "John Smith"})
    assert out.ok and out.changed
    with open_pdf(rich) as d:
        t = d[0].get_text()
    assert "123-45-6789" not in t and "John Smith" not in t and "phone 704" in t
    # redaction never leaves a pre-redaction snapshot behind
    assert history(rich) == []
    assert ctx.changes[0]["undoable"] is False


async def test_undo_and_redo_tools(rich):
    await run_tool(rich, "add_watermark", {"text": "DRAFT"})
    out, _ = await run_tool(rich, "undo", {"steps": 1})
    assert out.ok and "DRAFT" not in page_text(rich, 0)
    out, _ = await run_tool(rich, "redo", {})
    assert out.ok and "DRAFT" in page_text(rich, 0)


async def test_signed_document_guard_asks_user(rich, monkeypatch):
    monkeypatch.setattr(advanced_ops, "signature_info",
                        lambda p: {"signed": True, "count": 1, "signers": ["Alice"]})
    before, h0 = sha(rich), len(history(rich))
    out, ctx = await run_tool(rich, "add_watermark", {"text": "DRAFT"})
    assert not out.ok and out.code == "signed_document"
    assert ctx.events[0]["type"] == "needs_confirmation"
    assert sha(rich) == before and len(history(rich)) == h0
    # read tools still work on a signed document
    out, _ = await run_tool(rich, "list_paragraphs", {"page": 1})
    assert out.ok
    # after the user confirmed, the request carries the override header
    out, _ = await run_tool(rich, "add_watermark", {"text": "DRAFT"}, allow=True)
    assert out.ok and "DRAFT" in page_text(rich, 0)


def test_every_registered_tool_has_a_dispatch_test():
    tested = {m[0] for m in MUTATIONS} | {
        "get_document_info", "search_document", "read_pages", "view_page", "list_paragraphs",
        "find_paragraph", "list_objects", "list_form_fields", "list_bookmarks", "list_comments",
        "extract_tables", "security_audit", "get_edit_history", "read_reference_document",
        "compare_with_reference", "export_document", "create_download", "find_redaction_candidates",
        "propose_redactions", "apply_redactions", "undo", "redo"}
    assert set(ai_engine.TOOLS_REGISTRY) == tested
    for d in ai_engine.tool_definitions():
        assert d["input_schema"]["type"] == "object" and d["description"]
    mut = {n for n, s in ai_engine.TOOLS_REGISTRY.items() if s.mutating}
    assert mut == {m[0] for m in MUTATIONS} | {"apply_redactions"}


# ─── citations ───────────────────────────────────────────────────────────────

def test_parse_citations():
    t = 'Revenue was 4200 [p. 1] and the SSN is listed [p. 1 "SSN 123-45-6789"], see [p.12: “foo bar”].'
    c = ai_engine.parse_citations(t)
    assert [(x["page"], x["quote"]) for x in c] == [(1, None), (1, "SSN 123-45-6789"), (12, "foo bar")]
    assert ai_engine.parse_citations("no cites [page 3] [p. x]") == []


async def test_resolve_citations_rects(rich):
    c = ai_engine.resolve_citations(rich, 'A [p. 1 "total revenue was 4200"] B [p. 99] C [p. 1]')
    assert c[0]["page"] == 1 and c[0]["valid"] and len(c[0]["rects"]) == 1
    x0, y0, x1, y1 = c[0]["rects"][0]
    assert 90 < x0 < 100 and 95 < y0 < 115 and x1 > x0 + 50  # "total" follows "The " at x=72
    assert c[1] == {"page": 99, "quote": None, "rects": [], "valid": False, "marker": "[p. 99]"}


# ─── mocked Anthropic client ─────────────────────────────────────────────────

def text_block(t):
    return SimpleNamespace(type="text", text=t)


def tool_block(name, inp, tid=None):
    return SimpleNamespace(type="tool_use", id=tid or f"tu_{name}", name=name, input=inp)


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            for b in self.msg.content:
                if b.type == "text":
                    for i in range(0, len(b.text), 7):
                        yield SimpleNamespace(type="content_block_delta",
                                              delta=SimpleNamespace(type="text_delta", text=b.text[i:i + 7]))
        return gen()

    async def get_final_message(self):
        return self.msg


class FakeClient:
    """Stands in for anthropic.AsyncAnthropic: scripted turns, records requests."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []
        self.messages = self
        self.beta = SimpleNamespace(messages=self)

    def stream(self, **params):
        self.calls.append(json.loads(json.dumps(params, default=lambda o: vars(o))))
        blocks = self.turns.pop(0)
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        return FakeStream(SimpleNamespace(content=blocks, stop_reason=stop, stop_details=None))


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def install(turns, key="sk-ant-test"):
        fc = FakeClient(turns)
        holder["client"] = fc
        monkeypatch.setattr(ai_engine, "_make_client", lambda: fc)
        monkeypatch.setitem(ai_engine._runtime, "api_key", key)
        return fc
    yield install
    ai_engine._histories.clear()


def parse_sse(body: str) -> list:
    out = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        assert chunk.startswith("data: "), chunk
        payload = chunk[len("data: "):]
        out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


async def test_stream_event_format_edit_and_citation(client, rich, fake, monkeypatch):
    monkeypatch.setitem(ai_engine._runtime, "model", None)
    monkeypatch.delenv("AI_MODEL", raising=False)
    fc = fake([
        [text_block("Updating the revenue figure."),
         tool_block("edit_paragraph", {"page": 1, "match_text": "total revenue was 4200",
                                       "new_text": "The total revenue was 9000 dollars this quarter."})],
        [text_block('Done - revenue now reads 9000 [p. 1 "total revenue was 9000"]. You can undo this.')],
    ])
    r = await client.post("/api/ai/chat/stream", json={"doc_id": rich, "message": "make revenue 9000",
                                                       "current_page": 0})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = parse_sse(r.text)
    assert evs[-1] == "[DONE]"
    types = [e["type"] for e in evs[:-1]]
    assert types[0] == "start" and types[-1] == "done"
    assert types.index("tool_start") < types.index("tool_result") < len(types) - 1
    assert "".join(e["delta"] for e in evs if isinstance(e, dict) and e["type"] == "text").startswith("Updating")
    tr = next(e for e in evs if isinstance(e, dict) and e["type"] == "tool_result")
    assert tr["ok"] and tr["changed"] and tr["name"] == "edit_paragraph"
    done = evs[-2]
    assert done["changed"] and done["undo_steps"] == 1 and done["changes"][0]["tool"] == "edit_paragraph"
    assert done["citations"][0]["page"] == 1 and done["citations"][0]["rects"]
    assert "9000" in page_text(rich, 0)

    # request shape: current model, cached document block, all tools, effort
    first, second = fc.calls
    assert first["model"] == "claude-sonnet-5-5"
    assert first["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert '<page number="1">' in first["system"][1]["text"]
    assert len(first["tools"]) == len(ai_engine.TOOLS_REGISTRY)
    assert first["output_config"] == {"effort": "medium"}
    assert first["betas"] == ["server-side-fallback-2026-07-01"]
    # the tool result went back to the model in one user message
    last = second["messages"][-1]
    assert last["role"] == "user" and last["content"][0]["type"] == "tool_result"
    assert last["content"][0]["tool_use_id"] == "tu_edit_paragraph"


async def test_no_key_setup_state(client, rich, monkeypatch):
    monkeypatch.setitem(ai_engine._runtime, "api_key", None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = (await client.get("/api/ai/config")).json()
    assert cfg["api_key_set"] is False and cfg["ai_available"] is False
    assert {m["id"] for m in cfg["models"]} >= {"claude-sonnet-5-5", "claude-opus-5-5"}
    r = await client.post("/api/ai/chat/stream", json={"doc_id": rich, "message": "hi"})
    evs = parse_sse(r.text)
    assert [e["type"] for e in evs[:-1]] == ["start", "setup_required"]
    assert evs[1]["reason"] == "no_api_key"
    r = await client.post("/api/ai/chat", json={"doc_id": rich, "message": "hi"})
    assert r.json()["setup_required"]["reason"] == "no_api_key"

    assert (await client.post("/api/ai/key", json={"api_key": "not a key"})).status_code == 400
    secret = "sk-ant-test-SECRET-123"
    r = await client.post("/api/ai/key", json={"api_key": secret})
    try:
        assert r.status_code == 200 and secret not in r.text and r.json()["api_key_set"] is True
        cfg = await client.get("/api/ai/config")
        assert secret not in cfg.text and cfg.json()["ai_available"] is True
    finally:
        ai_engine.set_api_key("")


async def test_model_selection(client, rich, fake, monkeypatch):
    fc = fake([[text_block("ok")]])
    try:
        assert (await client.post("/api/ai/model", json={"model": "gpt-4"})).status_code == 400
        r = await client.post("/api/ai/model", json={"model": "claude-opus-5-5"})
        assert r.json()["model"] == "claude-opus-5-5"
        await client.post("/api/ai/chat", json={"doc_id": rich, "message": "hi"})
        assert fc.calls[0]["model"] == "claude-opus-5-5" and fc.calls[0]["output_config"]["effort"] == "high"
    finally:
        ai_engine._runtime["model"] = None
    monkeypatch.setenv("AI_MODEL", "claude-haiku-4-5")
    assert ai_engine.get_model() == "claude-haiku-4-5"
    assert ai_engine._effort_for("claude-haiku-4-5") is None
    assert not ai_engine._fallbacks_enabled("claude-haiku-4-5")


async def test_vision_attaches_scanned_page_and_actions(client, rich, fake):
    fc = fake([[text_block("It is an invoice [p. 2].")]])
    r = await client.post("/api/ai/chat", json={"doc_id": rich, "action": "summarize_short", "current_page": 1})
    data = r.json()
    assert data["citations"][0]["page"] == 2
    content = fc.calls[0]["messages"][-1]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"
    assert "2-3 sentences" in content[-1]["text"]
    assert (await client.post("/api/ai/chat", json={"doc_id": rich, "action": "nope"})).status_code == 400
    p = ai_engine.action_prompt("translate_document", {"language": "German"})
    assert "German" in p and "rewrite_paragraphs" in p


async def test_selection_profile_and_reference_go_to_model(client, rich, ref, fake):
    fc = fake([[text_block("ok")]])
    await client.post("/api/ai/chat", json={
        "doc_id": rich, "action": "autofill_profile", "current_page": 2,
        "region": {"page": 0, "x": 70, "y": 95, "width": 400, "height": 20},
        "profile": {"full_name": "Ada Lovelace"}, "reference_doc_ids": [ref, "../etc"]})
    text = fc.calls[0]["messages"][-1]["content"][-1]["text"]
    assert "total revenue was 4200" in text           # region text extracted server-side
    assert '"full_name": "Ada Lovelace"' in text
    assert f'doc_id="{ref}"' in text and "../etc" not in text


async def test_long_document_uses_retrieval(rich, monkeypatch):
    monkeypatch.setattr(ai_engine, "FULL_TEXT_CHAR_LIMIT", 10)
    texts = ai_engine.page_texts(rich)
    block, full = ai_engine.build_document_block(rich, texts, "what is the invoice number?")
    assert not full and 'mode="retrieved"' in block and "<outline>" in block
    assert ai_engine.retrieve_pages(["alpha beta", "invoice number 8841", "gamma"], "invoice number", 1) == [1]


async def test_history_is_text_only_and_appended(client, rich, fake):
    fc = fake([[text_block("first answer")], [text_block("second answer")]])
    await client.post("/api/ai/chat", json={"doc_id": rich, "message": "q1"})
    await client.post("/api/ai/chat", json={"doc_id": rich, "message": "q2"})
    msgs = fc.calls[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"] == "q1" and msgs[1]["content"] == "first answer"


async def test_stop_cancels_run(rich, fake):
    fake([[tool_block("add_watermark", {"text": "DRAFT"})], [text_block("never")]])
    ai_engine.cancel_run("run-x")
    evs = [e async for e in ai_engine.run_agent(rich, "watermark", run_id="run-x")]
    assert evs[-1]["type"] == "done" and evs[-1]["stopped"] is True and not evs[-1]["changed"]
    assert "DRAFT" not in page_text(rich, 0)


async def test_api_errors_become_events(rich, fake, monkeypatch):
    import anthropic
    import httpx

    class Boom:
        messages = None

    def boom():
        b = Boom()

        def stream(**p):
            req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=req), body=None)
        b.messages = SimpleNamespace(stream=stream)
        b.beta = SimpleNamespace(messages=b.messages)
        return b
    fake([])
    monkeypatch.setattr(ai_engine, "_make_client", boom)
    evs = [e async for e in ai_engine.run_agent(rich, "hi")]
    assert any(e["type"] == "setup_required" and e["reason"] == "auth_failed" for e in evs)


async def test_smart_redaction_review_then_apply_route(client, rich, fake):
    fake([[tool_block("propose_redactions", {"items": [{"page": 1, "text": "John Smith", "reason": "name"}],
                                             "presets": ["ssn"]})],
          [text_block("Found a name and an SSN; review them in the list.")]])
    data = (await client.post("/api/ai/chat", json={"doc_id": rich, "action": "smart_redact"})).json()
    review = next(e for e in data["events"] if e["type"] == "redaction_review")
    assert {i["text"] for i in review["items"]} == {"John Smith", "123-45-6789"}
    assert "John Smith" in page_text(rich, 0)  # not applied yet
    areas = [{"page": i["page_index"], "rect": r} for i in review["items"] for r in i["rects"]]
    r = await client.post("/api/ai/redactions/apply", json={"doc_id": rich, "areas": areas})
    assert r.status_code == 200, r.text
    t = page_text(rich, 0)
    assert "John Smith" not in t and "123-45-6789" not in t and "Contact:" in t


async def test_legacy_understand_and_execute(rich, fake):
    fake([[tool_block("add_watermark", {"text": "DRAFT"})], [text_block("Added a DRAFT watermark.")]])
    res = await ai_engine.understand_and_execute("watermark it", rich, 0, "", 4, [])
    assert res["changed"] is True and "DRAFT" in res["response"]
    assert "DRAFT" in page_text(rich, 0)


async def test_locate_route(client, rich):
    r = await client.post("/api/ai/locate", json={"doc_id": rich, "page": 1, "quote": "Caption beside"})
    assert r.json()["valid"] and r.json()["rects"]
    assert (await client.post("/api/ai/locate", json={"doc_id": "x", "page": 1, "quote": "a"})).status_code == 400


def _bad_request(msg):
    import anthropic
    import httpx
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError(msg, response=httpx.Response(400, request=req), body=None)


async def test_rejected_fallbacks_are_retried_without_them(rich, fake, monkeypatch):
    fc = fake([[text_block("hello")]])
    monkeypatch.setattr(ai_engine, "_fallbacks_rejected", False)
    real_beta_stream = fc.stream
    calls = {"beta": 0}

    def beta_stream(**params):
        calls["beta"] += 1
        raise _bad_request("fallbacks: Extra inputs are not permitted")
    fc.beta = SimpleNamespace(messages=SimpleNamespace(stream=beta_stream))
    evs = [e async for e in ai_engine.run_agent(rich, "hi")]
    assert calls["beta"] == 1 and evs[-1]["response"] == "hello"
    assert "betas" not in fc.calls[0]          # retried on the plain endpoint
    assert ai_engine._fallbacks_rejected is True
    assert real_beta_stream


async def test_out_of_credit_error_is_explained(rich, fake, monkeypatch):
    fc = fake([])

    def stream(**params):
        raise _bad_request("Your credit balance is too low to access the Anthropic API.")
    fc.messages = SimpleNamespace(stream=stream)
    fc.beta = SimpleNamespace(messages=fc.messages)
    evs = [e async for e in ai_engine.run_agent(rich, "hi")]
    err = next(e for e in evs if e["type"] == "error")
    assert err["code"] == "billing" and "credits" in err["message"]
