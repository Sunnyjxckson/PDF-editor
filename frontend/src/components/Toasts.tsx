"use client";

import { CheckCircle2, XCircle, Info, X } from "lucide-react";
import { useEditorStore } from "@/lib/store";

const icons = {
  success: CheckCircle2,
  error: XCircle,
  info: Info,
};

const colors = {
  success: "bg-green-50 dark:bg-green-950/50 border-green-200 dark:border-green-800 text-green-800 dark:text-green-200",
  error: "bg-red-50 dark:bg-red-950/50 border-red-200 dark:border-red-800 text-red-800 dark:text-red-200",
  info: "bg-blue-50 dark:bg-blue-950/50 border-blue-200 dark:border-blue-800 text-blue-800 dark:text-blue-200",
};

export default function Toasts() {
  const toasts = useEditorStore((s) => s.toasts);
  const removeToast = useEditorStore((s) => s.removeToast);

  if (toasts.length === 0) return null;

  return (
    <div className="fixed top-16 right-4 z-[100] flex flex-col gap-2 max-w-sm">
      {toasts.map((toast) => {
        const Icon = icons[toast.type];
        return (
          <div
            key={toast.id}
            className={`animate-toast-in flex items-start gap-2.5 px-4 py-3 rounded-xl border shadow-lg backdrop-blur-sm ${colors[toast.type]}`}
          >
            <Icon className="w-4 h-4 mt-0.5 shrink-0" />
            <span className="text-sm flex-1">{toast.message}</span>
            <button
              onClick={() => removeToast(toast.id)}
              className="p-0.5 rounded hover:bg-black/10 dark:hover:bg-white/10 shrink-0"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
