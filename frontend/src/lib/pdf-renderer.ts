import type * as PdfjsLibType from "pdfjs-dist";

// Lazy-load pdfjs-dist only on the client to avoid SSR crashes (DOMMatrix etc.)
let pdfjsLib: typeof PdfjsLibType | null = null;

async function getPdfjs(): Promise<typeof PdfjsLibType> {
  if (pdfjsLib) return pdfjsLib;
  const lib = await import("pdfjs-dist");
  lib.GlobalWorkerOptions.workerSrc = `//cdnjs.cloudflare.com/ajax/libs/pdf.js/${lib.version}/pdf.worker.min.mjs`;
  pdfjsLib = lib;
  return lib;
}

// ─── Types ────────────────────────────────────────────────────────────────────

export interface RenderResult {
  canvas: HTMLCanvasElement;
  width: number;
  height: number;
}

type CacheEntry = {
  blob: string; // object URL
  version: number;
  width: number;
  height: number;
};

// ─── Render Cache ─────────────────────────────────────────────────────────────
// Keyed by `${docId}-${pageNum}-${scale}-${version}`

const renderCache = new Map<string, CacheEntry>();
const MAX_CACHE_SIZE = 100;

function cacheKey(docId: string, pageNum: number, scale: number, version: number): string {
  return `${docId}-${pageNum}-${scale}-${version}`;
}

export function getCachedRender(docId: string, pageNum: number, scale: number, version: number): CacheEntry | undefined {
  return renderCache.get(cacheKey(docId, pageNum, scale, version));
}

function setCachedRender(docId: string, pageNum: number, scale: number, version: number, entry: CacheEntry): void {
  const key = cacheKey(docId, pageNum, scale, version);
  // Evict oldest entries if over limit
  if (renderCache.size >= MAX_CACHE_SIZE) {
    const firstKey = renderCache.keys().next().value;
    if (firstKey) {
      const old = renderCache.get(firstKey);
      if (old) URL.revokeObjectURL(old.blob);
      renderCache.delete(firstKey);
    }
  }
  renderCache.set(key, entry);
}

export function invalidateCache(docId: string): void {
  for (const [key, entry] of renderCache) {
    if (key.startsWith(docId)) {
      URL.revokeObjectURL(entry.blob);
      renderCache.delete(key);
    }
  }
}

// ─── Thumbnail Cache ──────────────────────────────────────────────────────────

const thumbnailCache = new Map<string, string>(); // key -> object URL
const MAX_THUMB_CACHE = 200;

function thumbKey(docId: string, pageNum: number, version: number): string {
  return `thumb-${docId}-${pageNum}-${version}`;
}

export function getCachedThumbnail(docId: string, pageNum: number, version: number): string | undefined {
  return thumbnailCache.get(thumbKey(docId, pageNum, version));
}

export function setCachedThumbnail(docId: string, pageNum: number, version: number, url: string): void {
  const key = thumbKey(docId, pageNum, version);
  if (thumbnailCache.size >= MAX_THUMB_CACHE) {
    const firstKey = thumbnailCache.keys().next().value;
    if (firstKey) {
      const old = thumbnailCache.get(firstKey);
      if (old) URL.revokeObjectURL(old);
      thumbnailCache.delete(firstKey);
    }
  }
  thumbnailCache.set(key, url);
}

// ─── Text Block Cache ─────────────────────────────────────────────────────────

import type { TextBlock } from "./api";

interface TextBlockCacheEntry {
  blocks: TextBlock[];
  version: number;
}

const textBlockCache = new Map<string, TextBlockCacheEntry>();

function textBlockKey(docId: string, pageNum: number): string {
  return `text-${docId}-${pageNum}`;
}

export function getCachedTextBlocks(docId: string, pageNum: number, version: number): TextBlock[] | undefined {
  const entry = textBlockCache.get(textBlockKey(docId, pageNum));
  if (entry && entry.version === version) return entry.blocks;
  return undefined;
}

export function setCachedTextBlocks(docId: string, pageNum: number, version: number, blocks: TextBlock[]): void {
  textBlockCache.set(textBlockKey(docId, pageNum), { blocks, version });
}

export function invalidateTextBlockCache(docId: string, pageNum?: number): void {
  if (pageNum !== undefined) {
    textBlockCache.delete(textBlockKey(docId, pageNum));
  } else {
    for (const key of textBlockCache.keys()) {
      if (key.startsWith(`text-${docId}`)) textBlockCache.delete(key);
    }
  }
}

// ─── PDF.js Document Management ───────────────────────────────────────────────

let currentPdfDoc: PdfjsLibType.PDFDocumentProxy | null = null;
let currentPdfDocId: string | null = null;
let currentPdfVersion: number = -1;
let loadingPromise: Promise<PdfjsLibType.PDFDocumentProxy> | null = null;

export async function loadPdfDocument(
  pdfUrl: string,
  docId: string,
  version: number,
): Promise<PdfjsLibType.PDFDocumentProxy> {
  // Return cached doc if same version
  if (currentPdfDoc && currentPdfDocId === docId && currentPdfVersion === version) {
    return currentPdfDoc;
  }

  // Avoid duplicate loads
  if (loadingPromise && currentPdfDocId === docId && currentPdfVersion === version) {
    return loadingPromise;
  }

  loadingPromise = (async () => {
    const lib = await getPdfjs();

    // Destroy old document
    if (currentPdfDoc) {
      currentPdfDoc.destroy();
    }

    const doc = await lib.getDocument({
      url: pdfUrl,
      cMapUrl: `//cdnjs.cloudflare.com/ajax/libs/pdf.js/${lib.version}/cmaps/`,
      cMapPacked: true,
    }).promise;

    currentPdfDoc = doc;
    currentPdfDocId = docId;
    currentPdfVersion = version;
    loadingPromise = null;

    return doc;
  })();

  return loadingPromise;
}

export function getCurrentPdfDoc(): PdfjsLibType.PDFDocumentProxy | null {
  return currentPdfDoc;
}

// ─── Page Rendering ───────────────────────────────────────────────────────────

export async function renderPage(
  doc: PdfjsLibType.PDFDocumentProxy,
  pageNum: number, // 0-indexed
  scale: number,
  targetCanvas: HTMLCanvasElement,
): Promise<{ width: number; height: number }> {
  const page = await doc.getPage(pageNum + 1); // pdf.js is 1-indexed
  const viewport = page.getViewport({ scale });

  targetCanvas.width = viewport.width;
  targetCanvas.height = viewport.height;

  const ctx = targetCanvas.getContext("2d");
  if (!ctx) throw new Error("Could not get canvas context");

  await page.render({ canvasContext: ctx, viewport, canvas: targetCanvas } as never).promise;

  return { width: viewport.width, height: viewport.height };
}

// Render a page and return it as a blob URL (for caching)
export async function renderPageToBlob(
  doc: PdfjsLibType.PDFDocumentProxy,
  pageNum: number,
  scale: number,
  docId: string,
  version: number,
): Promise<CacheEntry> {
  // Check cache first
  const cached = getCachedRender(docId, pageNum, scale, version);
  if (cached) return cached;

  const offscreen = document.createElement("canvas");
  const { width, height } = await renderPage(doc, pageNum, scale, offscreen);

  const blob = await new Promise<Blob>((resolve) => {
    offscreen.toBlob((b) => resolve(b!), "image/png");
  });
  const url = URL.createObjectURL(blob);

  const entry: CacheEntry = { blob: url, version, width, height };
  setCachedRender(docId, pageNum, scale, version, entry);

  return entry;
}

// ─── Pre-rendering ────────────────────────────────────────────────────────────

let preRenderAbort: AbortController | null = null;

export async function preRenderAdjacentPages(
  doc: PdfjsLibType.PDFDocumentProxy,
  currentPageNum: number,
  scale: number,
  docId: string,
  version: number,
): Promise<void> {
  // Cancel any in-flight pre-render
  if (preRenderAbort) preRenderAbort.abort();
  preRenderAbort = new AbortController();
  const signal = preRenderAbort.signal;

  const totalPages = doc.numPages;
  const pagesToPreRender = [currentPageNum - 1, currentPageNum + 1].filter(
    (p) => p >= 0 && p < totalPages,
  );

  for (const pageNum of pagesToPreRender) {
    if (signal.aborted) return;
    // Only pre-render if not already cached
    if (!getCachedRender(docId, pageNum, scale, version)) {
      try {
        await renderPageToBlob(doc, pageNum, scale, docId, version);
      } catch {
        // Silently ignore pre-render failures
      }
    }
  }
}

// ─── Cleanup ──────────────────────────────────────────────────────────────────

export function destroyPdfRenderer(): void {
  if (currentPdfDoc) {
    currentPdfDoc.destroy();
    currentPdfDoc = null;
    currentPdfDocId = null;
    currentPdfVersion = -1;
  }
  if (preRenderAbort) {
    preRenderAbort.abort();
    preRenderAbort = null;
  }
  // Clean up all cached object URLs
  for (const [, entry] of renderCache) {
    URL.revokeObjectURL(entry.blob);
  }
  renderCache.clear();
  for (const [, url] of thumbnailCache) {
    URL.revokeObjectURL(url);
  }
  thumbnailCache.clear();
  textBlockCache.clear();
}
