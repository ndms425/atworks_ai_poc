// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { Icon } from "web-shared";
import type { ScreenHighlightPayload } from "@/lib/types";

/**
 * A small floating chip telling the operator the assistant boxed some rows on screen, with a
 * dismiss button. Rendered only while `payload` is non-null (page.tsx owns that lifecycle).
 */
export default function ScreenHighlightOverlay({ payload, onDismiss }: { payload: ScreenHighlightPayload; onDismiss: () => void }) {
  return (
    <div
      role="status"
      className="fixed bottom-5 right-5 z-50 flex max-w-[320px] items-start gap-2 rounded-[var(--radius)] border border-(--line-strong) bg-(--card) px-3.5 py-3 shadow-(--shadow-lg)"
    >
      <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full bg-(--danger-soft) text-(--danger)">
        <Icon name="alert" size={13} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[12.5px] font-semibold text-(--ink)">{payload.headline ?? "화면 강조"}</div>
        {payload.note ? <div className="mt-0.5 text-[11.5px] text-(--ink-soft)">{payload.note}</div> : null}
      </div>
      <button
        type="button"
        aria-label="강조 닫기"
        onClick={onDismiss}
        className="grid h-5 w-5 shrink-0 place-items-center rounded-full text-(--ink-soft) hover:bg-(--well) hover:text-(--ink)"
      >
        <Icon name="x" size={12} />
      </button>
    </div>
  );
}
