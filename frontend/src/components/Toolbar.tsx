"use client";

import { useState } from "react";
import {
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
  MessageSquare,
  Undo2,
  Redo2,
  Sun,
  Moon,
  History,
  Keyboard,
} from "lucide-react";
import * as Tooltip from "@radix-ui/react-tooltip";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useEditorStore } from "@/lib/store";
import { rotatePage, deletePage, getExportUrl, getDocumentInfo, getHistory, type HistoryState } from "@/lib/api";
import { MODES, activateMode, isModeActive } from "@/lib/modes";
import { performUndo, performRedo } from "@/lib/history";

// 100% = actual physical size (72pt = 1in = 96 CSS px); see CSS_PX_PER_PT in the store.
const zoomPresets = [
  { label: "50%", value: 0.5 },
  { label: "75%", value: 0.75 },
  { label: "100%", value: 1 },
  { label: "125%", value: 1.25 },
  { label: "150%", value: 1.5 },
  { label: "200%", value: 2 },
  { label: "300%", value: 3 },
];

const fontSizeOptions = [8, 10, 12, 14, 16, 18, 20, 24, 28, 32, 36, 48];

const tooltipContentClass =
  "px-2.5 py-1.5 text-xs bg-gray-900 dark:bg-gray-100 text-white dark:text-gray-900 rounded-lg shadow-lg z-[100]";
const tooltipArrowClass = "fill-gray-900 dark:fill-gray-100";

function TooltipButton({
  label,
  shortcut,
  onClick,
  className,
  disabled,
  children,
}: {
  label: string;
  shortcut?: string;
  onClick?: () => void;
  className?: string;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <Tooltip.Root>
      <Tooltip.Trigger asChild>
        <button onClick={onClick} className={className} disabled={disabled}>
          {children}
        </button>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content side="bottom" className={tooltipContentClass} sideOffset={5}>
          {shortcut ? `${label} (${shortcut})` : label}
          <Tooltip.Arrow className={tooltipArrowClass} />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

const Divider = () => (
  <div className="hidden sm:block w-px h-6 bg-gray-200 dark:bg-gray-700 mx-0.5" />
);

export default function Toolbar() {
  const store = useEditorStore();
  const {
    activeTool,
    setActiveTool,
    zoom,
    setZoom,
    zoomMode,
    setZoomMode,
    zoomIn,
    zoomOut,
    currentPage,
    totalPages,
    setCurrentPage,
    docId,
    sidebarOpen,
    toggleSidebar,
    mobileMenuOpen,
    toggleMobileMenu,
    bumpVersion,
    document: docInfo,
    drawColor,
    setDrawColor,
    drawWidth,
    setDrawWidth,
    highlightColor,
    setHighlightColor,
    findReplaceOpen,
    setFindReplaceOpen,
    chatOpen,
    toggleChat,
    setDocument,
    darkMode,
    toggleDarkMode,
    addToast,
  } = store;

  const fontSize = useEditorStore((s) => s.fontSize);
  const setFontSize = useEditorStore((s) => s.setFontSize);
  const activePanel = useEditorStore((s) => s.activePanel);
  const organizeOpen = useEditorStore((s) => s.organizeOpen);
  const togglePanel = useEditorStore((s) => s.togglePanel);
  const setOrganizeOpen = useEditorStore((s) => s.setOrganizeOpen);
  const setShortcutsOpen = useEditorStore((s) => s.setShortcutsOpen);
  const pageVersion = useEditorStore((s) => s.pageVersion);
  const [history, setHistory] = useState<HistoryState | null>(null);
  const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);
  const mod = isMac ? "Cmd" : "Ctrl";

  const loadHistory = async () => {
    if (!docId) return;
    try {
      setHistory(await getHistory(docId));
    } catch {
      setHistory(null);
    }
  };

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
    addToast("PDF exported successfully", "success");
  };

  const btnBase =
    "p-2 rounded-lg transition-colors text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800";
  return (
    <Tooltip.Provider delayDuration={300}>
      {/* Top toolbar */}
      <div className="h-12 sm:h-14 bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-800 flex items-center px-2 sm:px-4 gap-1 sm:gap-1.5 shrink-0">
        {/* Mobile menu toggle */}
        <button
          onClick={toggleMobileMenu}
          className="sm:hidden p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800"
          aria-label="Toggle menu"
        >
          {mobileMenuOpen ? <X className="w-5 h-5" /> : <Menu className="w-5 h-5" />}
        </button>

        {/* Sidebar toggle - desktop */}
        <TooltipButton
          label={sidebarOpen ? "Hide pages" : "Show pages"}
          shortcut="["
          onClick={toggleSidebar}
          className={`hidden sm:flex ${btnBase}`}
        >
          {sidebarOpen ? (
            <PanelLeftClose className="w-5 h-5" />
          ) : (
            <PanelLeftOpen className="w-5 h-5" />
          )}
        </TooltipButton>

        <Divider />

        {/* Contextual tool options */}
        {activeTool === "draw" && (
          <div className="hidden sm:flex items-center gap-1.5 ml-1">
            <Tooltip.Root>
              <Tooltip.Trigger asChild>
                <input
                  type="color"
                  value={drawColor}
                  onChange={(e) => setDrawColor(e.target.value)}
                  className="w-7 h-7 rounded cursor-pointer border border-gray-300 dark:border-gray-600"
                />
              </Tooltip.Trigger>
              <Tooltip.Portal>
                <Tooltip.Content side="bottom" className={tooltipContentClass} sideOffset={5}>
                  Pen Color
                  <Tooltip.Arrow className={tooltipArrowClass} />
                </Tooltip.Content>
              </Tooltip.Portal>
            </Tooltip.Root>
            <select
              value={drawWidth}
              onChange={(e) => setDrawWidth(Number(e.target.value))}
              className="h-7 text-xs rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-1"
              title="Brush size"
            >
              <option value={1}>Thin</option>
              <option value={3}>Medium</option>
              <option value={5}>Thick</option>
              <option value={8}>Bold</option>
            </select>
          </div>
        )}
        {activeTool === "highlight" && (
          <div className="hidden sm:flex items-center gap-1.5 ml-1">
            <Tooltip.Root>
              <Tooltip.Trigger asChild>
                <input
                  type="color"
                  value={highlightColor}
                  onChange={(e) => setHighlightColor(e.target.value)}
                  className="w-7 h-7 rounded cursor-pointer border border-gray-300 dark:border-gray-600"
                />
              </Tooltip.Trigger>
              <Tooltip.Portal>
                <Tooltip.Content side="bottom" className={tooltipContentClass} sideOffset={5}>
                  Highlight Color
                  <Tooltip.Arrow className={tooltipArrowClass} />
                </Tooltip.Content>
              </Tooltip.Portal>
            </Tooltip.Root>
          </div>
        )}
        {activeTool === "text" && (
          <div className="hidden sm:flex items-center gap-1.5 ml-1">
            <select
              value={fontSize}
              onChange={(e) => setFontSize(Number(e.target.value))}
              className="h-7 text-xs rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-1"
              title="Font size"
            >
              {fontSizeOptions.map((size) => (
                <option key={size} value={size}>
                  {size}px
                </option>
              ))}
            </select>
          </div>
        )}

        <Divider />

        {/* Undo / Redo / History */}
        <div className="hidden sm:flex items-center gap-0.5">
          <TooltipButton label="Undo" shortcut={`${mod}+Z`} onClick={() => void performUndo()} className={btnBase}>
            <Undo2 className="w-5 h-5" />
          </TooltipButton>
          <TooltipButton label="Redo" shortcut={`${mod}+Shift+Z`} onClick={() => void performRedo()} className={btnBase}>
            <Redo2 className="w-5 h-5" />
          </TooltipButton>
          <DropdownMenu.Root onOpenChange={(open) => { if (open) void loadHistory(); }}>
            <DropdownMenu.Trigger asChild>
              <button className={btnBase} aria-label="Edit history" title="Edit history" data-version={pageVersion}>
                <History className="w-5 h-5" />
              </button>
            </DropdownMenu.Trigger>
            <DropdownMenu.Portal>
              <DropdownMenu.Content
                className="w-72 max-h-80 overflow-y-auto bg-white dark:bg-gray-800 rounded-lg shadow-xl border border-gray-200 dark:border-gray-700 py-1 z-[100] text-sm"
                sideOffset={5}
                align="start"
              >
                <DropdownMenu.Label className="px-3 py-1.5 text-xs font-semibold text-gray-500 dark:text-gray-400">Edit history</DropdownMenu.Label>
                {!history || history.versions.length === 0 ? (
                  <div className="px-3 py-2 text-xs text-gray-500">No changes yet</div>
                ) : (
                  history.versions
                    .filter((v) => !v.operation.startsWith("("))
                    .slice()
                    .reverse()
                    .map((v) => (
                      <div
                        key={v.index}
                        className={`px-3 py-1.5 text-xs flex justify-between gap-2 ${v.index > history.current ? "text-gray-400 line-through" : "text-gray-700 dark:text-gray-200"}`}
                        title={v.index > history.current ? "Undone (can be redone)" : undefined}
                      >
                        <span className="truncate">{v.operation}</span>
                        <span className="shrink-0 tabular-nums text-gray-400">{new Date(v.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
                      </div>
                    ))
                )}
                <DropdownMenu.Separator className="h-px bg-gray-200 dark:bg-gray-700 my-1" />
                <DropdownMenu.Item onSelect={() => void performUndo()} disabled={!history?.can_undo} className="px-3 py-1.5 text-xs cursor-pointer outline-none data-[disabled]:opacity-40 hover:bg-gray-100 dark:hover:bg-gray-700">Undo last change</DropdownMenu.Item>
                <DropdownMenu.Item onSelect={() => void performRedo()} disabled={!history?.can_redo} className="px-3 py-1.5 text-xs cursor-pointer outline-none data-[disabled]:opacity-40 hover:bg-gray-100 dark:hover:bg-gray-700">Redo</DropdownMenu.Item>
              </DropdownMenu.Content>
            </DropdownMenu.Portal>
          </DropdownMenu.Root>
        </div>

        <Divider />

        {/* Page operations - desktop */}
        <div className="hidden sm:flex items-center gap-0.5">
          <TooltipButton
            label="Rotate page"
            onClick={handleRotate}
            className={btnBase}
          >
            <RotateCw className="w-5 h-5" />
          </TooltipButton>
          <TooltipButton
            label="Delete page"
            onClick={handleDelete}
            className="p-2 rounded-lg transition-colors hover:bg-gray-100 dark:hover:bg-gray-800 text-red-500 disabled:opacity-30"
            disabled={totalPages <= 1}
          >
            <Trash2 className="w-5 h-5" />
          </TooltipButton>
          <TooltipButton
            label="Find & Replace"
            shortcut="Ctrl+F"
            onClick={() => setFindReplaceOpen(!findReplaceOpen)}
            className={
              findReplaceOpen
                ? "p-2 rounded-lg transition-colors bg-blue-100 dark:bg-blue-900/40 text-blue-600"
                : btnBase
            }
          >
            <Search className="w-5 h-5" />
          </TooltipButton>
          <TooltipButton
            label="AI Chat"
            shortcut="Ctrl+/"
            onClick={toggleChat}
            className={
              chatOpen
                ? "p-2 rounded-lg transition-colors bg-purple-100 dark:bg-purple-900/40 text-purple-600"
                : btnBase
            }
          >
            <MessageSquare className="w-5 h-5" />
          </TooltipButton>
        </div>

        <div className="flex-1" />

        {/* Page navigation */}
        <div className="flex items-center gap-1 sm:gap-2">
          <TooltipButton
            label="Previous page"
            onClick={() => setCurrentPage(Math.max(0, currentPage - 1))}
            disabled={currentPage === 0}
            className="p-1.5 sm:p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-30"
          >
            <ChevronLeft className="w-4 h-4 sm:w-5 sm:h-5" />
          </TooltipButton>
          <span className="text-xs sm:text-sm text-gray-600 dark:text-gray-400 min-w-[60px] text-center tabular-nums">
            {currentPage + 1} / {totalPages}
          </span>
          <TooltipButton
            label="Next page"
            onClick={() => setCurrentPage(Math.min(totalPages - 1, currentPage + 1))}
            disabled={currentPage >= totalPages - 1}
            className="p-1.5 sm:p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-30"
          >
            <ChevronRight className="w-4 h-4 sm:w-5 sm:h-5" />
          </TooltipButton>
        </div>

        <div className="w-px h-6 bg-gray-200 dark:bg-gray-700" />

        {/* Zoom controls - desktop */}
        <div className="hidden sm:flex items-center gap-0.5">
          <TooltipButton
            label="Zoom out"
            shortcut="Ctrl/Cmd -"
            onClick={zoomOut}
            className={btnBase}
          >
            <ZoomOut className="w-5 h-5" />
          </TooltipButton>

          <DropdownMenu.Root>
            <Tooltip.Root>
              <Tooltip.Trigger asChild>
                <DropdownMenu.Trigger asChild>
                  <button
                    aria-label={`Zoom ${Math.round(zoom * 100)}%${zoomMode === "fit-width" ? ", fit width" : zoomMode === "fit-page" ? ", fit page" : ""}`}
                    data-testid="zoom-menu"
                    className="text-sm text-gray-600 dark:text-gray-400 min-w-[48px] text-center tabular-nums px-1.5 py-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
                  >
                    {Math.round(zoom * 100)}%
                  </button>
                </DropdownMenu.Trigger>
              </Tooltip.Trigger>
              <Tooltip.Portal>
                <Tooltip.Content side="bottom" className={tooltipContentClass} sideOffset={5}>
                  Zoom level
                  <Tooltip.Arrow className={tooltipArrowClass} />
                </Tooltip.Content>
              </Tooltip.Portal>
            </Tooltip.Root>
            <DropdownMenu.Portal>
              <DropdownMenu.Content
                className="min-w-[120px] bg-white dark:bg-gray-800 rounded-lg shadow-xl border border-gray-200 dark:border-gray-700 py-1 z-[100]"
                sideOffset={5}
                align="center"
              >
                {zoomPresets.map((preset) => (
                  <DropdownMenu.Item
                    key={preset.value}
                    onSelect={() => setZoom(preset.value)}
                    className={`px-3 py-1.5 text-sm cursor-pointer outline-none transition-colors ${
                      zoomMode === "custom" && Math.abs(zoom - preset.value) < 0.01
                        ? "bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400"
                        : "text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
                    }`}
                  >
                    {preset.label}
                  </DropdownMenu.Item>
                ))}
                <DropdownMenu.Separator className="h-px bg-gray-200 dark:bg-gray-700 my-1" />
                {(
                  [
                    ["custom", "Actual size", "Ctrl/Cmd 0"],
                    ["fit-width", "Fit width", ""],
                    ["fit-page", "Fit page", ""],
                  ] as const
                ).map(([mode, label, hint]) => {
                  const on = mode === "custom" ? zoomMode === "custom" && Math.abs(zoom - 1) < 0.01 : zoomMode === mode;
                  return (
                    <DropdownMenu.Item
                      key={mode}
                      onSelect={() => (mode === "custom" ? setZoom(1) : setZoomMode(mode))}
                      className={`px-3 py-1.5 text-sm cursor-pointer outline-none transition-colors flex items-center justify-between gap-3 ${
                        on
                          ? "bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400"
                          : "text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
                      }`}
                    >
                      {label}
                      {hint && <span className="text-[10px] text-gray-400">{hint}</span>}
                    </DropdownMenu.Item>
                  );
                })}
              </DropdownMenu.Content>
            </DropdownMenu.Portal>
          </DropdownMenu.Root>

          <TooltipButton
            label="Zoom in"
            shortcut="Ctrl/Cmd +"
            onClick={zoomIn}
            className={btnBase}
          >
            <ZoomIn className="w-5 h-5" />
          </TooltipButton>
        </div>

        <div className="hidden sm:block w-px h-6 bg-gray-200 dark:bg-gray-700" />

        <TooltipButton
          label="Keyboard shortcuts"
          shortcut="?"
          onClick={() => setShortcutsOpen(true)}
          className={`hidden md:flex ${btnBase}`}
        >
          <Keyboard className="w-5 h-5" />
        </TooltipButton>

        {/* Dark mode toggle - desktop */}
        <TooltipButton
          label="Toggle dark mode"
          onClick={toggleDarkMode}
          className={`hidden sm:flex ${btnBase}`}
        >
          {darkMode ? <Sun className="w-5 h-5" /> : <Moon className="w-5 h-5" />}
        </TooltipButton>

        {/* Export */}
        <TooltipButton
          label="Download PDF"
          shortcut="Ctrl+S"
          onClick={handleExport}
          className="p-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white transition-colors"
        >
          <Download className="w-4 h-4 sm:w-5 sm:h-5" />
        </TooltipButton>
      </div>

      {/* Mobile dropdown menu */}
      {mobileMenuOpen && (
        <div className="sm:hidden bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-800 p-3 space-y-3">
          {/* Tool buttons */}
          <div className="flex items-center gap-1 justify-center flex-wrap">
            {MODES.map((mode) => (
              <button
                key={mode.id}
                onClick={() => {
                  activateMode(mode, { setActiveTool, togglePanel, setOrganizeOpen, organizeOpen });
                  toggleMobileMenu();
                }}
                className={`p-2 w-[72px] rounded-lg transition-colors flex flex-col items-center gap-1 ${
                  isModeActive(mode, { activeTool, activePanel, organizeOpen })
                    ? "bg-blue-100 dark:bg-blue-900/40 text-blue-600 dark:text-blue-300"
                    : "hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-300"
                }`}
              >
                <mode.icon className="w-5 h-5" />
                <span className="text-[10px]">{mode.label}</span>
              </button>
            ))}
          </div>

          {/* Actions */}
          <div className="flex items-center gap-2 justify-center flex-wrap">
            <button
              onClick={() => void performUndo()}
              className="flex items-center gap-1 px-3 py-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-sm"
            >
              <Undo2 className="w-4 h-4" /> Undo
            </button>
            <button
              onClick={() => void performRedo()}
              className="flex items-center gap-1 px-3 py-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-sm"
            >
              <Redo2 className="w-4 h-4" /> Redo
            </button>
            <button
              onClick={() => {
                handleRotate();
                toggleMobileMenu();
              }}
              className="flex items-center gap-1 px-3 py-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-sm"
            >
              <RotateCw className="w-4 h-4" /> Rotate
            </button>
            <button
              onClick={() => {
                handleDelete();
                toggleMobileMenu();
              }}
              className="flex items-center gap-1 px-3 py-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-sm text-red-500"
              disabled={totalPages <= 1}
            >
              <Trash2 className="w-4 h-4" /> Delete
            </button>
            <button
              onClick={() => {
                setFindReplaceOpen(!findReplaceOpen);
                toggleMobileMenu();
              }}
              className="flex items-center gap-1 px-3 py-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-sm"
            >
              <Search className="w-4 h-4" /> Find
            </button>
            <button
              onClick={() => {
                toggleChat();
                toggleMobileMenu();
              }}
              className="flex items-center gap-1 px-3 py-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-sm"
            >
              <MessageSquare className="w-4 h-4" /> Chat
            </button>
            <button
              onClick={() => {
                toggleDarkMode();
                toggleMobileMenu();
              }}
              className="flex items-center gap-1 px-3 py-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-sm"
            >
              {darkMode ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
              {darkMode ? "Light" : "Dark"}
            </button>
          </div>

          {/* Zoom */}
          <div className="flex items-center gap-2 justify-center">
            <button
              onClick={zoomOut}
              className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800"
            >
              <ZoomOut className="w-4 h-4" />
            </button>
            <span className="text-sm tabular-nums min-w-[40px] text-center">
              {Math.round(zoom * 100)}%
            </span>
            <button
              onClick={zoomIn}
              className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800"
            >
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
                onChange={(e) =>
                  activeTool === "draw"
                    ? setDrawColor(e.target.value)
                    : setHighlightColor(e.target.value)
                }
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
          {activeTool === "text" && (
            <div className="flex items-center gap-2 justify-center">
              <span className="text-xs text-gray-500">Font size:</span>
              <select
                value={fontSize}
                onChange={(e) => setFontSize(Number(e.target.value))}
                className="h-7 text-xs rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-1"
              >
                {fontSizeOptions.map((size) => (
                  <option key={size} value={size}>
                    {size}px
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>
      )}
    </Tooltip.Provider>
  );
}
