"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { X, Send, Sparkles, MessageSquare } from "lucide-react";
import { useEditorStore } from "@/lib/store";
import { sendChatMessage, getDocumentInfo } from "@/lib/api";

interface ChatMsg {
  role: "user" | "assistant";
  content: string;
}

const SUGGESTIONS = [
  'Replace "old text" with "new text"',
  "How many pages?",
  "Summarize this document",
  "Pull all email addresses",
  "Rotate this page",
  "Delete page 3",
  "How many times does revenue appear?",
  "Word count",
  "Redact all phone numbers",
];

export default function ChatPanel() {
  const {
    docId,
    chatOpen,
    setChatOpen,
    currentPage,
    bumpVersion,
    setDocument,
    setCurrentPage,
    totalPages,
  } = useEditorStore();

  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

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
      setMessages([{
        role: "assistant",
        content: "Hi! I'm your PDF editing assistant. Tell me what you'd like to change — for example:\n\n- \"Replace **John Smith** with **Sunny Jackson** everywhere\"\n- \"Delete pages 3 through 5\"\n- \"Pull all email addresses\"\n- \"How many times does **revenue** appear?\"\n\nJust type naturally and I'll handle it!",
      }]);
    }
  }, [chatOpen]);

  const handleSend = useCallback(async () => {
    if (!input.trim() || !docId || loading) return;

    const userMsg = input.trim();
    setInput("");
    setMessages((prev) => [...prev, { role: "user", content: userMsg }]);
    setLoading(true);

    try {
      const res = await sendChatMessage(docId, userMsg, currentPage);

      setMessages((prev) => [...prev, { role: "assistant", content: res.response }]);

      if (res.changed) {
        bumpVersion();
      }

      if (res.new_page_count !== null) {
        const updated = await getDocumentInfo(docId);
        setDocument(updated, docId);
        if (currentPage >= updated.page_count) {
          setCurrentPage(Math.max(0, updated.page_count - 1));
        }
      }
    } catch {
      setMessages((prev) => [...prev, {
        role: "assistant",
        content: "Sorry, something went wrong processing that command. Please try again.",
      }]);
    }

    setLoading(false);
  }, [input, docId, loading, currentPage, bumpVersion, setDocument, setCurrentPage]);

  const handleSuggestion = (s: string) => {
    setInput(s);
    inputRef.current?.focus();
  };

  if (!docId) return null;

  // Toggle button (always visible)
  if (!chatOpen) {
    return (
      <button
        onClick={() => setChatOpen(true)}
        className="fixed bottom-20 sm:bottom-4 right-4 z-50 w-12 h-12 sm:w-14 sm:h-14 rounded-full bg-purple-600 hover:bg-purple-700 text-white shadow-lg flex items-center justify-center transition-transform hover:scale-105"
        title="Open AI Chat (Ctrl+/)"
      >
        <MessageSquare className="w-5 h-5 sm:w-6 sm:h-6" />
      </button>
    );
  }

  return (
    <div className="fixed inset-0 sm:inset-auto sm:right-0 sm:top-0 sm:bottom-0 sm:w-[380px] lg:w-[420px] z-50 flex flex-col bg-white dark:bg-gray-900 border-l border-gray-200 dark:border-gray-800 shadow-2xl">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-gray-800 shrink-0">
        <div className="flex items-center gap-2">
          <Sparkles className="w-5 h-5 text-purple-500" />
          <h2 className="font-semibold text-sm">PDF Chat Assistant</h2>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-400">
            Page {currentPage + 1}/{totalPages}
          </span>
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
          <div
            key={i}
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
                <div
                  className="chat-content"
                  dangerouslySetInnerHTML={{
                    __html: msg.content
                      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
                      .replace(/\n/g, "<br/>"),
                  }}
                />
              ) : (
                msg.content
              )}
            </div>
          </div>
        ))}

        {loading && (
          <div className="flex justify-start">
            <div className="bg-gray-100 dark:bg-gray-800 rounded-2xl rounded-bl-md px-4 py-3">
              <div className="flex gap-1.5">
                <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
                <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
                <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
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
            onClick={handleSend}
            disabled={!input.trim() || loading}
            className="p-2.5 rounded-xl bg-purple-600 hover:bg-purple-700 text-white disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
          >
            <Send className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
