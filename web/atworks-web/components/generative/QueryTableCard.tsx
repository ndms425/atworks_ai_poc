// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { AskButton, formatDate, GenCard, GenCardHeader, Pill } from "web-shared";
import { QUERY_SOURCE_LABEL } from "@/lib/kinds";
import type { AttachedItem, QueryTableColumn, QueryTablePayload, QueryTableRow } from "@/lib/types";

/**
 * 자가발전 질의(query_runs) 결과 표. 숫자는 하나도 이 파일에서 계산하지 않는다 — 열 정의도, 값도,
 * 모집단도, 창도 전부 서버가 QueryResult에서 채워 보낸 payload 그대로다.
 */

/** 비율 측정값(fail_rate와 그 _prev/_delta)은 값이 정수(0, 1)여도 소수 4자리로 고정한다 — 형식은 값이 아니라
 *  컬럼이 정한다. 나머지 측정값은 정수 카운트라 자릿수 구분만 한다. */
function isRatioColumn(key: string): boolean {
  return key === "fail_rate" || key.startsWith("fail_rate_");
}

function cellText(value: number | null | undefined, kind: QueryTableColumn["kind"], key: string): string {
  if (value === null || value === undefined) return "—";
  const text = isRatioColumn(key) ? value.toFixed(4) : Number.isInteger(value) ? value.toLocaleString() : value.toFixed(4);
  return kind === "delta" && value > 0 ? `+${text}` : text;
}

function cellTone(value: number | null | undefined, kind: QueryTableColumn["kind"]): string {
  if (kind !== "delta" || value === null || value === undefined || value === 0) return "text-(--ink)";
  return value > 0 ? "text-(--danger)" : "text-(--ok)";
}

function rowKey(row: QueryTableRow, index: number): string {
  const keys = Object.values(row.keys).map((v) => v ?? "—").join(" · ");
  return keys.length > 0 ? `${index}:${keys}` : `${index}`;
}

export default function QueryTableCard({
  payload,
  onPrefill,
  onAttach,
}: {
  payload: QueryTablePayload;
  onPrefill?: (text: string) => void;
  onAttach?: (item: Omit<AttachedItem, "order">) => void;
}) {
  const scope = `${payload.population.toLocaleString()}건 · 그룹 ${payload.total_groups.toLocaleString()}개 · ${QUERY_SOURCE_LABEL[payload.source] ?? payload.source}`;
  const hasSamples = payload.rows.some((r) => r.run_ids.length > 0);
  return (
    <GenCard>
      <GenCardHeader title={payload.title} meta={<b className="font-semibold text-(--ink)">{scope}</b>} />
      <p className="px-3.5 pb-2 text-[11.5px] text-(--ink-soft)">
        {formatDate(payload.window.since)} ~ {formatDate(payload.window.until)}
        {payload.compare ? <span className="ml-1.5"><Pill tone="violet">이전 기간 비교</Pill></span> : null}
      </p>
      <div className="panel-scroll overflow-x-auto px-3.5 pb-2">
        {/* data-component: 스모크·E2E가 카드를 푸터 문구가 아니라 안정된 훅으로 찾게 한다(표시에는 영향 없음). */}
        <table data-component="query_table" className="w-full border-collapse text-[12.5px]">
          <thead>
            <tr className="text-left text-[11.5px] font-semibold text-(--ink-soft)">
              {payload.columns.map((column) => (
                <th
                  key={column.key}
                  className={column.kind === "dimension" ? "py-1.5 pr-3" : "px-2 py-1.5 text-right"}
                >
                  {column.label}
                </th>
              ))}
              {hasSamples ? <th className="py-1.5 pl-2" /> : null}
            </tr>
          </thead>
          <tbody>
            {payload.rows.map((row, index) => (
              <tr key={rowKey(row, index)} className="border-t border-(--line)">
                {payload.columns.map((column) =>
                  column.kind === "dimension" ? (
                    <td key={column.key} className="py-1.5 pr-3 font-mono text-(--ink)">
                      {row.keys[column.key] ?? "—"}
                    </td>
                  ) : (
                    <td
                      key={column.key}
                      className={`px-2 py-1.5 text-right tabular-nums ${cellTone(row.measures[column.key], column.kind)}`}
                    >
                      {cellText(row.measures[column.key], column.kind, column.key)}
                    </td>
                  ),
                )}
                {hasSamples ? (
                  <td className="py-1.5 pl-2">
                    {row.run_ids[0] ? (
                      <AskButton
                        label="채팅에 첨부"
                        onClick={() =>
                          onAttach?.({
                            kind: "run",
                            ref_id: row.run_ids[0],
                            label: Object.values(row.keys).map((v) => v ?? "—").join(" · ") || row.run_ids[0],
                          })
                        }
                      />
                    ) : null}
                  </td>
                ) : null}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {payload.rows.length === 0 ? (
        <p className="px-3.5 pb-3 text-[12px] text-(--ink-soft)">조건에 맞는 그룹이 없습니다.</p>
      ) : null}
      {payload.compare && payload.compare_note ? (
        <p className="px-3.5 pb-2 text-[11.5px] text-(--ink-soft)">{payload.compare_note}</p>
      ) : null}
      {payload.note ? <p className="px-3.5 pb-2 text-[12px] text-(--ink-soft)">{payload.note}</p> : null}
      {/* T6이 pending_alias를, T8이 👍/👎를 채운다 — 그때까지 아무것도 그리지 않는다. */}
      {payload.pending_alias ? (
        <p className="px-3.5 pb-2 text-[12px] text-(--ink-soft)">
          <b className="font-semibold text-(--ink)">‘{payload.pending_alias.term}’</b> = {payload.pending_alias.fragment_summary}
        </p>
      ) : null}
      <div className="px-3.5 pb-3 text-[12px] text-(--ink-soft)">
        <p>
          {payload.spec_summary}
          {onPrefill ? (
            <span className="ml-1.5">
              <AskButton label="조건 바꿔서 다시" onClick={() => onPrefill(`${payload.spec_summary} 조건을 바꿔서 다시 보여줘`)} />
            </span>
          ) : null}
        </p>
        <details className="mt-1.5">
          <summary className="cursor-pointer">실행된 질의(JSON)</summary>
          <pre className="panel-scroll mt-1 overflow-x-auto whitespace-pre font-mono text-[11.5px] text-(--ink-soft)">
            {JSON.stringify(payload.spec, null, 2)}
          </pre>
        </details>
      </div>
    </GenCard>
  );
}
