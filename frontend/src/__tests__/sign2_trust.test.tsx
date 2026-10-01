import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import {
  digitalSign,
  importCertificate,
  isSelfSignedId,
  isValidTsaUrl,
  loadSignPrefs,
  saveSignPrefs,
  signatureFacts,
  useSignStore,
  validateDocumentSignatures,
  validateUploadedPdf,
  DEFAULT_TSA_URL,
  SELF_SIGNED_NOTE,
  SIGN_PREFS_KEY,
  CERT_KEY,
  type SignatureValidation,
} from "@/lib/features/sign";
import SignPanel from "@/components/features/SignPanel";

const mockFetch = vi.fn();
global.fetch = mockFetch as unknown as typeof fetch;
const ok = (data: unknown) => ({ ok: true, json: () => Promise.resolve(data) });
const DOC = "11111111-1111-4111-8111-111111111111";

beforeEach(() => {
  mockFetch.mockReset();
  localStorage.clear();
  useSignStore.setState({ active: false, tool: null, items: [], library: [], selectedItemId: null });
});

const base: SignatureValidation = {
  field_name: "Signature1",
  signer_name: "Casey Trusted",
  signer_email: "casey@acme.test",
  issuer: "Common Name: Test Root CA",
  self_signed: false,
  intact: true,
  valid: true,
  trusted: true,
  trust_source: "user",
  trust_anchor: "Common Name: Test Root CA",
  modified_after_signing: false,
  summary: "Valid: document unchanged since signing",
  timestamped: true,
  timestamp_time: "2026-10-01T12:00:00+00:00",
  timestamp_trusted: true,
};

describe("signatureFacts", () => {
  it("answers signed by / issuer / trusted / modified / timestamped", () => {
    const f = signatureFacts(base);
    expect(f.map((x) => x.label)).toEqual(["Signed by", "Issuer", "Trusted?", "Modified since signing?", "Timestamped?"]);
    expect(f[0].value).toBe("Casey Trusted <casey@acme.test>");
    expect(f[2]).toMatchObject({ tone: "good" });
    expect(f[2].value).toContain("a certificate you trusted");
    expect(f[3]).toEqual({ label: "Modified since signing?", value: "No", tone: "good" });
    expect(f[4].value.startsWith("Yes")).toBe(true);
  });

  it("is honest about self-signed, unknown issuer, revoked and tampering", () => {
    const self = signatureFacts({ ...base, trusted: false, self_signed: true, trust_problem: "self_signed", timestamped: false });
    expect(self[2].value).toMatch(/self-signed/i);
    expect(self[2].value).toMatch(/Acrobat/);
    expect(self[4]).toMatchObject({ tone: "neutral" });
    expect(self[4].value.startsWith("No")).toBe(true);
    const unknown = signatureFacts({ ...base, trusted: false, trust_problem: "unknown_issuer" });
    expect(unknown[2].value).toMatch(/not in the trust store/);
    const revoked = signatureFacts({ ...base, trusted: false, revoked: true, revocation_checked: true, trust_problem: "revoked" });
    expect(revoked[2]).toMatchObject({ tone: "bad" });
    expect(revoked.at(-1)).toMatchObject({ label: "Revocation", value: "Revoked", tone: "bad" });
    const tampered = signatureFacts({ ...base, intact: false, modified_after_signing: true, summary: "INVALID: altered" });
    expect(tampered[3]).toMatchObject({ tone: "bad" });
  });
});

describe("helpers", () => {
  it("validates TSA URLs", () => {
    expect(isValidTsaUrl(DEFAULT_TSA_URL)).toBe(true);
    expect(isValidTsaUrl("https://freetsa.org/tsr")).toBe(true);
    expect(isValidTsaUrl("ftp://x")).toBe(false);
    expect(isValidTsaUrl("not a url")).toBe(false);
  });

  it("treats legacy IDs without a source as self-signed", () => {
    expect(isSelfSignedId({})).toBe(true);
    expect(isSelfSignedId({ source: "imported", self_signed: false })).toBe(false);
    expect(isSelfSignedId(null)).toBe(false);
  });

  it("persists prefs with timestamp/LTV/revocation OFF by default", () => {
    expect(loadSignPrefs()).toEqual({ timestamp: false, tsaUrl: DEFAULT_TSA_URL, ltv: false, fetchRevocation: false });
    saveSignPrefs({ timestamp: true, tsaUrl: "https://tsa.test/", ltv: true, fetchRevocation: true });
    expect(loadSignPrefs()).toEqual({ timestamp: true, tsaUrl: "https://tsa.test/", ltv: true, fetchRevocation: true });
    localStorage.setItem(SIGN_PREFS_KEY, "{broken");
    expect(loadSignPrefs().timestamp).toBe(false);
  });
});

describe("API client", () => {
  it("sends timestamp / tsa_url / ltv when signing, nothing extra by default", async () => {
    mockFetch.mockResolvedValue(ok({ status: "ok" }));
    await digitalSign(DOC, { certId: "c", page: 0 });
    let body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(body).toMatchObject({ timestamp: false, tsa_url: null, ltv: false });
    await digitalSign(DOC, { certId: "c", page: 0, timestamp: true, tsaUrl: " https://tsa.test/ ", ltv: true });
    body = JSON.parse(mockFetch.mock.calls[1][1].body);
    expect(body).toMatchObject({ timestamp: true, tsa_url: "https://tsa.test/", ltv: true });
  });

  it("imports a .p12 with its passphrase as multipart", async () => {
    mockFetch.mockResolvedValue(ok({ cert_id: "x" }));
    const f = new File([new Uint8Array([1, 2, 3])], "id.p12");
    await importCertificate(f, "pw");
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe("http://localhost:8000/api/pdf/signing/certificates/import");
    expect((init.body as FormData).get("passphrase")).toBe("pw");
    expect((init.body as FormData).get("file")).toBeInstanceOf(File);
  });

  it("only asks for online revocation checks when enabled", async () => {
    mockFetch.mockResolvedValue(ok({ signature_count: 0, signatures: [], empty_signature_fields: [] }));
    await validateDocumentSignatures(DOC);
    await validateDocumentSignatures(DOC, { fetchRevocation: true });
    await validateUploadedPdf(new File(["%PDF"], "a.pdf"), { fetchRevocation: true });
    expect(mockFetch.mock.calls[0][0]).toBe(`http://localhost:8000/api/pdf/${DOC}/sign/validate`);
    expect(mockFetch.mock.calls[1][0]).toBe(`http://localhost:8000/api/pdf/${DOC}/sign/validate?fetch_revocation=true`);
    expect(mockFetch.mock.calls[2][0]).toBe("http://localhost:8000/api/pdf/signing/validate?fetch_revocation=true");
  });
});

function routeFetch(routes: Record<string, unknown>) {
  mockFetch.mockImplementation((url: string) => {
    for (const [k, v] of Object.entries(routes)) if (url.includes(k)) return Promise.resolve(ok(v));
    return Promise.resolve(ok({ signature_count: 0, signatures: [], empty_signature_fields: [] }));
  });
}

describe("SignPanel digital ID copy", () => {
  it("shows the one-line honest note and offers importing a CA-issued ID first", async () => {
    routeFetch({});
    render(<SignPanel docId={DOC} currentPage={0} onDocumentChanged={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Digital ID" }));
    expect(await screen.findByText(SELF_SIGNED_NOTE)).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Import CA-issued ID" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("button", { name: /Import digital ID/ })).toBeTruthy();
  });

  it("disables LTV for a self-signed ID and sends timestamp + custom TSA when chosen", async () => {
    localStorage.setItem(CERT_KEY, "cid");
    routeFetch({
      "/signing/certificates/cid": {
        cert_id: "cid", name: "Me", email: null, organization: null, serial: "1", fingerprint_sha256: "ab".repeat(32),
        not_before: "2026-01-01", not_after: "2030-01-01", passphrase_protected: false, created: "2026-01-01",
        source: "self_signed", self_signed: true, issuer: "Me", chain_length: 0,
      },
      "/sign/digital": { status: "ok", signer: "Me", certified: false, timestamped: true, ltv: false },
    });
    render(<SignPanel docId={DOC} currentPage={0} onDocumentChanged={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Digital ID" }));
    await screen.findByText("Self-signed ID");
    const ltv = screen.getByText("Make LTV-enabled").closest("label")!.querySelector("input")!;
    expect(ltv.disabled).toBe(true);
    fireEvent.click(screen.getByText("Add trusted timestamp").closest("label")!.querySelector("input")!);
    const tsa = screen.getByLabelText("Timestamp server URL") as HTMLInputElement;
    expect(tsa.value).toBe(DEFAULT_TSA_URL);
    fireEvent.change(tsa, { target: { value: "https://tsa.example.test/" } });
    // invisible signature so the button is enabled without placing an image
    fireEvent.change(screen.getByDisplayValue(/place a signature|First placed/), { target: { value: "none" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Sign digitally/ }));
    });
    await waitFor(() => expect(mockFetch.mock.calls.some(([u]) => String(u).includes("/sign/digital"))).toBe(true));
    const call = mockFetch.mock.calls.find(([u]) => String(u).includes("/sign/digital"))!;
    expect(JSON.parse(call[1].body)).toMatchObject({ timestamp: true, tsa_url: "https://tsa.example.test/", ltv: false });
  });
});
