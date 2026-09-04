# Multi-dimension Jobs — Design

Date: 2026-09-04. Status: approved by the controller on the operator's delegation ("그 섹션이 맞는지도 너가 판단해"). Supersedes the one-env / one-schedule / no-data shape of `JobSpec` from `docs/superpowers/plans/2026-09-03-atworks-ai-chat-mvp.md`.

## 0. Problem

A job today runs one set of APIs against **one** target environment on **at most one** schedule with **no** test data. The utterance "오늘 업데이트한 API를 개발서버와 이관서버에서 동일한 테스트 데이터로 수행하고 결과를 비교해줘" cannot be staged as one job; the assistant correctly answered that two jobs are needed, and nothing in the product compares the two. The operator's mental model is: **one API is run under several criteria** (which servers, on which schedules, with which data). A job should therefore be a **matrix**: `api_ids × target_envs × test_data`, executed once per schedule occurrence, approved with one click, reported with the environments side by side.

Decisions taken with the operator (2026-09-04): matrix job (not a job group, not an explicit cell list); schedules as a list, each with its own occurrence count; test data as inline key-value sets proposed by the model or entered through the question form; comparison shown **in the report only** (no chat comparison card in this iteration). Data model: dimension lists on `JobSpec` (approach A), not flattened cells.

Impact analysis: `.superpowers/sdd/multi-dim-jobs-impact.md` (45 assumption sites; the three riskiest are the env guardrail rewrite, the scheduler's single `runs_remaining` counter, and the silent growth of one approval click's scope).

## 1. Data model (`atworks_agent/types.py`, `jobs.py`)

### 1.1 New and changed types

```python
class TestDataSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.\-가-힣 ]+$")
    values: dict[str, str] = Field(min_length=1)     # key ≤ 60 chars, value ≤ 200 chars (validator)

class JobSchedule(BaseModel):                      # existing, extended
    kind: Literal["once", "daily"]
    at: str; tz: str = "Asia/Seoul"; from_date: str
    count: int = Field(ge=1, le=30)                # existing validators stay (once ⇒ count == 1, real time/date)
    done: int = Field(default=0, ge=0)             # NEW — occurrences already executed
```

`JobSpec` and `JobDraft`:

| Field | Before | After |
|---|---|---|
| `target_env: str` | one env | **removed** → `target_envs: list[str]` (`min_length=1`; each ≤ 32 chars; de-duplicated preserving order at stage) |
| `schedule: JobSchedule \| None` | at most one | **removed** → `schedules: list[JobSchedule]` (empty ⇒ `run_now`, one execution) |
| — | — | `test_data: list[TestDataSet]` (default empty ⇒ no binding, today's behaviour) |
| `runs_remaining: int \| None` | one counter | **removed** → derived: `remaining_executions` property |
| — | — | `executions: int = 0` (total executions performed; `run_now` completes at 1) |

Derived (properties on `JobSpec`, pure functions of stored fields):

- `matrix_size = len(api_ids) * len(target_envs) * max(1, len(test_data))`
- `total_executions = sum(s.count for s in schedules) if schedules else 1`
- `remaining_executions = total_executions - executions` (never below 0)
- `runs_total = matrix_size * total_executions`

`RunResult` gains `test_data_label: str | None = None`. `target_env` on a run stays scalar (a run is always one env).

Confidence keys the model may set (used by the preview card's highlighting): `target_envs`, `schedules`, `test_data`, `binding`, `api_ids`, `report`.

### 1.2 Guardrails (`check_job_guardrails`, run at stage and at apply, unchanged two-phase rule)

New `AtworksAgentConfig` fields (guardrail values, not prompt bytes):

| Field | Default | Meaning |
|---|---|---|
| `max_target_envs_per_job` | 2 | envs per job |
| `max_schedules_per_job` | 3 | schedule entries per job |
| `max_test_data_sets` | 5 | data sets per job |
| `max_matrix_size` | 400 | `matrix_size` cap (apis × envs × data) |
| `max_schedule_count` | 14 (existing) | now applies to `sum(s.count)` across schedules |

Rules, each producing one violation string in the existing style:

1. `bad = [e for e in target_envs if e not in allowed_target_envs]` → "target_envs prod are not allowed targets (dev, stg); the assistant may never target them" — every offending env listed; **never** `all()`.
2. `len(target_envs) > max_target_envs_per_job`, `len(schedules) > max_schedules_per_job`, `len(test_data) > max_test_data_sets` → count vs limit messages.
3. `matrix_size > max_matrix_size` → "job expands to N runs per execution (A APIs × E envs × D data sets) and the limit is 400; narrow the selection".
4. `sum(count) > max_schedule_count` → existing message generalized ("schedules total N runs").
5. `kind == run_now and schedules` → violation; `kind == scheduled_run and not schedules` → violation (generalizes today's two rules).
6. Two schedules equal on `(kind, at, tz, from_date)` → "duplicate schedule" violation (a repeated schedule is a mistake, unlike a repeated api id which is de-duplicated).
7. A `test_data` set whose keys match **no** `params` of any selected API → "test data set '<label>' binds parameters (x, y) that none of the selected APIs declares". The check needs the `ApiSpec` records: `check_job_guardrails(draft, config, apis: Mapping[str, ApiSpec] | None = None)`; the executor passes `state.seen_apis`, the ledger passes what its backend knows (the mock passes its catalogue), and `None` skips rule 7 (documented).
8. Existing rules stay: api count, empty selection, LATE needs `select_where`.

### 1.3 Ledger (`JobLedger`)

- `record_execution(job_id, run_ids, schedule_index: int | None)` — appends run ids, `executions += 1`, and when `schedule_index is not None` increments `schedules[schedule_index].done`. Still exactly one call per execution.
- `stage` initialises `executions = 0` and every `schedule.done = 0` (a draft may not carry `done`; the ledger resets it).
- Everything else (two-phase guardrails, `add_guardrail_note`, `discarded_by_kind`) unchanged.

## 2. Tool contract and executor (`tools/registry.py`, `executor.py`, `enrichment.py`)

### 2.1 `stage_job` schema (prompt bytes; a pure function of config)

- `target_envs`: `{"type": "array", "minItems": 1, "maxItems": config.max_target_envs_per_job, "items": {"type": "string"}, "description": "dev | stg, one or more. Never prod. '양쪽/두 계/비교' means dev and stg. When the operator did not say, default [dev] with confidence 0.3."}` — replaces `target_env`.
- `schedules`: `{"type": "array", "maxItems": config.max_schedules_per_job, "items": <existing schedule object>}` — replaces `schedule`; description: "Empty for run_now. Each entry runs the whole matrix once per occurrence."
- `test_data`: `{"type": "array", "maxItems": config.max_test_data_sets, "items": {"type": "object", "properties": {"label": {"type": "string", "maxLength": 40}, "values": {"type": "object", "additionalProperties": {"type": "string", "maxLength": 200}}}, "required": ["label", "values"], "additionalProperties": false}, "description": "Parameter bindings applied identically to every environment. Keys must be params of the selected APIs (see get_api). When the operator says '임의로' propose one set per scenario worth covering, label each, and put every invented value into assumptions with confidence 0.4 — the operator approves the values on the preview card."}`
- `required`: `["kind", "summary", "api_ids", "target_envs"]`.
- `confidence` description lists the new keys.
- Tool description adds one sentence: "One job may span several environments, schedules and test-data sets; it runs the whole matrix once per schedule occurrence."

`test_same_config_same_bytes` keeps holding because every cap comes from config.

### 2.2 Executor `_stage_job`

1. `api_ids` de-duplicated (existing) → provenance gate (existing).
2. `target_envs`: coerce to list, sanitize each to 32 chars, de-duplicate preserving order.
3. `schedules`: coerce list; each parsed by pydantic through `JobDraft`.
4. `test_data`: each item `parse_argument(TestDataSet, ...)`; keys and values sanitized (fence sanitizer) before validation; label sanitized.
5. `check_job_guardrails(draft, config, apis=state.seen_apis)` **before** the backend call (existing R18 pattern; rule 7 now has the api records).
6. Backend `stage_job` → `_remember_and_preview` (unchanged).

### 2.3 Preview enrichment (`enrich_job_preview`)

Adds a server-computed `matrix` block so the approval click is informed consent over the whole matrix:

```json
"matrix": {"apis": 4, "envs": ["dev", "stg"], "data_sets": ["S1 정상", "S2 음수 금액"], "executions": 3, "runs_per_execution": 16, "runs_total": 48}
```

`low_confidence` keys come from `job.confidence` as today (now `target_envs`, `schedules`, `test_data`, …). `apis` list unchanged.

## 3. Prompt and skills

### 3.1 `prompt.py` (static; prompt bytes change once)

In the job contract paragraph (only when `stages_jobs`):

- "A job may span several target environments, several schedules and several test-data sets; every schedule occurrence runs the whole api × env × data matrix. The preview card lists all of them; approval covers the whole matrix."
- "Test-data values are inputs the operator approves on the card; they are never facts about the system. Never invent a value the operator did not ask for without naming it in assumptions."
- Hard line unchanged: nothing runs from chat; never target prod.

### 3.2 `skills/schedule-run/SKILL.md`

- **Resolve the selection**: unchanged.
- **Environments**: "Which environments" is a slot. '개발' → dev, '이관' → stg, '양쪽/두 계/비교/동일하게' → `[dev, stg]`. Missing → question form, default dev.
- **Schedules**: a list. Each distinct time pattern is one entry; `sum(count)` must stay within the deployment's limit — when the operator asks for more, say so and propose a shorter plan.
- **Test data**: when the operator names values, bind them; when the operator says '임의로/아무 값', propose at most 3 sets from each selected API's `params` (one normal, one boundary, one invalid when the rules suggest one), label each in the operator's language, and list every invented value in `assumptions` with confidence 0.4. Never bind a key no selected API declares.
- **Compare**: "Comparison across environments appears in the job's report, one column per environment; say where the report will be and do not compare in prose."
- The `## Fill the slots` question-form rule stays: two or more missing slots → one `present_question_form` (id `job-slots`, ≤ 4 questions).

`job-approval/SKILL.md`: one sentence: "A job's card lists every environment, schedule and data set the approval covers; if the operator wants only part of it, discard and stage a narrower job."

## 4. Backend, scheduler, reports (`backend.py`, `mock_backend.py`, `scheduler.py`, `reports.py`, `report_template.html`)

### 4.1 Backend ABC

- `execute_job_once(session, job_id, schedule_index: int | None) -> list[RunResult]` — one execution of the matrix: for each `env in target_envs`, for each `data in (test_data or [None])`, for each `api in api_ids` produce one run (`target_env=env`, `test_data_label=data.label if data else None`); then call `record_execution(session, job_id, run_ids, schedule_index)` **once**. Returns `[]` without consuming when `remaining_executions == 0`. LATE re-resolution and the R14/R29 over-limit skip stay, applied once per execution (not per env).
- `record_execution(session, job_id, run_ids, schedule_index)` — signature gains `schedule_index`.
- Everything else unchanged (`get_job`, `applied_jobs`, `all_jobs`, `runs_by_ids`, `add_guardrail_note`).

### 4.2 Mock verdict (`stub_verdict(api, env, data)`)

Deterministic and documented in the module docstring:

- If `data` binds a key containing `amount`/`Amount` whose value parses as a number `< 0` → `FAIL`, `failed_rules=[f"{key} >= 0"]`, 200.
- Else if `"refund" in api.path and data is None` → `FAIL` on `refundAmount >= 0` (today's rule, keeps existing fixtures and tests meaningful).
- Else if `api.api_id == "api-007" and env == "stg"` → `ERROR`, 503 (so a dev/stg comparison shows a difference); on dev api-007 passes.
- Else `PASS`, 200.

### 4.3 Scheduler

- `due_at(schedule, index)` takes one `JobSchedule` and the occurrence index.
- `tick(now)`: for each applied job with `remaining_executions > 0`: if `run_now` and `executions == 0` → execute (`schedule_index=None`); else for each `(i, s)` with `s.done < s.count` and `due_at(s, s.done) <= now` → execute with `schedule_index=i` (several schedules may be due in one tick; each is its own execution). Lock, per-job try/except, report-failure split (R30/R35) unchanged. Never calls the model.

### 4.4 Reports (LLM 0회)

`data.json` adds:

```json
"matrix": {
  "envs": ["dev", "stg"],
  "rows": [
    {"api_id": "api-003", "test_data_label": "S2 음수 금액",
     "cells": {"dev": {"status": "fail", "run_id": "run-0041"}, "stg": {"status": "fail", "run_id": "run-0049"}},
     "differs": false}
  ],
  "differs_count": 1
}
```

Rows are built from the **latest** run per `(api_id, test_data_label, env)`; `differs = len({cell.status}) > 1`. `summary` keeps total/pass/fail/error and adds per-env counts `by_env`. The template renders, when `len(envs) > 1`, a comparison table (rows api × data, one column per env, mismatching rows highlighted, "차이 N건" tile) above the existing run list; the run list gains `env` and `data` columns. All values go through the existing `esc()`; `</` escaping unchanged.

### 4.5 Routes and portal reads

`/runs` unchanged (`RunResult` carries the new field). `job_record` emits the new fields; the old `target_env`/`schedule`/`runs_remaining` keys disappear (web ships in the same plan, no alias period).

## 5. Web (`web/atworks-web`)

- `lib/types.ts`: `JobSpec.target_envs: string[]`, `schedules: JobSchedule[]` (with `done`), `test_data: {label; values: Record<string,string>}[]`, `executions: number`; `JobPreviewPayload.matrix`; `RunResult.test_data_label?`.
- `JobPreviewCard`: rows 대상 계 (list), 스케줄 (one line each: "09-05부터 매일 09:00 × 3회", "09-08 1회"), 테스트 데이터 (n sets, each expandable `label: k=v, …`), 총 실행 (`runs_per_execution × executions = runs_total`), 리포트. Low-confidence dots on `target_envs`, `schedules`, `test_data`.
- `JobsView`: envs joined, progress `executions/total_executions`.
- `RunsView`: `데이터` column (`test_data_label`), env already shown.
- `QuestionFormCard`: unchanged.
- No `web-shared` change.

## 6. Tests and verification

Unit (core): guardrail rules 1–7 (each positive and negative; rule 1 with `prod` in the second position); matrix cap when each dimension is individually within limits; duplicate schedule; `record_execution` per-schedule `done`; `remaining_executions`; registry caps wired to config and `test_same_config_same_bytes`; executor stage with two envs and two data sets → preview `matrix` block; guardrail before a permissive backend; unknown data key rejected; `TestDataSet` validation.

Runtime: scripted `stage_job` with `target_envs: ["dev","stg"]` and one data set → `job_preview` event carries `matrix.envs == ["dev","stg"]`.

Host: mock produces `apis × envs × data` runs per execution with distinct `(target_env, test_data_label)`; two schedules on one job (`daily×3` from 09-05 and `once` on 09-08) executed independently, no double count; same-tick two due schedules → two executions; report `matrix.rows` with `differs` for api-007 dev/stg; template contains both env columns and the 차이 tile; approval click covers the matrix (preview payload enumerates envs/schedules/data that `apply_job` then acts on).

Live: `scripts/smoke_chat.py` gains turn 4 = the comparison utterance (expects `job_preview` or `question_form`); browser scenario ④: stage → approve on Jobs → tick → report shows the comparison table.

Review gates as in the first plan: `/review-commerce-agent` Steps 1-3 + code review after the core tasks and after host+web; final whole-branch review.

## 7. Out of scope

Partial approval of a matrix; a chat comparison card (`present_env_comparison`) and the `get_job_results` read tool; dataset identifiers / file upload (Phase 2 F1); parameter-value lookup (F2); per-env or per-data verdict rules in aTworks (backend concern).
