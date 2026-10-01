"use client";

// Comment/markup tool palette. Holds no state of its own: the app keeps one
// MarkupSettings object and passes it to both this toolbar and
// <OrganizeMarkupOverlay> (which turns mouse gestures on the page into real
// PDF annotations).

import { useEffect, useState } from "react";
import {
  ArrowUpRight, Circle, Highlighter, MessageSquareText, Minus, MousePointer2, PenLine, Pentagon,
  Square, Stamp, StickyNote, Strikethrough, Type, Underline, Waves,
} from "lucide-react";
import {
  DEFAULT_TOOL_COLORS, listStamps, type MarkupSettings, type MarkupTool,
} from "@/lib/features/organize";

export interface OrganizeMarkupToolbarProps {
  settings: MarkupSettings;
  onChange: (s: MarkupSettings) => void;
  /** If given, the stamp list is loaded from the backend (/organize/stamps). */
  docId?: string;
  className?: string;
}

const TOOLS: { tool: MarkupTool; label: string; Icon: typeof Square }[] = [
  { tool: "note", label: "Sticky note (click)", Icon: StickyNote },
  { tool: "highlight", label: "Highlight text (drag across text)", Icon: Highlighter },
  { tool: "underline", label: "Underline text", Icon: Underline },
  { tool: "strikeout", label: "Strikethrough text", Icon: Strikethrough },
  { tool: "squiggly", label: "Squiggly underline", Icon: Waves },
  { tool: "freetext", label: "Text box (drag a box)", Icon: Type },
  { tool: "callout", label: "Callout (drag from the point to where the text goes)", Icon: MessageSquareText },
  { tool: "rect", label: "Rectangle", Icon: Square },
  { tool: "ellipse", label: "Ellipse", Icon: Circle },
  { tool: "line", label: "Line", Icon: Minus },
  { tool: "arrow", label: "Arrow", Icon: ArrowUpRight },
  { tool: "polygon", label: "Polygon (click points, double-click to finish)", Icon: Pentagon },
  { tool: "ink", label: "Freehand pen", Icon: PenLine },
  { tool: "stamp", label: "Stamp (click or drag)", Icon: Stamp },
];

const FALLBACK_STAMPS = [
  "Approved", "AsIs", "Confidential", "Departmental", "Draft", "Experimental", "Expired", "Final",
  "ForComment", "ForPublicRelease", "NotApproved", "NotForPublicRelease", "Sold", "TopSecret",
];

const SHAPES: MarkupTool[] = ["rect", "ellipse", "polygon"];
const STROKED: MarkupTool[] = ["rect", "ellipse", "line", "arrow", "polygon", "ink", "callout"];

export default function OrganizeMarkupToolbar({ settings, onChange, docId, className = "" }: OrganizeMarkupToolbarProps) {
  const [stamps, setStamps] = useState<string[]>(FALLBACK_STAMPS);
  useEffect(() => {
    if (!docId) return;
    let alive = true;
    listStamps(docId).then((s) => { if (alive && s.length) setStamps(s); }).catch(() => {});
    return () => { alive = false; };
  }, [docId]);

  const pick = (tool: MarkupTool | null) => {
    if (tool === settings.tool) tool = null;
    const color = tool && DEFAULT_TOOL_COLORS[tool] && (!settings.tool || DEFAULT_TOOL_COLORS[settings.tool] === settings.color || settings.color === "#d90000")
      ? DEFAULT_TOOL_COLORS[tool]!
      : settings.color;
    onChange({ ...settings, tool, color });
  };

  const t = settings.tool;
  const base = "p-1.5 rounded-lg transition-colors";
  const off = "text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800";
  const on = "bg-blue-100 dark:bg-blue-900/40 text-blue-600 dark:text-blue-400";
  const field =
    "px-1.5 py-0.5 rounded border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-800 dark:text-gray-200 text-xs";

  return (
    <div className={`flex flex-wrap items-center gap-0.5 px-2 py-1 bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-800 ${className}`} role="toolbar" aria-label="Comment tools">
      <button className={`${base} ${t === null ? on : off}`} onClick={() => pick(null)} title="Select (no markup tool)" aria-label="Select" aria-pressed={t === null}>
        <MousePointer2 className="w-4 h-4" />
      </button>
      <div className="w-px h-5 bg-gray-200 dark:bg-gray-700 mx-1" />
      {TOOLS.map(({ tool, label, Icon }) => (
        <button key={tool} className={`${base} ${t === tool ? on : off}`} onClick={() => pick(tool)} title={label} aria-label={label} aria-pressed={t === tool}>
          <Icon className="w-4 h-4" />
        </button>
      ))}
      <div className="w-px h-5 bg-gray-200 dark:bg-gray-700 mx-1" />
      <label className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400" title="Colour">
        <input type="color" value={settings.color} onChange={(e) => onChange({ ...settings, color: e.target.value })}
          className="w-6 h-6 rounded cursor-pointer border border-gray-300 dark:border-gray-600" aria-label="Markup colour" />
      </label>
      {t && SHAPES.includes(t) && (
        <label className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 ml-1">
          <input type="checkbox" checked={settings.fill !== null} onChange={(e) => onChange({ ...settings, fill: e.target.checked ? "#ffffff" : null })} />
          Fill
          {settings.fill !== null && (
            <input type="color" value={settings.fill} onChange={(e) => onChange({ ...settings, fill: e.target.value })}
              className="w-6 h-6 rounded cursor-pointer border border-gray-300 dark:border-gray-600" aria-label="Fill colour" />
          )}
        </label>
      )}
      {t && STROKED.includes(t) && (
        <label className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 ml-1">Width
          <select className={field} value={settings.width} onChange={(e) => onChange({ ...settings, width: Number(e.target.value) })}>
            {[0.5, 1, 1.5, 2, 3, 4, 6].map((w) => <option key={w} value={w}>{w}pt</option>)}
          </select>
        </label>
      )}
      {(t === "freetext" || t === "callout") && (
        <label className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 ml-1">Size
          <select className={field} value={settings.fontSize} onChange={(e) => onChange({ ...settings, fontSize: Number(e.target.value) })}>
            {[8, 9, 10, 11, 12, 14, 16, 18, 24, 32].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
      )}
      {t === "stamp" && (
        <select className={`${field} ml-1`} value={settings.stamp} onChange={(e) => onChange({ ...settings, stamp: e.target.value })} aria-label="Stamp">
          {stamps.map((s) => <option key={s} value={s}>{s.replace(/([a-z])([A-Z])/g, "$1 $2")}</option>)}
        </select>
      )}
      <label className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 ml-1">Opacity
        <select className={field} value={settings.opacity} onChange={(e) => onChange({ ...settings, opacity: Number(e.target.value) })}>
          {[1, 0.8, 0.6, 0.4, 0.2].map((o) => <option key={o} value={o}>{Math.round(o * 100)}%</option>)}
        </select>
      </label>
      <label className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 ml-1">Author
        <input className={`${field} w-24`} value={settings.author} onChange={(e) => onChange({ ...settings, author: e.target.value })} />
      </label>
    </div>
  );
}
