"use client";

import { useState } from "react";
import { AlertTriangle } from "lucide-react";

export interface RedactConfirmDialogProps {
  open: boolean;
  markCount: number;
  pageCount: number;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Irreversible-action confirmation for applying redactions. */
export default function RedactConfirmDialog({
  open, markCount, pageCount, busy, onConfirm, onCancel,
}: RedactConfirmDialogProps) {
  const [ack, setAck] = useState(false);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50 p-4" role="dialog" aria-modal="true" aria-labelledby="redact-confirm-title">
      <div className="w-full max-w-md rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 p-5 shadow-2xl">
        <div className="flex items-start gap-3">
          <div className="rounded-full bg-red-100 dark:bg-red-900/40 p-2">
            <AlertTriangle className="w-5 h-5 text-red-600 dark:text-red-400" />
          </div>
          <div className="flex-1">
            <h2 id="redact-confirm-title" className="text-base font-semibold">
              Permanently redact {markCount} area{markCount === 1 ? "" : "s"}?
            </h2>
            <p className="mt-1 text-sm text-gray-600 dark:text-gray-400">
              Text, image pixels and vector graphics under the marked areas on {pageCount} page
              {pageCount === 1 ? "" : "s"} will be <strong>deleted from the PDF</strong>, not just covered.
              Anyone you send the exported file to cannot recover them.
            </p>
            <p className="mt-2 text-xs text-gray-500 dark:text-gray-500">
              This cannot be undone: undo history is cleared so no unredacted copy is kept.
            </p>
            <label className="mt-3 flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={ack}
                onChange={(e) => setAck(e.target.checked)}
                className="h-4 w-4 accent-red-600"
              />
              I understand this cannot be reversed in the exported file
            </label>
          </div>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={() => { setAck(false); onCancel(); }}
            className="rounded-lg px-3 py-1.5 text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!ack || busy}
            onClick={() => { setAck(false); onConfirm(); }}
            className="rounded-lg bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {busy ? "Applying…" : "Apply redactions"}
          </button>
        </div>
      </div>
    </div>
  );
}
