"use client";

/**
 * "Objects" mode: select / move / resize / crop / rotate / replace / delete /
 * download images, move / resize / delete vector objects, insert images and draw
 * vector shapes (rect, ellipse, line, arrow). Every change is a real PDF edit via
 * backend/features/objects.py and is undoable through the existing undo endpoint.
 *
 * MOUNTING: render inside the element that wraps the rendered page image/canvas
 * (the `relative` div in PageViewer that receives the CSS zoom transform). The
 * overlay fills it with `absolute inset-0`. All geometry is computed from the
 * overlay's own getBoundingClientRect() and the page size in PDF points returned
 * by the backend, so render DPI, pdf.js scale and zoom need not be passed in.
 * The tool bar is portalled to <body> (fixed, bottom-centre) so it is not scaled.
 *
 * SELECTION: click selects one object; Shift+click toggles; dragging on empty
 * page draws a marquee (Shift adds to the selection); Cmd/Ctrl+A selects all.
 * A multi-selection moves, deletes, duplicates (Cmd/Ctrl+D), aligns,
 * distributes and arranges as ONE backend batch = one undo step. Arrow keys
 * nudge by 1pt (Shift: 10pt); nudges are previewed live and committed once,
 * 400ms after the last key press. Errors are announced via an ARIA live region.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  MousePointer2,
  Square,
  Circle,
  Minus,
  ArrowUpRight,
  ImagePlus,
  Crop,
  RotateCw,
  Download,
  Trash2,
  Replace,
  X,
  Loader2,
  Check,
  Copy,
  BringToFront,
  SendToBack,
  AlignStartVertical,
  AlignCenterVertical,
  AlignEndVertical,
  AlignStartHorizontal,
  AlignCenterHorizontal,
  AlignEndHorizontal,
  AlignHorizontalDistributeCenter,
  AlignVerticalDistributeCenter,
} from "lucide-react";
import {
  listObjects,
  moveImage,
  rotateImage,
  deleteImage,
  cropImage,
  replaceImage,
  insertImage,
  getImageExtractUrl,
  moveDrawing,
  deleteDrawing,
  addShape,
  clientToPdf,
  applyDrag,
  normalizeBbox,
  bboxToPercentStyle,
  clampToPage,
  intersectBbox,
  bboxEquals,
  roundBbox,
  hexToRgb01,
  defaultInsertRect,
  batchObjects,
  arrangeObject,
  objectRef,
  alignBboxes,
  distributeBboxes,
  objectsInMarquee,
  translateBbox,
  nudgeDelta,
  type AlignMode,
  type BatchOp,
  type Bbox,
  type Handle,
  type PageObjects,
  type PageObject,
  type ShapeType,
} from "@/lib/features/objects";

export interface ObjectsOverlayProps {
  docId: string;
  currentPage: number;
  /** Bump to force a refetch (e.g. the store's pageVersion after undo/redo). */
  pageVersion?: number;
  /** Called after every successful edit so the host re-renders the page. */
  onDocumentChanged: () => void;
  /** Optional toast hook; errors are also shown inline in the tool bar. */
  onNotify?: (message: string, type: "success" | "error" | "info") => void;
  /** Shows a close button in the tool bar when provided. */
  onExit?: () => void;
}

type Tool = "select" | ShapeType;

type DragState =
  | { kind: "object"; mode: "move" | Handle; start: [number, number]; orig: Bbox; obj: PageObject }
  | { kind: "group"; start: [number, number]; obj: PageObject }
  | { kind: "marquee"; start: [number, number]; cur: [number, number]; base: string[] }
  | { kind: "shape"; start: [number, number]; cur: [number, number] }
  | { kind: "crop"; start: [number, number]; cur: [number, number] };

const HANDLES: Handle[] = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
const HANDLE_CURSOR: Record<Handle, string> = {
  nw: "nwse-resize",
  se: "nwse-resize",
  ne: "nesw-resize",
  sw: "nesw-resize",
  n: "ns-resize",
  s: "ns-resize",
  e: "ew-resize",
  w: "ew-resize",
};

function handlePos(h: Handle): { left: string; top: string } {
  const left = h.includes("w") ? "0%" : h.includes("e") ? "100%" : "50%";
  const top = h.includes("n") ? "0%" : h.includes("s") ? "100%" : "50%";
  return { left, top };
}

const area = (b: Bbox) => (b[2] - b[0]) * (b[3] - b[1]);

/** Debounce for committing arrow-key nudges (ms). */
export const NUDGE_COMMIT_MS = 400;
const DUPLICATE_OFFSET = 10; // pt

const isEditable = (o: PageObject) => o.kind === "drawing" || o.editable;

export default function ObjectsOverlay({
  docId,
  currentPage,
  pageVersion = 0,
  onDocumentChanged,
  onNotify,
  onExit,
}: ObjectsOverlayProps) {
  const overlayRef = useRef<HTMLDivElement>(null);
  const replaceInputRef = useRef<HTMLInputElement>(null);
  const insertInputRef = useRef<HTMLInputElement>(null);

  const [data, setData] = useState<PageObjects | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [tool, setTool] = useState<Tool>("select");
  const [drag, setDrag] = useState<DragState | null>(null);
  const [draft, setDraft] = useState<Bbox | null>(null); // live bbox while moving/resizing ONE object
  const [groupDelta, setGroupDelta] = useState<[number, number] | null>(null); // live offset for the selection
  const [cropping, setCropping] = useState(false);
  const [cropRect, setCropRect] = useState<Bbox | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [mounted, setMounted] = useState(false);
  const [localVersion, setLocalVersion] = useState(0);

  const [strokeHex, setStrokeHex] = useState("#e11d48");
  const [fillHex, setFillHex] = useState("#fde68a");
  const [useFill, setUseFill] = useState(false);
  const [strokeWidth, setStrokeWidth] = useState(2);
  const [dashed, setDashed] = useState(false);
  const [replaceAll, setReplaceAll] = useState(false);

  useEffect(() => setMounted(true), []);

  // ─── Data ───────────────────────────────────────────────────────────
  const pendingReselect = useRef<{ kind: PageObject["kind"]; bbox: Bbox }[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    listObjects(docId, currentPage)
      .then((d) => {
        if (cancelled) return;
        setData(d);
        const want = pendingReselect.current;
        pendingReselect.current = null;
        if (want) {
          const ids: string[] = [];
          for (const w of want) {
            const list: PageObject[] = w.kind === "image" ? d.images : d.drawings;
            const hit = list.find((o) => !ids.includes(o.id) && bboxEquals(o.bbox, w.bbox, 1.5));
            if (hit) ids.push(hit.id);
          }
          setSelectedIds(ids);
        } else {
          // keep whatever still exists (e.g. after undo/redo elsewhere)
          setSelectedIds((cur) => cur.filter((id) => [...d.images, ...d.drawings].some((o) => o.id === id)));
        }
      })
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [docId, currentPage, pageVersion, localVersion]);

  // reset on page change
  useEffect(() => {
    setSelectedIds([]);
    setCropping(false);
    setCropRect(null);
    setDrag(null);
    setDraft(null);
    setGroupDelta(null);
  }, [docId, currentPage]);

  const objects: PageObject[] = useMemo(() => {
    if (!data) return [];
    // larger first so smaller objects sit on top and stay clickable
    return [...data.drawings, ...data.images].sort((a, b) => area(b.bbox) - area(a.bbox));
  }, [data]);

  const selection = useMemo(() => objects.filter((o) => selectedIds.includes(o.id)), [objects, selectedIds]);
  const selected = selection.length === 1 ? selection[0] : null;
  const multi = selection.length > 1;
  const pageW = data?.page_width ?? 1;
  const pageH = data?.page_height ?? 1;

  const notify = useCallback(
    (msg: string, type: "success" | "error" | "info") => {
      if (type === "error") setError(msg);
      else setStatus(msg);
      onNotify?.(msg, type);
    },
    [onNotify],
  );

  const run = useCallback(
    async (label: string, fn: () => Promise<unknown>, reselect?: { kind: PageObject["kind"]; bbox: Bbox }[]) => {
      setBusy(true);
      setError(null);
      try {
        await fn();
        pendingReselect.current = reselect ?? null;
        if (!reselect) setSelectedIds([]);
        setLocalVersion((v) => v + 1);
        onDocumentChanged();
        notify(label, "success");
      } catch (e) {
        notify((e as Error).message || "Operation failed", "error");
        setLocalVersion((v) => v + 1); // resync after a 409 etc.
      } finally {
        setBusy(false);
      }
    },
    [notify, onDocumentChanged],
  );

  /** Move several objects to new boxes: one request (one undo step). */
  const commitMoves = useCallback(
    async (moves: { obj: PageObject; bbox: Bbox }[], label?: string) => {
      const real = moves
        .map((m) => ({ ...m, bbox: roundBbox(m.bbox) }))
        .filter((m) => isEditable(m.obj) && !bboxEquals(m.bbox, m.obj.bbox, 0.01));
      if (!real.length) return;
      const reselect = moves.map((m) => ({ kind: m.obj.kind, bbox: roundBbox(m.bbox) }));
      if (real.length === 1) {
        const { obj, bbox } = real[0];
        await run(
          label ?? (obj.kind === "image" ? "Image moved" : "Object moved"),
          () => (obj.kind === "image" ? moveImage(docId, currentPage, obj, bbox) : moveDrawing(docId, currentPage, obj, bbox)),
          reselect,
        );
      } else {
        const ops: BatchOp[] = real.map(({ obj, bbox }) => ({ op: "move", ...objectRef(obj), new_bbox: bbox }));
        await run(label ?? `${real.length} objects moved`, () => batchObjects(docId, currentPage, ops, label), reselect);
      }
    },
    [run, docId, currentPage],
  );

  // ─── Pointer helpers ────────────────────────────────────────────────
  const toPdf = useCallback(
    (clientX: number, clientY: number): [number, number] => {
      const el = overlayRef.current;
      if (!el || !data) return [0, 0];
      return clientToPdf(clientX, clientY, el.getBoundingClientRect(), pageW, pageH);
    },
    [data, pageW, pageH],
  );

  const startObjectDrag = (e: React.PointerEvent, obj: PageObject, mode: "move" | Handle) => {
    if (busy || e.button !== 0) return;
    e.stopPropagation();
    e.preventDefault();
    if (mode === "move" && e.shiftKey && !cropping) {
      // Shift+click toggles membership; no drag
      setSelectedIds((cur) => (cur.includes(obj.id) ? cur.filter((id) => id !== obj.id) : [...cur, obj.id]));
      return;
    }
    if (mode === "move" && selectedIds.includes(obj.id) && selectedIds.length > 1 && !cropping) {
      setDrag({ kind: "group", start: toPdf(e.clientX, e.clientY), obj });
      return;
    }
    setSelectedIds([obj.id]);
    if (cropping) return;
    if (!isEditable(obj)) return;
    setDrag({ kind: "object", mode, start: toPdf(e.clientX, e.clientY), orig: obj.bbox, obj });
    setDraft(obj.bbox);
  };

  const onBackgroundPointerDown = (e: React.PointerEvent) => {
    if (busy || e.button !== 0 || !data) return;
    const p = toPdf(e.clientX, e.clientY);
    if (cropping && selected?.kind === "image") {
      e.preventDefault();
      setDrag({ kind: "crop", start: p, cur: p });
      setCropRect(null);
      return;
    }
    if (tool !== "select") {
      e.preventDefault();
      setDrag({ kind: "shape", start: p, cur: p });
      return;
    }
    e.preventDefault();
    const base = e.shiftKey ? selectedIds : [];
    setDrag({ kind: "marquee", start: p, cur: p, base });
    if (!e.shiftKey) setSelectedIds([]);
  };

  // window-level move/up so drags continue outside the overlay
  useEffect(() => {
    if (!drag) return;
    const onMove = (e: PointerEvent) => {
      const p = toPdf(e.clientX, e.clientY);
      if (drag.kind === "object") {
        const dx = p[0] - drag.start[0];
        const dy = p[1] - drag.start[1];
        let next = applyDrag(drag.orig, drag.mode, dx, dy, e.shiftKey);
        if (drag.mode === "move") next = clampToPage(next, pageW, pageH);
        setDraft(next);
      } else if (drag.kind === "group") {
        setGroupDelta([p[0] - drag.start[0], p[1] - drag.start[1]]);
      } else if (drag.kind === "marquee") {
        setDrag({ ...drag, cur: p });
        const hit = objectsInMarquee(objects, [drag.start[0], drag.start[1], p[0], p[1]]).map((o) => o.id);
        setSelectedIds([...drag.base, ...hit.filter((id) => !drag.base.includes(id))]);
      } else if (drag.kind === "shape") {
        setDrag({ ...drag, cur: p });
      } else {
        setDrag({ ...drag, cur: p });
        const r = normalizeBbox([drag.start[0], drag.start[1], p[0], p[1]]);
        const hit = selected ? intersectBbox(r, selected.bbox) : null;
        setCropRect(hit ? roundBbox(hit) : null);
      }
    };
    const onUp = async (e: PointerEvent) => {
      const p = toPdf(e.clientX, e.clientY);
      const d = drag;
      setDrag(null);
      if (d.kind === "object") {
        const dx = p[0] - d.start[0];
        const dy = p[1] - d.start[1];
        let next = applyDrag(d.orig, d.mode, dx, dy, e.shiftKey);
        if (d.mode === "move") next = clampToPage(next, pageW, pageH);
        next = roundBbox(next);
        if (bboxEquals(next, d.orig, 0.5)) {
          setDraft(null);
          return; // a click, not a drag
        }
        await commitMoves([{ obj: d.obj, bbox: next }]);
        setDraft(null);
      } else if (d.kind === "group") {
        const dx = p[0] - d.start[0];
        const dy = p[1] - d.start[1];
        if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) {
          setGroupDelta(null);
          setSelectedIds([d.obj.id]); // plain click inside a multi-selection
          return;
        }
        await commitMoves(selection.map((o) => ({ obj: o, bbox: translateBbox(o.bbox, dx, dy) })));
        setGroupDelta(null);
      } else if (d.kind === "marquee") {
        const r = normalizeBbox([d.start[0], d.start[1], p[0], p[1]]);
        if (r[2] - r[0] < 3 && r[3] - r[1] < 3) setSelectedIds(d.base); // a click on empty page
      } else if (d.kind === "shape") {
        commitShape(d.start, p);
      }
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drag, toPdf, pageW, pageH, selected, selection, objects, docId, currentPage, commitMoves]);

  const shapeOpts = () => ({
    strokeColor: hexToRgb01(strokeHex),
    fillColor: useFill ? hexToRgb01(fillHex) : null,
    width: strokeWidth,
    dashed,
  });

  const commitShape = (a: [number, number], b: [number, number]) => {
    if (tool === "select") return;
    const dist = Math.hypot(b[0] - a[0], b[1] - a[1]);
    if (dist < 3) return;
    const t = tool;
    if (t === "line" || t === "arrow") {
      run(`${t === "line" ? "Line" : "Arrow"} added`, () =>
        addShape(docId, currentPage, t, { start: a, end: b }, { ...shapeOpts(), fillColor: null }),
      );
    } else {
      const rect = roundBbox(normalizeBbox([a[0], a[1], b[0], b[1]]));
      if (rect[2] - rect[0] < 2 || rect[3] - rect[1] < 2) return;
      run(`${t === "rect" ? "Rectangle" : "Ellipse"} added`, () => addShape(docId, currentPage, t, { rect }, shapeOpts()));
    }
  };

  // ─── Actions on selection ───────────────────────────────────────────
  const doDelete = useCallback(() => {
    if (busy) return;
    const targets = selection.filter(isEditable);
    if (!targets.length) return;
    if (targets.length === 1) {
      const t = targets[0];
      if (t.kind === "image") run("Image deleted", () => deleteImage(docId, currentPage, t));
      else run("Object deleted", () => deleteDrawing(docId, currentPage, t));
      return;
    }
    const ops: BatchOp[] = targets.map((o) => ({ op: "delete", ...objectRef(o) }));
    run(`${targets.length} objects deleted`, () => batchObjects(docId, currentPage, ops, `Delete ${targets.length} objects`));
  }, [selection, busy, run, docId, currentPage]);

  const doDuplicate = useCallback(() => {
    if (busy) return;
    const targets = selection.filter(isEditable);
    if (!targets.length) return;
    const ops: BatchOp[] = targets.map((o) => ({ op: "duplicate", ...objectRef(o), dx: DUPLICATE_OFFSET, dy: DUPLICATE_OFFSET }));
    run(
      targets.length === 1 ? "Duplicated" : `${targets.length} objects duplicated`,
      () => batchObjects(docId, currentPage, ops, "Duplicate"),
      targets.map((o) => ({ kind: o.kind, bbox: roundBbox(translateBbox(o.bbox, DUPLICATE_OFFSET, DUPLICATE_OFFSET)) })),
    );
  }, [selection, busy, run, docId, currentPage]);

  const doArrange = (where: "front" | "back") => {
    if (busy) return;
    const targets = selection.filter(isEditable);
    if (!targets.length) return;
    const label = where === "front" ? "Brought to front" : "Sent to back";
    const reselect = targets.map((o) => ({ kind: o.kind, bbox: o.bbox }));
    if (targets.length === 1) {
      run(label, () => arrangeObject(docId, currentPage, targets[0], where), reselect);
    } else {
      const ops: BatchOp[] = targets.map((o) => ({ op: where, ...objectRef(o) }));
      run(label, () => batchObjects(docId, currentPage, ops, label), reselect);
    }
  };

  const doAlign = (mode: AlignMode) => {
    if (selection.length < 2 || busy) return;
    const next = alignBboxes(selection.map((o) => o.bbox), mode);
    commitMoves(selection.map((o, i) => ({ obj: o, bbox: next[i] })), `Align ${mode}`);
  };

  const doDistribute = (axis: "horizontal" | "vertical") => {
    if (selection.length < 3 || busy) return;
    const next = distributeBboxes(selection.map((o) => o.bbox), axis);
    commitMoves(selection.map((o, i) => ({ obj: o, bbox: next[i] })), `Distribute ${axis}ly`);
  };

  const doRotate = () => {
    if (selected?.kind !== "image") return;
    const b = selected.bbox;
    const cx = (b[0] + b[2]) / 2;
    const cy = (b[1] + b[3]) / 2;
    const hw = (b[3] - b[1]) / 2;
    const hh = (b[2] - b[0]) / 2;
    run("Image rotated", () => rotateImage(docId, currentPage, selected, 90), [
      { kind: "image", bbox: [cx - hw, cy - hh, cx + hw, cy + hh] },
    ]);
  };

  const applyCrop = () => {
    if (selected?.kind !== "image" || !cropRect) return;
    const img = selected;
    const r = cropRect;
    setCropping(false);
    setCropRect(null);
    run("Image cropped", () => cropImage(docId, currentPage, img, r), [{ kind: "image", bbox: r }]);
  };

  const onReplaceFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || selected?.kind !== "image") return;
    const img = selected;
    run("Image replaced", () => replaceImage(docId, currentPage, img, file, replaceAll ? "all" : "placement"), [
      { kind: "image", bbox: img.bbox },
    ]);
  };

  const onInsertFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || !data) return;
    const url = URL.createObjectURL(file);
    const probe = new Image();
    probe.onload = () => {
      URL.revokeObjectURL(url);
      const rect = defaultInsertRect(probe.naturalWidth || 200, probe.naturalHeight || 200, pageW, pageH);
      run("Image inserted", () => insertImage(docId, currentPage, rect, file, true), [{ kind: "image", bbox: rect }]);
    };
    probe.onerror = () => {
      URL.revokeObjectURL(url);
      notify("Could not read that image file", "error");
    };
    probe.src = url;
  };

  // ─── Arrow-key nudge: preview live, commit once after a pause ───────
  const nudgeRef = useRef<{ dx: number; dy: number; targets: PageObject[] } | null>(null);
  const nudgeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flushNudge = useCallback(async () => {
    if (nudgeTimer.current) clearTimeout(nudgeTimer.current);
    nudgeTimer.current = null;
    const n = nudgeRef.current;
    nudgeRef.current = null;
    if (!n || (n.dx === 0 && n.dy === 0)) {
      setGroupDelta(null);
      return;
    }
    await commitMoves(
      n.targets.map((o) => ({ obj: o, bbox: translateBbox(o.bbox, n.dx, n.dy) })),
      n.targets.length === 1 ? "Nudged" : `${n.targets.length} objects nudged`,
    );
    setGroupDelta(null);
  }, [commitMoves]);

  // never lose a pending nudge on unmount / page change
  useEffect(() => () => {
    if (nudgeTimer.current) clearTimeout(nudgeTimer.current);
  }, []);
  useEffect(() => {
    nudgeRef.current = null;
    if (nudgeTimer.current) clearTimeout(nudgeTimer.current);
    nudgeTimer.current = null;
  }, [docId, currentPage]);

  const nudge = useCallback(
    (dx: number, dy: number) => {
      const targets = selection.filter(isEditable);
      if (!targets.length) return;
      const cur = nudgeRef.current ?? { dx: 0, dy: 0, targets };
      cur.dx += dx;
      cur.dy += dy;
      nudgeRef.current = cur;
      setGroupDelta([cur.dx, cur.dy]);
      if (nudgeTimer.current) clearTimeout(nudgeTimer.current);
      nudgeTimer.current = setTimeout(() => void flushNudge(), NUDGE_COMMIT_MS);
    },
    [selection, flushNudge],
  );

  // keyboard (capture phase, so page navigation / zoom in Editor never sees
  // keys we consume): Delete, Escape, arrows, Cmd+D, Cmd+A
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (e.target as HTMLElement)?.isContentEditable) return;
      const mod = e.metaKey || e.ctrlKey;
      if ((e.key === "Delete" || e.key === "Backspace") && selection.length && !cropping) {
        e.preventDefault();
        e.stopPropagation();
        doDelete();
      } else if (mod && !e.altKey && e.key.toLowerCase() === "d" && selection.length && !cropping) {
        e.preventDefault();
        e.stopPropagation();
        doDuplicate();
      } else if (mod && !e.altKey && e.key.toLowerCase() === "a" && tool === "select" && !cropping) {
        e.preventDefault();
        e.stopPropagation();
        setSelectedIds(objects.filter(isEditable).map((o) => o.id));
      } else if (!mod && !e.altKey && selection.length && !cropping && tool === "select") {
        const d = nudgeDelta(e.key, e.shiftKey);
        if (!d) return;
        e.preventDefault();
        e.stopPropagation();
        if (!busy) nudge(d[0], d[1]);
      } else if (e.key === "Escape") {
        if (cropping) {
          setCropping(false);
          setCropRect(null);
        } else if (tool !== "select") setTool("select");
        else setSelectedIds([]);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [selection, objects, cropping, tool, busy, doDelete, doDuplicate, nudge]);

  /** Box to draw for an object right now (live drag / nudge preview). */
  const liveBbox = (o: PageObject): Bbox => {
    const isSel = selectedIds.includes(o.id);
    if (isSel && draft && selected?.id === o.id) return draft;
    if (isSel && groupDelta && isEditable(o)) return translateBbox(o.bbox, groupDelta[0], groupDelta[1]);
    return o.bbox;
  };

  // ─── Render ─────────────────────────────────────────────────────────
  const shapePreview =
    drag?.kind === "shape" && tool !== "select" ? { a: drag.start, b: drag.cur, t: tool } : null;

  const btn =
    "p-2 rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40 disabled:pointer-events-none";
  const btnOn = "p-2 rounded-lg bg-blue-100 dark:bg-blue-900/50 text-blue-600 dark:text-blue-400";

  const arrangeButtons = (
    <>
      <button title="Duplicate (Ctrl/Cmd+D)" aria-label="Duplicate" className={btn} disabled={busy || cropping} onClick={doDuplicate}>
        <Copy className="w-4 h-4" />
      </button>
      <button title="Bring to front" aria-label="Bring to front" className={btn} disabled={busy || cropping} onClick={() => doArrange("front")}>
        <BringToFront className="w-4 h-4" />
      </button>
      <button title="Send to back" aria-label="Send to back" className={btn} disabled={busy || cropping} onClick={() => doArrange("back")}>
        <SendToBack className="w-4 h-4" />
      </button>
    </>
  );

  const toolbar = (
    <div
      className="fixed bottom-20 sm:bottom-6 left-1/2 -translate-x-1/2 z-[60] max-w-[calc(100vw-1rem)] overflow-x-auto"
      data-testid="objects-toolbar"
    >
      <div className="flex items-center gap-1 px-2 py-1.5 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-xl shadow-xl text-gray-800 dark:text-gray-100">
        {(
          [
            ["select", MousePointer2, "Select / move objects"],
            ["rect", Square, "Rectangle"],
            ["ellipse", Circle, "Ellipse"],
            ["line", Minus, "Line"],
            ["arrow", ArrowUpRight, "Arrow"],
          ] as const
        ).map(([id, Icon, label]) => (
          <button
            key={id}
            title={label}
            aria-label={label}
            aria-pressed={tool === id}
            className={tool === id ? btnOn : btn}
            onClick={() => {
              setTool(id);
              setCropping(false);
              if (id !== "select") setSelectedIds([]);
            }}
          >
            <Icon className="w-4 h-4" />
          </button>
        ))}
        <button title="Insert image" aria-label="Insert image" className={btn} disabled={busy} onClick={() => insertInputRef.current?.click()}>
          <ImagePlus className="w-4 h-4" />
        </button>

        {tool !== "select" && (
          <div className="flex items-center gap-1.5 pl-1 ml-1 border-l border-gray-200 dark:border-gray-700 text-xs">
            <label className="flex items-center gap-1" title="Stroke colour">
              <input type="color" value={strokeHex} onChange={(e) => setStrokeHex(e.target.value)} className="w-6 h-6 rounded cursor-pointer border border-gray-300 dark:border-gray-600" />
            </label>
            {(tool === "rect" || tool === "ellipse") && (
              <label className="flex items-center gap-1" title="Fill colour">
                <input type="checkbox" checked={useFill} onChange={(e) => setUseFill(e.target.checked)} />
                Fill
                <input type="color" value={fillHex} disabled={!useFill} onChange={(e) => setFillHex(e.target.value)} className="w-6 h-6 rounded cursor-pointer border border-gray-300 dark:border-gray-600 disabled:opacity-40" />
              </label>
            )}
            <label className="flex items-center gap-1" title="Line width (pt)">
              W
              <input
                type="number"
                min={0.5}
                max={50}
                step={0.5}
                value={strokeWidth}
                onChange={(e) => setStrokeWidth(Math.max(0.5, Math.min(50, Number(e.target.value) || 1)))}
                className="w-12 px-1 py-0.5 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800"
              />
            </label>
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={dashed} onChange={(e) => setDashed(e.target.checked)} />
              Dashed
            </label>
          </div>
        )}

        {selected && tool === "select" && (
          <div className="flex items-center gap-1 pl-1 ml-1 border-l border-gray-200 dark:border-gray-700">
            <span className="text-xs text-gray-500 dark:text-gray-400 px-1 whitespace-nowrap">
              {selected.kind === "image"
                ? `Image ${selected.width}×${selected.height}px`
                : `Vector · ${selected.path_count} path${selected.path_count === 1 ? "" : "s"}`}
            </span>
            {selected.kind === "image" && selected.editable && (
              <>
                {cropping ? (
                  <>
                    <button title="Apply crop" aria-label="Apply crop" className={btnOn} disabled={!cropRect || busy} onClick={applyCrop}>
                      <Check className="w-4 h-4" />
                    </button>
                    <button title="Cancel crop" aria-label="Cancel crop" className={btn} onClick={() => { setCropping(false); setCropRect(null); }}>
                      <X className="w-4 h-4" />
                    </button>
                  </>
                ) : (
                  <button title="Crop (drag over the image)" aria-label="Crop" className={btn} disabled={busy} onClick={() => setCropping(true)}>
                    <Crop className="w-4 h-4" />
                  </button>
                )}
                <button title="Rotate 90° clockwise" aria-label="Rotate" className={btn} disabled={busy || cropping} onClick={doRotate}>
                  <RotateCw className="w-4 h-4" />
                </button>
                <button title="Replace image" aria-label="Replace image" className={btn} disabled={busy || cropping} onClick={() => replaceInputRef.current?.click()}>
                  <Replace className="w-4 h-4" />
                </button>
                <label className="flex items-center gap-1 text-[11px] text-gray-500 dark:text-gray-400 whitespace-nowrap" title="Replace every use of this image in the document">
                  <input type="checkbox" checked={replaceAll} onChange={(e) => setReplaceAll(e.target.checked)} />
                  all uses
                </label>
              </>
            )}
            {selected.kind === "image" && selected.xref > 0 && (
              <a
                title="Download original image"
                aria-label="Download image"
                className={btn}
                href={getImageExtractUrl(docId, selected.xref, "original")}
                download
              >
                <Download className="w-4 h-4" />
              </a>
            )}
            {(selected.kind === "drawing" || selected.editable) && arrangeButtons}
            {(selected.kind === "drawing" || selected.editable) && (
              <button title="Delete (Del)" aria-label="Delete object" className={`${btn} hover:text-red-600`} disabled={busy || cropping} onClick={doDelete}>
                <Trash2 className="w-4 h-4" />
              </button>
            )}
          </div>
        )}

        {multi && tool === "select" && (
          <div className="flex items-center gap-0.5 pl-1 ml-1 border-l border-gray-200 dark:border-gray-700" data-testid="objects-multi-actions">
            <span className="text-xs text-gray-500 dark:text-gray-400 px-1 whitespace-nowrap">{selection.length} objects</span>
            {(
              [
                ["left", AlignStartVertical, "Align left"],
                ["center", AlignCenterVertical, "Align centre"],
                ["right", AlignEndVertical, "Align right"],
                ["top", AlignStartHorizontal, "Align top"],
                ["middle", AlignCenterHorizontal, "Align middle"],
                ["bottom", AlignEndHorizontal, "Align bottom"],
              ] as const
            ).map(([mode, Icon, label]) => (
              <button key={mode} title={label} aria-label={label} className={btn} disabled={busy} onClick={() => doAlign(mode)}>
                <Icon className="w-4 h-4" />
              </button>
            ))}
            <button title="Distribute horizontally (3+)" aria-label="Distribute horizontally" className={btn} disabled={busy || selection.length < 3} onClick={() => doDistribute("horizontal")}>
              <AlignHorizontalDistributeCenter className="w-4 h-4" />
            </button>
            <button title="Distribute vertically (3+)" aria-label="Distribute vertically" className={btn} disabled={busy || selection.length < 3} onClick={() => doDistribute("vertical")}>
              <AlignVerticalDistributeCenter className="w-4 h-4" />
            </button>
            {arrangeButtons}
            <button title="Delete selected (Del)" aria-label="Delete selected objects" className={`${btn} hover:text-red-600`} disabled={busy} onClick={doDelete}>
              <Trash2 className="w-4 h-4" />
            </button>
          </div>
        )}

        {busy && <Loader2 className="w-4 h-4 animate-spin text-blue-500 mx-1" aria-hidden="true" />}
        {/* Live regions: errors are announced assertively, results politely. Always
            mounted (empty when idle) so screen readers pick up changes. */}
        <span
          role="alert"
          aria-live="assertive"
          data-testid="objects-error"
          className={error && !busy ? "text-xs text-red-600 dark:text-red-400 max-w-[220px] truncate px-1" : "sr-only"}
          title={error ?? undefined}
        >
          {error && !busy ? error : ""}
        </span>
        <span role="status" aria-live="polite" className="sr-only" data-testid="objects-status">
          {status}
        </span>
        {onExit && (
          <button title="Exit objects mode" aria-label="Exit objects mode" className={btn} onClick={onExit}>
            <X className="w-4 h-4" />
          </button>
        )}
      </div>
      <input ref={replaceInputRef} type="file" accept="image/*" className="hidden" onChange={onReplaceFile} />
      <input ref={insertInputRef} type="file" accept="image/*" className="hidden" onChange={onInsertFile} />
    </div>
  );

  return (
    <>
      <div
        ref={overlayRef}
        data-testid="objects-overlay"
        className="absolute inset-0 z-20 select-none"
        style={{ cursor: tool !== "select" ? "crosshair" : cropping ? "crosshair" : "default", touchAction: "none" }}
        onPointerDown={onBackgroundPointerDown}
      >
        {data &&
          objects.map((obj) => {
            const isSel = selectedIds.includes(obj.id);
            const isOnly = isSel && selected?.id === obj.id;
            const b = liveBbox(obj);
            const isImage = obj.kind === "image";
            const editable = !isImage || obj.editable;
            const bg = obj.kind === "drawing" && obj.background;
            return (
              <div
                key={obj.id}
                data-testid={`obj-${obj.id}`}
                aria-selected={isSel}
                className={`absolute ${
                  isSel
                    ? "outline outline-2 outline-blue-500 bg-blue-500/5"
                    : bg
                      ? "hover:outline hover:outline-1 hover:outline-dashed hover:outline-gray-400/60"
                      : isImage
                        ? "outline outline-1 outline-dashed outline-emerald-500/60 hover:outline-2 hover:bg-emerald-500/5"
                        : "outline outline-1 outline-dashed outline-violet-500/50 hover:outline-2 hover:bg-violet-500/5"
                }`}
                style={{
                  ...bboxToPercentStyle(b, pageW, pageH),
                  minWidth: 4,
                  minHeight: 4,
                  cursor: tool !== "select" ? "crosshair" : cropping ? "crosshair" : editable ? "move" : "pointer",
                  pointerEvents: tool !== "select" || (cropping && isSel) ? "none" : "auto",
                }}
                title={
                  isImage
                    ? `Image ${obj.width}×${obj.height}px${obj.method === "redact" ? " (inside a form)" : ""}${!obj.editable ? " (inline image, read-only)" : ""}`
                    : `Vector object (${obj.path_count} paths)`
                }
                onPointerDown={(e) => tool === "select" && startObjectDrag(e, obj, "move")}
              >
                {isSel && (draft || groupDelta) && isImage && obj.xref > 0 && (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={getImageExtractUrl(docId, obj.xref, "png")}
                    alt=""
                    className="w-full h-full opacity-60 pointer-events-none"
                    style={{ objectFit: "fill" }}
                  />
                )}
                {isOnly && editable && !cropping && tool === "select" &&
                  HANDLES.map((h) => (
                    <div
                      key={h}
                      data-testid={`handle-${h}`}
                      className="absolute w-2.5 h-2.5 -ml-[5px] -mt-[5px] bg-white dark:bg-gray-900 border-2 border-blue-500 rounded-sm"
                      style={{ ...handlePos(h), cursor: HANDLE_CURSOR[h] }}
                      onPointerDown={(e) => startObjectDrag(e, obj, h)}
                    />
                  ))}
              </div>
            );
          })}

        {/* marquee */}
        {drag?.kind === "marquee" && (
          <div
            data-testid="objects-marquee"
            className="absolute pointer-events-none border border-blue-500 bg-blue-500/10"
            style={bboxToPercentStyle(normalizeBbox([drag.start[0], drag.start[1], drag.cur[0], drag.cur[1]]), pageW, pageH)}
          />
        )}

        {/* crop selection */}
        {cropping && selected && (
          <>
            <div
              className="absolute pointer-events-none outline outline-2 outline-dashed outline-amber-500"
              style={bboxToPercentStyle(selected.bbox, pageW, pageH)}
            />
            {cropRect && (
              <div
                className="absolute pointer-events-none border-2 border-amber-500 bg-amber-400/20"
                style={bboxToPercentStyle(cropRect, pageW, pageH)}
              />
            )}
          </>
        )}

        {/* shape preview (SVG in PDF-point viewBox: exact geometry at any zoom) */}
        {shapePreview && data && (
          <svg
            className="absolute inset-0 w-full h-full pointer-events-none"
            viewBox={`0 0 ${pageW} ${pageH}`}
            preserveAspectRatio="none"
          >
            {(() => {
              const { a, b, t } = shapePreview;
              const stroke = strokeHex;
              const fill = useFill && (t === "rect" || t === "ellipse") ? fillHex : "none";
              const dash = dashed ? `${strokeWidth * 3}` : undefined;
              if (t === "rect") {
                const r = normalizeBbox([a[0], a[1], b[0], b[1]]);
                return <rect x={r[0]} y={r[1]} width={r[2] - r[0]} height={r[3] - r[1]} stroke={stroke} fill={fill} strokeWidth={strokeWidth} strokeDasharray={dash} />;
              }
              if (t === "ellipse") {
                const r = normalizeBbox([a[0], a[1], b[0], b[1]]);
                return <ellipse cx={(r[0] + r[2]) / 2} cy={(r[1] + r[3]) / 2} rx={(r[2] - r[0]) / 2} ry={(r[3] - r[1]) / 2} stroke={stroke} fill={fill} strokeWidth={strokeWidth} strokeDasharray={dash} />;
              }
              return (
                <>
                  <defs>
                    <marker id="objects-arrowhead" markerWidth="4" markerHeight="4" refX="3" refY="2" orient="auto" markerUnits="strokeWidth">
                      <path d="M0,0 L4,2 L0,4 z" fill={stroke} />
                    </marker>
                  </defs>
                  <line
                    x1={a[0]}
                    y1={a[1]}
                    x2={b[0]}
                    y2={b[1]}
                    stroke={stroke}
                    strokeWidth={strokeWidth}
                    strokeDasharray={dash}
                    markerEnd={t === "arrow" ? "url(#objects-arrowhead)" : undefined}
                  />
                </>
              );
            })()}
          </svg>
        )}
      </div>
      {mounted && typeof document !== "undefined" ? createPortal(toolbar, document.body) : null}
    </>
  );
}
