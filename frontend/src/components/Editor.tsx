"use client";

import { useEffect } from "react";
import { useEditorStore } from "@/lib/store";
import Toolbar from "./Toolbar";
import PageSidebar from "./PageSidebar";
import PageViewer from "./PageViewer";
import MobileBottomBar from "./MobileBottomBar";
import FindReplace from "./FindReplace";
import AIPanel from "./AIPanel";
import ChatPanel from "./ChatPanel";

export default function Editor() {
  const toggleChat = useEditorStore((s) => s.toggleChat);

  // Keyboard shortcut: Ctrl+/ to toggle chat
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "/") {
        e.preventDefault();
        toggleChat();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [toggleChat]);

  return (
    <div className="h-dvh flex flex-col bg-gray-50 dark:bg-gray-950 text-gray-900 dark:text-gray-100">
      <div className="relative">
        <Toolbar />
        <FindReplace />
        <AIPanel />
      </div>
      <div className="flex flex-1 overflow-hidden">
        <PageSidebar />
        <PageViewer />
      </div>
      <MobileBottomBar />
      <ChatPanel />
    </div>
  );
}
