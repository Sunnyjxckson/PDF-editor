"use client";

import { useCallback, useState } from "react";
import {
  Upload,
  FileText,
  Sparkles,
  Search,
  PenTool,
  Share2,
} from "lucide-react";
import { uploadPDF, getDocumentInfo } from "@/lib/api";
import { useEditorStore } from "@/lib/store";

const features = [
  {
    icon: Sparkles,
    title: "AI-Powered Editing",
    description: "Intelligent text editing with AI assistance",
  },
  {
    icon: Search,
    title: "Smart Search",
    description: "Find and replace across all pages instantly",
  },
  {
    icon: PenTool,
    title: "Drawing Tools",
    description: "Annotate, highlight, and draw freely",
  },
  {
    icon: Share2,
    title: "Export Anywhere",
    description: "Download your edited PDF in seconds",
  },
];

export default function UploadScreen() {
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const setDocument = useEditorStore((s) => s.setDocument);

  const handleFile = useCallback(
    async (file: File) => {
      if (!file.name.toLowerCase().endsWith(".pdf")) {
        setError("Please select a PDF file");
        return;
      }
      setError(null);
      setUploading(true);
      try {
        const result = await uploadPDF(file);
        const info = await getDocumentInfo(result.id);
        setDocument(info, result.id);
      } catch {
        setError("Failed to upload PDF. Make sure the backend is running.");
      } finally {
        setUploading(false);
      }
    },
    [setDocument]
  );

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragging(false);
      const file = e.dataTransfer.files[0];
      if (file) handleFile(file);
    },
    [handleFile]
  );

  return (
    <div className="flex flex-col items-center justify-center min-h-screen bg-gradient-to-br from-gray-50 via-white to-blue-50 dark:from-gray-950 dark:via-gray-900 dark:to-blue-950/30 p-4 sm:p-8 overflow-y-auto">
      {/* Background decorative elements */}
      <div className="pointer-events-none fixed inset-0 overflow-hidden">
        <div className="absolute -top-40 -right-40 h-80 w-80 rounded-full bg-blue-400/10 blur-3xl dark:bg-blue-500/5" />
        <div className="absolute -bottom-40 -left-40 h-80 w-80 rounded-full bg-purple-400/10 blur-3xl dark:bg-purple-500/5" />
        <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 h-[600px] w-[600px] rounded-full bg-indigo-300/5 blur-3xl dark:bg-indigo-400/5" />
      </div>

      <div className="relative z-10 w-full max-w-3xl flex flex-col items-center">
        {/* Hero section */}
        <div className="text-center mb-10 sm:mb-12">
          <h1 className="text-4xl sm:text-5xl md:text-6xl font-extrabold tracking-tight mb-4">
            <span className="bg-gradient-to-r from-blue-600 via-indigo-600 to-purple-600 dark:from-blue-400 dark:via-indigo-400 dark:to-purple-400 bg-clip-text text-transparent">
              AI PDF Editor
            </span>
          </h1>
          <p className="text-lg sm:text-xl text-gray-500 dark:text-gray-400 max-w-md mx-auto leading-relaxed">
            Edit, annotate, and transform your PDFs with the power of AI
          </p>
        </div>

        {/* Upload area */}
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
          className={`
            group relative w-full max-w-xl p-8 sm:p-12 rounded-2xl border-2 border-dashed
            transition-all duration-300 ease-out text-center
            backdrop-blur-sm
            ${
              dragging
                ? "border-blue-500 bg-blue-50/80 dark:bg-blue-950/40 scale-[1.02] shadow-2xl shadow-blue-500/10 dark:shadow-blue-500/5 animate-pulse"
                : "border-gray-300/70 dark:border-gray-700/70 bg-white/70 dark:bg-gray-900/70 hover:border-blue-400/60 dark:hover:border-blue-500/40 hover:shadow-xl hover:shadow-blue-500/5 dark:hover:shadow-blue-500/5"
            }
          `}
        >
          {/* Subtle gradient border glow on drag */}
          {dragging && (
            <div className="absolute inset-0 -z-10 rounded-2xl bg-gradient-to-r from-blue-500/20 via-indigo-500/20 to-purple-500/20 blur-xl" />
          )}

          <div className="flex justify-center mb-6">
            <div
              className={`
                w-20 h-20 sm:w-24 sm:h-24 rounded-2xl flex items-center justify-center
                transition-all duration-300
                ${
                  dragging
                    ? "bg-blue-100 dark:bg-blue-900/50 scale-110"
                    : "bg-gradient-to-br from-blue-50 to-indigo-100 dark:from-blue-900/40 dark:to-indigo-900/30"
                }
              `}
            >
              {uploading ? (
                <div className="w-10 h-10 border-3 border-blue-500 border-t-transparent rounded-full animate-spin" />
              ) : (
                <Upload
                  className={`
                    w-10 h-10 sm:w-12 sm:h-12 text-blue-600 dark:text-blue-400
                    animate-float transition-transform duration-300
                    ${dragging ? "scale-110" : ""}
                  `}
                />
              )}
            </div>
          </div>

          <h2 className="text-xl sm:text-2xl font-bold text-gray-900 dark:text-white mb-2">
            {uploading ? "Processing your PDF..." : "Drop your PDF here"}
          </h2>
          <p className="text-sm sm:text-base text-gray-500 dark:text-gray-400 mb-6">
            {uploading
              ? "Uploading and analyzing your document"
              : "or click the button below to browse your files"}
          </p>

          <label
            className={`
              inline-flex items-center gap-2.5 px-7 py-3.5
              bg-gradient-to-r from-blue-600 to-indigo-600
              hover:from-blue-700 hover:to-indigo-700
              active:from-blue-800 active:to-indigo-800
              text-white rounded-xl cursor-pointer
              transition-all duration-200 text-sm sm:text-base font-medium
              shadow-lg shadow-blue-500/25 hover:shadow-xl hover:shadow-blue-500/30
              dark:shadow-blue-500/15 dark:hover:shadow-blue-500/20
              hover:-translate-y-0.5 active:translate-y-0
              ${uploading ? "opacity-60 pointer-events-none" : ""}
            `}
          >
            <FileText className="w-4.5 h-4.5" />
            <span>{uploading ? "Uploading..." : "Choose PDF"}</span>
            <input
              type="file"
              accept=".pdf"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) handleFile(file);
              }}
              disabled={uploading}
            />
          </label>

          <p className="mt-4 text-xs text-gray-400 dark:text-gray-500">
            PDF files up to 50MB
          </p>

          {error && (
            <div className="mt-4 flex items-center justify-center gap-2 text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-950/30 rounded-lg px-4 py-2.5">
              <span className="inline-block w-1.5 h-1.5 rounded-full bg-red-500 shrink-0" />
              {error}
            </div>
          )}
        </div>

        {/* Feature cards */}
        <div className="mt-12 sm:mt-16 w-full grid grid-cols-2 sm:grid-cols-4 gap-3 sm:gap-4 max-w-2xl">
          {features.map((feature) => (
            <div
              key={feature.title}
              className="group/card flex flex-col items-center text-center p-4 sm:p-5 rounded-xl
                bg-white/50 dark:bg-gray-900/40 backdrop-blur-sm
                border border-gray-200/50 dark:border-gray-800/50
                hover:border-blue-300/50 dark:hover:border-blue-700/40
                hover:bg-white/80 dark:hover:bg-gray-900/60
                hover:shadow-lg hover:shadow-blue-500/5 dark:hover:shadow-blue-500/5
                transition-all duration-300 hover:-translate-y-1"
            >
              <div
                className="w-10 h-10 sm:w-11 sm:h-11 rounded-lg flex items-center justify-center mb-3
                  bg-gradient-to-br from-blue-50 to-indigo-100
                  dark:from-blue-900/30 dark:to-indigo-900/20
                  group-hover/card:from-blue-100 group-hover/card:to-indigo-200
                  dark:group-hover/card:from-blue-900/50 dark:group-hover/card:to-indigo-900/30
                  transition-all duration-300"
              >
                <feature.icon className="w-5 h-5 text-blue-600 dark:text-blue-400" />
              </div>
              <h3 className="text-xs sm:text-sm font-semibold text-gray-800 dark:text-gray-200 mb-1">
                {feature.title}
              </h3>
              <p className="text-[10px] sm:text-xs text-gray-500 dark:text-gray-400 leading-snug">
                {feature.description}
              </p>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
