"use client";

import { useEffect } from "react";
import { useEditorStore } from "@/lib/store";
import { modeForShortcut, activateMode } from "@/lib/modes";
import { performUndo, performRedo } from "@/lib/history";
import { useFormsStore } from "@/lib/features/forms";
import Toolbar from "./Toolbar";
import ToolRail from "./ToolRail";
import PageSidebar from "./PageSidebar";
import PageViewer from "./PageViewer";
import SidePanel from "./SidePanel";
import MobileBottomBar from "./MobileBottomBar";
import FindReplace from "./FindReplace";
import ChatPanel from "./ChatPanel";
import AIPanel from "./AIPanel";
import Toasts from "./Toasts";
import SignedDocGuard from "./SignedDocGuard";
import KeyboardShortcuts from "./KeyboardShortcuts";
import OrganizeView from "./features/OrganizeView";
import OrganizeHeaderFooterDialog from "./features/OrganizeHeaderFooterDialog";
import OrganizeMarkupToolbar from "./features/OrganizeMarkupToolbar";

function isTypingTarget(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

export default function Editor() {
  const toggleChat = useEditorStore((s) => s.toggleChat);
  const setFindReplaceOpen = useEditorStore((s) => s.setFindReplaceOpen);
  const findReplaceOpen = useEditorStore((s) => s.findReplaceOpen);
  const setShortcutsOpen = useEditorStore((s) => s.setShortcutsOpen);
  const toggleSidebar = useEditorStore((s) => s.toggleSidebar);
  const setActiveTool = useEditorStore((s) => s.setActiveTool);
  const activeTool = useEditorStore((s) => s.activeTool);
  const togglePanel = useEditorStore((s) => s.togglePanel);
  const setCurrentPage = useEditorStore((s) => s.setCurrentPage);
  const currentPage = useEditorStore((s) => s.currentPage);
  const totalPages = useEditorStore((s) => s.totalPages);
  const setZoom = useEditorStore((s) => s.setZoom);
  const chatPinned = useEditorStore((s) => s.chatPinned);
  const chatOpen = useEditorStore((s) => s.chatOpen);
  const docId = useEditorStore((s) => s.docId);
  const docInfo = useEditorStore((s) => s.document);
  const filename = useEditorStore((s) => s.filename);
  const pageVersion = useEditorStore((s) => s.pageVersion);
  const organizeOpen = useEditorStore((s) => s.organizeOpen);
  const setOrganizeOpen = useEditorStore((s) => s.setOrganizeOpen);
  const headerFooterOpen = useEditorStore((s) => s.headerFooterOpen);
  const setHeaderFooterOpen = useEditorStore((s) => s.setHeaderFooterOpen);
  const markupSettings = useEditorStore((s) => s.markupSettings);
  const setMarkupSettings = useEditorStore((s) => s.setMarkupSettings);
  const reloadDocument = useEditorStore((s) => s.reloadDocument);

  // Forms "prepare" mode captures the page pointer; it must not outlive the Forms tool.
  useEffect(() => {
    if (activeTool !== "forms") useFormsStore.getState().setPrepareMode(false);
  }, [activeTool]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // A feature overlay (forms nudge, objects delete...) already handled it.
      if (e.defaultPrevented) return;
      const isInput = isTypingTarget(e.target);
      const mod = e.ctrlKey || e.metaKey;

      // Ctrl/Cmd shortcuts that work even in inputs
      if (mod && e.key === "/") {
        e.preventDefault();
        toggleChat();
        return;
      }
      if (mod && (e.key === "f" || e.key === "F") && !e.shiftKey) {
        e.preventDefault();
        setFindReplaceOpen(!findReplaceOpen);
        return;
      }

      // Skip remaining shortcuts when typing (native undo in text fields stays native)
      if (isInput) return;

      if (mod && !e.altKey) {
        const k = e.key.toLowerCase();
        if (k === "z" && !e.shiftKey) {
          e.preventDefault();
          void performUndo();
          return;
        }
        if ((k === "z" && e.shiftKey) || k === "y") {
          e.preventDefault();
          void performRedo();
          return;
        }
        return; // leave other Ctrl/Cmd combos (copy, print...) to the browser
      }
      if (e.altKey) return;

      const mode = modeForShortcut(e.key);
      if (mode) {
        e.preventDefault();
        activateMode(mode, { setActiveTool, togglePanel, setOrganizeOpen, organizeOpen });
        return;
      }

      switch (e.key) {
        case "?":
          e.preventDefault();
          setShortcutsOpen(true);
          break;
        case "[":
          e.preventDefault();
          toggleSidebar();
          break;
        case "ArrowLeft":
        case "PageUp":
          if (organizeOpen) break;
          if (currentPage > 0) setCurrentPage(currentPage - 1);
          break;
        case "ArrowRight":
        case "PageDown":
          if (organizeOpen) break;
          if (currentPage < totalPages - 1) setCurrentPage(currentPage + 1);
          break;
        case "+":
        case "=":
          e.preventDefault();
          useEditorStore.getState().zoomIn();
          break;
        case "-":
          e.preventDefault();
          useEditorStore.getState().zoomOut();
          break;
        case "0":
          e.preventDefault();
          setZoom(1);
          break;
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [toggleChat, setFindReplaceOpen, findReplaceOpen, setShortcutsOpen, toggleSidebar, setActiveTool, togglePanel, setOrganizeOpen, organizeOpen, setCurrentPage, currentPage, totalPages, setZoom]);

  const changed = () => { void reloadDocument(); };
  const firstPage = docInfo?.pages[0];

  return (
    <div className="h-dvh flex flex-col bg-gray-50 dark:bg-gray-950 text-gray-900 dark:text-gray-100">
      <div className="relative">
        <Toolbar />
        <FindReplace />
        <AIPanel />
      </div>
      {activeTool === "comment" && !organizeOpen && docId && (
        <OrganizeMarkupToolbar settings={markupSettings} onChange={setMarkupSettings} docId={docId} />
      )}
      <div className="flex flex-1 overflow-hidden">
        <ToolRail />
        {organizeOpen && docId ? (
          <div className="flex-1 overflow-hidden">
            <OrganizeView
              docId={docId}
              pageCount={totalPages}
              currentPage={currentPage}
              onDocumentChanged={changed}
              onPageSelect={(p) => { setCurrentPage(p); setOrganizeOpen(false); }}
              onClose={() => setOrganizeOpen(false)}
              version={pageVersion}
              filename={filename ?? undefined}
            />
          </div>
        ) : (
          <>
            <PageSidebar />
            <PageViewer />
          </>
        )}
        <SidePanel />
        {chatOpen && chatPinned && <ChatPanel />}
      </div>
      <MobileBottomBar />
      {chatOpen && !chatPinned && <ChatPanel />}
      {docId && (
        <OrganizeHeaderFooterDialog
          docId={docId}
          pageCount={totalPages}
          open={headerFooterOpen}
          onClose={() => setHeaderFooterOpen(false)}
          onDocumentChanged={changed}
          pageWidth={firstPage?.width}
          pageHeight={firstPage?.height}
        />
      )}
      <Toasts />
      <SignedDocGuard />
      <KeyboardShortcuts />
    </div>
  );
}
