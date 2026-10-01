/**
 * Signatures & Fill-and-Sign: API client, shared placement store and pure helpers.
 *
 * COORDINATES: every rect in this module is [x0, y0, x1, y1] in PDF points with a
 * TOP-LEFT origin in the page's *visible* frame (the frame of the rendered page
 * image; for rotated pages that is the rotated frame). The overlay converts
 * pointer pixels -> points using its own bounding box, so it is independent of
 * render DPI, render mode and CSS zoom.
 */
import { create } from "zustand";

import { API_BASE, apiFetch } from "../api";

// ─── Types ────────────────────────────────────────────────────────────────

export type Rect = [number, number, number, number];
export type SignItemType = "image" | "text" | "date" | "check" | "cross";
export type SignatureKind = "signature" | "initials";

export interface SignatureEntry {
  id: string;
  kind: SignatureKind;
  dataUrl: string; // transparent PNG
  width: number; // px
  height: number; // px
  label?: string;
  createdAt: number;
}

export interface PlacedItem {
  id: string;
  type: SignItemType;
  page: number;
  rect: Rect;
  image?: string; // data URL (type=image)
  text?: string;
  fontSize?: number;
  color?: string;
  keepAspect?: boolean;
  sourceKind?: SignatureKind;
}

export interface ApplyResult {
  status: string;
  applied: Record<string, number>;
  locked: boolean;
  lock: { annotations_flattened: number; fields_flattened: number } | null;
}

export interface CertificateInfo {
  cert_id: string;
  name: string;
  email: string | null;
  organization: string | null;
  serial: string;
  fingerprint_sha256: string;
  not_before: string;
  not_after: string;
  passphrase_protected: boolean;
  created: string;
  /** "self_signed" = generated here; "imported" = a .p12/.pfx the user uploaded */
  source?: "self_signed" | "imported";
  self_signed?: boolean;
  issuer?: string | null;
  chain_length?: number;
}

export interface SignatureValidation {
  field_name: string;
  signer_name?: string | null;
  signer_email?: string | null;
  signer_organization?: string | null;
  issuer?: string;
  self_signed?: boolean;
  issued_by_this_app?: boolean;
  signing_time?: string | null;
  intact: boolean;
  valid: boolean;
  trusted: boolean;
  coverage?: string | null;
  modification_level?: string | null;
  docmdp_ok?: boolean | null;
  certified?: boolean | null;
  reason?: string | null;
  location?: string | null;
  modified_after_signing: boolean | null;
  summary: string;
  trust_anchor?: string | null;
  /** "user" = a certificate trusted in this app; otherwise the system root source */
  trust_source?: string | null;
  trust_problem?: "self_signed" | "unknown_issuer" | "revoked" | string | null;
  revoked?: boolean;
  revocation_checked?: boolean;
  timestamped?: boolean;
  timestamp_time?: string | null;
  timestamp_valid?: boolean | null;
  timestamp_trusted?: boolean | null;
  ltv?: boolean;
}

export interface ValidationReport {
  signature_count: number;
  signatures: SignatureValidation[];
  empty_signature_fields: { field_name: string; page: number; rect: Rect }[];
  revocation_checked?: boolean;
  trust_store?: { system: string; user_certificates: number };
}

export interface TrustedCertificate {
  fingerprint_sha256: string;
  subject: string;
  issuer: string;
  self_signed: boolean;
  is_ca: boolean;
  not_after: string;
}

export interface TrustedList {
  user: TrustedCertificate[];
  system: { source: string; count: number };
}

export interface DigitalSignResult {
  status: string;
  field_name: string;
  signer: string;
  certified: boolean;
  visible: boolean;
  timestamped?: boolean;
  tsa_url?: string | null;
  ltv?: boolean;
  self_signed?: boolean;
}

export interface DigitalSignOptions {
  certId: string;
  passphrase?: string;
  page: number;
  rect?: Rect;
  image?: string;
  reason?: string;
  location?: string;
  fieldName?: string;
  showDetails?: boolean;
  lock?: boolean;
  /** RFC 3161 timestamp from tsaUrl (off by default; needs network) */
  timestamp?: boolean;
  tsaUrl?: string;
  /** Embed OCSP/CRL so the signature validates long-term (CA-issued IDs only) */
  ltv?: boolean;
}

export const DEFAULT_TSA_URL = "http://timestamp.digicert.com";

/** One-line honest explanation shown wherever a digital ID is chosen. */
export const SELF_SIGNED_NOTE =
  "Self-signed IDs prove the PDF wasn't changed, but Acrobat shows the signer as untrusted; import a CA-issued ID (.p12/.pfx) to fix that.";

// ─── API client ───────────────────────────────────────────────────────────

async function asJson<T>(res: Response, fallback: string): Promise<T> {
  if (!res.ok) {
    let detail = fallback;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

function stripDataUrl(s: string): string {
  const i = s.indexOf(",");
  return s.startsWith("data:") && i >= 0 ? s.slice(i + 1) : s;
}

export function toApiItems(items: PlacedItem[]) {
  return items.map((it) => ({
    type: it.type,
    page: it.page,
    rect: it.rect.map((v) => Math.round(v * 100) / 100),
    ...(it.type === "image" && it.image ? { image: stripDataUrl(it.image) } : {}),
    ...(it.text !== undefined ? { text: it.text } : {}),
    ...(it.fontSize ? { font_size: it.fontSize } : {}),
    ...(it.color ? { color: it.color } : {}),
  }));
}

export async function applySignItems(docId: string, items: PlacedItem[], lock = false): Promise<ApplyResult> {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/sign/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: toApiItems(items), lock }),
  });
  return asJson<ApplyResult>(res, "Failed to apply signatures");
}

export async function stampSignature(docId: string, page: number, rect: Rect, image: string, lock = false): Promise<ApplyResult> {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/sign/stamp`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page, rect, image: stripDataUrl(image), lock }),
  });
  return asJson<ApplyResult>(res, "Failed to stamp signature");
}

export async function lockDocument(docId: string): Promise<{ annotations_flattened: number; fields_flattened: number }> {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/sign/lock`, { method: "POST" });
  return asJson(res, "Failed to lock document");
}

export async function createCertificate(data: {
  name: string;
  email?: string;
  organization?: string;
  passphrase?: string;
}): Promise<CertificateInfo> {
  const res = await apiFetch(`${API_BASE}/api/pdf/signing/certificates`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: data.name,
      email: data.email || null,
      organization: data.organization || null,
      passphrase: data.passphrase || null,
    }),
  });
  return asJson<CertificateInfo>(res, "Failed to create digital ID");
}

export async function getCertificate(certId: string): Promise<CertificateInfo> {
  const res = await apiFetch(`${API_BASE}/api/pdf/signing/certificates/${certId}`);
  return asJson<CertificateInfo>(res, "Digital ID not found");
}

export function getCertificateDownloadUrl(certId: string): string {
  return `${API_BASE}/api/pdf/signing/certificates/${certId}/download`;
}

export async function deleteCertificate(certId: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/api/pdf/signing/certificates/${certId}`, { method: "DELETE" });
  await asJson(res, "Failed to delete digital ID");
}

export async function digitalSign(docId: string, o: DigitalSignOptions) {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/sign/digital`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      cert_id: o.certId,
      passphrase: o.passphrase || null,
      page: o.page,
      rect: o.rect ?? null,
      image: o.image ? stripDataUrl(o.image) : null,
      reason: o.reason || null,
      location: o.location || null,
      field_name: o.fieldName || null,
      show_details: o.showDetails ?? true,
      lock: !!o.lock,
      timestamp: !!o.timestamp,
      tsa_url: o.timestamp ? (o.tsaUrl || "").trim() || null : null,
      ltv: !!o.ltv,
    }),
  });
  return asJson<DigitalSignResult>(res, "Digital signing failed");
}

/** Import a CA-issued digital ID. The passphrase is sent once and never stored by the server. */
export async function importCertificate(file: File, passphrase: string): Promise<CertificateInfo> {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("passphrase", passphrase);
  const res = await apiFetch(`${API_BASE}/api/pdf/signing/certificates/import`, { method: "POST", body: fd });
  return asJson<CertificateInfo>(res, "Could not import the digital ID");
}

export async function listTrustedCertificates(): Promise<TrustedList> {
  const res = await apiFetch(`${API_BASE}/api/pdf/signing/trusted`);
  return asJson<TrustedList>(res, "Could not load trusted certificates");
}

export async function addTrustedCertificate(file: File): Promise<{ added: TrustedCertificate[] }> {
  const fd = new FormData();
  fd.append("file", file);
  const res = await apiFetch(`${API_BASE}/api/pdf/signing/trusted`, { method: "POST", body: fd });
  return asJson(res, "Could not trust this certificate");
}

export async function removeTrustedCertificate(fingerprint: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/api/pdf/signing/trusted/${fingerprint}`, { method: "DELETE" });
  await asJson(res, "Could not remove the trusted certificate");
}

function revocationQuery(fetchRevocation?: boolean): string {
  return fetchRevocation ? "?fetch_revocation=true" : "";
}

export async function validateDocumentSignatures(
  docId: string,
  opts: { fetchRevocation?: boolean } = {},
): Promise<ValidationReport> {
  const res = await apiFetch(`${API_BASE}/api/pdf/${docId}/sign/validate${revocationQuery(opts.fetchRevocation)}`);
  return asJson<ValidationReport>(res, "Validation failed");
}

export async function validateUploadedPdf(file: File, opts: { fetchRevocation?: boolean } = {}): Promise<ValidationReport> {
  const fd = new FormData();
  fd.append("file", file);
  const res = await apiFetch(`${API_BASE}/api/pdf/signing/validate${revocationQuery(opts.fetchRevocation)}`, {
    method: "POST",
    body: fd,
  });
  return asJson<ValidationReport>(res, "Validation failed");
}

// ─── Trust helpers (pure) ─────────────────────────────────────────────────

export function isValidTsaUrl(url: string): boolean {
  try {
    const u = new URL(url.trim());
    return (u.protocol === "http:" || u.protocol === "https:") && !!u.hostname;
  } catch {
    return false;
  }
}

export function isSelfSignedId(c: Pick<CertificateInfo, "source" | "self_signed"> | null | undefined): boolean {
  if (!c) return false;
  return c.self_signed ?? c.source !== "imported";
}

export type FactTone = "good" | "warn" | "bad" | "neutral";
export interface SignatureFact {
  label: string;
  value: string;
  tone: FactTone;
}

/** The five questions people ask about a signature, answered plainly. */
export function signatureFacts(s: SignatureValidation): SignatureFact[] {
  const who = [s.signer_name ?? "Unknown signer", s.signer_email ? `<${s.signer_email}>` : ""].filter(Boolean).join(" ");
  let trusted: SignatureFact;
  if (s.trusted) {
    const via = s.trust_source === "user" ? "a certificate you trusted" : "system root";
    trusted = { label: "Trusted?", value: `Yes, via ${via}${s.trust_anchor ? ` (${s.trust_anchor})` : ""}`, tone: "good" };
  } else if (s.revoked || s.trust_problem === "revoked") {
    trusted = { label: "Trusted?", value: "No: the certificate was revoked", tone: "bad" };
  } else if (s.self_signed || s.trust_problem === "self_signed") {
    trusted = { label: "Trusted?", value: "No: self-signed ID (Acrobat shows it as untrusted too)", tone: "warn" };
  } else {
    trusted = { label: "Trusted?", value: "No: issuer is not in the trust store", tone: "warn" };
  }
  const modified: SignatureFact =
    s.modified_after_signing === null
      ? { label: "Modified since signing?", value: "Unknown", tone: "neutral" }
      : !s.intact
        ? { label: "Modified since signing?", value: "Yes: signed content was altered", tone: "bad" }
        : s.modified_after_signing
          ? {
              label: "Modified since signing?",
              value: s.summary.startsWith("INVALID") ? "Yes: changes break the signature" : "Yes: later revisions were added",
              tone: s.summary.startsWith("INVALID") ? "bad" : "warn",
            }
          : { label: "Modified since signing?", value: "No", tone: "good" };
  const ts: SignatureFact = s.timestamped
    ? {
        label: "Timestamped?",
        value: `Yes${s.timestamp_time ? `, ${new Date(s.timestamp_time).toLocaleString()}` : ""}${s.timestamp_trusted ? "" : " (TSA not trusted)"}`,
        tone: s.timestamp_trusted ? "good" : "warn",
      }
    : { label: "Timestamped?", value: "No (time comes from the signer's computer)", tone: "neutral" };
  const facts: SignatureFact[] = [
    { label: "Signed by", value: who, tone: "neutral" },
    { label: "Issuer", value: s.issuer ?? "Unknown", tone: "neutral" },
    trusted,
    modified,
    ts,
  ];
  if (s.revocation_checked) {
    facts.push({ label: "Revocation", value: s.revoked ? "Revoked" : "Checked online: not revoked", tone: s.revoked ? "bad" : "good" });
  }
  if (s.ltv) facts.push({ label: "LTV", value: "Validation info embedded", tone: "good" });
  return facts;
}

// ─── Sign preferences (localStorage, per viewer) ──────────────────────────

export const SIGN_PREFS_KEY = "pdfeditor.sign.prefs.v1";
export interface SignPrefs {
  timestamp: boolean;
  tsaUrl: string;
  ltv: boolean;
  fetchRevocation: boolean;
}
export const DEFAULT_SIGN_PREFS: SignPrefs = { timestamp: false, tsaUrl: DEFAULT_TSA_URL, ltv: false, fetchRevocation: false };

export function loadSignPrefs(): SignPrefs {
  try {
    const raw = localStorage.getItem(SIGN_PREFS_KEY);
    if (!raw) return { ...DEFAULT_SIGN_PREFS };
    const p = JSON.parse(raw) ?? {};
    return {
      timestamp: p.timestamp === true,
      tsaUrl: typeof p.tsaUrl === "string" && p.tsaUrl.trim() ? p.tsaUrl : DEFAULT_TSA_URL,
      ltv: p.ltv === true,
      fetchRevocation: p.fetchRevocation === true,
    };
  } catch {
    return { ...DEFAULT_SIGN_PREFS };
  }
}

export function saveSignPrefs(p: SignPrefs) {
  try {
    localStorage.setItem(SIGN_PREFS_KEY, JSON.stringify(p));
  } catch {
    /* ignore */
  }
}

// ─── Signature library (localStorage) ─────────────────────────────────────

export const LIBRARY_KEY = "pdfeditor.sign.library.v1";
export const CERT_KEY = "pdfeditor.sign.certId.v1";
export const MAX_LIBRARY = 24;

export function loadLibrary(): SignatureEntry[] {
  try {
    const raw = localStorage.getItem(LIBRARY_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (e) => e && typeof e.id === "string" && typeof e.dataUrl === "string" && e.dataUrl.startsWith("data:image/"),
    );
  } catch {
    return [];
  }
}

export function saveLibrary(entries: SignatureEntry[]): boolean {
  try {
    localStorage.setItem(LIBRARY_KEY, JSON.stringify(entries.slice(0, MAX_LIBRARY)));
    return true;
  } catch {
    return false; // quota or blocked storage
  }
}

export function addToLibrary(entries: SignatureEntry[], entry: SignatureEntry): SignatureEntry[] {
  return [entry, ...entries.filter((e) => e.id !== entry.id)].slice(0, MAX_LIBRARY);
}

export function loadCertId(): string | null {
  try {
    return localStorage.getItem(CERT_KEY);
  } catch {
    return null;
  }
}

export function saveCertId(id: string | null) {
  try {
    if (id) localStorage.setItem(CERT_KEY, id);
    else localStorage.removeItem(CERT_KEY);
  } catch {
    /* ignore */
  }
}

// ─── Pure helpers: geometry ───────────────────────────────────────────────

export function newId(): string {
  return Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
}

/** Pointer position (client px) -> PDF points, given the overlay's client box. */
export function clientToPdf(
  clientX: number,
  clientY: number,
  box: { left: number; top: number; width: number; height: number },
  pageWidthPt: number,
  pageHeightPt: number,
): { x: number; y: number } {
  return {
    x: ((clientX - box.left) / box.width) * pageWidthPt,
    y: ((clientY - box.top) / box.height) * pageHeightPt,
  };
}

export function clampRect(r: Rect, pageW: number, pageH: number): Rect {
  const w = Math.min(r[2] - r[0], pageW);
  const h = Math.min(r[3] - r[1], pageH);
  const x0 = Math.min(Math.max(0, r[0]), pageW - w);
  const y0 = Math.min(Math.max(0, r[1]), pageH - h);
  return [x0, y0, x0 + w, y0 + h];
}

export const DEFAULT_SIZES: Record<string, { w: number; h: number }> = {
  signature: { w: 160, h: 50 },
  initials: { w: 60, h: 34 },
  text: { w: 160, h: 18 },
  date: { w: 80, h: 16 },
  check: { w: 16, h: 16 },
  cross: { w: 16, h: 16 },
};

/** Rect centred on a click, sized for the item kind (images keep their aspect). */
export function defaultRectAt(
  kind: keyof typeof DEFAULT_SIZES,
  at: { x: number; y: number },
  pageW: number,
  pageH: number,
  aspect?: number, // width / height of an image
): Rect {
  let { w, h } = DEFAULT_SIZES[kind];
  if (aspect && aspect > 0 && (kind === "signature" || kind === "initials")) {
    h = w / aspect;
    const maxH = kind === "signature" ? 70 : 40;
    if (h > maxH) {
      h = maxH;
      w = h * aspect;
    }
  }
  const isText = kind === "text" || kind === "date";
  // Text anchors at its top-left (like typing); everything else centres on the click.
  const x0 = isText ? at.x : at.x - w / 2;
  const y0 = isText ? at.y - h / 2 : at.y - h / 2;
  return clampRect([x0, y0, x0 + w, y0 + h], pageW, pageH);
}

export type Handle = "move" | "nw" | "ne" | "sw" | "se";

/** Apply a drag delta (points) to a rect for a given handle. */
export function dragRect(orig: Rect, handle: Handle, dx: number, dy: number, keepAspect: boolean, minSize = 6): Rect {
  const [x0, y0, x1, y1] = orig;
  if (handle === "move") return [x0 + dx, y0 + dy, x1 + dx, y1 + dy];
  let nx0 = x0, ny0 = y0, nx1 = x1, ny1 = y1;
  if (handle.includes("w")) nx0 = Math.min(x0 + dx, x1 - minSize);
  if (handle.includes("e")) nx1 = Math.max(x1 + dx, x0 + minSize);
  if (handle.includes("n")) ny0 = Math.min(y0 + dy, y1 - minSize);
  if (handle.includes("s")) ny1 = Math.max(y1 + dy, y0 + minSize);
  if (keepAspect) {
    const aspect = (x1 - x0) / (y1 - y0);
    const w = nx1 - nx0;
    const h = Math.max(w / aspect, minSize);
    const fw = h * aspect;
    // anchor at the corner opposite the handle
    if (handle.includes("w")) nx0 = nx1 - fw; else nx1 = nx0 + fw;
    if (handle.includes("n")) ny0 = ny1 - h; else ny1 = ny0 + h;
  }
  return [nx0, ny0, nx1, ny1];
}

export function initialsFromName(name: string): string {
  return name
    .trim()
    .split(/[\s-]+/)
    .filter(Boolean)
    .map((p) => p[0]!.toUpperCase())
    .join("")
    .slice(0, 4);
}

export function formatToday(d = new Date(), fmt: "us" | "iso" | "long" = "us"): string {
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  if (fmt === "iso") return `${d.getFullYear()}-${mm}-${dd}`;
  if (fmt === "long") return d.toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric" });
  return `${mm}/${dd}/${d.getFullYear()}`;
}

// ─── Pure helpers: pixels ─────────────────────────────────────────────────

/**
 * Make near-white pixels transparent (in place) with a soft edge, so a photo or
 * scan of a signature on paper becomes a clean transparent stamp.
 * Luminance >= threshold -> alpha 0; luminance <= threshold - softness -> unchanged.
 * Remaining ink is darkened slightly to counter paper-grey cast.
 */
export function removeWhiteBackground(data: Uint8ClampedArray, threshold = 200, softness = 40): Uint8ClampedArray {
  const lo = Math.max(0, threshold - softness);
  for (let i = 0; i < data.length; i += 4) {
    const lum = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
    if (lum >= threshold) {
      data[i + 3] = 0;
    } else if (lum > lo) {
      const keep = (threshold - lum) / (threshold - lo);
      data[i + 3] = Math.round(data[i + 3] * keep);
    }
  }
  return data;
}

/** Bounding box [x, y, w, h] of pixels with alpha > alphaMin, or null if empty. */
export function opaqueBounds(
  data: Uint8ClampedArray,
  width: number,
  height: number,
  alphaMin = 8,
): [number, number, number, number] | null {
  let minX = width, minY = height, maxX = -1, maxY = -1;
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      if (data[(y * width + x) * 4 + 3] > alphaMin) {
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
  }
  if (maxX < 0) return null;
  return [minX, minY, maxX - minX + 1, maxY - minY + 1];
}

/**
 * Pen width for a stroke segment: pressure from a real stylus wins; otherwise
 * faster movement -> thinner line, like ink. Smoothed against the previous width.
 */
export function strokeWidth(
  base: number,
  velocityPxPerMs: number,
  pressure: number | undefined,
  prevWidth: number | null,
): number {
  let target: number;
  if (pressure !== undefined && pressure > 0 && pressure !== 0.5) {
    target = base * (0.4 + pressure * 1.2);
  } else {
    const v = Math.min(Math.max(velocityPxPerMs, 0), 4);
    target = base * (1.35 - v * 0.22); // 1.35x when still, ~0.47x when fast
  }
  target = Math.max(base * 0.35, Math.min(base * 1.6, target));
  return prevWidth === null ? target : prevWidth * 0.6 + target * 0.4;
}

export const SCRIPT_FONTS: { id: string; label: string; stack: string }[] = [
  { id: "brush", label: "Brush", stack: '"Brush Script MT", "Brush Script Std", "Segoe Script", cursive' },
  { id: "chancery", label: "Chancery", stack: '"Apple Chancery", "Lucida Handwriting", "URW Chancery L", cursive' },
  { id: "snell", label: "Roundhand", stack: '"Snell Roundhand", "Savoye LET", "Edwardian Script ITC", "Segoe Script", cursive' },
  { id: "hand", label: "Handwritten", stack: '"Bradley Hand", "Segoe Print", "Noteworthy", "Comic Sans MS", cursive' },
];

// ─── Shared placement store (panel <-> page overlay) ──────────────────────

export type SignTool = null | "signature" | "initials" | "text" | "date" | "check" | "cross";

interface SignState {
  active: boolean; // panel open => overlay interactive
  tool: SignTool;
  selectedEntryId: { signature: string | null; initials: string | null };
  library: SignatureEntry[];
  items: PlacedItem[];
  selectedItemId: string | null;
  inkColor: string;
  setActive: (v: boolean) => void;
  setTool: (t: SignTool) => void;
  setLibrary: (l: SignatureEntry[]) => void;
  selectEntry: (kind: SignatureKind, id: string | null) => void;
  addItem: (it: PlacedItem) => void;
  updateItem: (id: string, patch: Partial<PlacedItem>) => void;
  removeItem: (id: string) => void;
  selectItem: (id: string | null) => void;
  clearItems: () => void;
  setInkColor: (c: string) => void;
}

export const useSignStore = create<SignState>((set) => ({
  active: false,
  tool: null,
  selectedEntryId: { signature: null, initials: null },
  library: [],
  items: [],
  selectedItemId: null,
  inkColor: "#000000",
  setActive: (active) => set(active ? { active } : { active, tool: null, selectedItemId: null }),
  setTool: (tool) => set({ tool }),
  setLibrary: (library) => set({ library }),
  selectEntry: (kind, id) => set((s) => ({ selectedEntryId: { ...s.selectedEntryId, [kind]: id } })),
  addItem: (it) => set((s) => ({ items: [...s.items, it], selectedItemId: it.id })),
  updateItem: (id, patch) => set((s) => ({ items: s.items.map((i) => (i.id === id ? { ...i, ...patch } : i)) })),
  removeItem: (id) =>
    set((s) => ({ items: s.items.filter((i) => i.id !== id), selectedItemId: s.selectedItemId === id ? null : s.selectedItemId })),
  selectItem: (selectedItemId) => set({ selectedItemId }),
  clearItems: () => set({ items: [], selectedItemId: null }),
  setInkColor: (inkColor) => set({ inkColor }),
}));
