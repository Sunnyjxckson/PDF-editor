"use client";

import { Layers } from "lucide-react";
import { useEditorStore } from "@/lib/store";
import { MODES, activateMode, isModeActive } from "@/lib/modes";

// The most-used modes; the full list lives in the Toolbar's mobile menu.
const QUICK = ["select", "edit_text", "comment", "sign", "forms", "organize"];
const quickModes = MODES.filter((m) => QUICK.includes(m.id));

export default function MobileBottomBar() {
  const { activeTool, setActiveTool, sidebarOpen, toggleSidebar, docId, activePanel, organizeOpen, togglePanel, setOrganizeOpen } =
    useEditorStore();

  if (!docId) return null;
  return (
    <div className="sm:hidden fixed bottom-0 left-0 right-0 z-40 bg-white/95 dark:bg-gray-900/95 backdrop-blur-sm border-t border-gray-200 dark:border-gray-800 safe-area-bottom">
      <div className="flex items-center justify-around py-1.5 px-1">
        <button
          onClick={toggleSidebar}
          className={`flex flex-col items-center gap-0.5 p-1.5 rounded-lg transition-colors ${
            sidebarOpen
              ? "text-blue-600 dark:text-blue-400 bg-blue-50 dark:bg-blue-900/30"
              : "text-gray-500 dark:text-gray-400 active:bg-gray-100 dark:active:bg-gray-800"
          }`}
        >
          <Layers className="w-5 h-5" />
          <span className="text-[10px] font-medium">Pages</span>
        </button>

        {quickModes.map((tool) => (
          <button
            key={tool.id}
            onClick={() => activateMode(tool, { setActiveTool, togglePanel, setOrganizeOpen, organizeOpen })}
            className={`flex flex-col items-center gap-0.5 p-1.5 rounded-lg transition-colors ${
              isModeActive(tool, { activeTool, activePanel, organizeOpen })
                ? "text-blue-600 dark:text-blue-400 bg-blue-50 dark:bg-blue-900/30"
                : "text-gray-500 dark:text-gray-400 active:bg-gray-100 dark:active:bg-gray-800"
            }`}
          >
            <tool.icon className="w-5 h-5" />
            <span className="text-[10px] font-medium">{tool.label}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
