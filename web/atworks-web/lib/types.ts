// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

/** Mirrors atworks_agent/types.py and tools/presentation.py. */

/** Transport-level directive shape; defined in web-shared (merchant.ts can't import from here). */
export type { ScreenDirective } from "web-shared";

export type ScreenTargetKind = "api" | "run" | "job" | "rule";

export interface ScreenTarget {
  kind: ScreenTargetKind;
  ref_id: string;
  label?: string;
}

export interface ScreenFilter {
  status?: "all" | "pass" | "fail" | "error";
  query?: string;
}

export type PortalViewId = "home" | "apis" | "runs" | "jobs" | "rules";

/** The screen description sent as `screen_state` with every chat turn. */
export interface ScreenState {
  view: PortalViewId;
  focus?: ScreenTarget;
  filter?: ScreenFilter;
  visible: ScreenTarget[];
}

/** `screen_navigate` directive payload. */
export interface ScreenNavigatePayload {
  view: PortalViewId;
  focus?: { kind: ScreenTargetKind; ref_id: string };
  filter?: ScreenFilter;
  note?: string;
}

/** `screen_highlight` directive payload. */
export interface ScreenHighlightPayload {
  targets: { kind: ScreenTargetKind; ref_id: string; note?: string | null; number: number }[];
  headline?: string;
  note?: string;
}

/** A navigate directive turned into something a mounted view can apply once (tracked by `nonce`). */
export interface ScreenIntent {
  focus?: { kind: ScreenTargetKind; ref_id: string };
  filter?: ScreenFilter;
  nonce: number;
}

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
  pass_examples: string[];
  fail_examples: string[];
  format_name?: string | null;
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
  format_hint?: {
    label: string;
    example?: string | null;
    pattern?: string | null;
    pass_examples?: string[];
    fail_examples?: string[];
  };
  headline?: string;
  note?: string;
}

export interface FormatDefinition {
  name: string;
  pattern: string;
  pass_examples: string[];
  fail_examples: string[];
  builtin: boolean;
  created_at?: string | null;
  created_by?: string | null;
}

export type FormatBatchOutcome = "new" | "duplicate" | "invalid";

export interface FormatBatchEntry {
  name: string;
  pattern: string;
  pass_examples: string[];
  fail_examples: string[];
  outcome: FormatBatchOutcome;
  reason?: string | null;
}

export interface FormatBatch {
  batch_id: string;
  change_id: string;
  status: "staged" | "applied" | "discarded";
  summary?: string | null;
  entries: FormatBatchEntry[];
  created_at: string;
  created_by: string;
  created_by_kind: "operator" | "agent";
  applied_at?: string | null;
  applied_by?: string | null;
  discarded_at?: string | null;
  discarded_by?: string | null;
  discarded_by_kind?: "operator" | "agent" | null;
  new_count: number;
  duplicate_count: number;
  invalid_count: number;
}

export interface FormatBatchPayload {
  batch_id: string;
  change_id: string;
  batch: FormatBatch;
  change?: FormatBatch;
  entries: FormatBatchEntry[];
  new_count: number;
  duplicate_count: number;
  invalid_count: number;
  headline?: string;
  note?: string;
}

/** Mirrors atworks_agent.types.ComparisonProfile — serialization.profile_record adds change_id. */
export interface ComparisonProfile {
  profile_id: string;
  change_id: string;
  job_id: string;
  ignore_paths: string[];
  per_api_ignore: Record<string, string[]>;
  status: "staged" | "applied" | "discarded";
  summary: string;
  effective_from?: string | null;
  created_at: string;
  created_by: string;
  created_by_kind?: "operator" | "agent";
  applied_at?: string | null;
  applied_by?: string | null;
  discarded_at?: string | null;
  discarded_by?: string | null;
  discarded_by_kind?: "operator" | "agent" | null;
}

export type ParityVerdict = "equal" | "status_diff" | "value_diff";

export interface ParityRow {
  api_id: string;
  test_data_label?: string | null;
  verdict: ParityVerdict;
  diff_paths: string[];
  a_run_id?: string;
  b_run_id?: string;
}

/** Mirrors atworks_agent.parity.DiffCluster. */
export interface DiffCluster {
  paths: string[];
  count: number;
  row_keys: string[];
}

/** The report's `parity` block — mirrors host/atworks_host/reports.py `_parity`. */
export interface ParityBlock {
  targets: string[];
  rows: ParityRow[];
  clusters: DiffCluster[];
  value_diff_count: number;
  status_diff_count: number;
  ignore_paths: string[];
  per_api_ignore?: Record<string, string[]>;
}

export interface ParitySummaryPayload {
  job_id: string;
  title?: string;
  note?: string;
  parity: ParityBlock;
  clusters: DiffCluster[];
  value_diff_count: number;
  status_diff_count: number;
}

export interface ProfilePreviewPayload {
  profile_id: string;
  change_id: string;
  headline?: string;
  note?: string;
  profile: ComparisonProfile;
  change?: ComparisonProfile;
}

export type OperatorRole = "developer" | "qa" | "pm";

export const ROLE_KO: Record<OperatorRole, string> = {
  developer: "개발자",
  qa: "QA",
  pm: "PM",
};

export interface OperatorProfile {
  operator_id: string;
  name: string;
  role: OperatorRole;
}

export type InsightKind = "regression_suspect" | "flaky_cell" | "top_failed_rule" | "env_divergence" | "stale_pending";

export interface InsightCandidate {
  candidate_id: string;
  kind: InsightKind;
  label: string;
  figures: Record<string, number | string>;
  api_ids: string[];
  ref_ids: string[];
  priority: number;
}

export interface InsightNarrative {
  candidate_id: string;
  headline: string;
  why_it_matters: string;
  prompt: string;
}

export interface InsightItem {
  candidate: InsightCandidate;
  narrative?: InsightNarrative | null;
}

export interface InsightPanelData {
  operator_id: string;
  name: string;
  role: OperatorRole;
  scope_api_ids: string[];
  scope_fallback: boolean;
  window_days: number;
  generated_at: string;
  generated_by: "agent" | "deterministic";
  items: InsightItem[];
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
