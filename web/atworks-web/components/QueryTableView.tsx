// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { QUERY_DIMENSION_LABEL, QUERY_MEASURE_LABEL, QUERY_SOURCE_LABEL } from "@/lib/kinds";
import type { QueryDimension, QueryMeasure, QueryResult, QueryTableColumn, QueryTableRow } from "@/lib/types";

/**
 * `QueryResult` 하나를 그대로 그리는 표. 카드가 아니라 표만이라 저장 질문(Home)처럼 서버가
 * 카드 payload를 만들어 주지 않는 자리에서 쓴다 — 채팅의 `query_table` 카드는 여전히 서버가
 * 채운 payload를 읽는 `QueryTableCard`가 그린다(둘의 통합은 Task 10에서 본다).
 *
 * 숫자는 하나도 여기서 계산하지 않는다: 행·모집단·그룹 수·창은 전부 서버가 낸 값이고, 이 파일이
 * 하는 일은 `spec`의 차원·측정값을 열로 펴고 카탈로그 라벨(lib/kinds.ts)을 붙이는 것뿐이다.
 */

/** 비율 측정값(fail_rate와 그 _prev/_delta)은 값이 정수여도 소수 4자리로 고정 — 형식은 값이
 *  아니라 컬럼이 정한다(QueryTableCard와 같은 규칙). */
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
  const keys = Object.values(row.keys)
    .map((v) => v ?? "—")
    .join(" · ");
  return keys.length > 0 ? `${index}:${keys}` : `${index}`;
}

/** 서버의 enrich_query_table과 같은 순서·같은 라벨로 열을 편다: 차원(없으면 "전체" 한 열),
 *  그다음 측정값, 비교 질의면 측정값마다 (이전)/(증감)이 뒤따른다. */
export function columnsFor(result: QueryResult): QueryTableColumn[] {
  const dimensions = (result.spec.dimensions ?? []) as QueryDimension[];
  const columns: QueryTableColumn[] = dimensions.map((dimension) => ({
    key: dimension,
    label: QUERY_DIMENSION_LABEL[dimension] ?? dimension,
    kind: "dimension",
  }));
  if (columns.length === 0) columns.push({ key: "_total", label: "전체", kind: "dimension" });
  for (const measure of (result.spec.measures ?? []) as QueryMeasure[]) {
    const label = QUERY_MEASURE_LABEL[measure] ?? measure;
    columns.push({ key: measure, label, kind: "measure" });
    if (result.spec.compare_previous_window) {
      columns.push({ key: `${measure}_prev`, label: `${label} (이전)`, kind: "prev" });
      columns.push({ key: `${measure}_delta`, label: `${label} (증감)`, kind: "delta" });
    }
  }
  return columns;
}

export default function QueryTableView({ result }: { result: QueryResult }) {
  const columns = columnsFor(result);
  const noDimensions = (result.spec.dimensions ?? []).length === 0;
  const scope = `${result.population.toLocaleString()}건 · 그룹 ${result.total_groups.toLocaleString()}개 · ${
    QUERY_SOURCE_LABEL[result.source] ?? result.source
  }`;
  return (
    <div>
      <p className="pb-2 text-[11.5px] text-(--ink-soft)">
        <b className="font-semibold text-(--ink)">{scope}</b>
      </p>
      <div className="panel-scroll overflow-x-auto">
        <table data-component="saved_question_table" className="w-full border-collapse text-[12.5px]">
          <thead>
            <tr className="text-left text-[11.5px] font-semibold text-(--ink-soft)">
              {columns.map((column) => (
                <th key={column.key} className={column.kind === "dimension" ? "py-1.5 pr-3" : "px-2 py-1.5 text-right"}>
                  {column.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.map((row, index) => (
              <tr key={rowKey(row, index)} className="border-t border-(--line)">
                {columns.map((column) =>
                  column.kind === "dimension" ? (
                    <td key={column.key} className="py-1.5 pr-3 font-mono text-(--ink)">
                      {noDimensions ? "전체" : (row.keys[column.key] ?? "—")}
                    </td>
                  ) : (
                    <td key={column.key} className={`px-2 py-1.5 text-right tabular-nums ${cellTone(row.measures[column.key], column.kind)}`}>
                      {cellText(row.measures[column.key], column.kind, column.key)}
                    </td>
                  ),
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {result.rows.length === 0 ? <p className="pt-2 text-[12px] text-(--ink-soft)">조건에 맞는 그룹이 없습니다.</p> : null}
    </div>
  );
}
