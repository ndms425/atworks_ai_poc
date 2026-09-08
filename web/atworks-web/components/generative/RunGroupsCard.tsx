// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { AskButton, formatDate, GenCard, GenCardHeader, Pill } from "web-shared";
import { GROUP_BY_LABEL, POPULATION_FILTER_LABEL } from "@/lib/kinds";
import type { AttachedItem, RunGroupsPayload } from "@/lib/types";

export default function RunGroupsCard({
  payload,
  onPrefill,
  onAttach,
}: {
  payload: RunGroupsPayload;
  onPrefill?: (text: string) => void;
  onAttach?: (item: Omit<AttachedItem, "order">) => void;
}) {
  const scope = `${POPULATION_FILTER_LABEL[payload.population_filter]} ${payload.population}건 · ${GROUP_BY_LABEL[payload.group_by]} · 그룹 ${payload.shown}개`;
  const showTiming = payload.group_by === "api" || payload.group_by === "api_env_data";
  return (
    <GenCard>
      <GenCardHeader title={payload.title ?? "실행 묶음"} meta={<b className="font-semibold text-(--ink)">{scope}</b>} />
      <div className="panel-scroll overflow-x-auto px-3.5 pb-2">
        <table data-component="run_groups" className="w-full border-collapse text-[12.5px]">
          <thead>
            <tr className="text-left text-[11.5px] font-semibold text-(--ink-soft)">
              <th className="py-1.5 pr-3">그룹</th>
              <th className="px-2 py-1.5 text-right">건수</th>
              <th className="px-2 py-1.5 text-right">실패·에러</th>
              {showTiming ? <th className="px-2 py-1.5">첫 실패</th> : null}
              <th className="px-2 py-1.5 text-right">p95</th>
              <th className="px-2 py-1.5">표시</th>
              <th className="py-1.5 pl-2" />
            </tr>
          </thead>
          <tbody>
            {payload.items.map((g) => (
              <tr key={g.key} className="border-t border-(--line)">
                <td className="py-1.5 pr-3 font-mono text-(--ink)">{g.label}</td>
                <td className="px-2 py-1.5 text-right tabular-nums">{g.count}</td>
                <td className="px-2 py-1.5 text-right tabular-nums">{g.fail + g.error}</td>
                {showTiming ? (
                  <td className="px-2 py-1.5 tabular-nums text-(--ink-soft)">
                    {g.first_non_pass_at ? formatDate(g.first_non_pass_at) : "—"}
                    {g.last_pass_before ? <span> · 직전 성공 {formatDate(g.last_pass_before)}</span> : null}
                  </td>
                ) : null}
                <td className="px-2 py-1.5 text-right tabular-nums text-(--ink-soft)">{g.p95_duration_ms != null ? `${g.p95_duration_ms}ms` : "—"}</td>
                <td className="px-2 py-1.5">
                  <span className="flex flex-wrap gap-1">
                    {g.flaky ? <Pill tone="warn" dot>불안정</Pill> : null}
                    {g.regression_suspect ? <Pill tone="danger" dot>회귀 의심</Pill> : null}
                  </span>
                </td>
                <td className="py-1.5 pl-2">
                  <span className="flex gap-1">
                    {g.run_ids?.[0] ? (
                      <AskButton label="채팅에 첨부" onClick={() => onAttach?.({ kind: "run", ref_id: g.run_ids![0], label: g.label })} />
                    ) : null}
                    <AskButton label="자세히" onClick={() => onPrefill?.(`${g.label} 그룹 자세히 보여줘`)} />
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {payload.note ? <p className="px-3.5 pb-3 text-[12px] text-(--ink-soft)">{payload.note}</p> : null}
      <p className="px-3.5 pb-3 text-[12px] text-(--ink-soft)">
        불안정은 flaky_v1(기간 내 pass↔fail 전환 2회 이상), 회귀 의심은 스펙 수정일이 직전 성공과 첫 실패 사이에 있다는 표시입니다. 판정이 아닙니다.
      </p>
    </GenCard>
  );
}
