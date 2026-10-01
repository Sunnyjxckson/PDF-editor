"use client";

/**
 * "Edit text" mode overlay — Acrobat-style in-place text editing.
 *
 * Mount it INSIDE the element that wraps the rendered page image/canvas, as
 * that element's last child. The wrapper must be `position: relative` and
 * sized exactly like the rendered page (PageViewer's `relative shadow-xl`
 * div). The overlay fills it with `absolute inset-0`.
 *
 * Coordinates: the backend speaks PDF points (top-left origin, visible page
 * space). `pxPerPt` converts to the overlay's own CSS pixels BEFORE any CSS
 * transform, i.e. the rendered image's natural pixels per point
 * (PageViewer: PDF_SCALE = 150/72 in image mode, PDFJS_SCALE = 2 in pdf.js
 * mode). CSS zoom (`transform: scale(zoom)`) applied to an ancestor is
 * measured at drag time and needs no prop.
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type {
  CSSProperties, FocusEvent as ReactFocusEvent, KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
} from "react";
import {
  AlignCenter, AlignJustify, AlignLeft, AlignRight, Bold, Check, GripVertical, Italic,
  Loader2, Trash2, Type, X,
} from "lucide-react";
import {
  buildEditPayload, cssFontFamily, deleteText, editTextInPlace, getEditableText, initialDraft,
  isUpright, moveText, overflowChoices, ptRectToPx, pxDeltaToPt, rotBoxToCss, TextOverflowError,
  type Draft, type EditPayload, type EditResult, type EditablePage, type FamilyChoice,
  type OverflowInfo, type OverflowMode, type RotBox, type TargetKind,
  type TextAlign, type TextStyle, type TextTarget,
} from "@/lib/features/text_edit";

export type TextEditGranularity = "block" | "line" | "span";

export interface TextEditOverlayProps {
  docId: string;
  currentPage: number;
  /** Rendered pixels per PDF point in the overlay's un-zoomed layout space. */
  pxPerPt: number;
  /** Render nothing (and fetch nothing) when false. Default true. */
  active?: boolean;
  /** Bump to force a re-fetch (e.g. pass the store's pageVersion). */
  refreshKey?: number;
  /** Called after every successful edit/move/delete (re-render the page). */
  onDocumentChanged: () => void;
  /** Optional toast hook, signature-compatible with the store's addToast. */
  onMessage?: (message: string, type: "success" | "error" | "info") => void;
  /** Initial granularity. Default "block" (paragraph). */
  defaultGranularity?: TextEditGranularity;
}

interface Selectable {
  kind: TargetKind;
  id: string;
  bbox: number[];
  text: string;
  style: TextStyle;
  paragraph_text?: string;
  align?: TextAlign;
  line_height: number;
  editable: boolean;
  reason?: string;
  mixed: boolean;
  /** The text's own rotated box (absent on old servers / unknown geometry). */
  box?: RotBox;
}

/** Rotated box when the text is not upright, else null (use the bbox). */
function tilted(it: Selectable): RotBox | null {
  return it.box && !isUpright(it.box.angle) ? it.box : null;
}

/** Toast text for a successful edit, from the server's layout report. */
export function editResultMessage(res: Pick<EditResult, "pushed" | "overlap" | "overflow">): string | null {
  if (res.pushed) return `Moved the following text down to make room`;
  if (res.overlap) return "Text overlaps the text below (as requested)";
  if (res.overflow) return "Text did not fit in the original box; it extends below it";
  return null;
}

function flatten(page: EditablePage | null, gran: TextEditGranularity): Selectable[] {
  if (!page) return [];
  const out: Selectable[] = [];
  for (const b of page.blocks) {
    if (gran === "block") {
      out.push({
        kind: "block", id: b.id, bbox: b.bbox, text: b.text, style: b.style,
        paragraph_text: b.paragraph_text, align: b.align, line_height: b.line_height,
        editable: b.editable, reason: b.reason, mixed: b.mixed_styles, box: b.box,
      });
      continue;
    }
    for (const l of b.lines) {
      if (gran === "line") {
        const editable = l.editable ?? b.editable;
        out.push({
          kind: "line", id: l.id, bbox: l.bbox, text: l.text, style: l.style, line_height: 1.2,
          editable, reason: editable ? undefined : b.reason, mixed: l.spans.length > 1, box: l.box,
        });
        continue;
      }
      for (const s of l.spans) {
        if (!s.text.trim()) continue;
        const { id, bbox, text, box, ...style } = s;
        const editable = l.editable ?? b.editable;
        out.push({
          kind: "span", id, bbox, text, style: style as TextStyle, line_height: 1.2,
          editable, reason: editable ? undefined : b.reason, mixed: false, box,
        });
      }
    }
  }
  return out;
}

const GRAN_LABEL: Record<TextEditGranularity, string> = { block: "Paragraph", line: "Line", span: "Run" };
const ALIGN_ICON = { left: AlignLeft, center: AlignCenter, right: AlignRight, justify: AlignJustify };
const ALIGN_ORDER: TextAlign[] = ["left", "center", "right", "justify"];

export default function TextEditOverlay({
  docId, currentPage, pxPerPt, active = true, refreshKey = 0, onDocumentChanged, onMessage,
  defaultGranularity = "block",
}: TextEditOverlayProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [page, setPage] = useState<EditablePage | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [gran, setGran] = useState<TextEditGranularity>(defaultGranularity);
  const [hoverId, setHoverId] = useState<string | null>(null);
  const [editing, setEditing] = useState<Selectable | null>(null);
  const [original, setOriginal] = useState<Draft | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [drag, setDrag] = useState<{ x: number; y: number; dx: number; dy: number; zoom: number } | null>(null);
  const [reloadTick, setReloadTick] = useState(0);
  // a paragraph edit the server could not fit (422): offer shrink / overlap / cancel
  const [overflow, setOverflow] = useState<{ info: OverflowInfo; payload: EditPayload } | null>(null);
  const overflowRef = useRef<typeof overflow>(null);
  overflowRef.current = overflow;
  const committingRef = useRef(false);
  // An edit "session" is one open editor. Async handlers read state through
  // these refs, so a late blur/click from a closed editor can never resend.
  const sessionRef = useRef(0);
  const sessionCounter = useRef(0);
  const editingRef = useRef<Selectable | null>(null);
  const draftRef = useRef<Draft | null>(null);
  const originalRef = useRef<Draft | null>(null);
  editingRef.current = editing;
  draftRef.current = draft;
  originalRef.current = original;

  // ─── load ──────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!active || !docId) return;
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    getEditableText(docId, currentPage)
      .then((p) => { if (!cancelled) setPage(p); })
      .catch((e: unknown) => { if (!cancelled) setLoadError(e instanceof Error ? e.message : "Failed to load text"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [docId, currentPage, active, refreshKey, reloadTick]);

  // leaving the page / mode drops any open editor
  useEffect(() => {
    setEditing(null); setDraft(null); setOriginal(null); setError(null); setOverflow(null);
  }, [docId, currentPage, active]);

  const items = useMemo(() => flatten(page, gran), [page, gran]);

  // ─── editor lifecycle ─────────────────────────────────────────────────
  const close = useCallback(() => {
    sessionRef.current = 0;
    setEditing(null); setDraft(null); setOriginal(null); setError(null); setDrag(null); setOverflow(null);
  }, []);

  const open = (it: Selectable) => {
    if (!it.editable) {
      onMessage?.(it.reason || "This text cannot be edited in place", "info");
      return;
    }
    const d = initialDraft(it.kind, it);
    sessionRef.current = ++sessionCounter.current;
    setEditing(it); setOriginal(d); setDraft(d); setError(null);
  };

  useEffect(() => {
    if (editing && textareaRef.current) {
      const ta = textareaRef.current;
      ta.focus();
      ta.setSelectionRange(ta.value.length, ta.value.length);
    }
  }, [editing]);

  const target = (it: Selectable): TextTarget => ({ kind: it.kind, id: it.id, bbox: it.bbox });

  const afterChange = useCallback((msg?: string, res?: EditResult) => {
    onDocumentChanged();
    const layoutMsg = res ? editResultMessage(res) : null;
    if (layoutMsg) onMessage?.(layoutMsg, "info");
    else if (msg) onMessage?.(msg, "success");
    setReloadTick((t) => t + 1);
    close();
  }, [close, onDocumentChanged, onMessage]);

  const send = useCallback(async (payload: EditPayload) => {
    committingRef.current = true;
    setSaving(true); setError(null);
    try {
      const res = await editTextInPlace(docId, payload);
      afterChange(undefined, res);
    } catch (e: unknown) {
      if (e instanceof TextOverflowError) {
        // nothing was written: let the user pick how to resolve it
        setOverflow({ info: e.info, payload });
        return;
      }
      const msg = e instanceof Error ? e.message : "Text edit failed";
      setError(msg);
      onMessage?.(msg, "error");
      if (/changed; reload/i.test(msg)) setReloadTick((t) => t + 1);
    } finally {
      setSaving(false);
      committingRef.current = false;
    }
  }, [docId, afterChange, onMessage]);

  const commit = useCallback(async () => {
    const editing = editingRef.current, draft = draftRef.current, original = originalRef.current;
    if (!sessionRef.current || !editing || !draft || !original || committingRef.current) return;
    if (overflowRef.current) return; // waiting for the user's overflow choice
    const payload = buildEditPayload(currentPage, target(editing), original, draft);
    if (!payload) { close(); return; }
    await send(payload);
  }, [currentPage, close, send]);

  const resolveOverflow = (mode: OverflowMode | null) => {
    const pending = overflowRef.current;
    setOverflow(null);
    if (!pending || !mode) { textareaRef.current?.focus(); return; }
    void send({ ...pending.payload, overflow: mode });
  };

  const remove = async () => {
    if (!sessionRef.current || !editing || committingRef.current) return;
    committingRef.current = true;
    setSaving(true); setError(null);
    try {
      await deleteText(docId, { page: currentPage, target: target(editing) });
      afterChange("Text deleted");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Delete failed";
      setError(msg); onMessage?.(msg, "error");
    } finally {
      setSaving(false); committingRef.current = false;
    }
  };

  // ─── move by dragging the grip ────────────────────────────────────────
  const zoomFactor = () => {
    const el = rootRef.current;
    if (!el || !el.offsetWidth) return 1;
    return el.getBoundingClientRect().width / el.offsetWidth || 1;
  };

  const onGripDown = (e: ReactPointerEvent) => {
    if (!editing || editing.kind === "span" || saving) return;
    e.preventDefault();
    (e.target as Element).setPointerCapture?.(e.pointerId);
    setDrag({ x: e.clientX, y: e.clientY, dx: 0, dy: 0, zoom: zoomFactor() });
  };
  const onGripMove = (e: ReactPointerEvent) => {
    if (!drag) return;
    setDrag({ ...drag, dx: e.clientX - drag.x, dy: e.clientY - drag.y });
  };
  const onGripUp = async () => {
    if (!drag || !editing) return;
    const { dx, dy } = pxDeltaToPt(drag.dx, drag.dy, pxPerPt * drag.zoom);
    if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) { setDrag(null); return; }
    committingRef.current = true;
    setSaving(true); setError(null);
    try {
      await moveText(docId, { page: currentPage, target: target(editing), dx, dy });
      afterChange("Text moved");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Move failed";
      setError(msg); onMessage?.(msg, "error"); setDrag(null);
    } finally {
      setSaving(false); committingRef.current = false;
    }
  };

  // ─── keyboard / blur ──────────────────────────────────────────────────
  const onKeyDown = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Escape" && overflow) { e.preventDefault(); resolveOverflow(null); return; }
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    if (e.key === "Enter" && !(editing?.kind === "block" && e.shiftKey)) {
      e.preventDefault(); void commit(); return;
    }
    if ((e.metaKey || e.ctrlKey) && (e.key === "b" || e.key === "i") && draft) {
      e.preventDefault();
      setDraft(e.key === "b" ? { ...draft, bold: !draft.bold } : { ...draft, italic: !draft.italic });
    }
  };

  const onEditorBlur = (e: ReactFocusEvent<HTMLDivElement>) => {
    const next = e.relatedTarget as Node | null;
    if (next && editorRef.current?.contains(next)) return; // moving within the editor/format bar
    if (drag || overflowRef.current) return;
    void commit();
  };

  // auto-grow textarea
  useLayoutEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${ta.scrollHeight}px`;
  }, [draft?.text, draft?.size, editing]);

  if (!active) return null;

  // ─── render ───────────────────────────────────────────────────────────
  const editorBox = editing ? ptRectToPx(editing.bbox, pxPerPt) : null;
  const editTilt = editing ? tilted(editing) : null;
  const textStyle: CSSProperties | undefined = editing && draft ? {
    fontFamily: cssFontFamily(draft.family as FamilyChoice, editing.style.family),
    fontSize: `${draft.size * pxPerPt}px`,
    fontWeight: draft.bold ? 700 : 400,
    fontStyle: draft.italic ? "italic" : "normal",
    color: draft.color,
    lineHeight: editing.kind === "block" ? String(Math.max(editing.line_height, 1)) : "1.15",
    textAlign: editing.kind === "block" ? (draft.align === "justify" ? "justify" : draft.align) : "left",
  } : undefined;

  return (
    <div ref={rootRef} className="absolute inset-0 z-20" data-testid="text-edit-overlay">
      {/* mode chip */}
      <div className="absolute right-1 top-1 z-10 flex items-center gap-1 rounded-lg border border-gray-200 bg-white/95 p-0.5 text-xs shadow dark:border-gray-700 dark:bg-gray-800/95"
        onMouseDown={(e) => e.stopPropagation()}>
        <Type className="ml-1 h-3.5 w-3.5 text-blue-600 dark:text-blue-400" />
        {(Object.keys(GRAN_LABEL) as TextEditGranularity[]).map((g) => (
          <button key={g} type="button" onClick={() => { setGran(g); close(); }}
            className={`rounded px-2 py-0.5 ${gran === g
              ? "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300"
              : "text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700"}`}>
            {GRAN_LABEL[g]}
          </button>
        ))}
        {loading && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin text-gray-400" />}
        {loadError && <span className="mr-1 text-red-600 dark:text-red-400">{loadError}</span>}
      </div>

      {/* hover / click targets */}
      {items.map((it) => {
        if (editing && editing.id === it.id) return null;
        const tb = tilted(it);
        const r = ptRectToPx(it.bbox, pxPerPt);
        const hot = hoverId === it.id;
        const geom: CSSProperties = tb
          ? (({ left, top, width, height, transform, transformOrigin }) =>
              ({ left, top, width, height, transform, transformOrigin }))(rotBoxToCss(tb, pxPerPt, 1))
          : { left: r.left - 1, top: r.top - 1, width: r.width + 2, height: r.height + 2 };
        return (
          <div key={it.id} role="button" tabIndex={-1} data-testid={`text-target-${it.id}`}
            title={it.editable ? (it.mixed && it.kind === "block"
              ? "Click to edit (inline styles are kept for unchanged words)" : "Click to edit") : it.reason}
            onMouseEnter={() => setHoverId(it.id)} onMouseLeave={() => setHoverId((h) => (h === it.id ? null : h))}
            onMouseDown={(e) => e.stopPropagation()}
            onClick={(e) => { e.stopPropagation(); if (!editing) open(it); }}
            className={`absolute rounded-[2px] ${it.editable ? "cursor-text" : "cursor-not-allowed"} ${hot
              ? (it.editable ? "outline outline-2 outline-blue-500 bg-blue-500/10" : "outline outline-1 outline-dashed outline-gray-400")
              : "outline outline-1 outline-dashed outline-blue-300/50"}`}
            data-angle={tb ? tb.angle : undefined}
            style={geom} />
        );
      })}

      {/* inline editor */}
      {editing && draft && editorBox && (
        <div ref={editorRef} onBlur={onEditorBlur} onMouseDown={(e) => e.stopPropagation()}
          className="absolute" data-testid="text-edit-editor"
          style={{
            left: editorBox.left - 3 + (drag ? drag.dx / drag.zoom : 0),
            top: editorBox.top - 3 + (drag ? drag.dy / drag.zoom : 0),
            minWidth: Math.max(editorBox.width + 6, 60),
          }}>
          {/* floating format bar */}
          <div className={`absolute left-0 flex flex-wrap items-center gap-1 whitespace-nowrap rounded-lg border border-gray-200 bg-white p-1 text-xs text-gray-700 shadow-lg dark:border-gray-700 dark:bg-gray-800 dark:text-gray-200 ${editorBox.top < 44 ? "top-full mt-1" : "bottom-full mb-1"}`}
            onMouseDown={(e) => { if ((e.target as HTMLElement).tagName !== "SELECT" && (e.target as HTMLElement).tagName !== "INPUT") e.preventDefault(); }}>
            {editing.kind !== "span" && (
              <button type="button" aria-label="Drag to move" title="Drag to move"
                className="cursor-move rounded p-1 hover:bg-gray-100 dark:hover:bg-gray-700 touch-none"
                onPointerDown={onGripDown} onPointerMove={onGripMove} onPointerUp={onGripUp}>
                <GripVertical className="h-3.5 w-3.5" />
              </button>
            )}
            <select aria-label="Font family" value={draft.family}
              onChange={(e) => setDraft({ ...draft, family: e.target.value as FamilyChoice })}
              className="rounded border border-gray-300 bg-white px-1 py-0.5 dark:border-gray-600 dark:bg-gray-900"
              title={`Original: ${editing.style.font}`}>
              <option value="original">{editing.style.font.slice(0, 22) || "Original"}</option>
              <option value="sans">Sans (Helvetica)</option>
              <option value="serif">Serif (Times)</option>
              <option value="mono">Mono (Courier)</option>
            </select>
            <input aria-label="Font size" type="number" min={1} max={500} step={0.5}
              value={Math.round(draft.size * 100) / 100}
              onChange={(e) => { const v = parseFloat(e.target.value); if (v > 0) setDraft({ ...draft, size: v }); }}
              className="w-14 rounded border border-gray-300 bg-white px-1 py-0.5 dark:border-gray-600 dark:bg-gray-900" />
            <input aria-label="Text color" type="color" value={draft.color}
              onChange={(e) => setDraft({ ...draft, color: e.target.value })}
              className="h-6 w-6 cursor-pointer rounded border border-gray-300 bg-transparent p-0 dark:border-gray-600" />
            <button type="button" aria-label="Bold" aria-pressed={draft.bold}
              onClick={() => setDraft({ ...draft, bold: !draft.bold })}
              className={`rounded p-1 ${draft.bold ? "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300" : "hover:bg-gray-100 dark:hover:bg-gray-700"}`}>
              <Bold className="h-3.5 w-3.5" />
            </button>
            <button type="button" aria-label="Italic" aria-pressed={draft.italic}
              onClick={() => setDraft({ ...draft, italic: !draft.italic })}
              className={`rounded p-1 ${draft.italic ? "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300" : "hover:bg-gray-100 dark:hover:bg-gray-700"}`}>
              <Italic className="h-3.5 w-3.5" />
            </button>
            {editing.kind === "block" && (() => {
              const Icon = ALIGN_ICON[draft.align];
              return (
                <button type="button" aria-label={`Align: ${draft.align}`} title={`Align: ${draft.align}`}
                  onClick={() => setDraft({ ...draft, align: ALIGN_ORDER[(ALIGN_ORDER.indexOf(draft.align) + 1) % 4] })}
                  className="rounded p-1 hover:bg-gray-100 dark:hover:bg-gray-700">
                  <Icon className="h-3.5 w-3.5" />
                </button>
              );
            })()}
            <span className="mx-0.5 h-4 w-px bg-gray-200 dark:bg-gray-700" />
            <button type="button" aria-label="Delete text" title="Delete this text" onClick={() => void remove()}
              className="rounded p-1 text-red-600 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-900/30">
              <Trash2 className="h-3.5 w-3.5" />
            </button>
            <button type="button" aria-label="Cancel" title="Cancel (Esc)" onClick={close}
              className="rounded p-1 hover:bg-gray-100 dark:hover:bg-gray-700">
              <X className="h-3.5 w-3.5" />
            </button>
            <button type="button" aria-label="Apply" title={editing.kind === "block" ? "Apply (Enter, Shift+Enter = new line)" : "Apply (Enter)"}
              onClick={() => void commit()}
              className="rounded bg-blue-600 p-1 text-white hover:bg-blue-700">
              {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
            </button>
            {error && <span className="ml-1 max-w-[16rem] truncate text-red-600 dark:text-red-400" title={error}>{error}</span>}
          </div>

          {overflow && (() => {
            const ch = overflowChoices(overflow.info);
            return (
              <div role="alertdialog" aria-label="Text does not fit" data-testid="text-overflow-dialog"
                className={`absolute left-0 z-10 w-max max-w-[22rem] rounded-lg border border-amber-300 bg-amber-50 p-2 text-xs text-gray-800 shadow-lg dark:border-amber-700 dark:bg-gray-800 dark:text-gray-100 ${editorBox.top < 44 ? "top-full mt-10" : "bottom-full mb-10"}`}
                onMouseDown={(e) => e.preventDefault()}>
                <div className="mb-1 font-medium">The edited text does not fit here.</div>
                <div className="mb-2 text-gray-600 dark:text-gray-300">{ch.summary}; there is no room to move the text below down.</div>
                <div className="flex flex-wrap gap-1">
                  {ch.shrink && (
                    <button type="button" onClick={() => resolveOverflow("shrink")}
                      className="rounded bg-blue-600 px-2 py-1 text-white hover:bg-blue-700">{ch.shrink}</button>
                  )}
                  <button type="button" onClick={() => resolveOverflow("allow")}
                    className="rounded border border-gray-300 bg-white px-2 py-1 hover:bg-gray-100 dark:border-gray-600 dark:bg-gray-900 dark:hover:bg-gray-700">{ch.allow}</button>
                  <button type="button" onClick={() => resolveOverflow(null)}
                    className="rounded px-2 py-1 hover:bg-gray-100 dark:hover:bg-gray-700">{ch.cancel}</button>
                </div>
              </div>
            );
          })()}

          {(() => {
            const ta = (
              <textarea ref={textareaRef} aria-label="Edit text" value={draft.text} disabled={saving}
                spellCheck onKeyDown={onKeyDown}
                onChange={(e) => { setOverflow(null); setDraft({ ...draft, text: e.target.value }); }}
                rows={1}
                className="block w-full resize-none overflow-hidden rounded-sm bg-white p-[2px] outline outline-2 outline-blue-500 shadow-lg disabled:opacity-70"
                style={{
                  ...textStyle,
                  width: Math.max((editTilt ? editTilt.w * pxPerPt : editorBox.width) + 6, 60),
                  minHeight: (editTilt ? editTilt.h * pxPerPt : editorBox.height) + 6,
                }} />
            );
            if (!editTilt) return ta;
            // rotated text: the editor sits on the text's own box, at its angle
            const rc = rotBoxToCss(editTilt, pxPerPt, 3);
            return (
              <div data-testid="text-edit-rotated" className="absolute"
                style={{
                  left: rc.left - (editorBox.left - 3), top: rc.top - (editorBox.top - 3),
                  transform: rc.transform, transformOrigin: rc.transformOrigin,
                }}>
                {ta}
              </div>
            );
          })()}
        </div>
      )}
    </div>
  );
}
