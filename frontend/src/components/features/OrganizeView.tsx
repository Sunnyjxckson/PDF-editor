"use client";

// Full-screen "Organize pages" view: big thumbnails, multi-select, drag to
// reorder, and page actions (insert blank / from file, extract, duplicate,
// rotate, delete, crop, resize, split). Every mutation goes through
// backend/features/organize.py (or the existing /reorder) and snapshots for undo.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Copy, Crop, Download, FilePlus2, FileUp, Loader2, RotateCcw, RotateCw,
  Scissors, Scaling, SquareDashed, Trash2, X, CheckSquare, Square,
} from "lucide-react";
import {
  organizeThumbnailUrl, reorderDocument, movePages, isIdentityOrder, newIndexesOf, nextSelection,
  insertBlankPages, insertPagesFromFile, extractPages, duplicatePages, rotatePages, deletePages,
  cropPages, resizePages, splitToZip, saveBlob, parsePageRanges, formatPageRanges, type PaperSize,
} from "@/lib/features/organize";

export interface OrganizeViewProps {
  docId: string;
  /** Current page count (from /info). Updated locally after each action too. */
  pageCount: number;
  /** 0-based page the editor is showing; it is pre-selected. */
  currentPage: number;
  /** Called after every successful mutation so the app can re-fetch /info and re-render. */
  onDocumentChanged: () => void;
  /** Double-click on a thumbnail: open that page in the editor. */
  onPageSelect?: (page: number) => void;
  onClose?: () => void;
  /** Bump to force thumbnails to reload (e.g. after undo/redo elsewhere). */
  version?: number;
  filename?: string;
}

type Panel = null | "blank" | "file" | "extract" | "crop" | "resize" | "split";

const btn =
  "inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-colors " +
  "text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40 disabled:pointer-events-none";
const btnActive = "bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300";
const input =
  "px-2 py-1 rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 " +
  "text-gray-800 dark:text-gray-200 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500";
const primary =
  "px-3 py-1.5 rounded-lg text-xs font-semibold bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50";

export default function OrganizeView({
  docId, pageCount, currentPage, onDocumentChanged, onPageSelect, onClose, version = 0, filename = "document.pdf",
}: OrganizeViewProps) {
  const [count, setCount] = useState(pageCount);
  const [selection, setSelection] = useState<number[]>([currentPage]);
  const [anchor, setAnchor] = useState<number | null>(currentPage);
  const [localVersion, setLocalVersion] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [panel, setPanel] = useState<Panel>(null);
  const [dragging, setDragging] = useState(false);
  const [dropTarget, setDropTarget] = useState<number | null>(null);
  const [thumbSize, setThumbSize] = useState(180);

  useEffect(() => setCount(pageCount), [pageCount]);
  // keep selection within range when pages disappear
  useEffect(() => {
    setSelection((s) => {
      const f = s.filter((p) => p < count);
      return f.length ? f : count ? [Math.min(currentPage, count - 1)] : [];
    });
  }, [count, currentPage]);

  const thumbVersion = version * 100000 + localVersion;
  const selLabel = selection.length ? formatPageRanges(selection) : "none";

  const run = useCallback(
    async (label: string, fn: () => Promise<{ count?: number; select?: number[]; notice?: string } | void>) => {
      setBusy(label);
      setError(null);
      setNotice(null);
      try {
        const r = await fn();
        if (r?.count !== undefined) setCount(r.count);
        if (r?.select) setSelection(r.select);
        if (r?.notice) setNotice(r.notice);
        setLocalVersion((v) => v + 1);
        onDocumentChanged();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [onDocumentChanged],
  );

  // ─── selection & keyboard ───────────────────────────────────────────────
  const onThumbClick = (e: React.MouseEvent, p: number) => {
    const r = nextSelection(selection, p, anchor, { shift: e.shiftKey, toggle: e.metaKey || e.ctrlKey });
    setSelection(r.selection);
    setAnchor(r.anchor);
  };

  const containerRef = useRef<HTMLDivElement>(null);
  const onKeyDown = (e: React.KeyboardEvent) => {
    const tag = (e.target as HTMLElement).tagName;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "a") {
      e.preventDefault();
      setSelection(Array.from({ length: count }, (_, i) => i));
    } else if ((e.key === "Delete" || e.key === "Backspace") && selection.length) {
      e.preventDefault();
      doDelete();
    } else if (e.key === "Escape") {
      if (panel) setPanel(null);
      else onClose?.();
    }
  };

  // ─── drag to reorder ────────────────────────────────────────────────────
  const onDragStart = (e: React.DragEvent, p: number) => {
    if (!selection.includes(p)) {
      setSelection([p]);
      setAnchor(p);
    }
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(p));
    setDragging(true);
  };

  const onDragOverThumb = (e: React.DragEvent, p: number) => {
    e.preventDefault();
    const box = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const after = e.clientX > box.left + box.width / 2;
    setDropTarget(after ? p + 1 : p);
  };

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const target = dropTarget;
    setDragging(false);
    setDropTarget(null);
    if (target === null) return;
    const moving = selection.length ? selection : [Number(e.dataTransfer.getData("text/plain"))];
    const order = movePages(count, moving, target);
    if (isIdentityOrder(order)) return;
    run("Reordering", async () => {
      await reorderDocument(docId, order);
      return { select: newIndexesOf(order, moving) };
    });
  };

  // ─── actions ────────────────────────────────────────────────────────────
  const doRotate = (angle: number) =>
    run("Rotating", async () => {
      await rotatePages(docId, selection, angle);
    });

  const doDelete = () => {
    if (selection.length >= count) {
      setError("A PDF must keep at least one page.");
      return;
    }
    if (!window.confirm(`Delete ${selection.length} page(s): ${selLabel}?`)) return;
    run("Deleting", async () => {
      const r = await deletePages(docId, selection);
      return { count: r.page_count, select: [Math.min(selection[0], r.page_count - 1)] };
    });
  };

  const doDuplicate = () =>
    run("Duplicating", async () => {
      const r = await duplicatePages(docId, selection);
      // each original p moves by the number of selected pages before it
      const sorted = [...selection].sort((a, b) => a - b);
      const copies = sorted.map((p, i) => p + i + 1);
      return { count: r.page_count, select: copies };
    });

  const selectAll = () => setSelection(Array.from({ length: count }, (_, i) => i));
  const allSelected = selection.length === count && count > 0;

  return (
    <div
      ref={containerRef}
      tabIndex={0}
      onKeyDown={onKeyDown}
      className="flex flex-col h-full w-full bg-gray-50 dark:bg-gray-950 outline-none"
    >
      {/* header / action bar */}
      <div className="flex flex-wrap items-center gap-1 px-3 py-2 border-b border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900">
        <span className="text-sm font-semibold text-gray-800 dark:text-gray-100 mr-2">Organize pages</span>
        <button className={btn} onClick={() => (allSelected ? setSelection([]) : selectAll())} title="Select all (Ctrl/Cmd+A)">
          {allSelected ? <CheckSquare className="w-4 h-4" /> : <Square className="w-4 h-4" />}
          {selection.length}/{count}
        </button>
        <div className="w-px h-5 bg-gray-200 dark:bg-gray-700 mx-1" />
        <button className={`${btn} ${panel === "blank" ? btnActive : ""}`} onClick={() => setPanel(panel === "blank" ? null : "blank")}>
          <FilePlus2 className="w-4 h-4" /> Insert blank
        </button>
        <button className={`${btn} ${panel === "file" ? btnActive : ""}`} onClick={() => setPanel(panel === "file" ? null : "file")}>
          <FileUp className="w-4 h-4" /> Insert from file
        </button>
        <button className={`${btn} ${panel === "extract" ? btnActive : ""}`} disabled={!selection.length} onClick={() => setPanel(panel === "extract" ? null : "extract")}>
          <Download className="w-4 h-4" /> Extract
        </button>
        <button className={btn} disabled={!selection.length || !!busy} onClick={doDuplicate}>
          <Copy className="w-4 h-4" /> Duplicate
        </button>
        <button className={btn} disabled={!selection.length || !!busy} onClick={() => doRotate(-90)} title="Rotate left">
          <RotateCcw className="w-4 h-4" />
        </button>
        <button className={btn} disabled={!selection.length || !!busy} onClick={() => doRotate(90)} title="Rotate right">
          <RotateCw className="w-4 h-4" />
        </button>
        <button className={`${btn} ${panel === "crop" ? btnActive : ""}`} disabled={!selection.length} onClick={() => setPanel(panel === "crop" ? null : "crop")}>
          <Crop className="w-4 h-4" /> Crop
        </button>
        <button className={`${btn} ${panel === "resize" ? btnActive : ""}`} disabled={!selection.length} onClick={() => setPanel(panel === "resize" ? null : "resize")}>
          <Scaling className="w-4 h-4" /> Resize
        </button>
        <button className={`${btn} ${panel === "split" ? btnActive : ""}`} onClick={() => setPanel(panel === "split" ? null : "split")}>
          <Scissors className="w-4 h-4" /> Split
        </button>
        <button className={`${btn} text-red-600 dark:text-red-400`} disabled={!selection.length || !!busy} onClick={doDelete}>
          <Trash2 className="w-4 h-4" /> Delete
        </button>
        <div className="flex-1" />
        <input
          type="range" min={100} max={320} value={thumbSize} onChange={(e) => setThumbSize(Number(e.target.value))}
          className="w-24 accent-blue-600" aria-label="Thumbnail size"
        />
        {onClose && (
          <button className={btn} onClick={onClose} aria-label="Close organize view">
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      {/* option panels */}
      {panel && (
        <div className="px-3 py-2 border-b border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-900/60 text-xs text-gray-700 dark:text-gray-300">
          {panel === "blank" && <BlankPanel count={count} selection={selection} busy={!!busy} onRun={(o) =>
            run("Inserting", async () => {
              const r = await insertBlankPages(docId, o);
              return { count: r.page_count, select: Array.from({ length: o.count ?? 1 }, (_, i) => o.position + i) };
            })} />}
          {panel === "file" && <FilePanel count={count} selection={selection} busy={!!busy} onRun={(o) =>
            run("Inserting", async () => {
              const r = await insertPagesFromFile(docId, o);
              return { count: r.page_count, select: Array.from({ length: r.inserted }, (_, i) => o.position + i) };
            })} />}
          {panel === "extract" && <ExtractPanel selection={selection} count={count} busy={!!busy} filename={filename} onRun={(pages, del, name) =>
            run("Extracting", async () => {
              const blob = await extractPages(docId, pages, { deleteAfter: del, filename: name });
              saveBlob(blob, name);
              return del ? { count: count - pages.length, select: [Math.min(pages[0], count - pages.length - 1)] } : { notice: `Downloaded ${name}` };
            })} />}
          {panel === "crop" && <CropPanel selection={selection} busy={!!busy} onRun={(o) =>
            run("Cropping", async () => {
              const r = await cropPages(docId, selection, o);
              const skipped = r.pages.filter((p) => p.skipped).length;
              return { notice: skipped ? `${skipped} blank page(s) left uncropped` : `Cropped ${r.pages.length} page(s)` };
            })} />}
          {panel === "resize" && <ResizePanel busy={!!busy} onRun={(o) =>
            run("Resizing", async () => {
              await resizePages(docId, selection, o);
            })} />}
          {panel === "split" && <SplitPanel busy={!!busy} onRun={(o) =>
            run("Splitting", async () => {
              const blob = await splitToZip(docId, o);
              const base = filename.replace(/\.pdf$/i, "");
              saveBlob(blob, `${base}_split.zip`);
              return { notice: "Downloaded split parts as a zip" };
            })} />}
        </div>
      )}

      {(busy || error || notice) && (
        <div className={`px-3 py-1.5 text-xs flex items-center gap-2 ${error ? "bg-red-50 dark:bg-red-950/40 text-red-700 dark:text-red-300" : "bg-blue-50 dark:bg-blue-950/30 text-blue-700 dark:text-blue-300"}`}>
          {busy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
          <span>{busy ? `${busy}...` : error || notice}</span>
          {!busy && <button className="ml-auto" onClick={() => { setError(null); setNotice(null); }} aria-label="Dismiss"><X className="w-3.5 h-3.5" /></button>}
        </div>
      )}

      {/* thumbnail grid */}
      <div
        className="flex-1 overflow-auto p-4"
        onDragOver={(e) => e.preventDefault()}
        onDrop={onDrop}
        onDragEnd={() => { setDragging(false); setDropTarget(null); }}
        onClick={(e) => { if (e.target === e.currentTarget) setSelection([]); }}
      >
        <div className="flex flex-wrap gap-4" onClick={(e) => { if (e.target === e.currentTarget) setSelection([]); }}>
          {Array.from({ length: count }, (_, p) => {
            const selected = selection.includes(p);
            return (
              <div
                key={p}
                data-testid={`organize-thumb-${p}`}
                draggable={!busy}
                onDragStart={(e) => onDragStart(e, p)}
                onDragOver={(e) => onDragOverThumb(e, p)}
                onClick={(e) => onThumbClick(e, p)}
                onDoubleClick={() => onPageSelect?.(p)}
                className="relative flex flex-col items-center gap-1 select-none cursor-pointer"
                style={{ width: thumbSize }}
              >
                {dragging && dropTarget === p && <DropMarker side="left" />}
                {dragging && dropTarget === p + 1 && <DropMarker side="right" />}
                <div
                  className={`w-full flex items-center justify-center rounded-md overflow-hidden bg-white dark:bg-gray-800 shadow-sm transition-all ${
                    selected ? "ring-[3px] ring-blue-500" : "ring-1 ring-gray-200 dark:ring-gray-700 hover:ring-gray-400"
                  } ${dragging && selected ? "opacity-40" : ""}`}
                  style={{ height: thumbSize * 1.3 }}
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={organizeThumbnailUrl(docId, p, thumbVersion)}
                    alt={`Page ${p + 1}`}
                    draggable={false}
                    loading="lazy"
                    className="max-w-full max-h-full object-contain"
                  />
                </div>
                <span className={`text-xs ${selected ? "text-blue-600 dark:text-blue-400 font-semibold" : "text-gray-500 dark:text-gray-400"}`}>
                  {p + 1}
                </span>
              </div>
            );
          })}
        </div>
        <p className="mt-6 text-[11px] text-gray-400 dark:text-gray-500">
          Click to select, Shift-click for a range, Ctrl/Cmd-click to toggle. Drag to reorder (moves every selected page).
          Double-click opens a page. Delete removes the selection. Every change can be undone.
        </p>
      </div>
    </div>
  );
}

function DropMarker({ side }: { side: "left" | "right" }) {
  return (
    <div
      className="absolute top-0 bottom-6 w-1 rounded bg-blue-500 z-10"
      style={side === "left" ? { left: -10 } : { right: -10 }}
    />
  );
}

// ─── option panels ──────────────────────────────────────────────────────────

const SIZES: { v: "neighbor" | PaperSize; label: string }[] = [
  { v: "neighbor", label: "Same as neighbour" },
  { v: "letter", label: "Letter 8.5x11" },
  { v: "legal", label: "Legal 8.5x14" },
  { v: "a4", label: "A4" },
  { v: "a3", label: "A3" },
  { v: "a5", label: "A5" },
  { v: "tabloid", label: "Tabloid 11x17" },
];

function PositionPicker({ count, selection, value, onChange }: { count: number; selection: number[]; value: number; onChange: (n: number) => void }) {
  const first = selection.length ? Math.min(...selection) : 0;
  const last = selection.length ? Math.max(...selection) : count - 1;
  const mode = value === first && selection.length ? "before" : value === last + 1 && selection.length ? "after" : value === 0 ? "start" : value === count ? "end" : "custom";
  return (
    <label className="flex items-center gap-1.5">
      Where
      <select className={input} value={mode} onChange={(e) => {
        const m = e.target.value;
        onChange(m === "before" ? first : m === "after" ? last + 1 : m === "start" ? 0 : count);
      }}>
        {selection.length > 0 && <option value="before">Before page {first + 1}</option>}
        {selection.length > 0 && <option value="after">After page {last + 1}</option>}
        <option value="start">At the start</option>
        <option value="end">At the end</option>
        {mode === "custom" && <option value="custom">Position {value + 1}</option>}
      </select>
    </label>
  );
}

function BlankPanel({ count, selection, busy, onRun }: {
  count: number; selection: number[]; busy: boolean;
  onRun: (o: { position: number; size: "neighbor" | PaperSize; landscape: boolean; count: number }) => void;
}) {
  const [position, setPosition] = useState(selection.length ? Math.max(...selection) + 1 : count);
  const [size, setSize] = useState<"neighbor" | PaperSize>("neighbor");
  const [landscape, setLandscape] = useState(false);
  const [n, setN] = useState(1);
  return (
    <div className="flex flex-wrap items-center gap-3">
      <PositionPicker count={count} selection={selection} value={position} onChange={setPosition} />
      <label className="flex items-center gap-1.5">Size
        <select className={input} value={size} onChange={(e) => setSize(e.target.value as "neighbor" | PaperSize)}>
          {SIZES.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}
        </select>
      </label>
      <label className="flex items-center gap-1.5">
        <input type="checkbox" checked={landscape} disabled={size === "neighbor"} onChange={(e) => setLandscape(e.target.checked)} /> Landscape
      </label>
      <label className="flex items-center gap-1.5">Count
        <input type="number" min={1} max={500} className={`${input} w-16`} value={n} onChange={(e) => setN(Math.max(1, Number(e.target.value) || 1))} />
      </label>
      <button className={primary} disabled={busy} onClick={() => onRun({ position, size, landscape, count: n })}>Insert</button>
    </div>
  );
}

function FilePanel({ count, selection, busy, onRun }: {
  count: number; selection: number[]; busy: boolean;
  onRun: (o: { position: number; file: File; pageFrom?: number; pageTo?: number }) => void;
}) {
  const [position, setPosition] = useState(selection.length ? Math.max(...selection) + 1 : count);
  const [file, setFile] = useState<File | null>(null);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const submit = () => {
    if (!file) return setErr("Choose a PDF first");
    const f = from ? parseInt(from, 10) - 1 : undefined;
    const t = to ? parseInt(to, 10) - 1 : undefined;
    if ((f !== undefined && (Number.isNaN(f) || f < 0)) || (t !== undefined && (Number.isNaN(t) || t < 0))) return setErr("Page numbers start at 1");
    setErr(null);
    onRun({ position, file, pageFrom: f, pageTo: t });
  };
  return (
    <div className="flex flex-wrap items-center gap-3">
      <input type="file" accept="application/pdf,.pdf" className="text-xs" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
      <label className="flex items-center gap-1.5">Source pages
        <input className={`${input} w-14`} placeholder="first" value={from} onChange={(e) => setFrom(e.target.value)} />
        to
        <input className={`${input} w-14`} placeholder="last" value={to} onChange={(e) => setTo(e.target.value)} />
      </label>
      <PositionPicker count={count} selection={selection} value={position} onChange={setPosition} />
      <button className={primary} disabled={busy || !file} onClick={submit}>Insert pages</button>
      {err && <span className="text-red-600 dark:text-red-400">{err}</span>}
    </div>
  );
}

function ExtractPanel({ selection, count, busy, filename, onRun }: {
  selection: number[]; count: number; busy: boolean; filename: string;
  onRun: (pages: number[], deleteAfter: boolean, name: string) => void;
}) {
  const [range, setRange] = useState(formatPageRanges(selection));
  const [del, setDel] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => setRange(formatPageRanges(selection)), [selection]);
  const submit = () => {
    try {
      const pages = parsePageRanges(range, count);
      if (del && pages.length >= count) throw new Error("Cannot delete every page");
      setErr(null);
      const base = filename.replace(/\.pdf$/i, "");
      onRun(pages, del, `${base}_pages_${formatPageRanges(pages).replace(/[ ,]+/g, "_")}.pdf`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-3">
      <label className="flex items-center gap-1.5">Pages
        <input className={`${input} w-40`} value={range} onChange={(e) => setRange(e.target.value)} placeholder="e.g. 1-3, 7" />
      </label>
      <label className="flex items-center gap-1.5">
        <input type="checkbox" checked={del} onChange={(e) => setDel(e.target.checked)} /> Delete from this document after extracting
      </label>
      <button className={primary} disabled={busy} onClick={submit}>Extract &amp; download</button>
      {err && <span className="text-red-600 dark:text-red-400">{err}</span>}
    </div>
  );
}

function CropPanel({ selection, busy, onRun }: {
  selection: number[]; busy: boolean;
  onRun: (o: { mode: "margins" | "auto" | "reset"; margins?: { top: number; right: number; bottom: number; left: number }; padding?: number }) => void;
}) {
  const [mode, setMode] = useState<"auto" | "margins" | "reset">("auto");
  const [unit, setUnit] = useState<"pt" | "in" | "mm">("in");
  const [m, setM] = useState({ top: 0.5, right: 0.5, bottom: 0.5, left: 0.5 });
  const [padding, setPadding] = useState(6);
  const factor = unit === "pt" ? 1 : unit === "in" ? 72 : 72 / 25.4;
  return (
    <div className="flex flex-wrap items-center gap-3">
      <span className="text-gray-500 dark:text-gray-400">{selection.length} page(s):</span>
      <select className={input} value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
        <option value="auto">Remove white margins</option>
        <option value="margins">Trim margins</option>
        <option value="reset">Remove crop (restore full page)</option>
      </select>
      {mode === "auto" && (
        <label className="flex items-center gap-1.5">Keep padding (pt)
          <input type="number" min={0} className={`${input} w-16`} value={padding} onChange={(e) => setPadding(Number(e.target.value) || 0)} />
        </label>
      )}
      {mode === "margins" && (
        <>
          {(["top", "right", "bottom", "left"] as const).map((k) => (
            <label key={k} className="flex items-center gap-1 capitalize">{k}
              <input type="number" min={0} step={unit === "pt" ? 1 : 0.05} className={`${input} w-16`} value={m[k]}
                onChange={(e) => setM({ ...m, [k]: Number(e.target.value) || 0 })} />
            </label>
          ))}
          <select className={input} value={unit} onChange={(e) => setUnit(e.target.value as typeof unit)}>
            <option value="in">in</option><option value="mm">mm</option><option value="pt">pt</option>
          </select>
        </>
      )}
      <button className={primary} disabled={busy} onClick={() => onRun(
        mode === "margins"
          ? { mode, margins: { top: m.top * factor, right: m.right * factor, bottom: m.bottom * factor, left: m.left * factor } }
          : mode === "auto" ? { mode, padding } : { mode },
      )}>
        <SquareDashed className="w-3.5 h-3.5 inline mr-1" />Apply crop
      </button>
    </div>
  );
}

function ResizePanel({ busy, onRun }: {
  busy: boolean;
  onRun: (o: { size: PaperSize | "custom"; width?: number; height?: number; scale_content: boolean; match_orientation: boolean }) => void;
}) {
  const [size, setSize] = useState<PaperSize | "custom">("letter");
  const [w, setW] = useState(8.5);
  const [h, setH] = useState(11);
  const [scale, setScale] = useState(true);
  const [orient, setOrient] = useState(true);
  return (
    <div className="flex flex-wrap items-center gap-3">
      <label className="flex items-center gap-1.5">New size
        <select className={input} value={size} onChange={(e) => setSize(e.target.value as PaperSize | "custom")}>
          {SIZES.filter((s) => s.v !== "neighbor").map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}
          <option value="custom">Custom (inches)</option>
        </select>
      </label>
      {size === "custom" && (
        <>
          <input type="number" step={0.1} className={`${input} w-16`} value={w} onChange={(e) => setW(Number(e.target.value) || 0)} aria-label="width in inches" />
          x
          <input type="number" step={0.1} className={`${input} w-16`} value={h} onChange={(e) => setH(Number(e.target.value) || 0)} aria-label="height in inches" />
        </>
      )}
      <label className="flex items-center gap-1.5"><input type="checkbox" checked={scale} onChange={(e) => setScale(e.target.checked)} /> Scale content to fit</label>
      <label className="flex items-center gap-1.5"><input type="checkbox" checked={orient} onChange={(e) => setOrient(e.target.checked)} /> Keep each page&apos;s orientation</label>
      <button className={primary} disabled={busy} onClick={() => onRun({
        size, width: size === "custom" ? w * 72 : undefined, height: size === "custom" ? h * 72 : undefined,
        scale_content: scale, match_orientation: orient,
      })}>Resize</button>
    </div>
  );
}

function SplitPanel({ busy, onRun }: { busy: boolean; onRun: (o: { mode: "every_n" | "bookmarks" | "size"; n?: number; level?: number; max_mb?: number }) => void }) {
  const [mode, setMode] = useState<"every_n" | "bookmarks" | "size">("every_n");
  const [n, setN] = useState(1);
  const [level, setLevel] = useState(1);
  const [mb, setMb] = useState(10);
  return (
    <div className="flex flex-wrap items-center gap-3">
      <select className={input} value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
        <option value="every_n">Every N pages</option>
        <option value="bookmarks">At top-level bookmarks</option>
        <option value="size">By maximum file size</option>
      </select>
      {mode === "every_n" && <label className="flex items-center gap-1.5">N <input type="number" min={1} className={`${input} w-16`} value={n} onChange={(e) => setN(Math.max(1, Number(e.target.value) || 1))} /></label>}
      {mode === "bookmarks" && <label className="flex items-center gap-1.5">Bookmark level <input type="number" min={1} className={`${input} w-14`} value={level} onChange={(e) => setLevel(Math.max(1, Number(e.target.value) || 1))} /></label>}
      {mode === "size" && <label className="flex items-center gap-1.5">Max MB <input type="number" min={0.1} step={0.5} className={`${input} w-16`} value={mb} onChange={(e) => setMb(Number(e.target.value) || 1)} /></label>}
      <button className={primary} disabled={busy} onClick={() => onRun({ mode, n, level, max_mb: mb })}>Split &amp; download zip</button>
      <span className="text-gray-500 dark:text-gray-400">This document is not changed.</span>
    </div>
  );
}
