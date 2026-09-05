// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

/** Mirrors atworks_agent/types.py and tools/presentation.py. */

export interface ApiSpec {
  api_id: string;
  method: string;
  path: string;
  name: string;
  group?: string;
  updated_at: string;
  has_rules: boolean;
  params?: string[];
}

export type RunStatus = "pass" | "fail" | "error";

export interface RunResult {
  run_id: string;
  api_id: string;
  executed_at: string;
  target_env: string;
  test_data_label?: string | null;
  status: RunStatus;
  failed_rules?: string[];
  http_status?: number;
  duration_ms?: number;
  job_id?: string;
}

export interface FailedRank {
  run_id: string;
  api_id: string;
  scorer: string;
  score: number;
  reasons?: string[];
}

export interface JobSchedule {
  kind: "once" | "daily";
  at: string;
  tz: string;
  from_date: string;
  count: number;
  done: number;
}

export interface TestDataSet {
  label: string;
  values: Record<string, string>;
}

export interface JobSpec {
  job_id: string;
  change_id: string;
  kind: "run_now" | "scheduled_run";
  status: "staged" | "applied" | "discarded";
  summary: string;
  api_ids: string[];
  target_envs: string[];
  schedules: JobSchedule[];
  test_data: TestDataSet[];
  binding: "FROZEN" | "LATE";
  report: boolean;
  confidence?: Record<string, number>;
  assumptions?: string[];
  guardrail_notes?: string[];
  selection_basis?: string | null;
  created_at: string;
  created_by: string;
  created_by_kind: "operator" | "agent";
  applied_at?: string | null;
  applied_by?: string | null;
  discarded_by?: string | null;
  discarded_by_kind?: "operator" | "agent" | null;
  run_ids?: string[];
  executions: number;
  /** Server-derived (job_record, ruling M17) — read, never recompute client-side. */
  matrix_size: number;
  total_executions: number;
  remaining_executions: number;
  runs_total: number;
}

export interface DigestEntry {
  kind: "fail" | "error" | "pending_job" | "note";
  ref_id?: string;
  headline: string;
  why_it_matters?: string;
  run?: RunResult;
  api?: ApiSpec;
  rank?: FailedRank;
  job?: JobSpec;
}

/** How `list_runs` last filtered the population this digest was drawn from. */
export type PopulationFilter = "all" | "pass" | "fail" | "error" | "non_pass" | "attached";

export interface RunDigestPayload {
  title?: string;
  population: number;
  population_filter: PopulationFilter;
  shown: number;
  scorer?: string | null;
  items: DigestEntry[];
}

export interface JobMatrix {
  apis: number;
  envs: string[];
  data_sets: string[];
  executions: number;
  runs_per_execution: number;
  runs_total: number;
  /** The real per-execution/total ceiling: config.max_matrix_size for LATE (re-evaluated at
   *  every execution, so the staged runs_per_execution is only a lower bound), else equal to
   *  runs_per_execution/runs_total. Always read this for LATE — never job.matrix_size. */
  max_runs_per_execution: number;
  max_runs_total: number;
}

export interface JobPreviewPayload {
  job_id: string;
  change_id: string;
  headline?: string;
  note?: string;
  job: JobSpec;
  change?: JobSpec;
  low_confidence: string[];
  apis: ApiSpec[];
  matrix: JobMatrix;
}

export interface FormOption {
  label: string;
  value: string;
}

export interface FormQuestion {
  id: string;
  label: string;
  type: "radio" | "checkbox" | "select" | "text" | "date" | "time" | "number" | "switch";
  why: string;
  default?: string | string[];
  options?: FormOption[];
  placeholder?: string;
  confidence?: number;
  highlight: boolean;
}

export interface QuestionFormPayload {
  id: string;
  title: string;
  description?: string;
  questions: FormQuestion[];
}

export interface AttachedItem {
  order: number;
  kind: "run" | "api" | "job";
  ref_id: string;
  label: string;
  field?: string;
  actual?: string;
  expected?: string;
  comment?: string;
  details?: Record<string, string>;
}

export type GroupBy = "api" | "failed_rule" | "http_status" | "env" | "api_env_data";

export interface RunGroup {
  key: string;
  label: string;
  count: number;
  fail: number;
  error: number;
  passed: number;
  run_ids?: string[];
  first_non_pass_at?: string | null;
  last_pass_before?: string | null;
  latest_status?: RunStatus | null;
  transitions: number;
  flaky: boolean;
  p95_duration_ms?: number | null;
  regression_suspect: boolean;
  api_updated_at?: string | null;
}

export interface RunGroupsPayload {
  title?: string | null;
  note?: string | null;
  group_by: GroupBy;
  population: number;
  population_filter: PopulationFilter;
  since?: string | null;
  shown: number;
  items: RunGroup[];
}

export type RuleKind = "compare" | "membership" | "required" | "format";

export interface ValidationRule {
  rule_id: string;
  api_id: string;
  param: string;
  kind: RuleKind;
  op?: string | null;
  value?: string | null;
  values: string[];
  format?: string | null;
  pattern?: string | null;
  review_required: boolean;
  message: string;
  status: "staged" | "applied" | "discarded";
  change_id: string;
  effective_from?: string | null;
  confidence?: Record<string, number>;
  assumptions?: string[];
  created_at: string;
  created_by: string;
  created_by_kind: "operator" | "agent";
  applied_at?: string | null;
  applied_by?: string | null;
  discarded_at?: string | null;
  discarded_by?: string | null;
  discarded_by_kind?: "operator" | "agent" | null;
}

/** simulate_rule의 읽기 전용 결과 — 아무것도 저장하지 않는다. */
export interface RuleImpact {
  window_runs: number;
  known_inputs: number;
  would_fail: number;
  excluded_unknown: number;
}

export interface RulePreviewPayload {
  rule_id: string;
  change_id: string;
  rule: ValidationRule;
  change?: ValidationRule;
  api?: ApiSpec;
  review_required: boolean;
  low_confidence: string[];
  impact?: RuleImpact;
  format_hint?: { label: string; example?: string | null; pattern?: string | null };
  headline?: string;
  note?: string;
}

export interface Briefing {
  date: string;
  generated_at: string;
  window: { from: string; to: string };
  counts: { total: number; pass: number; fail: number; error: number };
  top_groups: RunGroup[];
  insights: { flaky: number; regression_suspect: number };
  jobs: { executed: { job_id: string; summary: string; runs: number }[]; pending: { job_id: string; summary: string; created_at: string }[]; stale_pending: number };
}
