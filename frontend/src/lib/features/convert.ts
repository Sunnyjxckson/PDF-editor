// Typed client for backend/features/convert.py (OCR, export, create, compress).
// lib/api.ts does not export its base URL, so it is read the same way here.
const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function errorDetail(res: Response, fallback: string): Promise<string> {
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") return body.detail;
  } catch {
    /* non-JSON body */
  }
  return fallback;
}

// ─── OCR ─────────────────────────────────────────────────────────────────────

export interface OcrPageInfo {
  page: number;
  text_chars: number;
  ocr_text_chars: number;
  image_coverage: number;
  is_scanned: boolean;
  has_ocr_layer: boolean;
  needs_ocr: boolean;
}

export interface OcrDetectResult {
  pages: OcrPageInfo[];
  scanned_pages: number[];
  needs_ocr: number[];
}

export type OcrMode = "searchable" | "editable";

export interface OcrOptions {
  language?: string;
  /** 0-based page indexes; omit for "every page that needs OCR". */
  pages?: number[] | null;
  dpi?: number;
  mode?: OcrMode;
  force?: boolean;
}

export interface OcrJobResult {
  pages_processed: number[];
  words_added: number;
  skipped: number[];
  total: number;
}

export interface OcrJob {
  job_id: string;
  doc_id: string;
  status: "queued" | "running" | "done" | "error";
  done: number;
  total: number | null;
  progress: number;
  current_page?: number;
  result?: OcrJobResult;
  error?: string;
}

export async function getOcrLanguages(): Promise<string[]> {
  const res = await fetch(`${API_BASE}/api/pdf/ocr/languages`);
  if (!res.ok) throw new Error(await errorDetail(res, "Failed to load OCR languages"));
  return (await res.json()).languages;
}

export async function detectScannedPages(docId: string): Promise<OcrDetectResult> {
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/ocr/detect`);
  if (!res.ok) throw new Error(await errorDetail(res, "Scan detection failed"));
  return res.json();
}

export async function startOcr(docId: string, opts: OcrOptions = {}): Promise<string> {
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/ocr`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(opts),
  });
  if (!res.ok) throw new Error(await errorDetail(res, "Failed to start OCR"));
  return (await res.json()).job_id;
}

export async function getOcrJob(jobId: string): Promise<OcrJob> {
  const res = await fetch(`${API_BASE}/api/pdf/ocr/jobs/${jobId}`);
  if (!res.ok) throw new Error(await errorDetail(res, "Failed to read OCR progress"));
  return res.json();
}

/** Start OCR and poll until it finishes. Rejects on job error. */
export async function runOcr(
  docId: string,
  opts: OcrOptions,
  onProgress?: (job: OcrJob) => void,
  intervalMs = 500,
  signal?: AbortSignal,
): Promise<OcrJobResult> {
  const jobId = await startOcr(docId, opts);
  for (;;) {
    if (signal?.aborted) throw new Error("Cancelled");
    const job = await getOcrJob(jobId);
    onProgress?.(job);
    if (job.status === "done") return job.result as OcrJobResult;
    if (job.status === "error") throw new Error(job.error || "OCR failed");
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

// ─── Export ──────────────────────────────────────────────────────────────────

export type ExportFormat = "docx" | "txt" | "md" | "html" | "png" | "jpg" | "xlsx" | "csv";

export function getExportFormatUrl(
  docId: string,
  fmt: ExportFormat,
  opts: { dpi?: number; filename?: string } = {},
): string {
  const q = new URLSearchParams();
  if (opts.dpi) q.set("dpi", String(opts.dpi));
  if (opts.filename) q.set("filename", opts.filename);
  const qs = q.toString();
  return `${API_BASE}/api/pdf/${docId}/export/${fmt}${qs ? `?${qs}` : ""}`;
}

export function filenameFromDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback;
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header);
  return m ? decodeURIComponent(m[1]) : fallback;
}

export async function exportDocument(
  docId: string,
  fmt: ExportFormat,
  opts: { dpi?: number; filename?: string } = {},
): Promise<{ blob: Blob; filename: string }> {
  const res = await fetch(getExportFormatUrl(docId, fmt, opts));
  if (!res.ok) throw new Error(await errorDetail(res, `Export to ${fmt.toUpperCase()} failed`));
  const blob = await res.blob();
  const filename = filenameFromDisposition(res.headers.get("Content-Disposition"), `document.${fmt}`);
  return { blob, filename };
}

/** Trigger a browser download for a blob. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ─── Create ──────────────────────────────────────────────────────────────────

export type CreatePageSize = "letter" | "a4" | "fit";

export interface CreatedDocument {
  id: string;
  filename: string;
  page_count: number;
  metadata: Record<string, string>;
}

export const CREATE_ACCEPT =
  ".png,.jpg,.jpeg,.gif,.bmp,.tif,.tiff,.webp,.txt,.md,.markdown,.docx,.pdf";

export async function createPdfFromFiles(
  files: File[],
  opts: { pageSize?: CreatePageSize; filename?: string } = {},
): Promise<CreatedDocument> {
  if (!files.length) throw new Error("No files selected");
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  fd.append("page_size", opts.pageSize ?? "letter");
  if (opts.filename) fd.append("filename", opts.filename);
  const res = await fetch(`${API_BASE}/api/pdf/create`, { method: "POST", body: fd });
  if (!res.ok) throw new Error(await errorDetail(res, "Could not create PDF"));
  return res.json();
}

// ─── Compress ────────────────────────────────────────────────────────────────

export type CompressPreset = "high" | "balanced" | "smallest";

export interface CompressResult {
  preset: CompressPreset;
  before_bytes: number;
  after_bytes: number;
  optimized_bytes: number;
  saved_bytes: number;
  saved_percent: number;
  applied: boolean;
  dry_run: boolean;
  images_total: number;
  images_rewritten: number;
  fonts_subset: boolean;
  target_dpi: number;
  quality: number;
}

export async function compressDocument(
  docId: string,
  preset: CompressPreset,
  opts: { dryRun?: boolean; targetDpi?: number; quality?: number } = {},
): Promise<CompressResult> {
  const body: Record<string, unknown> = { preset, dry_run: !!opts.dryRun };
  if (opts.targetDpi) body.target_dpi = opts.targetDpi;
  if (opts.quality) body.quality = opts.quality;
  const res = await fetch(`${API_BASE}/api/pdf/${docId}/compress`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await errorDetail(res, "Compression failed"));
  return res.json();
}

export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "-";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}
