/**
 * Image & vector-object editing API client (backend: backend/features/objects.py).
 *
 * COORDINATES: every rect/point exchanged with the backend is in PDF points in
 * the page's DISPLAYED orientation (rotation already applied), origin top-left,
 * y down. `page_width` / `page_height` from listObjects() are the displayed page
 * size in points. Convert to screen pixels with:
 *   px = pt / page_width * renderedElementWidthPx
 * The helpers below do that conversion from a DOMRect, which already includes any
 * CSS zoom transform, so callers never need to know the render DPI or zoom.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export type Bbox = [number, number, number, number]; // [x0, y0, x1, y1] PDF points

export interface ImageObject {
  id: string;
  kind: "image";
  xref: number;
  occurrence: number;
  bbox: Bbox;
  width: number; // pixels
  height: number; // pixels
  colorspace: string;
  bpc: number | null;
  size: number | null;
  has_mask: boolean;
  names: string[];
  editable: boolean;
  method: "stream" | "redact" | "none";
}

export interface DrawingObject {
  id: string;
  kind: "drawing";
  index: number;
  bbox: Bbox;
  path_count: number;
  seqnos: number[];
  stroke: number[] | null;
  fill: number[] | null;
  width: number | null;
  background: boolean;
}

export type PageObject = ImageObject | DrawingObject;

export interface PageObjects {
  page: number;
  page_width: number;
  page_height: number;
  rotation: number;
  images: ImageObject[];
  drawings: DrawingObject[];
}

export type ShapeType = "rect" | "ellipse" | "line" | "arrow";

export interface ShapeOptions {
  strokeColor: number[] | null; // [r,g,b] 0..1, null = no stroke
  fillColor: number[] | null; // null = no fill
  width: number;
  dashed?: boolean;
  strokeOpacity?: number;
  fillOpacity?: number;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

function postJson<T>(path: string, body: unknown): Promise<T> {
  return request<T>(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// ─── Listing ───────────────────────────────────────────────────────────────

export function listObjects(docId: string, page: number): Promise<PageObjects> {
  return request<PageObjects>(`${API_BASE}/api/pdf/${docId}/objects/${page}`);
}

// ─── Images ────────────────────────────────────────────────────────────────

function imageRef(page: number, img: ImageObject) {
  return { page, xref: img.xref, occurrence: img.occurrence, bbox: img.bbox };
}

export function moveImage(docId: string, page: number, img: ImageObject, newBbox: Bbox) {
  return postJson<{ status: string; method: string; bbox: Bbox }>(
    `/api/pdf/${docId}/objects/image/move`,
    { ...imageRef(page, img), new_bbox: newBbox },
  );
}

export function rotateImage(docId: string, page: number, img: ImageObject, angle: 90 | 180 | 270 = 90) {
  return postJson<{ status: string }>(`/api/pdf/${docId}/objects/image/rotate`, { ...imageRef(page, img), angle });
}

export function deleteImage(docId: string, page: number, img: ImageObject) {
  return postJson<{ status: string }>(`/api/pdf/${docId}/objects/image/delete`, imageRef(page, img));
}

export function cropImage(docId: string, page: number, img: ImageObject, cropBbox: Bbox) {
  return postJson<{ status: string; pixels: [number, number] }>(
    `/api/pdf/${docId}/objects/image/crop`,
    { ...imageRef(page, img), crop_bbox: cropBbox },
  );
}

export function replaceImage(
  docId: string,
  page: number,
  img: ImageObject,
  file: File,
  scope: "placement" | "all" = "placement",
) {
  const fd = new FormData();
  fd.append("page", String(page));
  fd.append("xref", String(img.xref));
  fd.append("occurrence", String(img.occurrence));
  fd.append("scope", scope);
  fd.append("file", file);
  return request<{ status: string; method: string }>(`${API_BASE}/api/pdf/${docId}/objects/image/replace`, {
    method: "POST",
    body: fd,
  });
}

export function insertImage(docId: string, page: number, rect: Bbox, file: File, keepProportion = true) {
  const fd = new FormData();
  fd.append("page", String(page));
  fd.append("x0", String(rect[0]));
  fd.append("y0", String(rect[1]));
  fd.append("x1", String(rect[2]));
  fd.append("y1", String(rect[3]));
  fd.append("keep_proportion", String(keepProportion));
  fd.append("file", file);
  return request<{ status: string; xref: number }>(`${API_BASE}/api/pdf/${docId}/objects/image/insert`, {
    method: "POST",
    body: fd,
  });
}

export function getImageExtractUrl(docId: string, xref: number, format: "original" | "png" = "original"): string {
  return `${API_BASE}/api/pdf/${docId}/objects/image/${xref}/extract?format=${format}`;
}

// ─── Vector drawings & shapes ──────────────────────────────────────────────

export function moveDrawing(docId: string, page: number, d: DrawingObject, newBbox: Bbox) {
  return postJson<{ status: string; bbox: Bbox }>(`/api/pdf/${docId}/objects/drawing/move`, {
    page,
    index: d.index,
    bbox: d.bbox,
    new_bbox: newBbox,
  });
}

export function deleteDrawing(docId: string, page: number, d: DrawingObject) {
  return postJson<{ status: string }>(`/api/pdf/${docId}/objects/drawing/delete`, {
    page,
    index: d.index,
    bbox: d.bbox,
  });
}

export function addShape(
  docId: string,
  page: number,
  type: ShapeType,
  geometry: { rect?: Bbox; start?: [number, number]; end?: [number, number] },
  opts: ShapeOptions,
) {
  return postJson<{ status: string }>(`/api/pdf/${docId}/objects/shape`, {
    page,
    type,
    ...geometry,
    stroke_color: opts.strokeColor,
    fill_color: opts.fillColor,
    width: opts.width,
    dashed: opts.dashed ?? false,
    stroke_opacity: opts.strokeOpacity ?? 1,
    fill_opacity: opts.fillOpacity ?? 1,
  });
}

// ─── Pure geometry helpers (unit tested) ───────────────────────────────────

export type Handle = "nw" | "n" | "ne" | "e" | "se" | "s" | "sw" | "w";

/** Screen point -> PDF point, given the overlay element's on-screen rect. */
export function clientToPdf(
  clientX: number,
  clientY: number,
  elRect: { left: number; top: number; width: number; height: number },
  pageWidth: number,
  pageHeight: number,
): [number, number] {
  const x = ((clientX - elRect.left) / elRect.width) * pageWidth;
  const y = ((clientY - elRect.top) / elRect.height) * pageHeight;
  return [x, y];
}

export function normalizeBbox(b: Bbox): Bbox {
  return [Math.min(b[0], b[2]), Math.min(b[1], b[3]), Math.max(b[0], b[2]), Math.max(b[1], b[3])];
}

export function bboxToPercentStyle(b: Bbox, pageWidth: number, pageHeight: number) {
  return {
    left: `${(b[0] / pageWidth) * 100}%`,
    top: `${(b[1] / pageHeight) * 100}%`,
    width: `${((b[2] - b[0]) / pageWidth) * 100}%`,
    height: `${((b[3] - b[1]) / pageHeight) * 100}%`,
  };
}

const MIN_SIZE = 4; // points

/**
 * New bbox after dragging by (dx, dy) PDF points from `orig`.
 * mode "move" translates; a handle resizes from that edge/corner.
 * keepAspect (e.g. Shift held) keeps the original aspect ratio on corner drags.
 */
export function applyDrag(orig: Bbox, mode: "move" | Handle, dx: number, dy: number, keepAspect = false): Bbox {
  let [x0, y0, x1, y1] = orig;
  if (mode === "move") return [x0 + dx, y0 + dy, x1 + dx, y1 + dy];
  if (mode.includes("w")) x0 = Math.min(x0 + dx, x1 - MIN_SIZE);
  if (mode.includes("e")) x1 = Math.max(x1 + dx, x0 + MIN_SIZE);
  if (mode.includes("n")) y0 = Math.min(y0 + dy, y1 - MIN_SIZE);
  if (mode.includes("s")) y1 = Math.max(y1 + dy, y0 + MIN_SIZE);
  if (keepAspect && mode.length === 2) {
    const ratio = (orig[2] - orig[0]) / (orig[3] - orig[1]);
    const w = x1 - x0;
    const h = y1 - y0;
    if (w / h > ratio) {
      const nw = h * ratio;
      if (mode.includes("w")) x0 = x1 - nw;
      else x1 = x0 + nw;
    } else {
      const nh = w / ratio;
      if (mode.includes("n")) y0 = y1 - nh;
      else y1 = y0 + nh;
    }
  }
  return [x0, y0, x1, y1];
}

/** Clamp a bbox so it stays (at least partly) on the page. */
export function clampToPage(b: Bbox, pageWidth: number, pageHeight: number): Bbox {
  const w = b[2] - b[0];
  const h = b[3] - b[1];
  const x0 = Math.min(Math.max(b[0], -w + 10), pageWidth - 10);
  const y0 = Math.min(Math.max(b[1], -h + 10), pageHeight - 10);
  return [x0, y0, x0 + w, y0 + h];
}

export function intersectBbox(a: Bbox, b: Bbox): Bbox | null {
  const r: Bbox = [Math.max(a[0], b[0]), Math.max(a[1], b[1]), Math.min(a[2], b[2]), Math.min(a[3], b[3])];
  return r[2] - r[0] >= 1 && r[3] - r[1] >= 1 ? r : null;
}

/** Round to 0.01 pt (removes float noise from pointer math before sending). */
export function roundBbox(b: Bbox): Bbox {
  return b.map((v) => Math.round(v * 100) / 100) as Bbox;
}

export function bboxEquals(a: Bbox, b: Bbox, tol = 0.5): boolean {
  return a.every((v, i) => Math.abs(v - b[i]) <= tol);
}

export function hexToRgb01(hex: string): number[] {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  const n = parseInt(full, 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}

/** Default placement for a newly inserted image: centred, max 50% of the page. */
export function defaultInsertRect(
  imgW: number,
  imgH: number,
  pageWidth: number,
  pageHeight: number,
): Bbox {
  const maxW = pageWidth * 0.5;
  const maxH = pageHeight * 0.5;
  const s = Math.min(maxW / imgW, maxH / imgH, 1); // 1px = 1pt unless too big
  const w = imgW * s;
  const h = imgH * s;
  const x0 = (pageWidth - w) / 2;
  const y0 = (pageHeight - h) / 2;
  return [x0, y0, x0 + w, y0 + h];
}
