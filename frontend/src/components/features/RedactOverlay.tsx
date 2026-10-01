"use client";

/**
 * RedactOverlay — sits exactly on top of the rendered page (absolute inset-0
 * inside the page wrapper) and draws/captures redaction marks.
 *
 * Coordinates: boxes are positioned in % of the page's visible size in PDF
 * points (pageWidth × pageHeight), and pointer events are converted with the
 * overlay's own getBoundingClientRect(), so any render DPI, CSS zoom or
 * transform: scale() on a parent is handled without extra props.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import {
  clientToPdfPoint,
  deleteRedactMark,
  getRedactWords,
  markRedactions,
  rectFromPoints,
  rectToPercentStyle,
  rectsIntersect,
  useRedactStore,
  wordsToAreas,
  type PdfRect,
  type RedactWord,
} from "@/lib/features/redact";

export interface RedactOverlayProps {
  docId: string;
  currentPage: number;
  /** Visible page width in PDF points (DocumentInfo.pages[i].width). */
  pageWidth: number;
  /** Visible page height in PDF points (DocumentInfo.pages[i].height). */
  pageHeight: number;
  /** Called after a mark is added/removed (re-render the page image). */
  onDocumentChanged: () => void;
  onError?: (message: string) => void;
}

const MIN_DRAG_PT = 3;

export default function RedactOverlay({
  docId, currentPage, pageWidth, pageHeight, onDocumentChanged, onError,
}: RedactOverlayProps) {
  const { active, tool, style, marks, previewMatches, refreshMarks } = useRedactStore();
  const ref = useRef<HTMLDivElement>(null);
  const [start, setStartState] = useState<[number, number] | null>(null);
  // Mirror in a ref so pointerup never reads a stale closure.
  const startRef = useRef<[number, number] | null>(null);
  const setStart = (p: [number, number] | null) => { startRef.current = p; setStartState(p); };
  const [current, setCurrent] = useState<[number, number] | null>(null);
  const [words, setWords] = useState<RedactWord[]>([]);
  const [hoverWord, setHoverWord] = useState<number | null>(null);

  // Load marks on mount / doc change.
  useEffect(() => {
    refreshMarks(docId).catch(() => {});
  }, [docId, refreshMarks]);

  // Words for click-to-mark.
  useEffect(() => {
    if (!active || tool !== "word") return;
    let cancelled = false;
    getRedactWords(docId, currentPage)
      .then((r) => { if (!cancelled) setWords(r.words); })
      .catch(() => { if (!cancelled) setWords([]); });
    return () => { cancelled = true; };
  }, [active, tool, docId, currentPage, marks.length]);

  const toPdf = useCallback(
    (e: React.PointerEvent): [number, number] | null => {
      const el = ref.current;
      if (!el || !pageWidth || !pageHeight) return null;
      return clientToPdfPoint(e.clientX, e.clientY, el.getBoundingClientRect(), pageWidth, pageHeight);
    },
    [pageWidth, pageHeight],
  );

  const commit = useCallback(
    async (areas: { page: number; rect: PdfRect }[], label: string) => {
      if (!areas.length) return;
      try {
        await markRedactions(docId, areas, style, label);
        await refreshMarks(docId);
        onDocumentChanged();
      } catch (err) {
        onError?.(err instanceof Error ? err.message : "Failed to mark redaction");
      }
    },
    [docId, style, refreshMarks, onDocumentChanged, onError],
  );

  const onPointerDown = (e: React.PointerEvent) => {
    if (!active || e.button !== 0) return;
    const p = toPdf(e);
    if (!p) return;
    (e.target as Element).setPointerCapture?.(e.pointerId);
    setStart(p);
    setCurrent(p);
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const p = toPdf(e);
    if (!p) return;
    if (startRef.current) setCurrent(p);
    if (active && tool === "word" && !start) {
      const idx = words.findIndex((w) => p[0] >= w.rect[0] && p[0] <= w.rect[2] && p[1] >= w.rect[1] && p[1] <= w.rect[3]);
      setHoverWord(idx >= 0 ? idx : null);
    }
  };

  const onPointerUp = async (e: React.PointerEvent) => {
    const origin = startRef.current;
    if (!origin) return;
    const end = toPdf(e) ?? current ?? origin;
    const r = rectFromPoints(origin, end);
    setStart(null);
    setCurrent(null);
    const isClick = r[2] - r[0] < MIN_DRAG_PT && r[3] - r[1] < MIN_DRAG_PT;

    if (tool === "area") {
      if (isClick) return;
      await commit([{ page: currentPage, rect: r }], "Area");
      return;
    }
    // word tool: click → the word under the pointer; drag → all words touched
    const hit = isClick
      ? words.filter((w) => end[0] >= w.rect[0] && end[0] <= w.rect[2] && end[1] >= w.rect[1] && end[1] <= w.rect[3])
      : words.filter((w) => rectsIntersect(w.rect, r));
    if (!hit.length) return;
    await commit(wordsToAreas(hit, currentPage), hit.map((w) => w.text).join(" ").slice(0, 200));
  };

  const removeMark = async (page: number, xref: number) => {
    try {
      await deleteRedactMark(docId, page, xref);
      await refreshMarks(docId);
      onDocumentChanged();
    } catch (err) {
      onError?.(err instanceof Error ? err.message : "Failed to remove mark");
    }
  };

  if (!pageWidth || !pageHeight) return null;
  const pageMarks = marks.filter((m) => m.page === currentPage);
  const pagePreview = previewMatches.filter((m) => m.page === currentPage);
  const drag = start && current ? rectFromPoints(start, current) : null;

  return (
    <div
      ref={ref}
      data-testid="redact-overlay"
      className="absolute inset-0 z-20 select-none"
      style={{
        pointerEvents: active ? "auto" : "none",
        cursor: active ? (tool === "word" ? "text" : "crosshair") : undefined,
        touchAction: active ? "none" : undefined,
      }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerLeave={() => setHoverWord(null)}
    >
      {/* hover word */}
      {active && tool === "word" && hoverWord !== null && words[hoverWord] && (
        <div
          className="absolute bg-red-500/20 ring-1 ring-red-400 rounded-sm"
          style={rectToPercentStyle(words[hoverWord].rect, pageWidth, pageHeight)}
        />
      )}

      {/* ticked search results not yet marked */}
      {pagePreview.flatMap((m) =>
        m.rects.map((r, i) => (
          <div
            key={`${m.id}-${i}`}
            title={`${m.kind}: ${m.text}`}
            className="absolute border-2 border-dashed border-orange-500 bg-orange-400/15"
            style={rectToPercentStyle(r, pageWidth, pageHeight)}
          />
        )),
      )}

      {/* pending marks: red outlined boxes */}
      {pageMarks.map((m) => (
        <div
          key={m.xref}
          className="group absolute border-2 border-red-600 bg-red-500/15 dark:bg-red-500/25"
          style={rectToPercentStyle(m.rect, pageWidth, pageHeight)}
          title={m.label || "Marked for redaction"}
        >
          {m.overlay_text && (
            <span className="absolute inset-0 flex items-center justify-center text-[10px] font-semibold text-red-700 dark:text-red-300 overflow-hidden">
              {m.overlay_text}
            </span>
          )}
          <button
            type="button"
            aria-label="Remove redaction mark"
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => { e.stopPropagation(); removeMark(m.page, m.xref); }}
            className="absolute -top-2.5 -right-2.5 hidden group-hover:flex w-5 h-5 items-center justify-center rounded-full bg-red-600 text-white shadow pointer-events-auto"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
      ))}

      {/* rubber band */}
      {drag && (
        <div
          className={`absolute border-2 ${tool === "area" ? "border-red-600 bg-red-500/20" : "border-red-400 border-dashed bg-red-300/10"}`}
          style={rectToPercentStyle(drag, pageWidth, pageHeight)}
        />
      )}
    </div>
  );
}
