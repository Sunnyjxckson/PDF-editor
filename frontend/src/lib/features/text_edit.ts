// Typed client for backend/features/text_edit.py (true in-place text editing).
//
// COORDINATES: every bbox returned / sent here is in PDF points, top-left
// origin, in the *visible* (rotation-applied) page space — the same space as
// the rendered page image. Convert to rendered pixels with `ptRectToPx`
// (multiply by pixels-per-point) and back with `pxDeltaToPt`.

import { API_BASE, apiFetch } from "../api";

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
  /** Horizontal scaling (PDF Tz / 100); 1 = none. */
  hscale?: number;
}

/**
 * The text's own box in visible-space points: top-left corner (x, y), size
 * along (w) / across (h) the text direction, clockwise angle in degrees.
 * Render with `transform: rotate(angle deg)` and `transform-origin: 0 0`.
 */
export interface RotBox {
  x: number;
  y: number;
  w: number;
  h: number;
  angle: number;
}

export interface EditableSpan extends TextStyle {
  id: string;
  bbox: number[];
  text: string;
  box?: RotBox;
}

export interface EditableLine {
  id: string;
  bbox: number[];
  text: string;
  style: TextStyle;
  spans: EditableSpan[];
  editable?: boolean;
  angle?: number;
  box?: RotBox;
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
  box?: RotBox;
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

/**
 * What to do when an edited paragraph does not fit. "auto" (server default):
 * shrink to 85%, then push the following text down; if there is no room the
 * server answers 422 (TextOverflowError). "shrink": shrink until it fits.
 * "allow": keep the size and overlap the text below.
 */
export type OverflowMode = "auto" | "shrink" | "allow";

export interface EditPayload {
  page: number;
  target: TextTarget;
  text?: string;
  style?: StyleOverride;
  align?: TextAlign;
  overflow?: OverflowMode;
}

export interface EditResult {
  status: "ok";
  kind: TargetKind;
  font?: string;
  fonts?: string[];
  font_size?: number;
  requested_size?: number;
  lines?: number;
  /** The text extends past its original space (pushed text down, or overlaps). */
  overflow?: boolean;
  /** It covers other text (only with overflow: "allow"). */
  overlap?: boolean;
  /** Number of following blocks moved down to make room, and by how much (pt). */
  pushed?: number;
  push_distance?: number;
  scale?: number;
  fallback?: string | null;
  align?: TextAlign;
  changed?: boolean;
}

/** 422 body of a paragraph edit that does not fit (nothing was written). */
export interface OverflowInfo {
  code: "overflow";
  message?: string;
  needed_height: number;
  available_height: number;
  requested_size: number;
  /** Size that would fit if shrunk (null: not even at the minimum size). */
  fit_size: number | null;
  fit_scale?: number | null;
}

export class TextOverflowError extends Error {
  readonly info: OverflowInfo;
  constructor(info: OverflowInfo) {
    super(info.message || "The edited text does not fit");
    this.name = "TextOverflowError";
    this.info = info;
  }
}

export function isOverflowInfo(d: unknown): d is OverflowInfo {
  return !!d && typeof d === "object" && (d as { code?: unknown }).code === "overflow";
}

async function errorFrom(res: Response, fallback: string): Promise<Error> {
  try {
    const body = await res.json();
    if (body && isOverflowInfo(body.detail)) return new TextOverflowError(body.detail);
    if (body && typeof body.detail === "string") return new Error(body.detail);
  } catch {
    /* not JSON */
  }
  return new Error(fallback);
}

async function errorDetail(res: Response, fallback: string): Promise<string> {
  return (await errorFrom(res, fallback)).message;
}

async function postJSON<T>(path: string, body: unknown, fallback: string): Promise<T> {
  const res = await apiFetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await errorFrom(res, fallback);
  return res.json();
}

export async function getEditableText(docId: string, page: number): Promise<EditablePage> {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/text-edit/page/${page}`);
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

/** True when an angle (degrees) is, within 0.05, an exact multiple of 360. */
export function isUpright(angle: number | null | undefined): boolean {
  if (angle == null || !Number.isFinite(angle)) return true;
  const a = ((angle % 360) + 360) % 360;
  return a < 0.05 || a > 359.95;
}

/**
 * CSS geometry for a (possibly rotated) text box at `pxPerPt`. Positioned by
 * its top-left corner and rotated around it, so the box lies exactly over the
 * text at any angle (90/180/270 included).
 */
export function rotBoxToCss(box: RotBox, pxPerPt: number, pad = 0) {
  const rad = (box.angle * Math.PI) / 180;
  const cos = Math.cos(rad), sin = Math.sin(rad);
  // move the corner out by `pad` along both box axes so padding grows evenly
  const left = box.x * pxPerPt - pad * cos + pad * sin;
  const top = box.y * pxPerPt - pad * sin - pad * cos;
  return {
    left,
    top,
    width: box.w * pxPerPt + 2 * pad,
    height: box.h * pxPerPt + 2 * pad,
    transform: isUpright(box.angle) ? undefined : `rotate(${Math.round(box.angle * 1000) / 1000}deg)`,
    transformOrigin: "0 0",
  };
}

/** Labels for the overflow choices offered after a 422. */
export function overflowChoices(info: OverflowInfo) {
  const fit = info.fit_size;
  return {
    shrink: fit != null ? `Shrink to fit (${Math.round(fit * 10) / 10} pt)` : null,
    allow: "Allow overlap",
    cancel: "Cancel",
    summary: `Needs ${Math.round(info.needed_height)} pt, only ${Math.round(info.available_height)} pt available`,
  };
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
