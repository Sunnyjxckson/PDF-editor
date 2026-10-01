"use client";

/**
 * FormsPanel — fill and author PDF forms (Acrobat "Fill & Sign" + "Prepare Form").
 *
 * Props:
 *   docId             current document id
 *   currentPage       0-based page shown in the viewer
 *   onDocumentChanged call after the PDF changed on the server (re-render page + thumbnails)
 *   refreshKey?       change it (e.g. the store's pageVersion) to force a reload of the field list,
 *                     e.g. after undo/redo done elsewhere
 *   onNavigatePage?   jump the viewer to a page (used when clicking a field on another page)
 *   onClose?          close button handler; omitted => no close button
 *
 * Mount: as a right-hand side panel next to <PageViewer/> (like ChatPanel when pinned).
 * Pair it with <FormsOverlay/> inside the page wrapper so fields can be drawn and edited on the page.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  X, FormInput, PenLine, Wand2, Download, Upload, Layers, Trash2, Type, CheckSquare,
  CircleDot, ChevronDownSquare, List, Signature, Loader2, AlertCircle, Save,
} from "lucide-react";
import {
  useFormsStore, fillFormFields, updateFormField, deleteFormField, flattenForm, detectFormFields,
  getFormDataExportUrl, importFormData, formatFromFilename, groupFieldsByName, missingRequired,
  summarizeDetected, toPdfDate, listSelection,
  type FormField, type FieldValue, type CreatableFieldType, type UpdateFieldInput,
} from "@/lib/features/forms";

export interface FormsPanelProps {
  docId: string;
  currentPage: number;
  onDocumentChanged: () => void;
  refreshKey?: number;
  onNavigatePage?: (page: number) => void;
  onClose?: () => void;
}

export const FIELD_TYPE_META: { type: CreatableFieldType; label: string; icon: typeof Type }[] = [
  { type: "text", label: "Text", icon: Type },
  { type: "checkbox", label: "Checkbox", icon: CheckSquare },
  { type: "radio", label: "Radio", icon: CircleDot },
  { type: "combo", label: "Dropdown", icon: ChevronDownSquare },
  { type: "list", label: "List box", icon: List },
  { type: "signature", label: "Signature", icon: Signature },
];

const inputCls =
  "w-full px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50";
const btnCls =
  "inline-flex items-center gap-1.5 px-2 py-1.5 rounded-lg text-xs font-medium transition-colors disabled:opacity-50";

export default function FormsPanel({ docId, currentPage, onDocumentChanged, refreshKey, onNavigatePage, onClose }: FormsPanelProps) {
  const {
    fields, loading, error, prepareMode, setPrepareMode, createType, setCreateType,
    selectedId, select, refresh,
  } = useFormsStore();
  const [scope, setScope] = useState<"page" | "all">("page");
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    refresh(docId);
  }, [docId, refreshKey, refresh]);

  const visible = useMemo(
    () => (scope === "page" ? fields.filter((f) => f.page === currentPage) : fields).slice().sort(
      (a, b) => a.page - b.page || a.rect[1] - b.rect[1] || a.rect[0] - b.rect[0],
    ),
    [fields, scope, currentPage],
  );
  const groups = useMemo(() => groupFieldsByName(visible), [visible]);
  const missing = useMemo(() => missingRequired(fields), [fields]);
  const selected = fields.find((f) => f.id === selectedId) ?? null;

  const run = async (label: string, fn: () => Promise<string | void>) => {
    setBusy(label);
    setMessage(null);
    try {
      const msg = await fn();
      await refresh(docId);
      onDocumentChanged();
      if (msg) setMessage({ kind: "ok", text: msg });
    } catch (e) {
      setMessage({ kind: "err", text: e instanceof Error ? e.message : String(e) });
      await refresh(docId);
    } finally {
      setBusy(null);
    }
  };

  const commitValue = (name: string, value: FieldValue) =>
    run(`fill:${name}`, async () => {
      const res = await fillFormFields(docId, { [name]: value });
      if (res.errors[name]) throw new Error(`${name}: ${res.errors[name]}`);
    });

  const onImportFile = async (file: File) => {
    const fmt = formatFromFilename(file.name);
    if (!fmt) {
      setMessage({ kind: "err", text: "Choose a .json, .fdf or .xfdf file" });
      return;
    }
    const text = await file.text();
    await run("import", async () => {
      const res = await importFormData(docId, fmt, text);
      const errs = Object.keys(res.errors).length;
      return `Imported ${res.filled.length} field${res.filled.length === 1 ? "" : "s"}${errs ? `, ${errs} skipped` : ""}`;
    });
  };

  return (
    <div className="w-full sm:w-80 h-full flex flex-col bg-white dark:bg-gray-900 border-l border-gray-200 dark:border-gray-700 text-gray-900 dark:text-gray-100">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-200 dark:border-gray-700">
        <div className="flex items-center gap-1.5">
          <FormInput className="w-4 h-4 text-blue-500" />
          <h3 className="text-sm font-semibold">Forms</h3>
          <span className="text-[10px] text-gray-500">{fields.length} widget{fields.length === 1 ? "" : "s"}</span>
          {loading && <Loader2 className="w-3 h-3 animate-spin text-gray-400" />}
        </div>
        {onClose && (
          <button onClick={onClose} aria-label="Close forms panel" className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800">
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      {/* Mode switch */}
      <div className="flex gap-1 p-2 border-b border-gray-200 dark:border-gray-700" role="tablist">
        {([
          { on: false, label: "Fill", icon: PenLine },
          { on: true, label: "Prepare form", icon: Layers },
        ] as const).map((m) => (
          <button
            key={m.label}
            role="tab"
            aria-selected={prepareMode === m.on}
            onClick={() => setPrepareMode(m.on)}
            className={`${btnCls} flex-1 justify-center ${
              prepareMode === m.on
                ? "bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300"
                : "text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800"
            }`}
          >
            <m.icon className="w-3.5 h-3.5" /> {m.label}
          </button>
        ))}
      </div>

      {message && (
        <div
          role={message.kind === "err" ? "alert" : "status"}
          className={`mx-2 mt-2 px-2 py-1.5 rounded text-xs flex items-start gap-1.5 ${
            message.kind === "err"
              ? "bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-300"
              : "bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300"
          }`}
        >
          {message.kind === "err" && <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-px" />}
          <span className="flex-1">{message.text}</span>
          <button onClick={() => setMessage(null)} aria-label="Dismiss" className="opacity-60 hover:opacity-100"><X className="w-3 h-3" /></button>
        </div>
      )}
      {error && <div className="mx-2 mt-2 text-xs text-red-600 dark:text-red-400">{error}</div>}

      <div className="flex-1 overflow-y-auto p-2 space-y-3">
        {prepareMode ? (
          <PrepareSection
            docId={docId}
            currentPage={currentPage}
            createType={createType}
            setCreateType={setCreateType}
            selected={selected}
            busy={busy}
            run={run}
            onDeselect={() => select(null)}
          />
        ) : (
          <>
            <div className="flex items-center justify-between text-xs">
              <div className="flex gap-1">
                {(["page", "all"] as const).map((s) => (
                  <button
                    key={s}
                    onClick={() => setScope(s)}
                    className={`px-2 py-0.5 rounded ${scope === s ? "bg-gray-200 dark:bg-gray-700" : "text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"}`}
                  >
                    {s === "page" ? `Page ${currentPage + 1}` : "All pages"}
                  </button>
                ))}
              </div>
              {missing.length > 0 && (
                <span className="text-red-600 dark:text-red-400" title={missing.join(", ")}>
                  {missing.length} required empty
                </span>
              )}
            </div>

            {groups.length === 0 && !loading && (
              <div className="text-xs text-gray-500 space-y-2 py-4 text-center">
                <p>No form fields {scope === "page" ? "on this page" : "in this document"}.</p>
                <button
                  onClick={() => setPrepareMode(true)}
                  className={`${btnCls} bg-purple-100 dark:bg-purple-900/40 text-purple-700 dark:text-purple-300 hover:bg-purple-200`}
                >
                  <Wand2 className="w-3.5 h-3.5" /> Prepare form / auto-detect
                </button>
              </div>
            )}

            <ul className="space-y-2">
              {groups.map((g) => (
                <li key={g.name}>
                  <FieldControl
                    group={g.widgets}
                    busy={busy === `fill:${g.name}`}
                    onCommit={(v) => commitValue(g.widgets[0].name, v)}
                    onFocusField={() => {
                      select(g.widgets[0].id);
                      if (g.widgets[0].page !== currentPage) onNavigatePage?.(g.widgets[0].page);
                    }}
                    highlighted={g.widgets.some((w) => w.id === selectedId)}
                  />
                </li>
              ))}
            </ul>
          </>
        )}
      </div>

      {/* Footer: data + flatten */}
      <div className="border-t border-gray-200 dark:border-gray-700 p-2 space-y-2">
        <div className="flex flex-wrap items-center gap-1">
          <span className="text-[10px] uppercase tracking-wide text-gray-500 mr-1">Export</span>
          {(["json", "fdf", "xfdf"] as const).map((f) => (
            <a
              key={f}
              href={getFormDataExportUrl(docId, f)}
              download={`form-data.${f}`}
              className={`${btnCls} bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700`}
            >
              <Download className="w-3 h-3" /> {f.toUpperCase()}
            </a>
          ))}
          <button
            onClick={() => fileRef.current?.click()}
            disabled={!!busy}
            className={`${btnCls} bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700`}
          >
            <Upload className="w-3 h-3" /> Import
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".json,.fdf,.xfdf,.xml"
            className="hidden"
            data-testid="forms-import-input"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) onImportFile(f);
              e.target.value = "";
            }}
          />
        </div>
        <button
          disabled={!!busy || fields.length === 0}
          onClick={() => {
            if (!window.confirm("Flatten the form? Field values become permanent page content and all fields are removed. (Undo is available.)")) return;
            run("flatten", async () => {
              const r = await flattenForm(docId);
              return `Flattened ${r.flattened} field${r.flattened === 1 ? "" : "s"}`;
            });
          }}
          className={`${btnCls} w-full justify-center bg-gray-800 dark:bg-gray-200 text-white dark:text-gray-900 hover:opacity-90`}
        >
          {busy === "flatten" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Layers className="w-3.5 h-3.5" />}
          Flatten form
        </button>
      </div>
    </div>
  );
}

// ─── Fill controls ───────────────────────────────────────────────────────────

function FieldControl({
  group, busy, onCommit, onFocusField, highlighted,
}: {
  group: FormField[];
  busy: boolean;
  onCommit: (v: FieldValue) => void;
  onFocusField: () => void;
  highlighted: boolean;
}) {
  const f = group[0];
  const [draft, setDraft] = useState<string>(typeof f.value === "string" ? f.value : "");
  // Re-sync the draft when the stored value changes (derived during render).
  const [syncedValue, setSyncedValue] = useState(f.value);
  if (syncedValue !== f.value) {
    setSyncedValue(f.value);
    setDraft(typeof f.value === "string" ? f.value : "");
  }
  const label = (
    <div className="flex items-center gap-1 text-xs font-medium mb-0.5">
      <span className="truncate" title={f.tooltip || f.name}>{f.tooltip || f.name}</span>
      {f.required && <span className="text-red-500" aria-label="required">*</span>}
      {f.readonly && <span className="text-[10px] text-gray-400">(read-only)</span>}
      <span className="ml-auto text-[10px] text-gray-400">p{f.page + 1}</span>
      {busy && <Loader2 className="w-3 h-3 animate-spin text-gray-400" />}
    </div>
  );
  const wrap = (children: React.ReactNode) => (
    <div
      className={`rounded-lg p-2 border ${highlighted ? "border-blue-500 bg-blue-50/50 dark:bg-blue-900/20" : "border-gray-200 dark:border-gray-700"}`}
      onFocus={onFocusField}
      data-field-name={f.name}
    >
      {label}
      {children}
    </div>
  );
  const disabled = f.readonly || busy;

  switch (f.type) {
    case "text":
      return wrap(
        f.multiline ? (
          <textarea
            aria-label={f.name}
            className={`${inputCls} min-h-[60px] resize-y`}
            value={draft}
            maxLength={f.max_len || undefined}
            disabled={disabled}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={() => draft !== (f.value ?? "") && onCommit(draft)}
          />
        ) : (
          <input
            aria-label={f.name}
            className={inputCls}
            value={draft}
            maxLength={f.max_len || undefined}
            disabled={disabled}
            placeholder={f.format === "date" ? "mm/dd/yyyy" : undefined}
            inputMode={f.format === "date" ? "numeric" : undefined}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={() => {
              const v = f.format === "date" ? toPdfDate(draft) : draft;
              if (v !== (f.value ?? "")) onCommit(v);
            }}
            onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
          />
        ),
      );
    case "checkbox": {
      const exports = [...new Set(group.map((g) => g.export_value))];
      if (exports.length > 1) {
        // same-name checkboxes with different exports behave like a radio group
        return wrap(
          <div className="flex flex-wrap gap-2">
            {group.map((g) => (
              <label key={g.id} className="flex items-center gap-1 text-sm">
                <input
                  type="checkbox"
                  checked={!!g.value}
                  disabled={disabled}
                  onChange={(e) => onCommit(e.target.checked ? (g.export_value ?? true) : false)}
                />
                {g.export_value}
              </label>
            ))}
          </div>,
        );
      }
      return wrap(
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            aria-label={f.name}
            checked={!!f.value}
            disabled={disabled}
            onChange={(e) => onCommit(e.target.checked)}
          />
          <span className="text-gray-500 text-xs">{f.value ? `Checked (${f.export_value})` : "Unchecked"}</span>
        </label>,
      );
    }
    case "radio":
      return wrap(
        <div className="flex flex-wrap gap-x-3 gap-y-1" role="radiogroup" aria-label={f.name}>
          {f.options.map((opt) => (
            <label key={opt} className="flex items-center gap-1 text-sm">
              <input
                type="radio"
                name={`forms-radio-${f.name}`}
                checked={f.value === opt}
                disabled={disabled}
                onChange={() => onCommit(opt)}
              />
              {opt}
            </label>
          ))}
          {f.value && (
            <button disabled={disabled} onClick={() => onCommit(null)} className="text-[10px] text-gray-500 underline">
              clear
            </button>
          )}
        </div>,
      );
    case "combo":
      return wrap(
        f.editable ? (
          <>
            <input
              aria-label={f.name}
              list={`forms-dl-${f.id}`}
              className={inputCls}
              value={draft}
              disabled={disabled}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={() => draft !== (f.value ?? "") && onCommit(draft)}
            />
            <datalist id={`forms-dl-${f.id}`}>
              {f.options.map((o, i) => <option key={o} value={o}>{f.option_labels[i]}</option>)}
            </datalist>
          </>
        ) : (
          <select aria-label={f.name} className={inputCls} value={typeof f.value === "string" ? f.value : ""} disabled={disabled}
            onChange={(e) => onCommit(e.target.value)}>
            <option value="">—</option>
            {f.options.map((o, i) => <option key={o} value={o}>{f.option_labels[i] ?? o}</option>)}
          </select>
        ),
      );
    case "list":
      if (f.multi_select) {
        const sel = listSelection(f);
        return wrap(
          <>
            <select
              aria-label={f.name}
              multiple
              size={Math.min(6, Math.max(3, f.options.length))}
              className={inputCls}
              value={sel}
              disabled={disabled}
              onChange={(e) => onCommit(Array.from(e.target.selectedOptions, (o) => o.value))}
            >
              {f.options.map((o, i) => <option key={o} value={o}>{f.option_labels[i] ?? o}</option>)}
            </select>
            <p className="text-[10px] text-gray-400 mt-0.5">
              {sel.length ? `${sel.length} selected` : "None selected"} · Ctrl/⌘-click to pick several
            </p>
          </>,
        );
      }
      return wrap(
        <select
          aria-label={f.name}
          size={Math.min(5, Math.max(2, f.options.length))}
          className={inputCls}
          value={typeof f.value === "string" ? f.value : ""}
          disabled={disabled}
          onChange={(e) => onCommit(e.target.value)}
        >
          {f.options.map((o, i) => <option key={o} value={o}>{f.option_labels[i] ?? o}</option>)}
        </select>,
      );
    case "signature":
      return wrap(
        <div className="text-xs text-gray-500 flex items-center gap-1">
          <Signature className="w-3.5 h-3.5" /> {f.value ? "Signed" : "Signature field — sign it with the signature tool"}
        </div>,
      );
    default:
      return wrap(<div className="text-xs text-gray-500">Unsupported field type: {f.type}</div>);
  }
}

// ─── Prepare (authoring) ─────────────────────────────────────────────────────

function PrepareSection({
  docId, currentPage, createType, setCreateType, selected, busy, run, onDeselect,
}: {
  docId: string;
  currentPage: number;
  createType: CreatableFieldType;
  setCreateType: (t: CreatableFieldType) => void;
  selected: FormField | null;
  busy: string | null;
  run: (label: string, fn: () => Promise<string | void>) => Promise<void>;
  onDeselect: () => void;
}) {
  const [detectScope, setDetectScope] = useState<"page" | "all">("page");
  return (
    <>
      <section>
        <h4 className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">Add field</h4>
        <div className="grid grid-cols-3 gap-1">
          {FIELD_TYPE_META.map((t) => (
            <button
              key={t.type}
              onClick={() => setCreateType(t.type)}
              aria-pressed={createType === t.type}
              className={`${btnCls} justify-center flex-col py-2 ${
                createType === t.type
                  ? "bg-purple-100 dark:bg-purple-900/40 text-purple-700 dark:text-purple-300 ring-1 ring-purple-400"
                  : "hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-700 dark:text-gray-300"
              }`}
            >
              <t.icon className="w-4 h-4" />
              {t.label}
            </button>
          ))}
        </div>
        <p className="text-[11px] text-gray-500 mt-1">
          Drag on the page to draw a {createType} field (click for default size). Click a field to select it; drag to move,
          use the corner handles to resize, Delete to remove.
        </p>
      </section>

      <section className="rounded-lg border border-purple-200 dark:border-purple-800 p-2 space-y-1.5">
        <div className="flex items-center justify-between">
          <h4 className="text-xs font-semibold flex items-center gap-1"><Wand2 className="w-3.5 h-3.5 text-purple-500" /> Auto-detect fields</h4>
          <select
            aria-label="Auto-detect scope"
            value={detectScope}
            onChange={(e) => setDetectScope(e.target.value as "page" | "all")}
            className="text-xs bg-transparent border border-gray-300 dark:border-gray-600 rounded px-1"
          >
            <option value="page">This page</option>
            <option value="all">All pages</option>
          </select>
        </div>
        <p className="text-[11px] text-gray-500">
          Finds blanks (____), empty boxes, signature lines, “Label:” gaps and checkbox squares, and turns them into fields.
          Option boxes that share a question become radio groups; “Date”/“DOB” blanks get a date format. Scanned pages are read with OCR.
        </p>
        <button
          disabled={!!busy}
          onClick={() =>
            run("detect", async () => {
              const r = await detectFormFields(docId, detectScope === "page" ? { pages: [currentPage] } : {});
              return summarizeDetected(r.created);
            })
          }
          className={`${btnCls} w-full justify-center bg-purple-600 text-white hover:bg-purple-700`}
        >
          {busy === "detect" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Wand2 className="w-3.5 h-3.5" />}
          Auto-detect fields
        </button>
      </section>

      {selected ? (
        <FieldProperties key={selected.id} docId={docId} field={selected} busy={busy} run={run} onDeselect={onDeselect} />
      ) : (
        <p className="text-xs text-gray-500 text-center py-2">Select a field on the page to edit its properties.</p>
      )}
    </>
  );
}

function FieldProperties({
  docId, field, busy, run, onDeselect,
}: {
  docId: string;
  field: FormField;
  busy: string | null;
  run: (label: string, fn: () => Promise<string | void>) => Promise<void>;
  onDeselect: () => void;
}) {
  const [name, setName] = useState(field.name);
  const [tooltip, setTooltip] = useState(field.tooltip);
  const [required, setRequired] = useState(field.required);
  const [readonly, setReadonly] = useState(field.readonly);
  const [fontSize, setFontSize] = useState(String(field.font_size || 0));
  const [multiline, setMultiline] = useState(field.multiline);
  const [maxLen, setMaxLen] = useState(String(field.max_len || 0));
  const [options, setOptions] = useState(field.options.join("\n"));
  const [exportValue, setExportValue] = useState(field.export_value ?? "");
  const [editable, setEditable] = useState(!!field.editable);
  const [multiSelect, setMultiSelect] = useState(!!field.multi_select);
  const [isDate, setIsDate] = useState(field.format === "date");
  const hasText = field.type === "text" || field.type === "combo" || field.type === "list";

  const save = () => {
    const patch: UpdateFieldInput = {};
    if (name.trim() !== field.name) patch.name = name.trim();
    if (tooltip !== field.tooltip) patch.tooltip = tooltip;
    if (required !== field.required) patch.required = required;
    if (readonly !== field.readonly) patch.readonly = readonly;
    if (hasText && Number(fontSize) !== field.font_size) patch.font_size = Math.max(0, Number(fontSize) || 0);
    if (field.type === "text" && multiline !== field.multiline) patch.multiline = multiline;
    if (field.type === "text" && Number(maxLen) !== field.max_len) patch.max_len = Math.max(0, Number(maxLen) || 0);
    if ((field.type === "combo" || field.type === "list")) {
      const opts = options.split("\n").map((s) => s.trim()).filter(Boolean);
      if (opts.join("\n") !== field.options.join("\n")) patch.options = opts;
      if (field.type === "combo" && editable !== !!field.editable) patch.editable = editable;
      if (field.type === "list" && multiSelect !== !!field.multi_select) patch.multi_select = multiSelect;
    }
    if (field.type === "text" && isDate !== (field.format === "date")) patch.format = isDate ? "date" : "";
    if ((field.type === "checkbox" || field.type === "radio") && exportValue.trim() && exportValue.trim() !== field.export_value) {
      patch.export_value = exportValue.trim();
    }
    if (Object.keys(patch).length === 0) return;
    run("props", async () => {
      await updateFormField(docId, field.id, patch);
      return "Field updated";
    });
  };

  const row = "flex items-center justify-between gap-2 text-xs";
  return (
    <section className="rounded-lg border border-gray-200 dark:border-gray-700 p-2 space-y-2">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold">Field properties <span className="font-normal text-gray-500">({field.type})</span></h4>
        <button onClick={onDeselect} aria-label="Deselect" className="p-0.5 rounded hover:bg-gray-100 dark:hover:bg-gray-800"><X className="w-3.5 h-3.5" /></button>
      </div>
      <label className="block text-xs">Name
        <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="block text-xs">Tooltip / label
        <input className={inputCls} value={tooltip} onChange={(e) => setTooltip(e.target.value)} />
      </label>
      {(field.type === "checkbox" || field.type === "radio") && (
        <label className="block text-xs">Export value (“on” state)
          <input className={inputCls} value={exportValue} onChange={(e) => setExportValue(e.target.value)} />
        </label>
      )}
      {(field.type === "combo" || field.type === "list") && (
        <label className="block text-xs">Options (one per line)
          <textarea className={`${inputCls} min-h-[70px]`} value={options} onChange={(e) => setOptions(e.target.value)} />
        </label>
      )}
      {hasText && (
        <label className={row}>Font size (0 = auto)
          <input type="number" min={0} max={72} className={`${inputCls} w-20`} value={fontSize} onChange={(e) => setFontSize(e.target.value)} />
        </label>
      )}
      {field.type === "text" && (
        <>
          <label className={row}>Max length (0 = none)
            <input type="number" min={0} className={`${inputCls} w-20`} value={maxLen} onChange={(e) => setMaxLen(e.target.value)} />
          </label>
          <label className={row}>Multi-line <input type="checkbox" checked={multiline} onChange={(e) => setMultiline(e.target.checked)} /></label>
        </>
      )}
      {field.type === "combo" && (
        <label className={row}>Allow custom text <input type="checkbox" checked={editable} onChange={(e) => setEditable(e.target.checked)} /></label>
      )}
      {field.type === "list" && (
        <label className={row}>Allow multiple selections <input type="checkbox" checked={multiSelect} onChange={(e) => setMultiSelect(e.target.checked)} /></label>
      )}
      {field.type === "text" && (
        <label className={row}>Date (mm/dd/yyyy) <input type="checkbox" checked={isDate} onChange={(e) => setIsDate(e.target.checked)} /></label>
      )}
      <label className={row}>Required <input type="checkbox" checked={required} onChange={(e) => setRequired(e.target.checked)} /></label>
      <label className={row}>Read-only <input type="checkbox" checked={readonly} onChange={(e) => setReadonly(e.target.checked)} /></label>
      <div className="text-[10px] text-gray-400">
        Page {field.page + 1} · [{field.rect.map((v) => Math.round(v)).join(", ")}] pt
      </div>
      <div className="flex gap-1">
        <button disabled={!!busy} onClick={save} className={`${btnCls} flex-1 justify-center bg-blue-600 text-white hover:bg-blue-700`}>
          {busy === "props" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />} Apply
        </button>
        <button
          disabled={!!busy}
          onClick={() =>
            run("delete", async () => {
              await deleteFormField(docId, field.id);
              onDeselect();
              return `Deleted ${field.name}`;
            })
          }
          className={`${btnCls} justify-center bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-300 hover:bg-red-100`}
          aria-label="Delete field"
        >
          <Trash2 className="w-3.5 h-3.5" /> Delete
        </button>
      </div>
    </section>
  );
}
