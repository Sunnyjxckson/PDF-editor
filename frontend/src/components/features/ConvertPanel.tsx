"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  X,
  ScanText,
  Download,
  FilePlus2,
  Minimize2,
  FileText,
  FileType,
  FileCode,
  FileSpreadsheet,
  Image as ImageIcon,
  Hash,
  Upload,
  ArrowUp,
  ArrowDown,
  Trash2,
  Loader2,
  CheckCircle2,
  AlertCircle,
} from "lucide-react";
import {
  CREATE_ACCEPT,
  compressDocument,
  createPdfFromFiles,
  detectScannedPages,
  exportDocument,
  formatBytes,
  getConvertCapabilities,
  getOcrLanguageInfo,
  installOcrLanguage,
  joinOcrLanguages,
  runOcr,
  saveBlob,
  type CompressPreset,
  type CompressResult,
  type CreatePageSize,
  type CreatedDocument,
  type ExportFormat,
  type HtmlLayout,
  type OcrDetectResult,
  type OcrJob,
  type OcrMode,
} from "@/lib/features/convert";

export interface ConvertPanelProps {
  docId: string;
  /** 0-based index of the page currently shown. */
  currentPage: number;
  /** Called after OCR or compression rewrote the document on the server. */
  onDocumentChanged: () => void;
  /** Called when "Create PDF from files" produced a new document (open it). */
  onDocumentCreated?: (doc: CreatedDocument) => void;
  /** Original filename, used to name downloads. */
  filename?: string;
  onClose?: () => void;
}

type OcrScope = "auto" | "current" | "all";

const EXPORTS: { key: string; fmt: ExportFormat; layout?: HtmlLayout; label: string; icon: typeof FileText; hint: string }[] = [
  { key: "docx", fmt: "docx", label: "Word", icon: FileType, hint: "DOCX with layout, tables, images" },
  { key: "xlsx", fmt: "xlsx", label: "Excel", icon: FileSpreadsheet, hint: "Detected tables, one sheet each" },
  { key: "md", fmt: "md", label: "Markdown", icon: Hash, hint: "Headings, lists, tables" },
  { key: "html", fmt: "html", layout: "positioned", label: "HTML", icon: FileCode, hint: "Positioned web page that looks like the PDF" },
  { key: "html-reflow", fmt: "html", layout: "reflow", label: "Web page", icon: FileCode, hint: "Reflowable HTML: headings, paragraphs, lists, tables, embedded images" },
  { key: "txt", fmt: "txt", label: "Text", icon: FileText, hint: "Plain text, pages split by form feed" },
  { key: "csv", fmt: "csv", label: "CSV", icon: FileSpreadsheet, hint: "Tables as CSV files (zip)" },
  { key: "png", fmt: "png", label: "PNG", icon: ImageIcon, hint: "One image per page (zip)" },
  { key: "jpg", fmt: "jpg", label: "JPG", icon: ImageIcon, hint: "One image per page (zip)" },
];

const PRESETS: { id: CompressPreset; label: string; desc: string }[] = [
  { id: "high", label: "High quality", desc: "300 dpi images, JPEG 90" },
  { id: "balanced", label: "Balanced", desc: "150 dpi images, JPEG 75" },
  { id: "smallest", label: "Smallest", desc: "96 dpi images, JPEG 50" },
];

const sectionCls = "border-t border-gray-200 dark:border-gray-700 pt-3 mt-3";
const labelCls = "text-xs font-medium text-gray-500 dark:text-gray-400";
const selectCls =
  "w-full text-sm rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-2 py-1.5";
const primaryBtn =
  "inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50 disabled:cursor-not-allowed";
const secondaryBtn =
  "inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium border border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed";

function Message({ kind, text }: { kind: "ok" | "err"; text: string }) {
  const Icon = kind === "ok" ? CheckCircle2 : AlertCircle;
  return (
    <div
      role={kind === "err" ? "alert" : "status"}
      className={`mt-2 flex items-start gap-1.5 text-xs ${
        kind === "ok" ? "text-green-700 dark:text-green-400" : "text-red-600 dark:text-red-400"
      }`}
    >
      <Icon className="w-3.5 h-3.5 mt-0.5 shrink-0" />
      <span>{text}</span>
    </div>
  );
}

export default function ConvertPanel({
  docId,
  currentPage,
  onDocumentChanged,
  onDocumentCreated,
  filename,
  onClose,
}: ConvertPanelProps) {
  // ── OCR state
  const [languages, setLanguages] = useState<string[]>(["eng"]);
  const [langNames, setLangNames] = useState<Record<string, string>>({});
  const [installable, setInstallable] = useState<{ code: string; name: string }[]>([]);
  const [language, setLanguage] = useState("eng");
  const [language2, setLanguage2] = useState("");
  const [installCode, setInstallCode] = useState("");
  const [installing, setInstalling] = useState(false);
  const [installMsg, setInstallMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [wordAvailable, setWordAvailable] = useState(false);
  const [useWord, setUseWord] = useState(false);
  const [ocrMode, setOcrMode] = useState<OcrMode>("searchable");
  const [scope, setScope] = useState<OcrScope>("auto");
  const [detect, setDetect] = useState<OcrDetectResult | null>(null);
  const [ocrJob, setOcrJob] = useState<OcrJob | null>(null);
  const [ocrBusy, setOcrBusy] = useState(false);
  const [ocrMsg, setOcrMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  // ── Export state
  const [exporting, setExporting] = useState<string | null>(null);
  const [imageDpi, setImageDpi] = useState(150);
  const [exportMsg, setExportMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  // ── Create state
  const [files, setFiles] = useState<File[]>([]);
  const [pageSize, setPageSize] = useState<CreatePageSize>("letter");
  const [dragOver, setDragOver] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createMsg, setCreateMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  // ── Compress state
  const [preset, setPreset] = useState<CompressPreset>("balanced");
  const [compressing, setCompressing] = useState<"estimate" | "apply" | null>(null);
  const [compressRes, setCompressRes] = useState<CompressResult | null>(null);
  const [compressErr, setCompressErr] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);

  const refreshDetect = useCallback(async () => {
    try {
      setDetect(await detectScannedPages(docId));
    } catch {
      setDetect(null);
    }
  }, [docId]);

  const applyLanguageInfo = useCallback(
    (info: { languages: string[]; installable?: { code: string; name: string }[]; names?: Record<string, string> }) => {
      const l = info.languages;
      if (l.length) {
        setLanguages(l);
        setLanguage((cur) => (l.includes(cur) ? cur : l.includes("eng") ? "eng" : l[0]));
        setLanguage2((cur) => (cur && l.includes(cur) ? cur : ""));
      }
      if (info.installable) {
        setInstallable(info.installable);
        setInstallCode((cur) => (info.installable!.some((x) => x.code === cur) ? cur : ""));
      }
      if (info.names) setLangNames(info.names);
    },
    [],
  );

  useEffect(() => {
    getOcrLanguageInfo().then(applyLanguageInfo).catch(() => undefined);
    getConvertCapabilities()
      .then((c) => setWordAvailable(!!c.word))
      .catch(() => setWordAvailable(false));
  }, [applyLanguageInfo]);

  const handleInstall = async () => {
    if (!installCode) return;
    setInstalling(true);
    setInstallMsg(null);
    const name = installable.find((x) => x.code === installCode)?.name ?? installCode;
    try {
      const res = await installOcrLanguage(installCode);
      applyLanguageInfo(await getOcrLanguageInfo().catch(() => ({ languages: res.languages })));
      setLanguage2(installCode);
      setInstallMsg({
        kind: "ok",
        text: res.already_installed ? `${name} was already installed.` : `Installed ${name} (${installCode}).`,
      });
    } catch (e) {
      setInstallMsg({ kind: "err", text: e instanceof Error ? e.message : "Install failed" });
    } finally {
      setInstalling(false);
    }
  };

  const langLabel = (code: string) => (langNames[code] && langNames[code] !== code ? `${langNames[code]} (${code})` : code);

  useEffect(() => {
    refreshDetect();
    setCompressRes(null);
  }, [refreshDetect]);

  const baseName = (filename || "document").replace(/\.pdf$/i, "");

  // ── Handlers
  const handleOcr = async () => {
    setOcrBusy(true);
    setOcrMsg(null);
    setOcrJob(null);
    abortRef.current?.abort();
    const ctl = new AbortController();
    abortRef.current = ctl;
    try {
      const pages = scope === "current" ? [currentPage] : scope === "all" ? detect?.pages.map((p) => p.page) ?? null : null;
      const res = await runOcr(
        docId,
        { language: joinOcrLanguages([language, language2]), mode: ocrMode, pages, force: scope !== "auto" },
        setOcrJob,
        500,
        ctl.signal,
      );
      if (res.total === 0) {
        setOcrMsg({ kind: "ok", text: "No pages needed OCR. Pick “Current page” or “All pages” to force it." });
      } else {
        setOcrMsg({
          kind: "ok",
          text: `Recognized ${res.words_added} words on ${res.pages_processed.length} page${
            res.pages_processed.length === 1 ? "" : "s"
          }.${res.skipped.length ? ` ${res.skipped.length} page(s) had no readable text.` : ""}`,
        });
        onDocumentChanged();
      }
      refreshDetect();
    } catch (e) {
      setOcrMsg({ kind: "err", text: e instanceof Error ? e.message : "OCR failed" });
    } finally {
      setOcrBusy(false);
    }
  };

  const handleExport = async (key: string, fmt: ExportFormat, layout?: HtmlLayout) => {
    setExporting(key);
    setExportMsg(null);
    try {
      const { blob, filename: name } = await exportDocument(docId, fmt, {
        dpi: fmt === "png" || fmt === "jpg" ? imageDpi : undefined,
        filename: layout === "reflow" ? `${baseName}-reflow` : baseName,
        layout,
      });
      saveBlob(blob, name);
      setExportMsg({ kind: "ok", text: `Downloaded ${name} (${formatBytes(blob.size)})` });
    } catch (e) {
      setExportMsg({ kind: "err", text: e instanceof Error ? e.message : "Export failed" });
    } finally {
      setExporting(null);
    }
  };

  const addFiles = (list: FileList | File[] | null) => {
    if (!list) return;
    const arr = Array.from(list);
    if (arr.length) setFiles((prev) => [...prev, ...arr]);
    setCreateMsg(null);
  };

  const moveFile = (i: number, d: -1 | 1) =>
    setFiles((prev) => {
      const j = i + d;
      if (j < 0 || j >= prev.length) return prev;
      const next = [...prev];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });

  const handleCreate = async () => {
    setCreating(true);
    setCreateMsg(null);
    try {
      const hasDocx = files.some((f) => /\.docx$/i.test(f.name));
      const doc = await createPdfFromFiles(files, {
        pageSize,
        docxEngine: useWord && wordAvailable && hasDocx ? "word" : "builtin",
      });
      setCreateMsg({ kind: "ok", text: `Created ${doc.filename} (${doc.page_count} page${doc.page_count === 1 ? "" : "s"})` });
      setFiles([]);
      onDocumentCreated?.(doc);
    } catch (e) {
      setCreateMsg({ kind: "err", text: e instanceof Error ? e.message : "Could not create PDF" });
    } finally {
      setCreating(false);
    }
  };

  const handleCompress = async (dryRun: boolean) => {
    setCompressing(dryRun ? "estimate" : "apply");
    setCompressErr(null);
    try {
      const res = await compressDocument(docId, preset, { dryRun });
      setCompressRes(res);
      if (res.applied) onDocumentChanged();
    } catch (e) {
      setCompressErr(e instanceof Error ? e.message : "Compression failed");
    } finally {
      setCompressing(null);
    }
  };

  const progressPct = ocrJob ? Math.round((ocrJob.progress || 0) * 100) : 0;
  const scannedCount = detect?.scanned_pages.length ?? 0;
  const needsCount = detect?.needs_ocr.length ?? 0;

  return (
    <div
      className="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-xl shadow-2xl p-3 w-full sm:w-80 max-h-[calc(100vh-5rem)] overflow-y-auto text-gray-900 dark:text-gray-100"
      aria-label="Convert and tools"
    >
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">Convert &amp; Tools</h3>
        {onClose && (
          <button onClick={onClose} aria-label="Close" className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800">
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      {/* ── OCR ─────────────────────────────────────────────── */}
      <section className={sectionCls} aria-labelledby="convert-ocr">
        <div className="flex items-center gap-1.5 mb-1">
          <ScanText className="w-4 h-4 text-blue-500" />
          <h4 id="convert-ocr" className="text-sm font-medium">Recognize text (OCR)</h4>
        </div>
        <p className="text-xs text-gray-500 dark:text-gray-400 mb-2" data-testid="ocr-detect">
          {detect === null
            ? "Checking pages…"
            : scannedCount === 0
              ? "No scanned pages detected."
              : `${scannedCount} scanned page${scannedCount === 1 ? "" : "s"}, ${needsCount} without a text layer.`}
        </p>
        <div className="grid grid-cols-2 gap-2">
          <label className="block">
            <span className={labelCls}>Language</span>
            <select aria-label="OCR language" className={selectCls} value={language} onChange={(e) => setLanguage(e.target.value)} disabled={ocrBusy}>
              {languages.map((l) => (
                <option key={l} value={l}>{l}</option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className={labelCls}>Also recognize</span>
            <select
              aria-label="Second OCR language"
              className={selectCls}
              value={language2}
              onChange={(e) => setLanguage2(e.target.value)}
              disabled={ocrBusy || languages.length < 2}
            >
              <option value="">None</option>
              {languages.filter((l) => l !== language).map((l) => (
                <option key={l} value={l}>+ {langLabel(l)}</option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className={labelCls}>Output</span>
            <select aria-label="OCR output" className={selectCls} value={ocrMode} onChange={(e) => setOcrMode(e.target.value as OcrMode)} disabled={ocrBusy}>
              <option value="searchable">Searchable image</option>
              <option value="editable">Editable text</option>
            </select>
          </label>
          <label className="block">
            <span className={labelCls}>Pages</span>
            <select aria-label="OCR pages" className={selectCls} value={scope} onChange={(e) => setScope(e.target.value as OcrScope)} disabled={ocrBusy}>
              <option value="auto">Pages that need it</option>
              <option value="current">Current page ({currentPage + 1})</option>
              <option value="all">All pages</option>
            </select>
          </label>
        </div>
        {ocrMode === "editable" && (
          <p className="text-[11px] text-gray-500 dark:text-gray-400 mt-1" data-testid="ocr-editable-hint">
            Removes the scanned letters from the page image and replaces them with real text you can change with Edit Text.
          </p>
        )}
        <button className={`${primaryBtn} w-full mt-2`} onClick={handleOcr} disabled={ocrBusy}>
          {ocrBusy ? <Loader2 className="w-4 h-4 animate-spin" /> : <ScanText className="w-4 h-4" />}
          {ocrBusy ? "Recognizing…" : "Recognize text"}
        </button>
        {ocrBusy && (
          <div className="mt-2">
            <div className="h-1.5 rounded bg-gray-200 dark:bg-gray-700 overflow-hidden" role="progressbar" aria-valuenow={progressPct} aria-valuemin={0} aria-valuemax={100}>
              <div className="h-full bg-blue-600 transition-all" style={{ width: `${progressPct}%` }} />
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              {ocrJob?.total ? `Page ${Math.min(ocrJob.done + 1, ocrJob.total)} of ${ocrJob.total}` : "Starting…"}
            </p>
          </div>
        )}
        {ocrMsg && <Message kind={ocrMsg.kind} text={ocrMsg.text} />}
        {installable.length > 0 && (
          <details className="mt-2 text-xs" data-testid="ocr-install">
            <summary className="cursor-pointer text-gray-600 dark:text-gray-300">Add a language…</summary>
            <div className="flex items-center gap-2 mt-1.5">
              <select
                aria-label="Language to install"
                className={selectCls}
                value={installCode}
                onChange={(e) => setInstallCode(e.target.value)}
                disabled={installing}
              >
                <option value="">Choose a language</option>
                {installable.map((l) => (
                  <option key={l.code} value={l.code}>{l.name} ({l.code})</option>
                ))}
              </select>
              <button className={secondaryBtn} onClick={handleInstall} disabled={installing || !installCode}>
                {installing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                Install
              </button>
            </div>
            <p className="text-[11px] text-gray-500 dark:text-gray-400 mt-1">
              Downloads the language model (about 1–4 MB) from github.com/tesseract-ocr/tessdata_fast to the server.
            </p>
            {installMsg && <Message kind={installMsg.kind} text={installMsg.text} />}
          </details>
        )}
      </section>

      {/* ── Export ──────────────────────────────────────────── */}
      <section className={sectionCls} aria-labelledby="convert-export">
        <div className="flex items-center gap-1.5 mb-2">
          <Download className="w-4 h-4 text-blue-500" />
          <h4 id="convert-export" className="text-sm font-medium">Export to</h4>
        </div>
        <div className="grid grid-cols-4 gap-1.5">
          {EXPORTS.map(({ key, fmt, layout, label, icon: Icon, hint }) => (
            <button
              key={key}
              title={hint}
              aria-label={`Export ${label}`}
              onClick={() => handleExport(key, fmt, layout)}
              disabled={exporting !== null}
              className="flex flex-col items-center gap-1 p-2 rounded-lg border border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50 text-xs"
            >
              {exporting === key ? <Loader2 className="w-4 h-4 animate-spin" /> : <Icon className="w-4 h-4" />}
              {label}
            </button>
          ))}
        </div>
        <label className="flex items-center justify-between mt-2">
          <span className={labelCls}>Image resolution</span>
          <select aria-label="Image DPI" className="text-sm rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-2 py-1" value={imageDpi} onChange={(e) => setImageDpi(Number(e.target.value))}>
            {[72, 150, 300, 600].map((d) => (
              <option key={d} value={d}>{d} dpi</option>
            ))}
          </select>
        </label>
        {exportMsg && <Message kind={exportMsg.kind} text={exportMsg.text} />}
      </section>

      {/* ── Create ──────────────────────────────────────────── */}
      <section className={sectionCls} aria-labelledby="convert-create">
        <div className="flex items-center gap-1.5 mb-2">
          <FilePlus2 className="w-4 h-4 text-blue-500" />
          <h4 id="convert-create" className="text-sm font-medium">Create PDF from files</h4>
        </div>
        <div
          data-testid="create-dropzone"
          role="button"
          tabIndex={0}
          onClick={() => fileInput.current?.click()}
          onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && fileInput.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            addFiles(e.dataTransfer.files);
          }}
          className={`flex flex-col items-center justify-center gap-1 p-4 rounded-lg border-2 border-dashed cursor-pointer text-xs text-center ${
            dragOver
              ? "border-blue-500 bg-blue-50 dark:bg-blue-900/20"
              : "border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-800"
          }`}
        >
          <Upload className="w-5 h-5 text-gray-400" />
          <span>Drop images, text, Markdown, Word or PDF files</span>
          <span className="text-gray-400">Files are combined in the order listed</span>
        </div>
        <input
          ref={fileInput}
          type="file"
          multiple
          accept={CREATE_ACCEPT}
          className="hidden"
          data-testid="create-input"
          onChange={(e) => {
            addFiles(e.target.files);
            e.target.value = "";
          }}
        />
        {files.length > 0 && (
          <ul className="mt-2 space-y-1" aria-label="Files to combine">
            {files.map((f, i) => (
              <li key={`${f.name}-${i}`} className="flex items-center gap-1 text-xs bg-gray-50 dark:bg-gray-800 rounded px-2 py-1">
                <span className="flex-1 truncate" title={f.name}>{f.name}</span>
                <span className="text-gray-400">{formatBytes(f.size)}</span>
                <button aria-label={`Move ${f.name} up`} onClick={() => moveFile(i, -1)} disabled={i === 0} className="p-0.5 disabled:opacity-30"><ArrowUp className="w-3 h-3" /></button>
                <button aria-label={`Move ${f.name} down`} onClick={() => moveFile(i, 1)} disabled={i === files.length - 1} className="p-0.5 disabled:opacity-30"><ArrowDown className="w-3 h-3" /></button>
                <button aria-label={`Remove ${f.name}`} onClick={() => setFiles((p) => p.filter((_, j) => j !== i))} className="p-0.5 text-red-500"><Trash2 className="w-3 h-3" /></button>
              </li>
            ))}
          </ul>
        )}
        {wordAvailable && files.some((f) => /\.docx$/i.test(f.name)) && (
          <label className="flex items-center gap-2 mt-2 text-xs">
            <input type="checkbox" checked={useWord} onChange={(e) => setUseWord(e.target.checked)} />
            <span>High fidelity (uses Microsoft Word)</span>
          </label>
        )}
        <div className="flex items-center gap-2 mt-2">
          <select aria-label="Page size" className={selectCls} value={pageSize} onChange={(e) => setPageSize(e.target.value as CreatePageSize)}>
            <option value="letter">Letter</option>
            <option value="a4">A4</option>
            <option value="fit">Fit to image</option>
          </select>
          <button className={primaryBtn} onClick={handleCreate} disabled={creating || files.length === 0}>
            {creating ? <Loader2 className="w-4 h-4 animate-spin" /> : <FilePlus2 className="w-4 h-4" />}
            Create
          </button>
        </div>
        {createMsg && <Message kind={createMsg.kind} text={createMsg.text} />}
      </section>

      {/* ── Compress ────────────────────────────────────────── */}
      <section className={sectionCls} aria-labelledby="convert-compress">
        <div className="flex items-center gap-1.5 mb-2">
          <Minimize2 className="w-4 h-4 text-blue-500" />
          <h4 id="convert-compress" className="text-sm font-medium">Compress &amp; optimize</h4>
        </div>
        <div className="space-y-1" role="radiogroup" aria-label="Compression preset">
          {PRESETS.map((p) => (
            <label key={p.id} className="flex items-center gap-2 text-sm cursor-pointer">
              <input type="radio" name="convert-preset" value={p.id} checked={preset === p.id} onChange={() => { setPreset(p.id); setCompressRes(null); }} />
              <span className="font-medium">{p.label}</span>
              <span className="text-xs text-gray-500 dark:text-gray-400">{p.desc}</span>
            </label>
          ))}
        </div>
        <div className="flex gap-2 mt-2">
          <button className={`${secondaryBtn} flex-1`} onClick={() => handleCompress(true)} disabled={compressing !== null}>
            {compressing === "estimate" && <Loader2 className="w-4 h-4 animate-spin" />}
            Estimate
          </button>
          <button className={`${primaryBtn} flex-1`} onClick={() => handleCompress(false)} disabled={compressing !== null}>
            {compressing === "apply" && <Loader2 className="w-4 h-4 animate-spin" />}
            Compress
          </button>
        </div>
        {compressRes && (
          <div className="mt-2 p-2 rounded-lg bg-gray-50 dark:bg-gray-800 text-xs space-y-0.5" data-testid="compress-result">
            <div className="flex justify-between"><span>Before</span><span>{formatBytes(compressRes.before_bytes)}</span></div>
            <div className="flex justify-between font-medium">
              <span>{compressRes.dry_run ? "Estimated after" : "After"}</span>
              <span>{formatBytes(compressRes.dry_run ? compressRes.optimized_bytes : compressRes.after_bytes)}</span>
            </div>
            <div className="flex justify-between text-green-700 dark:text-green-400">
              <span>Saved</span>
              <span>{compressRes.saved_bytes > 0 ? `${formatBytes(compressRes.saved_bytes)} (${compressRes.saved_percent}%)` : "nothing"}</span>
            </div>
            <div className="text-gray-500 dark:text-gray-400">
              {compressRes.images_rewritten}/{compressRes.images_total} images recompressed
              {compressRes.fonts_subset ? ", fonts subset" : ""}
              {!compressRes.dry_run && !compressRes.applied ? ". File already optimal, left unchanged." : ""}
            </div>
          </div>
        )}
        {compressErr && <Message kind="err" text={compressErr} />}
      </section>
    </div>
  );
}
