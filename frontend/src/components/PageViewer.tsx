"use client";

import { useRef, useState, useEffect, useCallback } from "react";
import { useEditorStore } from "@/lib/store";
import {
  getPageUrl,
  getTextBlocks,
  editText,
  addText,
  addHighlight,
  addDrawing,
  moveResizeContent,
  type TextBlock,
} from "@/lib/api";
import { Loader2 } from "lucide-react";

const RENDER_DPI = 150;
const PDF_SCALE = RENDER_DPI / 72;

interface ContentBlock {
  id: string;
  text: string;
  bbox: number[]; // PDF coords [x0, y0, x1, y1]
  screenBbox: number[]; // pixel coords
  font: string;
  size: number;
  color: number;
}

type DragMode = "move" | "nw" | "ne" | "sw" | "se" | "n" | "s" | "e" | "w" | null;

export default function PageViewer() {
  const {
    docId, currentPage, zoom, pageVersion, activeTool,
    drawColor, drawWidth, highlightColor, fontSize,
    setZoom, setCurrentPage, totalPages, bumpVersion,
    document: docInfo,
  } = useEditorStore();

  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const [loading, setLoading] = useState(true);
  const [imgSrc, setImgSrc] = useState("");
  const [imgSize, setImgSize] = useState({ w: 0, h: 0 });

  // Drawing
  const [isDrawing, setIsDrawing] = useState(false);
  const [drawPoints, setDrawPoints] = useState<number[][]>([]);
  const [allPaths, setAllPaths] = useState<{ points: number[][]; color: string; width: number }[]>([]);

  // Highlight
  const [highlightStart, setHighlightStart] = useState<{ x: number; y: number } | null>(null);
  const [highlightRect, setHighlightRect] = useState<{ x: number; y: number; w: number; h: number } | null>(null);

  // Text editing
  const [textBlocks, setTextBlocks] = useState<TextBlock[]>([]);
  const [editingBlock, setEditingBlock] = useState<TextBlock | null>(null);
  const [editText_, setEditText_] = useState("");
  const [showTextBlocks, setShowTextBlocks] = useState(false);
  const [addingText, setAddingText] = useState<{ x: number; y: number } | null>(null);
  const [newText, setNewText] = useState("");

  // Select / Move / Resize
  const [contentBlocks, setContentBlocks] = useState<ContentBlock[]>([]);
  const [selectedBlock, setSelectedBlock] = useState<ContentBlock | null>(null);
  const [dragMode, setDragMode] = useState<DragMode>(null);
  const [dragStart, setDragStart] = useState<{ x: number; y: number } | null>(null);
  const [dragRect, setDragRect] = useState<number[] | null>(null); // [x0, y0, x1, y1] in screen px
  const [originalRect, setOriginalRect] = useState<number[] | null>(null);

  // Touch
  const [pinchStart, setPinchStart] = useState<number | null>(null);
  const [swipeStartX, setSwipeStartX] = useState<number | null>(null);

  // Load page image
  useEffect(() => {
    if (!docId) return;
    setLoading(true);
    setImgSrc(`${getPageUrl(docId, currentPage, RENDER_DPI)}&v=${pageVersion}`);
    setAllPaths([]);
    setEditingBlock(null);
    setAddingText(null);
    setHighlightRect(null);
    setShowTextBlocks(false);
    setSelectedBlock(null);
    setDragRect(null);
  }, [docId, currentPage, pageVersion]);

  // Load text blocks for text tool
  useEffect(() => {
    if (!docId || activeTool !== "text") { setShowTextBlocks(false); return; }
    setShowTextBlocks(true);
    getTextBlocks(docId, currentPage).then((r) => { if (r.length > 0) setTextBlocks(r[0].blocks); });
  }, [docId, currentPage, activeTool, pageVersion]);

  // Load content blocks for select tool
  useEffect(() => {
    if (!docId || activeTool !== "select") { setContentBlocks([]); setSelectedBlock(null); return; }
    getTextBlocks(docId, currentPage).then((r) => {
      if (r.length === 0) return;
      // Group spans into logical blocks by proximity
      const spans = r[0].blocks;
      const blocks: ContentBlock[] = [];
      const grouped = new Set<number>();

      for (let i = 0; i < spans.length; i++) {
        if (grouped.has(i)) continue;
        const group = [spans[i]];
        grouped.add(i);
        const [x0, y0, x1, y1] = spans[i].bbox;

        // Group nearby spans on the same line
        for (let j = i + 1; j < spans.length; j++) {
          if (grouped.has(j)) continue;
          const [bx0, by0, bx1, by1] = spans[j].bbox;
          if (Math.abs(by0 - y0) < spans[i].size * 0.5 && bx0 - x1 < spans[i].size * 2) {
            group.push(spans[j]);
            grouped.add(j);
          }
        }

        const allBbox = group.reduce(
          (acc, s) => [
            Math.min(acc[0], s.bbox[0]), Math.min(acc[1], s.bbox[1]),
            Math.max(acc[2], s.bbox[2]), Math.max(acc[3], s.bbox[3]),
          ],
          [Infinity, Infinity, -Infinity, -Infinity]
        );

        blocks.push({
          id: `block-${i}`,
          text: group.map((s) => s.text).join(" "),
          bbox: allBbox,
          screenBbox: allBbox.map((v) => v * PDF_SCALE),
          font: group[0].font,
          size: group[0].size,
          color: group[0].color,
        });
      }
      setContentBlocks(blocks);
    });
  }, [docId, currentPage, activeTool, pageVersion]);

  const handleImageLoad = useCallback(() => {
    setLoading(false);
    if (imgRef.current) setImgSize({ w: imgRef.current.naturalWidth, h: imgRef.current.naturalHeight });
  }, []);

  // ─── Canvas Drawing ──────────────────────────────────────────────────

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || imgSize.w === 0) return;
    canvas.width = imgSize.w;
    canvas.height = imgSize.h;
    redrawCanvas();
  }, [imgSize, allPaths, drawPoints, highlightRect]);

  const redrawCanvas = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (const path of allPaths) {
      if (path.points.length < 2) continue;
      ctx.strokeStyle = path.color; ctx.lineWidth = path.width;
      ctx.lineCap = "round"; ctx.lineJoin = "round"; ctx.beginPath();
      ctx.moveTo(path.points[0][0], path.points[0][1]);
      for (let i = 1; i < path.points.length; i++) ctx.lineTo(path.points[i][0], path.points[i][1]);
      ctx.stroke();
    }
    if (drawPoints.length >= 2) {
      ctx.strokeStyle = drawColor; ctx.lineWidth = drawWidth;
      ctx.lineCap = "round"; ctx.lineJoin = "round"; ctx.beginPath();
      ctx.moveTo(drawPoints[0][0], drawPoints[0][1]);
      for (let i = 1; i < drawPoints.length; i++) ctx.lineTo(drawPoints[i][0], drawPoints[i][1]);
      ctx.stroke();
    }
    if (highlightRect) {
      ctx.fillStyle = highlightColor + "59";
      ctx.fillRect(highlightRect.x, highlightRect.y, highlightRect.w, highlightRect.h);
      ctx.strokeStyle = highlightColor; ctx.lineWidth = 1;
      ctx.strokeRect(highlightRect.x, highlightRect.y, highlightRect.w, highlightRect.h);
    }
  }, [allPaths, drawPoints, highlightRect, drawColor, drawWidth, highlightColor]);

  const getCanvasPos = (e: React.MouseEvent) => {
    const canvas = canvasRef.current;
    if (!canvas) return { x: 0, y: 0 };
    const rect = canvas.getBoundingClientRect();
    return { x: (e.clientX - rect.left) * (canvas.width / rect.width), y: (e.clientY - rect.top) * (canvas.height / rect.height) };
  };

  const getTouchCanvasPos = (touch: React.Touch) => {
    const canvas = canvasRef.current;
    if (!canvas) return { x: 0, y: 0 };
    const rect = canvas.getBoundingClientRect();
    return { x: (touch.clientX - rect.left) * (canvas.width / rect.width), y: (touch.clientY - rect.top) * (canvas.height / rect.height) };
  };

  // ─── Mouse Handlers (draw/highlight/eraser) ──────────────────────────

  const handleCanvasMouseDown = (e: React.MouseEvent) => {
    const pos = getCanvasPos(e);
    if (activeTool === "draw") { setIsDrawing(true); setDrawPoints([[pos.x, pos.y]]); }
    else if (activeTool === "highlight") { setHighlightStart(pos); setHighlightRect({ x: pos.x, y: pos.y, w: 0, h: 0 }); }
    else if (activeTool === "eraser") {
      const threshold = 20;
      const idx = allPaths.findLastIndex((path) => path.points.some((p) => Math.abs(p[0] - pos.x) < threshold && Math.abs(p[1] - pos.y) < threshold));
      if (idx >= 0) setAllPaths((prev) => prev.filter((_, i) => i !== idx));
    }
  };

  const handleCanvasMouseMove = (e: React.MouseEvent) => {
    const pos = getCanvasPos(e);
    if (activeTool === "draw" && isDrawing) setDrawPoints((prev) => [...prev, [pos.x, pos.y]]);
    else if (activeTool === "highlight" && highlightStart) {
      setHighlightRect({
        x: Math.min(highlightStart.x, pos.x), y: Math.min(highlightStart.y, pos.y),
        w: Math.abs(pos.x - highlightStart.x), h: Math.abs(pos.y - highlightStart.y),
      });
    }
  };

  const handleCanvasMouseUp = async () => {
    if (activeTool === "draw" && isDrawing && drawPoints.length >= 2) {
      const newPath = { points: [...drawPoints], color: drawColor, width: drawWidth };
      setAllPaths((prev) => [...prev, newPath]);
      setDrawPoints([]); setIsDrawing(false);
      if (docId) {
        const pdfPoints = newPath.points.map((p) => [p[0] / PDF_SCALE, p[1] / PDF_SCALE]);
        const hexToRgb = (hex: string) => [parseInt(hex.slice(1, 3), 16) / 255, parseInt(hex.slice(3, 5), 16) / 255, parseInt(hex.slice(5, 7), 16) / 255];
        await addDrawing(docId, currentPage, [{ points: pdfPoints, color: hexToRgb(newPath.color), width: newPath.width / PDF_SCALE }]);
        bumpVersion();
      }
    } else if (activeTool === "highlight" && highlightRect && highlightRect.w > 5 && highlightRect.h > 5) {
      if (docId) {
        const pdfRect = [highlightRect.x / PDF_SCALE, highlightRect.y / PDF_SCALE, (highlightRect.x + highlightRect.w) / PDF_SCALE, (highlightRect.y + highlightRect.h) / PDF_SCALE];
        const hexToRgb = (hex: string) => [parseInt(hex.slice(1, 3), 16) / 255, parseInt(hex.slice(3, 5), 16) / 255, parseInt(hex.slice(5, 7), 16) / 255];
        await addHighlight(docId, currentPage, [pdfRect], hexToRgb(highlightColor));
        bumpVersion();
      }
      setHighlightRect(null); setHighlightStart(null);
    } else {
      setIsDrawing(false); setDrawPoints([]); setHighlightStart(null); setHighlightRect(null);
    }
  };

  // ─── Select / Move / Resize Handlers ─────────────────────────────────

  const getHandleAtPos = (x: number, y: number, rect: number[]): DragMode => {
    const [x0, y0, x1, y1] = rect;
    const hs = 8; // handle size in screen px
    // Corners
    if (Math.abs(x - x0) < hs && Math.abs(y - y0) < hs) return "nw";
    if (Math.abs(x - x1) < hs && Math.abs(y - y0) < hs) return "ne";
    if (Math.abs(x - x0) < hs && Math.abs(y - y1) < hs) return "sw";
    if (Math.abs(x - x1) < hs && Math.abs(y - y1) < hs) return "se";
    // Edges
    if (Math.abs(y - y0) < hs && x > x0 && x < x1) return "n";
    if (Math.abs(y - y1) < hs && x > x0 && x < x1) return "s";
    if (Math.abs(x - x0) < hs && y > y0 && y < y1) return "w";
    if (Math.abs(x - x1) < hs && y > y0 && y < y1) return "e";
    // Inside = move
    if (x >= x0 && x <= x1 && y >= y0 && y <= y1) return "move";
    return null;
  };

  const handleSelectMouseDown = (e: React.MouseEvent, block: ContentBlock) => {
    e.stopPropagation();
    e.preventDefault();
    setSelectedBlock(block);
    const r = dragRect && selectedBlock?.id === block.id ? dragRect : block.screenBbox;
    setDragRect([...r]);
    setOriginalRect([...block.screenBbox]);

    const wrapper = imgRef.current?.parentElement;
    if (!wrapper) return;
    const wrapperRect = wrapper.getBoundingClientRect();
    const x = (e.clientX - wrapperRect.left) / zoom;
    const y = (e.clientY - wrapperRect.top) / zoom;

    const mode = getHandleAtPos(x, y, r);
    setDragMode(mode);
    setDragStart({ x, y });
  };

  const handleSelectGlobalMouseDown = (e: React.MouseEvent) => {
    // Click on empty area = deselect
    if (activeTool === "select" && selectedBlock) {
      const wrapper = imgRef.current?.parentElement;
      if (!wrapper) return;
      const wrapperRect = wrapper.getBoundingClientRect();
      const x = (e.clientX - wrapperRect.left) / zoom;
      const y = (e.clientY - wrapperRect.top) / zoom;
      // Check if click is on the selected block
      const r = dragRect || selectedBlock.screenBbox;
      if (x < r[0] || x > r[2] || y < r[1] || y > r[3]) {
        commitMoveResize();
      }
    }
  };

  useEffect(() => {
    if (!dragMode || !dragStart || !dragRect) return;

    const handleMouseMove = (e: MouseEvent) => {
      const wrapper = imgRef.current?.parentElement;
      if (!wrapper) return;
      const wrapperRect = wrapper.getBoundingClientRect();
      const x = (e.clientX - wrapperRect.left) / zoom;
      const y = (e.clientY - wrapperRect.top) / zoom;
      const dx = x - dragStart.x;
      const dy = y - dragStart.y;
      const orig = originalRect || dragRect;

      setDragRect((prev) => {
        if (!prev) return prev;
        const r = [...prev];
        if (dragMode === "move") {
          const w = orig[2] - orig[0];
          const h = orig[3] - orig[1];
          r[0] = orig[0] + dx; r[1] = orig[1] + dy;
          r[2] = r[0] + w; r[3] = r[1] + h;
        } else {
          // Reset to original + delta for resize
          r[0] = orig[0]; r[1] = orig[1]; r[2] = orig[2]; r[3] = orig[3];
          if (dragMode.includes("n")) r[1] = orig[1] + dy;
          if (dragMode.includes("s")) r[3] = orig[3] + dy;
          if (dragMode.includes("w")) r[0] = orig[0] + dx;
          if (dragMode.includes("e")) r[2] = orig[2] + dx;
          // Enforce minimum size
          if (r[2] - r[0] < 20) r[2] = r[0] + 20;
          if (r[3] - r[1] < 10) r[3] = r[1] + 10;
        }
        return r;
      });
    };

    const handleMouseUp = () => {
      setDragMode(null);
      setDragStart(null);
      setOriginalRect(dragRect ? [...dragRect] : null);
    };

    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, [dragMode, dragStart, dragRect, originalRect, zoom]);

  const commitMoveResize = async () => {
    if (!docId || !selectedBlock || !dragRect) {
      setSelectedBlock(null); setDragRect(null);
      return;
    }
    const orig = selectedBlock.screenBbox;
    const moved = dragRect;
    // Check if actually moved/resized
    const hasMoved = orig.some((v, i) => Math.abs(v - moved[i]) > 2);
    if (!hasMoved) {
      setSelectedBlock(null); setDragRect(null);
      return;
    }
    await moveResizeContent(docId, {
      page: currentPage,
      old_bbox: selectedBlock.bbox,
      new_bbox: moved.map((v) => v / PDF_SCALE),
    });
    setSelectedBlock(null);
    setDragRect(null);
    bumpVersion();
  };

  // ─── Text Edit ───────────────────────────────────────────────────────

  const commitTextEdit = async () => {
    if (!docId || !editingBlock) return;
    if (editText_ === editingBlock.text) { setEditingBlock(null); return; }
    await editText(docId, { page: currentPage, bbox: editingBlock.bbox, new_text: editText_, font_size: editingBlock.size });
    setEditingBlock(null); bumpVersion();
  };

  const commitNewText = async () => {
    if (!docId || !addingText || !newText.trim()) { setAddingText(null); return; }
    await addText(docId, { page: currentPage, x: addingText.x / PDF_SCALE, y: addingText.y / PDF_SCALE, text: newText, font_size: fontSize });
    setAddingText(null); setNewText(""); bumpVersion();
  };

  // ─── Touch ───────────────────────────────────────────────────────────

  const handleTouchStart = (e: React.TouchEvent) => {
    if (e.touches.length === 2) {
      setPinchStart(Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY));
    } else if (e.touches.length === 1) {
      setSwipeStartX(e.touches[0].clientX);
      if (activeTool === "draw") { const pos = getTouchCanvasPos(e.touches[0]); setIsDrawing(true); setDrawPoints([[pos.x, pos.y]]); }
      else if (activeTool === "highlight") { const pos = getTouchCanvasPos(e.touches[0]); setHighlightStart(pos); setHighlightRect({ x: pos.x, y: pos.y, w: 0, h: 0 }); }
    }
  };

  const handleTouchMove = (e: React.TouchEvent) => {
    if (e.touches.length === 2 && pinchStart !== null) {
      const dist = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
      const scale = dist / pinchStart;
      if (Math.abs(scale - 1) > 0.05) { setZoom(zoom * scale); setPinchStart(dist); }
    } else if (e.touches.length === 1) {
      if (activeTool === "draw" && isDrawing) { const pos = getTouchCanvasPos(e.touches[0]); setDrawPoints((prev) => [...prev, [pos.x, pos.y]]); }
      else if (activeTool === "highlight" && highlightStart) {
        const pos = getTouchCanvasPos(e.touches[0]);
        setHighlightRect({ x: Math.min(highlightStart.x, pos.x), y: Math.min(highlightStart.y, pos.y), w: Math.abs(pos.x - highlightStart.x), h: Math.abs(pos.y - highlightStart.y) });
      }
    }
  };

  const handleTouchEnd = (e: React.TouchEvent) => {
    setPinchStart(null);
    if (activeTool === "select" && swipeStartX !== null && e.changedTouches.length === 1) {
      const diff = e.changedTouches[0].clientX - swipeStartX;
      if (Math.abs(diff) > 80) { diff > 0 && currentPage > 0 ? setCurrentPage(currentPage - 1) : diff < 0 && currentPage < totalPages - 1 && setCurrentPage(currentPage + 1); }
    }
    setSwipeStartX(null);
    handleCanvasMouseUp();
  };

  const getCursor = () => {
    if (dragMode === "move") return "grabbing";
    if (dragMode === "nw" || dragMode === "se") return "nwse-resize";
    if (dragMode === "ne" || dragMode === "sw") return "nesw-resize";
    if (dragMode === "n" || dragMode === "s") return "ns-resize";
    if (dragMode === "e" || dragMode === "w") return "ew-resize";
    switch (activeTool) {
      case "draw": case "highlight": return "crosshair";
      case "text": return "text";
      case "eraser": return "pointer";
      case "select": return "default";
      default: return "default";
    }
  };

  if (!docId) return null;

  const selRect = dragRect || selectedBlock?.screenBbox;

  return (
    <div
      ref={containerRef}
      className="flex-1 overflow-auto bg-gray-100 dark:bg-gray-950 flex items-start justify-center p-2 sm:p-4 lg:p-8"
      onTouchStart={handleTouchStart}
      onTouchMove={handleTouchMove}
      onTouchEnd={handleTouchEnd}
    >
      <div
        className="relative shadow-xl rounded-sm bg-white"
        style={{ transform: `scale(${zoom})`, transformOrigin: "top center", cursor: getCursor() }}
        onMouseDown={handleSelectGlobalMouseDown}
      >
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-white dark:bg-gray-900 z-10 min-w-[300px] min-h-[400px]">
            <Loader2 className="w-8 h-8 text-blue-500 animate-spin" />
          </div>
        )}

        {imgSrc && (
          <img ref={imgRef} src={imgSrc} alt={`Page ${currentPage + 1}`} className="max-w-none select-none block"
            onLoad={handleImageLoad} draggable={false} style={{ display: loading ? "none" : "block" }} />
        )}

        {/* Drawing/Highlight Canvas */}
        {!loading && imgSize.w > 0 && (
          <canvas ref={canvasRef} className="absolute top-0 left-0 w-full h-full"
            style={{ pointerEvents: (activeTool === "draw" || activeTool === "highlight" || activeTool === "eraser") ? "auto" : "none" }}
            onMouseDown={handleCanvasMouseDown} onMouseMove={handleCanvasMouseMove}
            onMouseUp={handleCanvasMouseUp} onMouseLeave={handleCanvasMouseUp} />
        )}

        {/* Select mode: content block overlays */}
        {activeTool === "select" && !loading && contentBlocks.map((block) => {
          const isSelected = selectedBlock?.id === block.id;
          const r = isSelected && dragRect ? dragRect : block.screenBbox;
          return (
            <div key={block.id}>
              {/* Block outline */}
              <div
                className={`absolute transition-shadow ${
                  isSelected
                    ? "border-2 border-blue-500 shadow-lg bg-blue-500/5"
                    : "border border-transparent hover:border-blue-400/50 hover:bg-blue-400/5"
                }`}
                style={{
                  left: r[0], top: r[1],
                  width: r[2] - r[0], height: r[3] - r[1],
                  cursor: isSelected ? "grab" : "pointer",
                }}
                onMouseDown={(e) => handleSelectMouseDown(e, block)}
              />

              {/* Resize handles (when selected) */}
              {isSelected && (
                <>
                  {(["nw", "ne", "sw", "se", "n", "s", "e", "w"] as const).map((handle) => {
                    const hx = handle.includes("w") ? r[0] : handle.includes("e") ? r[2] : (r[0] + r[2]) / 2;
                    const hy = handle.includes("n") ? r[1] : handle.includes("s") ? r[3] : (r[1] + r[3]) / 2;
                    const cursor = (handle === "nw" || handle === "se") ? "nwse-resize"
                      : (handle === "ne" || handle === "sw") ? "nesw-resize"
                      : (handle === "n" || handle === "s") ? "ns-resize" : "ew-resize";
                    return (
                      <div
                        key={handle}
                        className="absolute w-3 h-3 bg-white border-2 border-blue-500 rounded-sm"
                        style={{
                          left: hx - 6, top: hy - 6,
                          cursor,
                          zIndex: 20,
                        }}
                        onMouseDown={(e) => {
                          e.stopPropagation();
                          e.preventDefault();
                          setDragMode(handle);
                          const wrapper = imgRef.current?.parentElement;
                          if (!wrapper) return;
                          const wr = wrapper.getBoundingClientRect();
                          setDragStart({ x: (e.clientX - wr.left) / zoom, y: (e.clientY - wr.top) / zoom });
                          setOriginalRect(dragRect ? [...dragRect] : [...block.screenBbox]);
                        }}
                      />
                    );
                  })}
                  {/* Commit button */}
                  <div className="absolute flex gap-1" style={{ left: r[2] + 4, top: r[1] }}>
                    <button
                      onClick={(e) => { e.stopPropagation(); commitMoveResize(); }}
                      className="px-2 py-1 text-[10px] bg-blue-600 text-white rounded shadow hover:bg-blue-700"
                    >
                      Apply
                    </button>
                    <button
                      onClick={(e) => { e.stopPropagation(); setSelectedBlock(null); setDragRect(null); }}
                      className="px-2 py-1 text-[10px] bg-gray-200 dark:bg-gray-700 rounded shadow hover:bg-gray-300"
                    >
                      Cancel
                    </button>
                  </div>
                </>
              )}
            </div>
          );
        })}

        {/* Text block overlays (text tool) */}
        {showTextBlocks && !loading && textBlocks.map((block, i) => {
          const [x0, y0, x1, y1] = block.bbox.map((v) => v * PDF_SCALE);
          return (
            <div key={i} className="absolute border border-blue-400/50 hover:border-blue-500 hover:bg-blue-500/10 cursor-text transition-colors"
              style={{ left: x0, top: y0, width: x1 - x0, height: y1 - y0 }}
              onClick={(e) => { e.stopPropagation(); setEditingBlock(block); setEditText_(block.text); }} />
          );
        })}

        {/* Click-to-add text */}
        {activeTool === "text" && !loading && !editingBlock && (
          <div className="absolute top-0 left-0 w-full h-full" style={{ pointerEvents: showTextBlocks ? "none" : "auto" }}
            onClick={(e) => {
              const rect = (e.target as HTMLElement).getBoundingClientRect();
              setAddingText({ x: (e.clientX - rect.left) * (imgSize.w / rect.width), y: (e.clientY - rect.top) * (imgSize.h / rect.height) });
              setNewText("");
            }} />
        )}

        {/* Text editing popup */}
        {editingBlock && (
          <div className="absolute z-30" style={{ left: editingBlock.bbox[0] * PDF_SCALE, top: editingBlock.bbox[1] * PDF_SCALE - 4 }}>
            <div className="bg-white dark:bg-gray-800 border border-blue-500 rounded-lg shadow-xl p-2 min-w-[200px]">
              <textarea autoFocus value={editText_} onChange={(e) => setEditText_(e.target.value)}
                className="w-full min-h-[60px] p-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-900 dark:text-white resize-y"
                style={{ fontSize: Math.max(11, editingBlock.size * 0.8) }}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); commitTextEdit(); } if (e.key === "Escape") setEditingBlock(null); }} />
              <div className="flex gap-1 mt-1">
                <button onClick={commitTextEdit} className="px-2 py-1 text-xs bg-blue-600 text-white rounded hover:bg-blue-700">Save</button>
                <button onClick={() => setEditingBlock(null)} className="px-2 py-1 text-xs bg-gray-200 dark:bg-gray-700 rounded">Cancel</button>
              </div>
            </div>
          </div>
        )}

        {/* New text input */}
        {addingText && (
          <div className="absolute z-30" style={{ left: addingText.x, top: addingText.y }}>
            <div className="bg-white dark:bg-gray-800 border border-green-500 rounded-lg shadow-xl p-2 min-w-[200px]">
              <input autoFocus value={newText} onChange={(e) => setNewText(e.target.value)} placeholder="Type new text..."
                className="w-full p-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-900 dark:text-white"
                onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commitNewText(); } if (e.key === "Escape") setAddingText(null); }} />
              <div className="flex gap-1 mt-1">
                <button onClick={commitNewText} className="px-2 py-1 text-xs bg-green-600 text-white rounded hover:bg-green-700">Add</button>
                <button onClick={() => setAddingText(null)} className="px-2 py-1 text-xs bg-gray-200 dark:bg-gray-700 rounded">Cancel</button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
