// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { type ReactNode, useEffect } from "react";
import { ActivityButton } from "../ActivityButton";
import { Composer, type Prefill } from "../Composer";
import { Icon } from "../icons";
import { useStickToBottom } from "../scroll";
import type { ErrorCardActions } from "../MessageBubble";
import type { AssistantChatItem } from "../protocol";
import { MessageRail } from "../MessageRail";
import { PlanPill } from "../PlanPill";
import { QuoteBar } from "../QuoteBar";
import { LatestPill, Transcript, type TranscriptProps } from "../Transcript";
import type { AgentTurn } from "../turn";
import { IconButton } from "../ui";

/** Under host approval only a card's own buttons can act, so approve/dismiss chips never render. */
const ACTION_CHIP = /\b(approve|apply|dismiss|discard)\b/i;
const isPlainChip = (text: string) => !ACTION_CHIP.test(text);

export interface AssistantPanelCopy {
  title: string;
  intro: string;
  starters: string[];
  /** aria-label of the message box. */
  label: string;
  placeholder: string;
}

/** The merchant portals' assistant rail. */
/** Starts a file download of `data` as pretty JSON, entirely client-side. */
function downloadJson(name: string, data: unknown): void {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function AssistantPanel({
  chat,
  copy,
  renderBlock,
  prefill,
  focusKey,
  newMemoryCount,
  onOpenActivity,
  onClose,
  fullscreen = false,
  onToggleFullscreen,
  aboveComposer,
  headerActions,
  onQueue,
  busyPlaceholder,
  draftKey,
  onQuote,
}: {
  chat: AgentTurn;
  copy: AssistantPanelCopy;
  renderBlock: TranscriptProps["renderBlock"];
  prefill?: Prefill | null;
  focusKey?: number;
  newMemoryCount: number;
  onOpenActivity: () => void;
  onClose: () => void;
  fullscreen?: boolean;
  onToggleFullscreen?: () => void;
  /** Rendered inside the composer's box, directly above the message field — the seat for
   * whatever the next send will carry (a portal's attached records, say). Layout only: this
   * file stays domain-agnostic, so the app owns what goes in here. */
  aboveComposer?: ReactNode;
  /** Extra header controls, left of Activity (an app's conversation switcher, say). */
  headerActions?: ReactNode;
  /** A send while a reply is streaming goes to this queue instead of being refused. */
  onQueue?: (text: string) => void;
  busyPlaceholder?: string;
  /** Keeps the composer's unsent text per conversation. */
  draftKey?: string | null;
  /** Selecting part of a reply offers to quote it into the next question. */
  onQuote?: (text: string) => void;
}) {
  const { scrollRef, onScroll, showLatest, jumpToLatest } = useStickToBottom(chat.items, chat.busy, {
    onlyWhileBusy: true,
  });

  // Scrolls a settled reply to its first card. Keyed on turnCount as well as busy so a reply
  // that settles within one render still scrolls.
  useEffect(() => {
    if (chat.busy) return;
    const container = scrollRef.current;
    const replies = container?.querySelectorAll<HTMLElement>("[data-turn]");
    const reply = replies?.[replies.length - 1];
    if (!container || !reply) return;
    const target = reply.querySelector<HTMLElement>("[data-component]") ?? reply;
    const top = target.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop;
    container.scrollTo({ top: Math.max(0, top - 8), behavior: "smooth" });
  }, [chat.busy, chat.turnCount, scrollRef]);

  const column = fullscreen ? "mx-auto w-full max-w-3xl" : "";

  // A failed reply offers: send its question again (only the latest reply, and not mid-stream),
  // copy a diagnostic, and download this session's trace. No session id goes into either: it is
  // the session's bearer token.
  const errorActions = (item: AssistantChatItem, index: number): ErrorCardActions => {
    const question = chat.items[index - 1];
    const errors = item.segments.filter((s) => s.type === "error").map((s) => (s.type === "error" ? s.text : ""));
    const diagnostic = JSON.stringify(
      { at: new Date().toISOString(), turn: item.turn, question: question?.kind === "user" ? question.text : null,
        tools: item.tools, errors },
      null,
      2,
    );
    return {
      diagnostic,
      onRetry:
        index === chat.items.length - 1 && !chat.busy && question?.kind === "user"
          ? () => void chat.send(question.text)
          : undefined,
      onExportLog: () =>
        downloadJson(`chat-log-${new Date().toISOString().replace(/[:.]/g, "-")}.json`, {
          exported_at: new Date().toISOString(),
          turns: chat.items.map((entry) =>
            entry.kind === "user"
              ? { role: "user", text: entry.text }
              : { role: "assistant", turn: entry.turn, tools: entry.tools,
                  segments: entry.segments.map((s) => (s.type === "ui" ? { type: "ui", component: s.block.component } : s)) },
          ),
          trace: chat.trace,
        }),
    };
  };

  return (
    <div className="flex h-full w-full flex-col border-l border-(--line) bg-(--card)">
      <div className="relative flex items-center gap-2.5 border-b border-(--line) py-3 pl-4 pr-2.5">
        <span className="grid h-[30px] w-[30px] shrink-0 place-items-center rounded-[10px] bg-(--accent) text-(--on-accent)" aria-hidden>
          <Icon name="spark" size={15} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[14px] font-semibold leading-tight text-(--ink)">{copy.title}</div>
          <div className="truncate text-[11.5px] text-(--ink-soft)">You approve every change</div>
        </div>
        {headerActions}
        <ActivityButton streaming={chat.streaming} newMemoryCount={newMemoryCount} onClick={onOpenActivity} />
        {onToggleFullscreen ? (
          <IconButton
            icon={fullscreen ? "collapse" : "expand"}
            label={fullscreen ? "Exit full screen" : "Full screen"}
            onClick={onToggleFullscreen}
            className="hidden lg:grid"
          />
        ) : null}
        <IconButton icon="x" label="Hide assistant" onClick={onClose} />
      </div>

      <div className="relative min-h-0 flex-1">
        <div ref={scrollRef} onScroll={onScroll} className="panel-scroll h-full overflow-y-auto px-4 py-4">
          <div className={`flex flex-col gap-3 text-[14.5px] ${column}`}>
            {chat.items.length === 0 ? (
              <div className="mt-4">
                <p className="text-[16px] leading-relaxed text-(--ink)">{copy.intro}</p>
                <div className="mt-4 flex flex-col gap-2">
                  {copy.starters.map((starter) => (
                    <button
                      key={starter}
                      type="button"
                      onClick={() => void chat.send(starter)}
                      disabled={chat.busy || !chat.ready}
                      className="group flex items-center gap-3 rounded-xl border border-(--line) bg-(--card) px-3.5 py-2.5 text-left text-[13.5px] text-(--ink) shadow-(--shadow-sm) transition hover:border-(--accent) hover:bg-(--accent-soft) disabled:opacity-50"
                    >
                      <Icon name="spark" size={14} className="text-(--accent)" />
                      <span className="min-w-0 flex-1">{starter}</span>
                      <Icon name="arrow-right" size={14} className="text-(--ink-faint) transition group-hover:text-(--accent-ink)" />
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <Transcript
                items={chat.items}
                busy={chat.busy}
                send={chat.send}
                renderBlock={renderBlock}
                suggestionFilter={isPlainChip}
                gap="gap-2.5"
                errorActions={errorActions}
              />
            )}
          </div>
        </div>
        {showLatest ? <LatestPill onClick={jumpToLatest} /> : null}
        {onQuote ? <QuoteBar containerRef={scrollRef} onQuote={onQuote} /> : null}
        <MessageRail containerRef={scrollRef} version={chat.items.length + (chat.busy ? 0.5 : 0)} />
      </div>

      <div className="border-t border-(--line) p-3">
        <div className={`mb-2 empty:hidden ${column}`}>
          <PlanPill item={[...chat.items].reverse().find((entry) => entry.kind === "assistant") as AssistantChatItem | undefined} />
        </div>
        {aboveComposer ? <div className={`mb-2 ${column}`}>{aboveComposer}</div> : null}
        <Composer
          send={chat.send}
          ready={chat.ready}
          busy={chat.busy}
          label={copy.label}
          placeholder={copy.placeholder}
          prefill={prefill}
          focusKey={focusKey}
          onQueue={onQueue}
          busyPlaceholder={busyPlaceholder}
          draftKey={draftKey}
          variant="field"
          className={column}
        />
      </div>
    </div>
  );
}
