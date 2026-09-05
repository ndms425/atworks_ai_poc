// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { ApproveBar, ChangeStatusPill, formatDate, GenCard, GenCardHeader, Pill, type Tone } from "web-shared";
import { type FormatBatchAction, useFormatBatchActions } from "@/lib/useFormatBatchActions";
import type { FormatBatch, FormatBatchEntry, FormatBatchOutcome, FormatBatchPayload } from "@/lib/types";

const OUTCOME_BADGE: Record<FormatBatchOutcome, { tone: Tone; label: string }> = {
  new: { tone: "accent", label: "신규" },
  duplicate: { tone: "muted", label: "중복" },
  invalid: { tone: "danger", label: "무효" },
};

function EntryRow({ entry }: { entry: FormatBatchEntry }) {
  const badge = OUTCOME_BADGE[entry.outcome];
  return (
    <li className="px-3.5 py-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[13px] font-semibold text-(--ink)">{entry.name}</span>
        <Pill tone={badge.tone} dot>
          {badge.label}
        </Pill>
      </div>
      <p className="mt-0.5 break-all font-mono text-[12px] text-(--ink-soft)">{entry.pattern}</p>
      {entry.reason ? <p className="mt-0.5 text-[12px] text-(--ink-soft)">{entry.reason}</p> : null}
      {entry.pass_examples.length ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <span className="text-[11.5px] text-(--ok)">통과</span>
          {entry.pass_examples.map((example) => (
            <span key={`pass-${example}`} className="rounded-full bg-(--ok-soft) px-2 py-0.5 font-mono text-[11.5px] text-(--ink)">
              {example}
            </span>
          ))}
        </div>
      ) : null}
      {entry.fail_examples.length ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <span className="text-[11.5px] text-(--danger)">실패</span>
          {entry.fail_examples.map((example) => (
            <span key={`fail-${example}`} className="rounded-full bg-(--danger-soft) px-2 py-0.5 font-mono text-[11.5px] text-(--ink)">
              {example}
            </span>
          ))}
        </div>
      ) : null}
    </li>
  );
}

export default function FormatBatchCard({
  payload,
  onFormatBatchAct,
}: {
  payload: FormatBatchPayload;
  onFormatBatchAct?: (id: string, action: FormatBatchAction) => Promise<FormatBatch | null>;
}) {
  // RulePreviewCard와 같은 패턴 — 후속 change_update가 payload.change에 얹히면 그게 최신.
  const { batch, busy, error, act, canAct } = useFormatBatchActions(payload.change ?? payload.batch, onFormatBatchAct);

  return (
    <GenCard>
      <GenCardHeader
        title={payload.headline ?? batch.summary ?? "포맷 일괄 등록"}
        meta={
          <>
            <ChangeStatusPill status={batch.status} />
            <span aria-hidden>·</span>
            <span>{formatDate(batch.created_at)}</span>
          </>
        }
      />
      {payload.note ? <p className="px-3.5 pt-1 text-[12.5px] text-(--ink-soft)">{payload.note}</p> : null}
      <p className="mx-3.5 mt-2 text-[12.5px] text-(--ink-soft)">
        신규 {payload.new_count} · 중복 {payload.duplicate_count} · 무효 {payload.invalid_count}
      </p>
      <ul className="mx-3.5 my-2 divide-y divide-(--line) rounded-[11px] border border-(--line)">
        {payload.entries.map((entry, i) => (
          <EntryRow key={`${entry.name}-${i}`} entry={entry} />
        ))}
      </ul>
      <ApproveBar change={batch} busy={busy} error={error} canAct={canAct} onAct={(action) => void act(action)} />
    </GenCard>
  );
}
