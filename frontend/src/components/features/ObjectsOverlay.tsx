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
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tool, setTool] = useState<Tool>("select");
  const [drag, setDrag] = useState<DragState | null>(null);
  const [draft, setDraft] = useState<Bbox | null>(null); // live bbox while moving/resizing
  const [cropping, setCropping] = useState(false);
  const [cropRect, setCropRect] = useState<Bbox | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
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
  const pendingReselect = useRef<{ kind: PageObject["kind"]; bbox: Bbox } | null>(null);

  useEffect(() => {
    let cancelled = false;
    listObjects(docId, currentPage)
      .then((d) => {
        if (cancelled) return;
        setData(d);
        const want = pendingReselect.current;
        pendingReselect.current = null;
        if (want) {
          const list: PageObject[] = want.kind === "image" ? d.images : d.drawings;
          const hit = list.find((o) => bboxEquals(o.bbox, want.bbox, 1.5));
          setSelectedId(hit ? hit.id : null);
        }
      })
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [docId, currentPage, pageVersion, localVersion]);

  // reset on page change
  useEffect(() => {
    setSelectedId(null);
    setCropping(false);
    setCropRect(null);
    setDrag(null);
    setDraft(null);
  }, [docId, currentPage]);

  const objects: PageObject[] = useMemo(() => {
    if (!data) return [];
    // larger first so smaller objects sit on top and stay clickable
    return [...data.drawings, ...data.images].sort((a, b) => area(b.bbox) - area(a.bbox));
  }, [data]);

  const selected = objects.find((o) => o.id === selectedId) ?? null;
  const pageW = data?.page_width ?? 1;
  const pageH = data?.page_height ?? 1;

  const notify = useCallback(
    (msg: string, type: "success" | "error" | "info") => {
      if (type === "error") setError(msg);
      onNotify?.(msg, type);
    },
    [onNotify],
  );

  const run = useCallback(
    async (label: string, fn: () => Promise<unknown>, reselect?: { kind: PageObject["kind"]; bbox: Bbox }) => {
      setBusy(true);
      setError(null);
      try {
        await fn();
        pendingReselect.current = reselect ?? null;
        if (!reselect) setSelectedId(null);
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
    setSelectedId(obj.id);
    if (cropping) return;
    const editable = obj.kind === "drawing" || obj.editable;
    if (!editable) return;
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
    setSelectedId(null);
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
      } else if (drag.kind === "shape") {
        setDrag({ ...drag, cur: p });
      } else {
        setDrag({ ...drag, cur: p });
        const r = normalizeBbox([drag.start[0], drag.start[1], p[0], p[1]]);
        const hit = selected ? intersectBbox(r, selected.bbox) : null;
        setCropRect(hit ? roundBbox(hit) : null);
      }
    };
    const onUp = (e: PointerEvent) => {
      const p = toPdf(e.clientX, e.clientY);
      const d = drag;
      setDrag(null);
      if (d.kind === "object") {
        const dx = p[0] - d.start[0];
        const dy = p[1] - d.start[1];
        let next = applyDrag(d.orig, d.mode, dx, dy, e.shiftKey);
        if (d.mode === "move") next = clampToPage(next, pageW, pageH);
        next = roundBbox(next);
        setDraft(null);
        if (bboxEquals(next, d.orig, 0.5)) return; // a click, not a drag
        const obj = d.obj;
        if (obj.kind === "image") {
          run("Image moved", () => moveImage(docId, currentPage, obj, next), { kind: "image", bbox: next });
        } else {
          run("Object moved", () => moveDrawing(docId, currentPage, obj, next), { kind: "drawing", bbox: next });
        }
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
  }, [drag, toPdf, pageW, pageH, selected, docId, currentPage, run]);

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
    if (!selected || busy) return;
    if (selected.kind === "image") {
      if (!selected.editable) return;
      run("Image deleted", () => deleteImage(docId, currentPage, selected));
    } else {
      run("Object deleted", () => deleteDrawing(docId, currentPage, selected));
    }
  }, [selected, busy, run, docId, currentPage]);

  const doRotate = () => {
    if (selected?.kind !== "image") return;
    const b = selected.bbox;
    const cx = (b[0] + b[2]) / 2;
    const cy = (b[1] + b[3]) / 2;
    const hw = (b[3] - b[1]) / 2;
    const hh = (b[2] - b[0]) / 2;
    run("Image rotated", () => rotateImage(docId, currentPage, selected, 90), {
      kind: "image",
      bbox: [cx - hw, cy - hh, cx + hw, cy + hh],
    });
  };

  const applyCrop = () => {
    if (selected?.kind !== "image" || !cropRect) return;
    const img = selected;
    const r = cropRect;
    setCropping(false);
    setCropRect(null);
    run("Image cropped", () => cropImage(docId, currentPage, img, r), { kind: "image", bbox: r });
  };

  const onReplaceFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || selected?.kind !== "image") return;
    const img = selected;
    run("Image replaced", () => replaceImage(docId, currentPage, img, file, replaceAll ? "all" : "placement"), {
      kind: "image",
      bbox: img.bbox,
    });
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
      run("Image inserted", () => insertImage(docId, currentPage, rect, file, true), { kind: "image", bbox: rect });
    };
    probe.onerror = () => {
      URL.revokeObjectURL(url);
      notify("Could not read that image file", "error");
    };
    probe.src = url;
  };

  // keyboard: Delete removes selection, Escape cancels
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || (e.target as HTMLElement)?.isContentEditable) return;
      if ((e.key === "Delete" || e.key === "Backspace") && selected && !cropping) {
        e.preventDefault();
        doDelete();
      } else if (e.key === "Escape") {
        if (cropping) {
          setCropping(false);
          setCropRect(null);
        } else if (tool !== "select") setTool("select");
        else setSelectedId(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected, cropping, tool, doDelete]);

  // ─── Render ─────────────────────────────────────────────────────────
  const shapePreview =
    drag?.kind === "shape" && tool !== "select" ? { a: drag.start, b: drag.cur, t: tool } : null;

  const btn =
    "p-2 rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40 disabled:pointer-events-none";
  const btnOn = "p-2 rounded-lg bg-blue-100 dark:bg-blue-900/50 text-blue-600 dark:text-blue-400";

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
              if (id !== "select") setSelectedId(null);
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
            {(selected.kind === "drawing" || selected.editable) && (
              <button title="Delete (Del)" aria-label="Delete object" className={`${btn} hover:text-red-600`} disabled={busy || cropping} onClick={doDelete}>
                <Trash2 className="w-4 h-4" />
              </button>
            )}
          </div>
        )}

        {busy && <Loader2 className="w-4 h-4 animate-spin text-blue-500 mx-1" />}
        {error && !busy && (
          <span className="text-xs text-red-600 dark:text-red-400 max-w-[220px] truncate px-1" title={error}>
            {error}
          </span>
        )}
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
            const isSel = obj.id === selectedId;
            const b = isSel && draft ? draft : obj.bbox;
            const isImage = obj.kind === "image";
            const editable = !isImage || obj.editable;
            const bg = obj.kind === "drawing" && obj.background;
            return (
              <div
                key={obj.id}
                data-testid={`obj-${obj.id}`}
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
                    ? `Image ${obj.width}×${obj.height}px${obj.method === "redact" ? " (inside a form; edits re-place it on top)" : ""}${!obj.editable ? " (inline image, read-only)" : ""}`
                    : `Vector object (${obj.path_count} paths)`
                }
                onPointerDown={(e) => tool === "select" && startObjectDrag(e, obj, "move")}
              >
                {isSel && draft && isImage && obj.xref > 0 && (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={getImageExtractUrl(docId, obj.xref, "png")}
                    alt=""
                    className="w-full h-full opacity-60 pointer-events-none"
                    style={{ objectFit: "fill" }}
                  />
                )}
                {isSel && editable && !cropping && tool === "select" &&
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
