// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useEffect, useState } from "react";
import { Icon } from "web-shared";
import type { PortalViewId, ScreenHighlightPayload, ScreenTargetKind } from "@/lib/types";

/** Where each kind of row lives. */
const VIEW_OF: Record<ScreenTargetKind, PortalViewId> = { api: "apis", run: "runs", job: "jobs", rule: "rules" };
const VIEW_LABEL: Record<string, string> = { apis: "APIs", runs: "Runs", jobs: "Jobs", rules: "Rules" };
const CIRCLED = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧"];

/**
 * A note that the assistant marked rows on screen (numbered boxes), with a dismiss button — a
 * pointer, not an alarm, so it uses the info colour and a pin, never the error look.
 *
 * It sits at the bottom-left of the PAGE, clear of the assistant rail and its composer. When none
 * of the marked rows is on screen (the operator is on another view, or the row is not loaded), it
 * offers "보러 가기", which switches to the view those rows live on and scrolls to the first one —
 * otherwise the note would point at boxes nobody can see. Still click-through except its buttons.
 */
export default function ScreenHighlightOverlay({
  payload,
  view,
  onDismiss,
  onGo,
}: {
  payload: ScreenHighlightPayload;
  view: PortalViewId;
  onDismiss: () => void;
  onGo: (view: PortalViewId, target: { kind: ScreenTargetKind; ref_id: string }) => void;
}) {
  const first = payload.targets[0];
  const targetView = first ? VIEW_OF[first.kind] : null;
  const [onScreen, setOnScreen] = useState(true);

  // Rows render after their view fetches, so look for a marked row a few times before deciding
  // none is on screen.
  useEffect(() => {
    let tries = 0;
    const look = () =>
      payload.targets.some((t) => document.querySelector(`[data-ref="${CSS.escape(`${t.kind}:${t.ref_id}`)}"]`));
    setOnScreen(look());
    const timer = window.setInterval(() => {
      tries += 1;
      const found = look();
      setOnScreen(found);
      if (found || tries >= 8) window.clearInterval(timer);
    }, 250);
    return () => window.clearInterval(timer);
  }, [payload, view]);

  const items = payload.targets.slice(0, 3).map((t) => `${CIRCLED[t.number - 1] ?? `${t.number}.`} ${t.ref_id}`);
  return (
    <div
      role="status"
      className="pointer-events-none fixed bottom-5 left-5 z-50 flex max-w-[360px] items-start gap-2.5 rounded-[var(--radius)] border border-(--line-strong) bg-(--card) px-3.5 py-3 shadow-(--shadow-lg) lg:left-[84px] xl:left-[240px]"
    >
      <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full bg-(--info-soft) text-(--info)">
        <Icon name="pin" size={12} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[12.5px] font-semibold text-(--ink)">{payload.headline ?? "화면에 표시한 항목"}</div>
        <div className="mt-0.5 truncate text-[11.5px] text-(--ink-soft)">
          {payload.note ?? items.join("  ")}
          {payload.targets.length > 3 ? ` 외 ${payload.targets.length - 3}개` : ""}
        </div>
        {!onScreen && targetView && first && view === targetView ? (
          // Already on the right view, but the row is not on the loaded page (paged lists show the
          // newest rows first): say so rather than offer a button that would change nothing.
          <div className="mt-1.5 text-[11.5px] text-(--warn)">지금 목록 페이지에는 없는 항목입니다. 필터나 다음 페이지에서 찾을 수 있습니다.</div>
        ) : null}
        {!onScreen && targetView && first && view !== targetView ? (
          <button
            type="button"
            onClick={() => onGo(targetView, { kind: first.kind, ref_id: first.ref_id })}
            className="pointer-events-auto mt-2 inline-flex items-center gap-1 rounded-lg bg-(--info) px-2.5 py-1 text-[12px] font-semibold text-white hover:brightness-110"
          >
            {VIEW_LABEL[targetView] ?? targetView} 화면에서 보기
            <Icon name="arrow-right" size={12} />
          </button>
        ) : null}
      </div>
      <button
        type="button"
        aria-label="표시 닫기"
        onClick={onDismiss}
        className="pointer-events-auto grid h-5 w-5 shrink-0 place-items-center rounded-full text-(--ink-soft) hover:bg-(--well) hover:text-(--ink)"
      >
        <Icon name="x" size={12} />
      </button>
    </div>
  );
}
