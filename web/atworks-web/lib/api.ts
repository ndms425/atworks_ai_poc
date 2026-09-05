// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import { AgentApi } from "web-shared";
import type { ApiSpec, Briefing, ComparisonProfile, FormatBatch, FormatDefinition, JobSpec, RunResult, ValidationRule } from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8010";
export const api = new AgentApi(API_URL, "/api/atworks");
export const UNREACHABLE = `The aTworks AI host at ${API_URL} is not reachable. Start it with: python -m atworks_host.main`;

export const fetchApis = (query = "") => api.get<{ apis: ApiSpec[] }>(`/apis?query=${encodeURIComponent(query)}`);
export const fetchRuns = (status?: string) => api.get<{ population: number; runs: RunResult[] }>(`/runs${status ? `?status=${status}` : ""}`);
export const fetchJobs = () => api.get<{ jobs: JobSpec[] }>("/jobs");
export const fetchRules = () => api.get<{ rules: ValidationRule[] }>("/rules");
// /changes/ 가 아니다 — rule_action은 job_action의 미러지만 별도 경로다.
export const actOnRule = (ruleId: string, action: "apply" | "discard") =>
  api.post<{ ok: boolean; change: ValidationRule | null }>(`/rules/${encodeURIComponent(ruleId)}/${action}`, {});
export const fetchFormats = () => api.get<{ formats: FormatDefinition[] }>("/formats");
export const fetchFormatBatches = () => api.get<{ format_batches: FormatBatch[] }>("/format-batches");
// /changes/ 가 아니다 — format_batch_action은 job_action의 미러지만 별도 경로다 (rule_action과 동일 패턴).
export const actOnFormatBatch = (batchId: string, action: "apply" | "discard") =>
  api.post<{ ok: boolean; change: FormatBatch | null }>(`/format-batches/${encodeURIComponent(batchId)}/${action}`, {});
export const fetchProfiles = (jobId?: string) =>
  api.get<{ profiles: ComparisonProfile[] }>(`/profiles${jobId ? `?job_id=${encodeURIComponent(jobId)}` : ""}`);
// /changes/ 가 아니다 — profile_action은 rule_action/format_batch_action의 미러지만 별도 경로다.
export const actOnProfile = (profileId: string, action: "apply" | "discard") =>
  api.post<{ ok: boolean; change: ComparisonProfile | null }>(`/profiles/${encodeURIComponent(profileId)}/${action}`, {});
export const fetchInsights = () => api.get<{ flaky: number; regression_suspect: number; window_days: number }>("/runs/insights");
export const fetchBriefing = () => api.get<Briefing>("/briefings/latest");
export const reportUrl = (jobId: string) => `${API_URL}/api/atworks/reports/${encodeURIComponent(jobId)}`;
export const briefingUrl = (date: string) => `${API_URL}/api/atworks/briefings/${encodeURIComponent(date)}`;
