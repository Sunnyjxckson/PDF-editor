export const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// ─── Signed-document guard (shared fetch path) ──────────────────────────────
//
// The backend answers any edit of a digitally signed PDF with
// 409 {"code":"signed_document"}. apiFetch() asks the user (via the handler a
// <SignedDocGuard/> registers) and retries with X-Allow-Break-Signature: 1 on
// Continue. installSignedDocFetchGuard() routes the feature clients' plain
// fetch() calls through the same logic.

export const ALLOW_BREAK_SIGNATURE_HEADER = "X-Allow-Break-Signature";

export type SignedDocChoice = "continue" | "copy" | "cancel";

export interface SignedDocConflict {
  docId: string;
  detail: string;
  signers: string[];
}

type ConfirmHandler = (info: SignedDocConflict) => Promise<SignedDocChoice>;

let confirmHandler: ConfirmHandler | null = null;
let nativeFetch: typeof fetch | null = null;
// Once the user has accepted breaking a document's signature, later edits in
// this session go straight through (the signature is already invalid).
const consentedDocs = new Set<string>();
const pendingPrompts = new Map<string, Promise<SignedDocChoice>>();

const DOC_ID_IN_URL = /\/api\/pdf\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[/?#]|$)/i;

export function setSignedDocConfirmHandler(fn: ConfirmHandler | null): () => void {
  confirmHandler = fn;
  return () => {
    if (confirmHandler === fn) confirmHandler = null;
  };
}

export function hasSignatureConsent(docId: string): boolean {
  return consentedDocs.has(docId);
}

export function clearSignatureConsent(docId?: string): void {
  if (docId) consentedDocs.delete(docId);
  else consentedDocs.clear();
}

function baseFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const f = nativeFetch ?? globalThis.fetch;
  // Keep the exact call shape (no trailing undefined) callers/tests expect.
  return init === undefined ? f(input) : f(input, init);
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return (input as Request).url;
}

function methodOf(input: RequestInfo | URL, init?: RequestInit): string {
  if (init?.method) return init.method.toUpperCase();
  if (typeof Request !== "undefined" && input instanceof Request) return input.method.toUpperCase();
  return "GET";
}

function withAllowHeader(input: RequestInfo | URL, init?: RequestInit): RequestInit {
  const base = init?.headers ?? (typeof Request !== "undefined" && input instanceof Request ? input.headers : undefined);
  const headers = new Headers(base);
  headers.set(ALLOW_BREAK_SIGNATURE_HEADER, "1");
  return { ...init, headers };
}

async function readSignedConflict(res: Response, docId: string): Promise<SignedDocConflict | null> {
  if (!res || res.status !== 409 || typeof res.clone !== "function") return null;
  try {
    const body = await res.clone().json();
    if (!body || body.code !== "signed_document") return null;
    return {
      docId: typeof body.doc_id === "string" ? body.doc_id : docId,
      detail: typeof body.detail === "string" ? body.detail : "This PDF is digitally signed.",
      signers: Array.isArray(body.signers) ? body.signers.filter((x: unknown) => typeof x === "string") : [],
    };
  } catch {
    return null;
  }
}

/** Download the stored (still validly signed) PDF before it gets modified. */
export async function saveSignedCopy(docId: string): Promise<void> {
  const res = await baseFetch(`${API_BASE}/api/pdf/${docId}/export?flatten=false`);
  if (!res.ok) throw new Error("Could not save a copy of the signed PDF");
  const blob = await res.blob();
  if (typeof document === "undefined" || typeof URL.createObjectURL !== "function") return;
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = "signed-original.pdf";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 10_000);
}

function askOnce(info: SignedDocConflict): Promise<SignedDocChoice> {
  // Several requests can hit the 409 at once (e.g. a batch); ask only once.
  const existing = pendingPrompts.get(info.docId);
  if (existing) return existing;
  const p = (confirmHandler as ConfirmHandler)(info).finally(() => pendingPrompts.delete(info.docId));
  pendingPrompts.set(info.docId, p);
  return p;
}

/**
 * fetch() with the signed-document confirm-and-retry flow. Behaves exactly
 * like fetch for everything else; on Cancel the original 409 response is
 * returned so callers surface its `detail` like any other error.
 */
export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = urlOf(input);
  const m = url.startsWith(API_BASE) || url.startsWith("/api/") ? DOC_ID_IN_URL.exec(url) : null;
  const docId = m ? m[1].toLowerCase() : null;
  const method = methodOf(input, init);
  const mutating = method !== "GET" && method !== "HEAD" && method !== "OPTIONS";
  if (!docId || !mutating) return baseFetch(input, init);

  if (consentedDocs.has(docId)) return baseFetch(input, withAllowHeader(input, init));

  // A Request body can be read only once; keep a copy for the retry.
  const retryInput = typeof Request !== "undefined" && input instanceof Request ? input.clone() : input;
  const res = await baseFetch(input, init);
  const conflict = await readSignedConflict(res, docId);
  if (!conflict || !confirmHandler) return res;

  const choice = await askOnce(conflict);
  if (choice === "cancel") return res;
  if (choice === "copy") await saveSignedCopy(docId);
  consentedDocs.add(docId);
  return baseFetch(retryInput, withAllowHeader(retryInput, init));
}

/** Route every global fetch() (feature clients included) through apiFetch. */
export function installSignedDocFetchGuard(): () => void {
  if (nativeFetch) return () => {};
  nativeFetch = globalThis.fetch.bind(globalThis);
  const original = globalThis.fetch;
  globalThis.fetch = apiFetch as typeof fetch;
  return () => {
    if (globalThis.fetch === (apiFetch as typeof fetch)) globalThis.fetch = original;
    nativeFetch = null;
  };
}

export interface PDFDocument {
  id: string;
  filename: string;
  page_count: number;
  metadata: Record<string, string>;
}

export interface PageInfo {
  index: number;
  width: number;
  height: number;
  rotation: number;
}

export interface DocumentInfo {
  id: string;
  page_count: number;
  metadata: Record<string, string>;
  pages: PageInfo[];
}

export interface TextBlock {
  text: string;
  bbox: number[];
  font: string;
  size: number;
  color: number;
  flags: number;
  page: number;
}

export interface TextPageResult {
  page: number;
  width: number;
  height: number;
  blocks: TextBlock[];
}

export interface FindResult {
  matches: { page: number; bbox: number[] }[];
  count: number;
}

// ─── Upload & Info ─────────────────────────────────────────────────────────

export async function uploadPDF(file: File): Promise<PDFDocument> {
  const formData = new FormData();
  formData.append("file", file);
  const res = await apiFetch(`${API_BASE}/api/pdf/upload`, {
    method: "POST",
    body: formData,
  });
  if (!res.ok) throw new Error("Upload failed");
  return res.json();
}

export async function getDocumentInfo(docId: string): Promise<DocumentInfo> {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/info`);
  if (!res.ok) throw new Error("Failed to get document info");
  return res.json();
}

// ─── Rendering ─────────────────────────────────────────────────────────────

export function getPageUrl(docId: string, pageNum: number, dpi = 150): string {
  return `${API_BASE}/api/pdf/${docId}/page/${pageNum}?dpi=${dpi}`;
}

export function getThumbnailUrl(docId: string, pageNum: number): string {
  return `${API_BASE}/api/pdf/${docId}/thumbnail/${pageNum}`;
}

// ─── Text ──────────────────────────────────────────────────────────────────

export async function getTextBlocks(docId: string, pageNum?: number): Promise<TextPageResult[]> {
  const url = pageNum !== undefined
    ? `${API_BASE}/api/pdf/${docId}/text?page_num=${pageNum}`
    : `${API_BASE}/api/pdf/${docId}/text`;
  const res = await apiFetch(url);
  if (!res.ok) throw new Error("Failed to get text");
  return res.json();
}

export async function editText(docId: string, data: {
  page: number;
  bbox: number[];
  new_text: string;
  font_size?: number;
  color?: number[];
}) {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/text/edit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Text edit failed");
  return res.json();
}

export async function addText(docId: string, data: {
  page: number;
  x: number;
  y: number;
  text: string;
  font_size?: number;
  color?: number[];
}) {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/text/add`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Add text failed");
  return res.json();
}

// ─── Move & Resize ────────────────────────────────────────────────────────

export async function moveResizeContent(docId: string, data: {
  page: number;
  old_bbox: number[];
  new_bbox: number[];
}) {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/text/move`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Move/resize failed");
  return res.json();
}

// ─── Find & Replace ───────────────────────────────────────────────────────

export async function findText(docId: string, findStr: string, page?: number, matchCase = false): Promise<FindResult> {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/find`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ find_text: findStr, page, match_case: matchCase }),
  });
  if (!res.ok) throw new Error("Find failed");
  return res.json();
}

export async function replaceText(docId: string, findStr: string, replaceStr: string, page?: number) {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/replace`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ find_text: findStr, replace_text: replaceStr, page }),
  });
  if (!res.ok) throw new Error("Replace failed");
  return res.json();
}

// ─── Highlights & Drawing ─────────────────────────────────────────────────

export async function addHighlight(docId: string, page: number, rects: number[][], color?: number[], opacity?: number) {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/highlight`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page, rects, color, opacity }),
  });
  if (!res.ok) throw new Error("Highlight failed");
  return res.json();
}

export async function addDrawing(docId: string, page: number, paths: { points: number[][]; color: number[]; width: number }[]) {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/draw`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page, paths }),
  });
  if (!res.ok) throw new Error("Drawing failed");
  return res.json();
}

// ─── Annotations (Fabric.js JSON) ─────────────────────────────────────────

export async function getAnnotations(docId: string, pageNum: number): Promise<unknown[]> {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/annotations/${pageNum}`);
  if (!res.ok) throw new Error("Failed to get annotations");
  return res.json();
}

export async function saveAnnotations(docId: string, pageNum: number, data: unknown[]) {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/annotations/${pageNum}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to save annotations");
  return res.json();
}

// ─── Page Operations ──────────────────────────────────────────────────────

export async function rotatePage(docId: string, pageNum: number, rotation: number) {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/edit`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page: pageNum, type: "rotate", rotation }),
  });
  if (!res.ok) throw new Error("Rotate failed");
  return res.json();
}

export async function deletePage(docId: string, pageNum: number) {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/edit`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page: pageNum, type: "delete" }),
  });
  if (!res.ok) throw new Error("Delete failed");
  return res.json();
}

export async function reorderPages(docId: string, pageOrder: number[]) {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/reorder`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page_order: pageOrder }),
  });
  if (!res.ok) throw new Error("Reorder failed");
  return res.json();
}

export async function splitPDF(docId: string, pageRanges: number[][]) {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/split`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page_ranges: pageRanges }),
  });
  if (!res.ok) throw new Error("Split failed");
  return res.json();
}

export function getExportUrl(docId: string): string {
  return `${API_BASE}/api/pdf/${docId}/export`;
}

export function getPdfFileUrl(docId: string): string {
  // The working file as-is (no re-save), so pdf.js shows exactly what is stored.
  return `${API_BASE}/api/pdf/${docId}/export?flatten=false`;
}

// ─── AI Assist ────────────────────────────────────────────────────────────

export async function aiAssist(docId: string, data: {
  page: number;
  action: string;
  selected_text?: string;
  prompt?: string;
}): Promise<{ result: unknown; action: string }> {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/ai/assist`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("AI assist failed");
  return res.json();
}

// ─── Chat ─────────────────────────────────────────────────────────────────

export interface ChatResponse {
  response: string;
  changed: boolean;
  intent: Record<string, unknown>;
  new_page_count: number | null;
}

export async function sendChatMessage(docId: string, message: string, currentPage: number): Promise<ChatResponse> {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, current_page: currentPage }),
  });
  if (!res.ok) throw new Error("Chat failed");
  return res.json();
}

// ─── Streaming Chat (SSE) ────────────────────────────────────────────────────

export interface StreamChatCallbacks {
  onToken: (token: string) => void;
  onDone: (data: ChatResponse) => void;
  onError: (error: Error) => void;
}

export interface RegionRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export function streamChatMessage(
  docId: string,
  message: string,
  currentPage: number,
  callbacks: StreamChatCallbacks,
  region?: { page: number; rect: RegionRect },
): AbortController {
  const controller = new AbortController();

  (async () => {
    try {
      const body: Record<string, unknown> = { message, current_page: currentPage, stream: true };
      if (region) {
        body.region = {
          page: region.page,
          x: Math.round(region.rect.x),
          y: Math.round(region.rect.y),
          width: Math.round(region.rect.width),
          height: Math.round(region.rect.height),
        };
      }
      const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      if (!res.ok) {
        // Fall back to non-streaming if server doesn't support it
        const errorText = await res.text();
        let message = errorText;
        try {
          const parsed = JSON.parse(errorText);
          if (parsed && typeof parsed.detail === "string") message = parsed.detail;
        } catch {
          // not JSON
        }
        throw new Error(message || "Chat failed");
      }

      const contentType = res.headers.get("content-type") || "";

      // If the server returned JSON instead of SSE, handle as non-streaming
      if (contentType.includes("application/json")) {
        const data: ChatResponse = await res.json();
        callbacks.onToken(data.response);
        callbacks.onDone(data);
        return;
      }

      // Parse SSE stream
      const reader = res.body?.getReader();
      if (!reader) throw new Error("No response body");

      const decoder = new TextDecoder();
      let buffer = "";
      let finalData: ChatResponse | null = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || ""; // Keep incomplete line in buffer

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            const data = line.slice(6).trim();
            if (data === "[DONE]") continue;

            try {
              const parsed = JSON.parse(data);
              if (parsed.token) {
                callbacks.onToken(parsed.token);
              }
              if (parsed.done) {
                finalData = {
                  response: parsed.full_response || "",
                  changed: parsed.changed || false,
                  intent: parsed.intent || {},
                  new_page_count: parsed.new_page_count ?? null,
                };
              }
            } catch {
              // If it's not JSON, treat as a raw token
              if (data) callbacks.onToken(data);
            }
          }
        }
      }

      if (finalData) {
        callbacks.onDone(finalData);
      }
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        callbacks.onError(err as Error);
      }
    }
  })();

  return controller;
}

export async function getChatHistory(docId: string): Promise<{ role: string; content: string }[]> {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/chat/history`);
  if (!res.ok) throw new Error("Failed to get chat history");
  return res.json();
}

// ─── History (undo / redo) ──────────────────────────────────────────────────

export interface HistoryEntry {
  index: number;
  operation: string;
  timestamp: string;
  is_current: boolean;
}

export interface HistoryState {
  versions: HistoryEntry[];
  current: number;
  can_undo: boolean;
  can_redo: boolean;
}

async function errorDetail(res: Response, fallback: string): Promise<Error> {
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") return new Error(body.detail);
  } catch {
    // not JSON
  }
  return new Error(fallback);
}

async function postJson<T>(path: string, body: unknown, fallback: string): Promise<T> {
  const res = await apiFetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) throw await errorDetail(res, fallback);
  return res.json();
}

export async function getHistory(docId: string): Promise<HistoryState> {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/history`);
  if (!res.ok) throw await errorDetail(res, "Could not load history");
  return res.json();
}

export function undo(docId: string) {
  return postJson<{ status: string; undone_operation: string; can_undo: boolean; can_redo: boolean }>(
    `/api/pdf/${docId}/undo`, {}, "Nothing to undo");
}

export function redo(docId: string) {
  return postJson<{ status: string; restored_operation: string; can_undo: boolean; can_redo: boolean }>(
    `/api/pdf/${docId}/redo`, {}, "Nothing to redo");
}

// ─── Document tools (advanced_ops) ───────────────────────────────────────────

export type PageSelection = "all" | number[];

export function addWatermark(docId: string, opts: {
  text: string; font_size?: number; color?: number[]; opacity?: number; rotation?: number; pages?: PageSelection;
}) {
  return postJson<{ status: string; pages_watermarked: number }>(`/api/pdf/${docId}/watermark`, opts, "Watermark failed");
}

export function addStamp(docId: string, opts: {
  text: string; position?: string; font_size?: number; color?: number[]; pages?: PageSelection; margin?: number;
}) {
  return postJson<{ status: string; pages_stamped: number }>(`/api/pdf/${docId}/stamp`, opts, "Stamp failed");
}

export function convertToPdfA(docId: string) {
  return postJson<{ status: string; note?: string }>(`/api/pdf/${docId}/convert-pdfa`, {}, "PDF/A conversion failed");
}

export function flattenDocument(docId: string) {
  return postJson<{ status: string; flattened: number }>(`/api/pdf/${docId}/flatten`, {}, "Flatten failed");
}

export interface CompareResult {
  doc1_pages: number;
  doc2_pages: number;
  pages_added: number;
  pages_removed: number;
  diffs: { page: number; lines_added: number; lines_removed: number; diff: string[] }[];
}

export function compareDocuments(docId1: string, docId2: string) {
  return postJson<CompareResult>(`/api/pdf/compare`, { doc_id_1: docId1, doc_id_2: docId2 }, "Compare failed");
}

export async function deleteDocument(docId: string) {
  await apiFetch(`${API_BASE}/api/pdf/${docId}`, { method: "DELETE" });
}
