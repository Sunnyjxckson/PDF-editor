"use client";

import { useEditorStore } from "@/lib/store";
import {
  getThumbnailUrl,
  reorderPages,
  rotatePage,
  deletePage,
  getDocumentInfo,
} from "@/lib/api";
import {
  getCachedThumbnail,
  setCachedThumbnail,
} from "@/lib/pdf-renderer";
import { X, Plus, RotateCw, Trash2, FilePlus } from "lucide-react";
import { useState, useRef, useEffect, useCallback } from "react";

interface ContextMenuState {
  open: boolean;
  pageIndex: number;
  x: number;
  y: number;
}

const THUMB_HEIGHT = 200; // Estimated height per thumbnail item in pixels
const BUFFER_COUNT = 3; // Extra items to render above/below viewport

export default function PageSidebar() {
  const {
    docId,
    totalPages,
    currentPage,
    setCurrentPage,
    sidebarOpen,
    setSidebarOpen,
    pageVersion,
    bumpVersion,
    setDocument,
    addToast,
    document: docInfo,
  } = useEditorStore();

  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [dragOverIndex, setDragOverIndex] = useState<number | null>(null);
  const [contextMenu, setContextMenu] = useState<ContextMenuState>({
    open: false,
    pageIndex: 0,
    x: 0,
    y: 0,
  });

  // Virtual scrolling state
  const [visibleRange, setVisibleRange] = useState({ start: 0, end: 20 });

  const sidebarRef = useRef<HTMLDivElement>(null);
  const thumbRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const observerRef = useRef<IntersectionObserver | null>(null);
  const sentinelRefs = useRef<Map<number, HTMLDivElement>>(new Map());

  // Smooth scroll to selected page thumbnail when currentPage changes
  useEffect(() => {
    const el = thumbRefs.current.get(currentPage);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, [currentPage]);

  // Virtual scrolling via scroll position calculation
  useEffect(() => {
    const sidebar = sidebarRef.current;
    if (!sidebar || totalPages <= 30) {
      // For small documents, render all
      setVisibleRange({ start: 0, end: totalPages });
      return;
    }

    const handleScroll = () => {
      const scrollTop = sidebar.scrollTop;
      const viewportHeight = sidebar.clientHeight;

      const start = Math.max(0, Math.floor(scrollTop / THUMB_HEIGHT) - BUFFER_COUNT);
      const visibleCount = Math.ceil(viewportHeight / THUMB_HEIGHT);
      const end = Math.min(totalPages, start + visibleCount + BUFFER_COUNT * 2);

      setVisibleRange((prev) => {
        if (prev.start === start && prev.end === end) return prev;
        return { start, end };
      });
    };

    handleScroll(); // Initial calculation
    sidebar.addEventListener("scroll", handleScroll, { passive: true });
    return () => sidebar.removeEventListener("scroll", handleScroll);
  }, [totalPages, sidebarOpen]);

  // Close context menu on outside click or scroll
  useEffect(() => {
    if (!contextMenu.open) return;
    const close = () => setContextMenu((prev) => ({ ...prev, open: false }));
    window.addEventListener("scroll", close, true);
    window.addEventListener("click", close);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("click", close);
    };
  }, [contextMenu.open]);

  const refreshDoc = useCallback(async () => {
    if (!docId) return;
    try {
      const info = await getDocumentInfo(docId);
      setDocument(info, docId);
    } catch {
      addToast("Failed to refresh document", "error");
    }
  }, [docId, setDocument, addToast]);

  // ── Drag and drop handlers ──────────────────────────────────────────────

  const handleDragStart = (e: React.DragEvent, index: number) => {
    setDragIndex(index);
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(index));
    if (e.currentTarget instanceof HTMLElement) {
      e.currentTarget.style.opacity = "0.5";
    }
  };

  const handleDragEnd = (e: React.DragEvent) => {
    if (e.currentTarget instanceof HTMLElement) {
      e.currentTarget.style.opacity = "1";
    }
    setDragIndex(null);
    setDragOverIndex(null);
  };

  const handleDragOver = (e: React.DragEvent, index: number) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    setDragOverIndex(index);
  };

  const handleDragLeave = () => {
    setDragOverIndex(null);
  };

  const handleDrop = async (e: React.DragEvent, targetIndex: number) => {
    e.preventDefault();
    setDragOverIndex(null);
    if (dragIndex === null || dragIndex === targetIndex || !docId) return;

    const order = Array.from({ length: totalPages }, (_, i) => i);
    const [moved] = order.splice(dragIndex, 1);
    order.splice(targetIndex, 0, moved);

    try {
      await reorderPages(docId, order);
      addToast(`Moved page ${dragIndex + 1} to position ${targetIndex + 1}`, "success");
      await refreshDoc();
      bumpVersion();
      if (currentPage === dragIndex) {
        setCurrentPage(targetIndex);
      } else if (currentPage > dragIndex && currentPage <= targetIndex) {
        setCurrentPage(currentPage - 1);
      } else if (currentPage < dragIndex && currentPage >= targetIndex) {
        setCurrentPage(currentPage + 1);
      }
    } catch {
      addToast("Failed to reorder pages", "error");
    }

    setDragIndex(null);
  };

  // ── Context menu handlers ───────────────────────────────────────────────

  const handleContextMenu = (e: React.MouseEvent, pageIndex: number) => {
    e.preventDefault();
    e.stopPropagation();
    const sidebarRect = sidebarRef.current?.getBoundingClientRect();
    const x = e.clientX - (sidebarRect?.left ?? 0);
    const y = e.clientY - (sidebarRect?.top ?? 0);
    setContextMenu({ open: true, pageIndex, x, y });
  };

  const handleRotate = async () => {
    if (!docId) return;
    const page = contextMenu.pageIndex;
    setContextMenu((prev) => ({ ...prev, open: false }));
    try {
      await rotatePage(docId, page, 90);
      addToast(`Rotated page ${page + 1} by 90 degrees`, "success");
      await refreshDoc();
      bumpVersion();
    } catch {
      addToast("Failed to rotate page", "error");
    }
  };

  const handleDelete = async () => {
    if (!docId || totalPages <= 1) return;
    const page = contextMenu.pageIndex;
    setContextMenu((prev) => ({ ...prev, open: false }));
    try {
      await deletePage(docId, page);
      addToast(`Deleted page ${page + 1}`, "success");
      await refreshDoc();
      bumpVersion();
      if (currentPage >= totalPages - 1) {
        setCurrentPage(Math.max(0, totalPages - 2));
      } else if (currentPage > page) {
        setCurrentPage(currentPage - 1);
      }
    } catch {
      addToast("Failed to delete page", "error");
    }
  };

  if (!docId || !sidebarOpen) return null;

  const useVirtualScrolling = totalPages > 30;

  return (
    <>
      {/* Mobile overlay backdrop */}
      <div
        className="fixed inset-0 bg-black/40 z-20 sm:hidden"
        onClick={() => setSidebarOpen(false)}
      />

      {/* Sidebar */}
      <div
        ref={sidebarRef}
        className={`
          fixed sm:relative z-30 sm:z-auto
          left-0 top-0 sm:top-auto h-full sm:h-auto
          w-[200px] sm:w-[160px] lg:w-[200px]
          bg-white dark:bg-gray-900
          border-r border-gray-200 dark:border-gray-800
          overflow-y-auto shrink-0
          pt-12 sm:pt-0
          animate-slide-in-left
        `}
      >
        {/* Mobile close button */}
        <button
          onClick={() => setSidebarOpen(false)}
          className="sm:hidden absolute top-2 right-2 p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800"
        >
          <X className="w-5 h-5" />
        </button>

        <div className="p-2 space-y-2" style={useVirtualScrolling ? { position: "relative", height: totalPages * THUMB_HEIGHT } : undefined}>
          {Array.from({ length: totalPages }, (_, i) => {
            // Virtual scrolling: skip items outside visible range
            if (useVirtualScrolling && (i < visibleRange.start || i >= visibleRange.end)) {
              return null;
            }

            return (
              <div
                key={`${i}-${pageVersion}`}
                ref={(el) => {
                  if (el) thumbRefs.current.set(i, el);
                  else thumbRefs.current.delete(i);
                }}
                draggable
                onDragStart={(e) => handleDragStart(e, i)}
                onDragEnd={handleDragEnd}
                onDragOver={(e) => handleDragOver(e, i)}
                onDragLeave={handleDragLeave}
                onDrop={(e) => handleDrop(e, i)}
                onContextMenu={(e) => handleContextMenu(e, i)}
                className={`
                  relative cursor-grab active:cursor-grabbing
                  ${dragOverIndex === i && dragIndex !== i
                    ? "border-t-2 border-blue-400"
                    : ""
                  }
                  ${dragIndex === i ? "opacity-50" : ""}
                `}
                style={useVirtualScrolling ? {
                  position: "absolute",
                  top: i * THUMB_HEIGHT,
                  left: 8,
                  right: 8,
                  height: THUMB_HEIGHT - 8,
                } : undefined}
              >
                <button
                  onClick={() => {
                    setCurrentPage(i);
                    if (window.innerWidth < 640) setSidebarOpen(false);
                  }}
                  className={`
                    w-full rounded-lg overflow-hidden border-2 transition-colors
                    ${
                      currentPage === i
                        ? "border-blue-500 ring-2 ring-blue-500/30"
                        : "border-transparent hover:border-gray-300 dark:hover:border-gray-600"
                    }
                  `}
                >
                  <div className="relative">
                    <CachedThumbnail docId={docId} pageIndex={i} pageVersion={pageVersion} />
                    {/* Prominent page number badge */}
                    <span
                      className={`
                        absolute bottom-1.5 left-1/2 -translate-x-1/2
                        text-xs font-semibold px-2 py-0.5 rounded-md
                        shadow-sm
                        ${
                          currentPage === i
                            ? "bg-blue-500 text-white"
                            : "bg-black/60 text-white"
                        }
                      `}
                    >
                      {i + 1}
                    </span>
                  </div>
                </button>
              </div>
            );
          })}

          {/* Add Page button */}
          <div className="pt-1" style={useVirtualScrolling ? { position: "absolute", top: totalPages * THUMB_HEIGHT, left: 8, right: 8 } : undefined}>
            <button
              disabled
              title="Coming soon"
              className="
                w-full flex items-center justify-center gap-1.5
                py-2 rounded-lg border-2 border-dashed
                border-gray-300 dark:border-gray-700
                text-gray-400 dark:text-gray-600
                cursor-not-allowed opacity-60
                text-sm font-medium
              "
            >
              <Plus className="w-4 h-4" />
              Add Page
            </button>
          </div>
        </div>

        {/* Right-click context menu */}
        {contextMenu.open && (
          <div
            className="
              absolute z-50
              bg-white dark:bg-gray-800
              border border-gray-200 dark:border-gray-700
              rounded-lg shadow-lg py-1 min-w-[180px]
              text-sm
            "
            style={{
              left: contextMenu.x,
              top: contextMenu.y,
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <button
              onClick={handleRotate}
              className="
                w-full flex items-center gap-2 px-3 py-2
                hover:bg-gray-100 dark:hover:bg-gray-700
                text-gray-700 dark:text-gray-200
                transition-colors
              "
            >
              <RotateCw className="w-4 h-4" />
              Rotate 90&deg;
            </button>
            <button
              onClick={handleDelete}
              disabled={totalPages <= 1}
              className={`
                w-full flex items-center gap-2 px-3 py-2
                transition-colors
                ${
                  totalPages <= 1
                    ? "text-gray-400 dark:text-gray-600 cursor-not-allowed"
                    : "text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20"
                }
              `}
            >
              <Trash2 className="w-4 h-4" />
              Delete Page
            </button>
            <div className="h-px bg-gray-200 dark:bg-gray-700 my-1" />
            <button
              disabled
              className="
                w-full flex items-center gap-2 px-3 py-2
                text-gray-400 dark:text-gray-600 cursor-not-allowed
              "
            >
              <FilePlus className="w-4 h-4" />
              Insert Blank Page
              <span className="ml-auto text-[10px] bg-gray-200 dark:bg-gray-700 text-gray-500 dark:text-gray-400 px-1.5 py-0.5 rounded">
                Soon
              </span>
            </button>
          </div>
        )}
      </div>
    </>
  );
}

// ─── Cached Thumbnail Component ───────────────────────────────────────────────
// Uses IntersectionObserver for lazy loading and caches loaded thumbnails

function CachedThumbnail({ docId, pageIndex, pageVersion }: { docId: string; pageIndex: number; pageVersion: number }) {
  const imgRef = useRef<HTMLImageElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [isVisible, setIsVisible] = useState(false);
  const [src, setSrc] = useState<string | null>(null);

  // IntersectionObserver for lazy loading
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setIsVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "200px" }, // Load 200px before entering viewport
    );

    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Load thumbnail when visible
  useEffect(() => {
    if (!isVisible) return;

    // Check cache first
    const cached = getCachedThumbnail(docId, pageIndex, pageVersion);
    if (cached) {
      setSrc(cached);
      return;
    }

    // Load and cache
    const url = `${getThumbnailUrl(docId, pageIndex)}?v=${pageVersion}`;
    setSrc(url);

    // Cache after load
    const img = new Image();
    img.onload = () => {
      // Create a blob URL from the loaded image for caching
      const canvas = document.createElement("canvas");
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      const ctx = canvas.getContext("2d");
      if (ctx) {
        ctx.drawImage(img, 0, 0);
        canvas.toBlob((blob) => {
          if (blob) {
            const blobUrl = URL.createObjectURL(blob);
            setCachedThumbnail(docId, pageIndex, pageVersion, blobUrl);
            setSrc(blobUrl);
          }
        });
      }
    };
    img.src = url;
  }, [isVisible, docId, pageIndex, pageVersion]);

  return (
    <div ref={containerRef} className="w-full aspect-[3/4] bg-gray-50 dark:bg-gray-800">
      {src ? (
        <img
          ref={imgRef}
          src={src}
          alt={`Page ${pageIndex + 1}`}
          className="w-full h-auto"
          draggable={false}
        />
      ) : (
        <div className="w-full h-full flex items-center justify-center">
          <div className="w-4 h-4 border-2 border-gray-300 border-t-blue-500 rounded-full animate-spin" />
        </div>
      )}
    </div>
  );
}
