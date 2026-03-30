import { create } from "zustand";
import type { DocumentInfo, TextBlock } from "./api";

export type Tool = "select" | "text" | "highlight" | "draw" | "eraser" | "region_select";
export type RenderMode = "image" | "pdfjs";

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
  currentPage: number;
  totalPages: number;
  zoom: number;
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
  setCurrentPage: (page: number) => void;
  setZoom: (zoom: number) => void;
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
  currentPage: 0,
  totalPages: 0,
  zoom: 1,
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

  toasts: [],

  textBlocks: [],

  regionSelection: null,

  optimisticEdits: [],

  setDocument: (doc, docId) =>
    set({ document: doc, docId, totalPages: doc.page_count, currentPage: 0, pageVersion: 0, pdfVersion: 0 }),
  setCurrentPage: (page) => set({ currentPage: page }),
  setZoom: (zoom) => set({ zoom: Math.max(0.25, Math.min(3, zoom)) }),
  setActiveTool: (tool) => set({ activeTool: tool }),
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
  reset: () =>
    set({
      document: null,
      docId: null,
      currentPage: 0,
      totalPages: 0,
      zoom: 1,
      activeTool: "select",
      pageVersion: 0,
      pdfVersion: 0,
      pdfBlobUrl: null,
      renderMode: "image",
      regionSelection: null,
      textBlocks: [],
      optimisticEdits: [],
    }),
}));
