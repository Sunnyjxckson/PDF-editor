"use client";

import { useState } from "react";
import { X, Search, Replace } from "lucide-react";
import { useEditorStore } from "@/lib/store";
import { findText, replaceText } from "@/lib/api";

export default function FindReplace() {
  const { docId, findReplaceOpen, setFindReplaceOpen, currentPage, bumpVersion, setCurrentPage } = useEditorStore();
  const [find, setFind] = useState("");
  const [replace, setReplace] = useState("");
  const [results, setResults] = useState<{ page: number; bbox: number[] }[]>([]);
  const [count, setCount] = useState(0);
  const [searched, setSearched] = useState(false);
  const [replacing, setReplacing] = useState(false);
  const [allPages, setAllPages] = useState(true);

  if (!findReplaceOpen || !docId) return null;

  const handleFind = async () => {
    if (!find.trim()) return;
    const result = await findText(docId, find, allPages ? undefined : currentPage);
    setResults(result.matches);
    setCount(result.count);
    setSearched(true);
    // Navigate to first result
    if (result.matches.length > 0) {
      setCurrentPage(result.matches[0].page);
    }
  };

  const handleReplace = async () => {
    if (!find.trim()) return;
    setReplacing(true);
    const result = await replaceText(docId, find, replace, allPages ? undefined : currentPage);
    setReplacing(false);
    setResults([]);
    setCount(0);
    setSearched(false);
    bumpVersion();
  };

  return (
    <div className="absolute top-14 right-2 sm:right-4 z-50 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-xl shadow-2xl p-3 w-[calc(100%-1rem)] sm:w-80">
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-semibold">Find & Replace</h3>
        <button onClick={() => setFindReplaceOpen(false)} className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800">
          <X className="w-4 h-4" />
        </button>
      </div>

      <div className="space-y-2">
        <div className="flex gap-1">
          <input
            autoFocus
            value={find}
            onChange={(e) => setFind(e.target.value)}
            placeholder="Find text..."
            className="flex-1 px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 dark:text-white"
            onKeyDown={(e) => e.key === "Enter" && handleFind()}
          />
          <button onClick={handleFind} className="px-2 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700">
            <Search className="w-4 h-4" />
          </button>
        </div>

        <div className="flex gap-1">
          <input
            value={replace}
            onChange={(e) => setReplace(e.target.value)}
            placeholder="Replace with..."
            className="flex-1 px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 dark:text-white"
            onKeyDown={(e) => e.key === "Enter" && handleReplace()}
          />
          <button
            onClick={handleReplace}
            disabled={replacing || !find.trim()}
            className="px-2 py-1.5 bg-orange-600 text-white rounded text-sm hover:bg-orange-700 disabled:opacity-50"
          >
            {replacing ? "..." : <Replace className="w-4 h-4" />}
          </button>
        </div>

        <label className="flex items-center gap-2 text-xs text-gray-500">
          <input
            type="checkbox"
            checked={allPages}
            onChange={(e) => setAllPages(e.target.checked)}
            className="rounded"
          />
          Search all pages
        </label>

        {searched && (
          <p className="text-xs text-gray-500">
            {count === 0 ? "No matches found" : `${count} match${count > 1 ? "es" : ""} found`}
          </p>
        )}

        {results.length > 0 && (
          <div className="max-h-32 overflow-y-auto space-y-1">
            {results.map((r, i) => (
              <button
                key={i}
                onClick={() => setCurrentPage(r.page)}
                className={`w-full text-left text-xs px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800 ${
                  r.page === currentPage ? "bg-blue-50 dark:bg-blue-900/20" : ""
                }`}
              >
                Page {r.page + 1}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
