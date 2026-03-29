import { create } from "zustand";
import type { DocumentInfo, TextBlock } from "./api";

export type Tool = "select" | "text" | "highlight" | "draw" | "eraser";

interface EditorState {
  // Document
  document: DocumentInfo | null;
  docId: string | null;
  currentPage: number;
  totalPages: number;
  zoom: number;
  pageVersion: number;

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

  // Text blocks for current page
  textBlocks: TextBlock[];

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
  setTextBlocks: (blocks: TextBlock[]) => void;
  bumpVersion: () => void;
  refreshDocument: () => void;
  reset: () => void;
}

export const useEditorStore = create<EditorState>((set, get) => ({
  document: null,
  docId: null,
  currentPage: 0,
  totalPages: 0,
  zoom: 1,
  pageVersion: 0,

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

  textBlocks: [],

  setDocument: (doc, docId) =>
    set({ document: doc, docId, totalPages: doc.page_count, currentPage: 0, pageVersion: 0 }),
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
  setTextBlocks: (blocks) => set({ textBlocks: blocks }),
  bumpVersion: () => set((s) => ({ pageVersion: s.pageVersion + 1 })),
  refreshDocument: () => {
    // Force re-fetch of document info
    set((s) => ({ pageVersion: s.pageVersion + 1 }));
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
      textBlocks: [],
    }),
}));
