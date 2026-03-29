"use client";

import {
  MousePointer2,
  Type,
  Highlighter,
  Pencil,
  Layers,
  Eraser,
} from "lucide-react";
import { useEditorStore, type Tool } from "@/lib/store";

const tools: { id: Tool; icon: typeof MousePointer2; label: string }[] = [
  { id: "select", icon: MousePointer2, label: "Select" },
  { id: "text", icon: Type, label: "Text" },
  { id: "highlight", icon: Highlighter, label: "Mark" },
  { id: "draw", icon: Pencil, label: "Draw" },
  { id: "eraser", icon: Eraser, label: "Erase" },
];

export default function MobileBottomBar() {
  const { activeTool, setActiveTool, sidebarOpen, toggleSidebar, docId } =
    useEditorStore();

  if (!docId) return null;

  return (
    <div className="sm:hidden fixed bottom-0 left-0 right-0 z-40 bg-white dark:bg-gray-900 border-t border-gray-200 dark:border-gray-800 safe-area-bottom">
      <div className="flex items-center justify-around py-1.5 px-1">
        <button
          onClick={toggleSidebar}
          className={`flex flex-col items-center gap-0.5 p-1.5 rounded-lg transition-colors ${
            sidebarOpen ? "text-blue-600 dark:text-blue-400" : "text-gray-500 dark:text-gray-400"
          }`}
        >
          <Layers className="w-5 h-5" />
          <span className="text-[10px]">Pages</span>
        </button>

        {tools.map((tool) => (
          <button
            key={tool.id}
            onClick={() => setActiveTool(tool.id)}
            className={`flex flex-col items-center gap-0.5 p-1.5 rounded-lg transition-colors ${
              activeTool === tool.id
                ? "text-blue-600 dark:text-blue-400"
                : "text-gray-500 dark:text-gray-400"
            }`}
          >
            <tool.icon className="w-5 h-5" />
            <span className="text-[10px]">{tool.label}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
