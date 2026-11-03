// The step list pinned above the composer while a reply runs. Two shapes, one component:
// with a plan from the model's present_plan call it reads "3/4단계" and shows the steps still
// to come; without one it reads "3단계 진행 중" and the list simply grows. The second case is
// the common one — a model that does not call present_plan never declares a total, and a
// denominator nobody committed to would be a number the model did not compute.
//
// A step ticks off only when the tool it names actually returned (`steps[].doneAt`), never
// because the model said so; a tool that returned an error stops the step rather than leaving
// it spinning. It lives above the composer, not in the transcript, so scrolling up cannot lose
// it — which is why its height is capped: the composer must not move.

"use client";

import { useState } from "react";
import { Icon } from "./icons";
import type { AssistantChatItem, TurnStep } from "./protocol";

/** Rows kept visible; older finished steps collapse into one summary row. */
const VISIBLE_ROWS = 4;

/**
 * Between one tool's result and the next call — and after the last one, while the reply is
 * written — no tool is in flight, but the turn is still running. Without a row for it the list
 * reads as every step struck through under a "진행 중" counter, which looks stalled. It is not a
 * step: the counter still counts tool calls only.
 */
const WORKING_ROW: Row = { label: "응답 생성 중…", state: "running" };

interface Row {
  label: string;
  state: "done" | "failed" | "running" | "upcoming";
  seconds?: number;
}

const isDone = (step: TurnStep) => step.doneAt != null && !step.failed;

function secondsOf(step: TurnStep, now: number): number {
  return Math.max(0, Math.round(((step.doneAt ?? now) - step.startedAt) / 100)) / 10;
}

/**
 * The plan's steps matched against the calls that actually happened. A step is complete when a
 * call naming its tool has come back clean; each finished call completes at most one step, so a
 * tool used twice completes two.
 */
function planRows(item: AssistantChatItem, now: number): Row[] | null {
  const segment = item.segments.find((s) => s.type === "ui" && s.block.component === "plan");
  if (!segment || segment.type !== "ui") return null;
  const steps = ((segment.block.payload as { steps?: { label: string; tool: string }[] }).steps ?? [])
    .filter((s) => s?.label);
  if (steps.length < 2) return null;
  const left = (item.steps ?? []).filter(isDone).map((s) => s.tool);
  const done = steps.map((step) => {
    const at = left.indexOf(step.tool);
    if (at < 0) return false;
    left.splice(at, 1);
    return true;
  });
  const running = done.findIndex((d) => !d);
  // The call in flight dates the running row; without one the row is simply current.
  const inFlight = (item.steps ?? []).find((s) => s.doneAt == null);
  return steps.map((step, index) => ({
    label: step.label,
    state: done[index] ? "done" : index === running ? "running" : "upcoming",
    seconds: done[index] ? undefined : index === running && inFlight ? secondsOf(inFlight, now) : undefined,
  }));
}

/** No plan: the calls themselves are the list, newest last, with no total invented for them. */
function callRows(item: AssistantChatItem, now: number): Row[] | null {
  const steps = item.steps ?? [];
  if (steps.length < 2) return null;
  return steps.map((step) => ({
    label: step.label,
    state: step.failed ? "failed" : step.doneAt != null ? "done" : "running",
    seconds: secondsOf(step, now),
  }));
}

function StepRow({ row }: { row: Row }) {
  const mark =
    row.state === "done" ? (
      <span className="grid h-4 w-4 shrink-0 place-items-center rounded-full bg-(--ok) text-white">
        <Icon name="check" size={10} />
      </span>
    ) : row.state === "failed" ? (
      <span className="grid h-4 w-4 shrink-0 place-items-center rounded-full bg-(--danger) text-[10px] font-semibold leading-none text-white">
        ×
      </span>
    ) : row.state === "running" ? (
      <span className="h-4 w-4 shrink-0 rounded-full border-2 border-(--line-strong) border-t-(--accent) motion-safe:animate-spin" />
    ) : (
      <span className="h-4 w-4 shrink-0 rounded-full border border-(--line-strong)" />
    );
  const text =
    row.state === "done"
      ? "text-(--ink-soft) line-through"
      : row.state === "failed"
        ? "text-(--danger)"
        : row.state === "running"
          ? "font-medium text-(--ink)"
          : "text-(--ink-faint)";
  return (
    <li className="flex items-center gap-2 py-[3px] text-[12.5px]">
      {mark}
      <span className={`truncate ${text}`}>{row.label}</span>
      {row.seconds != null && row.seconds >= 0.1 ? (
        <span className="ml-auto shrink-0 text-[11px] tabular-nums text-(--ink-faint)">{row.seconds.toFixed(1)}초</span>
      ) : null}
    </li>
  );
}

export function PlanPill({ item }: { item: AssistantChatItem | undefined }) {
  // Re-renders on every streamed event anyway; one timestamp per render dates the running row.
  const now = Date.now();
  const [expanded, setExpanded] = useState(false);
  if (!item?.pending) return null;
  const plan = planRows(item, now);
  const rows = plan ?? callRows(item, now);
  if (!rows) return null;

  const finished = rows.filter((r) => r.state === "done" || r.state === "failed").length;
  const counter = plan ? `${Math.min(finished + 1, rows.length)}/${rows.length}단계` : `${rows.length}단계 진행 중`;
  // The counter above is tool calls; this row is the turn still working between or after them.
  const listed = rows.some((r) => r.state === "running") ? rows : [...rows, WORKING_ROW];
  // Older finished rows fold away so the list cannot push the composer down the screen.
  const hidden = expanded ? 0 : Math.max(0, listed.length - VISIBLE_ROWS);
  const shown = listed.slice(hidden);

  return (
    <div className="rounded-[12px] border border-(--line-strong) bg-(--card) px-3 py-2">
      <div className="mb-1 flex items-center gap-2">
        <span className="rounded-full bg-(--accent-soft) px-2 py-px text-[11.5px] font-semibold tabular-nums text-(--accent-ink)">
          {counter}
        </span>
        {plan ? (
          <div className="h-[3px] flex-1 overflow-hidden rounded-full bg-(--well)">
            <div
              className="h-full bg-(--accent) transition-[width] duration-300"
              style={{ width: `${(finished / rows.length) * 100}%` }}
            />
          </div>
        ) : (
          <span className="text-[11px] text-(--ink-faint)">총 단계는 모름</span>
        )}
      </div>
      <ol aria-live="polite" aria-label="진행 단계">
        {hidden > 0 ? (
          <li>
            <button
              type="button"
              onClick={() => setExpanded(true)}
              className="flex w-full items-center gap-2 py-[3px] text-[12px] text-(--ink-soft) hover:text-(--ink)"
            >
              <span className="grid h-4 w-4 shrink-0 place-items-center rounded-full bg-(--ok) text-white">
                <Icon name="check" size={10} />
              </span>
              <span className="tabular-nums">앞의 {hidden}단계 완료</span>
            </button>
          </li>
        ) : null}
        {shown.map((row, index) => (
          <StepRow key={hidden + index} row={row} />
        ))}
      </ol>
    </div>
  );
}
