import { create } from "zustand";
import type { DocumentInfo, TextBlock } from "./api";
import { getDocumentInfo } from "./api";
import { DEFAULT_MARKUP_SETTINGS, type MarkupSettings } from "./features/organize";

/**
 * Page-level interaction modes. Exactly one is active at a time, and each one
 * owns the pointer on the page: the legacy annotate tools (select ... eraser)
 * are handled by PageViewer itself, the rest by a feature overlay.
 */
export type Tool =
  | "select" | "text" | "highlight" | "draw" | "eraser" | "region_select"
  | "edit_text" | "objects" | "comment" | "sign" | "forms" | "redact";
export type RenderMode = "image" | "pdfjs";

/**
 * Zoom is TRUE size: at zoom 1 (100%) one PDF point is 96/72 CSS px, i.e. 72pt =
 * 1 inch = 96 CSS px, so a US Letter page (612pt) is 816 CSS px wide.
 * "fit-width" / "fit-page" make the viewer recompute zoom from its own size.
 */
export const CSS_PX_PER_PT = 96 / 72;
export const ZOOM_MIN = 0.25;
export const ZOOM_MAX = 3;
export const ZOOM_STEPS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3];
export type ZoomMode = "custom" | "fit-width" | "fit-page";

export const clampZoom = (z: number) => Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, z));

/** Next/previous preset step from the current zoom (for Ctrl/Cmd +/-). */
export function stepZoom(zoom: number, dir: 1 | -1): number {
  const eps = 0.005;
  if (dir > 0) return ZOOM_STEPS.find((z) => z > zoom + eps) ?? ZOOM_MAX;
  return [...ZOOM_STEPS].reverse().find((z) => z < zoom - eps) ?? ZOOM_MIN;
}

/**
 * Zoom that fits a page (size in PDF points) into an area (CSS px).
 * fit-width uses only the width; fit-page fits both dimensions.
 */
export function computeFitZoom(
  mode: Exclude<ZoomMode, "custom">,
  pageWidthPt: number,
  pageHeightPt: number,
  availWidthPx: number,
  availHeightPx: number,
): number {
  if (pageWidthPt <= 0 || pageHeightPt <= 0 || availWidthPx <= 0) return 1;
  const byW = availWidthPx / (pageWidthPt * CSS_PX_PER_PT);
  if (mode === "fit-width" || availHeightPx <= 0) return clampZoom(byW);
  const byH = availHeightPx / (pageHeightPt * CSS_PX_PER_PT);
  return clampZoom(Math.min(byW, byH));
}

/** Right-hand side panels. sign/forms/redact are tied to the tool of the same name. */
export type SidePanel =
  | "sign" | "forms" | "redact" | "comments" | "bookmarks" | "convert" | "protect" | "tools";

/** Tools whose overlay needs a companion side panel. */
const TOOL_PANEL: Partial<Record<Tool, SidePanel>> = {
  sign: "sign",
  forms: "forms",
  redact: "redact",
  comment: "comments",
};
const PANEL_TOOLS = new Set<Tool>(["sign", "forms", "redact"]);
export interface Toast {
  id: string;
  message: string;
  type: "success" | "error" | "info";
}

export interface RegionSelection {
  page: number;
  rect: { x: number; y: number; width: number; height: number }; // PDF coordinates
  screenRect: { x: number; y: number; width: number; height: number }; // for rendering the overlay
}

// Optimistic update that can be reverted on failure
export interface OptimisticEdit {
  id: string;
  page: number;
  type: "text-edit" | "text-add" | "highlight" | "drawing";
  preview: unknown; // data for client-side preview
  pending: boolean;
}

interface EditorState {
  // Document
  document: DocumentInfo | null;
  docId: string | null;
  /** original upload name, used to name downloads */
  filename: string | null;
  currentPage: number;
  totalPages: number;
  zoom: number;
  /** custom = fixed zoom; fit-* = PageViewer recomputes zoom on resize / page change */
  zoomMode: ZoomMode;
  pageVersion: number;

  // Rendering
  renderMode: RenderMode;
  pdfBlobUrl: string | null; // URL of the downloaded PDF for pdf.js rendering
  pdfVersion: number; // tracks when to re-download the PDF for pdf.js

  // Tools
  activeTool: Tool;
  drawColor: string;
  drawWidth: number;
  highlightColor: string;
  fontSize: number;

  // UI
  sidebarOpen: boolean;
  mobileMenuOpen: boolean;
  findReplaceOpen: boolean;
  aiPanelOpen: boolean;
  propertiesPanelOpen: boolean;
  chatOpen: boolean;
  chatPinned: boolean;
  darkMode: boolean;
  shortcutsOpen: boolean;

  // Feature workspace
  activePanel: SidePanel | null;
  organizeOpen: boolean;
  headerFooterOpen: boolean;
  markupSettings: MarkupSettings;

  // Toasts
  toasts: Toast[];

  // Text blocks for current page
  textBlocks: TextBlock[];

  // Region selection for Select & Chat
  regionSelection: RegionSelection | null;

  // Optimistic edits
  optimisticEdits: OptimisticEdit[];

  // Actions
  setDocument: (doc: DocumentInfo, docId: string) => void;
  setFilename: (name: string | null) => void;
  setCurrentPage: (page: number) => void;
  /** Set an explicit zoom (switches zoomMode to "custom"). */
  setZoom: (zoom: number) => void;
  setZoomMode: (mode: ZoomMode) => void;
  /** Used by the viewer to apply a computed fit zoom without leaving fit mode. */
  applyFitZoom: (zoom: number) => void;
  zoomIn: () => void;
  zoomOut: () => void;
  setActiveTool: (tool: Tool) => void;
  setDrawColor: (color: string) => void;
  setDrawWidth: (width: number) => void;
  setHighlightColor: (color: string) => void;
  setFontSize: (size: number) => void;
  toggleSidebar: () => void;
  setSidebarOpen: (open: boolean) => void;
  toggleMobileMenu: () => void;
  setMobileMenuOpen: (open: boolean) => void;
  setFindReplaceOpen: (open: boolean) => void;
  setAiPanelOpen: (open: boolean) => void;
  setPropertiesPanelOpen: (open: boolean) => void;
  setChatOpen: (open: boolean) => void;
  toggleChat: () => void;
  setChatPinned: (pinned: boolean) => void;
  toggleDarkMode: () => void;
  setShortcutsOpen: (open: boolean) => void;
  addToast: (message: string, type?: Toast["type"]) => void;
  removeToast: (id: string) => void;
  setRegionSelection: (sel: RegionSelection | null) => void;
  setTextBlocks: (blocks: TextBlock[]) => void;
  setRenderMode: (mode: RenderMode) => void;
  setPdfBlobUrl: (url: string | null) => void;
  addOptimisticEdit: (edit: OptimisticEdit) => void;
  resolveOptimisticEdit: (id: string) => void;
  revertOptimisticEdit: (id: string) => void;
  bumpVersion: () => void;
  refreshDocument: () => void;
  /** Re-fetch /info (page count/sizes may have changed) and re-render everything. */
  reloadDocument: () => Promise<void>;
  setActivePanel: (panel: SidePanel | null) => void;
  togglePanel: (panel: SidePanel) => void;
  setOrganizeOpen: (open: boolean) => void;
  setHeaderFooterOpen: (open: boolean) => void;
  setMarkupSettings: (s: MarkupSettings) => void;
  reset: () => void;
}

function getInitialDarkMode(): boolean {
  if (typeof window === "undefined") return false;
  const stored = localStorage.getItem("pdf-editor-dark-mode");
  if (stored !== null) return stored === "true";
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export const useEditorStore = create<EditorState>((set, get) => ({
  document: null,
  docId: null,
  filename: null,
  currentPage: 0,
  totalPages: 0,
  zoom: 1,
  zoomMode: "custom",
  pageVersion: 0,

  renderMode: "image",
  pdfBlobUrl: null,
  pdfVersion: 0,

  activeTool: "select",
  drawColor: "#ff0000",
  drawWidth: 3,
  highlightColor: "#ffeb3b",
  fontSize: 12,

  sidebarOpen: true,
  mobileMenuOpen: false,
  findReplaceOpen: false,
  aiPanelOpen: false,
  propertiesPanelOpen: false,
  chatOpen: false,
  chatPinned: false,
  darkMode: getInitialDarkMode(),
  shortcutsOpen: false,

  activePanel: null,
  organizeOpen: false,
  headerFooterOpen: false,
  markupSettings: DEFAULT_MARKUP_SETTINGS,

  toasts: [],

  textBlocks: [],

  regionSelection: null,

  optimisticEdits: [],

  setDocument: (doc, docId) =>
    set((s) =>
      // Re-setting the SAME document (after a page op) must keep moving the
      // version counters forward: page images, thumbnails, text blocks and the
      // pdf.js document are all cached by version, so resetting to 0 would
      // serve stale renders from before the change.
      s.docId === docId
        ? {
            document: doc,
            totalPages: doc.page_count,
            currentPage: Math.max(0, Math.min(s.currentPage, doc.page_count - 1)),
            pageVersion: s.pageVersion + 1,
            pdfVersion: s.pdfVersion + 1,
          }
        : { document: doc, docId, totalPages: doc.page_count, currentPage: 0, pageVersion: 0, pdfVersion: 0 },
    ),
  setFilename: (filename) => set({ filename }),
  setCurrentPage: (page) => set({ currentPage: page }),
  setZoom: (zoom) => set({ zoom: clampZoom(zoom), zoomMode: "custom" }),
  setZoomMode: (zoomMode) => set({ zoomMode }),
  applyFitZoom: (zoom) => set((s) => (Math.abs(s.zoom - clampZoom(zoom)) < 1e-4 ? s : { zoom: clampZoom(zoom) })),
  zoomIn: () => set((s) => ({ zoom: stepZoom(s.zoom, 1), zoomMode: "custom" })),
  zoomOut: () => set((s) => ({ zoom: stepZoom(s.zoom, -1), zoomMode: "custom" })),
  setActiveTool: (tool) =>
    set((s) => {
      const next: Partial<EditorState> = { activeTool: tool, regionSelection: null };
      const panel = TOOL_PANEL[tool];
      if (panel) next.activePanel = panel;
      // Leaving sign/forms/redact closes its panel (and with it the overlay).
      else if (s.activePanel && PANEL_TOOLS.has(s.activePanel as Tool)) next.activePanel = null;
      if (tool === "comment" && !s.markupSettings.tool) {
        next.markupSettings = { ...s.markupSettings, tool: "highlight" };
      }
      return next;
    }),
  setDrawColor: (color) => set({ drawColor: color }),
  setDrawWidth: (width) => set({ drawWidth: width }),
  setHighlightColor: (color) => set({ highlightColor: color }),
  setFontSize: (size) => set({ fontSize: size }),
  toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
  setSidebarOpen: (open) => set({ sidebarOpen: open }),
  toggleMobileMenu: () => set((s) => ({ mobileMenuOpen: !s.mobileMenuOpen })),
  setMobileMenuOpen: (open) => set({ mobileMenuOpen: open }),
  setFindReplaceOpen: (open) => set({ findReplaceOpen: open }),
  setAiPanelOpen: (open) => set({ aiPanelOpen: open }),
  setPropertiesPanelOpen: (open) => set({ propertiesPanelOpen: open }),
  setChatOpen: (open) => set({ chatOpen: open }),
  toggleChat: () => set((s) => ({ chatOpen: !s.chatOpen })),
  setChatPinned: (pinned) => set({ chatPinned: pinned }),
  toggleDarkMode: () =>
    set((s) => {
      const next = !s.darkMode;
      if (typeof window !== "undefined") {
        localStorage.setItem("pdf-editor-dark-mode", String(next));
        document.documentElement.classList.toggle("dark", next);
      }
      return { darkMode: next };
    }),
  setShortcutsOpen: (open) => set({ shortcutsOpen: open }),
  addToast: (message, type = "info") => {
    const id = Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    set((s) => ({ toasts: [...s.toasts, { id, message, type }] }));
    setTimeout(() => get().removeToast(id), 4000);
  },
  removeToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
  setRegionSelection: (sel) => set({ regionSelection: sel }),
  setTextBlocks: (blocks) => set({ textBlocks: blocks }),
  setRenderMode: (mode) => set({ renderMode: mode }),
  setPdfBlobUrl: (url) => set({ pdfBlobUrl: url }),
  addOptimisticEdit: (edit) => set((s) => ({ optimisticEdits: [...s.optimisticEdits, edit] })),
  resolveOptimisticEdit: (id) => set((s) => ({ optimisticEdits: s.optimisticEdits.filter((e) => e.id !== id) })),
  revertOptimisticEdit: (id) => set((s) => ({ optimisticEdits: s.optimisticEdits.filter((e) => e.id !== id) })),
  bumpVersion: () => set((s) => ({ pageVersion: s.pageVersion + 1, pdfVersion: s.pdfVersion + 1 })),
  refreshDocument: () => {
    set((s) => ({ pageVersion: s.pageVersion + 1, pdfVersion: s.pdfVersion + 1 }));
  },
  reloadDocument: async () => {
    const { docId } = get();
    if (!docId) return;
    try {
      const info = await getDocumentInfo(docId);
      if (get().docId !== docId) return;
      get().setDocument(info, docId); // bumps versions for the same doc
    } catch {
      get().bumpVersion();
    }
  },
  setActivePanel: (panel) =>
    set((s) => {
      const next: Partial<EditorState> = { activePanel: panel };
      if (panel && PANEL_TOOLS.has(panel as Tool)) next.activeTool = panel as Tool;
      else if (PANEL_TOOLS.has(s.activeTool)) next.activeTool = "select";
      return next;
    }),
  togglePanel: (panel) => get().setActivePanel(get().activePanel === panel ? null : panel),
  setOrganizeOpen: (open) =>
    set((s) =>
      // The page overlays are gone while Organize replaces the viewer, so drop
      // a sign/forms/redact mode (and its panel) instead of leaving it dangling.
      open && PANEL_TOOLS.has(s.activeTool)
        ? { organizeOpen: true, activeTool: "select", activePanel: null }
        : { organizeOpen: open },
    ),
  setHeaderFooterOpen: (open) => set({ headerFooterOpen: open }),
  setMarkupSettings: (markupSettings) => set({ markupSettings }),
  reset: () =>
    set({
      document: null,
      docId: null,
      filename: null,
      currentPage: 0,
      totalPages: 0,
      zoom: 1,
      zoomMode: "custom",
      activeTool: "select",
      pageVersion: 0,
      pdfVersion: 0,
      pdfBlobUrl: null,
      renderMode: "image",
      regionSelection: null,
      textBlocks: [],
      optimisticEdits: [],
      activePanel: null,
      organizeOpen: false,
      headerFooterOpen: false,
    }),
}));
