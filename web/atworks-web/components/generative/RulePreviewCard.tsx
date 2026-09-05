// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import type { ReactNode } from "react";
import { ApproveBar, ChangeStatusPill, formatDate, GenCard, GenCardHeader, Icon } from "web-shared";
import { type RuleAction, useRuleActions } from "@/lib/useRuleActions";
import type { RuleKind, RulePreviewPayload, ValidationRule } from "@/lib/types";

const KIND_LABEL: Record<RuleKind, string> = {
  compare: "비교",
  membership: "포함/배제",
  required: "필수값",
  format: "형식",
};

export default function RulePreviewCard({
  payload,
  onRuleAct,
}: {
  payload: RulePreviewPayload;
  onRuleAct?: (id: string, action: RuleAction) => Promise<ValidationRule | null>;
}) {
  // web-shared JobPreviewCard와 같은 패턴 — 후속 change_update가 payload.change에 얹히면 그게 최신.
  const { rule, busy, error, act, canAct } = useRuleActions(payload.change ?? payload.rule, onRuleAct);
  const impact = payload.impact;

  const rows: Array<[string, ReactNode]> = [
    ["대상 API", payload.api ? `${payload.api.method} ${payload.api.path}` : rule.api_id],
    ["파라미터", rule.param],
    ["규칙", rule.message],
    ["종류", KIND_LABEL[rule.kind] ?? rule.kind],
  ];

  return (
    <GenCard>
      <GenCardHeader
        title={payload.headline ?? rule.message}
        meta={
          <>
            <ChangeStatusPill status={rule.status} />
            <span aria-hidden>·</span>
            <span>{formatDate(rule.created_at)}</span>
          </>
        }
      />
      {payload.note ? <p className="px-3.5 pt-1 text-[12.5px] text-(--ink-soft)">{payload.note}</p> : null}
      <table className="mx-3.5 my-2 w-[calc(100%-28px)] text-[13px]">
        <tbody>
          {rows.map(([label, value]) => (
            <tr key={label}>
              <td className="py-1 pr-3 align-top text-(--ink-soft)">{label}</td>
              <td className="py-1 font-medium">{value}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {payload.format_hint ? (
        <p className="mx-3.5 mb-2 text-[12.5px] text-(--ink-soft)">
          형식: {payload.format_hint.label}
          {payload.format_hint.example
            ? ` · 예: ${payload.format_hint.example}`
            : payload.format_hint.pattern
              ? ` · ${payload.format_hint.pattern}`
              : ""}
        </p>
      ) : null}
      {payload.review_required ? (
        <div className="mx-3.5 mb-2 flex items-start gap-2 rounded-[11px] bg-(--warn-soft) px-3 py-2 text-[12.5px] leading-snug text-(--ink)">
          <Icon name="alert" size={14} className="mt-[2px] text-(--warn)" />
          <span>직접 검토 필요: 정규식</span>
        </div>
      ) : null}
      <div className="mx-3.5 mb-2 rounded-[11px] bg-(--ground) px-3 py-2 text-[12.5px] leading-snug text-(--ink-soft)">
        <p>이 규칙은 승인 시점 이후 실행부터 적용됩니다. 과거 결과·성공률은 그대로입니다.</p>
        {impact && impact.known_inputs !== 0 ? (
          <p className="mt-1">
            최근 {impact.window_runs}건 중 입력 아는 {impact.known_inputs}건, 그중 {impact.would_fail}건 해당
          </p>
        ) : null}
      </div>
      <ApproveBar change={rule} busy={busy} error={error} canAct={canAct} onAct={(action) => void act(action)} />
    </GenCard>
  );
}
