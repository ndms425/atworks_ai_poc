// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

/** How each kind of aTworks record shows: its label, icon, and tone. */

import type { KindStyle, Tone } from "web-shared";
import type { PopulationFilter, RunStatus } from "./types";

export const RUN_STATUS: Record<RunStatus, { label: string; tone: Tone }> = {
  pass: { label: "pass", tone: "ok" },
  fail: { label: "fail", tone: "danger" },
  error: { label: "error", tone: "warn" },
};

export const DIGEST_KINDS: Record<"fail" | "error" | "pending_job" | "note", KindStyle> = {
  fail: { label: "Rule failed", icon: "alert", tone: "danger" },
  error: { label: "Error", icon: "alert", tone: "warn" },
  pending_job: { label: "Awaiting approval", icon: "clock", tone: "violet" },
  note: { label: "Note", icon: "message", tone: "muted" },
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
