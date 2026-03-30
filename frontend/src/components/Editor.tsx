"use client";

import { useEffect } from "react";
import { useEditorStore } from "@/lib/store";
import Toolbar from "./Toolbar";
import PageSidebar from "./PageSidebar";
import PageViewer from "./PageViewer";
import MobileBottomBar from "./MobileBottomBar";
import FindReplace from "./FindReplace";
import ChatPanel from "./ChatPanel";
import Toasts from "./Toasts";
import KeyboardShortcuts from "./KeyboardShortcuts";

export default function Editor() {
  const toggleChat = useEditorStore((s) => s.toggleChat);
  const setFindReplaceOpen = useEditorStore((s) => s.setFindReplaceOpen);
  const findReplaceOpen = useEditorStore((s) => s.findReplaceOpen);
  const setShortcutsOpen = useEditorStore((s) => s.setShortcutsOpen);
  const toggleSidebar = useEditorStore((s) => s.toggleSidebar);
  const setActiveTool = useEditorStore((s) => s.setActiveTool);
  const setCurrentPage = useEditorStore((s) => s.setCurrentPage);
  const currentPage = useEditorStore((s) => s.currentPage);
  const totalPages = useEditorStore((s) => s.totalPages);
  const zoom = useEditorStore((s) => s.zoom);
  const setZoom = useEditorStore((s) => s.setZoom);
  const chatPinned = useEditorStore((s) => s.chatPinned);
  const chatOpen = useEditorStore((s) => s.chatOpen);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Don't intercept when typing in inputs
      const tag = (e.target as HTMLElement)?.tagName;
      const isInput = tag === "INPUT" || tag === "TEXTAREA" || (e.target as HTMLElement)?.isContentEditable;

      // Ctrl/Cmd shortcuts work even in inputs
      if ((e.ctrlKey || e.metaKey) && e.key === "/") {
        e.preventDefault();
        toggleChat();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key === "f") {
        e.preventDefault();
        setFindReplaceOpen(!findReplaceOpen);
        return;
      }

      // Skip remaining shortcuts when in inputs
      if (isInput) return;

      switch (e.key) {
        case "?":
          e.preventDefault();
          setShortcutsOpen(true);
          break;
        case "[":
          e.preventDefault();
          toggleSidebar();
          break;
        case "v":
        case "V":
          setActiveTool("select");
          break;
        case "s":
        case "S":
          setActiveTool("region_select");
          break;
        case "t":
        case "T":
          setActiveTool("text");
          break;
        case "h":
        case "H":
          setActiveTool("highlight");
          break;
        case "d":
        case "D":
          setActiveTool("draw");
          break;
        case "e":
        case "E":
          setActiveTool("eraser");
          break;
        case "ArrowLeft":
          if (currentPage > 0) setCurrentPage(currentPage - 1);
          break;
        case "ArrowRight":
          if (currentPage < totalPages - 1) setCurrentPage(currentPage + 1);
          break;
        case "+":
        case "=":
          e.preventDefault();
          setZoom(zoom + 0.25);
          break;
        case "-":
          e.preventDefault();
          setZoom(zoom - 0.25);
          break;
        case "0":
          e.preventDefault();
          setZoom(1);
          break;
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [toggleChat, setFindReplaceOpen, findReplaceOpen, setShortcutsOpen, toggleSidebar, setActiveTool, setCurrentPage, currentPage, totalPages, zoom, setZoom]);

  return (
    <div className="h-dvh flex flex-col bg-gray-50 dark:bg-gray-950 text-gray-900 dark:text-gray-100">
      <div className="relative">
        <Toolbar />
        <FindReplace />
      </div>
      <div className="flex flex-1 overflow-hidden">
        <PageSidebar />
        <PageViewer />
        {chatOpen && chatPinned && <ChatPanel />}
      </div>
      <MobileBottomBar />
      {chatOpen && !chatPinned && <ChatPanel />}
      <Toasts />
      <KeyboardShortcuts />
    </div>
  );
}
