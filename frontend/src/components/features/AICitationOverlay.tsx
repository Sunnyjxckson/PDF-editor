"use client";

/**
 * Highlights the passage an AI citation points at. Mount it inside the page
 * wrapper (absolute inset-0 over the rendered page), like RedactOverlay:
 *
 *   <AICitationOverlay currentPage={currentPage} pageWidth={w} pageHeight={h} />
 *
 * currentPage is 0-based; the store's highlight page is 1-based. Rects are
 * visible-space PDF points, positioned in % so any zoom works. The highlight
 * fades after a few seconds.
 */
import { useEffect } from "react";
import { rectToPercentStyle, useAIStore } from "@/lib/features/ai";

export interface AICitationOverlayProps {
  currentPage: number;
  pageWidth: number;
  pageHeight: number;
  durationMs?: number;
}

export default function AICitationOverlay({ currentPage, pageWidth, pageHeight, durationMs = 6000 }: AICitationOverlayProps) {
  const highlight = useAIStore((s) => s.highlight);
  const setHighlight = useAIStore((s) => s.setHighlight);

  useEffect(() => {
    if (!highlight || !durationMs) return;
    const t = setTimeout(() => setHighlight(null), durationMs);
    return () => clearTimeout(t);
  }, [highlight, durationMs, setHighlight]);

  if (!highlight || highlight.page !== currentPage + 1 || !highlight.rects.length || !pageWidth || !pageHeight) {
    return null;
  }
  return (
    <div className="absolute inset-0 pointer-events-none z-30" data-testid="ai-citation-overlay">
      {highlight.rects.map((r, i) => (
        <div
          key={i}
          className="absolute rounded-sm bg-purple-400/35 ring-2 ring-purple-500/70 animate-pulse"
          style={rectToPercentStyle(r, pageWidth, pageHeight)}
        />
      ))}
    </div>
  );
}
