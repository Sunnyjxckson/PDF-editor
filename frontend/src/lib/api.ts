const API_BASE = process.env.NEXT_PUBLIC_API_URL || "";

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
  const res = await fetch(`${API_BASE}/api/pdf/upload`, {
    method: "POST",
    body: formData,
  });
  if (!res.ok) throw new Error("Upload failed");
  return res.json();
}

export async function getDocumentInfo(docId: string): Promise<DocumentInfo> {
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/info`);
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
  const res = await fetch(url);
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
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/text/edit`, {
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
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/text/add`, {
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
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/text/move`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Move/resize failed");
  return res.json();
}

// ─── Find & Replace ───────────────────────────────────────────────────────

export async function findText(docId: string, findStr: string, page?: number, matchCase = false): Promise<FindResult> {
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/find`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ find_text: findStr, page, match_case: matchCase }),
  });
  if (!res.ok) throw new Error("Find failed");
  return res.json();
}

export async function replaceText(docId: string, findStr: string, replaceStr: string, page?: number) {
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/replace`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ find_text: findStr, replace_text: replaceStr, page }),
  });
  if (!res.ok) throw new Error("Replace failed");
  return res.json();
}

// ─── Highlights & Drawing ─────────────────────────────────────────────────

export async function addHighlight(docId: string, page: number, rects: number[][], color?: number[], opacity?: number) {
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/highlight`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page, rects, color, opacity }),
  });
  if (!res.ok) throw new Error("Highlight failed");
  return res.json();
}

export async function addDrawing(docId: string, page: number, paths: { points: number[][]; color: number[]; width: number }[]) {
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/draw`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page, paths }),
  });
  if (!res.ok) throw new Error("Drawing failed");
  return res.json();
}

// ─── Annotations (Fabric.js JSON) ─────────────────────────────────────────

export async function getAnnotations(docId: string, pageNum: number): Promise<unknown[]> {
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/annotations/${pageNum}`);
  if (!res.ok) throw new Error("Failed to get annotations");
  return res.json();
}

export async function saveAnnotations(docId: string, pageNum: number, data: unknown[]) {
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/annotations/${pageNum}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error("Failed to save annotations");
  return res.json();
}

// ─── Page Operations ──────────────────────────────────────────────────────

export async function rotatePage(docId: string, pageNum: number, rotation: number) {
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/edit`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page: pageNum, type: "rotate", rotation }),
  });
  if (!res.ok) throw new Error("Rotate failed");
  return res.json();
}

export async function deletePage(docId: string, pageNum: number) {
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/edit`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page: pageNum, type: "delete" }),
  });
  if (!res.ok) throw new Error("Delete failed");
  return res.json();
}

export async function reorderPages(docId: string, pageOrder: number[]) {
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/reorder`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page_order: pageOrder }),
  });
  if (!res.ok) throw new Error("Reorder failed");
  return res.json();
}

export async function splitPDF(docId: string, pageRanges: number[][]) {
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/split`, {
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
  return `${API_BASE}/api/pdf/${docId}/export`;
}

// ─── AI Assist ────────────────────────────────────────────────────────────

export async function aiAssist(docId: string, data: {
  page: number;
  action: string;
  selected_text?: string;
  prompt?: string;
}): Promise<{ result: unknown; action: string }> {
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/ai/assist`, {
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
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/chat`, {
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
      const res = await fetch(`${API_BASE}/api/pdf/${docId}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      if (!res.ok) {
        // Fall back to non-streaming if server doesn't support it
        const errorText = await res.text();
        throw new Error(errorText || "Chat failed");
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
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/chat/history`);
  if (!res.ok) throw new Error("Failed to get chat history");
  return res.json();
}
