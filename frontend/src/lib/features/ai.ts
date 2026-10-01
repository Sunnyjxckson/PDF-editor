/**
 * AI assistant client + shared UI state.
 *
 * Backend: backend/features/ai.py (all routes under /api/ai). The chat streams
 * Server-Sent Events, one JSON object per `data:` line, terminated by
 * `data: [DONE]`. Event types are listed in AIEvent below.
 *
 * Pages: citations and review items use 1-based `page`; redaction areas sent
 * back to the server use the 0-based `page_index` the server gave us.
 * Rects are [x0,y0,x1,y1] PDF points in the visible page space (top-left).
 */
import { create } from "zustand";
import { API_BASE, hasSignatureConsent } from "@/lib/api";

export type PdfRect = [number, number, number, number];

export interface AIModel {
  id: string;
  label: string;
  note?: string;
}

export interface AIConfig {
  sdk_installed: boolean;
  api_key_set: boolean;
  key_source: "runtime" | "env" | null;
  ai_available: boolean;
  model: string;
  models: AIModel[];
}

export interface AICitation {
  page: number;
  quote: string | null;
  rects: PdfRect[];
  valid: boolean;
  marker: string;
}

export interface AIChange {
  tool: string;
  summary: string;
  undoable: boolean;
}

export interface RedactionReviewItem {
  id: string;
  page: number;
  page_index: number;
  text: string;
  rects: PdfRect[];
  category: string;
  reason: string;
  context?: string;
}

export interface AIDone {
  type: "done";
  run_id?: string;
  response: string;
  changed: boolean;
  changes: AIChange[];
  undo_steps: number;
  citations: AICitation[];
  page_count_changed: boolean;
  new_page_count: number | null;
  stopped: boolean;
}

export type AIEvent =
  | { type: "start"; run_id: string; model: string }
  | { type: "text"; delta: string }
  | { type: "tool_start"; id: string; name: string; input: unknown; label: string }
  | { type: "tool_result"; id: string; name: string; ok: boolean; summary: string; changed: boolean; code?: string }
  | { type: "redaction_review"; items: RedactionReviewItem[]; count: number }
  | { type: "file"; filename: string; mime: string; content: string }
  | { type: "download"; format: string; url: string; label: string }
  | { type: "needs_confirmation"; reason: string; message: string }
  | { type: "setup_required"; reason: string; message: string }
  | { type: "error"; code: string; message: string }
  | AIDone;

export type AIActionId =
  | "summarize_short" | "summarize_detailed" | "summarize_bullets"
  | "explain_selection" | "rewrite_selection" | "shorten_selection" | "fix_grammar_selection"
  | "fix_grammar_page" | "translate_document" | "smart_redact"
  | "autofill_profile" | "autofill_reference" | "generate_fields"
  | "compare_reference" | "extract_tables_csv" | "extract_data_json";

export interface AIRegion {
  page: number;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface AIChatRequest {
  doc_id: string;
  message?: string;
  action?: AIActionId;
  action_options?: Record<string, unknown>;
  current_page: number;
  selection_text?: string | null;
  region?: AIRegion | null;
  profile?: Record<string, string> | null;
  reference_doc_ids?: string[];
  allow_break_signature?: boolean;
  model?: string;
}

// ─── REST helpers ────────────────────────────────────────────────────────────

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") msg = body.detail;
    } catch {
      /* not json */
    }
    throw new Error(msg);
  }
  return res.json() as Promise<T>;
}

export async function getAIConfig(): Promise<AIConfig> {
  return json<AIConfig>(await fetch(`${API_BASE}/api/ai/config`));
}

/** Sends the key once; the server never returns it. */
export async function setAIKey(apiKey: string, persist = false): Promise<{ api_key_set: boolean; ai_available: boolean }> {
  return json(await fetch(`${API_BASE}/api/ai/key`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: apiKey.trim(), persist }),
  }));
}

export async function setAIModel(model: string): Promise<{ model: string }> {
  return json(await fetch(`${API_BASE}/api/ai/model`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  }));
}

export async function stopAIRun(runId: string): Promise<void> {
  try {
    await fetch(`${API_BASE}/api/ai/chat/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: runId }),
    });
  } catch {
    /* best effort; the aborted stream also cancels the run */
  }
}

export async function applyReviewedRedactions(
  docId: string, items: RedactionReviewItem[], allowBreakSignature = false,
): Promise<{ applied: number }> {
  const areas = items.flatMap((it) => it.rects.map((rect) => ({ page: it.page_index, rect })));
  return json(await fetch(`${API_BASE}/api/ai/redactions/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ doc_id: docId, areas, allow_break_signature: allowBreakSignature }),
  }));
}

export async function locateQuote(docId: string, page: number, quote: string): Promise<{ rects: PdfRect[]; valid: boolean }> {
  return json(await fetch(`${API_BASE}/api/ai/locate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ doc_id: docId, page, quote }),
  }));
}

/** Upload a second PDF (form source / comparison) and return its doc id. */
export async function uploadReferencePdf(file: File): Promise<{ id: string; filename: string }> {
  const fd = new FormData();
  fd.append("file", file);
  const data = await json<{ id: string; filename?: string }>(
    await fetch(`${API_BASE}/api/pdf/upload`, { method: "POST", body: fd }),
  );
  return { id: data.id, filename: data.filename ?? file.name };
}

export async function undoSteps(docId: string, steps: number): Promise<number> {
  let done = 0;
  for (let i = 0; i < steps; i++) {
    const res = await fetch(`${API_BASE}/api/pdf/${docId}/undo`, { method: "POST" });
    if (!res.ok) break;
    done++;
  }
  return done;
}

// ─── SSE streaming ───────────────────────────────────────────────────────────

/** Split an SSE buffer into complete events; returns [events, rest]. */
export function parseSSEChunk(buffer: string): [Array<AIEvent | "[DONE]">, string] {
  const out: Array<AIEvent | "[DONE]"> = [];
  const parts = buffer.split("\n\n");
  const rest = parts.pop() ?? "";
  for (const part of parts) {
    const line = part.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trimStart()).join("\n");
    if (!line) continue;
    if (line === "[DONE]") {
      out.push("[DONE]");
      continue;
    }
    try {
      out.push(JSON.parse(line) as AIEvent);
    } catch {
      /* ignore malformed */
    }
  }
  return [out, rest];
}

export interface StreamHandle {
  abort: () => void;
  done: Promise<void>;
}

/**
 * POST /api/ai/chat/stream and call onEvent for every event. Stop with
 * handle.abort() (also tells the server to cancel the run).
 */
export function streamAIChat(req: AIChatRequest, onEvent: (ev: AIEvent) => void): StreamHandle {
  const controller = new AbortController();
  let runId: string | null = null;
  const body: AIChatRequest = {
    ...req,
    allow_break_signature: req.allow_break_signature || hasSignatureConsent(req.doc_id),
  };
  const done = (async () => {
    let gotDone = false;
    try {
      const res = await fetch(`${API_BASE}/api/ai/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!res.ok || !res.body) {
        let msg = `AI request failed (${res.status})`;
        try {
          const b = await res.json();
          if (typeof b?.detail === "string") msg = b.detail;
        } catch {
          /* ignore */
        }
        onEvent({ type: "error", code: "http", message: msg });
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done: end, value } = await reader.read();
        if (end) break;
        buffer += decoder.decode(value, { stream: true });
        const [events, rest] = parseSSEChunk(buffer);
        buffer = rest;
        for (const ev of events) {
          if (ev === "[DONE]") continue;
          if (ev.type === "start") runId = ev.run_id;
          if (ev.type === "done") gotDone = true;
          onEvent(ev);
        }
      }
      if (buffer.trim()) {
        const [events] = parseSSEChunk(buffer + "\n\n");
        for (const ev of events) if (ev !== "[DONE]") { if (ev.type === "done") gotDone = true; onEvent(ev); }
      }
      if (!gotDone) onEvent({ type: "error", code: "incomplete", message: "The response ended unexpectedly." });
    } catch (e) {
      if ((e as Error)?.name === "AbortError") {
        onEvent({
          type: "done", response: "", changed: false, changes: [], undo_steps: 0, citations: [],
          page_count_changed: false, new_page_count: null, stopped: true,
        });
      } else {
        onEvent({ type: "error", code: "network", message: "Could not reach the server." });
      }
    }
  })();
  return {
    abort: () => {
      if (runId) void stopAIRun(runId);
      controller.abort();
    },
    done,
  };
}

// ─── Citations / message rendering helpers ───────────────────────────────────

export const CITATION_RE = /\[p\.\s*(\d{1,5})(?:\s*[:,]?\s*["“]([^"”\]]{2,300})["”])?\s*\]/g;

export type MessageSegment =
  | { kind: "text"; text: string }
  | { kind: "citation"; page: number; quote: string | null; marker: string };

/** Split assistant text into plain text and [p. N "quote"] citation segments. */
export function splitCitations(text: string): MessageSegment[] {
  const out: MessageSegment[] = [];
  let last = 0;
  for (const m of text.matchAll(CITATION_RE)) {
    const i = m.index ?? 0;
    if (i > last) out.push({ kind: "text", text: text.slice(last, i) });
    out.push({ kind: "citation", page: Number(m[1]), quote: m[2]?.trim() || null, marker: m[0] });
    last = i + m[0].length;
  }
  if (last < text.length) out.push({ kind: "text", text: text.slice(last) });
  return out;
}

export function rectToPercentStyle(rect: PdfRect, pageWidth: number, pageHeight: number) {
  const [x0, y0, x1, y1] = rect;
  return {
    left: `${(x0 / pageWidth) * 100}%`,
    top: `${(y0 / pageHeight) * 100}%`,
    width: `${((x1 - x0) / pageWidth) * 100}%`,
    height: `${((y1 - y0) / pageHeight) * 100}%`,
  };
}

// ─── Personal profile (browser-only) ─────────────────────────────────────────

const PROFILE_KEY = "pdf-editor.ai-profile.v1";

export const PROFILE_FIELDS: Array<{ key: string; label: string }> = [
  { key: "full_name", label: "Full name" },
  { key: "email", label: "Email" },
  { key: "phone", label: "Phone" },
  { key: "date_of_birth", label: "Date of birth" },
  { key: "street", label: "Street address" },
  { key: "city", label: "City" },
  { key: "state", label: "State / region" },
  { key: "postal_code", label: "Postal code" },
  { key: "country", label: "Country" },
  { key: "company", label: "Company" },
  { key: "job_title", label: "Job title" },
];

/** The profile lives only in this browser and is sent only with a request that needs it. */
export function loadProfile(): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(PROFILE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

export function saveProfile(p: Record<string, string>): void {
  const clean = Object.fromEntries(Object.entries(p).filter(([, v]) => typeof v === "string" && v.trim()));
  try {
    window.localStorage.setItem(PROFILE_KEY, JSON.stringify(clean));
  } catch {
    /* storage unavailable */
  }
}

export function clearProfile(): void {
  try {
    window.localStorage.removeItem(PROFILE_KEY);
  } catch {
    /* ignore */
  }
}

// ─── Shared state ────────────────────────────────────────────────────────────

export interface AIQueuedRequest {
  action?: AIActionId;
  message?: string;
  options?: Record<string, unknown>;
  selectionText?: string | null;
  includeProfile?: boolean;
  label: string;
}

export interface CitationHighlight {
  page: number; // 1-based
  rects: PdfRect[];
  quote: string | null;
}

interface AIState {
  /** A one-click action from AIPanel waiting for ChatPanel to send it. */
  queued: AIQueuedRequest | null;
  highlight: CitationHighlight | null;
  references: Array<{ id: string; filename: string }>;
  queue: (r: AIQueuedRequest) => void;
  takeQueued: () => AIQueuedRequest | null;
  setHighlight: (h: CitationHighlight | null) => void;
  addReference: (r: { id: string; filename: string }) => void;
  removeReference: (id: string) => void;
}

export const useAIStore = create<AIState>((set, get) => ({
  queued: null,
  highlight: null,
  references: [],
  queue: (r) => set({ queued: r }),
  takeQueued: () => {
    const q = get().queued;
    if (q) set({ queued: null });
    return q;
  },
  setHighlight: (h) => set({ highlight: h }),
  addReference: (r) => set((s) => ({ references: [...s.references.filter((x) => x.id !== r.id), r] })),
  removeReference: (id) => set((s) => ({ references: s.references.filter((x) => x.id !== id) })),
}));

export const ACTION_LABELS: Record<AIActionId, string> = {
  summarize_short: "Short summary",
  summarize_detailed: "Detailed summary",
  summarize_bullets: "Bullet summary",
  explain_selection: "Explain selection",
  rewrite_selection: "Rewrite selection",
  shorten_selection: "Shorten selection",
  fix_grammar_selection: "Fix grammar (selection)",
  fix_grammar_page: "Fix grammar (page)",
  translate_document: "Translate document",
  smart_redact: "Smart redaction",
  autofill_profile: "Fill form from my profile",
  autofill_reference: "Fill form from another PDF",
  generate_fields: "Make form fillable",
  compare_reference: "Compare with another PDF",
  extract_tables_csv: "Tables to CSV",
  extract_data_json: "Data to JSON",
};

export const SELECTION_ACTIONS: AIActionId[] = [
  "explain_selection", "rewrite_selection", "shorten_selection", "fix_grammar_selection",
];
export const REFERENCE_ACTIONS: AIActionId[] = ["autofill_reference", "compare_reference"];
