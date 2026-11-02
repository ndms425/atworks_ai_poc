// "2/4단계" pinned above the composer while a multi-step reply runs. The steps come from the
// model's present_plan call; a step is ticked off only when the tool it names has actually
// returned a result (`toolsDone`), never because the model said so. Follows open-design's
// PlanPill (Apache-2.0, apps/web/src/components/chat/PlanPill.tsx): collapsed to one line,
// the list on hover.

"use client";

import { useState } from "react";
import { Icon } from "./icons";
import type { AssistantChatItem } from "./protocol";

interface Step {
  label: string;
  tool: string;
}

export function planOf(item: AssistantChatItem | undefined): { steps: Step[]; done: boolean[] } | null {
  if (!item?.pending) return null;
  const segment = item.segments.find((s) => s.type === "ui" && s.block.component === "plan");
  if (!segment || segment.type !== "ui") return null;
  const steps = ((segment.block.payload as { steps?: Step[] }).steps ?? []).filter((s) => s?.label);
  if (steps.length < 2) return null;
  // Each finished tool result completes one step that names it (a tool used twice completes two).
  const left = [...(item.toolsDone ?? [])];
  const done = steps.map((step) => {
    const at = left.indexOf(step.tool);
    if (at < 0) return false;
    left.splice(at, 1);
    return true;
  });
  return { steps, done };
}

export function PlanPill({ item }: { item: AssistantChatItem | undefined }) {
  const [open, setOpen] = useState(false);
  const plan = planOf(item);
  if (!plan) {
    // No plan from the model: still show real progress once the reply is on its second tool —
    // the step it is on and what it is doing, without inventing a total.
    if (!item?.pending || item.tools.length < 2) return null;
    return (
      <div className="flex max-w-full items-center gap-2 rounded-full border border-(--line-strong) bg-(--card) py-1 pl-1.5 pr-3 text-[12px] text-(--ink)">
        <span className="rounded-full bg-(--accent-soft) px-2 py-px text-[11.5px] font-semibold tabular-nums text-(--accent-ink)">
          {item.tools.length}단계 진행 중
        </span>
        {item.activity ? <span className="truncate">{item.activity}</span> : null}
      </div>
    );
  }
  const finished = plan.done.filter(Boolean).length;
  const currentIndex = plan.done.findIndex((d) => !d);
  const current = currentIndex < 0 ? plan.steps.length - 1 : currentIndex;
  return (
    <div className="relative" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex max-w-full items-center gap-2 rounded-full border border-(--line-strong) bg-(--card) py-1 pl-1.5 pr-3 text-[12px] text-(--ink)"
      >
        <span className="rounded-full bg-(--accent-soft) px-2 py-px text-[11.5px] font-semibold tabular-nums text-(--accent-ink)">
          {Math.min(finished + 1, plan.steps.length)}/{plan.steps.length}단계
        </span>
        <span className="truncate">{plan.steps[current].label}</span>
      </button>
      {open ? (
        <ol className="absolute bottom-full left-0 z-40 mb-1.5 w-[min(300px,calc(100vw-2rem))] rounded-[12px] border border-(--line) bg-(--card) p-2 shadow-(--shadow-lg)">
          {plan.steps.map((step, index) => (
            <li key={index} className="flex items-center gap-2 rounded-md px-1.5 py-1 text-[12.5px]">
              <span
                className={`grid h-4 w-4 shrink-0 place-items-center rounded-full text-[10px] ${
                  plan.done[index]
                    ? "bg-(--ok) text-white"
                    : index === current
                      ? "border-2 border-(--accent)"
                      : "border border-(--line-strong) text-(--ink-faint)"
                }`}
              >
                {plan.done[index] ? <Icon name="check" size={10} /> : null}
              </span>
              <span className={plan.done[index] ? "text-(--ink-soft) line-through" : index === current ? "font-medium text-(--ink)" : "text-(--ink-2)"}>
                {step.label}
              </span>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}
