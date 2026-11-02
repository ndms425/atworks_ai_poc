// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { type ReactNode, useEffect, useState } from "react";
import { AssistantText, ErrorBubble, ErrorCard, type ErrorCardActions, UserBubble } from "./MessageBubble";
import { SCREEN_DIRECTIVE_COMPONENTS } from "./protocol";
import type { AssistantChatItem, ChatItem, UISegment } from "./protocol";
import { Suggestions } from "./Suggestions";

export interface TranscriptProps {
  items: ChatItem[];
  busy: boolean;
  send: (text: string) => void;
  renderBlock: (segment: UISegment, item: AssistantChatItem) => ReactNode;
  /** Defaults to `ActivityLine`. */
  renderPending?: (item: AssistantChatItem) => ReactNode;
  suggestionFilter?: (text: string) => boolean;
  /** Components that may extend past the text measure when the page has room. */
  wide?: ReadonlySet<string>;
  gap?: string;
  /** When given, an error renders as a card with these actions instead of a red line. */
  errorActions?: (item: AssistantChatItem, index: number) => ErrorCardActions;
}

/** Seconds since the reply started showing a step; shown from 3s on, so a slow step reads as
 *  "still working", never as stuck. */
function Elapsed() {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const started = Date.now();
    const timer = window.setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, []);
  if (seconds < 3) return null;
  return <span className="shrink-0 tabular-nums text-(--ink-faint)">{seconds}초</span>;
}

/** What shows under a reply while it is being made: the current step, or a shimmer before the first word. */
export function ActivityLine({ item }: { item: AssistantChatItem }) {
  if (item.activity) {
    return (
      <div role="status" className="flex items-center gap-2 text-[13px] text-(--ink-soft)">
        <span aria-hidden className="ac-spinner h-3.5 w-3.5 shrink-0 rounded-full border-2 border-(--accent-soft) border-t-(--accent)" />
        <span className="ac-text-shimmer min-w-0 truncate">{item.activity}</span>
        <span aria-hidden className="ac-dots shrink-0 text-(--accent)">
          <span>.</span>
          <span>.</span>
          <span>.</span>
        </span>
        <Elapsed />
      </div>
    );
  }
  if (item.segments.length) return null;
  return (
    <div role="status" aria-label="Working" className="flex flex-col gap-2">
      <div className="ac-skeleton h-4 w-3/5 rounded" />
      <div className="ac-skeleton h-4 w-2/5 rounded" />
    </div>
  );
}

export function Transcript({
  items,
  busy,
  send,
  renderBlock,
  renderPending = (item) => <ActivityLine item={item} />,
  suggestionFilter,
  wide,
  gap = "gap-3",
  errorActions,
}: TranscriptProps) {
  return items.map((item, index) =>
    item.kind === "user" ? (
      <UserBubble key={index} text={item.text} />
    ) : (
      <div key={index} data-turn={item.turn} className={`flex flex-col ${gap}`}>
        {item.segments.map((segment, i) => {
          if (segment.type === "text") {
            const last = item.pending && i === item.segments.length - 1;
            return <AssistantText key={i} text={segment.text} streaming={last} />;
          }
          if (segment.type === "error") {
            return errorActions ? (
              <ErrorCard key={i} text={segment.text} actions={errorActions(item, index)} />
            ) : (
              <ErrorBubble key={i} text={segment.text} />
            );
          }
          // Directive-only components (screen_navigate/screen_highlight) render nothing — the
          // page and view act on onScreenDirective instead — so skip the wrapper entirely.
          // Otherwise the empty slot is still a flex child of this gap-3 column and leaves a hole.
          if ((SCREEN_DIRECTIVE_COMPONENTS as readonly string[]).includes(segment.block.component)) return null;
          // The plan is pinned above the composer (PlanPill), not shown in the reply.
          if (segment.block.component === "plan") return null;
          return (
            <div
              key={segment.slotKey}
              data-component={segment.block.component}
              className={`transition-opacity duration-300 ${segment.status === "retrying" ? "opacity-60" : ""} ${
                wide?.has(segment.block.component) ? "min-[1500px]:-mx-14" : ""
              }`}
            >
              {renderBlock(segment, item)}
            </div>
          );
        })}
        {item.pending ? renderPending(item) : null}
        {!item.pending && index === items.length - 1 ? (
          <Suggestions
            suggestions={suggestionFilter ? item.suggestions.filter(suggestionFilter) : item.suggestions}
            onPick={send}
            disabled={busy || item.suggestionsStale}
          />
        ) : null}
      </div>
    ),
  );
}

export function LatestPill({ onClick }: { onClick: () => void }) {
  return (
    <div className="pointer-events-none absolute inset-x-0 bottom-3 flex justify-center">
      <button
        type="button"
        onClick={onClick}
        className="pointer-events-auto rounded-full border border-(--line) bg-(--card) px-3.5 py-1.5 text-[13px] font-semibold text-(--ink) shadow-md transition hover:border-(--accent)"
      >
        ↓ Latest
      </button>
    </div>
  );
}
