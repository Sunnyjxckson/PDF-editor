/**
 * Undo / redo through the backend's snapshot history (advanced_ops /undo,
 * /redo). Every feature module snapshots before it writes, so one history
 * covers text edits, objects, forms, signatures, redaction, organize, OCR...
 */
import { undo, redo } from "./api";
import { useEditorStore } from "./store";

let inFlight = false;

async function step(kind: "undo" | "redo"): Promise<boolean> {
  const st = useEditorStore.getState();
  const docId = st.docId;
  if (!docId || inFlight) return false;
  inFlight = true;
  try {
    const res = kind === "undo" ? await undo(docId) : await redo(docId);
    // Only /undo names the operation reliably (/redo reports the label of the
    // snapshot it restored from, which belongs to the following operation).
    const op = kind === "undo" ? (res as { undone_operation?: string }).undone_operation : undefined;
    // Page count / sizes can change (organize ops), so re-fetch /info.
    await useEditorStore.getState().reloadDocument();
    const label = op && !op.startsWith("(") ? `: ${op}` : "";
    useEditorStore.getState().addToast(kind === "undo" ? `Undone${label}` : `Redone${label}`, "info");
    return true;
  } catch (e) {
    useEditorStore.getState().addToast(e instanceof Error ? e.message : `Nothing to ${kind}`, "info");
    return false;
  } finally {
    inFlight = false;
  }
}

export const performUndo = () => step("undo");
export const performRedo = () => step("redo");
