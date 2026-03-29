"use client";

import {
  MousePointer2,
  Type,
  Highlighter,
  Pencil,
  Eraser,
  ZoomIn,
  ZoomOut,
  RotateCw,
  Trash2,
  Download,
  ChevronLeft,
  ChevronRight,
  PanelLeftClose,
  PanelLeftOpen,
  Menu,
  X,
  Search,
  Sparkles,
  MessageSquare,
} from "lucide-react";
import { useEditorStore, type Tool } from "@/lib/store";
import { rotatePage, deletePage, getExportUrl, getDocumentInfo } from "@/lib/api";

const tools: { id: Tool; icon: typeof MousePointer2; label: string }[] = [
  { id: "select", icon: MousePointer2, label: "Select" },
  { id: "text", icon: Type, label: "Text" },
  { id: "highlight", icon: Highlighter, label: "Highlight" },
  { id: "draw", icon: Pencil, label: "Draw" },
  { id: "eraser", icon: Eraser, label: "Eraser" },
];

export default function Toolbar() {
  const store = useEditorStore();
  const {
    activeTool, setActiveTool, zoom, setZoom,
    currentPage, totalPages, setCurrentPage,
    docId, sidebarOpen, toggleSidebar,
    mobileMenuOpen, toggleMobileMenu, bumpVersion,
    document: docInfo, drawColor, setDrawColor,
    drawWidth, setDrawWidth, highlightColor, setHighlightColor,
    findReplaceOpen, setFindReplaceOpen,
    aiPanelOpen, setAiPanelOpen,
    chatOpen, toggleChat,
    setDocument,
  } = store;

  const handleRotate = async () => {
    if (!docId || !docInfo) return;
    const current = docInfo.pages[currentPage]?.rotation || 0;
    const next = (current + 90) % 360;
    await rotatePage(docId, currentPage, next);
    const updated = await getDocumentInfo(docId);
    setDocument(updated, docId);
    bumpVersion();
  };

  const handleDelete = async () => {
    if (!docId || totalPages <= 1) return;
    if (!confirm(`Delete page ${currentPage + 1}?`)) return;
    await deletePage(docId, currentPage);
    const updated = await getDocumentInfo(docId);
    setDocument(updated, docId);
    if (currentPage >= updated.page_count) {
      setCurrentPage(Math.max(0, updated.page_count - 1));
    }
    bumpVersion();
  };

  const handleExport = () => {
    if (!docId) return;
    window.open(getExportUrl(docId), "_blank");
  };

  return (
    <>
      {/* Top toolbar */}
      <div className="h-12 sm:h-14 bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-800 flex items-center px-2 sm:px-4 gap-1 sm:gap-2 shrink-0">
        {/* Mobile menu toggle */}
        <button
          onClick={toggleMobileMenu}
          className="sm:hidden p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800"
          aria-label="Toggle menu"
        >
          {mobileMenuOpen ? <X className="w-5 h-5" /> : <Menu className="w-5 h-5" />}
        </button>

        {/* Sidebar toggle - desktop */}
        <button
          onClick={toggleSidebar}
          className="hidden sm:flex p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800"
          title={sidebarOpen ? "Hide pages" : "Show pages"}
        >
          {sidebarOpen ? <PanelLeftClose className="w-5 h-5" /> : <PanelLeftOpen className="w-5 h-5" />}
        </button>

        <div className="hidden sm:block w-px h-6 bg-gray-200 dark:bg-gray-700" />

        {/* Tools - desktop */}
        <div className="hidden sm:flex items-center gap-0.5">
          {tools.map((tool) => (
            <button
              key={tool.id}
              onClick={() => setActiveTool(tool.id)}
              className={`p-2 rounded-lg transition-colors ${
                activeTool === tool.id
                  ? "bg-blue-100 dark:bg-blue-900/40 text-blue-600 dark:text-blue-400"
                  : "hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-400"
              }`}
              title={tool.label}
            >
              <tool.icon className="w-5 h-5" />
            </button>
          ))}
        </div>

        {/* Tool-specific options */}
        {activeTool === "draw" && (
          <div className="hidden sm:flex items-center gap-1 ml-1">
            <input
              type="color"
              value={drawColor}
              onChange={(e) => setDrawColor(e.target.value)}
              className="w-7 h-7 rounded cursor-pointer border-0"
              title="Pen color"
            />
            <select
              value={drawWidth}
              onChange={(e) => setDrawWidth(Number(e.target.value))}
              className="h-7 text-xs rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-1"
            >
              <option value={1}>Thin</option>
              <option value={3}>Medium</option>
              <option value={5}>Thick</option>
              <option value={8}>Bold</option>
            </select>
          </div>
        )}
        {activeTool === "highlight" && (
          <div className="hidden sm:flex items-center gap-1 ml-1">
            <input
              type="color"
              value={highlightColor}
              onChange={(e) => setHighlightColor(e.target.value)}
              className="w-7 h-7 rounded cursor-pointer border-0"
              title="Highlight color"
            />
          </div>
        )}

        <div className="hidden sm:block w-px h-6 bg-gray-200 dark:bg-gray-700" />

        {/* Page operations - desktop */}
        <div className="hidden sm:flex items-center gap-0.5">
          <button
            onClick={handleRotate}
            className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-400"
            title="Rotate page"
          >
            <RotateCw className="w-5 h-5" />
          </button>
          <button
            onClick={handleDelete}
            className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-red-500 disabled:opacity-30"
            title="Delete page"
            disabled={totalPages <= 1}
          >
            <Trash2 className="w-5 h-5" />
          </button>
          <button
            onClick={() => setFindReplaceOpen(!findReplaceOpen)}
            className={`p-2 rounded-lg transition-colors ${
              findReplaceOpen
                ? "bg-blue-100 dark:bg-blue-900/40 text-blue-600"
                : "hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-400"
            }`}
            title="Find & Replace"
          >
            <Search className="w-5 h-5" />
          </button>
          <button
            onClick={() => setAiPanelOpen(!aiPanelOpen)}
            className={`p-2 rounded-lg transition-colors ${
              aiPanelOpen
                ? "bg-purple-100 dark:bg-purple-900/40 text-purple-600"
                : "hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-400"
            }`}
            title="AI Assist"
          >
            <Sparkles className="w-5 h-5" />
          </button>
          <button
            onClick={toggleChat}
            className={`p-2 rounded-lg transition-colors ${
              chatOpen
                ? "bg-purple-100 dark:bg-purple-900/40 text-purple-600"
                : "hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-400"
            }`}
            title="AI Chat (Ctrl+/)"
          >
            <MessageSquare className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1" />

        {/* Page navigation */}
        <div className="flex items-center gap-1 sm:gap-2">
          <button
            onClick={() => setCurrentPage(Math.max(0, currentPage - 1))}
            disabled={currentPage === 0}
            className="p-1.5 sm:p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-30"
          >
            <ChevronLeft className="w-4 h-4 sm:w-5 sm:h-5" />
          </button>
          <span className="text-xs sm:text-sm text-gray-600 dark:text-gray-400 min-w-[60px] text-center tabular-nums">
            {currentPage + 1} / {totalPages}
          </span>
          <button
            onClick={() => setCurrentPage(Math.min(totalPages - 1, currentPage + 1))}
            disabled={currentPage >= totalPages - 1}
            className="p-1.5 sm:p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-30"
          >
            <ChevronRight className="w-4 h-4 sm:w-5 sm:h-5" />
          </button>
        </div>

        <div className="w-px h-6 bg-gray-200 dark:bg-gray-700" />

        {/* Zoom - desktop */}
        <div className="hidden sm:flex items-center gap-1">
          <button onClick={() => setZoom(zoom - 0.25)} className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800">
            <ZoomOut className="w-5 h-5" />
          </button>
          <span className="text-sm text-gray-600 dark:text-gray-400 min-w-[48px] text-center tabular-nums">
            {Math.round(zoom * 100)}%
          </span>
          <button onClick={() => setZoom(zoom + 0.25)} className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800">
            <ZoomIn className="w-5 h-5" />
          </button>
        </div>

        {/* Export */}
        <button
          onClick={handleExport}
          className="p-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white"
          title="Download PDF"
        >
          <Download className="w-4 h-4 sm:w-5 sm:h-5" />
        </button>
      </div>

      {/* Mobile dropdown menu */}
      {mobileMenuOpen && (
        <div className="sm:hidden bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-800 p-3 space-y-3">
          <div className="flex items-center gap-1 justify-center flex-wrap">
            {tools.map((tool) => (
              <button
                key={tool.id}
                onClick={() => { setActiveTool(tool.id); toggleMobileMenu(); }}
                className={`p-3 rounded-lg transition-colors flex flex-col items-center gap-1 ${
                  activeTool === tool.id
                    ? "bg-blue-100 dark:bg-blue-900/40 text-blue-600"
                    : "hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600"
                }`}
              >
                <tool.icon className="w-5 h-5" />
                <span className="text-[10px]">{tool.label}</span>
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2 justify-center flex-wrap">
            <button onClick={() => { handleRotate(); toggleMobileMenu(); }} className="flex items-center gap-1 px-3 py-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-sm">
              <RotateCw className="w-4 h-4" /> Rotate
            </button>
            <button onClick={() => { handleDelete(); toggleMobileMenu(); }} className="flex items-center gap-1 px-3 py-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-sm text-red-500" disabled={totalPages <= 1}>
              <Trash2 className="w-4 h-4" /> Delete
            </button>
            <button onClick={() => { setFindReplaceOpen(!findReplaceOpen); toggleMobileMenu(); }} className="flex items-center gap-1 px-3 py-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-sm">
              <Search className="w-4 h-4" /> Find
            </button>
            <button onClick={() => { setAiPanelOpen(!aiPanelOpen); toggleMobileMenu(); }} className="flex items-center gap-1 px-3 py-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-sm">
              <Sparkles className="w-4 h-4" /> AI
            </button>
            <button onClick={() => { toggleChat(); toggleMobileMenu(); }} className="flex items-center gap-1 px-3 py-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-sm">
              <MessageSquare className="w-4 h-4" /> Chat
            </button>
          </div>
          <div className="flex items-center gap-2 justify-center">
            <button onClick={() => setZoom(zoom - 0.25)} className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800">
              <ZoomOut className="w-4 h-4" />
            </button>
            <span className="text-sm tabular-nums min-w-[40px] text-center">{Math.round(zoom * 100)}%</span>
            <button onClick={() => setZoom(zoom + 0.25)} className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800">
              <ZoomIn className="w-4 h-4" />
            </button>
          </div>
          {/* Color pickers for mobile */}
          {(activeTool === "draw" || activeTool === "highlight") && (
            <div className="flex items-center gap-2 justify-center">
              <span className="text-xs text-gray-500">Color:</span>
              <input
                type="color"
                value={activeTool === "draw" ? drawColor : highlightColor}
                onChange={(e) => activeTool === "draw" ? setDrawColor(e.target.value) : setHighlightColor(e.target.value)}
                className="w-8 h-8 rounded cursor-pointer"
              />
              {activeTool === "draw" && (
                <>
                  <span className="text-xs text-gray-500">Width:</span>
                  <select
                    value={drawWidth}
                    onChange={(e) => setDrawWidth(Number(e.target.value))}
                    className="h-7 text-xs rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-1"
                  >
                    <option value={1}>Thin</option>
                    <option value={3}>Medium</option>
                    <option value={5}>Thick</option>
                    <option value={8}>Bold</option>
                  </select>
                </>
              )}
            </div>
          )}
        </div>
      )}
    </>
  );
}
