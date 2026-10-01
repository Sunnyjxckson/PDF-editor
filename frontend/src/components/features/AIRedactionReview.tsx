"use client";

/**
 * Smart-redaction review list: the AI proposes, the user decides. Applying is
 * a REAL redaction (content removed from the file) and clears undo history.
 */
import { useMemo, useState } from "react";
import { ShieldAlert, Loader2 } from "lucide-react";
import { applyReviewedRedactions, type RedactionReviewItem } from "@/lib/features/ai";

export interface AIRedactionReviewProps {
  docId: string;
  items: RedactionReviewItem[];
  allowBreakSignature?: boolean;
  onShow?: (item: RedactionReviewItem) => void;
  onApplied: (count: number) => void;
}

export default function AIRedactionReview({ docId, items, allowBreakSignature, onShow, onApplied }: AIRedactionReviewProps) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set(items.map((i) => i.id)));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [applied, setApplied] = useState(false);

  const byCategory = useMemo(() => {
    const m = new Map<string, RedactionReviewItem[]>();
    for (const it of items) m.set(it.category, [...(m.get(it.category) ?? []), it]);
    return [...m.entries()];
  }, [items]);

  const toggle = (id: string) =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const apply = async () => {
    const chosen = items.filter((i) => selected.has(i.id));
    if (!chosen.length) return;
    setBusy(true);
    setError(null);
    try {
      await applyReviewedRedactions(docId, chosen, allowBreakSignature);
      setApplied(true);
      onApplied(chosen.length);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!items.length) return null;
  return (
    <div data-testid="ai-redaction-review" className="mt-2 rounded-xl border border-red-200 dark:border-red-900 bg-white dark:bg-gray-900 p-3 text-xs space-y-2">
      <div className="flex items-center gap-1.5 font-semibold text-red-700 dark:text-red-400">
        <ShieldAlert className="w-4 h-4" />
        Review {items.length} proposed redaction{items.length === 1 ? "" : "s"}
      </div>
      <div className="max-h-56 overflow-y-auto space-y-2">
        {byCategory.map(([cat, list]) => (
          <div key={cat}>
            <div className="uppercase tracking-wide text-[10px] text-gray-400 mb-0.5">{cat}</div>
            {list.map((it) => (
              <label key={it.id} className="flex items-start gap-2 py-0.5">
                <input
                  type="checkbox"
                  aria-label={`Redact ${it.text}`}
                  checked={selected.has(it.id)}
                  disabled={applied}
                  onChange={() => toggle(it.id)}
                  className="mt-0.5"
                />
                <span className="flex-1">
                  <span className="font-medium">{it.text}</span>
                  {it.reason && <span className="text-gray-500"> — {it.reason}</span>}
                </span>
                <button type="button" onClick={() => onShow?.(it)} className="text-purple-600 hover:underline shrink-0">
                  p. {it.page}
                </button>
              </label>
            ))}
          </div>
        ))}
      </div>
      {applied ? (
        <p className="text-green-700 dark:text-green-400">Redactions applied. The content is permanently removed.</p>
      ) : (
        <>
          <p className="text-gray-500">Applying permanently removes the selected text and clears undo history.</p>
          <button
            onClick={apply}
            disabled={busy || selected.size === 0}
            className="w-full rounded-lg bg-red-600 hover:bg-red-700 text-white py-1.5 disabled:opacity-40 flex items-center justify-center gap-1.5"
          >
            {busy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            Redact {selected.size} selected
          </button>
        </>
      )}
      {error && <p role="alert" className="text-red-600">{error}</p>}
    </div>
  );
}
