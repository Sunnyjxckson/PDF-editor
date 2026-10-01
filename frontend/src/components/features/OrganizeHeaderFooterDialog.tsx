"use client";

// Header & footer / page numbers / Bates numbering dialog with a live preview.
// The preview text is computed client-side by planHeaderFooter(), a mirror of
// the backend's _plan_header_footer, so it updates on every keystroke.
// Margins are entered in points (1/72 in) and drawn proportionally on a
// miniature of the preview page (page size in PDF points from /info).

import { useMemo, useState } from "react";
import { Hash, Loader2, PanelTop, Stamp, X } from "lucide-react";
import {
  addBates, addHeaderFooter, addPageNumbers, planHeaderFooter, positionToSlot, parsePageRanges,
  hexToRgb01, FONT_LABELS, HF_SLOTS, type Base14Font, type HFPosition, type HFSlot, type HeaderFooterOptions,
} from "@/lib/features/organize";

export interface OrganizeHeaderFooterDialogProps {
  docId: string;
  pageCount: number;
  open: boolean;
  onClose: () => void;
  onDocumentChanged: () => void;
  /** Visible size of the first page in PDF points, for the preview miniature. Defaults to Letter. */
  pageWidth?: number;
  pageHeight?: number;
  initialTab?: Tab;
}

type Tab = "header" | "numbers" | "bates";

const POSITIONS: HFPosition[] = ["top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right"];
const SLOT_LABEL: Record<HFSlot, string> = {
  header_left: "Header left", header_center: "Header center", header_right: "Header right",
  footer_left: "Footer left", footer_center: "Footer center", footer_right: "Footer right",
};
const PRESETS = ["Page {n} of {total}", "Page {n}", "{n}", "- {n} -", "{n} / {total}"];

const input =
  "px-2 py-1 rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 " +
  "text-gray-800 dark:text-gray-200 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500";

export default function OrganizeHeaderFooterDialog({
  docId, pageCount, open, onClose, onDocumentChanged, pageWidth = 612, pageHeight = 792, initialTab = "numbers",
}: OrganizeHeaderFooterDialogProps) {
  const [tab, setTab] = useState<Tab>(initialTab);
  // shared style
  const [font, setFont] = useState<Base14Font>("helv");
  const [fontSize, setFontSize] = useState(10);
  const [color, setColor] = useState("#000000");
  const [mt, setMt] = useState(36);
  const [mb, setMb] = useState(36);
  const [ml, setMl] = useState(72);
  const [mr, setMr] = useState(72);
  const [skipFirst, setSkipFirst] = useState(false);
  const [rangeText, setRangeText] = useState("");
  // header/footer
  const [slots, setSlots] = useState<Partial<Record<HFSlot, string>>>({});
  const [startNumber, setStartNumber] = useState(1);
  // numbers
  const [format, setFormat] = useState("Page {n} of {total}");
  const [numPos, setNumPos] = useState<HFPosition>("bottom-center");
  // bates
  const [prefix, setPrefix] = useState("");
  const [suffix, setSuffix] = useState("");
  const [digits, setDigits] = useState(6);
  const [batesStart, setBatesStart] = useState(1);
  const [batesPos, setBatesPos] = useState<HFPosition>("bottom-right");
  const [previewIdx, setPreviewIdx] = useState(0);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const pages = useMemo(() => {
    if (!rangeText.trim()) return { value: null as number[] | null, error: null as string | null };
    try {
      return { value: parsePageRanges(rangeText, pageCount), error: null };
    } catch (e) {
      return { value: null, error: e instanceof Error ? e.message : String(e) };
    }
  }, [rangeText, pageCount]);

  const opts: HeaderFooterOptions = useMemo(() => {
    const base: HeaderFooterOptions = {
      font, font_size: fontSize, color: hexToRgb01(color),
      margin_top: mt, margin_bottom: mb, margin_left: ml, margin_right: mr,
      skip_first: skipFirst, pages: pages.value,
    };
    if (tab === "header") return { ...base, ...slots, start_number: startNumber };
    if (tab === "numbers") return { ...base, [positionToSlot(numPos)]: format, start_number: startNumber };
    return {
      ...base, [positionToSlot(batesPos)]: "{bates}",
      bates_prefix: prefix, bates_suffix: suffix, bates_digits: digits, bates_start: batesStart,
    };
  }, [tab, font, fontSize, color, mt, mb, ml, mr, skipFirst, pages.value, slots, startNumber, numPos, format, batesPos, prefix, suffix, digits, batesStart]);

  const plan = useMemo(() => planHeaderFooter(opts, pageCount), [opts, pageCount]);
  const current = plan[Math.min(previewIdx, Math.max(0, plan.length - 1))];

  if (!open) return null;

  const apply = async () => {
    if (pages.error) return setError(pages.error);
    if (!plan.length) return setError("No pages would be stamped");
    if (tab === "header" && !HF_SLOTS.some((s) => slots[s])) return setError("Enter text for at least one header or footer slot");
    setBusy(true);
    setError(null);
    const style = {
      font, font_size: fontSize, color: hexToRgb01(color),
      margin_top: mt, margin_bottom: mb, margin_left: ml, margin_right: mr,
      skip_first: skipFirst, pages: pages.value,
    };
    try {
      if (tab === "header") await addHeaderFooter(docId, opts);
      else if (tab === "numbers") await addPageNumbers(docId, { ...style, format, position: numPos, start_number: startNumber });
      else await addBates(docId, { ...style, prefix, suffix, digits, start: batesStart, position: batesPos });
      onDocumentChanged();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  // miniature preview geometry
  const MINI_W = 220;
  const k = MINI_W / pageWidth;
  const MINI_H = pageHeight * k;
  const previewFont = Math.max(5, fontSize * k);
  const fontFamily = font.startsWith("ti") ? "Times New Roman, serif" : font.startsWith("co") ? "Courier New, monospace" : "Helvetica, Arial, sans-serif";
  const bold = ["hebo", "hebi", "tibo", "tibi", "cobo", "cobi"].includes(font);
  const italic = ["heit", "hebi", "tiit", "tibi", "coit", "cobi"].includes(font);

  const tabBtn = (t: Tab, label: string, Icon: typeof Hash) => (
    <button
      onClick={() => { setTab(t); setPreviewIdx(0); }}
      className={`flex items-center gap-1.5 px-3 py-2 text-xs font-medium border-b-2 ${tab === t
        ? "border-blue-600 text-blue-700 dark:text-blue-300"
        : "border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200"}`}
    >
      <Icon className="w-3.5 h-3.5" />{label}
    </button>
  );

  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label="Header, footer and page numbers">
      <div className="w-full max-w-3xl max-h-[92vh] overflow-auto rounded-xl bg-white dark:bg-gray-900 shadow-2xl border border-gray-200 dark:border-gray-800">
        <div className="flex items-center px-4 pt-3 border-b border-gray-200 dark:border-gray-800">
          {tabBtn("numbers", "Page numbers", Hash)}
          {tabBtn("header", "Header & footer", PanelTop)}
          {tabBtn("bates", "Bates numbering", Stamp)}
          <button onClick={onClose} className="ml-auto p-1.5 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800" aria-label="Close">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="grid md:grid-cols-[1fr_auto] gap-5 p-4 text-xs text-gray-700 dark:text-gray-300">
          <div className="space-y-3">
            {tab === "numbers" && (
              <>
                <label className="block">Format <span className="text-gray-400">({"{n}"} = page, {"{total}"} = count, {"{date}"})</span>
                  <input className={`${input} w-full mt-1`} value={format} onChange={(e) => setFormat(e.target.value)} />
                </label>
                <div className="flex flex-wrap gap-1">
                  {PRESETS.map((p) => (
                    <button key={p} onClick={() => setFormat(p)} className="px-2 py-0.5 rounded border border-gray-300 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800">{p}</button>
                  ))}
                </div>
                <PositionGrid value={numPos} onChange={setNumPos} />
                <label className="flex items-center gap-2">Start at
                  <input type="number" className={`${input} w-20`} value={startNumber} onChange={(e) => setStartNumber(parseInt(e.target.value, 10) || 0)} />
                </label>
              </>
            )}
            {tab === "header" && (
              <>
                <p className="text-gray-500 dark:text-gray-400">Tokens: {"{n}"} page number, {"{total}"} page count, {"{date}"} today, {"{bates}"} Bates number.</p>
                <div className="grid grid-cols-3 gap-2">
                  {HF_SLOTS.map((s) => (
                    <label key={s} className="block">{SLOT_LABEL[s]}
                      <input className={`${input} w-full mt-1`} value={slots[s] ?? ""} onChange={(e) => setSlots({ ...slots, [s]: e.target.value })} />
                    </label>
                  ))}
                </div>
                <label className="flex items-center gap-2">{"{n}"} starts at
                  <input type="number" className={`${input} w-20`} value={startNumber} onChange={(e) => setStartNumber(parseInt(e.target.value, 10) || 0)} />
                </label>
              </>
            )}
            {tab === "bates" && (
              <>
                <div className="grid grid-cols-2 gap-2">
                  <label className="block">Prefix<input className={`${input} w-full mt-1`} value={prefix} onChange={(e) => setPrefix(e.target.value)} placeholder="ACME-" /></label>
                  <label className="block">Suffix<input className={`${input} w-full mt-1`} value={suffix} onChange={(e) => setSuffix(e.target.value)} /></label>
                  <label className="block">Digits<input type="number" min={1} max={12} className={`${input} w-full mt-1`} value={digits} onChange={(e) => setDigits(Math.min(12, Math.max(1, parseInt(e.target.value, 10) || 1)))} /></label>
                  <label className="block">Start number<input type="number" min={0} className={`${input} w-full mt-1`} value={batesStart} onChange={(e) => setBatesStart(Math.max(0, parseInt(e.target.value, 10) || 0))} /></label>
                </div>
                <PositionGrid value={batesPos} onChange={setBatesPos} />
              </>
            )}

            <fieldset className="border-t border-gray-200 dark:border-gray-800 pt-3 grid grid-cols-2 sm:grid-cols-4 gap-2">
              <label className="block col-span-2">Font
                <select className={`${input} w-full mt-1`} value={font} onChange={(e) => setFont(e.target.value as Base14Font)}>
                  {(Object.keys(FONT_LABELS) as Base14Font[]).map((f) => <option key={f} value={f}>{FONT_LABELS[f]}</option>)}
                </select>
              </label>
              <label className="block">Size (pt)<input type="number" min={4} max={72} className={`${input} w-full mt-1`} value={fontSize} onChange={(e) => setFontSize(Number(e.target.value) || 10)} /></label>
              <label className="block">Colour<input type="color" className="block mt-1 w-full h-7 rounded border border-gray-300 dark:border-gray-700" value={color} onChange={(e) => setColor(e.target.value)} /></label>
              <label className="block">Top margin<input type="number" min={0} className={`${input} w-full mt-1`} value={mt} onChange={(e) => setMt(Number(e.target.value) || 0)} /></label>
              <label className="block">Bottom margin<input type="number" min={0} className={`${input} w-full mt-1`} value={mb} onChange={(e) => setMb(Number(e.target.value) || 0)} /></label>
              <label className="block">Left margin<input type="number" min={0} className={`${input} w-full mt-1`} value={ml} onChange={(e) => setMl(Number(e.target.value) || 0)} /></label>
              <label className="block">Right margin<input type="number" min={0} className={`${input} w-full mt-1`} value={mr} onChange={(e) => setMr(Number(e.target.value) || 0)} /></label>
              <label className="block col-span-2">Pages <span className="text-gray-400">(blank = all)</span>
                <input className={`${input} w-full mt-1`} value={rangeText} onChange={(e) => setRangeText(e.target.value)} placeholder="e.g. 2-10" />
              </label>
              <label className="flex items-center gap-1.5 col-span-2 mt-4"><input type="checkbox" checked={skipFirst} onChange={(e) => setSkipFirst(e.target.checked)} /> Skip first page</label>
            </fieldset>
            <p className="text-[11px] text-gray-400">Margins are in points (72 pt = 1 inch). Text is written into the page content, like Acrobat&apos;s header/footer; use Undo to remove it.</p>
          </div>

          {/* live preview */}
          <div className="flex flex-col items-center gap-2">
            <div className="relative bg-white shadow ring-1 ring-gray-300 dark:ring-gray-700" style={{ width: MINI_W, height: MINI_H }} data-testid="hf-preview-page">
              <div className="absolute border border-dashed border-blue-300/70" style={{ left: ml * k, right: mr * k, top: mt * k, bottom: mb * k }} />
              {current && (Object.entries(current.texts) as [HFSlot, string][]).map(([slot, text]) => {
                const isHeader = slot.startsWith("header");
                const h = slot.split("_")[1];
                const style: React.CSSProperties = {
                  position: "absolute", fontSize: previewFont, color, fontFamily, whiteSpace: "nowrap",
                  fontWeight: bold ? 700 : 400, fontStyle: italic ? "italic" : "normal", lineHeight: 1,
                };
                if (isHeader) style.top = mt * k; else style.bottom = mb * k;
                if (h === "left") style.left = ml * k;
                else if (h === "right") style.right = mr * k;
                else { style.left = "50%"; style.transform = "translateX(-50%)"; }
                return <span key={slot} style={style} data-testid={`hf-preview-${slot}`}>{text}</span>;
              })}
              {!current && <span className="absolute inset-0 flex items-center justify-center text-gray-400">No pages selected</span>}
            </div>
            {current && (
              <div className="flex items-center gap-2 text-gray-500 dark:text-gray-400">
                <button disabled={previewIdx <= 0} onClick={() => setPreviewIdx((i) => i - 1)} className="px-1.5 disabled:opacity-30">&lt;</button>
                <span>Preview: PDF page {current.page + 1}</span>
                <button disabled={previewIdx >= plan.length - 1} onClick={() => setPreviewIdx((i) => i + 1)} className="px-1.5 disabled:opacity-30">&gt;</button>
              </div>
            )}
            <span className="text-gray-500 dark:text-gray-400">{plan.length} page(s) will be stamped</span>
          </div>
        </div>

        <div className="flex items-center gap-2 px-4 py-3 border-t border-gray-200 dark:border-gray-800">
          {(error || pages.error) && <span className="text-xs text-red-600 dark:text-red-400">{error || pages.error}</span>}
          <div className="flex-1" />
          <button onClick={onClose} className="px-3 py-1.5 rounded-lg text-xs text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800">Cancel</button>
          <button onClick={apply} disabled={busy} className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50 inline-flex items-center gap-1.5">
            {busy && <Loader2 className="w-3.5 h-3.5 animate-spin" />} Apply to {plan.length} page(s)
          </button>
        </div>
      </div>
    </div>
  );
}

function PositionGrid({ value, onChange }: { value: HFPosition; onChange: (p: HFPosition) => void }) {
  return (
    <div className="flex items-center gap-3">
      <span>Position</span>
      <div className="grid grid-cols-3 gap-1 p-1 rounded-md ring-1 ring-gray-200 dark:ring-gray-700 w-28">
        {POSITIONS.map((p) => (
          <button
            key={p} title={p} aria-label={p} aria-pressed={value === p} onClick={() => onChange(p)}
            className={`h-4 rounded-sm ${value === p ? "bg-blue-600" : "bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600"} ${p.startsWith("bottom") ? "mt-6" : ""}`}
          />
        ))}
      </div>
    </div>
  );
}
