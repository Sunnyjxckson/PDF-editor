import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import {
  bookmarkDropPosition, canDropBookmark, canIndentBookmark, bookmarkSubtreeEnd, describeRun, replyDepths,
  type Bookmark, type HFRun,
} from "@/lib/features/organize";
import OrganizeBookmarksPanel from "@/components/features/OrganizeBookmarksPanel";
import OrganizeHeaderFooterDialog from "@/components/features/OrganizeHeaderFooterDialog";

const fetchMock = vi.fn();
function ok(data: unknown) {
  return { ok: true, json: () => Promise.resolve(data) };
}
function calls() {
  return fetchMock.mock.calls.map(([u, init]) => ({
    url: String(u),
    method: (init?.method ?? "GET") as string,
    body: init?.body ? JSON.parse(init.body as string) : undefined,
  }));
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

// jsdom has no DragEvent; a MouseEvent with the drag event's name carries clientY.
function dnd(type: string, clientY: number) {
  return new MouseEvent(type, { bubbles: true, cancelable: true, clientY });
}

const BM: Bookmark[] = [
  { index: 0, level: 1, title: "Intro", page: 0 },
  { index: 1, level: 2, title: "Intro.a", page: 1 },
  { index: 2, level: 2, title: "Intro.b", page: 2 },
  { index: 3, level: 1, title: "Chapter 5", page: 5 },
  { index: 4, level: 2, title: "5.1", page: 6 },
];

describe("bookmark tree helpers", () => {
  it("drop position, subtree and indent rules", () => {
    expect(bookmarkDropPosition(1, 20)).toBe("before");
    expect(bookmarkDropPosition(10, 20)).toBe("inside");
    expect(bookmarkDropPosition(19, 20)).toBe("after");
    expect(bookmarkSubtreeEnd(BM, 0)).toBe(3);
    expect(canDropBookmark(BM, 0, 1)).toBe(false); // into own child
    expect(canDropBookmark(BM, 0, 0)).toBe(false);
    expect(canDropBookmark(BM, 3, 0)).toBe(true);
    expect(canDropBookmark(BM, 1, 4)).toBe(true);
    expect(canIndentBookmark(BM, 0)).toBe(false);
    expect(canIndentBookmark(BM, 1)).toBe(false); // first child: no previous sibling
    expect(canIndentBookmark(BM, 2)).toBe(true);
    expect(canIndentBookmark(BM, 3)).toBe(true);
  });
});

describe("OrganizeBookmarksPanel", () => {
  it("adds a bookmark without level/index (backend places it top-level in page order)", async () => {
    fetchMock.mockResolvedValue(ok({ bookmarks: BM }));
    const changed = vi.fn();
    render(<OrganizeBookmarksPanel docId="d1" currentPage={3} onJumpToPage={() => {}} onDocumentChanged={changed} />);
    await screen.findByText("Chapter 5");
    fireEvent.change(screen.getByPlaceholderText("New bookmark -> page 4"), { target: { value: "Chapter 2" } });
    fireEvent.click(screen.getByLabelText("Add bookmark"));
    await waitFor(() => expect(changed).toHaveBeenCalled());
    const post = calls().find((c) => c.method === "POST")!;
    expect(post.url).toMatch(/organize\/bookmarks$/);
    expect(post.body).toEqual({ title: "Chapter 2", page: 3 }); // no level, no index, no parent
  });

  it("can nest a new bookmark under the selected one", async () => {
    fetchMock.mockResolvedValue(ok({ bookmarks: BM }));
    render(<OrganizeBookmarksPanel docId="d1" currentPage={7} onJumpToPage={() => {}} onDocumentChanged={() => {}} />);
    fireEvent.click(await screen.findByText("Chapter 5"));
    fireEvent.click(screen.getByLabelText(/Nest under/));
    fireEvent.change(screen.getByPlaceholderText("New bookmark -> page 8"), { target: { value: "5.2" } });
    fireEvent.click(screen.getByLabelText("Add bookmark"));
    await waitFor(() => expect(calls().some((c) => c.method === "POST")).toBe(true));
    expect(calls().find((c) => c.method === "POST")!.body).toEqual({ title: "5.2", page: 7, parent: 3 });
  });

  it("indent / outdent use the subtree-moving endpoints", async () => {
    fetchMock.mockResolvedValue(ok({ bookmarks: BM }));
    render(<OrganizeBookmarksPanel docId="d1" currentPage={0} onJumpToPage={() => {}} onDocumentChanged={() => {}} />);
    await screen.findByText("Chapter 5");
    fireEvent.click(screen.getAllByLabelText("Indent")[3]);
    await waitFor(() => expect(calls().some((c) => c.url.endsWith("/bookmarks/3/indent"))).toBe(true));
    fireEvent.click(screen.getAllByLabelText("Outdent")[1]);
    await waitFor(() => expect(calls().some((c) => c.url.endsWith("/bookmarks/1/outdent"))).toBe(true));
  });

  it("drag-and-drop reorders through the move endpoint with before/inside/after", async () => {
    fetchMock.mockResolvedValue(ok({ bookmarks: BM }));
    render(<OrganizeBookmarksPanel docId="d1" currentPage={0} onJumpToPage={() => {}} onDocumentChanged={() => {}} />);
    await screen.findByText("Chapter 5");
    const row = (i: number) => screen.getByTestId(`bookmark-${i}`);
    for (const r of [0, 1, 2, 3, 4]) {
      row(r).getBoundingClientRect = () => ({ top: 0, height: 20, left: 0, width: 100, bottom: 20, right: 100, x: 0, y: 0, toJSON: () => ({}) });
    }
    // Chapter 5 dropped on the top edge of Intro -> before
    fireEvent.dragStart(row(3));
    fireEvent(row(0), dnd("dragover", 2));
    fireEvent(row(0), dnd("drop", 2));
    await waitFor(() => expect(calls().filter((c) => c.url.includes("/move")).length).toBe(1));
    let mv = calls().filter((c) => c.url.includes("/move"))[0];
    expect(mv.url).toMatch(/bookmarks\/3\/move$/);
    expect(mv.body).toEqual({ target: 0, position: "before" });

    // 5.1 dropped in the middle of Intro.b -> nested inside it
    fireEvent.dragStart(row(4));
    fireEvent(row(2), dnd("dragover", 10));
    fireEvent(row(2), dnd("drop", 10));
    await waitFor(() => expect(calls().filter((c) => c.url.includes("/move")).length).toBe(2));
    mv = calls().filter((c) => c.url.includes("/move"))[1];
    expect(mv.body).toEqual({ target: 2, position: "inside" });

    // Intro onto its own child: ignored
    fireEvent.dragStart(row(0));
    fireEvent(row(1), dnd("dragover", 10));
    fireEvent(row(1), dnd("drop", 10));
    await new Promise((r) => setTimeout(r, 20));
    expect(calls().filter((c) => c.url.includes("/move")).length).toBe(2);
  });
});

const RUNS: HFRun[] = [
  { id: "acrobat", source: "external", kind: "header-footer", pages: [0, 1], subtypes: ["Header"], created: null, settings: null, editable: false },
  {
    id: "r1234abcd", source: "pylor", kind: "bates", pages: [0, 1, 2], subtypes: ["Footer"], created: "2026-10-01T00:00:00Z", editable: true,
    settings: { footer_right: "{bates}", bates_prefix: "ACME", bates_digits: 4, bates_start: 7, font: "helv", font_size: 9, color: [0, 0, 0], margin_bottom: 30 },
  },
];

describe("OrganizeHeaderFooterDialog existing runs", () => {
  it("describes runs", () => {
    expect(describeRun(RUNS[0])).toBe("Header added by another application");
    expect(describeRun(RUNS[1])).toBe("Bates: ACME0007");
  });

  it("lists runs, removes one, and updates another keeping its hidden Bates settings", async () => {
    fetchMock.mockImplementation((u: string, init?: RequestInit) => {
      const m = init?.method ?? "GET";
      if (m === "GET") return Promise.resolve(ok({ runs: RUNS }));
      if (m === "DELETE") return Promise.resolve(ok({ status: "ok", removed: 2, runs: [RUNS[1]] }));
      return Promise.resolve(ok({ status: "ok", run_id: "r1234abcd", stamped_count: 3, runs: [] }));
    });
    const changed = vi.fn();
    const close = vi.fn();
    render(<OrganizeHeaderFooterDialog docId="d1" pageCount={3} open onClose={close} onDocumentChanged={changed} />);
    expect(await screen.findByText("Header added by another application")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Remove Header added by another application"));
    await waitFor(() => expect(screen.queryByText("Header added by another application")).not.toBeInTheDocument());
    expect(calls().some((c) => c.method === "DELETE" && c.url.endsWith("/header-footer/runs/acrobat"))).toBe(true);

    fireEvent.click(screen.getByLabelText("Edit Bates: ACME0007"));
    // fields are pre-filled from the run's settings
    expect(screen.getByLabelText("Footer right")).toHaveValue("{bates}");
    expect(screen.getByTestId("hf-preview-footer_right")).toHaveTextContent("ACME0007");
    fireEvent.change(screen.getByLabelText("Footer right"), { target: { value: "Doc {bates}" } });
    fireEvent.click(screen.getByText(/Update 3 page/));
    await waitFor(() => expect(close).toHaveBeenCalled());
    const put = calls().find((c) => c.method === "PUT")!;
    expect(put.url).toMatch(/header-footer\/runs\/r1234abcd$/);
    expect(put.body).toMatchObject({ footer_right: "Doc {bates}", bates_prefix: "ACME", bates_digits: 4, bates_start: 7, font_size: 9, margin_bottom: 30, kind: "bates" });
  });
});

describe("reply depths", () => {
  it("nests replies to replies", () => {
    const d = replyDepths({
      id: 1,
      replies: [
        { id: 2, page: 0, type: "note", author: "", contents: "", created: null, modified: null, in_reply_to: 1 },
        { id: 3, page: 0, type: "note", author: "", contents: "", created: null, modified: null, in_reply_to: 2 },
      ],
    });
    expect(d).toEqual({ 2: 1, 3: 2 });
  });
});
