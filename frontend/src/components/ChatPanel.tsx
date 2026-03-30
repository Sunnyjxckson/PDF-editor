"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { X, Send, Sparkles, Pin, PinOff, Trash2, CheckCircle, ScanSearch } from "lucide-react";
import * as Tooltip from "@radix-ui/react-tooltip";
import { useEditorStore } from "@/lib/store";
import { streamChatMessage, getDocumentInfo, type RegionRect } from "@/lib/api";

interface ChatMsg {
  role: "user" | "assistant";
  content: string;
  changed?: boolean;
  streaming?: boolean;
}

const SUGGESTIONS = [
  "Summarize this document",
  "Pull all email addresses",
  'Replace "old text" with "new text"',
  "How many pages?",
  "Rotate this page",
  "Delete page 3",
  "Word count",
  "Redact all phone numbers",
];

const QUICK_ACTIONS = [
  { label: "Summarize", message: "Summarize this page" },
  { label: "Extract Data", message: "Extract all emails, phones, and dates from this page" },
  { label: "Fix Grammar", message: "Fix grammar and formatting on this page" },
  { label: "Word Count", message: "Word count for this page" },
];

const WELCOME_MESSAGE: ChatMsg = {
  role: "assistant",
  content:
    "Hi! I'm your PDF editing assistant. Tell me what you'd like to change — for example:\n\n- \"Replace **John Smith** with **Sunny Jackson** everywhere\"\n- \"Delete pages 3 through 5\"\n- \"Pull all email addresses\"\n- \"How many times does **revenue** appear?\"\n\nJust type naturally and I'll handle it!",
};

export default function ChatPanel() {
  const {
    docId,
    chatOpen,
    setChatOpen,
    currentPage,
    totalPages,
    bumpVersion,
    setDocument,
    setCurrentPage,
    chatPinned,
    setChatPinned,
    addToast,
    regionSelection,
    setRegionSelection,
  } = useEditorStore();

  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [panelWidth, setPanelWidth] = useState(400);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const resizingRef = useRef(false);
  const streamAbortRef = useRef<AbortController | null>(null);

  // Auto scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Focus input when panel opens
  useEffect(() => {
    if (chatOpen) {
      setTimeout(() => inputRef.current?.focus(), 100);
    }
  }, [chatOpen]);

  // Welcome message
  useEffect(() => {
    if (chatOpen && messages.length === 0) {
      setMessages([WELCOME_MESSAGE]);
    }
  }, [chatOpen, messages.length]);

  // Cleanup stream on unmount
  useEffect(() => {
    return () => {
      if (streamAbortRef.current) streamAbortRef.current.abort();
    };
  }, []);

  // Resize handling
  const handleResizeStart = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      resizingRef.current = true;
      const startX = e.clientX;
      const startWidth = panelWidth;

      const onMouseMove = (ev: MouseEvent) => {
        if (!resizingRef.current) return;
        const delta = startX - ev.clientX;
        const newWidth = Math.min(600, Math.max(320, startWidth + delta));
        setPanelWidth(newWidth);
      };

      const onMouseUp = () => {
        resizingRef.current = false;
        document.removeEventListener("mousemove", onMouseMove);
        document.removeEventListener("mouseup", onMouseUp);
      };

      document.addEventListener("mousemove", onMouseMove);
      document.addEventListener("mouseup", onMouseUp);
    },
    [panelWidth],
  );

  const handleSend = useCallback(
    async (overrideMsg?: string) => {
      const msgText = overrideMsg ?? input.trim();
      if (!msgText || !docId || loading) return;

      if (!overrideMsg) setInput("");
      setMessages((prev) => [...prev, { role: "user", content: msgText }]);
      setLoading(true);

      // Add a placeholder streaming message
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "", streaming: true },
      ]);

      // Abort any existing stream
      if (streamAbortRef.current) streamAbortRef.current.abort();

      // Capture region before sending (will be cleared after operation)
      const activeRegion = regionSelection
        ? { page: regionSelection.page, rect: regionSelection.rect }
        : undefined;

      const controller = streamChatMessage(docId, msgText, currentPage, {
        onToken: (token) => {
          setMessages((prev) => {
            const updated = [...prev];
            const lastAssistant = updated.findLastIndex((m) => m.streaming);
            if (lastAssistant >= 0) {
              updated[lastAssistant] = {
                ...updated[lastAssistant],
                content: updated[lastAssistant].content + token,
              };
            }
            return updated;
          });
        },
        onDone: async (data) => {
          // Finalize the streaming message
          setMessages((prev) => {
            const updated = [...prev];
            const lastAssistant = updated.findLastIndex((m) => m.streaming);
            if (lastAssistant >= 0) {
              updated[lastAssistant] = {
                role: "assistant",
                content: data.response || updated[lastAssistant].content,
                changed: data.changed,
                streaming: false,
              };
            }
            return updated;
          });

          if (data.changed) {
            bumpVersion();
            addToast("Changes applied to document", "success");
          }

          if (data.new_page_count !== null) {
            try {
              const updated = await getDocumentInfo(docId);
              setDocument(updated, docId);
              if (currentPage >= updated.page_count) {
                setCurrentPage(Math.max(0, updated.page_count - 1));
              }
            } catch {
              // ignore
            }
          }

          // Clear region selection after operation completes
          if (activeRegion) setRegionSelection(null);

          setLoading(false);
          streamAbortRef.current = null;
        },
        onError: () => {
          setMessages((prev) => {
            const updated = [...prev];
            const lastAssistant = updated.findLastIndex((m) => m.streaming);
            if (lastAssistant >= 0) {
              updated[lastAssistant] = {
                role: "assistant",
                content: "Sorry, something went wrong processing that command. Please try again.",
                streaming: false,
              };
            }
            return updated;
          });
          setLoading(false);
          streamAbortRef.current = null;
        },
      }, activeRegion);

      streamAbortRef.current = controller;
    },
    [input, docId, loading, currentPage, bumpVersion, setDocument, setCurrentPage, addToast, regionSelection, setRegionSelection],
  );

  const handleSuggestion = (s: string) => {
    setInput(s);
    inputRef.current?.focus();
  };

  const handleQuickAction = (message: string) => {
    handleSend(message);
  };

  const handleClearChat = () => {
    if (streamAbortRef.current) {
      streamAbortRef.current.abort();
      streamAbortRef.current = null;
    }
    setMessages([WELCOME_MESSAGE]);
    setInput("");
    setLoading(false);
  };

  if (!docId || !chatOpen) return null;

  const pinned = chatPinned;

  return (
    <Tooltip.Provider delayDuration={300}>
      <div
        className={`${
          pinned
            ? "relative shrink-0"
            : "fixed inset-0 sm:inset-auto sm:right-0 sm:top-0 sm:bottom-0 z-50"
        } flex flex-col bg-white dark:bg-gray-900 border-l border-gray-200 dark:border-gray-800 shadow-2xl animate-slide-in-right`}
        style={{ width: pinned ? panelWidth : undefined, maxWidth: pinned ? undefined : panelWidth }}
      >
        {/* Resize handle */}
        <div
          onMouseDown={handleResizeStart}
          className="resize-handle absolute left-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-purple-400/40 transition-colors z-10"
        />

        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-gray-800 shrink-0">
          <div className="flex items-center gap-2">
            <Sparkles className="w-5 h-5 text-purple-500" />
            <h2 className="font-semibold text-sm">PDF Chat Assistant</h2>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-gray-400 mr-1">
              Page {currentPage + 1}/{totalPages}
            </span>

            {/* Clear chat button */}
            <Tooltip.Root>
              <Tooltip.Trigger asChild>
                <button
                  onClick={handleClearChat}
                  className="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-500 hover:text-red-500 transition-colors"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </Tooltip.Trigger>
              <Tooltip.Portal>
                <Tooltip.Content
                  className="rounded-md bg-gray-900 dark:bg-gray-100 px-2.5 py-1.5 text-xs text-white dark:text-gray-900 shadow-md"
                  sideOffset={5}
                >
                  Clear chat
                  <Tooltip.Arrow className="fill-gray-900 dark:fill-gray-100" />
                </Tooltip.Content>
              </Tooltip.Portal>
            </Tooltip.Root>

            {/* Pin button */}
            <Tooltip.Root>
              <Tooltip.Trigger asChild>
                <button
                  onClick={() => setChatPinned(!pinned)}
                  className={`p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors ${
                    pinned
                      ? "text-purple-500"
                      : "text-gray-500 hover:text-purple-500"
                  }`}
                >
                  {pinned ? (
                    <PinOff className="w-3.5 h-3.5" />
                  ) : (
                    <Pin className="w-3.5 h-3.5" />
                  )}
                </button>
              </Tooltip.Trigger>
              <Tooltip.Portal>
                <Tooltip.Content
                  className="rounded-md bg-gray-900 dark:bg-gray-100 px-2.5 py-1.5 text-xs text-white dark:text-gray-900 shadow-md"
                  sideOffset={5}
                >
                  {pinned ? "Unpin panel" : "Pin panel"}
                  <Tooltip.Arrow className="fill-gray-900 dark:fill-gray-100" />
                </Tooltip.Content>
              </Tooltip.Portal>
            </Tooltip.Root>

            {/* Close button */}
            <button
              onClick={() => setChatOpen(false)}
              className="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {messages.map((msg, i) => (
            <div key={i}>
              <div
                className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
              >
                <div
                  className={`max-w-[85%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${
                    msg.role === "user"
                      ? "bg-purple-600 text-white rounded-br-md"
                      : "bg-gray-100 dark:bg-gray-800 text-gray-800 dark:text-gray-200 rounded-bl-md"
                  }`}
                >
                  {msg.role === "assistant" ? (
                    <div className="chat-content">
                      {msg.content ? (
                        <span
                          dangerouslySetInnerHTML={{
                            __html: msg.content
                              .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
                              .replace(/\n/g, "<br/>"),
                          }}
                        />
                      ) : msg.streaming ? (
                        <span className="inline-block w-2 h-4 bg-gray-400 animate-pulse" />
                      ) : null}
                      {/* Streaming cursor */}
                      {msg.streaming && msg.content && (
                        <span className="inline-block w-1.5 h-4 bg-purple-500 animate-pulse ml-0.5 align-text-bottom" />
                      )}
                    </div>
                  ) : (
                    msg.content
                  )}
                </div>
              </div>
              {/* Changes applied badge */}
              {msg.role === "assistant" && msg.changed && !msg.streaming && (
                <div className="flex justify-start mt-1 ml-1">
                  <span className="inline-flex items-center gap-1 text-xs text-green-600 dark:text-green-400 bg-green-50 dark:bg-green-900/30 px-2 py-0.5 rounded-full">
                    <CheckCircle className="w-3 h-3" />
                    Changes applied
                  </span>
                </div>
              )}
            </div>
          ))}

          {/* Only show bounce dots if loading AND no streaming message yet */}
          {loading && !messages.some((m) => m.streaming) && (
            <div className="flex justify-start">
              <div className="bg-gray-100 dark:bg-gray-800 rounded-2xl rounded-bl-md px-4 py-3">
                <div className="flex gap-1.5">
                  <div
                    className="w-2 h-2 bg-gray-400 rounded-full animate-bounce"
                    style={{ animationDelay: "0ms" }}
                  />
                  <div
                    className="w-2 h-2 bg-gray-400 rounded-full animate-bounce"
                    style={{ animationDelay: "150ms" }}
                  />
                  <div
                    className="w-2 h-2 bg-gray-400 rounded-full animate-bounce"
                    style={{ animationDelay: "300ms" }}
                  />
                </div>
              </div>
            </div>
          )}

          <div ref={messagesEndRef} />

          {/* Suggestions (shown when few messages) */}
          {messages.length <= 1 && !loading && (
            <div className="pt-2">
              <p className="text-xs text-gray-400 mb-2">Try one of these:</p>
              <div className="flex flex-wrap gap-1.5">
                {SUGGESTIONS.map((s, i) => (
                  <button
                    key={i}
                    onClick={() => handleSuggestion(s)}
                    className="text-xs px-2.5 py-1.5 rounded-full bg-gray-100 dark:bg-gray-800 hover:bg-purple-100 dark:hover:bg-purple-900/30 text-gray-600 dark:text-gray-300 transition-colors"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Quick action pills */}
        <div className="px-3 pt-2 pb-1 shrink-0 border-t border-gray-100 dark:border-gray-800/50">
          <div className="flex flex-wrap gap-1.5">
            {QUICK_ACTIONS.map((action) => (
              <button
                key={action.label}
                onClick={() => handleQuickAction(action.message)}
                disabled={loading}
                className="text-xs px-2.5 py-1 rounded-full border border-purple-200 dark:border-purple-800 text-purple-600 dark:text-purple-400 hover:bg-purple-50 dark:hover:bg-purple-900/30 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {action.label}
              </button>
            ))}
          </div>
        </div>

        {/* Region context banner */}
        {regionSelection && (
          <div className="px-3 py-2 shrink-0 border-t border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-950/50 flex items-center gap-2">
            <ScanSearch className="w-4 h-4 text-blue-500 shrink-0" />
            <span className="text-xs text-blue-700 dark:text-blue-300 flex-1">
              Selected region on page {regionSelection.page + 1} (x:{Math.round(regionSelection.rect.x)}, y:{Math.round(regionSelection.rect.y)}, w:{Math.round(regionSelection.rect.width)}, h:{Math.round(regionSelection.rect.height)})
            </span>
            <button
              onClick={() => setRegionSelection(null)}
              className="p-0.5 rounded hover:bg-blue-200 dark:hover:bg-blue-800 text-blue-500"
            >
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
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  handleSend();
                }
                if (e.key === "Escape") {
                  setChatOpen(false);
                }
              }}
              placeholder="Tell me what to change..."
              rows={1}
              className="flex-1 resize-none rounded-xl border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-purple-500 dark:text-white max-h-24"
              style={{
                height: "auto",
                minHeight: "40px",
              }}
              onInput={(e) => {
                const el = e.target as HTMLTextAreaElement;
                el.style.height = "auto";
                el.style.height = Math.min(el.scrollHeight, 96) + "px";
              }}
              disabled={loading}
            />
            <button
              onClick={() => handleSend()}
              disabled={!input.trim() || loading}
              className="p-2.5 rounded-xl bg-purple-600 hover:bg-purple-700 text-white disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
            >
              <Send className="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>
    </Tooltip.Provider>
  );
}
