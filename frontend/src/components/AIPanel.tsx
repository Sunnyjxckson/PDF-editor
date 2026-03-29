"use client";

import { useState } from "react";
import { X, Sparkles, FileText, CheckCheck, Database, AlignLeft, Hash } from "lucide-react";
import { useEditorStore } from "@/lib/store";
import { aiAssist } from "@/lib/api";

const actions = [
  { id: "summarize", label: "Summarize", icon: FileText, desc: "Get a summary of the page" },
  { id: "fix_grammar", label: "Fix Grammar", icon: CheckCheck, desc: "Clean up text formatting" },
  { id: "extract_data", label: "Extract Data", icon: Database, desc: "Find emails, phones, dates" },
  { id: "rewrite", label: "Clean Up", icon: AlignLeft, desc: "Reformat messy text" },
  { id: "word_count", label: "Word Count", icon: Hash, desc: "Count words and characters" },
];

export default function AIPanel() {
  const { docId, aiPanelOpen, setAiPanelOpen, currentPage } = useEditorStore();
  const [result, setResult] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [activeAction, setActiveAction] = useState("");

  if (!aiPanelOpen || !docId) return null;

  const handleAction = async (action: string) => {
    setLoading(true);
    setActiveAction(action);
    setResult(null);
    try {
      const res = await aiAssist(docId, { page: currentPage, action });
      setResult(res.result);
    } catch {
      setResult("Error: AI operation failed");
    }
    setLoading(false);
  };

  const renderResult = () => {
    if (loading) {
      return (
        <div className="flex items-center gap-2 text-sm text-gray-500">
          <div className="w-4 h-4 border-2 border-purple-500 border-t-transparent rounded-full animate-spin" />
          Processing...
        </div>
      );
    }
    if (result === null) return null;

    if (typeof result === "string") {
      return (
        <div className="mt-2 p-2 bg-gray-50 dark:bg-gray-800 rounded text-sm whitespace-pre-wrap max-h-48 overflow-y-auto">
          {result}
        </div>
      );
    }

    if (typeof result === "object") {
      const obj = result as Record<string, unknown>;
      return (
        <div className="mt-2 p-2 bg-gray-50 dark:bg-gray-800 rounded text-sm space-y-1 max-h-48 overflow-y-auto">
          {Object.entries(obj).map(([key, val]) => (
            <div key={key}>
              <span className="font-medium capitalize">{key}: </span>
              <span className="text-gray-600 dark:text-gray-400">
                {Array.isArray(val) ? (val.length > 0 ? val.join(", ") : "None found") : String(val)}
              </span>
            </div>
          ))}
        </div>
      );
    }

    return null;
  };

  return (
    <div className="absolute top-14 right-2 sm:right-4 z-50 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-xl shadow-2xl p-3 w-[calc(100%-1rem)] sm:w-80">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-1.5">
          <Sparkles className="w-4 h-4 text-purple-500" />
          <h3 className="text-sm font-semibold">AI Assist</h3>
        </div>
        <button onClick={() => setAiPanelOpen(false)} className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800">
          <X className="w-4 h-4" />
        </button>
      </div>

      <p className="text-xs text-gray-500 mb-2">
        Page {currentPage + 1} — Select an action:
      </p>

      <div className="grid grid-cols-2 gap-1.5">
        {actions.map((action) => (
          <button
            key={action.id}
            onClick={() => handleAction(action.id)}
            disabled={loading}
            className={`flex items-center gap-1.5 p-2 rounded-lg text-left text-xs transition-colors ${
              activeAction === action.id && loading
                ? "bg-purple-100 dark:bg-purple-900/40 text-purple-600"
                : "hover:bg-gray-100 dark:hover:bg-gray-800"
            } disabled:opacity-50`}
          >
            <action.icon className="w-3.5 h-3.5 shrink-0" />
            <div>
              <div className="font-medium">{action.label}</div>
              <div className="text-[10px] text-gray-400 hidden sm:block">{action.desc}</div>
            </div>
          </button>
        ))}
      </div>

      {renderResult()}
    </div>
  );
}
