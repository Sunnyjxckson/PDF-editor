"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { X, PenTool, Type, Upload, Eraser, Undo2, Check } from "lucide-react";
import {
  SCRIPT_FONTS,
  initialsFromName,
  newId,
  opaqueBounds,
  removeWhiteBackground,
  strokeWidth,
  type SignatureEntry,
  type SignatureKind,
} from "@/lib/features/sign";

interface Props {
  open: boolean;
  initialKind?: SignatureKind;
  defaultName?: string;
  onClose: () => void;
  onCreate: (entry: SignatureEntry) => void;
}

type Mode = "draw" | "type" | "upload";
interface Pt { x: number; y: number; t: number; p?: number }

const INKS = ["#000000", "#1e3a8a", "#1d4ed8", "#b91c1c"];
const PAD_W = 600;
const PAD_H = 220;

/** Trim a canvas to its opaque content (+padding) and return a PNG data URL. */
function trimCanvas(src: HTMLCanvasElement, pad = 6): { dataUrl: string; width: number; height: number } | null {
  const ctx = src.getContext("2d");
  if (!ctx) return null;
  const img = ctx.getImageData(0, 0, src.width, src.height);
  const b = opaqueBounds(img.data, src.width, src.height);
  if (!b) return null;
  const [x, y, w, h] = b;
  const sx = Math.max(0, x - pad), sy = Math.max(0, y - pad);
  const sw = Math.min(src.width - sx, w + pad * 2), sh = Math.min(src.height - sy, h + pad * 2);
  const out = document.createElement("canvas");
  out.width = sw;
  out.height = sh;
  out.getContext("2d")!.drawImage(src, sx, sy, sw, sh, 0, 0, sw, sh);
  return { dataUrl: out.toDataURL("image/png"), width: sw, height: sh };
}

export default function SignCreateDialog({ open, initialKind = "signature", defaultName = "", onClose, onCreate }: Props) {
  const [kind, setKind] = useState<SignatureKind>(initialKind);
  const [mode, setMode] = useState<Mode>("draw");
  const [ink, setInk] = useState(INKS[0]);
  const [name, setName] = useState(defaultName);
  const [fontId, setFontId] = useState(SCRIPT_FONTS[0].id);
  const [threshold, setThreshold] = useState(200);
  const [uploadImg, setUploadImg] = useState<HTMLImageElement | null>(null);
  const [error, setError] = useState<string | null>(null);

  const padRef = useRef<HTMLCanvasElement>(null);
  const previewRef = useRef<HTMLCanvasElement>(null);
  const strokesRef = useRef<Pt[][]>([]);
  const currentRef = useRef<Pt[] | null>(null);
  const lastWidthRef = useRef<number | null>(null);
  const [strokeCount, setStrokeCount] = useState(0);

  // Reset when the dialog (re)opens (derived during render, no effect needed).
  const [wasOpen, setWasOpen] = useState(open);
  const [lastInitialKind, setLastInitialKind] = useState(initialKind);
  if (open !== wasOpen || (open && initialKind !== lastInitialKind)) {
    setWasOpen(open);
    setLastInitialKind(initialKind);
    if (open) {
      setKind(initialKind);
      setError(null);
    }
  }

  // ─── Draw mode ──────────────────────────────────────────────────────
  const baseWidth = kind === "initials" ? 4 : 3.2;

  const redrawAll = useCallback(() => {
    const c = padRef.current;
    const ctx = c?.getContext("2d");
    if (!c || !ctx) return;
    ctx.clearRect(0, 0, c.width, c.height);
    for (const s of strokesRef.current) drawStroke(ctx, s, ink, baseWidth);
  }, [ink, baseWidth]);

  useEffect(() => {
    if (open && mode === "draw") redrawAll();
  }, [open, mode, redrawAll]);

  const padPoint = (e: React.PointerEvent<HTMLCanvasElement>): Pt => {
    const r = e.currentTarget.getBoundingClientRect();
    return {
      x: ((e.clientX - r.left) / r.width) * PAD_W,
      y: ((e.clientY - r.top) / r.height) * PAD_H,
      t: e.timeStamp || performance.now(),
      p: e.pointerType === "pen" ? e.pressure : undefined,
    };
  };

  const onPadDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    currentRef.current = [padPoint(e)];
    lastWidthRef.current = null;
    const ctx = padRef.current?.getContext("2d");
    if (ctx) {
      const p = currentRef.current[0];
      ctx.fillStyle = ink;
      ctx.beginPath();
      ctx.arc(p.x, p.y, baseWidth / 2, 0, Math.PI * 2);
      ctx.fill();
    }
  };

  const onPadMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const cur = currentRef.current;
    if (!cur) return;
    // Use coalesced events for high-frequency pens/mice when available.
    const native = e.nativeEvent as PointerEvent;
    const evs = typeof native.getCoalescedEvents === "function" ? native.getCoalescedEvents() : [];
    const pts: Pt[] = evs.length
      ? evs.map((ce) => {
          const r = e.currentTarget.getBoundingClientRect();
          return {
            x: ((ce.clientX - r.left) / r.width) * PAD_W,
            y: ((ce.clientY - r.top) / r.height) * PAD_H,
            t: ce.timeStamp,
            p: ce.pointerType === "pen" ? ce.pressure : undefined,
          };
        })
      : [padPoint(e)];
    const ctx = padRef.current?.getContext("2d");
    for (const p of pts) {
      const prev = cur[cur.length - 1];
      if (Math.hypot(p.x - prev.x, p.y - prev.y) < 0.8) continue;
      cur.push(p);
      if (ctx && cur.length >= 3) {
        const a = cur[cur.length - 3], b = cur[cur.length - 2], c = cur[cur.length - 1];
        const v = Math.hypot(c.x - b.x, c.y - b.y) / Math.max(1, c.t - b.t);
        const w = strokeWidth(baseWidth, v, c.p, lastWidthRef.current);
        lastWidthRef.current = w;
        (b as Pt & { w?: number }).w = w;
        segment(ctx, a, b, c, ink, w);
      }
    }
  };

  const onPadUp = () => {
    const cur = currentRef.current;
    if (!cur) return;
    currentRef.current = null;
    strokesRef.current.push(cur);
    setStrokeCount(strokesRef.current.length);
    redrawAll(); // re-render with the final smoothed path
  };

  const clearPad = () => {
    strokesRef.current = [];
    setStrokeCount(0);
    redrawAll();
  };
  const undoStroke = () => {
    strokesRef.current.pop();
    setStrokeCount(strokesRef.current.length);
    redrawAll();
  };

  // ─── Type mode ──────────────────────────────────────────────────────
  const typedText = kind === "initials" ? (name.length <= 4 ? name : initialsFromName(name)) : name;
  const fontStack = SCRIPT_FONTS.find((f) => f.id === fontId)?.stack ?? "cursive";

  const renderTyped = useCallback((): HTMLCanvasElement | null => {
    if (!typedText.trim()) return null;
    const size = 110;
    const c = document.createElement("canvas");
    const ctx = c.getContext("2d");
    if (!ctx) return null;
    ctx.font = `${size}px ${fontStack}`;
    const w = Math.ceil(ctx.measureText(typedText).width) + size;
    c.width = Math.min(w, 4000);
    c.height = Math.round(size * 1.8);
    ctx.font = `${size}px ${fontStack}`; // reset after resize
    ctx.fillStyle = ink;
    ctx.textBaseline = "middle";
    ctx.fillText(typedText, size / 2, c.height / 2);
    return c;
  }, [typedText, fontStack, ink]);

  // ─── Upload mode ────────────────────────────────────────────────────
  const processUpload = useCallback((): HTMLCanvasElement | null => {
    if (!uploadImg) return null;
    const maxW = 1200;
    const scale = Math.min(1, maxW / uploadImg.naturalWidth);
    const c = document.createElement("canvas");
    c.width = Math.max(1, Math.round(uploadImg.naturalWidth * scale));
    c.height = Math.max(1, Math.round(uploadImg.naturalHeight * scale));
    const ctx = c.getContext("2d");
    if (!ctx) return null;
    ctx.drawImage(uploadImg, 0, 0, c.width, c.height);
    const data = ctx.getImageData(0, 0, c.width, c.height);
    removeWhiteBackground(data.data, threshold, 40);
    ctx.putImageData(data, 0, 0);
    return c;
  }, [uploadImg, threshold]);

  const onFile = (f: File | undefined) => {
    setError(null);
    if (!f) return;
    if (!f.type.startsWith("image/")) {
      setError("Choose an image file (PNG, JPG, …)");
      return;
    }
    const url = URL.createObjectURL(f);
    const img = new Image();
    img.onload = () => {
      setUploadImg(img);
      URL.revokeObjectURL(url);
    };
    img.onerror = () => setError("Could not read that image");
    img.src = url;
  };

  // Live preview for type/upload modes on a checkerboard so transparency is visible.
  useEffect(() => {
    if (!open || mode === "draw") return;
    const pv = previewRef.current;
    const ctx = pv?.getContext("2d");
    if (!pv || !ctx) return;
    ctx.clearRect(0, 0, pv.width, pv.height);
    const src = mode === "type" ? renderTyped() : processUpload();
    if (!src) return;
    const s = Math.min(pv.width / src.width, pv.height / src.height, 1.5);
    const w = src.width * s, h = src.height * s;
    ctx.drawImage(src, (pv.width - w) / 2, (pv.height - h) / 2, w, h);
  }, [open, mode, renderTyped, processUpload]);

  const save = () => {
    setError(null);
    let src: HTMLCanvasElement | null = null;
    if (mode === "draw") src = strokesRef.current.length ? padRef.current : null;
    else if (mode === "type") src = renderTyped();
    else src = processUpload();
    if (!src) {
      setError(mode === "draw" ? "Draw your signature first" : mode === "type" ? "Type your name first" : "Upload an image first");
      return;
    }
    const trimmed = trimCanvas(src);
    if (!trimmed) {
      setError("Nothing visible to save — try a lower background threshold");
      return;
    }
    onCreate({
      id: newId(),
      kind,
      dataUrl: trimmed.dataUrl,
      width: trimmed.width,
      height: trimmed.height,
      label: mode === "type" ? typedText : undefined,
      createdAt: Date.now(),
    });
    clearPad();
    onClose();
  };

  if (!open) return null;

  const tabBtn = (m: Mode, Icon: typeof PenTool, label: string) => (
    <button
      key={m}
      onClick={() => setMode(m)}
      className={`flex-1 flex items-center justify-center gap-1.5 py-2 text-sm rounded-lg transition-colors ${
        mode === m
          ? "bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 font-medium"
          : "text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800"
      }`}
    >
      <Icon className="w-4 h-4" /> {label}
    </button>
  );

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/40 p-2" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-label="Create signature"
        className="w-full max-w-2xl bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 border border-gray-200 dark:border-gray-700 rounded-xl shadow-2xl p-4"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <div className="flex gap-1 bg-gray-100 dark:bg-gray-800 rounded-lg p-0.5">
            {(["signature", "initials"] as const).map((k) => (
              <button
                key={k}
                onClick={() => setKind(k)}
                className={`px-3 py-1 text-sm rounded-md capitalize ${
                  kind === k ? "bg-white dark:bg-gray-700 shadow font-medium" : "text-gray-500"
                }`}
              >
                {k}
              </button>
            ))}
          </div>
          <button onClick={onClose} aria-label="Close" className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex gap-1 mb-3">
          {tabBtn("draw", PenTool, "Draw")}
          {tabBtn("type", Type, "Type")}
          {tabBtn("upload", Upload, "Upload")}
        </div>

        {mode !== "upload" && (
          <div className="flex items-center gap-2 mb-2">
            <span className="text-xs text-gray-500">Ink</span>
            {INKS.map((c) => (
              <button
                key={c}
                aria-label={`Ink ${c}`}
                onClick={() => setInk(c)}
                className={`w-5 h-5 rounded-full border-2 ${ink === c ? "border-blue-500 scale-110" : "border-transparent"}`}
                style={{ backgroundColor: c }}
              />
            ))}
          </div>
        )}

        {mode === "draw" && (
          <div>
            <div className="relative rounded-lg border-2 border-dashed border-gray-300 dark:border-gray-600 bg-white">
              <canvas
                ref={padRef}
                width={PAD_W}
                height={PAD_H}
                className="w-full touch-none cursor-crosshair block"
                style={{ aspectRatio: `${PAD_W} / ${PAD_H}` }}
                onPointerDown={onPadDown}
                onPointerMove={onPadMove}
                onPointerUp={onPadUp}
                onPointerCancel={onPadUp}
              />
              <div className="pointer-events-none absolute left-6 right-6 bottom-10 border-b border-gray-300" />
              {strokeCount === 0 && (
                <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-gray-400 text-sm">
                  Sign here with mouse, finger or stylus
                </div>
              )}
            </div>
            <div className="flex gap-2 mt-2">
              <button onClick={undoStroke} disabled={!strokeCount} className="flex items-center gap-1 px-2 py-1 text-xs rounded hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40">
                <Undo2 className="w-3.5 h-3.5" /> Undo stroke
              </button>
              <button onClick={clearPad} disabled={!strokeCount} className="flex items-center gap-1 px-2 py-1 text-xs rounded hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40">
                <Eraser className="w-3.5 h-3.5" /> Clear
              </button>
            </div>
          </div>
        )}

        {mode === "type" && (
          <div className="space-y-2">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={kind === "initials" ? "Initials or full name" : "Full name"}
              className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-sm"
              autoFocus
            />
            <div className="grid grid-cols-2 gap-2">
              {SCRIPT_FONTS.map((f) => (
                <button
                  key={f.id}
                  onClick={() => setFontId(f.id)}
                  className={`px-3 py-2 rounded-lg border text-2xl truncate text-left ${
                    fontId === f.id ? "border-blue-500 bg-blue-50 dark:bg-blue-900/30" : "border-gray-200 dark:border-gray-700"
                  }`}
                  style={{ fontFamily: f.stack, color: ink }}
                  title={f.label}
                >
                  {typedText || f.label}
                </button>
              ))}
            </div>
          </div>
        )}

        {mode === "upload" && (
          <div className="space-y-2">
            <label className="flex items-center justify-center gap-2 px-3 py-3 rounded-lg border-2 border-dashed border-gray-300 dark:border-gray-600 cursor-pointer text-sm hover:bg-gray-50 dark:hover:bg-gray-800">
              <Upload className="w-4 h-4" /> Choose a photo or scan of your signature
              <input type="file" accept="image/*" className="hidden" onChange={(e) => onFile(e.target.files?.[0])} />
            </label>
            <label className="flex items-center gap-2 text-xs text-gray-600 dark:text-gray-400">
              Background removal
              <input type="range" min={120} max={250} value={threshold} onChange={(e) => setThreshold(Number(e.target.value))} className="flex-1" />
              <span className="w-8 text-right">{threshold}</span>
            </label>
          </div>
        )}

        {mode !== "draw" && (
          <canvas
            ref={previewRef}
            width={PAD_W}
            height={160}
            className="w-full mt-2 rounded-lg border border-gray-200 dark:border-gray-700"
            style={{
              backgroundImage:
                "linear-gradient(45deg,#e5e7eb 25%,transparent 25%),linear-gradient(-45deg,#e5e7eb 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#e5e7eb 75%),linear-gradient(-45deg,transparent 75%,#e5e7eb 75%)",
              backgroundSize: "16px 16px",
              backgroundPosition: "0 0,0 8px,8px -8px,-8px 0",
              backgroundColor: "#fff",
            }}
          />
        )}

        {error && <p className="mt-2 text-xs text-red-600">{error}</p>}

        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onClose} className="px-3 py-1.5 text-sm rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800">
            Cancel
          </button>
          <button onClick={save} className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-blue-600 text-white hover:bg-blue-700">
            <Check className="w-4 h-4" /> Save {kind}
          </button>
        </div>
      </div>
    </div>
  );
}

// Quadratic segment through midpoints: smooth curves from discrete samples.
function segment(ctx: CanvasRenderingContext2D, a: Pt, b: Pt, c: Pt, color: string, width: number) {
  const m1 = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  const m2 = { x: (b.x + c.x) / 2, y: (b.y + c.y) / 2 };
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.beginPath();
  ctx.moveTo(m1.x, m1.y);
  ctx.quadraticCurveTo(b.x, b.y, m2.x, m2.y);
  ctx.stroke();
}

function drawStroke(ctx: CanvasRenderingContext2D, s: Pt[], color: string, base: number) {
  if (s.length === 1 || s.length === 2) {
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(s[0].x, s[0].y, base / 2, 0, Math.PI * 2);
    ctx.fill();
    if (s.length === 2) segment(ctx, s[0], s[0], s[1], color, base);
    return;
  }
  let prevW: number | null = null;
  // lead-in from the first point to the first midpoint
  segment(ctx, s[0], s[0], s[1], color, base);
  for (let i = 2; i < s.length; i++) {
    const a = s[i - 2], b = s[i - 1], c = s[i];
    const stored = (b as Pt & { w?: number }).w;
    const v = Math.hypot(c.x - b.x, c.y - b.y) / Math.max(1, c.t - b.t);
    const w: number = stored ?? strokeWidth(base, v, c.p, prevW);
    prevW = w;
    segment(ctx, a, b, c, color, w);
  }
  const n = s.length;
  segment(ctx, s[n - 2], s[n - 1], s[n - 1], color, prevW ?? base);
}
