/**
 * Integration wiring gates: things no single feature owner could test because
 * they live in the shell or span several clients.
 *  - every feature client routes its requests through api.ts's apiFetch, so the
 *    signed-document confirm flow works without relying on the global patch
 *  - the editor shell mounts <SignedDocGuard />
 *  - PageViewer mounts the AI citation highlight
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

vi.mock("@/lib/pdf-renderer", () => ({
  loadPdfDocument: vi.fn(),
  renderPage: vi.fn(),
  preRenderAdjacentPages: vi.fn(),
  getCachedTextBlocks: vi.fn(() => []),
  setCachedTextBlocks: vi.fn(),
  invalidateTextBlockCache: vi.fn(),
  getCurrentPdfDoc: vi.fn(),
  getCachedThumbnail: vi.fn(() => undefined),
  setCachedThumbnail: vi.fn(),
}));

import PageViewer from "@/components/PageViewer";
import { useEditorStore } from "@/lib/store";
import { useAIStore } from "@/lib/features/ai";
import { setSignedDocConfirmHandler, clearSignatureConsent, ALLOW_BREAK_SIGNATURE_HEADER } from "@/lib/api";
import { fillFormFields } from "@/lib/features/forms";
import { rotatePages } from "@/lib/features/organize";

const SRC = join(__dirname, "..");
const DOC = "0f8fad5b-d9cb-469f-a165-70867728950e";

function headerOf(init: RequestInit | undefined, name: string): string | null {
  return new Headers(init?.headers).get(name);
}

describe("feature clients use the shared apiFetch", () => {
  it("no feature client (except the AI client, which handles consent itself) calls bare fetch()", () => {
    const dir = join(SRC, "lib", "features");
    const offenders: string[] = [];
    for (const f of readdirSync(dir)) {
      if (!f.endsWith(".ts") || f === "ai.ts") continue;
      const code = readFileSync(join(dir, f), "utf8");
      if (/(?<![\w.])fetch\(/.test(code)) offenders.push(f);
      if (/const API_BASE\s*=/.test(code)) offenders.push(`${f} (own API_BASE)`);
    }
    expect(offenders).toEqual([]);
  });

  describe("a 409 signed_document from a feature call asks once, then retries with the override header", () => {
    const mockFetch = vi.fn();
    beforeEach(() => {
      clearSignatureConsent();
      mockFetch.mockReset();
      global.fetch = mockFetch as unknown as typeof fetch;
      const conflict = {
        ok: false,
        status: 409,
        clone() { return this; },
        // main.py's middleware answers with a top-level body, not FastAPI's {detail: ...}
        json: () => Promise.resolve({
          code: "signed_document", detail: "This PDF is digitally signed by Alice.", signers: ["Alice"],
        }),
      };
      mockFetch.mockImplementation((_url: string, init?: RequestInit) =>
        Promise.resolve(
          headerOf(init, ALLOW_BREAK_SIGNATURE_HEADER) === "1"
            ? { ok: true, status: 200, clone() { return this; }, json: () => Promise.resolve({ status: "ok" }) }
            : conflict,
        ),
      );
    });
    afterEach(() => setSignedDocConfirmHandler(null));

    it.each([
      ["forms", () => fillFormFields(DOC, { a: "b" })],
      ["organize", () => rotatePages(DOC, [0], 90)],
    ])("%s", async (_name, call) => {
      const ask = vi.fn(() => Promise.resolve("continue" as const));
      setSignedDocConfirmHandler(ask);
      await call();
      expect(ask).toHaveBeenCalledTimes(1);
      expect(mockFetch).toHaveBeenCalledTimes(2);
      expect(headerOf(mockFetch.mock.calls[1][1], ALLOW_BREAK_SIGNATURE_HEADER)).toBe("1");
    });
  });
});

describe("editor shell", () => {
  it("mounts the signed-document guard once", () => {
    const code = readFileSync(join(SRC, "components", "Editor.tsx"), "utf8");
    expect(code).toMatch(/import SignedDocGuard from "\.\/SignedDocGuard"/);
    expect(code.match(/<SignedDocGuard\s*\/>/g)).toHaveLength(1);
  });
});

describe("PageViewer shows AI citation highlights", () => {
  beforeEach(() => {
    global.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve([]) }) as unknown as typeof fetch;
    useEditorStore.getState().reset();
    useEditorStore.setState({
      docId: "doc-cite",
      document: { page_count: 2, pages: [{ width: 612, height: 792 }, { width: 612, height: 792 }] } as never,
      totalPages: 2,
      currentPage: 0,
      zoom: 1,
      zoomMode: "custom",
      activeTool: "select",
      renderMode: "image",
    });
    useAIStore.getState().setHighlight(null);
  });

  it("draws the highlight on the cited page only", () => {
    const { container } = render(<PageViewer />);
    const img = container.querySelector("img")!;
    act(() => {
      Object.defineProperty(img, "naturalWidth", { value: 1275, configurable: true });
      Object.defineProperty(img, "naturalHeight", { value: 1650, configurable: true });
      fireEvent.load(img);
    });
    act(() => useAIStore.getState().setHighlight({ page: 2, rects: [[72, 72, 200, 90]] as never, quote: null }));
    expect(screen.queryByTestId("ai-citation-overlay")).toBeNull();
    act(() => useAIStore.getState().setHighlight({ page: 1, rects: [[72, 72, 200, 90]] as never, quote: null }));
    expect(screen.getByTestId("ai-citation-overlay")).toBeTruthy();
  });
});
