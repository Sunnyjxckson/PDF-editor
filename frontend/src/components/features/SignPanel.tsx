"use client";

import { useEffect, useMemo, useState } from "react";
import {
  X, PenLine, Plus, Trash2, Type, CalendarDays, Check, XIcon, Lock, ShieldCheck, ShieldAlert,
  ShieldQuestion, KeyRound, Download, Loader2, FileSearch, Upload, Signature, Clock, BadgeCheck,
} from "lucide-react";
import SignCreateDialog from "./SignCreateDialog";
import {
  addToLibrary,
  addTrustedCertificate,
  applySignItems,
  createCertificate,
  deleteCertificate,
  digitalSign,
  getCertificate,
  getCertificateDownloadUrl,
  importCertificate,
  isSelfSignedId,
  isValidTsaUrl,
  listTrustedCertificates,
  loadCertId,
  loadLibrary,
  loadSignPrefs,
  removeTrustedCertificate,
  saveCertId,
  saveLibrary,
  saveSignPrefs,
  signatureFacts,
  SELF_SIGNED_NOTE,
  DEFAULT_SIGN_PREFS,
  useSignStore,
  validateDocumentSignatures,
  validateUploadedPdf,
  type CertificateInfo,
  type SignatureEntry,
  type SignatureKind,
  type SignPrefs,
  type SignTool,
  type TrustedList,
  type ValidationReport,
} from "@/lib/features/sign";

/**
 * Signatures & Fill-and-Sign side panel.
 *
 * Mount next to the page viewer while the user is in "Sign" mode, AND mount
 * <SignOverlay/> over the rendered page (they share state via useSignStore).
 * The panel activates the overlay on mount and deactivates it on unmount.
 */
export interface SignPanelProps {
  docId: string;
  currentPage: number; // 0-based
  onDocumentChanged: () => void; // re-render pages (e.g. store.bumpVersion)
  onClose?: () => void;
  onNotify?: (message: string, type?: "success" | "error" | "info") => void;
}

type Tab = "fill" | "digital" | "verify";

export default function SignPanel({ docId, currentPage, onDocumentChanged, onClose, onNotify }: SignPanelProps) {
  const {
    tool, setTool, library, setLibrary, selectedEntryId, selectEntry, items, clearItems,
    removeItem, setActive, inkColor, setInkColor,
  } = useSignStore();
  const [tab, setTab] = useState<Tab>("fill");
  const [dialog, setDialog] = useState<SignatureKind | null>(null);
  const [lockAfter, setLockAfter] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<{ text: string; type: "success" | "error" | "info" } | null>(null);

  // Digital ID
  const [cert, setCert] = useState<CertificateInfo | null>(null);
  const [certForm, setCertForm] = useState({ name: "", email: "", organization: "", passphrase: "" });
  const [passphrase, setPassphrase] = useState("");
  const [reason, setReason] = useState("I approve this document");
  const [location, setLocation] = useState("");
  const [certify, setCertify] = useState(false);
  const [appearanceId, setAppearanceId] = useState<string>("auto");
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [uploadReport, setUploadReport] = useState<{ name: string; report: ValidationReport } | null>(null);
  const [idMode, setIdMode] = useState<"import" | "create">("import");
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importPass, setImportPass] = useState("");
  const [prefs, setPrefsState] = useState<SignPrefs>(DEFAULT_SIGN_PREFS);
  const [trusted, setTrusted] = useState<TrustedList | null>(null);
  const setPrefs = (patch: Partial<SignPrefs>) =>
    setPrefsState((p) => {
      const next = { ...p, ...patch };
      saveSignPrefs(next);
      return next;
    });
  const selfSigned = isSelfSignedId(cert);

  const notify = (text: string, type: "success" | "error" | "info" = "info") => {
    setMessage({ text, type });
    onNotify?.(text, type);
  };

  useEffect(() => {
    setActive(true);
    setLibrary(loadLibrary());
    setPrefsState(loadSignPrefs());
    const id = loadCertId();
    if (id) {
      getCertificate(id).then(setCert).catch(() => saveCertId(null));
    }
    return () => setActive(false);
  }, [setActive, setLibrary]);

  // Refresh the signature report when entering the digital/verify tabs
  // (online OCSP/CRL checks only on the Verify tab, and only when switched on)
  useEffect(() => {
    if (tab === "fill") return;
    const fetchRevocation = tab === "verify" && prefs.fetchRevocation;
    validateDocumentSignatures(docId, { fetchRevocation }).then(setReport).catch(() => setReport(null));
    if (tab === "verify") listTrustedCertificates().then(setTrusted).catch(() => setTrusted(null));
  }, [tab, docId, prefs.fetchRevocation]);

  const sigs = library.filter((l) => l.kind === "signature");
  const inits = library.filter((l) => l.kind === "initials");
  const pickedSig = sigs.find((s) => s.id === selectedEntryId.signature) ?? sigs[0];
  const pickedInit = inits.find((s) => s.id === selectedEntryId.initials) ?? inits[0];
  const imageItems = useMemo(() => items.filter((i) => i.type === "image"), [items]);
  const docSigned = (report?.signature_count ?? 0) > 0;

  const onCreated = (entry: SignatureEntry) => {
    const next = addToLibrary(library, entry);
    setLibrary(next);
    if (!saveLibrary(next)) notify("Saved for this session only (browser storage is unavailable or full)", "info");
    selectEntry(entry.kind, entry.id);
    setTool(entry.kind);
  };

  const deleteEntry = (id: string) => {
    const next = library.filter((l) => l.id !== id);
    setLibrary(next);
    saveLibrary(next);
  };

  const chooseTool = (t: SignTool) => {
    if ((t === "signature" && !pickedSig) || (t === "initials" && !pickedInit)) {
      setDialog(t);
      return;
    }
    setTool(tool === t ? null : t);
  };

  const apply = async () => {
    const ready = items.filter((i) => !((i.type === "text" || i.type === "date") && !(i.text ?? "").trim()));
    if (!ready.length) {
      notify("Place something on the page first", "info");
      return;
    }
    setBusy("apply");
    try {
      const res = await applySignItems(docId, ready, lockAfter);
      clearItems();
      setTool(null);
      onDocumentChanged();
      const n = Object.values(res.applied).reduce((a, b) => a + b, 0);
      notify(`Applied ${n} item${n === 1 ? "" : "s"}${res.locked ? " and locked the document" : ""}`, "success");
    } catch (e) {
      notify((e as Error).message, "error");
    } finally {
      setBusy(null);
    }
  };

  const createId = async () => {
    if (!certForm.name.trim()) {
      notify("Enter the name to put on your digital ID", "error");
      return;
    }
    setBusy("cert");
    try {
      const c = await createCertificate(certForm);
      setCert(c);
      saveCertId(c.cert_id);
      setCertForm({ name: "", email: "", organization: "", passphrase: "" });
      notify("Digital ID created", "success");
    } catch (e) {
      notify((e as Error).message, "error");
    } finally {
      setBusy(null);
    }
  };

  const importId = async () => {
    if (!importFile || !importPass) {
      notify("Choose your .p12/.pfx file and enter its passphrase", "error");
      return;
    }
    setBusy("cert");
    try {
      const c = await importCertificate(importFile, importPass);
      setCert(c);
      saveCertId(c.cert_id);
      setImportFile(null);
      setImportPass("");
      notify(`Imported digital ID for ${c.name}${c.self_signed ? " (self-signed)" : ` issued by ${c.issuer}`}`, "success");
    } catch (e) {
      notify((e as Error).message, "error");
    } finally {
      setBusy(null);
    }
  };

  const forgetId = () => {
    saveCertId(null);
    setCert(null);
    setPassphrase("");
  };

  const trustFile = async (f: File | undefined) => {
    if (!f) return;
    try {
      const r = await addTrustedCertificate(f);
      notify(`Trusted ${r.added.map((a) => a.subject).join(", ")}`, "success");
      setTrusted(await listTrustedCertificates());
      setReport(await validateDocumentSignatures(docId, { fetchRevocation: prefs.fetchRevocation }));
    } catch (e) {
      notify((e as Error).message, "error");
    }
  };

  const untrust = async (fp: string) => {
    try {
      await removeTrustedCertificate(fp);
      setTrusted(await listTrustedCertificates());
      setReport(await validateDocumentSignatures(docId, { fetchRevocation: prefs.fetchRevocation }));
    } catch (e) {
      notify((e as Error).message, "error");
    }
  };

  const removeId = async () => {
    if (!cert || !confirm("Delete this digital ID? Documents you already signed stay signed.")) return;
    try {
      await deleteCertificate(cert.cert_id);
    } catch {
      /* already gone */
    }
    saveCertId(null);
    setCert(null);
  };

  const signDigitally = async (fieldName?: string) => {
    if (!cert) return;
    // Appearance: chosen placed signature item, or the first placed one ("auto"), or invisible.
    const appearance =
      appearanceId === "none" ? undefined : appearanceId === "auto" ? imageItems[0] : imageItems.find((i) => i.id === appearanceId);
    const others = items.filter((i) => i.id !== appearance?.id && !((i.type === "text" || i.type === "date") && !(i.text ?? "").trim()));
    if (prefs.timestamp && !isValidTsaUrl(prefs.tsaUrl)) {
      notify("Enter a valid http(s) timestamp server URL", "error");
      return;
    }
    if (docSigned && others.length) {
      notify("This PDF is already signed; extra fill-in items would break that signature. Remove them first.", "error");
      return;
    }
    setBusy("digital");
    try {
      if (others.length) await applySignItems(docId, others, false);
      const res = await digitalSign(docId, {
        certId: cert.cert_id,
        passphrase: cert.passphrase_protected ? passphrase : undefined,
        page: fieldName ? 0 : appearance?.page ?? currentPage,
        rect: fieldName ? undefined : appearance?.rect,
        image: fieldName ? appearance?.image ?? pickedSig?.dataUrl : appearance?.image,
        reason,
        location,
        fieldName,
        lock: certify,
        timestamp: prefs.timestamp,
        tsaUrl: prefs.tsaUrl,
        ltv: prefs.ltv && !selfSigned,
      });
      clearItems();
      setTool(null);
      onDocumentChanged();
      setReport(await validateDocumentSignatures(docId));
      const extras = [res.certified && "certified, no changes allowed", res.timestamped && "timestamped", res.ltv && "LTV-enabled"]
        .filter(Boolean)
        .join(", ");
      notify(`Digitally signed as ${res.signer}${extras ? ` (${extras})` : ""}`, "success");
    } catch (e) {
      notify((e as Error).message, "error");
    } finally {
      setBusy(null);
    }
  };

  const verifyUpload = async (f: File | undefined) => {
    if (!f) return;
    setBusy("verify");
    try {
      setUploadReport({ name: f.name, report: await validateUploadedPdf(f, { fetchRevocation: prefs.fetchRevocation }) });
    } catch (e) {
      notify((e as Error).message, "error");
    } finally {
      setBusy(null);
    }
  };

  const toolBtn = (t: SignTool, Icon: typeof PenLine, label: string) => (
    <button
      key={String(t)}
      onClick={() => chooseTool(t)}
      title={label}
      className={`flex flex-col items-center gap-0.5 py-1.5 rounded-lg text-[11px] transition-colors ${
        tool === t
          ? "bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300"
          : "hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-700 dark:text-gray-300"
      }`}
    >
      <Icon className="w-4 h-4" /> {label}
    </button>
  );

  const entryRow = (kind: SignatureKind, list: SignatureEntry[], picked?: SignatureEntry) => (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs font-medium text-gray-600 dark:text-gray-400 capitalize">{kind === "signature" ? "Signatures" : "Initials"}</span>
        <button onClick={() => setDialog(kind)} className="flex items-center gap-0.5 text-xs text-blue-600 dark:text-blue-400 hover:underline">
          <Plus className="w-3 h-3" /> New
        </button>
      </div>
      {list.length === 0 ? (
        <button
          onClick={() => setDialog(kind)}
          className="w-full py-3 text-xs rounded-lg border-2 border-dashed border-gray-300 dark:border-gray-600 text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-800"
        >
          Create your {kind}
        </button>
      ) : (
        <div className="flex gap-1.5 overflow-x-auto pb-1">
          {list.map((e) => (
            <div key={e.id} className="relative group shrink-0">
              <button
                onClick={() => {
                  selectEntry(kind, e.id);
                  setTool(kind);
                }}
                title="Click, then click on the page to place"
                className={`h-12 w-24 rounded-lg border bg-white flex items-center justify-center p-1 ${
                  picked?.id === e.id ? "border-blue-500 ring-2 ring-blue-200 dark:ring-blue-800" : "border-gray-200 dark:border-gray-700"
                }`}
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={e.dataUrl} alt={kind} className="max-h-full max-w-full object-contain" />
              </button>
              <button
                aria-label={`Delete ${kind}`}
                onClick={() => deleteEntry(e.id)}
                className="absolute -top-1.5 -right-1.5 hidden group-hover:flex p-0.5 rounded-full bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600"
              >
                <X className="w-3 h-3 text-red-600" />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );

  return (
    <div className="w-full sm:w-80 h-full flex flex-col bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 border-l border-gray-200 dark:border-gray-700">
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-200 dark:border-gray-700">
        <div className="flex items-center gap-2 font-semibold text-sm">
          <Signature className="w-4 h-4 text-blue-600" /> Fill &amp; Sign
        </div>
        {onClose && (
          <button onClick={onClose} aria-label="Close sign panel" className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800">
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      <div className="flex gap-1 p-2 border-b border-gray-200 dark:border-gray-700">
        {([
          ["fill", "Fill & Sign"],
          ["digital", "Digital ID"],
          ["verify", "Verify"],
        ] as const).map(([t, label]) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`flex-1 py-1.5 text-xs rounded-md ${
              tab === t ? "bg-gray-100 dark:bg-gray-800 font-semibold" : "text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-800/50"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto p-3 space-y-4 text-sm">
        {message && (
          <div
            className={`text-xs rounded-lg px-2 py-1.5 ${
              message.type === "error"
                ? "bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-300"
                : message.type === "success"
                  ? "bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300"
                  : "bg-gray-50 dark:bg-gray-800 text-gray-600 dark:text-gray-300"
            }`}
          >
            {message.text}
          </div>
        )}

        {tab === "fill" && (
          <>
            {entryRow("signature", sigs, pickedSig)}
            {entryRow("initials", inits, pickedInit)}

            <div>
              <div className="text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Place on page</div>
              <div className="grid grid-cols-6 gap-1">
                {toolBtn("signature", PenLine, "Sign")}
                {toolBtn("initials", PenLine, "Initial")}
                {toolBtn("text", Type, "Text")}
                {toolBtn("date", CalendarDays, "Date")}
                {toolBtn("check", Check, "Check")}
                {toolBtn("cross", XIcon, "X")}
              </div>
              <div className="flex items-center gap-2 mt-2">
                <span className="text-xs text-gray-500">Color</span>
                {["#000000", "#1d4ed8", "#b91c1c"].map((c) => (
                  <button
                    key={c}
                    aria-label={`Color ${c}`}
                    onClick={() => setInkColor(c)}
                    className={`w-4 h-4 rounded-full border-2 ${inkColor === c ? "border-blue-500" : "border-transparent"}`}
                    style={{ backgroundColor: c }}
                  />
                ))}
              </div>
              <p className="text-[11px] text-gray-500 mt-2">
                {tool ? "Click on the page to place. Drag to move, corners to resize, Delete to remove." : "Pick a tool, then click on the page."}
              </p>
            </div>

            {items.length > 0 && (
              <div>
                <div className="flex items-center justify-between text-xs mb-1">
                  <span className="font-medium text-gray-600 dark:text-gray-400">Pending ({items.length})</span>
                  <button onClick={clearItems} className="text-red-600 hover:underline">Clear all</button>
                </div>
                <ul className="space-y-1 max-h-40 overflow-y-auto">
                  {items.map((i) => (
                    <li key={i.id} className="flex items-center justify-between text-xs px-2 py-1 rounded bg-gray-50 dark:bg-gray-800">
                      <span className="truncate">
                        p.{i.page + 1} · {i.type === "image" ? i.sourceKind ?? "image" : i.type}
                        {i.text ? ` “${i.text}”` : ""}
                      </span>
                      <button aria-label="Remove" onClick={() => removeItem(i.id)}>
                        <Trash2 className="w-3 h-3 text-gray-400 hover:text-red-600" />
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <label className="flex items-start gap-2 text-xs">
              <input type="checkbox" checked={lockAfter} onChange={(e) => setLockAfter(e.target.checked)} className="mt-0.5" />
              <span>
                <span className="font-medium flex items-center gap-1"><Lock className="w-3 h-3" /> Lock after signing</span>
                <span className="text-gray-500">Flattens form fields and comments into the page so nothing stays editable.</span>
              </span>
            </label>

            <button
              onClick={apply}
              disabled={!!busy || items.length === 0}
              className="w-full flex items-center justify-center gap-2 py-2 rounded-lg bg-blue-600 text-white font-medium hover:bg-blue-700 disabled:opacity-50"
            >
              {busy === "apply" ? <Loader2 className="w-4 h-4 animate-spin" /> : <PenLine className="w-4 h-4" />}
              Apply to document
            </button>
          </>
        )}

        {tab === "digital" && (
          <>
            {!cert ? (
              <div className="space-y-2">
                <p className="text-xs text-gray-600 dark:text-gray-400">
                  A digital ID cryptographically seals the PDF: any later change is detectable.
                </p>
                <p className="text-[11px] rounded-lg bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-300 px-2 py-1.5">
                  {SELF_SIGNED_NOTE}
                </p>
                <div className="flex gap-1 text-xs" role="tablist">
                  {([
                    ["import", "Import CA-issued ID"],
                    ["create", "Create self-signed ID"],
                  ] as const).map(([m, label]) => (
                    <button
                      key={m}
                      role="tab"
                      aria-selected={idMode === m}
                      onClick={() => setIdMode(m)}
                      className={`flex-1 py-1 rounded-lg border ${
                        idMode === m
                          ? "border-blue-500 bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300"
                          : "border-gray-300 dark:border-gray-600"
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                {idMode === "import" ? (
                  <>
                    <p className="text-[11px] text-gray-500">
                      Use the .p12/.pfx from a certificate authority (ideally on Adobe&apos;s AATL list). Its key is kept
                      encrypted with your passphrase; the passphrase is never saved, so you type it each time you sign.
                    </p>
                    <label className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 text-xs cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800">
                      <Upload className="w-4 h-4 shrink-0" />
                      <span className="truncate">{importFile ? importFile.name : "Choose .p12 / .pfx file…"}</span>
                      <input
                        type="file"
                        accept=".p12,.pfx,application/x-pkcs12"
                        className="hidden"
                        onChange={(e) => setImportFile(e.target.files?.[0] ?? null)}
                      />
                    </label>
                    <input
                      type="password"
                      value={importPass}
                      onChange={(e) => setImportPass(e.target.value)}
                      placeholder="Digital ID passphrase"
                      autoComplete="current-password"
                      className="w-full px-2 py-1.5 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-sm"
                    />
                    <button
                      onClick={importId}
                      disabled={!!busy || !importFile || !importPass}
                      className="w-full flex items-center justify-center gap-2 py-2 rounded-lg bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
                    >
                      {busy === "cert" ? <Loader2 className="w-4 h-4 animate-spin" /> : <BadgeCheck className="w-4 h-4" />}
                      Import digital ID
                    </button>
                  </>
                ) : (
                  <>
                {(["name", "email", "organization"] as const).map((k) => (
                  <input
                    key={k}
                    value={certForm[k]}
                    onChange={(e) => setCertForm({ ...certForm, [k]: e.target.value })}
                    placeholder={k === "name" ? "Full name (required)" : k === "email" ? "Email" : "Organization"}
                    className="w-full px-2 py-1.5 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-sm"
                  />
                ))}
                <input
                  type="password"
                  value={certForm.passphrase}
                  onChange={(e) => setCertForm({ ...certForm, passphrase: e.target.value })}
                  placeholder="Passphrase to protect the key (optional)"
                  autoComplete="new-password"
                  className="w-full px-2 py-1.5 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-sm"
                />
                <button
                  onClick={createId}
                  disabled={!!busy}
                  className="w-full flex items-center justify-center gap-2 py-2 rounded-lg bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
                >
                  {busy === "cert" ? <Loader2 className="w-4 h-4 animate-spin" /> : <KeyRound className="w-4 h-4" />}
                  Create self-signed ID
                </button>
                  </>
                )}
              </div>
            ) : (
              <div className="space-y-3">
                <div className="rounded-lg border border-gray-200 dark:border-gray-700 p-2 text-xs space-y-0.5">
                  <div className="font-semibold text-sm flex items-center gap-1"><KeyRound className="w-3.5 h-3.5" /> {cert.name}</div>
                  {cert.email && <div className="text-gray-500">{cert.email}</div>}
                  {cert.organization && <div className="text-gray-500">{cert.organization}</div>}
                  <div className={selfSigned ? "text-amber-700 dark:text-amber-400" : "text-green-700 dark:text-green-400"}>
                    {selfSigned ? "Self-signed ID" : `CA-issued by ${cert.issuer ?? "unknown issuer"}`}
                  </div>
                  {selfSigned && <div className="text-[11px] text-gray-500">{SELF_SIGNED_NOTE}</div>}
                  <div className="text-gray-500">Valid until {new Date(cert.not_after).toLocaleDateString()}</div>
                  <div className="text-gray-400 font-mono truncate" title={cert.fingerprint_sha256}>SHA-256 {cert.fingerprint_sha256.slice(0, 23)}…</div>
                  <div className="flex gap-3 pt-1">
                    <a href={getCertificateDownloadUrl(cert.cert_id)} className="flex items-center gap-1 text-blue-600 dark:text-blue-400 hover:underline">
                      <Download className="w-3 h-3" /> Public cert
                    </a>
                    <button onClick={forgetId} className="text-gray-600 dark:text-gray-300 hover:underline">Use another ID</button>
                    <button onClick={removeId} className="text-red-600 hover:underline">Delete ID</button>
                  </div>
                </div>

                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-gray-600 dark:text-gray-400">Visible appearance</label>
                  <select
                    value={appearanceId}
                    onChange={(e) => setAppearanceId(e.target.value)}
                    className="w-full px-2 py-1.5 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-xs"
                  >
                    <option value="auto">{imageItems.length ? "First placed signature" : "— place a signature on the Fill & Sign tab —"}</option>
                    {imageItems.map((i, n) => (
                      <option key={i.id} value={i.id}>Placed {i.sourceKind ?? "image"} #{n + 1} (page {i.page + 1})</option>
                    ))}
                    <option value="none">Invisible signature</option>
                  </select>
                  <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Reason"
                    className="w-full px-2 py-1.5 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-xs" />
                  <input value={location} onChange={(e) => setLocation(e.target.value)} placeholder="Location"
                    className="w-full px-2 py-1.5 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-xs" />
                  {cert.passphrase_protected && (
                    <input type="password" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} placeholder="Digital ID passphrase"
                      autoComplete="current-password"
                      className="w-full px-2 py-1.5 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-xs" />
                  )}
                  <label className="flex items-start gap-2 text-xs">
                    <input type="checkbox" checked={certify} disabled={docSigned} onChange={(e) => setCertify(e.target.checked)} className="mt-0.5" />
                    <span>
                      <span className="font-medium">Certify &amp; lock</span>{" "}
                      <span className="text-gray-500">— any later change marks the signature invalid.</span>
                    </span>
                  </label>
                  <label className="flex items-start gap-2 text-xs">
                    <input type="checkbox" checked={prefs.timestamp} onChange={(e) => setPrefs({ timestamp: e.target.checked })} className="mt-0.5" />
                    <span>
                      <span className="font-medium">Add trusted timestamp</span>{" "}
                      <span className="text-gray-500">— RFC 3161; contacts the timestamp server over the internet.</span>
                    </span>
                  </label>
                  {prefs.timestamp && (
                    <input
                      value={prefs.tsaUrl}
                      onChange={(e) => setPrefs({ tsaUrl: e.target.value })}
                      placeholder="Timestamp server URL"
                      aria-label="Timestamp server URL"
                      className={`w-full px-2 py-1.5 rounded-lg border bg-white dark:bg-gray-800 text-xs font-mono ${
                        isValidTsaUrl(prefs.tsaUrl) ? "border-gray-300 dark:border-gray-600" : "border-red-500"
                      }`}
                    />
                  )}
                  <label className={`flex items-start gap-2 text-xs ${selfSigned ? "opacity-60" : ""}`}>
                    <input
                      type="checkbox"
                      checked={prefs.ltv && !selfSigned}
                      disabled={selfSigned}
                      onChange={(e) => setPrefs({ ltv: e.target.checked })}
                      className="mt-0.5"
                    />
                    <span>
                      <span className="font-medium">Make LTV-enabled</span>{" "}
                      <span className="text-gray-500">
                        {selfSigned
                          ? "— needs a CA-issued ID (a self-signed ID has no revocation info to embed)."
                          : "— embeds OCSP/CRL answers so it still validates after the ID expires (fetched online)."}
                      </span>
                    </span>
                  </label>
                </div>

                {report && report.empty_signature_fields.length > 0 && (
                  <div className="space-y-1">
                    <div className="text-xs font-medium text-gray-600 dark:text-gray-400">Signature fields in this PDF</div>
                    {report.empty_signature_fields.map((f) => (
                      <button key={f.field_name} onClick={() => signDigitally(f.field_name)} disabled={!!busy}
                        className="w-full text-left text-xs px-2 py-1.5 rounded-lg border border-blue-300 dark:border-blue-700 hover:bg-blue-50 dark:hover:bg-blue-900/30">
                        Sign field “{f.field_name}” (page {f.page + 1})
                      </button>
                    ))}
                  </div>
                )}

                <button
                  onClick={() => signDigitally()}
                  disabled={!!busy || (appearanceId !== "none" && imageItems.length === 0)}
                  className="w-full flex items-center justify-center gap-2 py-2 rounded-lg bg-blue-600 text-white font-medium hover:bg-blue-700 disabled:opacity-50"
                >
                  {busy === "digital" ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldCheck className="w-4 h-4" />}
                  Sign digitally
                </button>
                <p className="text-[11px] text-gray-500">
                  Sign last: editing the PDF after a digital signature invalidates it. Other pending items are applied first.
                </p>
              </div>
            )}
          </>
        )}

        {tab === "verify" && (
          <div className="space-y-3">
            <button
              onClick={() =>
                validateDocumentSignatures(docId, { fetchRevocation: prefs.fetchRevocation })
                  .then(setReport)
                  .catch((e) => notify((e as Error).message, "error"))
              }
              className="w-full flex items-center justify-center gap-2 py-1.5 rounded-lg border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-800 text-xs"
            >
              <FileSearch className="w-4 h-4" /> Check this document
            </button>
            <label className="flex items-start gap-2 text-xs">
              <input
                type="checkbox"
                checked={prefs.fetchRevocation}
                onChange={(e) => setPrefs({ fetchRevocation: e.target.checked })}
                className="mt-0.5"
              />
              <span>
                <span className="font-medium">Check revocation online</span>{" "}
                <span className="text-gray-500">— asks the issuer&apos;s OCSP/CRL servers whether the ID was revoked.</span>
              </span>
            </label>
            {report && <ReportView report={report} />}
            <label className="w-full flex items-center justify-center gap-2 py-1.5 rounded-lg border-2 border-dashed border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-800 text-xs cursor-pointer">
              {busy === "verify" ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
              Verify another PDF…
              <input type="file" accept="application/pdf,.pdf" className="hidden" onChange={(e) => verifyUpload(e.target.files?.[0])} />
            </label>
            {uploadReport && (
              <div>
                <div className="text-xs font-medium mb-1 truncate">{uploadReport.name}</div>
                <ReportView report={uploadReport.report} />
              </div>
            )}
            <div className="rounded-lg border border-gray-200 dark:border-gray-700 p-2 space-y-1.5 text-xs">
              <div className="font-medium flex items-center gap-1"><ShieldCheck className="w-3.5 h-3.5" /> Trusted certificates</div>
              <p className="text-[11px] text-gray-500">
                {trusted
                  ? `${trusted.system.count} system root certificates (${trusted.system.source === "certifi" ? "Mozilla bundle" : "macOS"}) plus the ones you add.`
                  : "System root certificates plus the ones you add."}
              </p>
              {trusted?.user.map((t) => (
                <div key={t.fingerprint_sha256} className="flex items-center gap-1">
                  <span className="flex-1 truncate" title={`${t.subject}\nSHA-256 ${t.fingerprint_sha256}`}>{t.subject}</span>
                  <button onClick={() => untrust(t.fingerprint_sha256)} aria-label={`Stop trusting ${t.subject}`} className="p-0.5 text-red-600">
                    <X className="w-3 h-3" />
                  </button>
                </div>
              ))}
              <label className="flex items-center gap-1 text-blue-600 dark:text-blue-400 hover:underline cursor-pointer">
                <Plus className="w-3 h-3" /> Trust a certificate (.cer)…
                <input
                  type="file"
                  accept=".cer,.crt,.pem,.der,application/pkix-cert,application/x-x509-ca-cert"
                  className="hidden"
                  onChange={(e) => {
                    trustFile(e.target.files?.[0]);
                    e.target.value = "";
                  }}
                />
              </label>
            </div>
          </div>
        )}
      </div>

      <SignCreateDialog
        open={dialog !== null}
        initialKind={dialog ?? "signature"}
        defaultName={cert?.name ?? ""}
        onClose={() => setDialog(null)}
        onCreate={onCreated}
      />
    </div>
  );
}

const FACT_TONE = {
  good: "text-green-700 dark:text-green-400",
  warn: "text-amber-700 dark:text-amber-400",
  bad: "text-red-600",
  neutral: "text-gray-700 dark:text-gray-300",
} as const;

function ReportView({ report }: { report: ValidationReport }) {
  if (report.signature_count === 0) {
    return (
      <p className="text-xs text-gray-500">
        No digital signatures{report.empty_signature_fields.length ? ` (${report.empty_signature_fields.length} empty signature field(s))` : ""}.
      </p>
    );
  }
  return (
    <ul className="space-y-2">
      {report.signatures.map((s) => {
        const ok = s.intact && s.valid && !s.summary.startsWith("INVALID");
        const Icon = !ok ? ShieldAlert : s.trusted ? ShieldCheck : ShieldQuestion;
        const color = !ok ? "text-red-600" : s.modified_after_signing ? "text-amber-600" : s.trusted ? "text-green-600" : "text-amber-600";
        return (
          <li key={s.field_name} className="rounded-lg border border-gray-200 dark:border-gray-700 p-2 text-xs space-y-0.5">
            <div className={`flex items-center gap-1 font-semibold ${color}`}>
              <Icon className="w-4 h-4" /> {s.signer_name ?? "Unknown signer"}
            </div>
            <div className="text-gray-700 dark:text-gray-300">{s.summary}</div>
            <dl className="grid grid-cols-[auto,1fr] gap-x-2 gap-y-0.5 pt-1">
              {signatureFacts(s).map((f) => (
                <div key={f.label} className="contents">
                  <dt className="text-gray-500">{f.label}</dt>
                  <dd className={FACT_TONE[f.tone]}>{f.value}</dd>
                </div>
              ))}
            </dl>
            {s.signing_time && (
              <div className="text-gray-500 flex items-center gap-1">
                <Clock className="w-3 h-3" /> Signed {new Date(s.signing_time).toLocaleString()}
              </div>
            )}
            {s.reason && <div className="text-gray-500">Reason: {s.reason}</div>}
            <div className="text-gray-400">
              {s.certified ? "Certification signature · " : ""}
              {s.issued_by_this_app ? "Self-signed ID created in this app" : s.self_signed ? "Self-signed ID" : "CA-issued ID"}
            </div>
          </li>
        );
      })}
    </ul>
  );
}
