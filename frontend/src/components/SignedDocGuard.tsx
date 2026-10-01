"use client";

import { useEffect, useRef, useState } from "react";
import { ShieldAlert } from "lucide-react";
import {
  installSignedDocFetchGuard,
  setSignedDocConfirmHandler,
  type SignedDocChoice,
  type SignedDocConflict,
} from "@/lib/api";

interface Pending {
  info: SignedDocConflict;
  resolve: (choice: SignedDocChoice) => void;
}

/**
 * Mount once in the editor shell. Routes every API fetch through the
 * signed-document guard and shows the confirm dialog when the backend answers
 * 409 signed_document. Continue / Save a copy first retry the edit with
 * X-Allow-Break-Signature: 1; Cancel leaves the signed PDF untouched.
 */
export default function SignedDocGuard() {
  const [pending, setPending] = useState<Pending | null>(null);
  const continueRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const uninstall = installSignedDocFetchGuard();
    const unregister = setSignedDocConfirmHandler(
      (info) => new Promise<SignedDocChoice>((resolve) => setPending({ info, resolve })),
    );
    return () => {
      unregister();
      uninstall();
    };
  }, []);

  useEffect(() => {
    if (pending) continueRef.current?.focus();
  }, [pending]);

  if (!pending) return null;

  const { info } = pending;
  const who = info.signers.length ? info.signers.join(", ") : "an unknown signer";
  const choose = (c: SignedDocChoice) => {
    pending.resolve(c);
    setPending(null);
  };

  return (
    <div
      className="fixed inset-0 z-[110] flex items-center justify-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="signed-doc-title"
      aria-describedby="signed-doc-desc"
      onKeyDown={(e) => {
        if (e.key === "Escape") choose("cancel");
      }}
    >
      <div className="w-full max-w-md rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 p-5 shadow-2xl">
        <div className="flex items-start gap-3">
          <div className="rounded-full bg-amber-100 dark:bg-amber-900/40 p-2">
            <ShieldAlert className="w-5 h-5 text-amber-600 dark:text-amber-400" />
          </div>
          <div className="flex-1">
            <h2 id="signed-doc-title" className="text-base font-semibold">
              This PDF is digitally signed
            </h2>
            <p id="signed-doc-desc" className="mt-1 text-sm text-gray-600 dark:text-gray-400">
              This PDF is digitally signed by <strong>{who}</strong>. Editing will invalidate the
              signature{info.signers.length > 1 ? "s" : ""}. Recipients will see the document as
              modified after signing.
            </p>
            <p className="mt-2 text-xs text-gray-500">
              Save a copy first downloads the signed original, then applies your edit.
            </p>
          </div>
        </div>
        <div className="mt-5 flex flex-wrap justify-end gap-2">
          <button
            type="button"
            onClick={() => choose("cancel")}
            className="rounded-lg px-3 py-1.5 text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => choose("copy")}
            className="rounded-lg border border-gray-300 dark:border-gray-600 px-3 py-1.5 text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
          >
            Save a copy first
          </button>
          <button
            ref={continueRef}
            type="button"
            onClick={() => choose("continue")}
            className="rounded-lg bg-amber-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-700"
          >
            Continue
          </button>
        </div>
      </div>
    </div>
  );
}
