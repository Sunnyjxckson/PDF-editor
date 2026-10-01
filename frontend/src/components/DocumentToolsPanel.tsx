"use client";

import { useState } from "react";
import {
  X, Lock, Unlock, Download, Droplets, Hash, BookMarked, Archive, Layers, GitCompare, Sparkles, Stamp, Loader2,
} from "lucide-react";
import { useEditorStore } from "@/lib/store";
import {
  addWatermark, addStamp, convertToPdfA, flattenDocument, compareDocuments, uploadPDF, deleteDocument,
  type CompareResult,
} from "@/lib/api";
import { protectDocument, unlockDocument, permissionsFrom, type Permission } from "@/lib/features/redact";
import { saveBlob } from "@/lib/features/convert";

/**
 * Document-level tools that the backend has always had but the UI never exposed
 * (advanced_ops: watermark, stamp, PDF/A, flatten, compare), plus password
 * protection through the redact module's /security/protect (which, unlike
 * advanced_ops /protect, never locks the working copy behind an open password).
 */
export default function DocumentToolsPanel({ section }: { section: "protect" | "tools" }) {
  const docId = useEditorStore((s) => s.docId);
  const setActivePanel = useEditorStore((s) => s.setActivePanel);

  if (!docId) return null;
  return (
    <aside className="flex h-full w-full sm:w-80 flex-col border-l border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100">
      <div className="flex items-center justify-between border-b border-gray-200 dark:border-gray-700 px-3 py-2">
        <h2 className="text-sm font-semibold">{section === "protect" ? "Protect" : "More tools"}</h2>
        <button onClick={() => setActivePanel(null)} aria-label="Close panel" className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800">
          <X className="w-4 h-4" />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-3 space-y-3 text-sm">
        {section === "protect" ? <ProtectSection docId={docId} /> : <ToolsSection docId={docId} />}
      </div>
    </aside>
  );
}

// ─── shared bits ────────────────────────────────────────────────────────────

const card = "rounded-lg border border-gray-200 dark:border-gray-700 p-3 space-y-2";
const input =
  "w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500";
const btn =
  "inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-50";
const btnPrimary = `${btn} bg-blue-600 text-white hover:bg-blue-700`;
const btnSecondary = `${btn} bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700`;

function hexToRgb01(hex: string): number[] {
  const h = hex.replace("#", "");
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
}

function useRunner() {
  const addToast = useEditorStore((s) => s.addToast);
  const reloadDocument = useEditorStore((s) => s.reloadDocument);
  const [busy, setBusy] = useState<string | null>(null);
  const run = async (key: string, fn: () => Promise<string | void>, changesDoc = true) => {
    setBusy(key);
    try {
      const msg = await fn();
      if (changesDoc) await reloadDocument();
      if (msg) addToast(msg, "success");
    } catch (e) {
      addToast(e instanceof Error ? e.message : String(e), "error");
    } finally {
      setBusy(null);
    }
  };
  return { busy, run };
}

function Title({ icon: Icon, children }: { icon: typeof Lock; children: React.ReactNode }) {
  return (
    <h3 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
      <Icon className="w-3.5 h-3.5" /> {children}
    </h3>
  );
}

// ─── Protect ────────────────────────────────────────────────────────────────

const PERMISSION_LABELS: Record<Permission, string> = {
  print: "Printing",
  copy: "Copying text and images",
  modify: "Editing",
  annotate: "Commenting",
  fill_forms: "Filling forms",
  assemble: "Inserting, deleting, rotating pages",
};

function ProtectSection({ docId }: { docId: string }) {
  const { busy, run } = useRunner();
  const [userPw, setUserPw] = useState("");
  const [ownerPw, setOwnerPw] = useState("");
  const [perms, setPerms] = useState<Record<Permission, boolean>>({
    print: true, copy: false, modify: false, annotate: true, fill_forms: true, assemble: false,
  });
  const [unlockPw, setUnlockPw] = useState("");
  const setActiveTool = useEditorStore((s) => s.setActiveTool);

  return (
    <>
      <section className={card}>
        <Title icon={Lock}>Password protect</Title>
        <label className="block text-xs">
          Open password <span className="text-gray-400">(optional, needed to view)</span>
          <input type="password" autoComplete="new-password" className={input} value={userPw} onChange={(e) => setUserPw(e.target.value)} />
        </label>
        <label className="block text-xs">
          Permissions password <span className="text-red-500">*</span>
          <input type="password" autoComplete="new-password" className={input} value={ownerPw} onChange={(e) => setOwnerPw(e.target.value)} />
        </label>
        <fieldset className="space-y-1">
          <legend className="text-xs text-gray-500 dark:text-gray-400 mb-1">Allow</legend>
          {(Object.keys(PERMISSION_LABELS) as Permission[]).map((p) => (
            <label key={p} className="flex items-center gap-2 text-xs">
              <input type="checkbox" checked={perms[p]} onChange={(e) => setPerms({ ...perms, [p]: e.target.checked })} />
              {PERMISSION_LABELS[p]}
            </label>
          ))}
        </fieldset>
        <p className="text-[11px] text-gray-500 dark:text-gray-400">AES-256. The download is protected; your working copy stays editable.</p>
        <div className="flex flex-wrap gap-2">
          <button
            className={btnPrimary}
            disabled={!ownerPw || busy !== null}
            onClick={() =>
              run("download", async () => {
                const blob = await protectDocument(docId, { user_password: userPw || undefined, owner_password: ownerPw, permissions: permissionsFrom(perms) });
                if (blob instanceof Blob) saveBlob(blob, "protected.pdf");
                return "Protected copy downloaded";
              }, false)
            }
          >
            {busy === "download" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Download className="w-3.5 h-3.5" />}
            Download protected copy
          </button>
          <button
            className={btnSecondary}
            disabled={!ownerPw || !!userPw || busy !== null}
            title={userPw ? "Only permission restrictions can be applied to the working copy" : undefined}
            onClick={() =>
              run("apply", async () => {
                await protectDocument(docId, { owner_password: ownerPw, permissions: permissionsFrom(perms), apply_to_document: true });
                return "Restrictions applied to this document";
              })
            }
          >
            Apply restrictions here
          </button>
        </div>
      </section>

      <section className={card}>
        <Title icon={Unlock}>Remove security</Title>
        <input type="password" placeholder="Permissions password" className={input} value={unlockPw} onChange={(e) => setUnlockPw(e.target.value)} />
        <button
          className={btnSecondary}
          disabled={!unlockPw || busy !== null}
          onClick={() => run("unlock", async () => { await unlockDocument(docId, unlockPw); setUnlockPw(""); return "Security removed"; })}
        >
          Remove security
        </button>
      </section>

      <p className="text-xs text-gray-500 dark:text-gray-400">
        To strip metadata, hidden text, scripts and attachments, use{" "}
        <button className="text-blue-600 dark:text-blue-400 underline" onClick={() => setActiveTool("redact")}>Redact &rsaquo; Sanitize</button>.
      </p>
    </>
  );
}

// ─── More tools ─────────────────────────────────────────────────────────────

function ToolsSection({ docId }: { docId: string }) {
  const { busy, run } = useRunner();
  const currentPage = useEditorStore((s) => s.currentPage);
  const setHeaderFooterOpen = useEditorStore((s) => s.setHeaderFooterOpen);
  const setActivePanel = useEditorStore((s) => s.setActivePanel);
  const setAiPanelOpen = useEditorStore((s) => s.setAiPanelOpen);
  const addToast = useEditorStore((s) => s.addToast);

  const [wm, setWm] = useState({ text: "CONFIDENTIAL", opacity: 0.3, rotation: -45, font_size: 60, color: "#999999", scope: "all" as "all" | "current" });
  const [stamp, setStamp] = useState({ text: "Page {n} of {total}", position: "bottom-center", font_size: 10 });
  const [compare, setCompare] = useState<CompareResult | null>(null);
  const [compareName, setCompareName] = useState("");

  const runCompare = async (file: File) => {
    setCompare(null);
    setCompareName(file.name);
    await run("compare", async () => {
      const other = await uploadPDF(file);
      try {
        setCompare(await compareDocuments(docId, other.id));
      } finally {
        await deleteDocument(other.id);
      }
    }, false);
  };

  return (
    <>
      <section className={card}>
        <Title icon={Droplets}>Watermark</Title>
        <input className={input} value={wm.text} onChange={(e) => setWm({ ...wm, text: e.target.value })} aria-label="Watermark text" />
        <div className="grid grid-cols-2 gap-2 text-xs">
          <label>Opacity
            <input type="range" min={0.05} max={1} step={0.05} value={wm.opacity} onChange={(e) => setWm({ ...wm, opacity: Number(e.target.value) })} className="w-full" />
          </label>
          <label>Angle
            <select className={input} value={wm.rotation} onChange={(e) => setWm({ ...wm, rotation: Number(e.target.value) })}>
              <option value={-45}>Diagonal</option><option value={0}>Horizontal</option><option value={45}>Diagonal up</option><option value={90}>Vertical</option>
            </select>
          </label>
          <label>Size
            <input type="number" min={8} max={200} className={input} value={wm.font_size} onChange={(e) => setWm({ ...wm, font_size: Number(e.target.value) })} />
          </label>
          <label>Colour
            <input type="color" className="w-full h-7 rounded border border-gray-300 dark:border-gray-600" value={wm.color} onChange={(e) => setWm({ ...wm, color: e.target.value })} />
          </label>
        </div>
        <div className="flex items-center gap-3 text-xs">
          <label className="flex items-center gap-1"><input type="radio" checked={wm.scope === "all"} onChange={() => setWm({ ...wm, scope: "all" })} /> All pages</label>
          <label className="flex items-center gap-1"><input type="radio" checked={wm.scope === "current"} onChange={() => setWm({ ...wm, scope: "current" })} /> This page</label>
        </div>
        <button
          className={btnPrimary}
          disabled={!wm.text.trim() || busy !== null}
          onClick={() => run("wm", async () => {
            const r = await addWatermark(docId, {
              text: wm.text, opacity: wm.opacity, rotation: wm.rotation, font_size: wm.font_size,
              color: hexToRgb01(wm.color), pages: wm.scope === "all" ? "all" : [currentPage],
            });
            return `Watermark added to ${r.pages_watermarked} page(s)`;
          })}
        >
          {busy === "wm" && <Loader2 className="w-3.5 h-3.5 animate-spin" />} Add watermark
        </button>
      </section>

      <section className={card}>
        <Title icon={Hash}>Header, footer &amp; Bates</Title>
        <p className="text-xs text-gray-500 dark:text-gray-400">Page numbers, six header/footer slots with {"{n} {total} {date}"} tokens, and Bates numbering with a live preview.</p>
        <button className={btnSecondary} onClick={() => setHeaderFooterOpen(true)}>Open header &amp; footer…</button>
        <div className="pt-1 border-t border-gray-100 dark:border-gray-800 space-y-2">
          <div className="flex items-center gap-1.5 text-xs font-medium"><Stamp className="w-3.5 h-3.5" /> Quick text stamp</div>
          <input className={input} value={stamp.text} onChange={(e) => setStamp({ ...stamp, text: e.target.value })} aria-label="Stamp text" />
          <div className="grid grid-cols-2 gap-2">
            <select className={input} value={stamp.position} onChange={(e) => setStamp({ ...stamp, position: e.target.value })} aria-label="Stamp position">
              {["top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right"].map((p) => <option key={p} value={p}>{p.replace("-", " ")}</option>)}
            </select>
            <input type="number" min={6} max={72} className={input} value={stamp.font_size} onChange={(e) => setStamp({ ...stamp, font_size: Number(e.target.value) })} aria-label="Stamp font size" />
          </div>
          <button
            className={btnSecondary}
            disabled={!stamp.text.trim() || busy !== null}
            onClick={() => run("stamp", async () => {
              const r = await addStamp(docId, { ...stamp, pages: "all" });
              return `Stamped ${r.pages_stamped} page(s)`;
            })}
          >
            {busy === "stamp" && <Loader2 className="w-3.5 h-3.5 animate-spin" />} Stamp all pages
          </button>
        </div>
      </section>

      <section className={card}>
        <Title icon={BookMarked}>Navigation</Title>
        <button className={btnSecondary} onClick={() => setActivePanel("bookmarks")}>Edit bookmarks</button>
      </section>

      <section className={card}>
        <Title icon={Layers}>Finalize</Title>
        <div className="flex flex-wrap gap-2">
          <button
            className={btnSecondary}
            disabled={busy !== null}
            onClick={() => {
              if (!confirm("Flatten all comments and form fields into the page? They will no longer be editable (Undo still works).")) return;
              run("flatten", async () => { const r = await flattenDocument(docId); return `Flattened ${r.flattened} item(s)`; });
            }}
          >
            {busy === "flatten" && <Loader2 className="w-3.5 h-3.5 animate-spin" />} Flatten
          </button>
          <button
            className={btnSecondary}
            disabled={busy !== null}
            onClick={() => run("pdfa", async () => { await convertToPdfA(docId); return "Converted for archiving (PDF/A-style)"; })}
          >
            {busy === "pdfa" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Archive className="w-3.5 h-3.5" />} PDF/A
          </button>
        </div>
        <p className="text-[11px] text-gray-500 dark:text-gray-400">PDF/A embeds fonts, removes scripts and bakes form values. It is archival clean-up, not a validated PDF/A-1b/2b file.</p>
      </section>

      <section className={card}>
        <Title icon={GitCompare}>Compare documents</Title>
        <label className={`${btnSecondary} cursor-pointer w-full`}>
          {busy === "compare" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <GitCompare className="w-3.5 h-3.5" />}
          Choose a PDF to compare with…
          <input
            type="file"
            accept="application/pdf,.pdf"
            className="sr-only"
            onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) runCompare(f); }}
          />
        </label>
        {compare && (
          <div className="space-y-2 text-xs" data-testid="compare-result">
            <p>
              <span className="font-medium">{compareName}</span>: {compare.doc2_pages} page(s) vs {compare.doc1_pages} here.{" "}
              {compare.diffs.length === 0 ? "No text differences." : `${compare.diffs.length} page(s) differ.`}
            </p>
            {compare.diffs.map((d) => (
              <details key={d.page} className="rounded border border-gray-200 dark:border-gray-700">
                <summary className="cursor-pointer px-2 py-1">
                  Page {d.page + 1}: <span className="text-green-600 dark:text-green-400">+{d.lines_added}</span>{" "}
                  <span className="text-red-600 dark:text-red-400">-{d.lines_removed}</span>
                </summary>
                <pre className="max-h-60 overflow-auto px-2 py-1 font-mono text-[11px] leading-snug">
                  {d.diff.slice(2).map((line, i) => (
                    <div
                      key={i}
                      className={line.startsWith("+") ? "bg-green-50 dark:bg-green-900/30 text-green-800 dark:text-green-300" : line.startsWith("-") ? "bg-red-50 dark:bg-red-900/30 text-red-800 dark:text-red-300" : "text-gray-500"}
                    >
                      {line || " "}
                    </div>
                  ))}
                </pre>
              </details>
            ))}
          </div>
        )}
      </section>

      <section className={card}>
        <Title icon={Sparkles}>AI</Title>
        <div className="flex flex-wrap gap-2">
          <button className={btnSecondary} onClick={() => setAiPanelOpen(true)}>AI page actions</button>
          <button className={btnSecondary} onClick={() => { useEditorStore.getState().setChatOpen(true); addToast("Tip: Ctrl+/ toggles the AI chat", "info"); }}>Chat with this PDF</button>
        </div>
      </section>
    </>
  );
}
