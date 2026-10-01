import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import {
  applyDrag,
  clientToPdf,
  normalizeBbox,
  bboxToPercentStyle,
  intersectBbox,
  hexToRgb01,
  defaultInsertRect,
  clampToPage,
  listObjects,
  moveImage,
  addShape,
  replaceImage,
  getImageExtractUrl,
  type ImageObject,
  type PageObjects,
} from "@/lib/features/objects";
import ObjectsOverlay from "@/components/features/ObjectsOverlay";

// jsdom has no PointerEvent; without this fireEvent.pointerDown carries no `button`.
if (typeof window.PointerEvent === "undefined") {
  class PointerEventPolyfill extends MouseEvent {}
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).PointerEvent = PointerEventPolyfill;
}

const mockFetch = vi.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ok = (data: unknown) => ({ ok: true, json: () => Promise.resolve(data) });

/** List requests get the page objects; every mutation gets `mutation`. */
function routeFetch(mutation: unknown) {
  mockFetch.mockImplementation((url: string) =>
    Promise.resolve(/\/objects\/\d+$/.test(String(url)) ? ok(PAGE) : ok(mutation)),
  );
}

const IMG: ImageObject = {
  id: "img-7-0",
  kind: "image",
  xref: 7,
  occurrence: 0,
  bbox: [100, 200, 300, 300],
  width: 40,
  height: 20,
  colorspace: "DeviceRGB",
  bpc: 8,
  size: 100,
  has_mask: false,
  names: ["fzImg0"],
  editable: true,
  method: "stream",
};

const PAGE: PageObjects = {
  page: 0,
  page_width: 612,
  page_height: 792,
  rotation: 0,
  images: [IMG],
  drawings: [
    {
      id: "drw-0",
      kind: "drawing",
      index: 0,
      bbox: [100, 500, 200, 560],
      path_count: 1,
      seqnos: [2],
      stroke: [1, 0, 0],
      fill: [0, 0, 1],
      width: 2,
      background: false,
    },
  ],
};

beforeEach(() => mockFetch.mockReset());

describe("objects geometry helpers", () => {
  it("clientToPdf maps screen pixels to PDF points regardless of zoom", () => {
    // page rendered at 1224x1584 px (2x) starting at (10, 20)
    const r = { left: 10, top: 20, width: 1224, height: 1584 };
    expect(clientToPdf(10 + 612, 20 + 792, r, 612, 792)).toEqual([306, 396]);
    expect(clientToPdf(10, 20, r, 612, 792)).toEqual([0, 0]);
  });

  it("applyDrag moves and resizes from each handle", () => {
    const b: [number, number, number, number] = [100, 100, 200, 150];
    expect(applyDrag(b, "move", 10, -5)).toEqual([110, 95, 210, 145]);
    expect(applyDrag(b, "se", 20, 10)).toEqual([100, 100, 220, 160]);
    expect(applyDrag(b, "nw", -10, -10)).toEqual([90, 90, 200, 150]);
    expect(applyDrag(b, "e", 50, 999)).toEqual([100, 100, 250, 150]);
    // cannot invert past the opposite edge
    expect(applyDrag(b, "w", 500, 0)[0]).toBeLessThan(200);
  });

  it("applyDrag keeps aspect ratio on corner drags when asked", () => {
    const b: [number, number, number, number] = [0, 0, 200, 100];
    const r = applyDrag(b, "se", 100, 0, true);
    expect((r[2] - r[0]) / (r[3] - r[1])).toBeCloseTo(2);
  });

  it("normalize / intersect / percent / clamp / color / insert rect", () => {
    expect(normalizeBbox([10, 20, 0, 5])).toEqual([0, 5, 10, 20]);
    expect(intersectBbox([0, 0, 10, 10], [5, 5, 20, 20])).toEqual([5, 5, 10, 10]);
    expect(intersectBbox([0, 0, 10, 10], [50, 50, 60, 60])).toBeNull();
    expect(bboxToPercentStyle([61.2, 79.2, 306, 396], 612, 792)).toEqual({
      left: "10%",
      top: "10%",
      width: "40%",
      height: "40%",
    });
    expect(clampToPage([-500, 0, -400, 10], 612, 792)[0]).toBe(-90);
    expect(hexToRgb01("#ff8000")).toEqual([1, 128 / 255, 0]);
    expect(hexToRgb01("#fff")).toEqual([1, 1, 1]);
    const ins = defaultInsertRect(2000, 1000, 612, 792);
    expect(ins[2] - ins[0]).toBeCloseTo(306);
    expect((ins[0] + ins[2]) / 2).toBeCloseTo(306);
  });
});

describe("objects API client", () => {
  it("lists objects for a page", async () => {
    mockFetch.mockResolvedValueOnce(ok(PAGE));
    const res = await listObjects("doc1", 0);
    expect(mockFetch.mock.calls[0][0]).toMatch(/\/api\/pdf\/doc1\/objects\/0$/);
    expect(res.images[0].xref).toBe(7);
  });

  it("moveImage posts the placement and new bbox", async () => {
    mockFetch.mockResolvedValueOnce(ok({ status: "ok" }));
    await moveImage("doc1", 0, IMG, [1, 2, 3, 4]);
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toMatch(/\/objects\/image\/move$/);
    expect(JSON.parse(init.body)).toEqual({ page: 0, xref: 7, occurrence: 0, bbox: [100, 200, 300, 300], new_bbox: [1, 2, 3, 4] });
  });

  it("addShape sends snake_case options", async () => {
    mockFetch.mockResolvedValueOnce(ok({ status: "ok" }));
    await addShape("doc1", 2, "arrow", { start: [1, 2], end: [30, 40] }, { strokeColor: [1, 0, 0], fillColor: null, width: 3 });
    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(body).toMatchObject({ page: 2, type: "arrow", start: [1, 2], end: [30, 40], stroke_color: [1, 0, 0], fill_color: null, width: 3, dashed: false });
  });

  it("replaceImage uploads multipart form data", async () => {
    mockFetch.mockResolvedValueOnce(ok({ status: "ok" }));
    const f = new File(["x"], "a.png", { type: "image/png" });
    await replaceImage("doc1", 0, IMG, f, "all");
    const fd = mockFetch.mock.calls[0][1].body as FormData;
    expect(fd.get("xref")).toBe("7");
    expect(fd.get("scope")).toBe("all");
    expect(fd.get("file")).toBeInstanceOf(File);
  });

  it("surfaces the backend's error detail", async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 409, json: () => Promise.resolve({ detail: "Image has changed" }) });
    await expect(moveImage("doc1", 0, IMG, [0, 0, 5, 5])).rejects.toThrow("Image has changed");
  });

  it("builds extract URLs", () => {
    expect(getImageExtractUrl("d", 7, "png")).toMatch(/\/api\/pdf\/d\/objects\/image\/7\/extract\?format=png$/);
  });
});

describe("ObjectsOverlay component", () => {
  function setupOverlayRect() {
    const el = screen.getByTestId("objects-overlay");
    // rendered at 2px per PDF point (e.g. 144 dpi or 2x zoom), at the viewport origin
    el.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 1224, height: 1584, right: 1224, bottom: 1584, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
    return el;
  }

  it("renders a box per object positioned in page percentages", async () => {
    mockFetch.mockResolvedValue(ok(PAGE));
    render(<ObjectsOverlay docId="doc1" currentPage={0} onDocumentChanged={() => {}} />);
    const img = await screen.findByTestId("obj-img-7-0");
    expect(img.style.left).toBe(`${(100 / 612) * 100}%`);
    expect(img.style.width).toBe(`${(200 / 612) * 100}%`);
    expect(screen.getByTestId("obj-drw-0")).toBeInTheDocument();
  });

  it("dragging an image commits a move with the dragged bbox and refreshes", async () => {
    mockFetch.mockResolvedValue(ok(PAGE));
    const changed = vi.fn();
    render(<ObjectsOverlay docId="doc1" currentPage={0} onDocumentChanged={changed} />);
    const box = await screen.findByTestId("obj-img-7-0");
    setupOverlayRect();
    mockFetch.mockClear();
    routeFetch({ status: "ok", method: "stream", bbox: [150, 230, 350, 330] });

    fireEvent.pointerDown(box, { button: 0, clientX: 300, clientY: 500 });
    await act(async () => {
      window.dispatchEvent(new MouseEvent("pointermove", { clientX: 400, clientY: 560 }));
    });
    await act(async () => {
      window.dispatchEvent(new MouseEvent("pointerup", { clientX: 400, clientY: 560 }));
    });

    await waitFor(() => expect(changed).toHaveBeenCalled());
    const moveCall = mockFetch.mock.calls.find((c) => String(c[0]).endsWith("/objects/image/move"));
    expect(moveCall).toBeTruthy();
    expect(JSON.parse(moveCall![1].body).new_bbox).toEqual([150, 230, 350, 330]);
  });

  it("selecting an image shows image actions; Delete removes it", async () => {
    mockFetch.mockResolvedValue(ok(PAGE));
    render(<ObjectsOverlay docId="doc1" currentPage={0} onDocumentChanged={() => {}} />);
    const box = await screen.findByTestId("obj-img-7-0");
    setupOverlayRect();
    fireEvent.pointerDown(box, { button: 0, clientX: 150, clientY: 250 });
    await act(async () => {
      window.dispatchEvent(new MouseEvent("pointerup", { clientX: 150, clientY: 250 }));
    });
    expect(screen.getByLabelText("Rotate")).toBeInTheDocument();
    expect(screen.getByLabelText("Crop")).toBeInTheDocument();
    expect(screen.getByLabelText("Download image").getAttribute("href")).toMatch(/objects\/image\/7\/extract/);
    // a click without movement must not call move
    expect(mockFetch.mock.calls.some((c) => String(c[0]).endsWith("/move"))).toBe(false);

    routeFetch({ status: "ok" });
    fireEvent.keyDown(window, { key: "Delete" });
    await waitFor(() =>
      expect(mockFetch.mock.calls.some((c) => String(c[0]).endsWith("/objects/image/delete"))).toBe(true),
    );
  });

  it("shape tool drag-creates a rectangle in PDF points", async () => {
    mockFetch.mockResolvedValue(ok(PAGE));
    render(<ObjectsOverlay docId="doc1" currentPage={0} onDocumentChanged={() => {}} />);
    await screen.findByTestId("obj-img-7-0");
    const overlay = setupOverlayRect();
    fireEvent.click(screen.getByLabelText("Rectangle"));
    routeFetch({ status: "ok" });
    fireEvent.pointerDown(overlay, { button: 0, clientX: 800, clientY: 1200 });
    await act(async () => {
      window.dispatchEvent(new MouseEvent("pointerup", { clientX: 600, clientY: 1300 }));
    });
    await waitFor(() => expect(mockFetch.mock.calls.some((c) => String(c[0]).endsWith("/objects/shape"))).toBe(true));
    const call = mockFetch.mock.calls.find((c) => String(c[0]).endsWith("/objects/shape"))!;
    expect(JSON.parse(call[1].body)).toMatchObject({ type: "rect", rect: [300, 600, 400, 650] });
  });
});
