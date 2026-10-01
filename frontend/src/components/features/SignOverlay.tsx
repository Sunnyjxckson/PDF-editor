"use client";

import { useEffect, useRef } from "react";
import { X } from "lucide-react";
import {
  clampRect,
  clientToPdf,
  defaultRectAt,
  dragRect,
  formatToday,
  newId,
  useSignStore,
  type Handle,
  type PlacedItem,
  type Rect,
} from "@/lib/features/sign";

/**
 * Page overlay for Fill & Sign placement. Mount it INSIDE the element that
 * wraps the rendered page image, absolutely covering it (it uses `absolute inset-0`).
 *
 * Props are in PDF points (the page's visible frame, top-left origin, i.e.
 * DocumentInfo.pages[i].width/height). Pixel <-> point conversion uses the
 * overlay's own bounding box, so render DPI, render mode and CSS zoom don't matter.
 */
export interface SignOverlayProps {
  pageIndex: number;
  pageWidthPt: number;
  pageHeightPt: number;
}

export default function SignOverlay({ pageIndex, pageWidthPt, pageHeightPt }: SignOverlayProps) {
  const ref = useRef<HTMLDivElement>(null);
  const {
    active, tool, library, selectedEntryId, items, selectedItemId, inkColor,
    addItem, updateItem, removeItem, selectItem, setTool,
  } = useSignStore();
  const drag = useRef<{ id: string; handle: Handle; start: { x: number; y: number }; orig: Rect; keep: boolean } | null>(null);

  const toPt = (clientX: number, clientY: number) => {
    const box = ref.current!.getBoundingClientRect();
    return clientToPdf(clientX, clientY, box, pageWidthPt, pageHeightPt);
  };

  // Global move/up while dragging an item or handle
  useEffect(() => {
    const move = (e: PointerEvent) => {
      const d = drag.current;
      if (!d || !ref.current) return;
      const p = toPt(e.clientX, e.clientY);
      let r = dragRect(d.orig, d.handle, p.x - d.start.x, p.y - d.start.y, d.keep && !e.shiftKey);
      if (d.handle === "move") r = clampRect(r, pageWidthPt, pageHeightPt);
      const it = useSignStore.getState().items.find((i) => i.id === d.id);
      if (it && (it.type === "text" || it.type === "date") && d.handle !== "move") {
        // Text boxes: height drives the font size (one line ≈ 1.3 × font size).
        updateItem(d.id, { rect: r, fontSize: Math.max(4, Math.round(((r[3] - r[1]) / 1.3) * 10) / 10) });
      } else {
        updateItem(d.id, { rect: r });
      }
    };
    const up = () => {
      drag.current = null;
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageWidthPt, pageHeightPt]);

  // Delete / Escape keys for the selected item
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if ((e.key === "Delete" || e.key === "Backspace") && selectedItemId) {
        e.preventDefault();
        removeItem(selectedItemId);
      } else if (e.key === "Escape") {
        selectItem(null);
        setTool(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, selectedItemId, removeItem, selectItem, setTool]);

  if (!active || pageWidthPt <= 0 || pageHeightPt <= 0) return null;

  const pageItems = items.filter((i) => i.page === pageIndex);

  const placeAt = (e: React.PointerEvent) => {
    if (!tool) {
      selectItem(null);
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    const p = toPt(e.clientX, e.clientY);
    let item: PlacedItem | null = null;
    if (tool === "signature" || tool === "initials") {
      const entry = library.find((l) => l.id === selectedEntryId[tool]) ?? library.find((l) => l.kind === tool);
      if (!entry) return;
      const rect = defaultRectAt(tool, p, pageWidthPt, pageHeightPt, entry.width / entry.height);
      item = { id: newId(), type: "image", page: pageIndex, rect, image: entry.dataUrl, keepAspect: true, sourceKind: tool };
    } else if (tool === "text" || tool === "date") {
      const rect = defaultRectAt(tool, p, pageWidthPt, pageHeightPt);
      item = {
        id: newId(), type: tool, page: pageIndex, rect,
        text: tool === "date" ? formatToday() : "", fontSize: 11, color: inkColor,
      };
    } else {
      const rect = defaultRectAt(tool, p, pageWidthPt, pageHeightPt);
      item = { id: newId(), type: tool, page: pageIndex, rect, color: inkColor, keepAspect: true };
    }
    addItem(item);
    // Signatures are usually placed once; marks/text are often repeated.
    if (tool === "signature" || tool === "initials") setTool(null);
  };

  const startDrag = (e: React.PointerEvent, it: PlacedItem, handle: Handle) => {
    e.preventDefault();
    e.stopPropagation();
    selectItem(it.id);
    drag.current = { id: it.id, handle, start: toPt(e.clientX, e.clientY), orig: it.rect, keep: !!it.keepAspect };
  };

  const pct = (r: Rect) => ({
    left: `${(r[0] / pageWidthPt) * 100}%`,
    top: `${(r[1] / pageHeightPt) * 100}%`,
    width: `${((r[2] - r[0]) / pageWidthPt) * 100}%`,
    height: `${((r[3] - r[1]) / pageHeightPt) * 100}%`,
  });

  return (
    <div
      ref={ref}
      data-testid="sign-overlay"
      className="absolute inset-0 z-30 select-none"
      style={{ cursor: tool ? "crosshair" : "default", containerType: "inline-size" } as React.CSSProperties}
      onPointerDown={placeAt}
    >
      {pageItems.map((it) => {
        const sel = it.id === selectedItemId;
        // Font size in points -> % of page width so it scales with the page (cqw units)
        const fsCqw = ((it.fontSize ?? 11) / pageWidthPt) * 100;
        return (
          <div
            key={it.id}
            className={`absolute group ${sel ? "ring-2 ring-blue-500" : "ring-1 ring-blue-400/40 hover:ring-blue-500"} rounded-sm`}
            style={{ ...pct(it.rect), cursor: "move", background: sel ? "rgba(59,130,246,0.06)" : undefined }}
            onPointerDown={(e) => startDrag(e, it, "move")}
          >
            {it.type === "image" && it.image && (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={it.image} alt="signature" draggable={false} className="w-full h-full object-contain pointer-events-none" />
            )}
            {(it.type === "text" || it.type === "date") && (
              <input
                value={it.text ?? ""}
                autoFocus={sel && it.type === "text" && !it.text}
                placeholder="Type…"
                onPointerDown={(e) => e.stopPropagation()}
                onFocus={() => selectItem(it.id)}
                onChange={(e) => updateItem(it.id, { text: e.target.value })}
                className="w-full h-full bg-transparent outline-none px-0 leading-none"
                style={{ fontSize: `${fsCqw}cqw`, color: it.color, fontFamily: "Helvetica, Arial, sans-serif" }}
              />
            )}
            {(it.type === "check" || it.type === "cross") && (
              <svg viewBox="0 0 100 100" className="w-full h-full pointer-events-none" preserveAspectRatio="none">
                {it.type === "check" ? (
                  <polyline points="8,55 38,85 92,15" fill="none" stroke={it.color} strokeWidth={12} strokeLinecap="round" strokeLinejoin="round" />
                ) : (
                  <g stroke={it.color} strokeWidth={12} strokeLinecap="round">
                    <line x1="12" y1="12" x2="88" y2="88" />
                    <line x1="88" y1="12" x2="12" y2="88" />
                  </g>
                )}
              </svg>
            )}
            {sel && (
              <>
                {(["nw", "ne", "sw", "se"] as const).map((h) => (
                  <span
                    key={h}
                    onPointerDown={(e) => startDrag(e, it, h)}
                    className="absolute w-2.5 h-2.5 bg-white border-2 border-blue-500 rounded-sm"
                    style={{
                      left: h.includes("w") ? -5 : undefined,
                      right: h.includes("e") ? -5 : undefined,
                      top: h.includes("n") ? -5 : undefined,
                      bottom: h.includes("s") ? -5 : undefined,
                      cursor: h === "nw" || h === "se" ? "nwse-resize" : "nesw-resize",
                    }}
                  />
                ))}
                <button
                  aria-label="Remove item"
                  onPointerDown={(e) => e.stopPropagation()}
                  onClick={() => removeItem(it.id)}
                  className="absolute -top-6 right-0 p-0.5 rounded bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 shadow"
                >
                  <X className="w-3 h-3 text-red-600" />
                </button>
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}
