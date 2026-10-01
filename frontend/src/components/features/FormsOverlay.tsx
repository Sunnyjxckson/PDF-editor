"use client";

/**
 * FormsOverlay — draws the current page's form fields on top of the rendered page.
 *
 *  - Fill mode: fields are highlighted; click a text/dropdown/list field to edit it in place,
 *    click a checkbox/radio to toggle it.  Commits go straight to the PDF.
 *  - Prepare mode (toggled in FormsPanel): drag on empty page area to create a field of the
 *    chosen type; click to select; drag to move; corner/edge handles to resize; Delete/Backspace
 *    removes the selected widget; Escape deselects.
 *
 * Props:
 *   docId, currentPage, onDocumentChanged  as for FormsPanel
 *   pageWidth, pageHeight                  the page size in PDF points AS DISPLAYED (rotation applied),
 *                                          e.g. DocumentInfo.pages[i].width/height for unrotated pages
 *                                          (swap them when rotation is 90/270)
 *   refreshKey?                            bump to reload fields (e.g. pageVersion)
 *
 * Mount: inside PageViewer's page wrapper (the `relative` div that holds the <img>/<canvas> and is
 * CSS-scaled by zoom), as a sibling AFTER the drawing canvas:
 *     <FormsOverlay docId=... currentPage=... pageWidth=... pageHeight=... onDocumentChanged=... />
 * It is `absolute inset-0`, positions fields in percentages and converts mouse positions with the
 * element's on-screen bounding box, so it is correct at any zoom / render DPI without a scale prop.
 * In fill mode the container is pointer-events:none (only the field boxes are clickable), so the
 * viewer's own tools keep working everywhere else.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  useFormsStore, createFormField, updateFormField, deleteFormField, fillFormFields,
  rectToPercentStyle, clientToPoints, applyDrag, normalizeRect, defaultRectAt, clampRect,
  type FormField, type Rect, type Handle, type FieldValue,
} from "@/lib/features/forms";

export interface FormsOverlayProps {
  docId: string;
  currentPage: number;
  pageWidth: number;
  pageHeight: number;
  onDocumentChanged: () => void;
  refreshKey?: number;
}

type Interaction =
  | { kind: "create"; start: [number, number]; current: [number, number] }
  | { kind: "edit"; id: number; handle: Handle; start: [number, number]; orig: Rect; current: Rect };

const HANDLES: Handle[] = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
const HANDLE_POS: Record<string, string> = {
  nw: "-left-1 -top-1 cursor-nwse-resize",
  n: "left-1/2 -translate-x-1/2 -top-1 cursor-ns-resize",
  ne: "-right-1 -top-1 cursor-nesw-resize",
  e: "-right-1 top-1/2 -translate-y-1/2 cursor-ew-resize",
  se: "-right-1 -bottom-1 cursor-nwse-resize",
  s: "left-1/2 -translate-x-1/2 -bottom-1 cursor-ns-resize",
  sw: "-left-1 -bottom-1 cursor-nesw-resize",
  w: "-left-1 top-1/2 -translate-y-1/2 cursor-ew-resize",
};

export default function FormsOverlay({ docId, currentPage, pageWidth, pageHeight, onDocumentChanged, refreshKey }: FormsOverlayProps) {
  const { fields, prepareMode, createType, selectedId, select, refresh, docId: storeDoc, upsertLocal } = useFormsStore();
  const ref = useRef<HTMLDivElement>(null);
  const [inter, setInterState] = useState<Interaction | null>(null);
  // Mirror of `inter` so the window mouseup handler sees the latest drag state.
  const interRef = useRef<Interaction | null>(null);
  const setInter = (next: Interaction | null | ((cur: Interaction | null) => Interaction | null)) => {
    const value = typeof next === "function" ? next(interRef.current) : next;
    interRef.current = value;
    setInterState(value);
  };
  const [editing, setEditing] = useState<{ field: FormField; draft: string } | null>(null);
  const [pending, setPending] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const page = { width: pageWidth, height: pageHeight };

  useEffect(() => {
    if (storeDoc !== docId || refreshKey !== undefined) refresh(docId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId, refreshKey]);

  useEffect(() => setEditing(null), [currentPage, prepareMode]);

  const pageFields = fields.filter((f) => f.page === currentPage);

  const toPts = useCallback(
    (e: { clientX: number; clientY: number }): [number, number] => {
      const el = ref.current;
      if (!el) return [0, 0];
      return clientToPoints(e.clientX, e.clientY, el.getBoundingClientRect(), { width: pageWidth, height: pageHeight });
    },
    [pageWidth, pageHeight],
  );

  const mutate = async (fn: () => Promise<unknown>) => {
    setPending(true);
    setErr(null);
    try {
      await fn();
      onDocumentChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      await refresh(docId);
      setPending(false);
    }
  };

  // ── global drag listeners ──
  useEffect(() => {
    if (!inter) return;
    const move = (e: MouseEvent) => {
      const p = toPts(e);
      setInter((cur) => {
        if (!cur) return cur;
        if (cur.kind === "create") return { ...cur, current: p };
        const next = applyDrag(cur.orig, cur.handle, p[0] - cur.start[0], p[1] - cur.start[1], page);
        return { ...cur, current: next };
      });
    };
    const up = () => {
      const cur = interRef.current;
      setInter(null);
      if (!cur) return;
      if (cur.kind === "create") {
        const [x0, y0] = cur.start;
        const [x1, y1] = cur.current;
        const dragged = Math.abs(x1 - x0) > 4 && Math.abs(y1 - y0) > 4;
        const rect = dragged ? clampRect(normalizeRect([x0, y0, x1, y1]), page) : defaultRectAt(createType, x0, y0, page);
        mutate(async () => {
          const res = await createFormField(docId, {
            page: currentPage,
            type: createType,
            rect: rect.map((v) => Math.round(v * 100) / 100) as Rect,
            ...(createType === "combo" || createType === "list" ? { options: ["Option 1", "Option 2", "Option 3"] } : {}),
          });
          upsertLocal(res.field);
          select(res.field.id);
        });
      } else {
        const moved = cur.current.some((v, i) => Math.abs(v - cur.orig[i]) > 0.5);
        if (moved) {
          mutate(() => updateFormField(docId, cur.id, { rect: cur.current.map((v) => Math.round(v * 100) / 100) as Rect }));
        }
      }
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up, { once: true });
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inter?.kind, toPts]);

  // ── keyboard: Delete / Escape / arrow nudge in prepare mode ──
  useEffect(() => {
    if (!prepareMode) return;
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (e.target as HTMLElement)?.isContentEditable) return;
      const sel = fields.find((f) => f.id === selectedId && f.page === currentPage);
      if (e.key === "Escape") select(null);
      if (!sel) return;
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        e.stopPropagation();
        select(null);
        mutate(() => deleteFormField(docId, sel.id));
      }
      const step = e.shiftKey ? 10 : 1;
      const d: Record<string, [number, number]> = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
      if (d[e.key]) {
        e.preventDefault();
        e.stopPropagation();
        const r = applyDrag(sel.rect, "move", d[e.key][0], d[e.key][1], page);
        upsertLocal({ ...sel, rect: r });
        mutate(() => updateFormField(docId, sel.id, { rect: r }));
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prepareMode, selectedId, fields, currentPage, docId]);

  const commitFill = (f: FormField, value: FieldValue) =>
    mutate(async () => {
      const res = await fillFormFields(docId, { [f.name]: value });
      if (res.errors[f.name]) throw new Error(`${f.name}: ${res.errors[f.name]}`);
    });

  const onFieldMouseDown = (e: React.MouseEvent, f: FormField, handle: Handle = "move") => {
    if (!prepareMode) return;
    e.stopPropagation();
    e.preventDefault();
    select(f.id);
    setInter({ kind: "edit", id: f.id, handle, start: toPts(e), orig: f.rect, current: f.rect });
  };

  const onFieldClick = (e: React.MouseEvent, f: FormField) => {
    if (prepareMode) return;
    e.stopPropagation();
    select(f.id);
    if (f.readonly || pending) return;
    if (f.type === "checkbox") commitFill(f, !f.value ? (f.export_value ?? true) : false);
    else if (f.type === "radio") commitFill(f, f.checked ? null : f.export_value);
    else if (f.type === "text" || f.type === "combo" || f.type === "list")
      setEditing({ field: f, draft: typeof f.value === "string" ? f.value : "" });
  };

  if (!pageWidth || !pageHeight) return null;

  return (
    <div
      ref={ref}
      data-testid="forms-overlay"
      className="absolute inset-0 z-20 select-none"
      style={{ pointerEvents: prepareMode ? "auto" : "none", cursor: prepareMode ? "crosshair" : undefined }}
      onMouseDown={(e) => {
        if (!prepareMode || e.button !== 0) return;
        e.stopPropagation();
        select(null);
        const p = toPts(e);
        setInter({ kind: "create", start: p, current: p });
      }}
    >
      {pageFields.map((f) => {
        const live = inter?.kind === "edit" && inter.id === f.id ? inter.current : f.rect;
        const isSel = selectedId === f.id;
        const base = prepareMode
          ? `border border-dashed ${isSel ? "border-purple-600 bg-purple-400/20" : "border-purple-500/80 bg-purple-300/10 hover:bg-purple-300/25"} cursor-move`
          : `border ${f.required ? "border-red-400/80" : "border-blue-400/70"} ${f.readonly ? "bg-gray-300/20" : "bg-blue-300/15 hover:bg-blue-300/30 cursor-pointer"} ${isSel ? "ring-2 ring-blue-500" : ""}`;
        return (
          <div
            key={f.id}
            data-field-id={f.id}
            title={`${f.tooltip || f.name} (${f.type})`}
            className={`absolute ${base} ${f.type === "radio" ? "rounded-full" : "rounded-[2px]"}`}
            style={{ ...rectToPercentStyle(live, page), pointerEvents: "auto" }}
            onMouseDown={(e) => onFieldMouseDown(e, f)}
            onClick={(e) => onFieldClick(e, f)}
          >
            {prepareMode && (
              <span className="absolute -top-4 left-0 text-[9px] leading-none px-1 py-0.5 rounded bg-purple-600 text-white whitespace-nowrap pointer-events-none">
                {f.name}{f.type === "radio" && f.export_value ? ` = ${f.export_value}` : ""}{f.required ? " *" : ""}
              </span>
            )}
            {prepareMode && isSel &&
              HANDLES.map((h) => (
                <span
                  key={h}
                  data-handle={h}
                  className={`absolute w-2 h-2 bg-white border border-purple-600 ${HANDLE_POS[h]}`}
                  onMouseDown={(e) => onFieldMouseDown(e, f, h)}
                />
              ))}
          </div>
        );
      })}

      {/* rubber band while creating */}
      {inter?.kind === "create" && (
        <div
          className="absolute border-2 border-purple-600 bg-purple-400/20 pointer-events-none"
          style={rectToPercentStyle(normalizeRect([...inter.start, ...inter.current] as Rect), page)}
        />
      )}

      {/* in-place editor (fill mode) */}
      {editing && (
        <div
          className="absolute z-30"
          style={{ ...rectToPercentStyle(editing.field.rect, page), height: "auto", minWidth: 140, pointerEvents: "auto" }}
          onMouseDown={(e) => e.stopPropagation()}
        >
          {editing.field.type === "text" ? (
            editing.field.multiline ? (
              <textarea
                autoFocus
                aria-label={`Fill ${editing.field.name}`}
                className="w-full min-h-[60px] p-1 text-sm border border-blue-500 rounded bg-white dark:bg-gray-900 dark:text-white shadow-lg"
                value={editing.draft}
                maxLength={editing.field.max_len || undefined}
                onChange={(e) => setEditing({ ...editing, draft: e.target.value })}
                onBlur={() => { commitFill(editing.field, editing.draft); setEditing(null); }}
                onKeyDown={(e) => e.key === "Escape" && setEditing(null)}
              />
            ) : (
              <input
                autoFocus
                aria-label={`Fill ${editing.field.name}`}
                className="w-full p-1 text-sm border border-blue-500 rounded bg-white dark:bg-gray-900 dark:text-white shadow-lg"
                value={editing.draft}
                maxLength={editing.field.max_len || undefined}
                onChange={(e) => setEditing({ ...editing, draft: e.target.value })}
                onBlur={() => { if (editing.draft !== (editing.field.value ?? "")) commitFill(editing.field, editing.draft); setEditing(null); }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                  if (e.key === "Escape") setEditing(null);
                }}
              />
            )
          ) : (
            <select
              autoFocus
              aria-label={`Fill ${editing.field.name}`}
              className="w-full p-1 text-sm border border-blue-500 rounded bg-white dark:bg-gray-900 dark:text-white shadow-lg"
              value={editing.draft}
              onChange={(e) => { commitFill(editing.field, e.target.value); setEditing(null); }}
              onBlur={() => setEditing(null)}
            >
              <option value="">—</option>
              {editing.field.options.map((o, i) => <option key={o} value={o}>{editing.field.option_labels[i] ?? o}</option>)}
            </select>
          )}
        </div>
      )}

      {(pending || err) && (
        <div className="absolute top-1 right-1 z-30 pointer-events-auto">
          {pending && <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-800/80 text-white">Saving…</span>}
          {err && (
            <button onClick={() => setErr(null)} className="text-[10px] px-1.5 py-0.5 rounded bg-red-600 text-white max-w-[240px] text-left" role="alert">
              {err}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
