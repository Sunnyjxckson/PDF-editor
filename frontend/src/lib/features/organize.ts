// Typed client for backend/features/organize.py: page organization, headers /
// footers / page numbers / Bates, bookmarks, and comments (real PDF annotations).
//
// COORDINATES: every rect/point sent to or received from these endpoints is in
// VISIBLE PAGE POINTS: PDF points (1/72 in), TOP-LEFT origin, measured on the
// page as displayed (after /Rotate, relative to the CropBox). DocumentInfo
// pages[i].width/height (from /info) are in the same space. To convert a mouse
// position over a rendered page element of any scale/zoom:
//     pt = (clientX - box.left) / box.width * pageWidthPt
// (see clientToPagePoint). Page indexes are 0-based.
//
// lib/api.ts does not export its base URL, so it is read the same way here.
import { API_BASE, apiFetch } from "../api";

async function errorDetail(res: Response, fallback: string): Promise<string> {
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") return body.detail;
  } catch {
    /* non-JSON body */
  }
  return fallback;
}

function url(docId: string, path: string): string {
  return `${API_BASE}/api/pdf/${docId}/organize/${path}`;
}

async function call<T>(method: string, u: string, body: unknown, fallback: string): Promise<T> {
  const init: RequestInit = { method };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const res = await apiFetch(u, init);
  if (!res.ok) throw new Error(await errorDetail(res, fallback));
  return res.json() as Promise<T>;
}

async function callBlob(u: string, body: unknown, fallback: string): Promise<Blob> {
  const res = await apiFetch(u, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await errorDetail(res, fallback));
  return res.blob();
}

/** Thumbnail URL with a cache-buster so it refreshes after page mutations. */
export function organizeThumbnailUrl(docId: string, page: number, version = 0): string {
  return `${API_BASE}/api/pdf/${docId}/thumbnail/${page}?v=${version}`;
}

/** Existing (non-organize) endpoint: POST /api/pdf/{id}/reorder with the full new order. */
export async function reorderDocument(docId: string, pageOrder: number[]): Promise<void> {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/reorder`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page_order: pageOrder }),
  });
  if (!res.ok) throw new Error(await errorDetail(res, "Reorder failed"));
}

/** Trigger a browser download for a blob. */
export function saveBlob(blob: Blob, filename: string): void {
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}

// ─── Page operations ─────────────────────────────────────────────────────────

export type PaperSize = "letter" | "legal" | "a4" | "a3" | "a5" | "tabloid";

export interface InsertBlankOptions {
  position: number;
  size?: "neighbor" | PaperSize | "custom";
  width?: number;
  height?: number;
  landscape?: boolean;
  count?: number;
}

export function insertBlankPages(docId: string, opts: InsertBlankOptions) {
  return call<{ status: string; page_count: number; inserted_at: number }>(
    "POST", url(docId, "insert-blank"), opts, "Insert blank page failed");
}

export interface InsertFileOptions {
  position: number;
  file?: File | Blob;
  sourceDocId?: string;
  /** 0-based inclusive range in the source PDF; omit for all pages. */
  pageFrom?: number;
  pageTo?: number;
}

export async function insertPagesFromFile(docId: string, opts: InsertFileOptions) {
  const fd = new FormData();
  fd.append("position", String(opts.position));
  if (opts.file) fd.append("file", opts.file, (opts.file as File).name || "insert.pdf");
  if (opts.sourceDocId) fd.append("source_doc_id", opts.sourceDocId);
  if (opts.pageFrom !== undefined) fd.append("page_from", String(opts.pageFrom));
  if (opts.pageTo !== undefined) fd.append("page_to", String(opts.pageTo));
  const res = await apiFetch(url(docId, "insert-file"), { method: "POST", body: fd });
  if (!res.ok) throw new Error(await errorDetail(res, "Insert from file failed"));
  return res.json() as Promise<{ status: string; page_count: number; inserted: number; inserted_at: number }>;
}

export function extractPages(docId: string, pages: number[], opts: { deleteAfter?: boolean; filename?: string } = {}) {
  return callBlob(url(docId, "extract"),
    { pages, delete_after: !!opts.deleteAfter, filename: opts.filename || "extracted.pdf" },
    "Extract failed");
}

export function duplicatePages(docId: string, pages: number[]) {
  return call<{ status: string; page_count: number }>("POST", url(docId, "duplicate"), { pages }, "Duplicate failed");
}

export function rotatePages(docId: string, pages: number[], angle: number, relative = true) {
  return call<{ status: string; rotations: Record<string, number> }>(
    "POST", url(docId, "rotate"), { pages, angle, relative }, "Rotate failed");
}

export function deletePages(docId: string, pages: number[]) {
  return call<{ status: string; page_count: number }>("POST", url(docId, "delete"), { pages }, "Delete failed");
}

export interface CropOptions {
  mode: "box" | "margins" | "auto" | "reset";
  /** visible points [x0, y0, x1, y1] */
  box?: number[];
  margins?: { top: number; right: number; bottom: number; left: number };
  padding?: number;
  threshold?: number;
}

export function cropPages(docId: string, pages: number[], opts: CropOptions) {
  return call<{ status: string; pages: { page: number; cropbox: number[]; skipped?: string }[] }>(
    "POST", url(docId, "crop"), { pages, ...opts }, "Crop failed");
}

export interface ResizeOptions {
  size: PaperSize | "custom";
  width?: number;
  height?: number;
  match_orientation?: boolean;
  scale_content?: boolean;
}

export function resizePages(docId: string, pages: number[], opts: ResizeOptions) {
  return call<{ status: string }>("POST", url(docId, "resize"), { pages, ...opts }, "Resize failed");
}

export interface SplitOptions {
  mode: "every_n" | "bookmarks" | "size";
  n?: number;
  level?: number;
  max_mb?: number;
}

/** Split and download all parts as a zip. The source document is not changed. */
export function splitToZip(docId: string, opts: SplitOptions) {
  return callBlob(url(docId, "split"), { ...opts, download: true }, "Split failed");
}

export interface SplitPart { id: string; filename: string; pages: number[]; page_count: number; size: number }

/** Split into new stored documents (returns their ids). */
export function splitToDocuments(docId: string, opts: SplitOptions) {
  return call<{ status: string; documents: SplitPart[] }>(
    "POST", url(docId, "split"), { ...opts, download: false }, "Split failed");
}

// ─── Headers / footers / page numbers / Bates ────────────────────────────────

export type Base14Font =
  | "helv" | "hebo" | "heit" | "hebi"
  | "tiro" | "tibo" | "tiit" | "tibi"
  | "cour" | "cobo" | "coit" | "cobi";

export const FONT_LABELS: Record<Base14Font, string> = {
  helv: "Helvetica", hebo: "Helvetica Bold", heit: "Helvetica Oblique", hebi: "Helvetica Bold Oblique",
  tiro: "Times", tibo: "Times Bold", tiit: "Times Italic", tibi: "Times Bold Italic",
  cour: "Courier", cobo: "Courier Bold", coit: "Courier Oblique", cobi: "Courier Bold Oblique",
};

export type HFPosition = "top-left" | "top-center" | "top-right" | "bottom-left" | "bottom-center" | "bottom-right";
export type HFSlot = "header_left" | "header_center" | "header_right" | "footer_left" | "footer_center" | "footer_right";
export const HF_SLOTS: HFSlot[] = ["header_left", "header_center", "header_right", "footer_left", "footer_center", "footer_right"];

export interface HFStyle {
  font?: Base14Font;
  font_size?: number;
  color?: number[];
  margin_top?: number;
  margin_bottom?: number;
  margin_left?: number;
  margin_right?: number;
  skip_first?: boolean;
  pages?: number[] | null;
}

export interface HeaderFooterOptions extends HFStyle, Partial<Record<HFSlot, string>> {
  start_number?: number;
  bates_prefix?: string;
  bates_suffix?: string;
  bates_digits?: number;
  bates_start?: number;
  /** Python strftime format used for {date} */
  date_format?: string;
}

export interface HFPlanItem { page: number; n: number; total: number; bates: string; texts: Partial<Record<HFSlot, string>> }

export function addHeaderFooter(docId: string, opts: HeaderFooterOptions) {
  return call<{ status: string; run_id: string; stamped_count: number; first: HFPlanItem | null }>(
    "POST", url(docId, "header-footer"), opts, "Header/footer failed");
}

export function previewHeaderFooter(docId: string, opts: HeaderFooterOptions) {
  return call<{ pages: HFPlanItem[]; stamped_count: number }>(
    "POST", url(docId, "header-footer/preview"), opts, "Preview failed");
}

export interface PageNumberOptions extends HFStyle {
  /** e.g. "Page {n} of {total}" */
  format?: string;
  position?: HFPosition;
  start_number?: number;
}

export function addPageNumbers(docId: string, opts: PageNumberOptions) {
  return call<{ status: string; stamped_count: number }>("POST", url(docId, "page-numbers"), opts, "Page numbers failed");
}

export interface BatesOptions extends HFStyle {
  prefix?: string;
  suffix?: string;
  digits?: number;
  start?: number;
  position?: HFPosition;
}

export function addBates(docId: string, opts: BatesOptions) {
  return call<{ status: string; stamped_count: number; first_bates: string | null }>(
    "POST", url(docId, "bates"), opts, "Bates numbering failed");
}

// Every add (header/footer, page numbers, Bates) is a removable "run": its text is
// written as /Artifact /Pagination marked content, like Acrobat's, so it can be
// listed, updated or removed later. Headers/footers made by Acrobat (or any tool
// using the same structure) show up as the single run id "acrobat".

export type HFRunKind = "header-footer" | "page-numbers" | "bates";

export interface HFRun {
  id: string;
  source: "pylor" | "external";
  kind: HFRunKind;
  pages: number[];
  subtypes: ("Header" | "Footer")[];
  created: string | null;
  /** Full settings the run was made with (null for external runs). */
  settings: (HeaderFooterOptions & Required<Pick<HFStyle, "font" | "font_size">>) | null;
  editable: boolean;
}

export async function listHeaderFooterRuns(docId: string): Promise<HFRun[]> {
  return (await call<{ runs: HFRun[] }>("GET", url(docId, "header-footer/runs"), undefined, "Failed to load headers/footers")).runs;
}

/** Replace a run's text/format (old marked content is removed, then re-stamped). */
export function updateHeaderFooterRun(docId: string, runId: string, opts: HeaderFooterOptions & { kind?: HFRunKind }) {
  return call<{ status: string; run_id: string; stamped_count: number; runs: HFRun[] }>(
    "PUT", url(docId, `header-footer/runs/${encodeURIComponent(runId)}`), opts, "Update header/footer failed");
}

export function removeHeaderFooterRun(docId: string, runId: string) {
  return call<{ status: string; removed: number; runs: HFRun[] }>(
    "DELETE", url(docId, `header-footer/runs/${encodeURIComponent(runId)}`), undefined, "Remove header/footer failed");
}

export function removeAllHeaderFooterRuns(docId: string) {
  return call<{ status: string; removed: number; runs: HFRun[] }>(
    "DELETE", url(docId, "header-footer/runs"), undefined, "Remove headers/footers failed");
}

/** Short human label for a run, e.g. "Bates: ACME000001", "Header: CONFIDENTIAL". */
export function describeRun(run: HFRun): string {
  if (!run.settings) return `${run.subtypes.join(" & ")} added by another application`;
  const s = run.settings;
  if (run.kind === "bates") return `Bates: ${batesLabel(s.bates_prefix ?? "", s.bates_start ?? 1, 0, s.bates_digits ?? 6, s.bates_suffix ?? "")}`;
  const texts = HF_SLOTS.map((k) => s[k]).filter(Boolean) as string[];
  const label = run.kind === "page-numbers" ? "Page numbers" : "Header/footer";
  return `${label}: ${texts.join(" | ") || "(empty)"}`;
}

/** Mirror of the backend's token expansion ({n} {total} {bates} {date}). */
export function formatTokens(template: string, v: { n: number; total: number; bates: string; date: string }): string {
  return template
    .split("{n}").join(String(v.n))
    .split("{total}").join(String(v.total))
    .split("{bates}").join(v.bates)
    .split("{date}").join(v.date);
}

export function batesLabel(prefix: string, start: number, index: number, digits: number, suffix = ""): string {
  return `${prefix}${String(start + index).padStart(Math.max(1, digits), "0")}${suffix}`;
}

/** Mirror of backend _plan_header_footer, for an instant live preview. */
export function planHeaderFooter(
  opts: HeaderFooterOptions,
  pageCount: number,
  date = new Date().toLocaleDateString(),
): HFPlanItem[] {
  let targets = opts.pages != null
    ? Array.from(new Set(opts.pages)).sort((a, b) => a - b)
    : Array.from({ length: pageCount }, (_, i) => i);
  targets = targets.filter((p) => p >= 0 && p < pageCount);
  if (opts.skip_first && targets.length) targets = targets.slice(1);
  const start = opts.start_number ?? 1;
  const total = start + targets.length - 1;
  return targets.map((page, i) => {
    const n = start + i;
    const bates = batesLabel(opts.bates_prefix ?? "", opts.bates_start ?? 1, i, opts.bates_digits ?? 6, opts.bates_suffix ?? "");
    const texts: Partial<Record<HFSlot, string>> = {};
    for (const slot of HF_SLOTS) {
      const tpl = opts[slot];
      if (tpl) texts[slot] = formatTokens(tpl, { n, total, bates, date });
    }
    return { page, n, total, bates, texts };
  });
}

export function positionToSlot(position: HFPosition): HFSlot {
  const [v, h] = position.split("-");
  return `${v === "top" ? "header" : "footer"}_${h}` as HFSlot;
}

// ─── Bookmarks ───────────────────────────────────────────────────────────────

export interface Bookmark { index: number; level: number; title: string; page: number }

export async function listBookmarks(docId: string): Promise<Bookmark[]> {
  const r = await call<{ bookmarks: Bookmark[] }>("GET", url(docId, "bookmarks"), undefined, "Failed to load bookmarks");
  return r.bookmarks;
}

/** Add a bookmark. With no `parent`/`index` it becomes a TOP-LEVEL bookmark placed
 * among the top-level ones in page order; with `parent` it becomes a child of
 * that bookmark, in page order among its children. */
export async function addBookmark(docId: string, b: { title: string; page: number; parent?: number; index?: number; level?: number }) {
  return (await call<{ bookmarks: Bookmark[] }>("POST", url(docId, "bookmarks"), b, "Add bookmark failed")).bookmarks;
}

export type BookmarkDropPosition = "before" | "after" | "inside";

/** Drag-reorder / nest: move a bookmark (with its children) relative to `target`. */
export async function moveBookmark(docId: string, index: number, target: number, position: BookmarkDropPosition) {
  return (await call<{ bookmarks: Bookmark[] }>("POST", url(docId, `bookmarks/${index}/move`), { target, position }, "Move bookmark failed")).bookmarks;
}

/** Nest under the previous sibling (children come along). */
export async function indentBookmark(docId: string, index: number) {
  return (await call<{ bookmarks: Bookmark[] }>("POST", url(docId, `bookmarks/${index}/indent`), undefined, "Indent bookmark failed")).bookmarks;
}

/** Un-nest: becomes the next sibling of its parent (children come along). */
export async function outdentBookmark(docId: string, index: number) {
  return (await call<{ bookmarks: Bookmark[] }>("POST", url(docId, `bookmarks/${index}/outdent`), undefined, "Outdent bookmark failed")).bookmarks;
}

/** Flat index just past bookmark i's subtree. */
export function bookmarkSubtreeEnd(items: Bookmark[], i: number): number {
  let j = i + 1;
  while (j < items.length && items[j].level > items[i].level) j++;
  return j;
}

/** Drop zone from the pointer's offset inside a row: top quarter = before,
 * bottom quarter = after, middle = nest inside. */
export function bookmarkDropPosition(offsetY: number, rowHeight: number): BookmarkDropPosition {
  if (rowHeight <= 0) return "before";
  const f = offsetY / rowHeight;
  return f < 0.25 ? "before" : f > 0.75 ? "after" : "inside";
}

/** A bookmark cannot be dropped on itself or on one of its own descendants. */
export function canDropBookmark(items: Bookmark[], src: number, target: number): boolean {
  if (src < 0 || target < 0 || src >= items.length || target >= items.length) return false;
  return target < src || target >= bookmarkSubtreeEnd(items, src);
}

/** True if the bookmark has a previous sibling to nest under. */
export function canIndentBookmark(items: Bookmark[], i: number): boolean {
  for (let j = i - 1; j >= 0; j--) {
    if (items[j].level === items[i].level) return true;
    if (items[j].level < items[i].level) return false;
  }
  return false;
}

export async function updateBookmark(docId: string, index: number, patch: { title?: string; page?: number; level?: number }) {
  return (await call<{ bookmarks: Bookmark[] }>("PATCH", url(docId, `bookmarks/${index}`), patch, "Edit bookmark failed")).bookmarks;
}

export async function deleteBookmark(docId: string, index: number) {
  return (await call<{ bookmarks: Bookmark[] }>("DELETE", url(docId, `bookmarks/${index}`), undefined, "Delete bookmark failed")).bookmarks;
}

export async function replaceBookmarks(docId: string, items: { level: number; title: string; page: number }[]) {
  return (await call<{ bookmarks: Bookmark[] }>("PUT", url(docId, "bookmarks"), { bookmarks: items }, "Save bookmarks failed")).bookmarks;
}

// ─── Comments / markup ───────────────────────────────────────────────────────

export type CommentType =
  | "note" | "highlight" | "underline" | "strikeout" | "squiggly"
  | "freetext" | "callout" | "rect" | "ellipse" | "line" | "arrow"
  | "polygon" | "polyline" | "stamp" | "ink";

export type ReviewStatus = "Accepted" | "Rejected" | "Cancelled" | "Completed" | "None";
export const REVIEW_STATUSES: ReviewStatus[] = ["Accepted", "Rejected", "Cancelled", "Completed", "None"];

export interface CommentReply {
  id: number;
  page: number;
  type: string;
  author: string;
  contents: string;
  created: string | null;
  modified: string | null;
  /** id of the comment this one answers (/IRT); replies to replies chain */
  in_reply_to?: number | null;
}

export interface PdfComment extends CommentReply {
  pdf_subtype: string;
  subject: string;
  color: number[] | null;
  fill: number[] | null;
  opacity: number;
  /** visible points */
  rect: number[];
  in_reply_to: number | null;
  status: string | null;
  replies: CommentReply[];
}

export interface CommentCreate {
  page: number;
  type: CommentType;
  text?: string;
  author?: string;
  subject?: string;
  rect?: number[];
  quads?: number[][];
  area?: number[];
  search?: string;
  points?: number[][];
  callout?: number[][];
  color?: number[];
  fill?: number[];
  opacity?: number;
  width?: number;
  font_size?: number;
  stamp?: string;
  icon?: string;
}

export async function listComments(docId: string): Promise<PdfComment[]> {
  return (await call<{ comments: PdfComment[] }>("GET", url(docId, "comments"), undefined, "Failed to load comments")).comments;
}

export async function createComment(docId: string, c: CommentCreate): Promise<PdfComment> {
  return (await call<{ comment: PdfComment }>("POST", url(docId, "comments"), c, "Add comment failed")).comment;
}

export async function replyToComment(docId: string, id: number, text: string, author = "User") {
  return (await call<{ reply: CommentReply }>("POST", url(docId, `comments/${id}/reply`), { text, author }, "Reply failed")).reply;
}

export function setCommentStatus(docId: string, id: number, status: ReviewStatus, author = "User") {
  return call<{ status: string; state: string }>("POST", url(docId, `comments/${id}/status`), { status, author }, "Set status failed");
}

export async function updateComment(
  docId: string, id: number,
  patch: { text?: string; author?: string; subject?: string; color?: number[]; fill?: number[]; opacity?: number; width?: number },
): Promise<PdfComment> {
  return (await call<{ comment: PdfComment }>("PATCH", url(docId, `comments/${id}`), patch, "Edit comment failed")).comment;
}

export function deleteComment(docId: string, id: number) {
  return call<{ status: string; deleted: number }>("DELETE", url(docId, `comments/${id}`), undefined, "Delete comment failed");
}

export async function listStamps(docId: string): Promise<string[]> {
  return (await call<{ stamps: string[] }>("GET", url(docId, "stamps"), undefined, "Failed to load stamps")).stamps;
}

/** Nesting depth of each reply inside a thread (1 = answers the root). */
export function replyDepths(root: { id: number; replies: CommentReply[] }): Record<number, number> {
  const parent: Record<number, number | null | undefined> = {};
  for (const r of root.replies) parent[r.id] = r.in_reply_to;
  const out: Record<number, number> = {};
  for (const r of root.replies) {
    let d = 1;
    let p = parent[r.id];
    const seen = new Set<number>();
    while (p != null && p !== root.id && p in parent && !seen.has(p)) {
      seen.add(p);
      d++;
      p = parent[p];
    }
    out[r.id] = Math.min(d, 4);
  }
  return out;
}

// ─── Pure helpers (unit-tested) ──────────────────────────────────────────────

/**
 * Parse a 1-based page range string like "1-3, 5, 8-" into sorted unique
 * 0-based indexes. Throws on malformed or out-of-range input.
 */
export function parsePageRanges(input: string, pageCount: number): number[] {
  const out = new Set<number>();
  const parts = input.split(",").map((s) => s.trim()).filter(Boolean);
  if (!parts.length) throw new Error("Enter at least one page");
  for (const part of parts) {
    const m = /^(\d*)\s*-\s*(\d*)$/.exec(part);
    let a: number, b: number;
    if (m) {
      a = m[1] ? parseInt(m[1], 10) : 1;
      b = m[2] ? parseInt(m[2], 10) : pageCount;
    } else if (/^\d+$/.test(part)) {
      a = b = parseInt(part, 10);
    } else {
      throw new Error(`Bad page range "${part}"`);
    }
    if (a < 1 || b > pageCount || a > b) throw new Error(`Page range "${part}" is outside 1-${pageCount}`);
    for (let p = a; p <= b; p++) out.add(p - 1);
  }
  return Array.from(out).sort((x, y) => x - y);
}

/** Inverse of parsePageRanges: [0,1,2,4] -> "1-3, 5". */
export function formatPageRanges(pages: number[]): string {
  const s = Array.from(new Set(pages)).sort((a, b) => a - b);
  const out: string[] = [];
  let i = 0;
  while (i < s.length) {
    let j = i;
    while (j + 1 < s.length && s[j + 1] === s[j] + 1) j++;
    out.push(i === j ? `${s[i] + 1}` : `${s[i] + 1}-${s[j] + 1}`);
    i = j + 1;
  }
  return out.join(", ");
}

/**
 * Move the `selected` pages (as a block, keeping their relative order) so they
 * land before the page currently at index `target` (target === order.length
 * means "to the end"). Returns the new page order for POST /reorder:
 * result[i] = the old page index that ends up at position i.
 */
export function movePages(pageCount: number, selected: number[], target: number): number[] {
  const order = Array.from({ length: pageCount }, (_, i) => i);
  const sel = new Set(selected);
  const moving = order.filter((p) => sel.has(p));
  const rest = order.filter((p) => !sel.has(p));
  // insertion point in `rest`: number of non-moving pages before `target`
  const insertAt = order.slice(0, Math.max(0, Math.min(target, pageCount))).filter((p) => !sel.has(p)).length;
  return [...rest.slice(0, insertAt), ...moving, ...rest.slice(insertAt)];
}

export function isIdentityOrder(order: number[]): boolean {
  return order.every((p, i) => p === i);
}

/** After a reorder, where did each moved page end up (new indexes)? */
export function newIndexesOf(order: number[], oldPages: number[]): number[] {
  const set = new Set(oldPages);
  return order.map((p, i) => (set.has(p) ? i : -1)).filter((i) => i >= 0);
}

/**
 * Shift+click / Cmd-click / click selection logic for the thumbnail grid.
 */
export function nextSelection(
  current: number[], clicked: number, anchor: number | null,
  mods: { shift?: boolean; toggle?: boolean },
): { selection: number[]; anchor: number } {
  if (mods.shift && anchor !== null) {
    const [a, b] = anchor < clicked ? [anchor, clicked] : [clicked, anchor];
    const range = Array.from({ length: b - a + 1 }, (_, i) => a + i);
    const merged = mods.toggle ? Array.from(new Set([...current, ...range])) : range;
    return { selection: merged.sort((x, y) => x - y), anchor };
  }
  if (mods.toggle) {
    const has = current.includes(clicked);
    const sel = has ? current.filter((p) => p !== clicked) : [...current, clicked];
    return { selection: sel.sort((x, y) => x - y), anchor: clicked };
  }
  return { selection: [clicked], anchor: clicked };
}

/** Mouse position over a rendered page element -> visible page points. */
export function clientToPagePoint(
  clientX: number, clientY: number,
  box: { left: number; top: number; width: number; height: number },
  pageWidthPt: number, pageHeightPt: number,
): [number, number] {
  const x = ((clientX - box.left) / box.width) * pageWidthPt;
  const y = ((clientY - box.top) / box.height) * pageHeightPt;
  return [Math.min(Math.max(x, 0), pageWidthPt), Math.min(Math.max(y, 0), pageHeightPt)];
}

/** Two corner points (any drag direction) -> normalized [x0, y0, x1, y1]. */
export function rectFromPoints(a: [number, number], b: [number, number]): number[] {
  return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])];
}

export function hexToRgb01(hex: string): number[] {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  const n = parseInt(full, 16);
  if (Number.isNaN(n) || full.length !== 6) throw new Error(`Bad colour ${hex}`);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}

export function rgb01ToHex(rgb: number[] | null | undefined, fallback = "#000000"): string {
  if (!rgb || rgb.length < 3) return fallback;
  return "#" + rgb.slice(0, 3).map((v) => Math.round(Math.min(1, Math.max(0, v)) * 255).toString(16).padStart(2, "0")).join("");
}

/** Group threaded comments by page, preserving page order. */
export function groupCommentsByPage(comments: PdfComment[]): { page: number; comments: PdfComment[] }[] {
  const map = new Map<number, PdfComment[]>();
  for (const c of comments) {
    if (!map.has(c.page)) map.set(c.page, []);
    map.get(c.page)!.push(c);
  }
  return Array.from(map.entries()).sort((a, b) => a[0] - b[0]).map(([page, cs]) => ({ page, comments: cs }));
}

export function filterComments(
  comments: PdfComment[],
  f: { query?: string; author?: string; type?: string; status?: string },
): PdfComment[] {
  const q = (f.query || "").toLowerCase();
  return comments.filter((c) => {
    if (f.author && c.author !== f.author) return false;
    if (f.type && c.type !== f.type) return false;
    if (f.status && (c.status || "None") !== f.status) return false;
    if (q) {
      const hay = [c.contents, c.author, c.subject, ...c.replies.map((r) => r.contents)].join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

// ─── Markup tools (shared by OrganizeMarkupToolbar + OrganizeMarkupOverlay) ──

export type MarkupTool =
  | "note" | "highlight" | "underline" | "strikeout" | "squiggly"
  | "freetext" | "callout" | "rect" | "ellipse" | "line" | "arrow"
  | "polygon" | "ink" | "stamp";

export const TEXT_MARKUP_TOOLS: MarkupTool[] = ["highlight", "underline", "strikeout", "squiggly"];
/** Tools that ask for text after the gesture. */
export const TEXT_ENTRY_TOOLS: MarkupTool[] = ["note", "freetext", "callout"];

export interface MarkupSettings {
  tool: MarkupTool | null;
  /** stroke / text colour, "#rrggbb" */
  color: string;
  /** interior fill for rect/ellipse/polygon, "#rrggbb" or null for none */
  fill: string | null;
  opacity: number;
  /** stroke width in points */
  width: number;
  fontSize: number;
  stamp: string;
  author: string;
}

export const DEFAULT_TOOL_COLORS: Partial<Record<MarkupTool, string>> = {
  highlight: "#ffeb3b", underline: "#008000", strikeout: "#d90000", squiggly: "#3366ff",
  note: "#ffd933", freetext: "#000000", callout: "#000000",
};

export const DEFAULT_MARKUP_SETTINGS: MarkupSettings = {
  tool: null, color: "#d90000", fill: null, opacity: 1, width: 1.5, fontSize: 12, stamp: "Approved", author: "User",
};

type Pt = [number, number];

export interface MarkupGesture {
  /** mousedown point, visible page points */
  start: Pt;
  /** mouseup point, visible page points */
  end: Pt;
  /** polygon vertices / ink path, visible page points */
  path?: Pt[];
  text?: string;
}

const MIN_DRAG = 3; // points

function clampRect(r: number[], w: number, h: number): number[] {
  const bw = r[2] - r[0];
  const bh = r[3] - r[1];
  const x0 = Math.min(Math.max(r[0], 0), Math.max(0, w - bw));
  const y0 = Math.min(Math.max(r[1], 0), Math.max(0, h - bh));
  return [x0, y0, x0 + bw, y0 + bh];
}

/**
 * Turn a finished mouse gesture on a page into a POST /comments body.
 * Throws an Error with a user-facing message if the gesture is not usable.
 * pageWidth/pageHeight: visible page size in points.
 */
export function gestureToComment(
  page: number, s: MarkupSettings, g: MarkupGesture, pageWidth: number, pageHeight: number,
): CommentCreate {
  const tool = s.tool;
  if (!tool) throw new Error("No markup tool selected");
  const base: CommentCreate = {
    page, type: tool, author: s.author || "User", text: g.text ?? "",
    color: hexToRgb01(s.color), opacity: s.opacity, width: s.width,
  };
  const r = rectFromPoints(g.start, g.end);
  const dragged = r[2] - r[0] >= MIN_DRAG || r[3] - r[1] >= MIN_DRAG;
  const boxed = r[2] - r[0] >= MIN_DRAG && r[3] - r[1] >= MIN_DRAG;

  if (TEXT_MARKUP_TOOLS.includes(tool)) {
    if (!dragged) throw new Error("Drag across the text to mark it up");
    // A thin horizontal drag still catches the line it runs through.
    const area = boxed ? r : [r[0], r[1] - 4, r[2], r[3] + 4];
    return { ...base, area };
  }
  switch (tool) {
    case "note":
      return { ...base, rect: clampRect([g.end[0], g.end[1], g.end[0] + 20, g.end[1] + 20], pageWidth, pageHeight) };
    case "rect":
    case "ellipse":
      if (!boxed) throw new Error("Drag to draw the shape");
      return { ...base, rect: r, ...(s.fill ? { fill: hexToRgb01(s.fill) } : {}) };
    case "line":
    case "arrow":
      if (Math.hypot(g.end[0] - g.start[0], g.end[1] - g.start[1]) < MIN_DRAG) throw new Error("Drag to draw the line");
      return { ...base, points: [g.start, g.end] };
    case "freetext": {
      const rect = boxed ? r : clampRect([g.start[0], g.start[1], g.start[0] + 200, g.start[1] + 40], pageWidth, pageHeight);
      return { ...base, rect, font_size: s.fontSize, ...(s.fill ? { fill: hexToRgb01(s.fill) } : {}) };
    }
    case "callout": {
      // start = arrow tip; the text box is placed at the release point
      const box = clampRect([g.end[0], g.end[1] - 22, g.end[0] + 180, g.end[1] + 22], pageWidth, pageHeight);
      const tip = g.start;
      const kx = tip[0] < box[0] ? box[0] : tip[0] > box[2] ? box[2] : (box[0] + box[2]) / 2;
      const ky = tip[0] >= box[0] && tip[0] <= box[2] ? (tip[1] < box[1] ? box[1] : box[3]) : Math.min(Math.max(tip[1], box[1]), box[3]);
      if (tip[0] >= box[0] && tip[0] <= box[2] && tip[1] >= box[1] && tip[1] <= box[3]) {
        throw new Error("Drag from the point you want to call out to where the text box should go");
      }
      return { ...base, rect: box, callout: [tip, [kx, ky]], font_size: s.fontSize, ...(s.fill ? { fill: hexToRgb01(s.fill) } : {}) };
    }
    case "polygon": {
      const pts = g.path ?? [];
      if (pts.length < 3) throw new Error("A polygon needs at least 3 points");
      return { ...base, points: pts, ...(s.fill ? { fill: hexToRgb01(s.fill) } : {}) };
    }
    case "ink": {
      const pts = g.path ?? [];
      if (pts.length < 2) throw new Error("Draw a stroke");
      return { ...base, points: pts };
    }
    case "stamp": {
      const rect = boxed ? r : clampRect([g.end[0] - 80, g.end[1] - 25, g.end[0] + 80, g.end[1] + 25], pageWidth, pageHeight);
      return { page, type: "stamp", author: base.author, text: base.text, rect, stamp: s.stamp, opacity: s.opacity };
    }
    default:
      throw new Error(`Unsupported tool ${tool}`);
  }
}
