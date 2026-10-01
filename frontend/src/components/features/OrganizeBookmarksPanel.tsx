"use client";

// Bookmarks (PDF outline) editor: list, jump, add at current page, rename,
// retarget to current page, indent/outdent, delete (with children).

import { useCallback, useEffect, useState } from "react";
import { Bookmark as BookmarkIcon, ChevronLeft, ChevronRight, Crosshair, Loader2, Plus, Trash2, X } from "lucide-react";
import {
  addBookmark, deleteBookmark, listBookmarks, updateBookmark, type Bookmark,
} from "@/lib/features/organize";

export interface OrganizeBookmarksPanelProps {
  docId: string;
  /** 0-based page shown in the editor (used for "add here" / "set target"). */
  currentPage: number;
  onJumpToPage: (page: number) => void;
  onDocumentChanged: () => void;
  refreshKey?: number;
  onClose?: () => void;
}

const input =
  "px-1.5 py-0.5 rounded border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-800 dark:text-gray-200 text-xs";

export default function OrganizeBookmarksPanel({
  docId, currentPage, onJumpToPage, onDocumentChanged, refreshKey = 0, onClose,
}: OrganizeBookmarksPanelProps) {
  const [items, setItems] = useState<Bookmark[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newTitle, setNewTitle] = useState("");
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await listBookmarks(docId));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [docId]);

  useEffect(() => { load(); }, [load, refreshKey]);

  const mutate = async (fn: () => Promise<Bookmark[]>) => {
    try {
      setItems(await fn());
      setError(null);
      onDocumentChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="flex flex-col h-full w-full bg-white dark:bg-gray-900 border-l border-gray-200 dark:border-gray-800 text-xs">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-200 dark:border-gray-800">
        <BookmarkIcon className="w-4 h-4 text-blue-600" />
        <span className="font-semibold text-sm text-gray-800 dark:text-gray-100">Bookmarks</span>
        {loading && <Loader2 className="w-3.5 h-3.5 animate-spin text-gray-400" />}
        <div className="flex-1" />
        {onClose && <button onClick={onClose} className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-500" aria-label="Close bookmarks"><X className="w-4 h-4" /></button>}
      </div>
      <form
        className="flex gap-1 px-3 py-2 border-b border-gray-200 dark:border-gray-800"
        onSubmit={(e) => {
          e.preventDefault();
          const title = newTitle.trim();
          if (!title) return;
          const lvl = items.length ? Math.min(items[items.length - 1].level, 1) : 1;
          mutate(() => addBookmark(docId, { title, page: currentPage, level: lvl }));
          setNewTitle("");
        }}
      >
        <input className={`${input} flex-1`} placeholder={`New bookmark -> page ${currentPage + 1}`} value={newTitle} onChange={(e) => setNewTitle(e.target.value)} />
        <button type="submit" className="p-1 rounded bg-blue-600 text-white disabled:opacity-40" disabled={!newTitle.trim()} aria-label="Add bookmark"><Plus className="w-3.5 h-3.5" /></button>
      </form>
      {error && <div className="px-3 py-1.5 bg-red-50 dark:bg-red-950/40 text-red-700 dark:text-red-300">{error}</div>}
      <div className="flex-1 overflow-auto py-1">
        {!loading && !items.length && <p className="px-3 py-2 text-gray-500 dark:text-gray-400">No bookmarks.</p>}
        {items.map((b, i) => {
          const prevLevel = i > 0 ? items[i - 1].level : 0;
          return (
            <div key={`${b.index}-${b.title}`} className="group flex items-center gap-1 pr-2 hover:bg-gray-50 dark:hover:bg-gray-800/50" style={{ paddingLeft: 8 + (b.level - 1) * 14 }}>
              {editing === b.index ? (
                <form className="flex-1 flex gap-1 py-0.5" onSubmit={(e) => { e.preventDefault(); setEditing(null); if (draft.trim() && draft !== b.title) mutate(() => updateBookmark(docId, b.index, { title: draft.trim() })); }}>
                  <input autoFocus className={`${input} flex-1`} value={draft} onChange={(e) => setDraft(e.target.value)} onBlur={() => setEditing(null)} />
                </form>
              ) : (
                <button className="flex-1 min-w-0 text-left py-1 truncate text-gray-700 dark:text-gray-200" onClick={() => onJumpToPage(b.page)}
                  onDoubleClick={() => { setEditing(b.index); setDraft(b.title); }} title="Click to go, double-click to rename">
                  {b.title} <span className="text-gray-400">p.{b.page + 1}</span>
                </button>
              )}
              <div className="hidden group-hover:flex items-center gap-0.5 text-gray-500">
                <button disabled={b.level <= 1} className="p-0.5 disabled:opacity-30" title="Outdent" aria-label="Outdent"
                  onClick={() => mutate(() => updateBookmark(docId, b.index, { level: b.level - 1 }))}><ChevronLeft className="w-3.5 h-3.5" /></button>
                <button disabled={b.level > prevLevel} className="p-0.5 disabled:opacity-30" title="Indent" aria-label="Indent"
                  onClick={() => mutate(() => updateBookmark(docId, b.index, { level: b.level + 1 }))}><ChevronRight className="w-3.5 h-3.5" /></button>
                <button className="p-0.5" title={`Point at page ${currentPage + 1}`} aria-label="Set target to current page"
                  onClick={() => mutate(() => updateBookmark(docId, b.index, { page: currentPage }))}><Crosshair className="w-3.5 h-3.5" /></button>
                <button className="p-0.5 text-red-600" title="Delete (with children)" aria-label="Delete bookmark"
                  onClick={() => mutate(() => deleteBookmark(docId, b.index))}><Trash2 className="w-3.5 h-3.5" /></button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
