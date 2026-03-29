const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

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

export async function getChatHistory(docId: string): Promise<{ role: string; content: string }[]> {
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/chat/history`);
  if (!res.ok) throw new Error("Failed to get chat history");
  return res.json();
}
