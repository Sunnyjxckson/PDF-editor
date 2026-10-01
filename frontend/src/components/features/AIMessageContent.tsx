"use client";

/**
 * Renders assistant markdown (a safe subset: headings, bullets, numbered
 * lists, **bold**, *italic*, `code`) as React nodes — never innerHTML, since
 * the text comes from a model that read an untrusted PDF — and turns
 * [p. N] / [p. N "quote"] citations into clickable page chips.
 */
import { Fragment, type ReactNode } from "react";
import { splitCitations } from "@/lib/features/ai";

export interface AIMessageContentProps {
  text: string;
  onCitation?: (page: number, quote: string | null, marker: string) => void;
}

function inline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)/g;
  let last = 0;
  let i = 0;
  for (const m of text.matchAll(re)) {
    const idx = m.index ?? 0;
    if (idx > last) out.push(text.slice(last, idx));
    const tok = m[0];
    const k = `${keyBase}-${i++}`;
    if (tok.startsWith("**")) out.push(<strong key={k}>{tok.slice(2, -2)}</strong>);
    else if (tok.startsWith("`")) out.push(<code key={k} className="px-1 rounded bg-black/5 dark:bg-white/10 text-[0.85em]">{tok.slice(1, -1)}</code>);
    else out.push(<em key={k}>{tok.slice(1, -1)}</em>);
    last = idx + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function lineWithCitations(line: string, key: string, onCitation?: AIMessageContentProps["onCitation"]): ReactNode[] {
  return splitCitations(line).map((seg, i) =>
    seg.kind === "text" ? (
      <Fragment key={`${key}-t${i}`}>{inline(seg.text, `${key}-t${i}`)}</Fragment>
    ) : (
      <button
        key={`${key}-c${i}`}
        type="button"
        data-testid="ai-citation"
        title={seg.quote ? `Page ${seg.page}: “${seg.quote}”` : `Go to page ${seg.page}`}
        onClick={() => onCitation?.(seg.page, seg.quote, seg.marker)}
        className="mx-0.5 inline-flex items-center rounded-md bg-purple-100 dark:bg-purple-900/40 px-1.5 py-0 text-[11px] font-medium text-purple-700 dark:text-purple-300 hover:bg-purple-200 dark:hover:bg-purple-800/60 align-baseline"
      >
        p. {seg.page}
      </button>
    ),
  );
}

export default function AIMessageContent({ text, onCitation }: AIMessageContentProps) {
  const lines = text.split("\n");
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: ReactNode[] } | null = null;
  const flush = () => {
    if (!list) return;
    const k = `l${blocks.length}`;
    blocks.push(list.ordered
      ? <ol key={k} className="list-decimal pl-5 space-y-0.5">{list.items}</ol>
      : <ul key={k} className="list-disc pl-5 space-y-0.5">{list.items}</ul>);
    list = null;
  };
  lines.forEach((raw, i) => {
    const key = `b${i}`;
    const bullet = /^\s*[-*•]\s+(.*)$/.exec(raw);
    const num = /^\s*\d+[.)]\s+(.*)$/.exec(raw);
    const head = /^(#{1,4})\s+(.*)$/.exec(raw);
    if (bullet || num) {
      const ordered = !!num && !bullet;
      if (!list || list.ordered !== ordered) {
        flush();
        list = { ordered, items: [] };
      }
      list.items.push(<li key={key}>{lineWithCitations((bullet ?? num)![1], key, onCitation)}</li>);
      return;
    }
    flush();
    if (head) {
      blocks.push(<p key={key} className="font-semibold mt-1">{lineWithCitations(head[2], key, onCitation)}</p>);
    } else if (raw.trim() === "") {
      blocks.push(<div key={key} className="h-2" />);
    } else {
      blocks.push(<p key={key}>{lineWithCitations(raw, key, onCitation)}</p>);
    }
  });
  flush();
  return <div className="space-y-0.5 break-words">{blocks}</div>;
}
