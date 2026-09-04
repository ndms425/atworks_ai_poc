// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

/** How each kind of aTworks record shows: its label, icon, and tone. */

import type { KindStyle, Tone } from "web-shared";
import type { GroupBy, PopulationFilter, RunStatus } from "./types";

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
