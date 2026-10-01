"use client";

/**
 * RedactPanel — Redaction & Security side panel.
 *
 * Tabs:
 *  - Redact: mark tools (drag area / click words) that drive <RedactOverlay>,
 *    mark style (fill colour, overlay text), search-and-redact (text, regex,
 *    PII presets) with a reviewable checkbox list, pending marks list, and
 *    "Apply redactions" behind an irreversible-action confirmation.
 *  - Sanitize: audit of hidden information + one-click removal.
 *  - Protect: AES-256 open/permissions passwords; remove security.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Eraser, EyeOff, FileSearch, Lock, MousePointerSquareDashed, ShieldCheck, Trash2, Type, Unlock, X,
} from "lucide-react";
import {
  applyRedactions,
  clearRedactMarks,
  DEFAULT_SANITIZE,
  deleteRedactMark,
  getSecurityAudit,
  markRedactions,
  matchesToAreas,
  permissionsFrom,
  protectDocument,
  REDACT_PRESETS,
  sanitizeDocument,
  searchRedactions,
  unlockDocument,
  useRedactStore,
  type ApplyOptions,
  type Permission,
  type RedactMatch,
  type SanitizeOptions,
  type SecurityAudit,
} from "@/lib/features/redact";
import RedactConfirmDialog from "./RedactConfirmDialog";

export interface RedactPanelProps {
  docId: string;
  currentPage: number;
  /** Re-render pages / bump version after the PDF changed. */
  onDocumentChanged: () => void;
  /** Jump the viewer to a page (used when clicking a search hit). */
  onNavigate?: (page: number) => void;
  onClose?: () => void;
  /** Toast hook, e.g. useEditorStore().addToast. */
  notify?: (message: string, type?: "success" | "error" | "info") => void;
}

type Tab = "redact" | "sanitize" | "protect";

const btn = "rounded-lg px-2.5 py-1.5 text-xs font-medium transition-colors disabled:opacity-40";
const subtle = `${btn} hover:bg-gray-100 dark:hover:bg-gray-800`;
const input = "w-full rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-2 py-1.5 text-sm outline-none focus:ring-2 focus:ring-red-500/40";

export default function RedactPanel({ docId, currentPage, onDocumentChanged, onNavigate, onClose, notify }: RedactPanelProps) {
  const [tab, setTab] = useState<Tab>("redact");
  const toast = useCallback(
    (m: string, t: "success" | "error" | "info" = "info") => notify?.(m, t),
    [notify],
  );
  const err = useCallback((e: unknown) => toast(e instanceof Error ? e.message : String(e), "error"), [toast]);

  // Deactivate the overlay when the panel unmounts.
  const setActive = useRedactStore((s) => s.setActive);
  useEffect(() => () => setActive(false), [setActive]);

  return (
    <aside className="flex h-full w-full sm:w-80 flex-col border-l border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100">
      <div className="flex items-center justify-between border-b border-gray-200 dark:border-gray-700 px-3 py-2">
        <div className="flex items-center gap-1.5">
          <ShieldCheck className="w-4 h-4 text-red-600" />
          <h3 className="text-sm font-semibold">Redact &amp; Protect</h3>
        </div>
        {onClose && (
          <button onClick={onClose} aria-label="Close" className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800">
            <X className="w-4 h-4" />
          </button>
        )}
      </div>
      <div className="flex gap-1 border-b border-gray-200 dark:border-gray-700 px-2 py-1.5" role="tablist">
        {([
          ["redact", "Redact", EyeOff],
          ["sanitize", "Sanitize", Eraser],
          ["protect", "Protect", Lock],
        ] as const).map(([id, label, Icon]) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
            className={`${btn} flex items-center gap-1 ${tab === id ? "bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-300" : "hover:bg-gray-100 dark:hover:bg-gray-800"}`}
          >
            <Icon className="w-3.5 h-3.5" /> {label}
          </button>
        ))}
      </div>
      <div className="flex-1 overflow-y-auto p-3 space-y-4">
        {tab === "redact" && (
          <RedactTab docId={docId} currentPage={currentPage} onDocumentChanged={onDocumentChanged} onNavigate={onNavigate} toast={toast} err={err} />
        )}
        {tab === "sanitize" && <SanitizeTab docId={docId} onDocumentChanged={onDocumentChanged} toast={toast} err={err} />}
        {tab === "protect" && <ProtectTab docId={docId} onDocumentChanged={onDocumentChanged} toast={toast} err={err} />}
      </div>
    </aside>
  );
}

interface TabProps {
  docId: string;
  onDocumentChanged: () => void;
  toast: (m: string, t?: "success" | "error" | "info") => void;
  err: (e: unknown) => void;
}

// ─── Redact tab ──────────────────────────────────────────────────────────────

function RedactTab({ docId, currentPage, onDocumentChanged, onNavigate, toast, err }: TabProps & { currentPage: number; onNavigate?: (p: number) => void }) {
  const { active, tool, style, marks, setActive, setTool, setStyle, refreshMarks, setPreviewMatches, busy, setBusy } = useRedactStore();

  // search state
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<"text" | "regex">("text");
  const [caseSensitive, setCaseSensitive] = useState(false);
  const [wholeWord, setWholeWord] = useState(false);
  const [presets, setPresets] = useState<Set<string>>(new Set());
  const [matches, setMatches] = useState<RedactMatch[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [searched, setSearched] = useState(false);
  const [truncated, setTruncated] = useState(false);

  // apply state
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [images, setImages] = useState<NonNullable<ApplyOptions["images"]>>("pixels");
  const [graphics, setGraphics] = useState<NonNullable<ApplyOptions["graphics"]>>("covered");

  useEffect(() => { refreshMarks(docId).catch(() => {}); }, [docId, refreshMarks]);

  // Show ticked search hits on the page overlay.
  useEffect(() => {
    setPreviewMatches(matches.filter((m) => selected.has(m.id)));
  }, [matches, selected, setPreviewMatches]);
  useEffect(() => () => setPreviewMatches([]), [setPreviewMatches]);

  const togglePreset = (id: string) =>
    setPresets((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });

  const runSearch = async () => {
    if (!query.trim() && presets.size === 0) return;
    setBusy(true);
    try {
      const r = await searchRedactions(docId, {
        query: query.trim() || undefined, mode, case_sensitive: caseSensitive, whole_word: wholeWord, presets: [...presets],
      });
      setMatches(r.matches);
      setSelected(new Set(r.matches.map((m) => m.id)));
      setTruncated(r.truncated);
      setSearched(true);
    } catch (e) { err(e); }
    setBusy(false);
  };

  const markSelected = async () => {
    const areas = matchesToAreas(matches, selected);
    if (!areas.length) return;
    setBusy(true);
    try {
      const label = query.trim() || [...presets].join(", ");
      await markRedactions(docId, areas, style, `Search: ${label}`.slice(0, 200));
      await refreshMarks(docId);
      onDocumentChanged();
      toast(`Marked ${selected.size} match${selected.size === 1 ? "" : "es"} for redaction`, "success");
      setMatches([]); setSelected(new Set()); setSearched(false);
    } catch (e) { err(e); }
    setBusy(false);
  };

  const removeMark = async (page: number, xref: number) => {
    try { await deleteRedactMark(docId, page, xref); await refreshMarks(docId); onDocumentChanged(); } catch (e) { err(e); }
  };

  const clearAll = async () => {
    try { await clearRedactMarks(docId); await refreshMarks(docId); onDocumentChanged(); } catch (e) { err(e); }
  };

  const doApply = async () => {
    setBusy(true);
    try {
      const r = await applyRedactions(docId, { images, graphics });
      setConfirmOpen(false);
      await refreshMarks(docId);
      onDocumentChanged();
      if (r.verified) {
        toast(`Redacted ${r.applied} area${r.applied === 1 ? "" : "s"} — verified, ${r.removed_chars} characters removed`, "success");
      } else {
        toast(`Redaction applied but ${r.leftover_chars} character(s) still detected in marked areas — review the page`, "error");
      }
    } catch (e) { err(e); setConfirmOpen(false); }
    setBusy(false);
  };

  const markPages = useMemo(() => new Set(marks.map((m) => m.page)).size, [marks]);
  const allSelected = matches.length > 0 && selected.size === matches.length;

  return (
    <>
      {/* Tools */}
      <section className="space-y-2">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">Mark for redaction</h4>
        <div className="grid grid-cols-2 gap-1.5">
          <button
            onClick={() => { setTool("area"); setActive(!(active && tool === "area")); }}
            aria-pressed={active && tool === "area"}
            className={`${btn} flex items-center justify-center gap-1 border ${active && tool === "area" ? "border-red-600 bg-red-600 text-white" : "border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800"}`}
          >
            <MousePointerSquareDashed className="w-3.5 h-3.5" /> Drag area
          </button>
          <button
            onClick={() => { setTool("word"); setActive(!(active && tool === "word")); }}
            aria-pressed={active && tool === "word"}
            className={`${btn} flex items-center justify-center gap-1 border ${active && tool === "word" ? "border-red-600 bg-red-600 text-white" : "border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800"}`}
          >
            <Type className="w-3.5 h-3.5" /> Click words
          </button>
        </div>
        {active && (
          <p className="text-[11px] text-gray-500">
            {tool === "area" ? "Drag on the page to mark a rectangle." : "Click a word, or drag across words, to mark them."} Marks appear as red boxes.
          </p>
        )}
        <div className="flex items-center gap-2 text-xs">
          <label className="flex items-center gap-1">
            Fill
            <input type="color" value={style.fill_color} onChange={(e) => setStyle({ fill_color: e.target.value })} className="h-6 w-8 cursor-pointer rounded border border-gray-200 dark:border-gray-700 bg-transparent" />
          </label>
          <input
            value={style.overlay_text}
            onChange={(e) => setStyle({ overlay_text: e.target.value })}
            placeholder="Overlay text (e.g. REDACTED)"
            className={`${input} text-xs`}
          />
          <input type="color" aria-label="Overlay text colour" value={style.overlay_text_color} onChange={(e) => setStyle({ overlay_text_color: e.target.value })} className="h-6 w-8 shrink-0 cursor-pointer rounded border border-gray-200 dark:border-gray-700 bg-transparent" />
        </div>
      </section>

      {/* Search & redact */}
      <section className="space-y-2">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">Search &amp; redact</h4>
        <div className="flex gap-1">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && runSearch()}
            placeholder={mode === "regex" ? "Regular expression" : "Find text (all pages)"}
            className={input}
          />
          <button onClick={runSearch} disabled={busy || (!query.trim() && presets.size === 0)} className={`${btn} bg-gray-900 text-white dark:bg-gray-100 dark:text-gray-900`} aria-label="Search">
            <FileSearch className="w-4 h-4" />
          </button>
        </div>
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-gray-600 dark:text-gray-400">
          <label className="flex items-center gap-1"><input type="checkbox" checked={mode === "regex"} onChange={(e) => setMode(e.target.checked ? "regex" : "text")} /> Regex</label>
          <label className="flex items-center gap-1"><input type="checkbox" checked={caseSensitive} onChange={(e) => setCaseSensitive(e.target.checked)} /> Match case</label>
          <label className="flex items-center gap-1"><input type="checkbox" checked={wholeWord} onChange={(e) => setWholeWord(e.target.checked)} /> Whole word</label>
        </div>
        <div className="flex flex-wrap gap-1">
          {REDACT_PRESETS.map((p) => (
            <button
              key={p.id}
              onClick={() => togglePreset(p.id)}
              aria-pressed={presets.has(p.id)}
              className={`rounded-full border px-2 py-0.5 text-[11px] ${presets.has(p.id) ? "border-red-600 bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-300" : "border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800"}`}
            >
              {p.label}
            </button>
          ))}
        </div>
        {searched && (
          <div className="rounded-lg border border-gray-200 dark:border-gray-700">
            <div className="flex items-center justify-between border-b border-gray-200 dark:border-gray-700 px-2 py-1.5 text-xs">
              <label className="flex items-center gap-1.5">
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={(e) => setSelected(e.target.checked ? new Set(matches.map((m) => m.id)) : new Set())}
                />
                {matches.length} match{matches.length === 1 ? "" : "es"}{truncated ? " (truncated)" : ""}
              </label>
              <button onClick={markSelected} disabled={busy || selected.size === 0} className={`${btn} bg-red-600 text-white hover:bg-red-700`}>
                Mark {selected.size}
              </button>
            </div>
            <ul className="max-h-56 overflow-y-auto divide-y divide-gray-100 dark:divide-gray-800">
              {matches.map((m) => (
                <li key={m.id} className="flex items-start gap-2 px-2 py-1.5 text-xs">
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={selected.has(m.id)}
                    onChange={() => setSelected((s) => { const n = new Set(s); if (n.has(m.id)) n.delete(m.id); else n.add(m.id); return n; })}
                  />
                  <button className="flex-1 text-left" onClick={() => onNavigate?.(m.page)}>
                    <div className="font-mono font-medium break-all">{m.text}</div>
                    <div className="text-[10px] text-gray-500">p.{m.page + 1} · {m.kind} · …{m.context}…</div>
                  </button>
                </li>
              ))}
              {matches.length === 0 && <li className="px-2 py-2 text-xs text-gray-500">No matches.</li>}
            </ul>
          </div>
        )}
      </section>

      {/* Pending marks + apply */}
      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">Marked ({marks.length})</h4>
          {marks.length > 0 && (
            <button onClick={clearAll} className={`${subtle} text-gray-500`}>Clear all</button>
          )}
        </div>
        {marks.length === 0 ? (
          <p className="text-xs text-gray-500">No areas marked yet. Marks are stored in the PDF until applied.</p>
        ) : (
          <ul className="max-h-40 overflow-y-auto space-y-1">
            {marks.map((m) => (
              <li key={m.xref} className={`flex items-center gap-2 rounded-md px-2 py-1 text-xs ${m.page === currentPage ? "bg-red-50 dark:bg-red-900/20" : "bg-gray-50 dark:bg-gray-800"}`}>
                <span className="h-3 w-3 shrink-0 rounded-sm border border-red-600" style={{ background: m.fill_color ?? "#000" }} />
                <button className="flex-1 truncate text-left" onClick={() => onNavigate?.(m.page)}>
                  p.{m.page + 1} — {m.label || "Area"}{m.overlay_text ? ` · "${m.overlay_text}"` : ""}
                </button>
                <button onClick={() => removeMark(m.page, m.xref)} aria-label="Remove mark" className="p-0.5 rounded hover:bg-gray-200 dark:hover:bg-gray-700">
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </li>
            ))}
          </ul>
        )}
        <details className="text-xs text-gray-600 dark:text-gray-400">
          <summary className="cursor-pointer select-none">Advanced</summary>
          <div className="mt-2 space-y-1.5">
            <label className="flex items-center justify-between gap-2">Images under marks
              <select value={images} onChange={(e) => setImages(e.target.value as typeof images)} className="rounded border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-1 py-0.5">
                <option value="pixels">Blank covered pixels</option>
                <option value="remove">Remove whole image</option>
                <option value="none">Keep images</option>
              </select>
            </label>
            <label className="flex items-center justify-between gap-2">Vector graphics
              <select value={graphics} onChange={(e) => setGraphics(e.target.value as typeof graphics)} className="rounded border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-1 py-0.5">
                <option value="covered">Remove if fully covered</option>
                <option value="touched">Remove if touched</option>
                <option value="none">Keep</option>
              </select>
            </label>
          </div>
        </details>
        <button
          onClick={() => setConfirmOpen(true)}
          disabled={busy || marks.length === 0}
          className="w-full rounded-lg bg-red-600 py-2 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-40"
        >
          Apply redactions…
        </button>
      </section>

      <RedactConfirmDialog
        open={confirmOpen}
        markCount={marks.length}
        pageCount={markPages}
        busy={busy}
        onCancel={() => setConfirmOpen(false)}
        onConfirm={doApply}
      />
    </>
  );
}

// ─── Sanitize tab ────────────────────────────────────────────────────────────

const SANITIZE_LABELS: [keyof SanitizeOptions, string][] = [
  ["metadata", "Document properties (author, title…)"],
  ["xmp_metadata", "XMP metadata"],
  ["embedded_files", "Attachments / embedded files"],
  ["javascript", "JavaScript & actions"],
  ["hidden_text", "Invisible text (incl. OCR text layer)"],
  ["white_text", "White, transparent, tiny or off-page text"],
  ["annotations", "Comments & markup"],
  ["form_data", "Form field values"],
  ["links", "Links"],
  ["thumbnails", "Page thumbnails"],
];

function auditRows(a: SecurityAudit): [string, string | number, boolean][] {
  return [
    ["Metadata fields", Object.keys(a.metadata ?? {}).length, Object.keys(a.metadata ?? {}).length > 0],
    ["XMP metadata", a.has_xmp_metadata ? "yes" : "no", !!a.has_xmp_metadata],
    ["Embedded files", a.embedded_files?.length ?? 0, (a.embedded_files?.length ?? 0) > 0],
    ["JavaScript objects", a.javascript_objects ?? 0, (a.javascript_objects ?? 0) > 0],
    ["Hidden text spans", a.hidden_text_count ?? 0, (a.hidden_text_count ?? 0) > 0],
    ["Comments / markup", a.annotation_count ?? 0, (a.annotation_count ?? 0) > 0],
    ["Links", a.links ?? 0, (a.links ?? 0) > 0],
    ["Filled form fields", `${a.form_fields_filled ?? 0}/${a.form_fields ?? 0}`, (a.form_fields_filled ?? 0) > 0],
    ["Pending redaction marks", a.pending_redactions ?? 0, (a.pending_redactions ?? 0) > 0],
  ];
}

function SanitizeTab({ docId, onDocumentChanged, toast, err }: TabProps) {
  const [audit, setAudit] = useState<SecurityAudit | null>(null);
  const [opts, setOpts] = useState<SanitizeOptions>(DEFAULT_SANITIZE);
  const [busy, setBusy] = useState(false);
  const [actions, setActions] = useState<string[]>([]);

  useEffect(() => {
    getSecurityAudit(docId).then(setAudit, err);
  }, [docId, err]);

  const run = async () => {
    setBusy(true);
    try {
      const r = await sanitizeDocument(docId, opts);
      setAudit(r.after);
      setActions(r.actions);
      onDocumentChanged();
      toast("Document sanitized", "success");
    } catch (e) { err(e); }
    setBusy(false);
  };

  return (
    <div className="space-y-4">
      <section className="space-y-1.5">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">Hidden information found</h4>
        {!audit ? (
          <p className="text-xs text-gray-500">Scanning…</p>
        ) : audit.needs_password ? (
          <p className="text-xs text-amber-600">Document is password-protected. Unlock it in the Protect tab first.</p>
        ) : (
          <table className="w-full text-xs">
            <tbody>
              {auditRows(audit).map(([k, v, warn]) => (
                <tr key={k} className="border-b border-gray-100 dark:border-gray-800">
                  <td className="py-1">{k}</td>
                  <td className={`py-1 text-right font-medium ${warn ? "text-amber-600 dark:text-amber-400" : "text-gray-500"}`}>{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {audit?.hidden_text && audit.hidden_text.length > 0 && (
          <details className="text-[11px] text-gray-600 dark:text-gray-400">
            <summary className="cursor-pointer">Show hidden text</summary>
            <ul className="mt-1 max-h-32 overflow-y-auto space-y-0.5">
              {audit.hidden_text.map((h, i) => (
                <li key={i}><span className="font-medium">p.{h.page + 1} [{h.reason}]</span> {h.text}</li>
              ))}
            </ul>
          </details>
        )}
      </section>
      <section className="space-y-1.5">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">Remove</h4>
        {SANITIZE_LABELS.map(([k, label]) => (
          <label key={k} className="flex items-center gap-2 text-xs">
            <input type="checkbox" checked={opts[k]} onChange={(e) => setOpts((o) => ({ ...o, [k]: e.target.checked }))} />
            {label}
          </label>
        ))}
        <p className="text-[11px] text-gray-500">Pending redaction marks are kept. Removing invisible text also removes the searchable layer of scanned/OCR&apos;d pages.</p>
        <button onClick={run} disabled={busy || !audit || audit.needs_password} className="w-full rounded-lg bg-gray-900 py-2 text-sm font-semibold text-white dark:bg-gray-100 dark:text-gray-900 disabled:opacity-40">
          {busy ? "Sanitizing…" : "Sanitize document"}
        </button>
        {actions.length > 0 && (
          <ul className="list-disc pl-4 text-[11px] text-green-700 dark:text-green-400">
            {actions.map((a) => <li key={a}>{a}</li>)}
          </ul>
        )}
      </section>
    </div>
  );
}

// ─── Protect tab ─────────────────────────────────────────────────────────────

const PERMISSION_LABELS: [Permission, string][] = [
  ["print", "Printing"],
  ["copy", "Copying text & images"],
  ["modify", "Editing content"],
  ["annotate", "Commenting"],
  ["fill_forms", "Filling forms"],
  ["assemble", "Inserting / rotating pages"],
];

function ProtectTab({ docId, onDocumentChanged, toast, err }: TabProps) {
  const [userPw, setUserPw] = useState("");
  const [ownerPw, setOwnerPw] = useState("");
  const [perms, setPerms] = useState<Record<Permission, boolean>>({
    print: true, copy: false, modify: false, annotate: false, fill_forms: true, assemble: false,
  });
  const [unlockPw, setUnlockPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [audit, setAudit] = useState<SecurityAudit | null>(null);

  const load = useCallback(async () => { try { setAudit(await getSecurityAudit(docId)); } catch { /* ignore */ } }, [docId]);
  useEffect(() => {
    getSecurityAudit(docId).then(setAudit, () => { /* ignore */ });
  }, [docId]);

  const protect = async (applyToDocument: boolean) => {
    if (!ownerPw) { toast("Set a permissions password", "error"); return; }
    setBusy(true);
    try {
      const r = await protectDocument(docId, {
        user_password: userPw, owner_password: ownerPw, permissions: permissionsFrom(perms), apply_to_document: applyToDocument,
      });
      if (r instanceof Blob) {
        const url = URL.createObjectURL(r);
        const a = document.createElement("a");
        a.href = url;
        a.download = "protected.pdf";
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        toast("Protected copy downloaded (AES-256)", "success");
      } else {
        onDocumentChanged();
        await load();
        toast("Restrictions applied to this document (AES-256)", "success");
      }
    } catch (e) { err(e); }
    setBusy(false);
  };

  const unlock = async () => {
    setBusy(true);
    try {
      await unlockDocument(docId, unlockPw);
      setUnlockPw("");
      onDocumentChanged();
      await load();
      toast("Security removed", "success");
    } catch (e) { err(e); }
    setBusy(false);
  };

  return (
    <div className="space-y-4">
      {audit?.encrypted && (
        <section className="space-y-2 rounded-lg border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 p-2">
          <p className="text-xs">This document is encrypted{audit.encryption ? ` (${audit.encryption})` : ""}.</p>
          <div className="flex gap-1">
            <input type="password" value={unlockPw} onChange={(e) => setUnlockPw(e.target.value)} placeholder="Permissions password" className={input} />
            <button onClick={unlock} disabled={busy || !unlockPw} className={`${btn} flex items-center gap-1 bg-gray-900 text-white dark:bg-gray-100 dark:text-gray-900`}>
              <Unlock className="w-3.5 h-3.5" /> Remove
            </button>
          </div>
        </section>
      )}
      <section className="space-y-2">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">Password protect (AES-256)</h4>
        <label className="block text-xs">Open password <span className="text-gray-400">(optional)</span>
          <input type="password" value={userPw} onChange={(e) => setUserPw(e.target.value)} className={`${input} mt-1`} autoComplete="new-password" />
        </label>
        <label className="block text-xs">Permissions password
          <input type="password" value={ownerPw} onChange={(e) => setOwnerPw(e.target.value)} className={`${input} mt-1`} autoComplete="new-password" />
        </label>
        <div className="space-y-1">
          <p className="text-xs text-gray-500">Allow:</p>
          {PERMISSION_LABELS.map(([k, label]) => (
            <label key={k} className="flex items-center gap-2 text-xs">
              <input type="checkbox" checked={perms[k]} onChange={(e) => setPerms((p) => ({ ...p, [k]: e.target.checked }))} />
              {label}
            </label>
          ))}
        </div>
        <button onClick={() => protect(false)} disabled={busy || !ownerPw} className="w-full rounded-lg bg-red-600 py-2 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-40">
          Download protected PDF
        </button>
        <button
          onClick={() => protect(true)}
          disabled={busy || !ownerPw || !!userPw}
          title={userPw ? "An open password can only be applied to a downloaded copy" : undefined}
          className={`${subtle} w-full border border-gray-200 dark:border-gray-700`}
        >
          Apply restrictions to this document
        </button>
        <p className="text-[11px] text-gray-500">The downloaded copy is encrypted; your working copy stays editable.</p>
      </section>
    </div>
  );
}
