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
  getPdfFileUrl,
  type TextBlock,
} from "@/lib/api";
import {
  loadPdfDocument,
  renderPage,
  preRenderAdjacentPages,
  getCachedTextBlocks,
  setCachedTextBlocks,
  invalidateTextBlockCache,
  getCurrentPdfDoc,
} from "@/lib/pdf-renderer";
import { Loader2, Monitor, FileImage } from "lucide-react";

const RENDER_DPI = 150;
const PDF_SCALE = RENDER_DPI / 72;
const PDFJS_SCALE = 2; // Scale factor for pdf.js rendering (higher = sharper)

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
    document: docInfo, renderMode, pdfVersion,
    addOptimisticEdit, resolveOptimisticEdit, revertOptimisticEdit,
    optimisticEdits, addToast,
    regionSelection, setRegionSelection, setChatOpen,
  } = useEditorStore();

  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const pdfCanvasRef = useRef<HTMLCanvasElement>(null);
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
  const [dragRect, setDragRect] = useState<number[] | null>(null);
  const [originalRect, setOriginalRect] = useState<number[] | null>(null);

  // Region select
  const [regionStart, setRegionStart] = useState<{ x: number; y: number } | null>(null);
  const [regionDragRect, setRegionDragRect] = useState<{ x: number; y: number; w: number; h: number } | null>(null);

  // Touch
  const [pinchStart, setPinchStart] = useState<number | null>(null);
  const [swipeStartX, setSwipeStartX] = useState<number | null>(null);

  // Optimistic text edits shown as overlays
  const [optimisticTextOverlays, setOptimisticTextOverlays] = useState<
    { id: string; bbox: number[]; text: string; fontSize: number }[]
  >([]);

  // ─── Fetch text blocks with cache ───────────────────────────────────
  const fetchTextBlocksCached = useCallback(
    async (page: number): Promise<TextBlock[]> => {
      if (!docId) return [];
      const cached = getCachedTextBlocks(docId, page, pageVersion);
      if (cached) return cached;
      const result = await getTextBlocks(docId, page);
      if (result.length > 0) {
        setCachedTextBlocks(docId, page, pageVersion, result[0].blocks);
        return result[0].blocks;
      }
      return [];
    },
    [docId, pageVersion],
  );

  // ─── PDF.js Rendering ───────────────────────────────────────────────
  useEffect(() => {
    if (!docId || renderMode !== "pdfjs") return;
    let cancelled = false;

    const renderWithPdfJs = async () => {
      setLoading(true);
      try {
        const pdfUrl = getPdfFileUrl(docId);
        const doc = await loadPdfDocument(pdfUrl, docId, pdfVersion);
        if (cancelled) return;

        const canvas = pdfCanvasRef.current;
        if (!canvas) return;

        const { width, height } = await renderPage(doc, currentPage, PDFJS_SCALE, canvas);
        if (cancelled) return;

        setImgSize({ w: width, h: height });
        setLoading(false);

        // Pre-render adjacent pages in background
        preRenderAdjacentPages(doc, currentPage, PDFJS_SCALE, docId, pdfVersion);
      } catch (err) {
        if (!cancelled) {
          console.error("PDF.js render failed:", err);
          setLoading(false);
        }
      }
    };

    renderWithPdfJs();
    setAllPaths([]);
    setEditingBlock(null);
    setAddingText(null);
    setHighlightRect(null);
    setShowTextBlocks(false);
    setSelectedBlock(null);
    setDragRect(null);
    setRegionStart(null);
    setRegionDragRect(null);

    return () => { cancelled = true; };
  }, [docId, currentPage, pdfVersion, renderMode]);

  // ─── Image-based Rendering (existing) ───────────────────────────────
  useEffect(() => {
    if (!docId || renderMode !== "image") return;
    setLoading(true);
    setImgSrc(`${getPageUrl(docId, currentPage, RENDER_DPI)}&v=${pageVersion}`);
    setAllPaths([]);
    setEditingBlock(null);
    setAddingText(null);
    setHighlightRect(null);
    setShowTextBlocks(false);
    setSelectedBlock(null);
    setDragRect(null);
    setRegionStart(null);
    setRegionDragRect(null);
  }, [docId, currentPage, pageVersion, renderMode]);

  // ─── Load text blocks for text tool (with cache) ────────────────────
  useEffect(() => {
    if (!docId || activeTool !== "text") { setShowTextBlocks(false); return; }
    setShowTextBlocks(true);
    fetchTextBlocksCached(currentPage).then(setTextBlocks);
  }, [docId, currentPage, activeTool, pageVersion, fetchTextBlocksCached]);

  // ─── Load content blocks for select tool (with cache) ───────────────
  useEffect(() => {
    if (!docId || activeTool !== "select") { setContentBlocks([]); setSelectedBlock(null); return; }
    fetchTextBlocksCached(currentPage).then((spans) => {
      const blocks: ContentBlock[] = [];
      const grouped = new Set<number>();

      for (let i = 0; i < spans.length; i++) {
        if (grouped.has(i)) continue;
        const group = [spans[i]];
        grouped.add(i);
        const [x0, y0, x1] = spans[i].bbox;

        for (let j = i + 1; j < spans.length; j++) {
          if (grouped.has(j)) continue;
          const [bx0, by0] = spans[j].bbox;
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
          [Infinity, Infinity, -Infinity, -Infinity],
        );

        const scale = renderMode === "pdfjs" ? PDFJS_SCALE : PDF_SCALE;
        blocks.push({
          id: `block-${i}`,
          text: group.map((s) => s.text).join(" "),
          bbox: allBbox,
          screenBbox: allBbox.map((v) => v * scale),
          font: group[0].font,
          size: group[0].size,
          color: group[0].color,
        });
      }
      setContentBlocks(blocks);
    });
  }, [docId, currentPage, activeTool, pageVersion, fetchTextBlocksCached, renderMode]);

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
  }, [imgSize, allPaths, drawPoints, highlightRect, regionDragRect]);

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

  // ─── Current scale factor based on render mode ──────────────────────
  const currentScale = renderMode === "pdfjs" ? PDFJS_SCALE : PDF_SCALE;

  // ─── Mouse Handlers (draw/highlight/eraser) ──────────────────────────

  const handleCanvasMouseDown = (e: React.MouseEvent) => {
    const pos = getCanvasPos(e);
    if (activeTool === "draw") { setIsDrawing(true); setDrawPoints([[pos.x, pos.y]]); }
    else if (activeTool === "highlight") { setHighlightStart(pos); setHighlightRect({ x: pos.x, y: pos.y, w: 0, h: 0 }); }
    else if (activeTool === "region_select") { setRegionStart(pos); setRegionDragRect({ x: pos.x, y: pos.y, w: 0, h: 0 }); setRegionSelection(null); }
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
    } else if (activeTool === "region_select" && regionStart) {
      setRegionDragRect({
        x: Math.min(regionStart.x, pos.x), y: Math.min(regionStart.y, pos.y),
        w: Math.abs(pos.x - regionStart.x), h: Math.abs(pos.y - regionStart.y),
      });
    }
  };

  const handleCanvasMouseUp = async () => {
    if (activeTool === "draw" && isDrawing && drawPoints.length >= 2) {
      const newPath = { points: [...drawPoints], color: drawColor, width: drawWidth };
      setAllPaths((prev) => [...prev, newPath]);
      setDrawPoints([]); setIsDrawing(false);
      if (docId) {
        const pdfPoints = newPath.points.map((p) => [p[0] / currentScale, p[1] / currentScale]);
        const hexToRgb = (hex: string) => [parseInt(hex.slice(1, 3), 16) / 255, parseInt(hex.slice(3, 5), 16) / 255, parseInt(hex.slice(5, 7), 16) / 255];
        await addDrawing(docId, currentPage, [{ points: pdfPoints, color: hexToRgb(newPath.color), width: newPath.width / currentScale }]);
        invalidateTextBlockCache(docId, currentPage);
        bumpVersion();
      }
    } else if (activeTool === "highlight" && highlightRect && highlightRect.w > 5 && highlightRect.h > 5) {
      if (docId) {
        const pdfRect = [highlightRect.x / currentScale, highlightRect.y / currentScale, (highlightRect.x + highlightRect.w) / currentScale, (highlightRect.y + highlightRect.h) / currentScale];
        const hexToRgb = (hex: string) => [parseInt(hex.slice(1, 3), 16) / 255, parseInt(hex.slice(3, 5), 16) / 255, parseInt(hex.slice(5, 7), 16) / 255];
        await addHighlight(docId, currentPage, [pdfRect], hexToRgb(highlightColor));
        bumpVersion();
      }
      setHighlightRect(null); setHighlightStart(null);
    } else if (activeTool === "region_select" && regionDragRect && regionDragRect.w > 5 && regionDragRect.h > 5) {
      // Convert screen coords to PDF coords
      const pdfRect = {
        x: regionDragRect.x / currentScale,
        y: regionDragRect.y / currentScale,
        width: regionDragRect.w / currentScale,
        height: regionDragRect.h / currentScale,
      };
      setRegionSelection({
        page: currentPage,
        rect: pdfRect,
        screenRect: { x: regionDragRect.x, y: regionDragRect.y, width: regionDragRect.w, height: regionDragRect.h },
      });
      setRegionDragRect(null);
      setRegionStart(null);
      // Auto-open chat panel
      setChatOpen(true);
    } else {
      setIsDrawing(false); setDrawPoints([]); setHighlightStart(null); setHighlightRect(null);
      setRegionStart(null); setRegionDragRect(null);
    }
  };

  // ─── Select / Move / Resize Handlers ─────────────────────────────────

  const getHandleAtPos = (x: number, y: number, rect: number[]): DragMode => {
    const [x0, y0, x1, y1] = rect;
    const hs = 8;
    if (Math.abs(x - x0) < hs && Math.abs(y - y0) < hs) return "nw";
    if (Math.abs(x - x1) < hs && Math.abs(y - y0) < hs) return "ne";
    if (Math.abs(x - x0) < hs && Math.abs(y - y1) < hs) return "sw";
    if (Math.abs(x - x1) < hs && Math.abs(y - y1) < hs) return "se";
    if (Math.abs(y - y0) < hs && x > x0 && x < x1) return "n";
    if (Math.abs(y - y1) < hs && x > x0 && x < x1) return "s";
    if (Math.abs(x - x0) < hs && y > y0 && y < y1) return "w";
    if (Math.abs(x - x1) < hs && y > y0 && y < y1) return "e";
    if (x >= x0 && x <= x1 && y >= y0 && y <= y1) return "move";
    return null;
  };

  const getWrapperPos = (e: React.MouseEvent | MouseEvent) => {
    const wrapper = renderMode === "pdfjs" ? pdfCanvasRef.current?.parentElement : imgRef.current?.parentElement;
    if (!wrapper) return { x: 0, y: 0 };
    const wrapperRect = wrapper.getBoundingClientRect();
    return { x: (e.clientX - wrapperRect.left) / zoom, y: (e.clientY - wrapperRect.top) / zoom };
  };

  const handleSelectMouseDown = (e: React.MouseEvent, block: ContentBlock) => {
    e.stopPropagation();
    e.preventDefault();
    setSelectedBlock(block);
    const r = dragRect && selectedBlock?.id === block.id ? dragRect : block.screenBbox;
    setDragRect([...r]);
    setOriginalRect([...block.screenBbox]);

    const pos = getWrapperPos(e);
    const mode = getHandleAtPos(pos.x, pos.y, r);
    setDragMode(mode);
    setDragStart(pos);
  };

  const handleSelectGlobalMouseDown = (e: React.MouseEvent) => {
    if (activeTool === "select" && selectedBlock) {
      const pos = getWrapperPos(e);
      const r = dragRect || selectedBlock.screenBbox;
      if (pos.x < r[0] || pos.x > r[2] || pos.y < r[1] || pos.y > r[3]) {
        commitMoveResize();
      }
    }
    // Clear finalized region selection when clicking outside of it (except when dragging a new one)
    if (activeTool !== "region_select" && regionSelection) {
      setRegionSelection(null);
    }
  };

  useEffect(() => {
    if (!dragMode || !dragStart || !dragRect) return;

    const handleMouseMove = (e: MouseEvent) => {
      const pos = getWrapperPos(e);
      const dx = pos.x - dragStart.x;
      const dy = pos.y - dragStart.y;
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
          r[0] = orig[0]; r[1] = orig[1]; r[2] = orig[2]; r[3] = orig[3];
          if (dragMode.includes("n")) r[1] = orig[1] + dy;
          if (dragMode.includes("s")) r[3] = orig[3] + dy;
          if (dragMode.includes("w")) r[0] = orig[0] + dx;
          if (dragMode.includes("e")) r[2] = orig[2] + dx;
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
  }, [dragMode, dragStart, dragRect, originalRect, zoom, renderMode]);

  const commitMoveResize = async () => {
    if (!docId || !selectedBlock || !dragRect) {
      setSelectedBlock(null); setDragRect(null);
      return;
    }
    const orig = selectedBlock.screenBbox;
    const moved = dragRect;
    const hasMoved = orig.some((v, i) => Math.abs(v - moved[i]) > 2);
    if (!hasMoved) {
      setSelectedBlock(null); setDragRect(null);
      return;
    }
    await moveResizeContent(docId, {
      page: currentPage,
      old_bbox: selectedBlock.bbox,
      new_bbox: moved.map((v) => v / currentScale),
    });
    setSelectedBlock(null);
    setDragRect(null);
    invalidateTextBlockCache(docId, currentPage);
    bumpVersion();
  };

  // ─── Text Edit (with optimistic updates) ────────────────────────────

  const commitTextEdit = async () => {
    if (!docId || !editingBlock) return;
    if (editText_ === editingBlock.text) { setEditingBlock(null); return; }

    const editId = `opt-${Date.now()}`;
    const bbox = editingBlock.bbox;

    // Optimistic: show the new text immediately as an overlay
    addOptimisticEdit({
      id: editId,
      page: currentPage,
      type: "text-edit",
      preview: { bbox, text: editText_, fontSize: editingBlock.size },
      pending: true,
    });
    setOptimisticTextOverlays((prev) => [
      ...prev,
      { id: editId, bbox, text: editText_, fontSize: editingBlock.size },
    ]);
    setEditingBlock(null);

    try {
      await editText(docId, { page: currentPage, bbox, new_text: editText_, font_size: editingBlock.size });
      resolveOptimisticEdit(editId);
      invalidateTextBlockCache(docId, currentPage);
      bumpVersion();
    } catch {
      revertOptimisticEdit(editId);
      addToast("Text edit failed — reverted", "error");
    }
    // Remove overlay after version bump triggers re-render
    setOptimisticTextOverlays((prev) => prev.filter((o) => o.id !== editId));
  };

  const commitNewText = async () => {
    if (!docId || !addingText || !newText.trim()) { setAddingText(null); return; }

    const editId = `opt-${Date.now()}`;
    const pdfX = addingText.x / currentScale;
    const pdfY = addingText.y / currentScale;

    // Optimistic: show the text immediately
    addOptimisticEdit({
      id: editId,
      page: currentPage,
      type: "text-add",
      preview: { x: addingText.x, y: addingText.y, text: newText, fontSize },
      pending: true,
    });
    setOptimisticTextOverlays((prev) => [
      ...prev,
      { id: editId, bbox: [pdfX, pdfY, pdfX + 100, pdfY + fontSize], text: newText, fontSize },
    ]);
    setAddingText(null);
    const savedText = newText;
    setNewText("");

    try {
      await addText(docId, { page: currentPage, x: pdfX, y: pdfY, text: savedText, font_size: fontSize });
      resolveOptimisticEdit(editId);
      invalidateTextBlockCache(docId, currentPage);
      bumpVersion();
    } catch {
      revertOptimisticEdit(editId);
      addToast("Failed to add text — reverted", "error");
    }
    setOptimisticTextOverlays((prev) => prev.filter((o) => o.id !== editId));
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
      case "draw": case "highlight": case "region_select": return "crosshair";
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
      {/* Render mode toggle */}
      <div className="fixed bottom-4 left-4 z-20 flex gap-1 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-700 p-1">
        <button
          onClick={() => useEditorStore.getState().setRenderMode("image")}
          className={`p-1.5 rounded-md text-xs flex items-center gap-1 transition-colors ${
            renderMode === "image"
              ? "bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300"
              : "text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700"
          }`}
          title="Image rendering (server-side)"
        >
          <FileImage className="w-3.5 h-3.5" />
          <span className="hidden sm:inline">Image</span>
        </button>
        <button
          onClick={() => useEditorStore.getState().setRenderMode("pdfjs")}
          className={`p-1.5 rounded-md text-xs flex items-center gap-1 transition-colors ${
            renderMode === "pdfjs"
              ? "bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300"
              : "text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700"
          }`}
          title="PDF.js rendering (client-side)"
        >
          <Monitor className="w-3.5 h-3.5" />
          <span className="hidden sm:inline">PDF.js</span>
        </button>
      </div>

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

        {/* Image-based rendering */}
        {renderMode === "image" && imgSrc && (
          <img ref={imgRef} src={imgSrc} alt={`Page ${currentPage + 1}`} className="max-w-none select-none block"
            onLoad={handleImageLoad} draggable={false} style={{ display: loading ? "none" : "block" }} />
        )}

        {/* PDF.js canvas rendering */}
        {renderMode === "pdfjs" && (
          <canvas
            ref={pdfCanvasRef}
            className="max-w-none select-none block"
            style={{ display: loading ? "none" : "block" }}
          />
        )}

        {/* Drawing/Highlight Canvas */}
        {!loading && imgSize.w > 0 && (
          <canvas ref={canvasRef} className="absolute top-0 left-0 w-full h-full"
            style={{ pointerEvents: (activeTool === "draw" || activeTool === "highlight" || activeTool === "eraser" || activeTool === "region_select") ? "auto" : "none" }}
            onMouseDown={handleCanvasMouseDown} onMouseMove={handleCanvasMouseMove}
            onMouseUp={handleCanvasMouseUp} onMouseLeave={handleCanvasMouseUp} />
        )}

        {/* Optimistic text overlays */}
        {optimisticTextOverlays
          .filter((o) => optimisticEdits.some((e) => e.id === o.id))
          .map((overlay) => {
            const [x0, y0, x1, y1] = overlay.bbox.map((v) => v * currentScale);
            return (
              <div
                key={overlay.id}
                className="absolute bg-yellow-100/80 dark:bg-yellow-900/40 border border-yellow-400 px-1 pointer-events-none"
                style={{
                  left: x0, top: y0,
                  minWidth: x1 - x0, minHeight: y1 - y0,
                  fontSize: Math.max(10, overlay.fontSize * 0.8),
                  lineHeight: 1.2,
                }}
              >
                {overlay.text}
              </div>
            );
          })}

        {/* Region select: dragging preview */}
        {activeTool === "region_select" && regionDragRect && regionDragRect.w > 0 && (
          <div
            className="absolute pointer-events-none"
            style={{
              left: regionDragRect.x,
              top: regionDragRect.y,
              width: regionDragRect.w,
              height: regionDragRect.h,
              border: "2px dashed rgb(59, 130, 246)",
              backgroundColor: "rgba(59, 130, 246, 0.1)",
              zIndex: 25,
            }}
          />
        )}

        {/* Region select: finalized selection */}
        {regionSelection && regionSelection.page === currentPage && (
          <div
            className="absolute pointer-events-none"
            style={{
              left: regionSelection.screenRect.x,
              top: regionSelection.screenRect.y,
              width: regionSelection.screenRect.width,
              height: regionSelection.screenRect.height,
              border: "2px dashed rgb(59, 130, 246)",
              backgroundColor: "rgba(59, 130, 246, 0.08)",
              zIndex: 25,
            }}
          />
        )}

        {/* Select mode: content block overlays */}
        {activeTool === "select" && !loading && contentBlocks.map((block) => {
          const isSelected = selectedBlock?.id === block.id;
          const r = isSelected && dragRect ? dragRect : block.screenBbox;
          return (
            <div key={block.id}>
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
                          const pos = getWrapperPos(e);
                          setDragStart(pos);
                          setOriginalRect(dragRect ? [...dragRect] : [...block.screenBbox]);
                        }}
                      />
                    );
                  })}
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
          const [x0, y0, x1, y1] = block.bbox.map((v) => v * currentScale);
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
          <div className="absolute z-30" style={{ left: editingBlock.bbox[0] * currentScale, top: editingBlock.bbox[1] * currentScale - 4 }}>
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
