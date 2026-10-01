"use client";

/**
 * One-click AI actions. Each button queues a request in the AI store and
 * opens the chat panel, which streams the result (so progress, citations,
 * undo and review lists all work the same as in chat).
 */
import { useRef, useState } from "react";
import {
  X, Sparkles, FileText, ListOrdered, AlignLeft, MessageSquareText, PenLine, Scissors, CheckCheck,
  Languages, ShieldAlert, UserRound, FileInput, FormInput, GitCompare, Table, Braces, Paperclip, Settings2,
} from "lucide-react";
import { useEditorStore } from "@/lib/store";
import {
  useAIStore, uploadReferencePdf, ACTION_LABELS, SELECTION_ACTIONS, REFERENCE_ACTIONS, type AIActionId,
} from "@/lib/features/ai";
import AIProfileDialog from "./features/AIProfileDialog";

const LANGUAGES = ["Spanish", "French", "German", "Portuguese", "Italian", "Chinese (Simplified)", "Japanese", "Korean", "Arabic", "Hindi", "English"];

const GROUPS: Array<{ title: string; items: Array<{ id: AIActionId; icon: typeof FileText }> }> = [
  { title: "Understand", items: [
    { id: "summarize_short", icon: FileText },
    { id: "summarize_detailed", icon: AlignLeft },
    { id: "summarize_bullets", icon: ListOrdered },
    { id: "explain_selection", icon: MessageSquareText },
  ] },
  { title: "Edit in place", items: [
    { id: "rewrite_selection", icon: PenLine },
    { id: "shorten_selection", icon: Scissors },
    { id: "fix_grammar_selection", icon: CheckCheck },
    { id: "fix_grammar_page", icon: CheckCheck },
  ] },
  { title: "Forms", items: [
    { id: "autofill_profile", icon: UserRound },
    { id: "autofill_reference", icon: FileInput },
    { id: "generate_fields", icon: FormInput },
  ] },
  { title: "Protect & compare", items: [
    { id: "smart_redact", icon: ShieldAlert },
    { id: "compare_reference", icon: GitCompare },
  ] },
  { title: "Extract", items: [
    { id: "extract_tables_csv", icon: Table },
    { id: "extract_data_json", icon: Braces },
  ] },
];

function currentSelectionText(): string {
  if (typeof window === "undefined" || !window.getSelection) return "";
  return (window.getSelection()?.toString() ?? "").trim();
}

export default function AIPanel() {
  const { docId, aiPanelOpen, setAiPanelOpen, currentPage, regionSelection, setChatOpen, addToast } = useEditorStore();
  const queue = useAIStore((s) => s.queue);
  const references = useAIStore((s) => s.references);
  const addReference = useAIStore((s) => s.addReference);
  const [language, setLanguage] = useState("Spanish");
  const [profileOpen, setProfileOpen] = useState(false);
  const [selection, setSelection] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  if (!aiPanelOpen || !docId) return null;

  const hasSelection = !!selection || !!regionSelection;

  const run = (id: AIActionId, options?: Record<string, unknown>) => {
    const label = id === "translate_document" ? `Translate document to ${options?.language}` : ACTION_LABELS[id];
    queue({
      action: id,
      options,
      selectionText: SELECTION_ACTIONS.includes(id) ? selection || null : null,
      includeProfile: id === "autofill_profile",
      label,
    });
    setChatOpen(true);
    setAiPanelOpen(false);
  };

  const attach = async (file?: File) => {
    if (!file) return;
    try {
      const r = await uploadReferencePdf(file);
      addReference({ id: r.id, filename: r.filename });
      addToast(`Attached ${r.filename}`, "success");
    } catch (e) {
      addToast((e as Error).message, "error");
    }
  };

  return (
    <div
      data-testid="ai-panel"
      // Capture the text selection before the click moves focus and clears it.
      onMouseDownCapture={() => { const t = currentSelectionText(); if (t) setSelection(t); }}
      className="absolute top-14 right-2 sm:right-4 z-50 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-xl shadow-2xl p-3 w-[calc(100%-1rem)] sm:w-96 max-h-[80vh] overflow-y-auto"
    >
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-1.5">
          <Sparkles className="w-4 h-4 text-purple-500" />
          <h3 className="text-sm font-semibold">AI actions</h3>
        </div>
        <button onClick={() => setAiPanelOpen(false)} aria-label="Close AI actions" className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800">
          <X className="w-4 h-4" />
        </button>
      </div>
      <p className="text-[11px] text-gray-500 mb-2">
        Page {currentPage + 1}
        {hasSelection ? " · selection captured" : " · select text (or a region) for the selection actions"}
        {references.length ? ` · ${references.length} reference PDF${references.length > 1 ? "s" : ""}` : ""}
      </p>

      {GROUPS.map((g) => (
        <div key={g.title} className="mb-2">
          <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-1">{g.title}</div>
          <div className="grid grid-cols-2 gap-1.5">
            {g.items.map(({ id, icon: Icon }) => {
              const needsSel = SELECTION_ACTIONS.includes(id) && !hasSelection;
              const needsRef = REFERENCE_ACTIONS.includes(id) && references.length === 0;
              return (
                <button
                  key={id}
                  onClick={() => run(id)}
                  disabled={needsSel || needsRef}
                  title={needsSel ? "Select text or a region first" : needsRef ? "Attach a reference PDF first" : undefined}
                  className="flex items-center gap-1.5 p-2 rounded-lg text-left text-xs hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  <Icon className="w-3.5 h-3.5 shrink-0 text-purple-500" />
                  <span className="font-medium">{ACTION_LABELS[id]}</span>
                </button>
              );
            })}
          </div>
        </div>
      ))}

      <div className="mb-2">
        <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-1">Translate (keeps layout)</div>
        <div className="flex gap-1.5">
          <select aria-label="Target language" value={language} onChange={(e) => setLanguage(e.target.value)} className="flex-1 text-xs rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-2 py-1">
            {LANGUAGES.map((l) => <option key={l}>{l}</option>)}
          </select>
          <button onClick={() => run("translate_document", { language })} className="inline-flex items-center gap-1 text-xs px-2.5 py-1 rounded-md bg-purple-600 text-white hover:bg-purple-700">
            <Languages className="w-3.5 h-3.5" /> Translate
          </button>
        </div>
      </div>

      <div className="flex gap-1.5 pt-1 border-t border-gray-100 dark:border-gray-800">
        <button onClick={() => fileRef.current?.click()} className="flex-1 inline-flex items-center justify-center gap-1 text-xs px-2 py-1.5 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800">
          <Paperclip className="w-3.5 h-3.5" /> Attach reference PDF
        </button>
        <button onClick={() => setProfileOpen(true)} className="flex-1 inline-flex items-center justify-center gap-1 text-xs px-2 py-1.5 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800">
          <Settings2 className="w-3.5 h-3.5" /> My profile
        </button>
        <input ref={fileRef} type="file" accept="application/pdf" className="hidden" data-testid="ai-panel-reference-input"
          onChange={(e) => { void attach(e.target.files?.[0]); e.target.value = ""; }} />
      </div>
      <AIProfileDialog open={profileOpen} onClose={() => setProfileOpen(false)} />
    </div>
  );
}
