// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import { AgentApi } from "web-shared";
import type {
  ApiSpec,
  AskEntry,
  Briefing,
  ComparisonProfile,
  FormatBatch,
  FormatDefinition,
  GrowthSummary,
  HomeSummary,
  InsightPanelData,
  JobSpec,
  OperatorProfile,
  QueryResult,
  RunResult,
  SavedQuestion,
  ValidationRule,
  VocabularyEntry,
} from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8010";
export const api = new AgentApi(API_URL, "/api/atworks");
export const UNREACHABLE = `The aTworks AI host at ${API_URL} is not reachable. Start it with: python -m atworks_host.main`;

/**
 * How many rows one page of a list holds. The host caps every list route at 200 and defaults to
 * 50; this is the portal's own choice within that, and it is also the ceiling on how many rows a
 * view ever renders at once — the `total` in the envelope, not the array length, is what the
 * header reports.
 */
export const PAGE_SIZE = 50;

// Every list read below returns the paged envelope {items, next_cursor, total}. `cursor` is an
// opaque server string: a view stores what a page handed back and echoes it to ask for the next
// one — it never builds, parses or offsets one.
export const fetchApis = (query = "", cursor: string | null = null) =>
  api.getPage<ApiSpec>("/apis", { query, cursor, limit: PAGE_SIZE });
export const fetchRuns = (status?: string, cursor: string | null = null) =>
  api.getPage<RunResult>("/runs", { status, cursor, limit: PAGE_SIZE });
export const fetchJobs = (cursor: string | null = null) => api.getPage<JobSpec>("/jobs", { cursor, limit: PAGE_SIZE });
// `status` is a SERVER-side filter (host `/rules?status=`): the Rules page re-queries per
// segment instead of bucketing one page, so "적용됨" is the ledger's applied rules and its
// count is the envelope's `total`, not this page's share.
export const fetchRules = (status?: string, cursor: string | null = null) =>
  api.getPage<ValidationRule>("/rules", { status, cursor, limit: PAGE_SIZE });
// /changes/ 가 아니다 — rule_action은 job_action의 미러지만 별도 경로다.
export const actOnRule = (ruleId: string, action: "apply" | "discard") =>
  api.post<{ ok: boolean; change: ValidationRule | null }>(`/rules/${encodeURIComponent(ruleId)}/${action}`, {});
export const fetchFormats = () => api.get<{ formats: FormatDefinition[] }>("/formats");
export const fetchFormatBatches = () => api.get<{ format_batches: FormatBatch[] }>("/format-batches");
// /changes/ 가 아니다 — format_batch_action은 job_action의 미러지만 별도 경로다 (rule_action과 동일 패턴).
export const actOnFormatBatch = (batchId: string, action: "apply" | "discard") =>
  api.post<{ ok: boolean; change: FormatBatch | null }>(`/format-batches/${encodeURIComponent(batchId)}/${action}`, {});
export const fetchProfiles = (jobId?: string, cursor: string | null = null) =>
  api.getPage<ComparisonProfile>("/profiles", { job_id: jobId, cursor, limit: PAGE_SIZE });
// /changes/ 가 아니다 — profile_action은 rule_action/format_batch_action의 미러지만 별도 경로다.
export const actOnProfile = (profileId: string, action: "apply" | "discard") =>
  api.post<{ ok: boolean; change: ComparisonProfile | null }>(`/profiles/${encodeURIComponent(profileId)}/${action}`, {});
// /changes/ 도 /rules/ 도 아니다 — 어휘는 job 승인이 아니라서 자기 네임스페이스를 쓴다(spec §10).
// 확인/거부/삭제는 사람의 클릭만 닿는 경로이고, 서버는 감사 2행을 남긴다.
export const actOnVocabulary = (term: string, action: "confirm" | "reject" | "delete") =>
  api.post<{ ok: boolean; entry: VocabularyEntry }>(`/vocabulary/${encodeURIComponent(term)}/${action}`, {});
export const fetchVocabulary = (status?: string, cursor: string | null = null) =>
  api.getPage<VocabularyEntry>("/vocabulary", { status, cursor, limit: PAGE_SIZE });

// 질문 기록 (spec §6). 읽기 전용이고 `question`은 저장 시점에 이미 마스킹된 요약이라 화면은 그대로
// 보여 준다. `outcome`은 서버 필터라 세그먼트를 바꾸면 첫 페이지부터 다시 묻는다(/rules?status=와
// 같은 규칙) — 한 페이지를 클라이언트에서 쪼개면 3페이지의 unmet 한 줄이 그냥 안 보인다.
export const fetchAskLog = (outcome?: string, cursor: string | null = null) =>
  api.getPage<AskEntry>("/ask-log", { outcome, cursor, limit: PAGE_SIZE });

// 카드 푸터의 👍/👎 (spec §9). 같은 턴을 다시 투표하면 마지막 표만 남고, 감사 로그는 움직이지
// 않는다 — 표는 공유 상태의 변경이 아니다. 👍가 회귀 eval 케이스를 만드는 것은 서버 쪽 일이다.
export const sendFeedback = (turnId: string, vote: "up" | "down") =>
  api.post<{ ok: boolean; entry: { feedback: "up" | "down" | null }; case_path: string | null }>(
    "/feedback",
    { turn_id: turnId, vote },
  );

// /changes/ 도 /rules/ 도 아니다 — 저장 질문도 job 승인이 아니라서 자기 네임스페이스를 쓴다
// (spec §8). 목록은 서버가 `uses` 순으로 정렬해 보내고, 실행은 지금의 창으로 다시 계산한다.
export const fetchSavedQuestions = (status?: string, cursor: string | null = null, limit = PAGE_SIZE) =>
  api.getPage<SavedQuestion>("/saved-questions", { status, cursor, limit });
export const runSavedQuestion = (savedId: string) =>
  api.get<QueryResult>(`/saved-questions/${encodeURIComponent(savedId)}/run`);
export const actOnSavedQuestion = (savedId: string, action: "hide" | "unhide") =>
  api.post<{ ok: boolean; saved_question: SavedQuestion }>(`/saved-questions/${encodeURIComponent(savedId)}/${action}`, {});
/** 성장 요약 — 저장 질문 카드는 빈 상태 문구의 숫자(승격 문턱)만 여기서 읽는다. */
export const fetchGrowthSummary = (days = 7) => api.get<GrowthSummary>("/growth/summary", { days: String(days) });

/** Home's whole above-the-fold state in ONE call — counts, insight flags and the briefing header.
 * It replaced four parallel reads, two of which downloaded a run page only to count it. */
export const fetchHomeSummary = () => api.get<HomeSummary>("/home/summary");
export const fetchOperators = () => api.get<{ operators: OperatorProfile[] }>("/operators");
export const fetchInsightPanel = () => api.get<InsightPanelData>("/home/insights");
export const refreshInsightPanel = () => api.post<InsightPanelData>("/home/insights/refresh", {});
export const fetchBriefing = () => api.get<Briefing>("/briefings/latest");
export const reportUrl = (jobId: string) => `${API_URL}/api/atworks/reports/${encodeURIComponent(jobId)}`;
export const briefingUrl = (date: string) => `${API_URL}/api/atworks/briefings/${encodeURIComponent(date)}`;
