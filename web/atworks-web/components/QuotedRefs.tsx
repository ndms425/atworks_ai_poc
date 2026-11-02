// The quotes the next question will carry: ONE chip however many there are ("인용 3개", a single
// height), the full text in a hover/focus popover, each removable. Follows open-design's
// QuotedRefs (Apache-2.0, apps/web/src/components/chat/QuotedRefs.tsx).

"use client";

import { useState } from "react";
import { Icon } from "web-shared";

/** How a question carries its quotes: quoted lines first, then the question itself. */
export function withQuotes(quotes: string[], question: string): string {
  if (!quotes.length) return question;
  const quoted = quotes.map((q) => q.split("\n").map((line) => `> ${line}`).join("\n")).join("\n>\n");
  return `${quoted}\n\n${question}`;
}

export default function QuotedRefs({ quotes, onRemove, onClear }: { quotes: string[]; onRemove: (index: number) => void; onClear: () => void }) {
  const [open, setOpen] = useState(false);
  if (!quotes.length) return null;
  return (
    <div className="relative" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          className="flex items-center gap-1.5 rounded-full border border-(--line-strong) bg-(--card) px-2.5 py-1 text-[12px] font-medium text-(--ink)"
        >
          <span aria-hidden className="text-[14px] leading-none text-(--accent)">❝</span>
          인용 {quotes.length}개
        </button>
        <button type="button" onClick={onClear} className="text-[11.5px] text-(--ink-faint) hover:text-(--ink)">
          모두 빼기
        </button>
      </div>
      {open ? (
        <div className="absolute bottom-full left-0 z-40 mb-1.5 w-[min(340px,calc(100vw-2rem))] rounded-[12px] border border-(--line) bg-(--card) p-2 shadow-(--shadow-lg)">
          <ol className="panel-scroll flex max-h-60 flex-col gap-1.5 overflow-y-auto">
            {quotes.map((quote, index) => (
              <li key={index} className="flex items-start gap-2 rounded-lg bg-(--ground) px-2 py-1.5">
                <span className="mt-px text-[11px] tabular-nums text-(--ink-faint)">{index + 1}</span>
                <span className="line-clamp-4 min-w-0 flex-1 whitespace-pre-wrap text-[12px] leading-snug text-(--ink-2)">{quote}</span>
                <button type="button" aria-label="이 인용 빼기" onClick={() => onRemove(index)} className="text-(--ink-faint) hover:text-(--danger)">
                  <Icon name="x" size={12} />
                </button>
              </li>
            ))}
          </ol>
        </div>
      ) : null}
    </div>
  );
}
