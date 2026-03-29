"use client";

import { useCallback, useState } from "react";
import { Upload, FileText } from "lucide-react";
import { uploadPDF, getDocumentInfo } from "@/lib/api";
import { useEditorStore } from "@/lib/store";

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
    <div className="flex items-center justify-center min-h-screen bg-gray-50 dark:bg-gray-950 p-4">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        className={`
          w-full max-w-lg p-8 sm:p-12 rounded-2xl border-2 border-dashed
          transition-all duration-200 text-center
          ${dragging
            ? "border-blue-500 bg-blue-50 dark:bg-blue-950/30"
            : "border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900"
          }
        `}
      >
        <div className="flex justify-center mb-6">
          <div className="w-16 h-16 sm:w-20 sm:h-20 rounded-full bg-blue-100 dark:bg-blue-900/40 flex items-center justify-center">
            {uploading ? (
              <div className="w-8 h-8 border-3 border-blue-500 border-t-transparent rounded-full animate-spin" />
            ) : (
              <Upload className="w-8 h-8 sm:w-10 sm:h-10 text-blue-600 dark:text-blue-400" />
            )}
          </div>
        </div>

        <h1 className="text-xl sm:text-2xl font-bold text-gray-900 dark:text-white mb-2">
          AI PDF Editor
        </h1>
        <p className="text-sm sm:text-base text-gray-500 dark:text-gray-400 mb-6">
          {uploading
            ? "Uploading and analyzing your PDF..."
            : "Drop a PDF file here or click to browse"}
        </p>

        <label className="inline-flex items-center gap-2 px-6 py-3 bg-blue-600 hover:bg-blue-700 text-white rounded-lg cursor-pointer transition-colors text-sm sm:text-base">
          <FileText className="w-4 h-4" />
          <span>Choose PDF</span>
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

        {error && (
          <p className="mt-4 text-sm text-red-600 dark:text-red-400">{error}</p>
        )}
      </div>
    </div>
  );
}
