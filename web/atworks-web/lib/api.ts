// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import { AgentApi } from "web-shared";
import type { ApiSpec, Briefing, JobSpec, RunResult } from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8010";
export const api = new AgentApi(API_URL, "/api/atworks");
export const UNREACHABLE = `The aTworks AI host at ${API_URL} is not reachable. Start it with: python -m atworks_host.main`;

export const fetchApis = (query = "") => api.get<{ apis: ApiSpec[] }>(`/apis?query=${encodeURIComponent(query)}`);
export const fetchRuns = (status?: string) => api.get<{ population: number; runs: RunResult[] }>(`/runs${status ? `?status=${status}` : ""}`);
export const fetchJobs = () => api.get<{ jobs: JobSpec[] }>("/jobs");
export const fetchInsights = () => api.get<{ flaky: number; regression_suspect: number; window_days: number }>("/runs/insights");
export const fetchBriefing = () => api.get<Briefing>("/briefings/latest");
export const reportUrl = (jobId: string) => `${API_URL}/api/atworks/reports/${encodeURIComponent(jobId)}`;
export const briefingUrl = (date: string) => `${API_URL}/api/atworks/briefings/${encodeURIComponent(date)}`;
