"""
AI assistant routes (all under /api/ai so the signed-document guard, which
only inspects /api/pdf/{doc_id}/... templates, does not refuse a chat that
merely READS a signed PDF; each mutating tool call is still guarded because
it goes through the real /api/pdf/{doc_id}/... feature route).

    GET    /api/ai/config              status, current model, model list (never the key)
    POST   /api/ai/key                 {api_key, persist?}  store key server-side
    DELETE /api/ai/key                 forget the runtime key
    POST   /api/ai/model               {model}
    POST   /api/ai/chat/stream         SSE: start/text/tool_start/tool_result/.../done
    POST   /api/ai/chat                same, non-streaming JSON
    POST   /api/ai/chat/stop           {run_id}
    POST   /api/ai/redactions/apply    apply the user-approved smart-redaction review list
    POST   /api/ai/locate              {doc_id, page, quote} -> highlight rects for a citation
    GET    /api/ai/actions             one-click action ids

SSE wire format: one ``data: <json>\\n\\n`` line per event, then ``data: [DONE]``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Optional

import fitz
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend import ai_engine

router = APIRouter(prefix="/api/ai", tags=["ai"])

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_MESSAGE = 20000


def _check_doc(doc_id: str) -> None:
    if not _UUID_RE.match(doc_id or ""):
        raise HTTPException(status_code=400, detail="Invalid document ID")
    try:
        ai_engine.doc_path(doc_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document not found")


class KeyRequest(BaseModel):
    api_key: str = Field(..., min_length=1, max_length=400)
    persist: bool = False


class ModelRequest(BaseModel):
    model: str


class Region(BaseModel):
    page: int
    x: float
    y: float
    width: float
    height: float


class ChatRequest(BaseModel):
    doc_id: str
    message: Optional[str] = None
    action: Optional[str] = None
    action_options: dict[str, Any] = Field(default_factory=dict)
    current_page: int = 0
    selection_text: Optional[str] = None
    region: Optional[Region] = None
    profile: Optional[dict[str, Any]] = None
    reference_doc_ids: list[str] = Field(default_factory=list)
    allow_break_signature: bool = False
    model: Optional[str] = None


class StopRequest(BaseModel):
    run_id: str


class ReviewArea(BaseModel):
    page: int  # 0-based page index (redaction_review item.page_index)
    rect: list[float]


class ApplyReviewRequest(BaseModel):
    doc_id: str
    areas: list[ReviewArea]
    overlay_text: Optional[str] = None
    allow_break_signature: bool = False


class LocateRequest(BaseModel):
    doc_id: str
    page: int  # 1-based
    quote: str


@router.get("/config")
async def get_config():
    return ai_engine.ai_status()


@router.post("/key")
async def set_key(req: KeyRequest):
    key = req.api_key.strip()
    if not key.startswith("sk-") or any(c.isspace() for c in key):
        raise HTTPException(status_code=400, detail="That does not look like an Anthropic API key (sk-ant-...).")
    ai_engine.set_api_key(key, persist=req.persist)
    st = ai_engine.ai_status()
    return {"status": "ok", "api_key_set": st["api_key_set"], "ai_available": st["ai_available"]}


@router.delete("/key")
async def clear_key():
    ai_engine.set_api_key("")  # forget it for this process (a key in .env returns on restart)
    return {"status": "ok", **{k: v for k, v in ai_engine.ai_status().items() if k != "models"}}


@router.post("/model")
async def set_model(req: ModelRequest):
    try:
        ai_engine.set_model(req.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "model": ai_engine.get_model()}


@router.get("/actions")
async def list_actions():
    return {"actions": sorted(ai_engine.ACTION_PROMPTS)}


def _agent_kwargs(req: ChatRequest) -> tuple[str, dict]:
    _check_doc(req.doc_id)
    if req.action:
        try:
            msg = ai_engine.action_prompt(req.action, req.action_options)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if req.message:
            msg += "\n\nAdditional instructions: " + req.message
    elif req.message and req.message.strip():
        msg = req.message.strip()
    else:
        raise HTTPException(status_code=400, detail="message or action is required")
    if len(msg) > MAX_MESSAGE:
        raise HTTPException(status_code=413, detail="Message too long")
    if req.model and req.model not in ai_engine.MODEL_IDS:
        raise HTTPException(status_code=400, detail="Unknown model")
    selection = req.selection_text
    region = req.region.model_dump() if req.region else None
    if region and not selection:
        try:
            with fitz.open(str(ai_engine.doc_path(req.doc_id))) as doc:
                if 0 <= region["page"] < len(doc):
                    pg = doc[region["page"]]
                    clip = fitz.Rect(region["x"], region["y"], region["x"] + region["width"],
                                     region["y"] + region["height"]) * pg.derotation_matrix
                    selection = pg.get_text("text", clip=clip).strip() or None
        except Exception:
            selection = None
    refs = [r for r in req.reference_doc_ids if _UUID_RE.match(r)][:5]
    return msg, dict(current_page=req.current_page, selection_text=selection, region=region,
                     profile=req.profile, reference_doc_ids=refs,
                     allow_break_signature=req.allow_break_signature, model=req.model)


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    msg, kw = _agent_kwargs(req)

    async def gen():
        run_id = None
        try:
            async for ev in ai_engine.run_agent(req.doc_id, msg, **kw):
                if ev["type"] == "start":
                    run_id = ev["run_id"]
                yield sse(ev)
                if await request.is_disconnected():
                    if run_id:
                        ai_engine.cancel_run(run_id)
                    break
        finally:
            if run_id and await request.is_disconnected():
                ai_engine.cancel_run(run_id)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})


@router.post("/chat")
async def chat(req: ChatRequest):
    msg, kw = _agent_kwargs(req)
    return await ai_engine.run_agent_collect(req.doc_id, msg, **kw)


@router.post("/chat/stop")
async def stop(req: StopRequest):
    ai_engine.cancel_run(req.run_id)
    return {"status": "ok"}


@router.post("/redactions/apply")
async def apply_reviewed_redactions(req: ApplyReviewRequest):
    """Apply the redactions the user approved in the smart-redaction review list.

    Goes through the real /redact/apply route (which also purges undo history,
    by design: snapshots would retain the redacted content)."""
    _check_doc(req.doc_id)
    if not req.areas:
        raise HTTPException(status_code=400, detail="No areas selected")
    from httpx import ASGITransport, AsyncClient
    body: dict = {"areas": [a.model_dump() for a in req.areas]}
    if req.overlay_text:
        body["overlay_text"] = req.overlay_text
    headers = {ai_engine.SIGNED_HEADER: "1"} if req.allow_break_signature else {}
    async with AsyncClient(transport=ASGITransport(app=ai_engine._app()), base_url="http://ai.internal") as http:
        resp = await http.post(f"/api/pdf/{req.doc_id}/redact/apply", json=body, headers=headers)
    try:
        data = resp.json()
    except ValueError:
        data = {"detail": resp.text[:500]}
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=data.get("detail", data) if isinstance(data, dict) else data)
    return {"status": "ok", "applied": len(req.areas), "result": data}


@router.post("/locate")
async def locate(req: LocateRequest):
    _check_doc(req.doc_id)
    return ai_engine.locate_quote(req.doc_id, req.page, req.quote[:300])
