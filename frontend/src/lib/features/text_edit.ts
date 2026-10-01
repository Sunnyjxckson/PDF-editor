// Typed client for backend/features/text_edit.py (true in-place text editing).
//
// COORDINATES: every bbox returned / sent here is in PDF points, top-left
// origin, in the *visible* (rotation-applied) page space — the same space as
// the rendered page image. Convert to rendered pixels with `ptRectToPx`
// (multiply by pixels-per-point) and back with `pxDeltaToPt`.

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export type TextFamily = "sans" | "serif" | "mono";
export type FamilyChoice = "original" | TextFamily;
export type TextAlign = "left" | "center" | "right" | "justify";
export type TargetKind = "block" | "line" | "span";

export interface TextStyle {
  font: string;
  family: TextFamily;
  size: number;
  color: string; // "#rrggbb"
  bold: boolean;
  italic: boolean;
  flags: number;
}

export interface EditableSpan extends TextStyle {
  id: string;
  bbox: number[];
  text: string;
}

export interface EditableLine {
  id: string;
  bbox: number[];
  text: string;
  style: TextStyle;
  spans: EditableSpan[];
}

export interface EditableBlock {
  id: string;
  bbox: number[];
  text: string;
  paragraph_text: string;
  style: TextStyle;
  mixed_styles: boolean;
  editable: boolean;
  reason?: string;
  align: TextAlign;
  line_height: number;
  angle: number | null;
  lines: EditableLine[];
}

export interface EditablePage {
  page: number;
  width: number;
  height: number;
  rotation: number;
  blocks: EditableBlock[];
  fonts: { xref: number; name: string; type: string; embedded: boolean; subset: boolean }[];
}

export interface TextTarget {
  kind: TargetKind;
  id: string;
  bbox: number[];
}

export interface StyleOverride {
  family?: FamilyChoice;
  size?: number;
  color?: string;
  bold?: boolean;
  italic?: boolean;
}

export interface EditPayload {
  page: number;
  target: TextTarget;
  text?: string;
  style?: StyleOverride;
  align?: TextAlign;
}

export interface EditResult {
  status: "ok";
  kind: TargetKind;
  font?: string;
  fonts?: string[];
  font_size?: number;
  requested_size?: number;
  lines?: number;
  overflow?: boolean;
  align?: TextAlign;
  changed?: boolean;
}

async function errorDetail(res: Response, fallback: string): Promise<string> {
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") return body.detail;
  } catch {
    /* not JSON */
  }
  return fallback;
}

async function postJSON<T>(path: string, body: unknown, fallback: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await errorDetail(res, fallback));
  return res.json();
}

export async function getEditableText(docId: string, page: number): Promise<EditablePage> {
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/text-edit/page/${page}`);
  if (!res.ok) throw new Error(await errorDetail(res, "Failed to load page text"));
  return res.json();
}

export function editTextInPlace(docId: string, payload: EditPayload): Promise<EditResult> {
  return postJSON(`/api/pdf/${docId}/text-edit/edit`, payload, "Text edit failed");
}

export function moveText(
  docId: string,
  data: { page: number; target: TextTarget; dx: number; dy: number },
): Promise<{ status: "ok"; moved: number }> {
  return postJSON(`/api/pdf/${docId}/text-edit/move`, data, "Move failed");
}

export function deleteText(
  docId: string,
  data: { page: number; target: TextTarget },
): Promise<{ status: "ok" }> {
  return postJSON(`/api/pdf/${docId}/text-edit/delete`, data, "Delete failed");
}

// ─── Pure helpers (unit-tested) ─────────────────────────────────────────────

/** PDF-point bbox -> rendered-pixel box at `pxPerPt` pixels per point. */
export function ptRectToPx(bbox: number[], pxPerPt: number) {
  const [x0, y0, x1, y1] = bbox;
  return { left: x0 * pxPerPt, top: y0 * pxPerPt, width: (x1 - x0) * pxPerPt, height: (y1 - y0) * pxPerPt };
}

/** Screen-pixel drag delta -> PDF points. `screenPxPerPt` must include any CSS zoom. */
export function pxDeltaToPt(dxPx: number, dyPx: number, screenPxPerPt: number) {
  if (!(screenPxPerPt > 0)) return { dx: 0, dy: 0 };
  return { dx: dxPx / screenPxPerPt, dy: dyPx / screenPxPerPt };
}

export function cssFontFamily(family: FamilyChoice, original: TextFamily): string {
  const f = family === "original" ? original : family;
  if (f === "serif") return '"Times New Roman", Times, serif';
  if (f === "mono") return '"Courier New", Courier, monospace';
  return "Helvetica, Arial, sans-serif";
}

export interface Draft {
  text: string;
  family: FamilyChoice;
  size: number;
  color: string;
  bold: boolean;
  italic: boolean;
  align: TextAlign;
}

export function initialDraft(
  kind: TargetKind,
  item: { text: string; style: TextStyle; paragraph_text?: string; align?: TextAlign },
): Draft {
  return {
    text: kind === "block" ? item.paragraph_text ?? item.text : item.text,
    family: "original",
    size: item.style.size,
    color: item.style.color,
    bold: item.style.bold,
    italic: item.style.italic,
    align: item.align ?? "left",
  };
}

/**
 * Build the minimal edit request: only fields the user actually changed are
 * sent, so untouched mixed inline styles are preserved by the backend.
 * Returns null when nothing changed (commit becomes a cancel).
 */
export function buildEditPayload(
  page: number,
  target: TextTarget,
  original: Draft,
  draft: Draft,
): EditPayload | null {
  const payload: EditPayload = { page, target };
  const norm = (s: string) => s.replace(/\r\n?/g, "\n");
  if (norm(draft.text) !== norm(original.text)) payload.text = norm(draft.text);
  const style: StyleOverride = {};
  if (draft.family !== original.family) style.family = draft.family;
  if (Math.abs(draft.size - original.size) > 0.01 && draft.size > 0) style.size = Math.round(draft.size * 100) / 100;
  if (draft.color.toLowerCase() !== original.color.toLowerCase()) style.color = draft.color;
  if (draft.bold !== original.bold) style.bold = draft.bold;
  if (draft.italic !== original.italic) style.italic = draft.italic;
  if (Object.keys(style).length) payload.style = style;
  if (target.kind === "block" && draft.align !== original.align) payload.align = draft.align;
  if (payload.text === undefined && !payload.style && !payload.align) return null;
  return payload;
}
