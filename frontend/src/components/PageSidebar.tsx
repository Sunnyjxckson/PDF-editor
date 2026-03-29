"use client";

import { useEditorStore } from "@/lib/store";
import { getThumbnailUrl } from "@/lib/api";
import { X } from "lucide-react";

export default function PageSidebar() {
  const {
    docId,
    totalPages,
    currentPage,
    setCurrentPage,
    sidebarOpen,
    setSidebarOpen,
    pageVersion,
  } = useEditorStore();

  if (!docId || !sidebarOpen) return null;

  return (
    <>
      {/* Mobile overlay backdrop */}
      <div
        className="fixed inset-0 bg-black/40 z-20 sm:hidden"
        onClick={() => setSidebarOpen(false)}
      />

      {/* Sidebar */}
      <div
        className={`
          fixed sm:relative z-30 sm:z-auto
          left-0 top-0 sm:top-auto h-full sm:h-auto
          w-[200px] sm:w-[160px] lg:w-[200px]
          bg-white dark:bg-gray-900
          border-r border-gray-200 dark:border-gray-800
          overflow-y-auto shrink-0
          pt-12 sm:pt-0
        `}
      >
        {/* Mobile close button */}
        <button
          onClick={() => setSidebarOpen(false)}
          className="sm:hidden absolute top-2 right-2 p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800"
        >
          <X className="w-5 h-5" />
        </button>

        <div className="p-2 space-y-2">
          {Array.from({ length: totalPages }, (_, i) => (
            <button
              key={`${i}-${pageVersion}`}
              onClick={() => {
                setCurrentPage(i);
                // Close sidebar on mobile after selection
                if (window.innerWidth < 640) setSidebarOpen(false);
              }}
              className={`
                w-full rounded-lg overflow-hidden border-2 transition-colors
                ${
                  currentPage === i
                    ? "border-blue-500"
                    : "border-transparent hover:border-gray-300 dark:hover:border-gray-600"
                }
              `}
            >
              <div className="relative">
                <img
                  src={`${getThumbnailUrl(docId, i)}?v=${pageVersion}`}
                  alt={`Page ${i + 1}`}
                  className="w-full h-auto"
                  loading="lazy"
                />
                <span
                  className={`
                    absolute bottom-1 left-1/2 -translate-x-1/2
                    text-[10px] px-1.5 py-0.5 rounded
                    ${
                      currentPage === i
                        ? "bg-blue-500 text-white"
                        : "bg-black/50 text-white"
                    }
                  `}
                >
                  {i + 1}
                </span>
              </div>
            </button>
          ))}
        </div>
      </div>
    </>
  );
}
