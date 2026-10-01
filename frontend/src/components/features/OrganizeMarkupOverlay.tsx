"use client";

// Transparent layer placed exactly over the rendered page (absolute inset-0
// inside the page wrapper). It converts mouse gestures to VISIBLE PAGE POINTS
// with clientToPagePoint(), which divides by the element's on-screen size, so
// it is correct at any render DPI / pdf.js scale / CSS zoom transform. It
// previews the gesture in an SVG whose viewBox is the page in points, then
// POSTs a real PDF annotation via createComment().

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import {
  clientToPagePoint, createComment, gestureToComment, rectFromPoints, TEXT_ENTRY_TOOLS, TEXT_MARKUP_TOOLS,
  type MarkupGesture, type MarkupSettings, type PdfComment,
} from "@/lib/features/organize";

export interface OrganizeMarkupOverlayProps {
  docId: string;
  /** 0-based page index being shown */
  page: number;
  /** Visible page size in PDF points (DocumentInfo.pages[page].width / height) */
  pageWidth: number;
  pageHeight: number;
  settings: MarkupSettings;
  /** Called after the annotation is saved. Re-render the page and refresh the comments panel. */
  onCreated: (comment: PdfComment) => void;
  onError?: (message: string) => void;
}

type Pt = [number, number];

export default function OrganizeMarkupOverlay({
  docId, page, pageWidth, pageHeight, settings, onCreated, onError,
}: OrganizeMarkupOverlayProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [start, setStart] = useState<Pt | null>(null);
  const [cur, setCur] = useState<Pt | null>(null);
  const [path, setPath] = useState<Pt[]>([]);
  const [pendingText, setPendingText] = useState<MarkupGesture | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const tool = settings.tool;

  const reset = useCallback(() => {
    setStart(null); setCur(null); setPath([]); setPendingText(null); setText("");
  }, []);

  useEffect(() => { reset(); }, [tool, page, reset]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") reset(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [reset]);

  const toPt = (e: { clientX: number; clientY: number }): Pt => {
    const box = ref.current!.getBoundingClientRect();
    return clientToPagePoint(e.clientX, e.clientY, box, pageWidth, pageHeight);
  };

  const fail = (m: string) => {
    setMsg(m);
    onError?.(m);
    setTimeout(() => setMsg(null), 2500);
  };

  const submit = async (g: MarkupGesture) => {
    let body;
    try {
      body = gestureToComment(page, settings, g, pageWidth, pageHeight);
    } catch (e) {
      reset();
      return fail(e instanceof Error ? e.message : String(e));
    }
    setBusy(true);
    try {
      const c = await createComment(docId, body);
      onCreated(c);
    } catch (e) {
      fail(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      reset();
    }
  };

  const finishGesture = (g: MarkupGesture) => {
    if (tool && TEXT_ENTRY_TOOLS.includes(tool)) {
      setPendingText(g);
      setText("");
    } else {
      submit(g);
    }
  };

  if (!tool) return null;

  const onMouseDown = (e: React.MouseEvent) => {
    if (e.button !== 0 || busy || pendingText) return;
    e.preventDefault();
    e.stopPropagation();
    const p = toPt(e);
    if (tool === "polygon") {
      if (path.length >= 3 && Math.hypot(p[0] - path[0][0], p[1] - path[0][1]) < 8) {
        submit({ start: path[0], end: path[path.length - 1], path });
      } else {
        setPath([...path, p]);
      }
      return;
    }
    setStart(p);
    setCur(p);
    if (tool === "ink") setPath([p]);
  };

  const onMouseMove = (e: React.MouseEvent) => {
    const p = toPt(e);
    if (tool === "polygon") { setCur(p); return; }
    if (!start) return;
    setCur(p);
    if (tool === "ink") setPath((old) => [...old, p]);
  };

  const onMouseUp = (e: React.MouseEvent) => {
    if (!start || tool === "polygon") return;
    e.stopPropagation();
    const end = toPt(e);
    const g: MarkupGesture = { start, end, path: tool === "ink" ? [...path, end] : undefined };
    setStart(null);
    finishGesture(g);
  };

  const onDoubleClick = (e: React.MouseEvent) => {
    if (tool !== "polygon") return;
    e.preventDefault();
    // the double-click's two mousedowns added the same point twice; drop the duplicate
    const pts = path.filter((p, i) => i === 0 || Math.hypot(p[0] - path[i - 1][0], p[1] - path[i - 1][1]) > 0.5);
    submit({ start: pts[0], end: pts[pts.length - 1], path: pts });
  };

  // ─── preview ────────────────────────────────────────────────────────────
  const stroke = settings.color;
  const sw = Math.max(0.75, settings.width);
  let preview: React.ReactNode = null;
  if (start && cur) {
    const [x0, y0, x1, y1] = rectFromPoints(start, cur);
    if (TEXT_MARKUP_TOOLS.includes(tool)) {
      preview = <rect x={x0} y={y0} width={x1 - x0} height={y1 - y0} fill={stroke} fillOpacity={0.25} stroke={stroke} strokeDasharray="3 2" strokeWidth={0.75} />;
    } else if (tool === "rect" || tool === "freetext" || tool === "stamp") {
      preview = <rect x={x0} y={y0} width={x1 - x0} height={y1 - y0} fill={settings.fill ?? "none"} stroke={stroke} strokeWidth={sw} strokeDasharray={tool === "rect" ? undefined : "4 3"} />;
    } else if (tool === "ellipse") {
      preview = <ellipse cx={(x0 + x1) / 2} cy={(y0 + y1) / 2} rx={(x1 - x0) / 2} ry={(y1 - y0) / 2} fill={settings.fill ?? "none"} stroke={stroke} strokeWidth={sw} />;
    } else if (tool === "line" || tool === "arrow" || tool === "callout") {
      preview = (
        <>
          <line x1={start[0]} y1={start[1]} x2={cur[0]} y2={cur[1]} stroke={stroke} strokeWidth={sw} markerStart={tool !== "line" ? "url(#org-arrow)" : undefined} />
          {tool === "callout" && <rect x={cur[0]} y={cur[1] - 22} width={180} height={44} fill="none" stroke={stroke} strokeDasharray="4 3" strokeWidth={0.75} />}
        </>
      );
    } else if (tool === "ink") {
      preview = <polyline points={path.map((p) => p.join(",")).join(" ")} fill="none" stroke={stroke} strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round" />;
    }
  }
  if (tool === "polygon" && path.length) {
    const pts = cur ? [...path, cur] : path;
    preview = (
      <>
        <polyline points={pts.map((p) => p.join(",")).join(" ")} fill={settings.fill ?? "none"} fillOpacity={0.4} stroke={stroke} strokeWidth={sw} />
        <circle cx={path[0][0]} cy={path[0][1]} r={4} fill="white" stroke={stroke} strokeWidth={1} />
      </>
    );
  }

  const popupAt = pendingText ? (tool === "callout" ? pendingText.end : pendingText.start) : null;

  return (
    <div
      ref={ref}
      data-testid="organize-markup-overlay"
      className="absolute inset-0 z-20"
      style={{ cursor: tool === "note" || tool === "stamp" ? "copy" : "crosshair" }}
      onMouseDown={onMouseDown}
      onMouseMove={onMouseMove}
      onMouseUp={onMouseUp}
      onDoubleClick={onDoubleClick}
      onMouseLeave={(e) => { if (start && tool !== "polygon" && tool !== "ink") onMouseUp(e); }}
    >
      <svg className="absolute inset-0 w-full h-full pointer-events-none" viewBox={`0 0 ${pageWidth} ${pageHeight}`} preserveAspectRatio="none">
        <defs>
          <marker id="org-arrow" viewBox="0 0 10 10" refX="1" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
            <path d="M 10 0 L 0 5 L 10 10" fill="none" stroke={stroke} strokeWidth={1.5} />
          </marker>
        </defs>
        <g opacity={settings.opacity}>{preview}</g>
      </svg>

      {pendingText && popupAt && (
        <div
          className="absolute z-30 w-56 p-2 rounded-lg shadow-xl bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700"
          style={{ left: `${(popupAt[0] / pageWidth) * 100}%`, top: `${(popupAt[1] / pageHeight) * 100}%` }}
          onMouseDown={(e) => e.stopPropagation()}
          onMouseUp={(e) => e.stopPropagation()}
        >
          <textarea
            autoFocus rows={3} value={text} onChange={(e) => setText(e.target.value)}
            placeholder={tool === "note" ? "Comment" : "Text"}
            className="w-full text-xs p-1.5 rounded border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-950 text-gray-800 dark:text-gray-100"
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit({ ...pendingText, text });
              if (e.key === "Escape") reset();
            }}
          />
          <div className="flex justify-end gap-1 mt-1">
            <button className="px-2 py-0.5 text-xs rounded hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-300" onClick={reset}>Cancel</button>
            <button
              className="px-2 py-0.5 text-xs rounded bg-blue-600 text-white disabled:opacity-50"
              disabled={tool !== "note" && !text.trim()}
              onClick={() => submit({ ...pendingText, text })}
            >Add</button>
          </div>
        </div>
      )}

      {(busy || msg) && (
        <div className="absolute top-2 left-1/2 -translate-x-1/2 z-30 px-2.5 py-1 rounded-full text-xs shadow bg-gray-900/85 text-white flex items-center gap-1.5">
          {busy && <Loader2 className="w-3 h-3 animate-spin" />}{busy ? "Saving" : msg}
        </div>
      )}
    </div>
  );
}
