"use client";

// Bookmarks (PDF outline) editor: list, jump, add at current page (top level in
// page order by default, or as a child of the selected bookmark), rename,
// retarget to current page, nest/un-nest, drag-and-drop reorder, delete (with
// children). Every structural change moves a bookmark together with its children.

import { useCallback, useEffect, useState } from "react";
import { Bookmark as BookmarkIcon, ChevronLeft, ChevronRight, Crosshair, GripVertical, Loader2, Plus, Trash2, X } from "lucide-react";
import {
  addBookmark, bookmarkDropPosition, canDropBookmark, canIndentBookmark, deleteBookmark, indentBookmark,
  listBookmarks, moveBookmark, outdentBookmark, updateBookmark,
  type Bookmark, type BookmarkDropPosition,
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
  const [asChild, setAsChild] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [dragFrom, setDragFrom] = useState<number | null>(null);
  const [dropAt, setDropAt] = useState<{ index: number; pos: BookmarkDropPosition } | null>(null);

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
      setSelected(null);
      setError(null);
      onDocumentChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const selectedItem = selected != null ? items[selected] : undefined;

  const endDrag = () => { setDragFrom(null); setDropAt(null); };

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
        className="px-3 py-2 border-b border-gray-200 dark:border-gray-800 space-y-1"
        onSubmit={(e) => {
          e.preventDefault();
          const title = newTitle.trim();
          if (!title) return;
          // Default: top level, placed in page order (the backend decides the slot).
          const parent = asChild && selected != null ? selected : undefined;
          mutate(() => addBookmark(docId, parent != null ? { title, page: currentPage, parent } : { title, page: currentPage }));
          setNewTitle("");
        }}
      >
        <div className="flex gap-1">
          <input className={`${input} flex-1`} placeholder={`New bookmark -> page ${currentPage + 1}`} value={newTitle} onChange={(e) => setNewTitle(e.target.value)} />
          <button type="submit" className="p-1 rounded bg-blue-600 text-white disabled:opacity-40" disabled={!newTitle.trim()} aria-label="Add bookmark"><Plus className="w-3.5 h-3.5" /></button>
        </div>
        <label className={`flex items-center gap-1 ${selectedItem ? "text-gray-600 dark:text-gray-300" : "text-gray-400"}`}>
          <input type="checkbox" disabled={!selectedItem} checked={asChild && !!selectedItem} onChange={(e) => setAsChild(e.target.checked)} />
          {selectedItem ? <>Nest under &ldquo;{selectedItem.title}&rdquo;</> : "Select a bookmark to nest a new one under it"}
        </label>
      </form>
      {error && <div className="px-3 py-1.5 bg-red-50 dark:bg-red-950/40 text-red-700 dark:text-red-300">{error}</div>}
      <div className="flex-1 overflow-auto py-1" role="tree" aria-label="Bookmarks">
        {!loading && !items.length && <p className="px-3 py-2 text-gray-500 dark:text-gray-400">No bookmarks.</p>}
        {items.map((b, i) => {
          const drop = dropAt && dropAt.index === i ? dropAt.pos : null;
          return (
            <div
              key={`${b.index}-${b.title}`}
              data-testid={`bookmark-${i}`}
              role="treeitem"
              aria-level={b.level}
              aria-selected={selected === i}
              draggable={editing !== b.index}
              onDragStart={(e) => { setDragFrom(i); e.dataTransfer?.setData("text/plain", String(i)); if (e.dataTransfer) e.dataTransfer.effectAllowed = "move"; }}
              onDragOver={(e) => {
                if (dragFrom == null || !canDropBookmark(items, dragFrom, i)) return;
                e.preventDefault();
                const box = e.currentTarget.getBoundingClientRect();
                const pos = bookmarkDropPosition(e.clientY - box.top, box.height);
                if (!dropAt || dropAt.index !== i || dropAt.pos !== pos) setDropAt({ index: i, pos });
              }}
              onDragLeave={() => { if (dropAt?.index === i) setDropAt(null); }}
              onDrop={(e) => {
                e.preventDefault();
                const from = dragFrom;
                const pos = dropAt?.index === i ? dropAt.pos : "before";
                endDrag();
                if (from == null || from === i || !canDropBookmark(items, from, i)) return;
                mutate(() => moveBookmark(docId, from, i, pos));
              }}
              onDragEnd={endDrag}
              className={`group flex items-center gap-1 pr-2 border-y-2 ${selected === i ? "bg-blue-50 dark:bg-blue-950/40" : "hover:bg-gray-50 dark:hover:bg-gray-800/50"} ${
                drop === "before" ? "border-t-blue-500 border-b-transparent" : drop === "after" ? "border-b-blue-500 border-t-transparent" : "border-transparent"
              } ${drop === "inside" ? "ring-2 ring-inset ring-blue-400" : ""} ${dragFrom === i ? "opacity-40" : ""}`}
              style={{ paddingLeft: 4 + (b.level - 1) * 14 }}
            >
              <GripVertical className="w-3 h-3 text-gray-300 dark:text-gray-600 cursor-grab shrink-0" aria-hidden />
              {editing === b.index ? (
                <form className="flex-1 flex gap-1 py-0.5" onSubmit={(e) => { e.preventDefault(); setEditing(null); if (draft.trim() && draft !== b.title) mutate(() => updateBookmark(docId, b.index, { title: draft.trim() })); }}>
                  <input autoFocus className={`${input} flex-1`} value={draft} onChange={(e) => setDraft(e.target.value)} onBlur={() => setEditing(null)} />
                </form>
              ) : (
                <button className="flex-1 min-w-0 text-left py-1 truncate text-gray-700 dark:text-gray-200"
                  onClick={() => { setSelected(i); onJumpToPage(b.page); }}
                  onDoubleClick={() => { setEditing(b.index); setDraft(b.title); }} title="Click to go, double-click to rename, drag to move">
                  {b.title} {b.page >= 0 && <span className="text-gray-400">p.{b.page + 1}</span>}
                </button>
              )}
              <div className="hidden group-hover:flex items-center gap-0.5 text-gray-500">
                <button disabled={b.level <= 1} className="p-0.5 disabled:opacity-30" title="Un-nest (move out of parent)" aria-label="Outdent"
                  onClick={() => mutate(() => outdentBookmark(docId, b.index))}><ChevronLeft className="w-3.5 h-3.5" /></button>
                <button disabled={!canIndentBookmark(items, i)} className="p-0.5 disabled:opacity-30" title="Nest under the bookmark above" aria-label="Indent"
                  onClick={() => mutate(() => indentBookmark(docId, b.index))}><ChevronRight className="w-3.5 h-3.5" /></button>
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
