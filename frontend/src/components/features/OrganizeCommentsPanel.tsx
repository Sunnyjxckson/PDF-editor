"use client";

// Comments side panel. Lists every real PDF annotation comment (sticky notes,
// text markup, shapes, callouts, stamps, ink, including ones made in Acrobat or
// other tools) grouped by page, with replies (/IRT) and review status
// (/StateModel Review). Supports jump-to-page, reply, status, colour/opacity
// edit, text edit and delete.

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CheckCircle2, Highlighter, Loader2, MessageSquare, MessageSquareReply, Pencil, RefreshCw,
  Search, Shapes, Stamp, StickyNote, Strikethrough, Trash2, Type, Underline, X,
} from "lucide-react";
import {
  listComments, replyToComment, replyDepths, setCommentStatus, updateComment, deleteComment,
  groupCommentsByPage, filterComments, rgb01ToHex, hexToRgb01, REVIEW_STATUSES,
  type PdfComment, type ReviewStatus,
} from "@/lib/features/organize";

export interface OrganizeCommentsPanelProps {
  docId: string;
  /** 0-based page shown in the editor; its comments are highlighted. */
  currentPage: number;
  /** Jump the editor to a 0-based page. */
  onJumpToPage: (page: number) => void;
  /** Called after any change so the page image re-renders. */
  onDocumentChanged: () => void;
  /** Bump to make the panel re-fetch (e.g. after a markup was drawn or undo/redo). */
  refreshKey?: number;
  /** Author name stamped on replies and status changes. */
  author?: string;
  onClose?: () => void;
}

const ICONS: Record<string, typeof MessageSquare> = {
  note: StickyNote, highlight: Highlighter, underline: Underline, strikeout: Strikethrough, squiggly: Underline,
  freetext: Type, callout: Type, rect: Shapes, ellipse: Shapes, line: Shapes, arrow: Shapes,
  polygon: Shapes, polyline: Shapes, ink: Pencil, stamp: Stamp,
};

const input =
  "px-2 py-1 rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 " +
  "text-gray-800 dark:text-gray-200 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500";

function fmtDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export default function OrganizeCommentsPanel({
  docId, currentPage, onJumpToPage, onDocumentChanged, refreshKey = 0, author = "User", onClose,
}: OrganizeCommentsPanelProps) {
  const [comments, setComments] = useState<PdfComment[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [authorFilter, setAuthorFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [expanded, setExpanded] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setComments(await listComments(docId));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [docId]);

  useEffect(() => { load(); }, [load, refreshKey]);

  const mutate = useCallback(async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      onDocumentChanged();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [load, onDocumentChanged]);

  const authors = useMemo(() => Array.from(new Set(comments.map((c) => c.author).filter(Boolean))).sort(), [comments]);
  const types = useMemo(() => Array.from(new Set(comments.map((c) => c.type))).sort(), [comments]);
  const visible = useMemo(
    () => filterComments(comments, { query, author: authorFilter, type: typeFilter, status: statusFilter }),
    [comments, query, authorFilter, typeFilter, statusFilter],
  );
  const groups = useMemo(() => groupCommentsByPage(visible), [visible]);

  return (
    <div className="flex flex-col h-full w-full bg-white dark:bg-gray-900 border-l border-gray-200 dark:border-gray-800 text-xs">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-200 dark:border-gray-800">
        <MessageSquare className="w-4 h-4 text-blue-600" />
        <span className="font-semibold text-sm text-gray-800 dark:text-gray-100">Comments</span>
        <span className="text-gray-400">{visible.length}{visible.length !== comments.length ? ` of ${comments.length}` : ""}</span>
        <div className="flex-1" />
        <button onClick={load} className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-500" aria-label="Refresh comments">
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
        </button>
        {onClose && <button onClick={onClose} className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-500" aria-label="Close comments"><X className="w-4 h-4" /></button>}
      </div>

      <div className="px-3 py-2 space-y-1.5 border-b border-gray-200 dark:border-gray-800">
        <div className="relative">
          <Search className="w-3.5 h-3.5 absolute left-2 top-1.5 text-gray-400" />
          <input className={`${input} w-full pl-7`} placeholder="Search comments" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <div className="flex gap-1.5">
          <select className={`${input} flex-1 min-w-0`} value={authorFilter} onChange={(e) => setAuthorFilter(e.target.value)} aria-label="Filter by author">
            <option value="">All authors</option>
            {authors.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
          <select className={`${input} flex-1 min-w-0`} value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} aria-label="Filter by type">
            <option value="">All types</option>
            {types.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <select className={`${input} flex-1 min-w-0`} value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} aria-label="Filter by status">
            <option value="">Any status</option>
            {REVIEW_STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
      </div>

      {error && <div className="px-3 py-1.5 bg-red-50 dark:bg-red-950/40 text-red-700 dark:text-red-300">{error}</div>}

      <div className="flex-1 overflow-auto">
        {!loading && comments.length === 0 && (
          <p className="p-4 text-gray-500 dark:text-gray-400">No comments yet. Use the markup toolbar to add notes, highlights, shapes and callouts.</p>
        )}
        {groups.map((g) => (
          <div key={g.page}>
            <button
              onClick={() => onJumpToPage(g.page)}
              className={`sticky top-0 z-10 w-full text-left px-3 py-1 font-semibold border-b border-gray-100 dark:border-gray-800 ${
                g.page === currentPage ? "bg-blue-50 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300" : "bg-gray-50 dark:bg-gray-800/60 text-gray-600 dark:text-gray-300"}`}
            >
              Page {g.page + 1} <span className="font-normal text-gray-400">({g.comments.length})</span>
            </button>
            {g.comments.map((c) => (
              <CommentCard
                key={c.id} c={c} author={author}
                expanded={expanded === c.id}
                onToggle={() => { setExpanded(expanded === c.id ? null : c.id); onJumpToPage(c.page); }}
                onReply={(text) => mutate(() => replyToComment(docId, c.id, text, author))}
                onStatus={(s) => mutate(() => setCommentStatus(docId, c.id, s, author))}
                onEdit={(patch) => mutate(() => updateComment(docId, c.id, patch))}
                onDelete={() => { if (window.confirm("Delete this comment and its replies?")) mutate(() => deleteComment(docId, c.id)); }}
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

function CommentCard({ c, expanded, onToggle, onReply, onStatus, onEdit, onDelete }: {
  c: PdfComment; author: string; expanded: boolean;
  onToggle: () => void;
  onReply: (text: string) => void;
  onStatus: (s: ReviewStatus) => void;
  onEdit: (patch: { text?: string; color?: number[]; opacity?: number }) => void;
  onDelete: () => void;
}) {
  const [reply, setReply] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(c.contents);
  const Icon = ICONS[c.type] || MessageSquare;
  const depths = replyDepths(c); // replies are /IRT thread members, nested by what they answer
  const swatch = rgb01ToHex(c.color, "#facc15");

  return (
    <div data-testid={`comment-${c.id}`} className="px-3 py-2 border-b border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/40">
      <button onClick={onToggle} className="w-full text-left">
        <div className="flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ background: swatch, opacity: c.opacity }} />
          <Icon className="w-3.5 h-3.5 text-gray-500 shrink-0" />
          <span className="font-medium text-gray-800 dark:text-gray-100 truncate">{c.author || "Unknown"}</span>
          {c.status && c.status !== "None" && (
            <span className="inline-flex items-center gap-0.5 px-1.5 rounded-full bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300 text-[10px]">
              <CheckCircle2 className="w-3 h-3" />{c.status}
            </span>
          )}
          <span className="ml-auto text-[10px] text-gray-400 shrink-0">{fmtDate(c.modified || c.created)}</span>
        </div>
        <div className="mt-0.5 text-gray-500 dark:text-gray-400 text-[11px]">{c.subject || c.type}</div>
        {c.contents && !editing && <p className="mt-1 text-gray-700 dark:text-gray-200 whitespace-pre-wrap break-words">{c.contents}</p>}
        {c.replies.length > 0 && !expanded && (
          <p className="mt-1 text-[11px] text-blue-600 dark:text-blue-400">{c.replies.length} repl{c.replies.length === 1 ? "y" : "ies"}</p>
        )}
      </button>

      {expanded && (
        <div className="mt-2 space-y-2">
          {editing ? (
            <div className="space-y-1">
              <textarea className={`${input} w-full`} rows={3} value={draft} onChange={(e) => setDraft(e.target.value)} />
              <div className="flex gap-1">
                <button className="px-2 py-1 rounded bg-blue-600 text-white" onClick={() => { onEdit({ text: draft }); setEditing(false); }}>Save</button>
                <button className="px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800" onClick={() => { setDraft(c.contents); setEditing(false); }}>Cancel</button>
              </div>
            </div>
          ) : null}

          {c.replies.map((r) => (
            <div key={r.id} data-testid={`reply-${r.id}`} className="pl-2 border-l-2 border-gray-200 dark:border-gray-700"
              style={{ marginLeft: 12 * (depths[r.id] ?? 1) }}>
              <div className="flex gap-1.5 text-[11px]">
                <span className="font-medium text-gray-700 dark:text-gray-200">{r.author || "Unknown"}</span>
                <span className="text-gray-400">{fmtDate(r.created)}</span>
              </div>
              <p className="text-gray-700 dark:text-gray-300 whitespace-pre-wrap break-words">{r.contents}</p>
            </div>
          ))}

          <form
            className="flex gap-1"
            onSubmit={(e) => { e.preventDefault(); if (reply.trim()) { onReply(reply.trim()); setReply(""); } }}
          >
            <input className={`${input} flex-1`} placeholder="Reply..." value={reply} onChange={(e) => setReply(e.target.value)} />
            <button type="submit" disabled={!reply.trim()} className="p-1.5 rounded bg-blue-600 text-white disabled:opacity-40" aria-label="Send reply">
              <MessageSquareReply className="w-3.5 h-3.5" />
            </button>
          </form>

          <div className="flex flex-wrap items-center gap-1.5">
            <select className={input} value={c.status || "None"} onChange={(e) => onStatus(e.target.value as ReviewStatus)} aria-label="Review status">
              {REVIEW_STATUSES.map((s) => <option key={s} value={s}>{s === "None" ? "No status" : s}</option>)}
            </select>
            <input
              type="color" value={swatch} aria-label="Comment colour"
              className="w-7 h-6 rounded border border-gray-300 dark:border-gray-700"
              onChange={(e) => onEdit({ color: hexToRgb01(e.target.value) })}
            />
            <select className={input} value={String(Math.round((c.opacity ?? 1) * 100))} aria-label="Opacity"
              onChange={(e) => onEdit({ opacity: Number(e.target.value) / 100 })}>
              {[100, 80, 60, 40, 20].map((o) => <option key={o} value={o}>{o}%</option>)}
              {![100, 80, 60, 40, 20].includes(Math.round((c.opacity ?? 1) * 100)) && (
                <option value={String(Math.round((c.opacity ?? 1) * 100))}>{Math.round((c.opacity ?? 1) * 100)}%</option>
              )}
            </select>
            <button className="p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-300" onClick={() => setEditing(true)} aria-label="Edit text">
              <Pencil className="w-3.5 h-3.5" />
            </button>
            <button className="p-1.5 rounded hover:bg-red-50 dark:hover:bg-red-950/40 text-red-600" onClick={onDelete} aria-label="Delete comment">
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
