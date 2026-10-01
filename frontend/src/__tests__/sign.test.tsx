import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent, screen, act } from "@testing-library/react";
import {
  addToLibrary,
  applySignItems,
  clampRect,
  clientToPdf,
  defaultRectAt,
  digitalSign,
  dragRect,
  initialsFromName,
  loadLibrary,
  opaqueBounds,
  removeWhiteBackground,
  saveLibrary,
  strokeWidth,
  toApiItems,
  useSignStore,
  validateUploadedPdf,
  LIBRARY_KEY,
  type PlacedItem,
  type SignatureEntry,
} from "@/lib/features/sign";
import SignOverlay from "@/components/features/SignOverlay";

const mockFetch = vi.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ok = (data: unknown) => ({ ok: true, json: () => Promise.resolve(data) });

beforeEach(() => {
  mockFetch.mockReset();
  localStorage.clear();
  useSignStore.setState({ active: false, tool: null, items: [], library: [], selectedItemId: null,
    selectedEntryId: { signature: null, initials: null } });
});

describe("geometry helpers", () => {
  it("converts client pixels to PDF points via the overlay box (zoom independent)", () => {
    const box = { left: 100, top: 50, width: 1224, height: 1584 }; // 2x of 612x792
    expect(clientToPdf(100 + 612, 50 + 792, box, 612, 792)).toEqual({ x: 306, y: 396 });
    const half = { left: 0, top: 0, width: 306, height: 396 }; // 0.5x zoom
    expect(clientToPdf(153, 198, half, 612, 792)).toEqual({ x: 306, y: 396 });
  });

  it("centres signatures on the click, keeps aspect, and clamps into the page", () => {
    const r = defaultRectAt("signature", { x: 300, y: 400 }, 612, 792, 4);
    expect(r[2] - r[0]).toBeCloseTo(160);
    expect(r[3] - r[1]).toBeCloseTo(40);
    expect((r[0] + r[2]) / 2).toBeCloseTo(300);
    const edge = defaultRectAt("signature", { x: 605, y: 790 }, 612, 792, 4);
    expect(edge[2]).toBeLessThanOrEqual(612);
    expect(edge[3]).toBeLessThanOrEqual(792);
    expect(edge[0]).toBeGreaterThanOrEqual(0);
  });

  it("clampRect keeps size and stays inside the page", () => {
    expect(clampRect([-10, -5, 40, 15], 612, 792)).toEqual([0, 0, 50, 20]);
    expect(clampRect([600, 780, 650, 800], 612, 792)).toEqual([562, 772, 612, 792]);
  });

  it("dragRect moves, resizes and preserves aspect ratio", () => {
    expect(dragRect([10, 10, 110, 60], "move", 5, -5, false)).toEqual([15, 5, 115, 55]);
    expect(dragRect([10, 10, 110, 60], "se", 20, 0, false)).toEqual([10, 10, 130, 60]);
    const a = dragRect([10, 10, 110, 60], "se", 100, 0, true);
    expect((a[2] - a[0]) / (a[3] - a[1])).toBeCloseTo(2);
    expect(a[0]).toBe(10);
    const nw = dragRect([10, 10, 110, 60], "nw", -100, 0, true); // anchored at se corner
    expect(nw[2]).toBe(110);
    expect(nw[3]).toBe(60);
    expect((nw[2] - nw[0]) / (nw[3] - nw[1])).toBeCloseTo(2);
    const tiny = dragRect([10, 10, 110, 60], "se", -500, -500, false);
    expect(tiny[2] - tiny[0]).toBeGreaterThanOrEqual(6);
  });

  it("derives initials", () => {
    expect(initialsFromName("jane q. public")).toBe("JQP");
    expect(initialsFromName("  Mary-Kate Olsen ")).toBe("MKO");
  });
});

describe("pixel helpers", () => {
  it("removes a white paper background but keeps dark ink", () => {
    // [white, light grey, mid grey (soft edge), black]
    const px = new Uint8ClampedArray([255, 255, 255, 255, 230, 230, 230, 255, 180, 180, 180, 255, 10, 10, 10, 255]);
    removeWhiteBackground(px, 200, 40);
    expect(px[3]).toBe(0);
    expect(px[7]).toBe(0);
    expect(px[11]).toBeGreaterThan(0);
    expect(px[11]).toBeLessThan(255);
    expect(px[15]).toBe(255);
  });

  it("finds the opaque bounding box", () => {
    const w = 5, h = 4;
    const px = new Uint8ClampedArray(w * h * 4);
    px[(1 * w + 2) * 4 + 3] = 255;
    px[(2 * w + 3) * 4 + 3] = 255;
    expect(opaqueBounds(px, w, h)).toEqual([2, 1, 2, 2]);
    expect(opaqueBounds(new Uint8ClampedArray(16), 2, 2)).toBeNull();
  });

  it("stroke width: slower = thicker, pen pressure respected, smoothed", () => {
    const slow = strokeWidth(3, 0.1, undefined, null);
    const fast = strokeWidth(3, 3.5, undefined, null);
    expect(slow).toBeGreaterThan(fast);
    expect(strokeWidth(3, 1, 1.0, null)).toBeGreaterThan(strokeWidth(3, 1, 0.1, null));
    const smoothed = strokeWidth(3, 3.5, undefined, slow);
    expect(smoothed).toBeGreaterThan(fast);
    expect(smoothed).toBeLessThan(slow);
  });
});

describe("signature library", () => {
  const entry = (id: string): SignatureEntry => ({
    id, kind: "signature", dataUrl: "data:image/png;base64,AAAA", width: 10, height: 5, createdAt: 1,
  });

  it("round-trips through localStorage and ignores junk", () => {
    expect(loadLibrary()).toEqual([]);
    saveLibrary([entry("a")]);
    expect(loadLibrary().map((e) => e.id)).toEqual(["a"]);
    localStorage.setItem(LIBRARY_KEY, "{not json");
    expect(loadLibrary()).toEqual([]);
    localStorage.setItem(LIBRARY_KEY, JSON.stringify([{ id: "x", dataUrl: "javascript:alert(1)" }, entry("b")]));
    expect(loadLibrary().map((e) => e.id)).toEqual(["b"]);
  });

  it("addToLibrary puts newest first and de-duplicates", () => {
    const lib = addToLibrary(addToLibrary([], entry("a")), entry("b"));
    expect(lib.map((e) => e.id)).toEqual(["b", "a"]);
    expect(addToLibrary(lib, entry("a")).map((e) => e.id)).toEqual(["a", "b"]);
  });
});

describe("API client", () => {
  const items: PlacedItem[] = [
    { id: "1", type: "image", page: 0, rect: [1.234, 2, 3, 4], image: "data:image/png;base64,QUJD" },
    { id: "2", type: "text", page: 1, rect: [0, 0, 10, 10], text: "Hi", fontSize: 12, color: "#000000" },
  ];

  it("toApiItems strips data-URL prefixes and maps field names", () => {
    const api = toApiItems(items);
    expect(api[0]).toEqual({ type: "image", page: 0, rect: [1.23, 2, 3, 4], image: "QUJD" });
    expect(api[1]).toEqual({ type: "text", page: 1, rect: [0, 0, 10, 10], text: "Hi", font_size: 12, color: "#000000" });
  });

  it("applySignItems posts to /sign/apply with lock flag", async () => {
    mockFetch.mockResolvedValueOnce(ok({ status: "ok", applied: { image: 1, text: 1 }, locked: true, lock: null }));
    await applySignItems("doc-1", items, true);
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe("http://localhost:8000/api/pdf/doc-1/sign/apply");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body);
    expect(body.lock).toBe(true);
    expect(body.items).toHaveLength(2);
  });

  it("surfaces the server's error detail", async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 409, json: () => Promise.resolve({ detail: "Document carries a digital signature" }) });
    await expect(applySignItems("d", items)).rejects.toThrow("Document carries a digital signature");
  });

  it("digitalSign sends snake_case body", async () => {
    mockFetch.mockResolvedValueOnce(ok({ status: "ok", field_name: "Signature1", signer: "J", certified: false, visible: true }));
    await digitalSign("d", { certId: "c", page: 2, rect: [1, 2, 3, 4], image: "data:image/png;base64,Zm9v", reason: "ok", lock: true });
    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(body).toMatchObject({ cert_id: "c", page: 2, rect: [1, 2, 3, 4], image: "Zm9v", reason: "ok", lock: true, field_name: null });
  });

  it("validateUploadedPdf posts multipart", async () => {
    mockFetch.mockResolvedValueOnce(ok({ signature_count: 0, signatures: [], empty_signature_fields: [] }));
    await validateUploadedPdf(new File(["%PDF"], "a.pdf"));
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe("http://localhost:8000/api/pdf/signing/validate");
    expect(init.body).toBeInstanceOf(FormData);
  });
});

// jsdom has no PointerEvent constructor: dispatch a MouseEvent named "pointerdown" so clientX/Y survive.
function pdown(el: Element, clientX: number, clientY: number) {
  fireEvent(el, new MouseEvent("pointerdown", { bubbles: true, cancelable: true, clientX, clientY }));
}

describe("SignOverlay", () => {
  function mountOverlay() {
    const utils = render(
      <div style={{ position: "relative" }}>
        <SignOverlay pageIndex={1} pageWidthPt={612} pageHeightPt={792} />
      </div>,
    );
    const overlay = screen.getByTestId("sign-overlay");
    // jsdom has no layout: pretend the page renders at 2x (1224 x 1584 px) at (0,0)
    overlay.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 1224, height: 1584, right: 1224, bottom: 1584, x: 0, y: 0, toJSON() {} }) as DOMRect;
    return { ...utils, overlay };
  }

  it("renders nothing while the sign panel is inactive", () => {
    render(<SignOverlay pageIndex={0} pageWidthPt={612} pageHeightPt={792} />);
    expect(screen.queryByTestId("sign-overlay")).toBeNull();
  });

  it("places a checkmark in PDF points at the clicked spot and lets it be dragged", () => {
    act(() => useSignStore.setState({ active: true, tool: "check" }));
    const { overlay } = mountOverlay();
    pdown(overlay, 400, 600); // = (200pt, 300pt)
    const items = useSignStore.getState().items;
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ type: "check", page: 1 });
    expect(items[0].rect).toEqual([192, 292, 208, 308]);

    // drag it 20pt right / 10pt down (40px / 20px at 2x)
    const box = overlay.querySelector("div.absolute.group") as HTMLElement;
    pdown(box, 400, 600);
    act(() => {
      window.dispatchEvent(new MouseEvent("pointermove", { clientX: 440, clientY: 620 }));
      window.dispatchEvent(new MouseEvent("pointerup"));
    });
    expect(useSignStore.getState().items[0].rect).toEqual([212, 302, 228, 318]);
  });

  it("places the selected library signature with its aspect ratio and then disarms the tool", () => {
    act(() =>
      useSignStore.setState({
        active: true,
        tool: "signature",
        library: [{ id: "s1", kind: "signature", dataUrl: "data:image/png;base64,AAAA", width: 400, height: 100, createdAt: 1 }],
        selectedEntryId: { signature: "s1", initials: null },
      }),
    );
    const { overlay } = mountOverlay();
    pdown(overlay, 612, 792); // centre (306, 396)
    const [it] = useSignStore.getState().items;
    expect(it.type).toBe("image");
    expect(it.image).toBe("data:image/png;base64,AAAA");
    expect((it.rect[2] - it.rect[0]) / (it.rect[3] - it.rect[1])).toBeCloseTo(4);
    expect((it.rect[0] + it.rect[2]) / 2).toBeCloseTo(306);
    expect(useSignStore.getState().tool).toBeNull();
  });

  it("Delete key removes the selected item", () => {
    act(() => useSignStore.setState({ active: true, tool: "cross" }));
    const { overlay } = mountOverlay();
    pdown(overlay, 100, 100);
    expect(useSignStore.getState().items).toHaveLength(1);
    fireEvent.keyDown(window, { key: "Delete" });
    expect(useSignStore.getState().items).toHaveLength(0);
  });
});
