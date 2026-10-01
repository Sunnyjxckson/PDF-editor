import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import {
  parsePageRanges, formatPageRanges, movePages, isIdentityOrder, newIndexesOf, nextSelection,
  clientToPagePoint, rectFromPoints, hexToRgb01, rgb01ToHex, planHeaderFooter, formatTokens, batesLabel,
  positionToSlot, gestureToComment, groupCommentsByPage, filterComments, DEFAULT_MARKUP_SETTINGS,
  rotatePages, insertPagesFromFile, createComment, listComments, replyToComment, deleteBookmark,
  type PdfComment, type MarkupSettings,
} from "@/lib/features/organize";
import OrganizeView from "@/components/features/OrganizeView";
import OrganizeHeaderFooterDialog from "@/components/features/OrganizeHeaderFooterDialog";
import OrganizeCommentsPanel from "@/components/features/OrganizeCommentsPanel";
import OrganizeMarkupOverlay from "@/components/features/OrganizeMarkupOverlay";
import OrganizeMarkupToolbar from "@/components/features/OrganizeMarkupToolbar";

const fetchMock = vi.fn();

function ok(data: unknown) {
  return { ok: true, json: () => Promise.resolve(data), blob: () => Promise.resolve(new Blob(["x"])) };
}
function bad(detail: string) {
  return { ok: false, status: 400, json: () => Promise.resolve({ detail }) };
}
function lastCall() {
  const [u, init] = fetchMock.mock.calls[fetchMock.mock.calls.length - 1];
  return { url: String(u), method: (init?.method ?? "GET") as string, body: init?.body ? (typeof init.body === "string" ? JSON.parse(init.body) : init.body) : undefined };
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

// ─── pure helpers ────────────────────────────────────────────────────────────

describe("page range parsing", () => {
  it("parses 1-based ranges into sorted unique 0-based indexes", () => {
    expect(parsePageRanges("1-3, 5, 3", 10)).toEqual([0, 1, 2, 4]);
    expect(parsePageRanges("8-", 10)).toEqual([7, 8, 9]);
    expect(parsePageRanges("-2", 10)).toEqual([0, 1]);
  });
  it("rejects bad input and out-of-range pages", () => {
    expect(() => parsePageRanges("", 5)).toThrow();
    expect(() => parsePageRanges("0", 5)).toThrow();
    expect(() => parsePageRanges("4-9", 5)).toThrow(/outside/);
    expect(() => parsePageRanges("a-b", 5)).toThrow(/Bad/);
    expect(() => parsePageRanges("3-1", 5)).toThrow();
  });
  it("formats indexes back to compact ranges", () => {
    expect(formatPageRanges([0, 1, 2, 4, 6, 7])).toBe("1-3, 5, 7-8");
    expect(formatPageRanges([])).toBe("");
  });
});

describe("movePages (drag-to-reorder)", () => {
  it("moves a single page forward and backward", () => {
    expect(movePages(5, [0], 3)).toEqual([1, 2, 0, 3, 4]);
    expect(movePages(5, [4], 1)).toEqual([0, 4, 1, 2, 3]);
    expect(movePages(5, [1], 5)).toEqual([0, 2, 3, 4, 1]);
  });
  it("moves a non-contiguous selection as a block, keeping order", () => {
    expect(movePages(6, [1, 4], 0)).toEqual([1, 4, 0, 2, 3, 5]);
    expect(movePages(6, [0, 2], 6)).toEqual([1, 3, 4, 5, 0, 2]);
  });
  it("drop onto itself is a no-op and is a valid permutation", () => {
    const o = movePages(4, [2], 2);
    expect(isIdentityOrder(o)).toBe(true);
    expect(isIdentityOrder(movePages(4, [2], 3))).toBe(true);
    const p = movePages(7, [1, 5, 6], 3);
    expect([...p].sort()).toEqual([0, 1, 2, 3, 4, 5, 6]);
  });
  it("reports where moved pages ended up", () => {
    const order = movePages(5, [0, 1], 4);
    expect(order).toEqual([2, 3, 0, 1, 4]);
    expect(newIndexesOf(order, [0, 1])).toEqual([2, 3]);
  });
});

describe("thumbnail selection", () => {
  it("click, toggle, and shift-range", () => {
    expect(nextSelection([2], 5, 2, {})).toEqual({ selection: [5], anchor: 5 });
    expect(nextSelection([2], 5, 2, { toggle: true }).selection).toEqual([2, 5]);
    expect(nextSelection([2, 5], 5, 5, { toggle: true }).selection).toEqual([2]);
    expect(nextSelection([2], 5, 2, { shift: true }).selection).toEqual([2, 3, 4, 5]);
    expect(nextSelection([2], 0, 2, { shift: true }).selection).toEqual([0, 1, 2]);
  });
});

describe("coordinates", () => {
  it("maps client pixels to visible PDF points independent of render scale/zoom", () => {
    // a 612x792pt page rendered at 2x then CSS-zoomed 0.75 => 918x1188 px at (100, 50)
    const box = { left: 100, top: 50, width: 918, height: 1188 };
    expect(clientToPagePoint(100 + 459, 50 + 594, box, 612, 792)).toEqual([306, 396]);
    expect(clientToPagePoint(100 + 918, 50, box, 612, 792)).toEqual([612, 0]);
    // clamps outside the page
    expect(clientToPagePoint(0, 5000, box, 612, 792)).toEqual([0, 792]);
  });
  it("normalizes drag rects in any direction", () => {
    expect(rectFromPoints([300, 400], [100, 50])).toEqual([100, 50, 300, 400]);
  });
  it("converts colours", () => {
    expect(hexToRgb01("#ff8000")).toEqual([1, 128 / 255, 0]);
    expect(hexToRgb01("#fff")).toEqual([1, 1, 1]);
    expect(rgb01ToHex([1, 0.5, 0])).toBe("#ff8000");
    expect(rgb01ToHex(null, "#123456")).toBe("#123456");
    expect(() => hexToRgb01("nope")).toThrow();
  });
});

describe("header/footer planning (mirror of backend)", () => {
  it("expands tokens, skips first page, numbers from start", () => {
    const plan = planHeaderFooter(
      { footer_center: "Page {n} of {total}", header_right: "{bates}", skip_first: true, start_number: 1,
        bates_prefix: "ACME-", bates_digits: 4, bates_start: 10 },
      4, "01/02/2026",
    );
    expect(plan.map((p) => p.page)).toEqual([1, 2, 3]);
    expect(plan[0].texts.footer_center).toBe("Page 1 of 3");
    expect(plan[2].texts.footer_center).toBe("Page 3 of 3");
    expect(plan[1].texts.header_right).toBe("ACME-0011");
  });
  it("restricts to a page subset", () => {
    const plan = planHeaderFooter({ footer_left: "{n}", pages: [5, 2, 9, 2], start_number: 7 }, 8);
    expect(plan.map((p) => [p.page, p.texts.footer_left])).toEqual([[2, "7"], [5, "8"]]);
  });
  it("helpers", () => {
    expect(formatTokens("{n}/{total} {date} {bates} {n}", { n: 3, total: 9, date: "D", bates: "B" })).toBe("3/9 D B 3");
    expect(batesLabel("X", 100, 2, 6, "-c")).toBe("X000102-c");
    expect(positionToSlot("bottom-center")).toBe("footer_center");
    expect(positionToSlot("top-left")).toBe("header_left");
  });
});

describe("gestureToComment", () => {
  const s = (tool: MarkupSettings["tool"], extra: Partial<MarkupSettings> = {}): MarkupSettings =>
    ({ ...DEFAULT_MARKUP_SETTINGS, tool, color: "#ff0000", author: "Ann", ...extra });

  it("text markup sends an area that snaps to words", () => {
    const c = gestureToComment(2, s("highlight"), { start: [300, 120], end: [70, 95] }, 612, 792);
    expect(c).toMatchObject({ page: 2, type: "highlight", area: [70, 95, 300, 120], author: "Ann", color: [1, 0, 0] });
    // thin horizontal swipe gets padded vertically
    const t = gestureToComment(0, s("strikeout"), { start: [70, 100], end: [200, 101] }, 612, 792);
    expect(t.area).toEqual([70, 96, 200, 105]);
    expect(() => gestureToComment(0, s("underline"), { start: [70, 100], end: [71, 100] }, 612, 792)).toThrow(/Drag/);
  });
  it("shapes, lines, notes, stamps", () => {
    expect(gestureToComment(0, s("rect", { fill: "#00ff00" }), { start: [10, 10], end: [50, 40] }, 612, 792))
      .toMatchObject({ type: "rect", rect: [10, 10, 50, 40], fill: [0, 1, 0] });
    expect(gestureToComment(0, s("arrow"), { start: [10, 10], end: [50, 40] }, 612, 792).points).toEqual([[10, 10], [50, 40]]);
    expect(gestureToComment(0, s("note"), { start: [600, 780], end: [600, 780], text: "hi" }, 612, 792))
      .toMatchObject({ type: "note", rect: [592, 772, 612, 792], text: "hi" }); // clamped inside page
    expect(gestureToComment(0, s("stamp"), { start: [300, 300], end: [300, 300] }, 612, 792))
      .toMatchObject({ type: "stamp", rect: [220, 275, 380, 325], stamp: "Approved" });
    expect(() => gestureToComment(0, s("ellipse"), { start: [1, 1], end: [2, 2] }, 612, 792)).toThrow();
    expect(() => gestureToComment(0, s(null), { start: [1, 1], end: [2, 2] }, 612, 792)).toThrow();
  });
  it("callout: tip at drag start, box at release, knee on the box edge", () => {
    const c = gestureToComment(0, s("callout"), { start: [50, 300], end: [200, 200], text: "Look" }, 612, 792);
    expect(c.rect).toEqual([200, 178, 380, 222]);
    expect(c.callout).toEqual([[50, 300], [200, 222]]);
  });
  it("polygon / ink need enough points", () => {
    expect(() => gestureToComment(0, s("polygon"), { start: [0, 0], end: [0, 0], path: [[0, 0], [5, 5]] }, 612, 792)).toThrow();
    expect(gestureToComment(0, s("ink"), { start: [0, 0], end: [3, 3], path: [[0, 0], [1, 2], [3, 3]] }, 612, 792).points).toHaveLength(3);
  });
});

const sampleComments: PdfComment[] = [
  { id: 11, page: 2, type: "note", pdf_subtype: "Text", author: "Ann", contents: "fix the total", subject: "Sticky Note",
    created: "2026-10-01T10:00:00Z", modified: null, color: [1, 0.85, 0.2], fill: null, opacity: 1, rect: [1, 2, 3, 4],
    in_reply_to: null, status: "Accepted", replies: [{ id: 12, page: 2, type: "note", author: "Bob", contents: "done", created: null, modified: null }] },
  { id: 20, page: 0, type: "highlight", pdf_subtype: "Highlight", author: "Bob", contents: "", subject: "Highlight",
    created: null, modified: null, color: [1, 1, 0], fill: null, opacity: 0.5, rect: [1, 2, 3, 4],
    in_reply_to: null, status: null, replies: [] },
];

describe("comment grouping/filtering", () => {
  it("groups by page in order and filters", () => {
    expect(groupCommentsByPage(sampleComments).map((g) => g.page)).toEqual([0, 2]);
    expect(filterComments(sampleComments, { query: "DONE" }).map((c) => c.id)).toEqual([11]); // reply text searched
    expect(filterComments(sampleComments, { author: "Bob" }).map((c) => c.id)).toEqual([20]);
    expect(filterComments(sampleComments, { status: "None" }).map((c) => c.id)).toEqual([20]);
    expect(filterComments(sampleComments, { type: "note" }).map((c) => c.id)).toEqual([11]);
  });
});

// ─── API client ──────────────────────────────────────────────────────────────

describe("organize API client", () => {
  it("rotatePages posts pages+angle to /organize/rotate", async () => {
    fetchMock.mockResolvedValueOnce(ok({ status: "ok", rotations: {} }));
    await rotatePages("doc1", [0, 2], 90);
    const c = lastCall();
    expect(c.url).toMatch(/\/api\/pdf\/doc1\/organize\/rotate$/);
    expect(c.method).toBe("POST");
    expect(c.body).toEqual({ pages: [0, 2], angle: 90, relative: true });
  });
  it("insertPagesFromFile sends multipart with range", async () => {
    fetchMock.mockResolvedValueOnce(ok({ status: "ok", page_count: 5, inserted: 2, inserted_at: 1 }));
    await insertPagesFromFile("doc1", { position: 1, file: new File(["%PDF"], "a.pdf"), pageFrom: 0, pageTo: 1 });
    const fd = fetchMock.mock.calls[0][1].body as FormData;
    expect(fd.get("position")).toBe("1");
    expect(fd.get("page_from")).toBe("0");
    expect(fd.get("page_to")).toBe("1");
    expect((fd.get("file") as File).name).toBe("a.pdf");
  });
  it("surfaces backend error detail", async () => {
    fetchMock.mockResolvedValueOnce(bad("No text found to mark up"));
    await expect(createComment("d", { page: 0, type: "highlight", search: "zz" })).rejects.toThrow("No text found to mark up");
  });
  it("comments endpoints", async () => {
    fetchMock.mockResolvedValueOnce(ok({ comments: sampleComments }));
    expect(await listComments("d")).toHaveLength(2);
    fetchMock.mockResolvedValueOnce(ok({ reply: { id: 1 } }));
    await replyToComment("d", 11, "yes", "Zed");
    expect(lastCall()).toMatchObject({ method: "POST", body: { text: "yes", author: "Zed" } });
    expect(lastCall().url).toMatch(/organize\/comments\/11\/reply$/);
    fetchMock.mockResolvedValueOnce(ok({ bookmarks: [] }));
    await deleteBookmark("d", 3);
    expect(lastCall()).toMatchObject({ method: "DELETE" });
    expect(lastCall().url).toMatch(/organize\/bookmarks\/3$/);
  });
});

// ─── components ──────────────────────────────────────────────────────────────

describe("OrganizeView", () => {
  it("renders a thumbnail per page with cache-busted URLs and multi-selects", () => {
    render(<OrganizeView docId="d1" pageCount={4} currentPage={1} onDocumentChanged={() => {}} />);
    const imgs = screen.getAllByRole("img");
    expect(imgs).toHaveLength(4);
    expect(imgs[2].getAttribute("src")).toMatch(/\/api\/pdf\/d1\/thumbnail\/2\?v=\d+$/);
    expect(screen.getByText("1/4")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("organize-thumb-3"), { shiftKey: true });
    expect(screen.getByText("3/4")).toBeInTheDocument();
  });

  it("rotates the selected pages and reports the change", async () => {
    const changed = vi.fn();
    fetchMock.mockResolvedValue(ok({ status: "ok", rotations: {} }));
    render(<OrganizeView docId="d1" pageCount={3} currentPage={0} onDocumentChanged={changed} />);
    fireEvent.click(screen.getByTestId("organize-thumb-2"), { metaKey: true });
    fireEvent.click(screen.getByTitle("Rotate right"));
    await waitFor(() => expect(changed).toHaveBeenCalled());
    expect(lastCall().body).toEqual({ pages: [0, 2], angle: 90, relative: true });
  });

  it("drag-and-drop calls /reorder with the moved order", async () => {
    const changed = vi.fn();
    fetchMock.mockResolvedValue(ok({ status: "ok" }));
    render(<OrganizeView docId="d1" pageCount={4} currentPage={0} onDocumentChanged={changed} />);
    const src = screen.getByTestId("organize-thumb-0");
    // jsdom DragEvents carry no clientX, so this lands on the left half => "before page 4"
    const dst = screen.getByTestId("organize-thumb-3");
    dst.getBoundingClientRect = () => ({ left: 0, top: 0, width: 100, height: 100, right: 100, bottom: 100, x: 0, y: 0, toJSON: () => ({}) });
    const dt = { setData: vi.fn(), getData: () => "0", effectAllowed: "" };
    fireEvent.dragStart(src, { dataTransfer: dt });
    fireEvent.dragOver(dst, { dataTransfer: dt });
    fireEvent.drop(dst, { dataTransfer: dt });
    await waitFor(() => expect(changed).toHaveBeenCalled());
    const c = lastCall();
    expect(c.url).toMatch(/\/api\/pdf\/d1\/reorder$/);
    expect(c.body).toEqual({ page_order: [1, 2, 0, 3] });
  });

  it("refuses to delete every page without calling the server", () => {
    render(<OrganizeView docId="d1" pageCount={1} currentPage={0} onDocumentChanged={() => {}} />);
    fireEvent.click(screen.getByText("Delete"));
    expect(screen.getByText(/at least one page/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("OrganizeHeaderFooterDialog", () => {
  it("shows a live preview and applies page numbers with the chosen options", async () => {
    const changed = vi.fn();
    const close = vi.fn();
    fetchMock.mockResolvedValue(ok({ status: "ok", stamped_count: 2 }));
    render(<OrganizeHeaderFooterDialog docId="d1" pageCount={3} open onClose={close} onDocumentChanged={changed} />);
    expect(screen.getByTestId("hf-preview-footer_center")).toHaveTextContent("Page 1 of 3");
    fireEvent.click(screen.getByLabelText("Skip first page"));
    expect(screen.getByTestId("hf-preview-footer_center")).toHaveTextContent("Page 1 of 2");
    expect(screen.getByText("Preview: PDF page 2")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("top-right"));
    expect(screen.getByTestId("hf-preview-header_right")).toHaveTextContent("Page 1 of 2");
    fireEvent.click(screen.getByText(/Apply to 2 page/));
    await waitFor(() => expect(changed).toHaveBeenCalled());
    const c = lastCall();
    expect(c.url).toMatch(/organize\/page-numbers$/);
    expect(c.body).toMatchObject({ format: "Page {n} of {total}", position: "top-right", skip_first: true, font: "helv", color: [0, 0, 0] });
    expect(close).toHaveBeenCalled();
  });

  it("Bates tab previews the padded number", () => {
    render(<OrganizeHeaderFooterDialog docId="d1" pageCount={3} open onClose={() => {}} onDocumentChanged={() => {}} initialTab="bates" />);
    fireEvent.change(screen.getByPlaceholderText("ACME-"), { target: { value: "SMITH" } });
    expect(screen.getByTestId("hf-preview-footer_right")).toHaveTextContent("SMITH000001");
  });
});

describe("OrganizeCommentsPanel", () => {
  it("lists comments with author/page/status, jumps, and replies", async () => {
    const jump = vi.fn();
    const changed = vi.fn();
    fetchMock.mockResolvedValue(ok({ comments: sampleComments }));
    render(<OrganizeCommentsPanel docId="d1" currentPage={0} onJumpToPage={jump} onDocumentChanged={changed} author="Me" />);
    await screen.findByText("fix the total");
    expect(screen.getByText("Page 1")).toBeInTheDocument();
    expect(screen.getByText("Page 3")).toBeInTheDocument();
    expect(screen.getByTestId("comment-11")).toHaveTextContent("Accepted");
    expect(screen.getByTestId("comment-20")).not.toHaveTextContent("Accepted");
    fireEvent.click(screen.getByText("fix the total"));
    expect(jump).toHaveBeenCalledWith(2);
    expect(screen.getByText("done")).toBeInTheDocument(); // reply visible when expanded
    fireEvent.change(screen.getByPlaceholderText("Reply..."), { target: { value: "thanks" } });
    fetchMock.mockResolvedValueOnce(ok({ reply: { id: 99 } }));
    fireEvent.click(screen.getByLabelText("Send reply"));
    await waitFor(() => expect(changed).toHaveBeenCalled());
    const replyCall = fetchMock.mock.calls.find(([u]) => String(u).endsWith("/comments/11/reply"));
    expect(JSON.parse(replyCall![1].body)).toEqual({ text: "thanks", author: "Me" });
  });
});

describe("OrganizeMarkupOverlay", () => {
  it("converts a drag in rendered pixels into a rect in PDF points and saves it", async () => {
    const created = vi.fn();
    fetchMock.mockResolvedValue(ok({ comment: { id: 5 } }));
    const settings: MarkupSettings = { ...DEFAULT_MARKUP_SETTINGS, tool: "rect", color: "#0000ff", author: "Ann" };
    render(<OrganizeMarkupOverlay docId="d1" page={1} pageWidth={612} pageHeight={792} settings={settings} onCreated={created} />);
    const el = screen.getByTestId("organize-markup-overlay");
    // page drawn at 1224x1584 px (scale 2) at offset 10,20
    el.getBoundingClientRect = () => ({ left: 10, top: 20, width: 1224, height: 1584, right: 1234, bottom: 1604, x: 10, y: 20, toJSON: () => ({}) });
    fireEvent.mouseDown(el, { button: 0, clientX: 10 + 200, clientY: 20 + 100 });
    fireEvent.mouseMove(el, { clientX: 10 + 400, clientY: 20 + 300 });
    fireEvent.mouseUp(el, { clientX: 10 + 400, clientY: 20 + 300 });
    await waitFor(() => expect(created).toHaveBeenCalled());
    const c = lastCall();
    expect(c.url).toMatch(/organize\/comments$/);
    expect(c.body).toMatchObject({ page: 1, type: "rect", rect: [100, 50, 200, 150], color: [0, 0, 1], author: "Ann" });
  });

  it("sticky note asks for text before saving", async () => {
    const created = vi.fn();
    fetchMock.mockResolvedValue(ok({ comment: { id: 6 } }));
    const settings: MarkupSettings = { ...DEFAULT_MARKUP_SETTINGS, tool: "note" };
    render(<OrganizeMarkupOverlay docId="d1" page={0} pageWidth={612} pageHeight={792} settings={settings} onCreated={created} />);
    const el = screen.getByTestId("organize-markup-overlay");
    el.getBoundingClientRect = () => ({ left: 0, top: 0, width: 612, height: 792, right: 612, bottom: 792, x: 0, y: 0, toJSON: () => ({}) });
    fireEvent.mouseDown(el, { button: 0, clientX: 100, clientY: 100 });
    fireEvent.mouseUp(el, { clientX: 100, clientY: 100 });
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.change(screen.getByPlaceholderText("Comment"), { target: { value: "Check this" } });
    fireEvent.click(screen.getByText("Add"));
    await waitFor(() => expect(created).toHaveBeenCalled());
    expect(lastCall().body).toMatchObject({ type: "note", text: "Check this", rect: [100, 100, 120, 120] });
  });

  it("renders nothing when no tool is active", () => {
    render(<OrganizeMarkupOverlay docId="d1" page={0} pageWidth={612} pageHeight={792} settings={DEFAULT_MARKUP_SETTINGS} onCreated={() => {}} />);
    expect(screen.queryByTestId("organize-markup-overlay")).toBeNull();
  });
});

describe("OrganizeMarkupToolbar", () => {
  it("selects a tool and applies its default colour", () => {
    const onChange = vi.fn();
    render(<OrganizeMarkupToolbar settings={DEFAULT_MARKUP_SETTINGS} onChange={onChange} />);
    fireEvent.click(screen.getByLabelText("Highlight text (drag across text)"));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ tool: "highlight", color: "#ffeb3b" }));
  });
});
