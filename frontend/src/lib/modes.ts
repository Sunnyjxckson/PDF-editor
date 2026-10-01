/**
 * The editor's tool modes, shared by the desktop tool rail, the mobile menu,
 * the keyboard shortcut handler and the shortcuts dialog. One definition so the
 * four never drift apart.
 */
import {
  MousePointer2,
  TextCursorInput,
  Type,
  Shapes,
  MessageSquarePlus,
  Highlighter,
  Pencil,
  Eraser,
  ScanSearch,
  PenLine,
  ClipboardList,
  EyeOff,
  LayoutGrid,
  FileOutput,
  Lock,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import type { SidePanel, Tool } from "./store";

export type ModeGroup = "Edit" | "Review" | "Prepare" | "Document";

export interface Mode {
  id: string;
  label: string;
  /** longer text for tooltips */
  hint: string;
  icon: LucideIcon;
  /** single key (case-sensitive: "E" means Shift+E) */
  shortcut?: string;
  group: ModeGroup;
  tool?: Tool;
  panel?: SidePanel;
  organize?: boolean;
}

export const MODES: Mode[] = [
  { id: "select", label: "Select", hint: "Select and move text blocks", icon: MousePointer2, shortcut: "v", group: "Edit", tool: "select" },
  { id: "edit_text", label: "Edit text", hint: "Edit existing text in place: paragraphs reflow, fonts are kept", icon: TextCursorInput, shortcut: "e", group: "Edit", tool: "edit_text" },
  { id: "text", label: "Add text", hint: "Click anywhere to add new text", icon: Type, shortcut: "t", group: "Edit", tool: "text" },
  { id: "objects", label: "Objects", hint: "Move, resize, crop, replace images; draw shapes", icon: Shapes, shortcut: "o", group: "Edit", tool: "objects" },

  { id: "comment", label: "Comment", hint: "Notes, highlights, shapes, stamps (real PDF annotations)", icon: MessageSquarePlus, shortcut: "c", group: "Review", tool: "comment" },
  { id: "highlight", label: "Highlight", hint: "Quick highlight", icon: Highlighter, shortcut: "h", group: "Review", tool: "highlight" },
  { id: "draw", label: "Draw", hint: "Freehand ink", icon: Pencil, shortcut: "d", group: "Review", tool: "draw" },
  { id: "eraser", label: "Eraser", hint: "Erase unsaved ink", icon: Eraser, shortcut: "E", group: "Review", tool: "eraser" },
  { id: "region_select", label: "Ask AI", hint: "Select a region and ask the AI about it", icon: ScanSearch, shortcut: "s", group: "Review", tool: "region_select" },

  { id: "sign", label: "Fill & Sign", hint: "Signatures, initials, dates; digital IDs and verification", icon: PenLine, shortcut: "g", group: "Prepare", tool: "sign" },
  { id: "forms", label: "Forms", hint: "Fill, create, detect and flatten form fields", icon: ClipboardList, shortcut: "f", group: "Prepare", tool: "forms" },
  { id: "redact", label: "Redact", hint: "True redaction, PII search, sanitize", icon: EyeOff, shortcut: "r", group: "Prepare", tool: "redact" },

  { id: "organize", label: "Organize", hint: "Reorder, insert, extract, crop, split pages", icon: LayoutGrid, shortcut: "p", group: "Document", organize: true },
  { id: "convert", label: "Convert", hint: "OCR, export to Word/Excel/images, create, compress", icon: FileOutput, group: "Document", panel: "convert" },
  { id: "protect", label: "Protect", hint: "Passwords and permissions", icon: Lock, group: "Document", panel: "protect" },
  { id: "tools", label: "More", hint: "Watermark, header & footer, PDF/A, flatten, compare, bookmarks", icon: Wrench, group: "Document", panel: "tools" },
];

export const MODE_GROUPS: ModeGroup[] = ["Edit", "Review", "Prepare", "Document"];

interface ModeState {
  activeTool: Tool;
  activePanel: SidePanel | null;
  organizeOpen: boolean;
}

export function isModeActive(mode: Mode, s: ModeState): boolean {
  if (mode.organize) return s.organizeOpen;
  if (mode.tool) return !s.organizeOpen && s.activeTool === mode.tool;
  if (mode.panel) return s.activePanel === mode.panel;
  return false;
}

interface ModeActions {
  setActiveTool: (t: Tool) => void;
  togglePanel: (p: SidePanel) => void;
  setOrganizeOpen: (open: boolean) => void;
  organizeOpen: boolean;
}

export function activateMode(mode: Mode, a: ModeActions): void {
  if (mode.organize) {
    a.setOrganizeOpen(!a.organizeOpen);
    return;
  }
  if (a.organizeOpen) a.setOrganizeOpen(false);
  if (mode.tool) a.setActiveTool(mode.tool);
  else if (mode.panel) a.togglePanel(mode.panel);
}

export function modeForShortcut(key: string): Mode | undefined {
  return MODES.find((m) => m.shortcut === key);
}

/** "e" -> "E", "E" -> "Shift+E" */
export function shortcutLabel(key: string): string {
  return key !== key.toLowerCase() ? `Shift+${key}` : key.toUpperCase();
}
