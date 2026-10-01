"use client";

/**
 * Shown instead of the chat when the server has no Anthropic API key. The key
 * is POSTed once to /api/ai/key, kept server-side, and never read back.
 */
import { useState } from "react";
import { KeyRound, Loader2 } from "lucide-react";
import { setAIKey } from "@/lib/features/ai";

export interface AISetupCardProps {
  reason?: string;
  message?: string;
  onConfigured: () => void;
}

export default function AISetupCard({ reason, message, onConfigured }: AISetupCardProps) {
  const [key, setKey] = useState("");
  const [persist, setPersist] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (!key.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const r = await setAIKey(key, persist);
      setKey("");
      if (r.ai_available) onConfigured();
      else setError("The key was stored but the AI SDK is not available on the server.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div data-testid="ai-setup" className="m-4 rounded-xl border border-purple-200 dark:border-purple-800 bg-purple-50/60 dark:bg-purple-950/30 p-4 space-y-3">
      <div className="flex items-center gap-2">
        <KeyRound className="w-4 h-4 text-purple-500" />
        <h3 className="text-sm font-semibold">Connect Claude</h3>
      </div>
      <p className="text-xs text-gray-600 dark:text-gray-300">
        {reason === "auth_failed"
          ? "The saved Anthropic API key was rejected. Paste a valid key."
          : reason === "missing_sdk"
            ? message
            : "The AI assistant needs an Anthropic API key. It is stored on your server only and is never sent back to the browser."}
      </p>
      {reason !== "missing_sdk" && (
        <>
          <input
            type="password"
            autoComplete="off"
            aria-label="Anthropic API key"
            placeholder="sk-ant-..."
            value={key}
            onChange={(e) => setKey(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
            className="w-full rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-purple-500"
          />
          <label className="flex items-center gap-2 text-xs text-gray-600 dark:text-gray-300">
            <input type="checkbox" checked={persist} onChange={(e) => setPersist(e.target.checked)} />
            Also save to the server&apos;s backend/.env (otherwise kept in memory until restart)
          </label>
          <button
            onClick={submit}
            disabled={!key.trim() || busy}
            className="w-full rounded-lg bg-purple-600 hover:bg-purple-700 text-white text-sm py-2 disabled:opacity-40 flex items-center justify-center gap-2"
          >
            {busy && <Loader2 className="w-4 h-4 animate-spin" />}
            Save key
          </button>
          <p className="text-[11px] text-gray-400">
            Get a key at console.anthropic.com → API keys.
          </p>
        </>
      )}
      {error && <p role="alert" className="text-xs text-red-600">{error}</p>}
    </div>
  );
}
