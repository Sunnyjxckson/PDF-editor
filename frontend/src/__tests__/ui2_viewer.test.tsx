import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";

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

import PageViewer, { BufferedPageImage } from "@/components/PageViewer";
import { CachedThumbnail } from "@/components/PageSidebar";
import {
  useEditorStore,
  CSS_PX_PER_PT,
  computeFitZoom,
  stepZoom,
} from "@/lib/store";

/** Give an <img> the natural size a real decode would. */
function loadImg(img: HTMLElement, w = 1275, h = 1650) {
  Object.defineProperty(img, "naturalWidth", { value: w, configurable: true });
  Object.defineProperty(img, "naturalHeight", { value: h, configurable: true });
  fireEvent.load(img);
}

afterEach(() => vi.useRealTimers());

// ─── Double-buffered page image ───────────────────────────────────────────────

describe("BufferedPageImage: no blank flash between renders", () => {
  it("keeps the old src on screen until the new render has loaded", () => {
    vi.useFakeTimers();
    const onShown = vi.fn();
    const { rerender, container } = render(
      <BufferedPageImage src="/p0?v=0" resetKey="d|0" alt="Page 1" onShown={onShown} />,
    );
    const first = container.querySelector("img")!;
    loadImg(first);
    expect(screen.getByAltText("Page 1").getAttribute("src")).toBe("/p0?v=0");
    expect(onShown).toHaveBeenCalledTimes(1);

    // an edit bumps the version: the new render starts loading in the back buffer
    rerender(<BufferedPageImage src="/p0?v=1" resetKey="d|0" alt="Page 1" onShown={onShown} />);
    const imgs = container.querySelectorAll("img");
    expect(imgs).toHaveLength(2);
    const back = container.querySelector('img[data-buffer="back"]') as HTMLImageElement;
    expect(back.getAttribute("src")).toBe("/p0?v=1");
    expect(back.style.visibility).toBe("hidden");

    // however long the server takes, the visible image is still the old one
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    const front = screen.getByAltText("Page 1");
    expect(front.getAttribute("src")).toBe("/p0?v=0");
    expect(front.style.visibility).not.toBe("hidden");
    expect(front.getAttribute("data-buffer")).toBe("front");

    // swap only on load; the old buffer is released
    loadImg(back);
    expect(screen.getByAltText("Page 1").getAttribute("src")).toBe("/p0?v=1");
    expect(container.querySelectorAll("img")).toHaveLength(1);
    expect(onShown).toHaveBeenCalledTimes(2);

    // a third version swaps back into the other slot the same way
    rerender(<BufferedPageImage src="/p0?v=2" resetKey="d|0" alt="Page 1" onShown={onShown} />);
    expect(screen.getByAltText("Page 1").getAttribute("src")).toBe("/p0?v=1");
    loadImg(container.querySelector('img[data-buffer="back"]')!);
    expect(screen.getByAltText("Page 1").getAttribute("src")).toBe("/p0?v=2");
  });

  it("a different page drops the old image instead of showing it under the new page", () => {
    const { rerender, container } = render(<BufferedPageImage src="/p0" resetKey="d|0" alt="Page 1" />);
    loadImg(container.querySelector("img")!);
    rerender(<BufferedPageImage src="/p1" resetKey="d|1" alt="Page 2" />);
    expect(screen.queryByAltText("Page 1")).toBeNull();
    expect(container.querySelectorAll("img")).toHaveLength(1);
    expect(container.querySelector("img")!.getAttribute("src")).toBe("/p1");
  });
});

// ─── Thumbnails ───────────────────────────────────────────────────────────────

describe("CachedThumbnail: swaps in the new render only once it loaded", () => {
  const created: { src: string; onload: (() => void) | null }[] = [];
  const RealImage = globalThis.Image;
  beforeEach(() => {
    created.length = 0;
    class FakeImage {
      crossOrigin = "";
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      naturalWidth = 10;
      naturalHeight = 10;
      private _src = "";
      constructor() {
        created.push(this as unknown as { src: string; onload: (() => void) | null });
      }
      set src(v: string) {
        this._src = v;
      }
      get src() {
        return this._src;
      }
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (globalThis as any).Image = FakeImage;
    HTMLCanvasElement.prototype.getContext = (() => null) as never;
  });
  afterEach(() => {
    globalThis.Image = RealImage;
  });

  it("keeps the previous thumbnail visible while the new version loads", () => {
    const { rerender } = render(<CachedThumbnail docId="thumbdoc" pageIndex={0} pageVersion={0} />);
    expect(screen.queryByAltText("Page 1")).toBeNull(); // first load: spinner
    expect(created.at(-1)!.src).toMatch(/thumbnail\/0\?v=0$/);
    act(() => created.at(-1)!.onload!());
    expect(screen.getByAltText("Page 1").getAttribute("src")).toMatch(/\?v=0$/);

    rerender(<CachedThumbnail docId="thumbdoc" pageIndex={0} pageVersion={1} />);
    expect(created.at(-1)!.src).toMatch(/\?v=1$/);
    // still the old picture, no spinner
    expect(screen.getByAltText("Page 1").getAttribute("src")).toMatch(/\?v=0$/);
    expect(screen.queryByLabelText("Loading page 1")).toBeNull();

    act(() => created.at(-1)!.onload!());
    expect(screen.getByAltText("Page 1").getAttribute("src")).toMatch(/\?v=1$/);
  });
});

// ─── True-size zoom ───────────────────────────────────────────────────────────

describe("zoom model: 100% is physical size", () => {
  it("72pt = 1in = 96 CSS px, so Letter is 816px wide at 100%", () => {
    expect(612 * CSS_PX_PER_PT).toBe(816);
    expect(computeFitZoom("fit-width", 612, 792, 816, 300)).toBeCloseTo(1);
    expect(computeFitZoom("fit-width", 612, 792, 1632, 300)).toBeCloseTo(2);
    // fit page is limited by the height here
    expect(computeFitZoom("fit-page", 612, 792, 1632, 528)).toBeCloseTo(0.5);
    expect(computeFitZoom("fit-page", 612, 792, 100000, 100000)).toBe(3); // clamped
  });
  it("steps through presets and setZoom leaves fit mode", () => {
    expect(stepZoom(1, 1)).toBe(1.1);
    expect(stepZoom(1, -1)).toBe(0.9);
    expect(stepZoom(3, 1)).toBe(3);
    expect(stepZoom(0.25, -1)).toBe(0.25);
    useEditorStore.getState().setZoomMode("fit-width");
    useEditorStore.getState().setZoom(1.5);
    expect(useEditorStore.getState().zoomMode).toBe("custom");
  });
});

describe("PageViewer layout", () => {
  const mockFetch = vi.fn();
  beforeEach(() => {
    global.fetch = mockFetch as unknown as typeof fetch;
    mockFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve([]) });
    useEditorStore.getState().reset();
    useEditorStore.setState({
      docId: "doc-ui2",
      document: {
        page_count: 1,
        pages: [{ width: 612, height: 792 }],
      } as never,
      totalPages: 1,
      currentPage: 0,
      zoom: 1,
      zoomMode: "custom",
      activeTool: "select",
      renderMode: "image",
    });
  });

  function shownPage(container: HTMLElement) {
    const img = container.querySelector("img")!;
    act(() => loadImg(img)); // 150 dpi render of Letter: 1275 x 1650 px
  }

  it("renders a Letter page 816 CSS px wide at 100% and scales with zoom", () => {
    const { container } = render(<PageViewer />);
    shownPage(container);
    const sizer = screen.getByTestId("page-sizer");
    expect(parseFloat(sizer.style.width)).toBeCloseTo(816);
    expect(parseFloat(sizer.style.height)).toBeCloseTo(1056);
    // the scaled surface is anchored top-left inside a layout box of the zoomed
    // size, so an overflowing page scrolls from its left edge
    expect(screen.getByTestId("page-surface").style.transformOrigin).toBe("top left");
    act(() => useEditorStore.getState().setZoom(2));
    expect(parseFloat(screen.getByTestId("page-sizer").style.width)).toBeCloseTo(1632);
  });

  it("Cmd/Ctrl + = / - / 0 zoom in, out and back to actual size", () => {
    const { container } = render(<PageViewer />);
    shownPage(container);
    const press = (key: string) => {
      const ev = new KeyboardEvent("keydown", { key, metaKey: true, bubbles: true, cancelable: true });
      act(() => {
        window.dispatchEvent(ev);
      });
      return ev;
    };
    expect(press("=").defaultPrevented).toBe(true); // not the browser's page zoom
    expect(useEditorStore.getState().zoom).toBe(1.1);
    press("-");
    press("-");
    expect(useEditorStore.getState().zoom).toBe(0.9);
    press("0");
    expect(useEditorStore.getState().zoom).toBe(1);
  });

  it("fit width follows the viewer's width", () => {
    const { container } = render(<PageViewer />);
    shownPage(container);
    const scroller = screen.getByTestId("page-sizer").parentElement!;
    Object.defineProperty(scroller, "clientWidth", { value: 1632, configurable: true });
    Object.defineProperty(scroller, "clientHeight", { value: 600, configurable: true });
    act(() => useEditorStore.getState().setZoomMode("fit-width"));
    act(() => {
      window.dispatchEvent(new Event("resize"));
    });
    expect(useEditorStore.getState().zoom).toBeCloseTo(2);
    expect(parseFloat(screen.getByTestId("page-sizer").style.width)).toBeCloseTo(1632);
    act(() => useEditorStore.getState().setZoomMode("fit-page"));
    expect(useEditorStore.getState().zoom).toBeCloseTo(600 / 1056);
  });

  it("an edit keeps the current page image on screen until the new render loads", () => {
    const { container } = render(<PageViewer />);
    shownPage(container);
    const before = screen.getByAltText("Page 1").getAttribute("src");
    act(() => useEditorStore.getState().bumpVersion());
    // old image still visible, the new one loading hidden, no loader covering the page
    expect(screen.getByAltText("Page 1").getAttribute("src")).toBe(before);
    const back = container.querySelector('img[data-buffer="back"]') as HTMLImageElement;
    expect(back.getAttribute("src")).toMatch(/v=1$/);
    expect(container.querySelector(".animate-spin")).toBeNull();
    act(() => loadImg(back));
    expect(screen.getByAltText("Page 1").getAttribute("src")).toMatch(/v=1$/);
  });
});
