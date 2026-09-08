// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

/** How each kind of aTworks record shows: its label, icon, and tone. */

import type { KindStyle, Tone } from "web-shared";
import type { GroupBy, PopulationFilter, QueryDimension, QueryMeasure, QuerySource, RunStatus } from "./types";

export const RUN_STATUS: Record<RunStatus, { label: string; tone: Tone }> = {
  pass: { label: "pass", tone: "ok" },
  fail: { label: "fail", tone: "danger" },
  error: { label: "error", tone: "warn" },
};

// Only `.icon`/`.tone` are read (RunDigestCard); `KindStyle.label` is dropped here since
// nothing in this app consumes a digest-kind label.
export const DIGEST_KINDS: Record<"fail" | "error" | "pending_job" | "note", Omit<KindStyle, "label">> = {
  fail: { icon: "alert", tone: "danger" },
  error: { icon: "alert", tone: "warn" },
  pending_job: { icon: "clock", tone: "violet" },
  note: { icon: "message", tone: "muted" },
};

/** R20: the digest header scope, "<label> <population>건 중 먼저 볼 <shown>건". */
export const POPULATION_FILTER_LABEL: Record<PopulationFilter, string> = {
  all: "실행",
  pass: "성공",
  fail: "실패",
  error: "에러",
  non_pass: "실패·에러",
  attached: "첨부",
};

export const GROUP_BY_LABEL: Record<GroupBy, string> = {
  api: "API별",
  failed_rule: "원인(규칙)별",
  http_status: "HTTP 상태별",
  env: "환경별",
  api_env_data: "API×환경×데이터",
};

/**
 * query_table 카드의 라벨 — atworks_agent/catalog.py의 DIMENSIONS/MEASURES 라벨을 그대로 미러한다.
 * 카드는 서버가 보낸 column.label을 쓰므로 이 표는 폴백(모르는 클라이언트/구버전 payload)이자
 * 두 쪽 라벨이 어긋나면 눈에 띄게 하는 대조표다.
 */
export const QUERY_DIMENSION_LABEL: Record<QueryDimension, string> = {
  api: "API별",
  path_segment_1: "경로 1조각별",
  path_segment_2: "경로 2조각별",
  path_prefix_2: "경로 앞 2조각별",
  method: "HTTP 메서드별",
  api_group: "API 그룹별",
  target_env: "대상 환경별",
  test_data_label: "테스트 데이터별",
  failed_rule: "실패 규칙별",
  http_status: "HTTP 상태별",
  executed_by: "실행자별",
  day: "일별",
  week: "주별",
};

export const QUERY_MEASURE_LABEL: Record<QueryMeasure, string> = {
  runs: "실행 수",
  pass: "성공 수",
  fail: "실패 수",
  error: "에러 수",
  non_pass: "실패+에러 수",
  fail_rate: "실패율",
  apis: "API 수",
  transitions: "전환 수",
  p95_duration_ms: "p95 응답시간(ms)",
};

/** 어느 물질화 소스에서 읽었는지 — 카드 헤더의 출처 표시. */
export const QUERY_SOURCE_LABEL: Record<QuerySource, string> = {
  rollup_day: "일별 롤업",
  rollup_key_day: "키 축 롤업",
  rollup_operator_day: "실행자별 롤업",
  runs: "실행 기록",
};
