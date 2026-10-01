import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import {
  alignBboxes,
  distributeBboxes,
  objectsInMarquee,
  nudgeDelta,
  unionBbox,
  type Bbox,
  type ImageObject,
  type DrawingObject,
  type PageObjects,
} from "@/lib/features/objects";
import ObjectsOverlay, { NUDGE_COMMIT_MS } from "@/components/features/ObjectsOverlay";

if (typeof window.PointerEvent === "undefined") {
  class PointerEventPolyfill extends MouseEvent {}
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).PointerEvent = PointerEventPolyfill;
}

const mockFetch = vi.fn();
global.fetch = mockFetch as unknown as typeof fetch;
const ok = (data: unknown) => ({ ok: true, json: () => Promise.resolve(data) });

const IMG: ImageObject = {
  id: "img-7-0", kind: "image", xref: 7, occurrence: 0, bbox: [100, 100, 200, 150],
  width: 40, height: 20, colorspace: "DeviceRGB", bpc: 8, size: 100, has_mask: false,
  names: ["fzImg0"], editable: true, method: "stream",
};
const DRW = (index: number, bbox: Bbox): DrawingObject => ({
  id: `drw-${index}`, kind: "drawing", index, bbox, path_count: 1, seqnos: [index],
  stroke: [0, 0, 0], fill: null, width: 1, background: false,
});
const PAGE: PageObjects = {
  page: 0, page_width: 612, page_height: 792, rotation: 0,
  images: [IMG],
  drawings: [DRW(0, [300, 100, 340, 120]), DRW(1, [400, 300, 420, 330])],
};

function route(mutation: unknown = { status: "ok", results: [] }) {
  mockFetch.mockImplementation((url: string) =>
    Promise.resolve(/\/objects\/\d+$/.test(String(url)) ? ok(PAGE) : ok(mutation)),
  );
}
const calls = (suffix: string) => mockFetch.mock.calls.filter((c) => String(c[0]).endsWith(suffix));
const body = (c: unknown[]) => JSON.parse((c[1] as RequestInit).body as string);

/** 2 CSS px per PDF point, at the viewport origin. */
function sizeOverlay() {
  const el = screen.getByTestId("objects-overlay");
  el.getBoundingClientRect = () =>
    ({ left: 0, top: 0, width: 1224, height: 1584, right: 1224, bottom: 1584, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
  return el;
}

async function mount() {
  route();
  const changed = vi.fn();
  render(<ObjectsOverlay docId="doc1" currentPage={0} onDocumentChanged={changed} />);
  await screen.findByTestId("obj-img-7-0");
  sizeOverlay();
  return changed;
}

async function click(el: Element, opts: { shiftKey?: boolean } = {}) {
  const r = { clientX: 1, clientY: 1 };
  fireEvent.pointerDown(el, { button: 0, ...r, ...opts });
  await act(async () => {
    window.dispatchEvent(new MouseEvent("pointerup", r));
  });
}

beforeEach(() => mockFetch.mockReset());
afterEach(() => vi.useRealTimers());

// ─── pure helpers ─────────────────────────────────────────────────────────────

describe("alignment helpers", () => {
  const boxes: Bbox[] = [
    [10, 10, 30, 20],
    [50, 40, 60, 80],
    [100, 0, 140, 10],
  ];
  it("aligns to the edges / centres of the union box without resizing", () => {
    expect(unionBbox(boxes)).toEqual([10, 0, 140, 80]);
    expect(alignBboxes(boxes, "left").map((b) => b[0])).toEqual([10, 10, 10]);
    expect(alignBboxes(boxes, "right").map((b) => b[2])).toEqual([140, 140, 140]);
    expect(alignBboxes(boxes, "top").map((b) => b[1])).toEqual([0, 0, 0]);
    expect(alignBboxes(boxes, "bottom").map((b) => b[3])).toEqual([80, 80, 80]);
    expect(alignBboxes(boxes, "center").map((b) => (b[0] + b[2]) / 2)).toEqual([75, 75, 75]);
    expect(alignBboxes(boxes, "middle").map((b) => (b[1] + b[3]) / 2)).toEqual([40, 40, 40]);
    // sizes kept
    alignBboxes(boxes, "center").forEach((b, i) => expect(b[2] - b[0]).toBe(boxes[i][2] - boxes[i][0]));
  });
  it("distributes equal gaps, keeping the outermost boxes in place and input order", () => {
    const d = distributeBboxes([[100, 0, 120, 10], [0, 0, 10, 10], [30, 0, 40, 10]], "horizontal");
    // order by x: [0..10], [30..40]->?, [100..120]; total span 120, sizes 40 -> gap 40
    expect(d[1]).toEqual([0, 0, 10, 10]);
    expect(d[0]).toEqual([100, 0, 120, 10]);
    expect(d[2]).toEqual([50, 0, 60, 10]);
    expect(distributeBboxes([[0, 0, 1, 1], [5, 5, 6, 6]], "vertical")).toEqual([[0, 0, 1, 1], [5, 5, 6, 6]]);
  });
  it("marquee hit-test and nudge deltas", () => {
    const objs = [{ bbox: [0, 0, 10, 10] as Bbox }, { bbox: [50, 50, 60, 60] as Bbox }];
    expect(objectsInMarquee(objs, [20, 20, 5, 5])).toEqual([objs[0]]); // reversed drag works
    expect(objectsInMarquee(objs, [0, 0, 100, 100])).toHaveLength(2);
    expect(nudgeDelta("ArrowLeft", false)).toEqual([-1, 0]);
    expect(nudgeDelta("ArrowDown", true)).toEqual([0, 10]);
    expect(nudgeDelta("a", false)).toBeNull();
  });
});

// ─── multi-select in the overlay ──────────────────────────────────────────────

describe("ObjectsOverlay multi-select", () => {
  it("shift-click builds a selection; align left commits ONE batch request", async () => {
    const changed = await mount();
    await click(screen.getByTestId("obj-img-7-0"));
    await click(screen.getByTestId("obj-drw-0"), { shiftKey: true });
    await click(screen.getByTestId("obj-drw-1"), { shiftKey: true });
    expect(screen.getByTestId("objects-multi-actions")).toHaveTextContent("3 objects");
    expect(screen.getByTestId("obj-drw-1")).toHaveAttribute("aria-selected", "true");

    mockFetch.mockClear();
    route();
    fireEvent.click(screen.getByLabelText("Align left"));
    await waitFor(() => expect(changed).toHaveBeenCalled());
    const batch = calls("/objects/batch");
    expect(batch).toHaveLength(1);
    expect(calls("/move")).toHaveLength(0);
    const ops = body(batch[0]).ops;
    // the image is already leftmost (x0=100): only the two drawings move
    expect(ops).toEqual([
      { op: "move", kind: "drawing", index: 0, bbox: [300, 100, 340, 120], new_bbox: [100, 100, 140, 120] },
      { op: "move", kind: "drawing", index: 1, bbox: [400, 300, 420, 330], new_bbox: [100, 300, 120, 330] },
    ]);
  });

  it("shift-click on a selected object removes it", async () => {
    await mount();
    await click(screen.getByTestId("obj-img-7-0"));
    await click(screen.getByTestId("obj-drw-0"), { shiftKey: true });
    await click(screen.getByTestId("obj-img-7-0"), { shiftKey: true });
    expect(screen.getByTestId("obj-img-7-0")).toHaveAttribute("aria-selected", "false");
    expect(screen.queryByTestId("objects-multi-actions")).toBeNull();
  });

  it("marquee drag on empty page selects every object it touches", async () => {
    await mount();
    const overlay = screen.getByTestId("objects-overlay");
    // PDF (90,90)->(350,130) = client (180,180)->(700,260): image + drawing 0
    fireEvent.pointerDown(overlay, { button: 0, clientX: 180, clientY: 180 });
    await act(async () => {
      window.dispatchEvent(new MouseEvent("pointermove", { clientX: 700, clientY: 260 }));
    });
    expect(screen.getByTestId("objects-marquee")).toBeInTheDocument();
    await act(async () => {
      window.dispatchEvent(new MouseEvent("pointerup", { clientX: 700, clientY: 260 }));
    });
    expect(screen.getByTestId("obj-img-7-0")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("obj-drw-0")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("obj-drw-1")).toHaveAttribute("aria-selected", "false");
    expect(screen.getByTestId("objects-multi-actions")).toHaveTextContent("2 objects");
  });

  it("dragging one of several selected objects moves them all in one batch", async () => {
    const changed = await mount();
    await click(screen.getByTestId("obj-img-7-0"));
    await click(screen.getByTestId("obj-drw-0"), { shiftKey: true });
    mockFetch.mockClear();
    route();
    fireEvent.pointerDown(screen.getByTestId("obj-drw-0"), { button: 0, clientX: 600, clientY: 200 });
    await act(async () => {
      window.dispatchEvent(new MouseEvent("pointermove", { clientX: 620, clientY: 240 }));
    });
    // live preview: both boxes follow (+10pt, +20pt)
    expect(screen.getByTestId("obj-img-7-0").style.left).toBe(`${(110 / 612) * 100}%`);
    await act(async () => {
      window.dispatchEvent(new MouseEvent("pointerup", { clientX: 620, clientY: 240 }));
    });
    await waitFor(() => expect(changed).toHaveBeenCalled());
    const batch = calls("/objects/batch");
    expect(batch).toHaveLength(1);
    expect(body(batch[0]).ops.map((o: { new_bbox: Bbox }) => o.new_bbox).sort()).toEqual([
      [110, 120, 210, 170],
      [310, 120, 350, 140],
    ]);
  });

  it("Delete with several selected objects deletes them in one batch", async () => {
    await mount();
    await click(screen.getByTestId("obj-img-7-0"));
    await click(screen.getByTestId("obj-drw-1"), { shiftKey: true });
    mockFetch.mockClear();
    route();
    fireEvent.keyDown(window, { key: "Delete" });
    await waitFor(() => expect(calls("/objects/batch")).toHaveLength(1));
    expect(body(calls("/objects/batch")[0]).ops.map((o: { op: string; kind: string }) => `${o.op}:${o.kind}`).sort()).toEqual([
      "delete:drawing",
      "delete:image",
    ]);
    expect(calls("/delete")).toHaveLength(0);
  });
});

describe("ObjectsOverlay keyboard", () => {
  it("arrow keys nudge 1pt / Shift 10pt, previewed live and committed ONCE after a pause", async () => {
    await mount();
    await click(screen.getByTestId("obj-img-7-0"));
    vi.useFakeTimers();
    mockFetch.mockClear();
    route({ status: "ok", method: "stream", bbox: [103, 110, 203, 160] });

    const ev = new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true });
    act(() => {
      window.dispatchEvent(ev);
    });
    expect(ev.defaultPrevented).toBe(true); // Editor's page navigation must not also fire
    act(() => {
      fireEvent.keyDown(window, { key: "ArrowRight" });
      fireEvent.keyDown(window, { key: "ArrowRight" });
      fireEvent.keyDown(window, { key: "ArrowDown", shiftKey: true });
    });
    expect(screen.getByTestId("obj-img-7-0").style.left).toBe(`${(103 / 612) * 100}%`);
    expect(screen.getByTestId("obj-img-7-0").style.top).toBe(`${(110 / 792) * 100}%`);
    act(() => {
      vi.advanceTimersByTime(NUDGE_COMMIT_MS - 50);
    });
    expect(calls("/move")).toHaveLength(0); // still debouncing
    await act(async () => {
      vi.advanceTimersByTime(100);
    });
    vi.useRealTimers();
    await waitFor(() => expect(calls("/objects/image/move")).toHaveLength(1));
    expect(body(calls("/objects/image/move")[0]).new_bbox).toEqual([103, 110, 203, 160]);
  });

  it("Cmd/Ctrl+D duplicates the selection with an offset", async () => {
    await mount();
    await click(screen.getByTestId("obj-drw-0"));
    mockFetch.mockClear();
    route();
    const ev = new KeyboardEvent("keydown", { key: "d", metaKey: true, bubbles: true, cancelable: true });
    act(() => {
      window.dispatchEvent(ev);
    });
    expect(ev.defaultPrevented).toBe(true); // not the browser's bookmark shortcut
    await waitFor(() => expect(calls("/objects/batch")).toHaveLength(1));
    expect(body(calls("/objects/batch")[0]).ops).toEqual([
      { op: "duplicate", kind: "drawing", index: 0, bbox: [300, 100, 340, 120], dx: 10, dy: 10 },
    ]);
  });

  it("Bring to front / Send to back call the arrange endpoint", async () => {
    await mount();
    await click(screen.getByTestId("obj-img-7-0"));
    mockFetch.mockClear();
    route({ status: "ok", method: "stream" });
    fireEvent.click(screen.getByLabelText("Send to back"));
    await waitFor(() => expect(calls("/objects/arrange")).toHaveLength(1));
    expect(body(calls("/objects/arrange")[0])).toEqual({
      page: 0, where: "back", kind: "image", xref: 7, occurrence: 0, bbox: [100, 100, 200, 150],
    });
  });

  it("errors are announced in an assertive live region", async () => {
    await mount();
    await click(screen.getByTestId("obj-img-7-0"));
    const alert = screen.getByRole("alert");
    expect(alert).toHaveAttribute("aria-live", "assertive");
    expect(alert).toHaveTextContent("");
    mockFetch.mockImplementation((url: string) =>
      Promise.resolve(
        /\/objects\/\d+$/.test(String(url))
          ? ok(PAGE)
          : { ok: false, status: 409, json: () => Promise.resolve({ detail: "Image has changed since it was selected; refresh" }) },
      ),
    );
    fireEvent.click(screen.getByLabelText("Bring to front"));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Image has changed since it was selected"));
  });
});
