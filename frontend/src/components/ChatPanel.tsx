"use client";

/**
 * AI chat side panel. Streams /api/ai/chat/stream: text tokens, live tool
 * progress, citations that jump to and highlight the source passage, a
 * smart-redaction review list, downloads, a change summary with one-click
 * undo, a Stop button, the signed-document confirmation, and a key-setup
 * state when the server has no Anthropic API key.
 */
import { useState, useRef, useEffect, useCallback } from "react";
import {
  X, Send, Sparkles, Pin, PinOff, Trash2, CheckCircle, ScanSearch, Square, Undo2,
  Wrench, AlertTriangle, Download, FileText, Loader2, Paperclip,
} from "lucide-react";
import * as Tooltip from "@radix-ui/react-tooltip";
import { useEditorStore } from "@/lib/store";
import { getDocumentInfo, API_BASE } from "@/lib/api";
import {
  getAIConfig, setAIModel, streamAIChat, locateQuote, undoSteps, loadProfile, uploadReferencePdf,
  useAIStore, ACTION_LABELS,
  type AIConfig, type AIEvent, type AIChange, type AICitation, type AIChatRequest,
  type RedactionReviewItem, type StreamHandle, type AIQueuedRequest,
} from "@/lib/features/ai";
import AIMessageContent from "./features/AIMessageContent";
import AISetupCard from "./features/AISetupCard";
import AIRedactionReview from "./features/AIRedactionReview";

interface ToolTrace {
  id: string;
  label: string;
  status: "running" | "ok" | "error";
  summary?: string;
}

interface ChatMsg {
  id: number;
  role: "user" | "assistant";
  content: string;
  streaming?: boolean;
  tools?: ToolTrace[];
  citations?: AICitation[];
  changes?: AIChange[];
  undoSteps?: number;
  undone?: boolean;
  review?: RedactionReviewItem[];
  files?: { filename: string; mime: string; content: string }[];
  downloads?: { url: string; label: string }[];
  confirm?: string;
  setup?: { reason: string; message: string };
  error?: string;
  stopped?: boolean;
  request?: AIChatRequest;
}

const SUGGESTIONS = [
  "Summarize this document",
  "What are the key dates and amounts?",
  "Rewrite the first paragraph to be more concise",
  "Translate this document to Spanish",
  "Find and redact personal information",
  "Fill this form from my profile",
  "Add page numbers at the bottom",
  "Extract all tables as CSV",
];

const QUICK_ACTIONS: Array<{ label: string; req: AIQueuedRequest }> = [
  { label: "Summarize", req: { action: "summarize_bullets", label: "Bullet summary" } },
  { label: "Fix grammar", req: { action: "fix_grammar_page", label: "Fix grammar (page)" } },
  { label: "Smart redact", req: { action: "smart_redact", label: "Smart redaction" } },
  { label: "Tables → CSV", req: { action: "extract_tables_csv", label: "Tables to CSV" } },
];

const WELCOME: ChatMsg = {
  id: 0,
  role: "assistant",
  content:
    "Hi! I can read this whole PDF, answer with page citations, and **edit it for you** — rewrite or translate text in place, fill forms, redact, reorganize pages, add page numbers and more. Every change can be undone.",
};

let nextId = 1;

export default function ChatPanel() {
  const {
    docId, chatOpen, setChatOpen, currentPage, totalPages, bumpVersion, setDocument, setCurrentPage,
    chatPinned, setChatPinned, addToast, regionSelection, setRegionSelection,
  } = useEditorStore();
  const queued = useAIStore((s) => s.queued);
  const takeQueued = useAIStore((s) => s.takeQueued);
  const setHighlight = useAIStore((s) => s.setHighlight);
  const references = useAIStore((s) => s.references);
  const addReference = useAIStore((s) => s.addReference);
  const removeReference = useAIStore((s) => s.removeReference);

  const [messages, setMessages] = useState<ChatMsg[]>([WELCOME]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [panelWidth, setPanelWidth] = useState(420);
  const [config, setConfig] = useState<AIConfig | null>(null);
  const [allowSigned, setAllowSigned] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const resizingRef = useRef(false);
  const streamRef = useRef<StreamHandle | null>(null);

  const refreshConfig = useCallback(() => {
    getAIConfig().then(setConfig).catch(() => setConfig(null));
  }, []);

  useEffect(() => {
    if (chatOpen) refreshConfig();
  }, [chatOpen, refreshConfig]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView?.({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (chatOpen) setTimeout(() => inputRef.current?.focus(), 100);
  }, [chatOpen]);

  useEffect(() => () => streamRef.current?.abort(), []);

  const handleResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    resizingRef.current = true;
    const startX = e.clientX;
    const startWidth = panelWidth;
    const onMove = (ev: MouseEvent) => {
      if (resizingRef.current) setPanelWidth(Math.min(680, Math.max(320, startWidth + startX - ev.clientX)));
    };
    const onUp = () => {
      resizingRef.current = false;
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }, [panelWidth]);

  const patchLast = (fn: (m: ChatMsg) => ChatMsg) =>
    setMessages((prev) => {
      const i = prev.findLastIndex((m) => m.role === "assistant");
      if (i < 0) return prev;
      const next = [...prev];
      next[i] = fn(next[i]);
      return next;
    });

  const reloadInfo = useCallback(async () => {
    if (!docId) return;
    try {
      const info = await getDocumentInfo(docId);
      setDocument(info, docId);
      if (currentPage >= info.page_count) setCurrentPage(Math.max(0, info.page_count - 1));
    } catch {
      /* ignore */
    }
  }, [docId, setDocument, currentPage, setCurrentPage]);

  const handleEvent = useCallback((ev: AIEvent) => {
    switch (ev.type) {
      case "text":
        patchLast((m) => ({ ...m, content: m.content + ev.delta }));
        break;
      case "tool_start":
        patchLast((m) => ({ ...m, tools: [...(m.tools ?? []), { id: ev.id, label: ev.label || ev.name, status: "running" }] }));
        break;
      case "tool_result":
        patchLast((m) => ({
          ...m,
          tools: (m.tools ?? []).map((t) => t.id === ev.id ? { ...t, status: ev.ok ? "ok" : "error", summary: ev.summary } : t),
        }));
        if (ev.changed) bumpVersion();
        break;
      case "redaction_review":
        patchLast((m) => ({ ...m, review: ev.items }));
        break;
      case "file":
        patchLast((m) => ({ ...m, files: [...(m.files ?? []), { filename: ev.filename, mime: ev.mime, content: ev.content }] }));
        break;
      case "download":
        patchLast((m) => ({ ...m, downloads: [...(m.downloads ?? []), { url: ev.url, label: ev.label }] }));
        break;
      case "needs_confirmation":
        patchLast((m) => ({ ...m, confirm: ev.message }));
        break;
      case "setup_required":
        patchLast((m) => ({ ...m, setup: { reason: ev.reason, message: ev.message }, streaming: false }));
        setConfig((c) => (c ? { ...c, ai_available: false, api_key_set: ev.reason !== "no_api_key" && c.api_key_set } : c));
        break;
      case "error":
        patchLast((m) => ({ ...m, error: ev.message }));
        break;
      case "done":
        patchLast((m) => ({
          ...m,
          streaming: false,
          content: ev.response && ev.response.length >= m.content.trim().length ? ev.response : m.content,
          citations: ev.citations,
          changes: ev.changes,
          undoSteps: ev.undo_steps,
          stopped: ev.stopped,
        }));
        setLoading(false);
        streamRef.current = null;
        if (ev.changed) {
          bumpVersion();
          addToast(`${ev.changes.length} change${ev.changes.length === 1 ? "" : "s"} applied`, "success");
        }
        if (ev.page_count_changed || ev.new_page_count !== null) void reloadInfo();
        if (regionSelection) setRegionSelection(null);
        break;
    }
  }, [bumpVersion, addToast, reloadInfo, regionSelection, setRegionSelection]);

  const send = useCallback((req: Omit<AIChatRequest, "doc_id" | "current_page">, display: string, extra?: Partial<AIChatRequest>) => {
    if (!docId || loading) return;
    const region = regionSelection
      ? { page: regionSelection.page, x: regionSelection.rect.x, y: regionSelection.rect.y,
          width: regionSelection.rect.width, height: regionSelection.rect.height }
      : null;
    const full: AIChatRequest = {
      doc_id: docId,
      current_page: currentPage,
      region,
      reference_doc_ids: references.map((r) => r.id),
      allow_break_signature: allowSigned,
      ...req,
      ...extra,
    };
    setMessages((prev) => [
      ...prev,
      { id: nextId++, role: "user", content: display },
      { id: nextId++, role: "assistant", content: "", streaming: true, request: full },
    ]);
    setLoading(true);
    streamRef.current?.abort();
    streamRef.current = streamAIChat(full, handleEvent);
  }, [docId, loading, regionSelection, currentPage, references, allowSigned, handleEvent]);

  const handleSend = useCallback((override?: string) => {
    const text = (override ?? input).trim();
    if (!text) return;
    if (override === undefined) setInput("");
    const wantsProfile = /\bprofile\b/i.test(text);
    send({ message: text, profile: wantsProfile ? loadProfile() : null }, text);
  }, [input, send]);

  // One-click actions queued by AIPanel.
  useEffect(() => {
    if (!queued || !chatOpen || loading || !docId) return;
    const q = takeQueued();
    if (!q) return;
    send(
      {
        action: q.action,
        message: q.message,
        action_options: q.options,
        selection_text: q.selectionText ?? undefined,
        profile: q.includeProfile ? loadProfile() : null,
      },
      q.label || (q.action ? ACTION_LABELS[q.action] : q.message ?? ""),
    );
  }, [queued, chatOpen, loading, docId, takeQueued, send]);

  const handleStop = () => {
    streamRef.current?.abort();
  };

  const handleClear = () => {
    streamRef.current?.abort();
    streamRef.current = null;
    setMessages([WELCOME]);
    setInput("");
    setLoading(false);
  };

  const handleUndo = async (msg: ChatMsg) => {
    if (!docId || !msg.undoSteps) return;
    const n = await undoSteps(docId, msg.undoSteps);
    setMessages((prev) => prev.map((m) => (m.id === msg.id ? { ...m, undone: true } : m)));
    bumpVersion();
    void reloadInfo();
    addToast(n === msg.undoSteps ? "AI changes undone" : `Undid ${n} of ${msg.undoSteps} changes`, n ? "success" : "error");
  };

  const handleProceedSigned = (msg: ChatMsg) => {
    if (!msg.request) return;
    setAllowSigned(true);
    setMessages((prev) => prev.map((m) => (m.id === msg.id ? { ...m, confirm: undefined } : m)));
    const { doc_id: _d, current_page: _c, ...rest } = msg.request;
    void _d; void _c;
    send(rest, "Proceed anyway (this breaks the digital signature)", { allow_break_signature: true });
  };

  const handleCitation = async (msg: ChatMsg, page: number, quote: string | null, marker: string) => {
    if (!docId || page < 1 || (totalPages && page > totalPages)) return;
    setCurrentPage(page - 1);
    let rects = msg.citations?.find((c) => c.marker === marker)?.rects ?? [];
    if (!rects.length && quote) {
      try {
        rects = (await locateQuote(docId, page, quote)).rects;
      } catch {
        rects = [];
      }
    }
    setHighlight({ page, rects, quote });
  };

  const saveFile = (f: { filename: string; mime: string; content: string }) => {
    const url = URL.createObjectURL(new Blob([f.content], { type: f.mime }));
    const a = document.createElement("a");
    a.href = url;
    a.download = f.filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  };

  const attachReference = async (file: File | undefined) => {
    if (!file) return;
    try {
      const r = await uploadReferencePdf(file);
      addReference({ id: r.id, filename: r.filename });
      addToast(`Attached ${r.filename} as a reference`, "success");
    } catch (e) {
      addToast((e as Error).message, "error");
    }
  };

  const changeModel = async (model: string) => {
    try {
      const r = await setAIModel(model);
      setConfig((c) => (c ? { ...c, model: r.model } : c));
    } catch (e) {
      addToast((e as Error).message, "error");
    }
  };

  if (!docId || !chatOpen) return null;
  const pinned = chatPinned;
  const needsSetup = config !== null && !config.ai_available;

  return (
    <Tooltip.Provider delayDuration={300}>
      <div
        data-testid="chat-panel"
        className={`${pinned ? "relative shrink-0" : "fixed inset-0 sm:inset-auto sm:right-0 sm:top-0 sm:bottom-0 z-50"} flex flex-col bg-white dark:bg-gray-900 border-l border-gray-200 dark:border-gray-800 shadow-2xl animate-slide-in-right`}
        style={{ width: pinned ? panelWidth : undefined, maxWidth: pinned ? undefined : panelWidth }}
      >
        <div onMouseDown={handleResizeStart} className="resize-handle absolute left-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-purple-400/40 transition-colors z-10" />

        {/* Header */}
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-gray-200 dark:border-gray-800 shrink-0 gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <Sparkles className="w-5 h-5 text-purple-500 shrink-0" />
            <h2 className="font-semibold text-sm truncate">AI Assistant</h2>
            {config?.ai_available && (
              <select
                aria-label="Model"
                value={config.model}
                onChange={(e) => changeModel(e.target.value)}
                disabled={loading}
                className="text-[11px] rounded-md border border-gray-200 dark:border-gray-700 bg-transparent px-1 py-0.5 max-w-[9rem]"
              >
                {config.models.map((m) => (
                  <option key={m.id} value={m.id}>{m.label}</option>
                ))}
              </select>
            )}
          </div>
          <div className="flex items-center gap-1">
            <span className="text-xs text-gray-400 mr-1 hidden sm:inline">p. {currentPage + 1}/{totalPages}</span>
            <IconBtn label="Attach a reference PDF" onClick={() => fileRef.current?.click()}>
              <Paperclip className="w-3.5 h-3.5" />
            </IconBtn>
            <IconBtn label="Clear chat" onClick={handleClear}>
              <Trash2 className="w-3.5 h-3.5" />
            </IconBtn>
            <IconBtn label={pinned ? "Unpin panel" : "Pin panel"} onClick={() => setChatPinned(!pinned)} active={pinned}>
              {pinned ? <PinOff className="w-3.5 h-3.5" /> : <Pin className="w-3.5 h-3.5" />}
            </IconBtn>
            <button onClick={() => setChatOpen(false)} aria-label="Close chat" className="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800">
              <X className="w-4 h-4" />
            </button>
          </div>
          <input
            ref={fileRef}
            type="file"
            accept="application/pdf"
            className="hidden"
            data-testid="ai-reference-input"
            onChange={(e) => { void attachReference(e.target.files?.[0]); e.target.value = ""; }}
          />
        </div>

        {references.length > 0 && (
          <div className="px-3 py-1.5 border-b border-gray-100 dark:border-gray-800 flex flex-wrap gap-1">
            {references.map((r) => (
              <span key={r.id} className="inline-flex items-center gap-1 text-[11px] rounded-full bg-gray-100 dark:bg-gray-800 px-2 py-0.5">
                <FileText className="w-3 h-3" /> {r.filename}
                <button aria-label={`Remove ${r.filename}`} onClick={() => removeReference(r.id)}><X className="w-3 h-3" /></button>
              </span>
            ))}
          </div>
        )}

        {needsSetup && <AISetupCard reason={config?.sdk_installed === false ? "missing_sdk" : undefined} onConfigured={refreshConfig} />}

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4" data-testid="chat-messages">
          {messages.map((msg) => (
            <div key={msg.id}>
              <div className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
                <div className={`max-w-[90%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${msg.role === "user" ? "bg-purple-600 text-white rounded-br-md whitespace-pre-wrap" : "bg-gray-100 dark:bg-gray-800 text-gray-800 dark:text-gray-200 rounded-bl-md"}`}>
                  {msg.role === "user" ? msg.content : (
                    <div className="chat-content">
                      {msg.tools && msg.tools.length > 0 && (
                        <ul className="mb-2 space-y-0.5" data-testid="ai-tools">
                          {msg.tools.map((t) => (
                            <li key={t.id} className="flex items-start gap-1.5 text-[11px] text-gray-500 dark:text-gray-400">
                              {t.status === "running" ? <Loader2 className="w-3 h-3 mt-0.5 animate-spin shrink-0" />
                                : t.status === "ok" ? <CheckCircle className="w-3 h-3 mt-0.5 text-green-500 shrink-0" />
                                  : <AlertTriangle className="w-3 h-3 mt-0.5 text-amber-500 shrink-0" />}
                              <span><Wrench className="inline w-3 h-3 mr-0.5" />{t.label}{t.summary ? ` — ${t.summary}` : ""}</span>
                            </li>
                          ))}
                        </ul>
                      )}
                      {msg.content ? (
                        <AIMessageContent text={msg.content} onCitation={(p, q, mk) => handleCitation(msg, p, q, mk)} />
                      ) : msg.streaming && !msg.tools?.length ? (
                        <span className="inline-block w-2 h-4 bg-gray-400 animate-pulse" />
                      ) : null}
                      {msg.streaming && msg.content && <span className="inline-block w-1.5 h-4 bg-purple-500 animate-pulse ml-0.5 align-text-bottom" />}
                      {msg.setup && !needsSetup && (
                        <AISetupCard reason={msg.setup.reason} message={msg.setup.message} onConfigured={refreshConfig} />
                      )}
                      {msg.error && <p role="alert" className="mt-2 text-xs text-red-600 dark:text-red-400">{msg.error}</p>}
                      {msg.stopped && <p className="mt-1 text-[11px] text-gray-400">Stopped.</p>}
                      {msg.confirm && (
                        <div className="mt-2 rounded-lg border border-amber-300 bg-amber-50 dark:bg-amber-950/40 p-2 text-xs space-y-1.5" data-testid="ai-signed-confirm">
                          <p>{msg.confirm}</p>
                          <button onClick={() => handleProceedSigned(msg)} className="px-2 py-1 rounded-md bg-amber-600 text-white hover:bg-amber-700">
                            Proceed anyway (breaks signature)
                          </button>
                        </div>
                      )}
                      {msg.review && docId && (
                        <AIRedactionReview
                          docId={docId}
                          items={msg.review}
                          allowBreakSignature={allowSigned}
                          onShow={(it) => { setCurrentPage(it.page - 1); setHighlight({ page: it.page, rects: it.rects, quote: it.text }); }}
                          onApplied={(n) => { bumpVersion(); addToast(`Redacted ${n} item${n === 1 ? "" : "s"}`, "success"); }}
                        />
                      )}
                      {(msg.files?.length || msg.downloads?.length) ? (
                        <div className="mt-2 flex flex-wrap gap-1.5">
                          {msg.files?.map((f, i) => (
                            <button key={`f${i}`} onClick={() => saveFile(f)} className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded-md bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 hover:border-purple-400">
                              <Download className="w-3 h-3" /> {f.filename}
                            </button>
                          ))}
                          {msg.downloads?.map((d, i) => (
                            <a key={`d${i}`} href={`${API_BASE}${d.url}`} className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded-md bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 hover:border-purple-400">
                              <Download className="w-3 h-3" /> {d.label}
                            </a>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  )}
                </div>
              </div>
              {msg.role === "assistant" && !msg.streaming && msg.changes && msg.changes.length > 0 && (
                <div className="mt-1 ml-1 flex flex-wrap items-center gap-1.5" data-testid="ai-changes">
                  <span className="inline-flex items-center gap-1 text-xs text-green-600 dark:text-green-400 bg-green-50 dark:bg-green-900/30 px-2 py-0.5 rounded-full">
                    <CheckCircle className="w-3 h-3" />
                    {msg.changes.length} change{msg.changes.length === 1 ? "" : "s"} applied
                  </span>
                  {!!msg.undoSteps && !msg.undone && (
                    <button onClick={() => handleUndo(msg)} className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-800">
                      <Undo2 className="w-3 h-3" /> Undo
                    </button>
                  )}
                  {msg.undone && <span className="text-xs text-gray-400">Undone</span>}
                </div>
              )}
            </div>
          ))}
          <div ref={messagesEndRef} />
          {messages.length <= 1 && !loading && !needsSetup && (
            <div className="pt-2">
              <p className="text-xs text-gray-400 mb-2">Try one of these:</p>
              <div className="flex flex-wrap gap-1.5">
                {SUGGESTIONS.map((s) => (
                  <button key={s} onClick={() => { setInput(s); inputRef.current?.focus(); }} className="text-xs px-2.5 py-1.5 rounded-full bg-gray-100 dark:bg-gray-800 hover:bg-purple-100 dark:hover:bg-purple-900/30 text-gray-600 dark:text-gray-300 transition-colors">
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Quick actions */}
        <div className="px-3 pt-2 pb-1 shrink-0 border-t border-gray-100 dark:border-gray-800/50">
          <div className="flex flex-wrap gap-1.5">
            {QUICK_ACTIONS.map((a) => (
              <button
                key={a.label}
                onClick={() => send({ action: a.req.action }, a.req.label)}
                disabled={loading || needsSetup}
                className="text-xs px-2.5 py-1 rounded-full border border-purple-200 dark:border-purple-800 text-purple-600 dark:text-purple-400 hover:bg-purple-50 dark:hover:bg-purple-900/30 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {a.label}
              </button>
            ))}
          </div>
        </div>

        {regionSelection && (
          <div className="px-3 py-2 shrink-0 border-t border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-950/50 flex items-center gap-2">
            <ScanSearch className="w-4 h-4 text-blue-500 shrink-0" />
            <span className="text-xs text-blue-700 dark:text-blue-300 flex-1">
              Selected region on page {regionSelection.page + 1} — your next message is about this area
            </span>
            <button onClick={() => setRegionSelection(null)} aria-label="Clear region" className="p-0.5 rounded hover:bg-blue-200 dark:hover:bg-blue-800 text-blue-500">
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        )}

        {/* Input */}
        <div className="border-t border-gray-200 dark:border-gray-800 p-3 shrink-0 safe-area-bottom">
          <div className="flex gap-2 items-end">
            <textarea
              ref={inputRef}
              value={input}
              aria-label="Message"
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  handleSend();
                }
                if (e.key === "Escape") setChatOpen(false);
              }}
              placeholder={needsSetup ? "Add an API key above to start" : "Ask about the PDF or tell me what to change…"}
              rows={1}
              className="flex-1 resize-none rounded-xl border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-purple-500 dark:text-white max-h-24"
              style={{ height: "auto", minHeight: "40px" }}
              onInput={(e) => {
                const el = e.target as HTMLTextAreaElement;
                el.style.height = "auto";
                el.style.height = Math.min(el.scrollHeight, 96) + "px";
              }}
              disabled={loading || needsSetup}
            />
            {loading ? (
              <button onClick={handleStop} aria-label="Stop" className="p-2.5 rounded-xl bg-gray-800 hover:bg-gray-900 dark:bg-gray-200 dark:text-gray-900 text-white shrink-0">
                <Square className="w-4 h-4" />
              </button>
            ) : (
              <button onClick={() => handleSend()} aria-label="Send" disabled={!input.trim() || needsSetup} className="p-2.5 rounded-xl bg-purple-600 hover:bg-purple-700 text-white disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0">
                <Send className="w-4 h-4" />
              </button>
            )}
          </div>
        </div>
      </div>
    </Tooltip.Provider>
  );
}

function IconBtn({ label, onClick, children, active }: { label: string; onClick: () => void; children: React.ReactNode; active?: boolean }) {
  return (
    <Tooltip.Root>
      <Tooltip.Trigger asChild>
        <button
          onClick={onClick}
          aria-label={label}
          className={`p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors ${active ? "text-purple-500" : "text-gray-500 hover:text-purple-500"}`}
        >
          {children}
        </button>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content className="rounded-md bg-gray-900 dark:bg-gray-100 px-2.5 py-1.5 text-xs text-white dark:text-gray-900 shadow-md" sideOffset={5}>
          {label}
          <Tooltip.Arrow className="fill-gray-900 dark:fill-gray-100" />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}
