// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect, useRef, useState } from "react";
import { Icon } from "./icons";

export interface Prefill {
  text: string;
  /** Changes on every request so the same text can be offered twice. */
  nonce: number;
}

const VARIANTS = {
  /** The storefront's composer under the page: one roomy field with the send arrow inside it. */
  dock: {
    form: "items-center gap-2 rounded-[16px] border border-(--line-strong) bg-(--card) py-1.5 pl-4 pr-1.5 shadow-(--shadow) transition-colors focus-within:border-(--accent)",
    input: "bg-transparent py-1.5 text-[16px]",
    button: "h-9 w-9 rounded-[11px]",
  },
  /** The portal rail: the same field, compact. */
  field: {
    form: "items-center gap-1.5 rounded-[14px] border border-(--line-strong) bg-(--card) py-[5px] pl-3.5 pr-[5px] shadow-(--shadow-sm) transition-colors focus-within:border-(--accent)",
    // 16px below lg so touch browsers do not zoom on focus.
    input: "bg-transparent py-1.5 text-[16px] lg:text-[14.5px]",
    button: "h-8 w-8 rounded-[10px]",
  },
};

const DRAFT_PREFIX = "chat-draft:";

function readDraft(key: string): string {
  try {
    return localStorage.getItem(DRAFT_PREFIX + key) ?? "";
  } catch {
    return "";
  }
}

function writeDraft(key: string, text: string): void {
  try {
    if (text.trim()) localStorage.setItem(DRAFT_PREFIX + key, text);
    else localStorage.removeItem(DRAFT_PREFIX + key);
  } catch {
    // Storage may be unavailable; the draft simply does not survive a reload.
  }
}

/** A prefill only fills the draft. screenshot_tour.py waits on the "Working…" placeholder. */
export function Composer({
  send,
  ready,
  busy,
  label,
  placeholder,
  prefill,
  focusKey = 0,
  onQueue,
  busyPlaceholder = "Working…",
  draftKey,
  variant = "dock",
  className = "",
}: {
  send: (text: string) => void;
  ready: boolean;
  busy: boolean;
  label: string;
  placeholder: string;
  prefill?: Prefill | null;
  /** Bump to pull the caret into the field (e.g. right after something was attached). 0 = never. */
  focusKey?: number;
  /** While `busy`, a submit goes here (a send queue) instead of being refused. */
  onQueue?: (text: string) => void;
  /** The placeholder while busy. */
  busyPlaceholder?: string;
  /** When set, the unsent text is kept per key (a conversation) across reloads and switches. */
  draftKey?: string | null;
  variant?: keyof typeof VARIANTS;
  className?: string;
}) {
  const [draft, setDraft] = useState("");
  const boxRef = useRef<HTMLTextAreaElement>(null);

  // Drafts: the unsent text of each conversation survives a reload and a switch. On a key change
  // the outgoing key's text is written at once (a pending debounced write would be dropped).
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const keyRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (keyRef.current && keyRef.current !== draftKey) writeDraft(keyRef.current, draftRef.current);
    keyRef.current = draftKey;
    if (draftKey) setDraft(readDraft(draftKey));
  }, [draftKey]);
  useEffect(() => {
    if (!draftKey) return;
    const timer = window.setTimeout(() => writeDraft(draftKey, draft), 250);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft]);

  useEffect(() => {
    if (!prefill) return;
    setDraft(prefill.text);
    boxRef.current?.focus();
  }, [prefill]);

  // On mount as well as on change, so a click that also OPENS the panel still lands in the field.
  useEffect(() => {
    if (!focusKey) return;
    const frame = requestAnimationFrame(() => boxRef.current?.focus({ preventScroll: true }));
    return () => cancelAnimationFrame(frame);
  }, [focusKey]);

  const submit = () => {
    if (!draft.trim() || !ready) return;
    if (busy) {
      if (!onQueue) return;
      onQueue(draft);
    } else {
      send(draft);
    }
    setDraft("");
  };

  return (
    <form
      className={`flex ${VARIANTS[variant].form} ${className}`}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <textarea
        ref={boxRef}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            submit();
          }
        }}
        rows={1}
        aria-label={label}
        placeholder={busy ? busyPlaceholder : placeholder}
        className={`max-h-40 min-w-0 flex-1 resize-none text-(--ink) outline-none transition placeholder:text-(--ink-soft)/70 ${VARIANTS[variant].input}`}
      />
      <button
        type="submit"
        disabled={(busy && !onQueue) || !ready || !draft.trim()}
        aria-label="Send"
        className={`grid shrink-0 place-items-center bg-(--ink) text-(--surface) transition hover:brightness-110 disabled:opacity-35 ${VARIANTS[variant].button}`}
      >
        <Icon name="arrow-up" size={16} />
      </button>
    </form>
  );
}
