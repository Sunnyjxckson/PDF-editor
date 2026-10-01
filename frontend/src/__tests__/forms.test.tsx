import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import {
  listFormFields, fillFormFields, createFormField, updateFormField, deleteFormField, flattenForm,
  detectFormFields, importFormData, getFormDataExportUrl, formatFromFilename,
  normalizeRect, clampRect, rectToPercentStyle, clientToPoints, applyDrag, defaultRectAt,
  groupFieldsByName, missingRequired, useFormsStore, type FormField,
} from "@/lib/features/forms";
import FormsPanel from "@/components/features/FormsPanel";
import FormsOverlay from "@/components/features/FormsOverlay";

const mockFetch = vi.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ok = (data: unknown) => ({ ok: true, status: 200, json: () => Promise.resolve(data) });
const fail = (status: number, detail: unknown) => ({ ok: false, status, json: () => Promise.resolve({ detail }) });

const BASE = "http://localhost:8000/api/pdf/doc1/form-fields";

function field(p: Partial<FormField>): FormField {
  return {
    id: 1, page: 0, name: "f", type: "text", rect: [10, 10, 110, 30], pdf_rect: [10, 10, 110, 30],
    required: false, readonly: false, tooltip: "", font_size: 0, multiline: false, max_len: 0,
    options: [], option_labels: [], export_value: null, value: "", ...p,
  };
}

beforeEach(() => {
  mockFetch.mockReset();
  useFormsStore.setState({
    docId: null, fields: [], pages: [], loading: false, error: null,
    prepareMode: false, createType: "text", selectedId: null, version: 0,
  });
});

describe("forms API client", () => {
  it("lists fields, optionally per page", async () => {
    mockFetch.mockResolvedValue(ok({ fields: [], count: 0, is_form: false, pages: [] }));
    await listFormFields("doc1");
    await listFormFields("doc1", 2);
    expect(mockFetch.mock.calls[0][0]).toBe(BASE);
    expect(mockFetch.mock.calls[1][0]).toBe(`${BASE}?page=2`);
  });

  it("sends fill/create/update/delete/flatten/detect/import with correct verbs and bodies", async () => {
    mockFetch.mockResolvedValue(ok({ status: "ok", filled: [], errors: {}, field: field({}) }));
    await fillFormFields("doc1", { a: "x", b: true, r: null });
    await createFormField("doc1", { page: 0, type: "radio", rect: [1, 2, 3, 4], name: "g", export_value: "A" });
    await updateFormField("doc1", 42, { rect: [5, 6, 7, 8], required: true });
    await deleteFormField("doc1", 42, true);
    await flattenForm("doc1");
    await detectFormFields("doc1", { pages: [1] });
    await importFormData("doc1", "xfdf", "<xfdf/>");

    const calls = mockFetch.mock.calls.map(([url, init]) => [url, init?.method ?? "GET", init?.body && JSON.parse(init.body)]);
    expect(calls).toEqual([
      [`${BASE}/fill`, "POST", { values: { a: "x", b: true, r: null } }],
      [BASE, "POST", { page: 0, type: "radio", rect: [1, 2, 3, 4], name: "g", export_value: "A" }],
      [`${BASE}/42`, "PATCH", { rect: [5, 6, 7, 8], required: true }],
      [`${BASE}/42?whole_field=true`, "DELETE", undefined],
      [`${BASE}/flatten`, "POST", undefined],
      [`${BASE}/detect`, "POST", { pages: [1] }],
      [`${BASE}/import`, "POST", { format: "xfdf", data: "<xfdf/>" }],
    ]);
  });

  it("surfaces backend error details (string and structured)", async () => {
    mockFetch.mockResolvedValueOnce(fail(409, "A field named 'x' already exists"));
    await expect(createFormField("doc1", { page: 0, type: "text", rect: [0, 0, 9, 9], name: "x" })).rejects.toThrow(
      "A field named 'x' already exists",
    );
    mockFetch.mockResolvedValueOnce(fail(400, { message: "No fields were filled", errors: { c: "bad option" } }));
    await expect(fillFormFields("doc1", { c: "z" })).rejects.toThrow("No fields were filled; c: bad option");
  });

  it("builds export URLs and infers import formats", () => {
    expect(getFormDataExportUrl("doc1", "fdf")).toBe(`${BASE}/export?format=fdf`);
    expect(formatFromFilename("data.XFDF")).toBe("xfdf");
    expect(formatFromFilename("a.xml")).toBe("xfdf");
    expect(formatFromFilename("a.json")).toBe("json");
    expect(formatFromFilename("a.csv")).toBeNull();
  });
});

describe("forms geometry", () => {
  const page = { width: 612, height: 792 };
  it("normalizes and clamps rects", () => {
    expect(normalizeRect([50, 60, 10, 20])).toEqual([10, 20, 50, 60]);
    expect(clampRect([600, 780, 650, 800], page)).toEqual([562, 772, 612, 792]);
  });
  it("converts points to percentages and client px back to points (zoom-independent)", () => {
    expect(rectToPercentStyle([61.2, 79.2, 306, 396], page)).toEqual({ left: "10%", top: "10%", width: "40%", height: "40%" });
    // overlay drawn 2x larger on screen (zoom) at offset 100,50
    const box = { left: 100, top: 50, width: 1224, height: 1584 };
    expect(clientToPoints(100 + 122.4, 50 + 158.4, box, page)).toEqual([61.2, 79.2]);
    expect(clientToPoints(0, 0, box, page)).toEqual([0, 0]); // clamped
  });
  it("applies move and resize drags with a minimum size", () => {
    const r: [number, number, number, number] = [100, 100, 200, 120];
    expect(applyDrag(r, "move", 10, -5, page)).toEqual([110, 95, 210, 115]);
    expect(applyDrag(r, "se", 20, 30, page)).toEqual([100, 100, 220, 150]);
    expect(applyDrag(r, "w", 500, 0, page)).toEqual([194, 100, 200, 120]);
    expect(defaultRectAt("checkbox", 50, 50, page)).toEqual([50, 43, 64, 57]);
  });
  it("groups radio widgets and reports empty required fields", () => {
    const fs = [
      field({ id: 1, name: "plan", type: "radio", export_value: "a", value: null, required: true }),
      field({ id: 2, name: "plan", type: "radio", export_value: "b", value: null, required: true }),
      field({ id: 3, name: "nm", value: "Ada", required: true }),
      field({ id: 4, name: "ok", type: "checkbox", value: false, required: true }),
    ];
    const g = groupFieldsByName(fs);
    expect(g.map((x) => [x.name, x.widgets.length])).toEqual([["plan", 2], ["nm", 1], ["ok", 1]]);
    expect(missingRequired(fs)).toEqual(["plan", "ok"]);
  });
});

describe("FormsPanel", () => {
  const fields: FormField[] = [
    field({ id: 1, name: "full_name", value: "Ada", required: true }),
    field({ id: 2, name: "agree", type: "checkbox", value: false, export_value: "Yes", rect: [10, 40, 24, 54] }),
    field({ id: 3, name: "plan", type: "radio", options: ["basic", "pro"], option_labels: ["basic", "pro"], export_value: "basic", value: "basic" }),
    field({ id: 4, name: "plan", type: "radio", options: ["basic", "pro"], option_labels: ["basic", "pro"], export_value: "pro", value: "basic" }),
    field({ id: 5, name: "country", type: "combo", options: ["US", "CA"], option_labels: ["US", "CA"], value: "US" }),
    field({ id: 6, name: "sig", type: "signature", value: false }),
  ];

  it("renders a proper control per field type and fills via the API", async () => {
    mockFetch.mockImplementation((url: string, init?: RequestInit) => {
      if (!init) return Promise.resolve(ok({ fields, count: fields.length, is_form: true, pages: [] }));
      return Promise.resolve(ok({ status: "ok", filled: ["agree"], errors: {} }));
    });
    const changed = vi.fn();
    render(<FormsPanel docId="doc1" currentPage={0} onDocumentChanged={changed} />);

    expect(await screen.findByLabelText("full_name")).toHaveValue("Ada");
    expect(screen.getByLabelText("agree")).not.toBeChecked();
    expect(screen.getByRole("radiogroup", { name: "plan" })).toBeInTheDocument();
    expect(screen.getAllByRole("radio")).toHaveLength(2); // one radio group, two buttons
    expect(screen.getByLabelText("country")).toHaveValue("US");
    expect(screen.getByText(/sign it with the signature tool/)).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("agree"));
    await waitFor(() => expect(changed).toHaveBeenCalled());
    const fillCall = mockFetch.mock.calls.find(([u]) => String(u).endsWith("/fill"));
    expect(JSON.parse(fillCall![1].body)).toEqual({ values: { agree: true } });

    fireEvent.click(screen.getByLabelText("pro"));
    await waitFor(() =>
      expect(mockFetch.mock.calls.filter(([u]) => String(u).endsWith("/fill")).map(([, i]) => JSON.parse(i.body))).toContainEqual({
        values: { plan: "pro" },
      }),
    );
  });

  it("runs auto-detect in prepare mode and reports the result", async () => {
    mockFetch.mockImplementation((url: string, init?: RequestInit) => {
      if (String(url).endsWith("/detect"))
        return Promise.resolve(ok({ created: [{ type: "text" }, { type: "checkbox" }], candidates: [], count: 2 }));
      return Promise.resolve(ok({ fields: [], count: 0, is_form: false, pages: [] }));
    });
    render(<FormsPanel docId="doc1" currentPage={3} onDocumentChanged={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Prepare form/ }));
    fireEvent.click(screen.getByRole("button", { name: /Auto-detect fields/ }));
    expect(await screen.findByText("Created 2 fields (1 text, 1 checkbox)")).toBeInTheDocument();
    const call = mockFetch.mock.calls.find(([u]) => String(u).endsWith("/detect"));
    expect(JSON.parse(call![1].body)).toEqual({ pages: [3] });
  });
});

describe("FormsOverlay", () => {
  it("drag-creates a field with rect converted from screen pixels to PDF points", async () => {
    const created = field({ id: 77, name: "text", rect: [61.2, 79.2, 306, 118.8] });
    let serverFields: FormField[] = [];
    mockFetch.mockImplementation((url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        serverFields = [created];
        return Promise.resolve(ok({ status: "ok", field: created }));
      }
      return Promise.resolve(ok({ fields: serverFields, count: serverFields.length, is_form: true, pages: [] }));
    });
    useFormsStore.setState({ prepareMode: true, createType: "text", docId: "doc1" });
    const changed = vi.fn();
    render(<FormsOverlay docId="doc1" currentPage={0} pageWidth={612} pageHeight={792} onDocumentChanged={changed} />);
    const overlay = screen.getByTestId("forms-overlay");
    // On screen the page is drawn at 2x (e.g. 150dpi render + zoom), offset by (100, 50)
    overlay.getBoundingClientRect = () => ({ left: 100, top: 50, width: 1224, height: 1584, right: 1324, bottom: 1634, x: 100, y: 50, toJSON: () => ({}) });

    fireEvent.mouseDown(overlay, { button: 0, clientX: 100 + 122.4, clientY: 50 + 158.4 });
    await act(async () => {
      fireEvent.mouseMove(window, { clientX: 100 + 612, clientY: 50 + 237.6 });
    });
    await act(async () => {
      fireEvent.mouseUp(window);
    });
    await waitFor(() => expect(changed).toHaveBeenCalled());
    const post = mockFetch.mock.calls.find(([, i]) => i?.method === "POST");
    expect(JSON.parse(post![1].body)).toEqual({ page: 0, type: "text", rect: [61.2, 79.2, 306, 118.8] });
    expect(useFormsStore.getState().selectedId).toBe(77);
  });

  it("toggles a checkbox in fill mode", async () => {
    const cb = field({ id: 9, name: "agree", type: "checkbox", value: false, export_value: "On", rect: [10, 10, 24, 24] });
    mockFetch.mockImplementation((url: string, init?: RequestInit) => {
      if (init?.method === "POST") return Promise.resolve(ok({ status: "ok", filled: ["agree"], errors: {} }));
      return Promise.resolve(ok({ fields: [cb], count: 1, is_form: true, pages: [] }));
    });
    useFormsStore.setState({ fields: [cb], docId: "doc1" });
    render(<FormsOverlay docId="doc1" currentPage={0} pageWidth={612} pageHeight={792} onDocumentChanged={() => {}} />);
    const box = document.querySelector('[data-field-id="9"]') as HTMLElement;
    expect(box.style.left).toBe(`${(10 / 612) * 100}%`);
    fireEvent.click(box);
    await waitFor(() => expect(mockFetch.mock.calls.some(([u]) => String(u).endsWith("/fill"))).toBe(true));
    const fillCall = mockFetch.mock.calls.find(([u]) => String(u).endsWith("/fill"));
    expect(JSON.parse(fillCall![1].body)).toEqual({ values: { agree: "On" } });
  });
});
