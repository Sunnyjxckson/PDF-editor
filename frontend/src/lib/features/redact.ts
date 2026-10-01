/**
 * Redaction & security API client + shared UI state.
 *
 * COORDINATES: every rect is [x0, y0, x1, y1] in PDF points, top-left origin,
 * in the *visible* page space (the rendered page, rotation already applied —
 * the same width/height that GET /api/pdf/{id}/info returns per page).
 * Convert screen → points with `clientToPdfPoint`, which works off the
 * overlay's getBoundingClientRect(), so CSS zoom/transform is accounted for.
 */
import { create } from "zustand";

import { API_BASE, apiFetch } from "../api";

export type PdfRect = [number, number, number, number];

export interface RedactArea {
  page: number;
  rect: PdfRect;
}

export interface RedactMark {
  page: number;
  xref: number;
  rect: PdfRect;
  label: string;
  overlay_text: string;
  fill_color: string | null;
}

export interface RedactWord {
  text: string;
  rect: PdfRect;
  block: number;
  line: number;
  word: number;
}

export interface RedactMatch {
  id: string;
  page: number;
  text: string;
  kind: string;
  rects: PdfRect[];
  context: string;
}

export interface MarkStyle {
  fill_color: string; // "#rrggbb"
  overlay_text: string; // "" = none
  overlay_text_color: string;
  overlay_font_size: number;
}

export interface ApplyOptions {
  pages?: number[];
  images?: "pixels" | "remove" | "none";
  graphics?: "covered" | "touched" | "none";
}

export interface ApplyResult {
  status: string;
  applied: number;
  pages_affected: number[];
  removed_chars: number;
  verified: boolean;
  leftover_chars: number;
}

export interface SearchOptions {
  query?: string;
  mode?: "text" | "regex";
  case_sensitive?: boolean;
  whole_word?: boolean;
  presets?: string[];
  pages?: number[];
}

export interface HiddenTextItem {
  page: number;
  reason: "invisible" | "transparent" | "white" | "tiny" | "off_page";
  text: string;
  rect: PdfRect;
}

export interface SecurityAudit {
  encrypted: boolean;
  needs_password: boolean;
  encryption?: string | null;
  permissions?: number;
  metadata?: Record<string, string>;
  has_xmp_metadata?: boolean;
  embedded_files?: string[];
  javascript_objects?: number;
  annotations?: Record<string, number>;
  annotation_count?: number;
  pending_redactions?: number;
  links?: number;
  form_fields?: number;
  form_fields_filled?: number;
  hidden_text?: HiddenTextItem[];
  hidden_text_count?: number;
}

export interface SanitizeOptions {
  metadata: boolean;
  xmp_metadata: boolean;
  embedded_files: boolean;
  javascript: boolean;
  hidden_text: boolean;
  white_text: boolean;
  annotations: boolean;
  form_data: boolean;
  links: boolean;
  thumbnails: boolean;
}

export const DEFAULT_SANITIZE: SanitizeOptions = {
  metadata: true,
  xmp_metadata: true,
  embedded_files: true,
  javascript: true,
  hidden_text: true,
  white_text: true,
  annotations: true,
  form_data: true,
  links: true,
  thumbnails: true,
};

export interface SanitizeResult {
  status: string;
  before: SecurityAudit;
  after: SecurityAudit;
  actions: string[];
}

export type Permission = "print" | "copy" | "modify" | "annotate" | "fill_forms" | "assemble";

export interface ProtectOptions {
  user_password?: string;
  owner_password: string;
  permissions: Permission[];
  apply_to_document?: boolean;
}

export const REDACT_PRESETS: { id: string; label: string }[] = [
  { id: "ssn", label: "SSN" },
  { id: "phone", label: "Phone" },
  { id: "email", label: "Email" },
  { id: "credit_card", label: "Credit card" },
  { id: "date", label: "Dates" },
  { id: "money", label: "Money" },
  { id: "address", label: "Addresses" },
];

// ─── fetch helpers ───────────────────────────────────────────────────────────

async function errorFrom(res: Response, fallback: string): Promise<Error> {
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") return new Error(body.detail);
  } catch {
    /* ignore */
  }
  return new Error(`${fallback} (${res.status})`);
}

async function jsonRequest<T>(path: string, init: RequestInit, fallback: string): Promise<T> {
  const res = await apiFetch(`${API_BASE}${path}`, init);
  if (!res.ok) throw await errorFrom(res, fallback);
  return res.json() as Promise<T>;
}

function post(body: unknown): RequestInit {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

// ─── API ─────────────────────────────────────────────────────────────────────

export function getRedactWords(docId: string, page: number) {
  return jsonRequest<{ page: number; width: number; height: number; words: RedactWord[] }>(
    `/api/pdf/${docId}/redact/words/${page}`, { method: "GET" }, "Failed to load words",
  );
}

export function markRedactions(docId: string, areas: RedactArea[], style?: Partial<MarkStyle>, label?: string) {
  const body: Record<string, unknown> = { areas };
  if (style) {
    if (style.fill_color) body.fill_color = style.fill_color;
    if (style.overlay_text) body.overlay_text = style.overlay_text;
    if (style.overlay_text_color) body.overlay_text_color = style.overlay_text_color;
    if (style.overlay_font_size) body.overlay_font_size = style.overlay_font_size;
  }
  if (label) body.label = label;
  return jsonRequest<{ status: string; count: number; marked: { page: number; xref: number; rect: PdfRect }[] }>(
    `/api/pdf/${docId}/redact/mark`, post(body), "Failed to mark redaction",
  );
}

export function getRedactMarks(docId: string) {
  return jsonRequest<{ marks: RedactMark[]; count: number }>(
    `/api/pdf/${docId}/redact/marks`, { method: "GET" }, "Failed to load redaction marks",
  );
}

export function deleteRedactMark(docId: string, page: number, xref: number) {
  return jsonRequest<{ status: string }>(
    `/api/pdf/${docId}/redact/marks/${page}/${xref}`, { method: "DELETE" }, "Failed to remove mark",
  );
}

export function clearRedactMarks(docId: string) {
  return jsonRequest<{ status: string; removed: number }>(
    `/api/pdf/${docId}/redact/marks`, { method: "DELETE" }, "Failed to clear marks",
  );
}

export function applyRedactions(docId: string, opts: ApplyOptions = {}) {
  return jsonRequest<ApplyResult>(`/api/pdf/${docId}/redact/apply`, post(opts), "Failed to apply redactions");
}

export function searchRedactions(docId: string, opts: SearchOptions) {
  return jsonRequest<{ matches: RedactMatch[]; count: number; truncated: boolean }>(
    `/api/pdf/${docId}/redact/search`, post(opts), "Search failed",
  );
}

export function getSecurityAudit(docId: string) {
  return jsonRequest<SecurityAudit>(`/api/pdf/${docId}/security/audit`, { method: "GET" }, "Audit failed");
}

export function sanitizeDocument(docId: string, opts: SanitizeOptions) {
  return jsonRequest<SanitizeResult>(`/api/pdf/${docId}/security/sanitize`, post(opts), "Sanitize failed");
}

/** apply_to_document=false → resolves to a Blob of the encrypted PDF. */
export async function protectDocument(docId: string, opts: ProtectOptions): Promise<Blob | { status: string }> {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/security/protect`, post(opts));
  if (!res.ok) throw await errorFrom(res, "Protect failed");
  if (opts.apply_to_document) return res.json();
  return res.blob();
}

export function unlockDocument(docId: string, password: string) {
  return jsonRequest<{ status: string }>(`/api/pdf/${docId}/security/unlock`, post({ password }), "Unlock failed");
}

// ─── geometry (pure, unit-tested) ────────────────────────────────────────────

export interface BoxLike {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** Client (screen) point → visible-page PDF points, clamped to the page. */
export function clientToPdfPoint(
  clientX: number, clientY: number, box: BoxLike, pageWidth: number, pageHeight: number,
): [number, number] {
  const x = ((clientX - box.left) / box.width) * pageWidth;
  const y = ((clientY - box.top) / box.height) * pageHeight;
  return [Math.min(Math.max(x, 0), pageWidth), Math.min(Math.max(y, 0), pageHeight)];
}

/** Normalised rect from two corner points. */
export function rectFromPoints(a: [number, number], b: [number, number]): PdfRect {
  return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])];
}

/** CSS percentage box for a PDF rect on a page (resolution-independent). */
export function rectToPercentStyle(r: PdfRect, pageWidth: number, pageHeight: number) {
  return {
    left: `${(r[0] / pageWidth) * 100}%`,
    top: `${(r[1] / pageHeight) * 100}%`,
    width: `${((r[2] - r[0]) / pageWidth) * 100}%`,
    height: `${((r[3] - r[1]) / pageHeight) * 100}%`,
  };
}

export function rectsIntersect(a: PdfRect, b: PdfRect): boolean {
  return a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];
}

/** Flatten selected search matches into mark areas. */
export function matchesToAreas(matches: RedactMatch[], selected: Set<string>): RedactArea[] {
  return matches
    .filter((m) => selected.has(m.id))
    .flatMap((m) => m.rects.map((rect) => ({ page: m.page, rect })));
}

/** Union word rects per line so a multi-word selection yields tidy boxes. */
export function wordsToAreas(words: RedactWord[], page: number): RedactArea[] {
  const byLine = new Map<string, PdfRect>();
  for (const w of words) {
    const key = `${w.block}:${w.line}`;
    const cur = byLine.get(key);
    byLine.set(
      key,
      cur
        ? [Math.min(cur[0], w.rect[0]), Math.min(cur[1], w.rect[1]), Math.max(cur[2], w.rect[2]), Math.max(cur[3], w.rect[3])]
        : [...w.rect] as PdfRect,
    );
  }
  return [...byLine.values()].map((rect) => ({ page, rect }));
}

/** Map the UI's permission checkboxes to the API list. */
export function permissionsFrom(flags: Record<Permission, boolean>): Permission[] {
  return (Object.keys(flags) as Permission[]).filter((k) => flags[k]);
}

// ─── shared state between RedactPanel and RedactOverlay ──────────────────────

export type RedactTool = "area" | "word";

interface RedactState {
  active: boolean; // overlay captures the pointer when true
  tool: RedactTool;
  style: MarkStyle;
  marks: RedactMark[];
  previewMatches: RedactMatch[]; // search results ticked for redaction (shown dashed)
  busy: boolean;
  setActive: (v: boolean) => void;
  setTool: (t: RedactTool) => void;
  setStyle: (s: Partial<MarkStyle>) => void;
  setMarks: (m: RedactMark[]) => void;
  setPreviewMatches: (m: RedactMatch[]) => void;
  setBusy: (b: boolean) => void;
  refreshMarks: (docId: string) => Promise<void>;
}

export const useRedactStore = create<RedactState>((set) => ({
  active: false,
  tool: "area",
  style: { fill_color: "#000000", overlay_text: "", overlay_text_color: "#ffffff", overlay_font_size: 10 },
  marks: [],
  previewMatches: [],
  busy: false,
  setActive: (active) => set({ active }),
  setTool: (tool) => set({ tool }),
  setStyle: (s) => set((st) => ({ style: { ...st.style, ...s } })),
  setMarks: (marks) => set({ marks }),
  setPreviewMatches: (previewMatches) => set({ previewMatches }),
  setBusy: (busy) => set({ busy }),
  refreshMarks: async (docId) => {
    const { marks } = await getRedactMarks(docId);
    set({ marks });
  },
}));
