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
}
