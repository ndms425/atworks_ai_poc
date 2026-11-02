// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect, useRef, useState } from "react";
import { Icon } from "./icons";
import { Markdown } from "./Markdown";

/** Copies `text` and says so for a moment. */
function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);
  return (
    <button
      type="button"
      aria-label={copied ? "복사됨" : label}
      title={copied ? "복사됨" : label}
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => setCopied(true));
      }}
      className="grid h-7 w-7 shrink-0 place-items-center rounded-md text-(--ink-faint) opacity-0 transition-opacity hover:bg-(--ground) hover:text-(--ink) focus-visible:opacity-100 group-hover:opacity-100"
    >
      <Icon name={copied ? "check" : "copy"} size={14} />
    </button>
  );
}

/** `data-user-msg` is what the question rail finds; the copy button shows on hover. */
export function UserBubble({ text }: { text: string }) {
  return (
    <div className="group flex items-start justify-end gap-1" data-user-msg="">
      <CopyButton text={text} label="질문 복사" />
      <div className="user-bubble max-w-[72%] whitespace-pre-wrap px-3.5 py-2 text-[14.5px] leading-normal">{text}</div>
    </div>
  );
}

/**
 * Streamed prose reveals at a steady pace instead of in network-sized bursts: each frame shows a
 * slice of what has arrived, larger when far behind, so it never falls behind the stream for long.
 * Once the stream ends (or for history) the whole text shows at once.
 */
function useCharReveal(text: string, active: boolean): string {
  const [shown, setShown] = useState(active ? 0 : text.length);
  const target = useRef(text.length);
  target.current = text.length;
  useEffect(() => {
    if (!active || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setShown(target.current);
      return;
    }
    let frame = 0;
    const tick = () => {
      setShown((n) => (n >= target.current ? n : Math.min(target.current, n + Math.max(1, Math.ceil((target.current - n) / 10)))));
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [active]);
  return active ? text.slice(0, shown) : text;
}

/** Some models (Qwen through an Anthropic-compatible proxy) leak their tool-call markup into the
 *  prose; it is never meant for a reader. */
const LEAKED_MARKUP = /<\/?tool_call>|<\/?function_calls?>|<\/?invoke[^>]*>/g;

export function AssistantText({ text: raw, streaming }: { text: string; streaming?: boolean }) {
  const text = raw.replace(LEAKED_MARKUP, "");
  const shown = useCharReveal(text, !!streaming);
  if (!text.trim() && !streaming) return null;
  return (
    <div className={`leading-relaxed text-(--ink) ${streaming ? "streaming-caret" : ""}`}>
      <Markdown text={shown} />
    </div>
  );
}

export function ErrorBubble({ text }: { text: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-(--danger)/40 bg-(--danger-soft) px-3 py-2 text-[13px] leading-relaxed text-(--danger)"
    >
      {text}
    </div>
  );
}

export interface ErrorCardActions {
  /** Sends the failed turn's question again. Absent when a retry cannot apply (an older turn). */
  onRetry?: () => void;
  /** The diagnostic text "오류 진단 복사" puts on the clipboard. */
  diagnostic: string;
  /** Downloads the session's trace as a file. */
  onExportLog?: () => void;
}

/**
 * A failed reply as a card: what happened in one line, the message, and what to do about it.
 * Follows open-design's RunErrorCard (Apache-2.0, apps/web/src/components/chat/RunErrorCard.tsx).
 */
export function ErrorCard({ text, actions }: { text: string; actions: ErrorCardActions }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);
  return (
    <div role="alert" className="rounded-[12px] border border-(--danger)/30 bg-(--card) px-3.5 py-3 shadow-(--shadow-sm)">
      <div className="flex items-center gap-2 text-[13.5px] font-semibold text-(--danger)">
        <Icon name="alert" size={16} />
        답변을 끝까지 만들지 못했습니다
      </div>
      <p className="mt-1 text-[13px] leading-relaxed text-(--ink-2)">{text}</p>
      <div className="mt-2.5 flex flex-wrap items-center justify-end gap-1.5">
        <button
          type="button"
          onClick={() => {
            void navigator.clipboard?.writeText(actions.diagnostic).then(() => setCopied(true));
          }}
          className="rounded-lg px-2.5 py-1.5 text-[12.5px] text-(--ink-soft) hover:bg-(--ground) hover:text-(--ink)"
        >
          {copied ? "복사됨" : "오류 진단 복사"}
        </button>
        {actions.onExportLog ? (
          <button
            type="button"
            onClick={actions.onExportLog}
            className="rounded-lg border border-(--line-strong) px-2.5 py-1.5 text-[12.5px] text-(--ink) hover:bg-(--ground)"
          >
            로그 내보내기
          </button>
        ) : null}
        {actions.onRetry ? (
          <button
            type="button"
            onClick={actions.onRetry}
            className="rounded-lg bg-(--ink) px-3 py-1.5 text-[12.5px] font-semibold text-(--surface) hover:brightness-110"
          >
            다시 시도
          </button>
        ) : null}
      </div>
    </div>
  );
}
