/**
 * Forms feature: typed API client, geometry helpers and a small shared store
 * so FormsPanel (side panel) and FormsOverlay (on top of the page) stay in sync
 * without touching the global editor store.
 *
 * COORDINATES: every `rect` exchanged with the backend is [x0, y0, x1, y1] in
 * PDF points, top-left origin, in the page *as displayed* (rotation applied).
 * The overlay converts to/from screen pixels purely by proportion
 * (point / pageWidthPts), so it is independent of render DPI and zoom.
 */
import { create } from "zustand";

import { API_BASE, apiFetch } from "../api";

export type FieldType = "text" | "checkbox" | "radio" | "combo" | "list" | "signature" | "button" | "unknown";
export type CreatableFieldType = "text" | "checkbox" | "radio" | "combo" | "list" | "signature";
export type Rect = [number, number, number, number];
export type FieldValue = string | boolean | string[] | null;

export interface FormField {
  id: number; // PDF object number of the widget
  page: number;
  name: string;
  type: FieldType;
  rect: Rect; // display points
  pdf_rect: Rect;
  required: boolean;
  readonly: boolean;
  tooltip: string;
  font_size: number;
  multiline: boolean;
  max_len: number;
  options: string[];
  option_labels: string[];
  export_value: string | null;
  value: FieldValue;
  checked?: boolean; // radio widget: is this button the selected one
  editable?: boolean; // combo
  multi_select?: boolean; // list box: several selections allowed (value is string[])
  format?: "date" | null; // text: Acrobat date format actions (mm/dd/yyyy)
}

export interface FormPageInfo {
  index: number;
  width: number;
  height: number;
  rotation: number;
}

export interface FormFieldsResponse {
  fields: FormField[];
  count: number;
  is_form: boolean;
  pages: FormPageInfo[];
}

export interface CreateFieldInput {
  page: number;
  type: CreatableFieldType;
  rect: Rect;
  name?: string;
  value?: FieldValue;
  options?: string[];
  export_value?: string;
  font_size?: number;
  required?: boolean;
  readonly?: boolean;
  tooltip?: string;
  multiline?: boolean;
  max_len?: number;
  editable?: boolean;
  multi_select?: boolean;
  /** text fields: "date" adds mm/dd/yyyy format actions, "" removes them (update only) */
  format?: "date" | "";
}

export type UpdateFieldInput = Partial<Omit<CreateFieldInput, "page" | "type">>;

export type DetectType = "text" | "checkbox" | "radio";

export interface DetectedField {
  page: number;
  type: DetectType;
  name: string;
  label: string;
  /** scan-* sources come from OCR + raster line detection on scanned pages */
  source: "box" | "glyph" | "underscore" | "line" | "label" | "scan-box" | "scan-line" | "scan-label";
  rect: Rect;
  id?: number;
  export_value?: string; // radio
  group?: string; // radio: the shared question label
  format?: "date";
}

export interface DetectResponse {
  created: DetectedField[];
  candidates: DetectedField[];
  count: number;
}

export interface FillResponse {
  status: string;
  filled: string[];
  errors: Record<string, string>;
}

export type FormDataFormat = "json" | "fdf" | "xfdf";

async function errorMessage(res: Response, fallback: string): Promise<string> {
  try {
    const body = await res.json();
    const d = body?.detail;
    if (typeof d === "string") return d;
    if (d && typeof d === "object") {
      const errs = d.errors ? Object.entries(d.errors as Record<string, string>).map(([k, v]) => `${k}: ${v}`) : [];
      return [d.message, ...errs].filter(Boolean).join("; ") || fallback;
    }
  } catch {
    /* non-JSON */
  }
  return fallback;
}

async function request<T>(url: string, init: RequestInit | undefined, fallback: string): Promise<T> {
  const res = await apiFetch(url, init);
  if (!res.ok) throw new Error(await errorMessage(res, fallback));
  return res.json() as Promise<T>;
}

const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

// ─── API client ─────────────────────────────────────────────────────────────

export function listFormFields(docId: string, page?: number): Promise<FormFieldsResponse> {
  const q = page !== undefined ? `?page=${page}` : "";
  return request(`${API_BASE}/api/pdf/${docId}/form-fields${q}`, undefined, "Failed to load form fields");
}

export function fillFormFields(docId: string, values: Record<string, FieldValue>): Promise<FillResponse> {
  return request(`${API_BASE}/api/pdf/${docId}/form-fields/fill`, json("POST", { values }), "Failed to fill form");
}

export function createFormField(docId: string, input: CreateFieldInput): Promise<{ status: string; field: FormField }> {
  return request(`${API_BASE}/api/pdf/${docId}/form-fields`, json("POST", input), "Failed to create field");
}

export function updateFormField(
  docId: string,
  fieldId: number,
  input: UpdateFieldInput,
): Promise<{ status: string; field: FormField }> {
  return request(`${API_BASE}/api/pdf/${docId}/form-fields/${fieldId}`, json("PATCH", input), "Failed to update field");
}

export function deleteFormField(
  docId: string,
  fieldId: number,
  wholeField = false,
): Promise<{ status: string; deleted: number[]; name: string }> {
  const q = wholeField ? "?whole_field=true" : "";
  return request(`${API_BASE}/api/pdf/${docId}/form-fields/${fieldId}${q}`, { method: "DELETE" }, "Failed to delete field");
}

export function flattenForm(docId: string): Promise<{ status: string; flattened: number }> {
  return request(`${API_BASE}/api/pdf/${docId}/form-fields/flatten`, { method: "POST" }, "Failed to flatten form");
}

export function detectFormFields(
  docId: string,
  opts: { pages?: number[]; dry_run?: boolean; types?: DetectType[] } = {},
): Promise<DetectResponse> {
  return request(`${API_BASE}/api/pdf/${docId}/form-fields/detect`, json("POST", opts), "Auto-detect failed");
}

export function getFormDataExportUrl(docId: string, format: FormDataFormat = "json"): string {
  return `${API_BASE}/api/pdf/${docId}/form-fields/export?format=${format}`;
}

export function importFormData(
  docId: string,
  format: FormDataFormat,
  data: string | Record<string, unknown>,
): Promise<FillResponse> {
  return request(`${API_BASE}/api/pdf/${docId}/form-fields/import`, json("POST", { format, data }), "Import failed");
}

export function formatFromFilename(name: string): FormDataFormat | null {
  const ext = name.toLowerCase().split(".").pop();
  if (ext === "json" || ext === "fdf" || ext === "xfdf") return ext;
  if (ext === "xml") return "xfdf";
  return null;
}

// ─── Geometry (pure, unit-tested) ───────────────────────────────────────────

export interface PageBox {
  width: number; // PDF points (displayed page)
  height: number;
}

/** Normalise so x0<=x1, y0<=y1. */
export function normalizeRect(r: Rect): Rect {
  return [Math.min(r[0], r[2]), Math.min(r[1], r[3]), Math.max(r[0], r[2]), Math.max(r[1], r[3])];
}

/** Clamp a rect inside the page while preserving its size where possible. */
export function clampRect(r: Rect, page: PageBox): Rect {
  const [x0, y0, x1, y1] = normalizeRect(r);
  const w = Math.min(x1 - x0, page.width);
  const h = Math.min(y1 - y0, page.height);
  const nx = Math.max(0, Math.min(x0, page.width - w));
  const ny = Math.max(0, Math.min(y0, page.height - h));
  return [nx, ny, nx + w, ny + h];
}

/** Rect in points -> CSS percentages of the page box (for absolutely positioned overlays). */
export function rectToPercentStyle(r: Rect, page: PageBox) {
  const [x0, y0, x1, y1] = normalizeRect(r);
  return {
    left: `${(x0 / page.width) * 100}%`,
    top: `${(y0 / page.height) * 100}%`,
    width: `${((x1 - x0) / page.width) * 100}%`,
    height: `${((y1 - y0) / page.height) * 100}%`,
  };
}

/** Client (mouse) coordinates -> PDF points, given the overlay element's on-screen box. */
export function clientToPoints(
  clientX: number,
  clientY: number,
  box: { left: number; top: number; width: number; height: number },
  page: PageBox,
): [number, number] {
  const x = ((clientX - box.left) / box.width) * page.width;
  const y = ((clientY - box.top) / box.height) * page.height;
  return [Math.max(0, Math.min(page.width, x)), Math.max(0, Math.min(page.height, y))];
}

export type Handle = "move" | "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";

/** Apply a drag delta (points) to a rect for the given handle; keeps a minimum size. */
export function applyDrag(r: Rect, handle: Handle, dx: number, dy: number, page: PageBox, min = 6): Rect {
  let [x0, y0, x1, y1] = normalizeRect(r);
  if (handle === "move") return clampRect([x0 + dx, y0 + dy, x1 + dx, y1 + dy], page);
  if (handle.includes("w")) x0 = Math.min(x0 + dx, x1 - min);
  if (handle.includes("e")) x1 = Math.max(x1 + dx, x0 + min);
  if (handle.includes("n")) y0 = Math.min(y0 + dy, y1 - min);
  if (handle.includes("s")) y1 = Math.max(y1 + dy, y0 + min);
  return [Math.max(0, x0), Math.max(0, y0), Math.min(page.width, x1), Math.min(page.height, y1)];
}

/** Default box for a click (no drag) when creating a field of `type` at point (x, y). */
export function defaultRectAt(type: CreatableFieldType, x: number, y: number, page: PageBox): Rect {
  const size: Record<CreatableFieldType, [number, number]> = {
    text: [160, 20],
    checkbox: [14, 14],
    radio: [14, 14],
    combo: [140, 20],
    list: [140, 60],
    signature: [180, 40],
  };
  const [w, h] = size[type];
  return clampRect([x, y - h / 2, x + w, y + h / 2], page);
}

/** Group fields by name preserving first-seen order (radio groups => one entry). */
export function groupFieldsByName(fields: FormField[]): { name: string; type: FieldType; widgets: FormField[] }[] {
  const order: string[] = [];
  const map = new Map<string, FormField[]>();
  for (const f of fields) {
    const key = f.name || `#${f.id}`;
    if (!map.has(key)) {
      map.set(key, []);
      order.push(key);
    }
    map.get(key)!.push(f);
  }
  return order.map((name) => ({ name, type: map.get(name)![0].type, widgets: map.get(name)! }));
}

/** One-line summary of an auto-detect result, e.g. "Created 5 fields (3 text, 1 checkbox, 1 radio group)". */
export function summarizeDetected(created: DetectedField[]): string {
  if (created.length === 0) return "No new fields found";
  const text = created.filter((c) => c.type === "text");
  const dates = text.filter((c) => c.format === "date").length;
  const checks = created.filter((c) => c.type === "checkbox").length;
  const groups = new Set(created.filter((c) => c.type === "radio").map((c) => `${c.page}:${c.name}`)).size;
  const parts = [`${text.length} text${dates ? ` (${dates} date)` : ""}`, `${checks} checkbox`];
  if (groups) parts.push(`${groups} radio group${groups === 1 ? "" : "s"}`);
  const scanned = created.some((c) => String(c.source ?? "").startsWith("scan"));
  return `Created ${created.length} field${created.length === 1 ? "" : "s"} (${parts.join(", ")})${scanned ? " from the scanned page" : ""}`;
}

/** ISO yyyy-mm-dd -> mm/dd/yyyy (what date-formatted PDF fields store); other input unchanged. */
export function toPdfDate(v: string): string {
  const m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(v.trim());
  return m ? `${m[2].padStart(2, "0")}/${m[3].padStart(2, "0")}/${m[1]}` : v;
}

/** Selected values of a list box field as an array (single or multi-select). */
export function listSelection(f: FormField): string[] {
  if (Array.isArray(f.value)) return f.value;
  return typeof f.value === "string" && f.value ? [f.value] : [];
}

/** Names of required fields that are still empty. */
export function missingRequired(fields: FormField[]): string[] {
  const out = new Set<string>();
  for (const g of groupFieldsByName(fields)) {
    const f = g.widgets[0];
    if (!f.required) continue;
    const v = f.value;
    const empty = v === null || v === "" || v === false || (Array.isArray(v) && v.length === 0);
    if (empty) out.add(g.name);
  }
  return [...out];
}

// ─── Shared store (panel <-> overlay) ───────────────────────────────────────

export interface FormsState {
  docId: string | null;
  fields: FormField[];
  pages: FormPageInfo[];
  loading: boolean;
  error: string | null;
  prepareMode: boolean;
  createType: CreatableFieldType;
  selectedId: number | null;
  /** bumped after every successful mutation so views can re-render */
  version: number;
  setPrepareMode: (on: boolean) => void;
  setCreateType: (t: CreatableFieldType) => void;
  select: (id: number | null) => void;
  refresh: (docId: string) => Promise<void>;
  /** replace one field in local state (after create/update) */
  upsertLocal: (f: FormField) => void;
}

export const useFormsStore = create<FormsState>((set, get) => ({
  docId: null,
  fields: [],
  pages: [],
  loading: false,
  error: null,
  prepareMode: false,
  createType: "text",
  selectedId: null,
  version: 0,
  setPrepareMode: (on) => set({ prepareMode: on, selectedId: on ? get().selectedId : null }),
  setCreateType: (t) => set({ createType: t }),
  select: (id) => set({ selectedId: id }),
  refresh: async (docId) => {
    set({ loading: true, error: null, docId });
    try {
      const res = await listFormFields(docId);
      if (get().docId !== docId) return; // stale
      const sel = get().selectedId;
      set({
        fields: res.fields,
        pages: res.pages,
        loading: false,
        version: get().version + 1,
        selectedId: sel !== null && res.fields.some((f) => f.id === sel) ? sel : null,
      });
    } catch (e) {
      set({ loading: false, error: e instanceof Error ? e.message : String(e) });
    }
  },
  upsertLocal: (f) => {
    const fields = get().fields.filter((x) => x.id !== f.id);
    set({ fields: [...fields, f], version: get().version + 1 });
  },
}));
