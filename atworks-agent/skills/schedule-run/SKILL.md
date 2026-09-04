---
name: schedule-run
description: Running or scheduling a set of APIs — a selection by date, group, or text, one or more target environments, one or more schedules, optional test data, and a report — as one staged job for approval. / API 묶음을 하나 이상의 대상 계에서, 지금 또는 정해진 시각마다, 정해진 테스트 데이터로 실행하고 리포트를 남기는 실행 계획.
---

# Schedule a run

Every run is a staged job. Nothing runs until the operator approves it on the Jobs page. One job is a
matrix: each schedule occurrence runs every selected API against every target environment with every
test-data set. Approval covers the whole matrix.

## Resolve the selection
- Turn the operator's words into a `search_apis` call: "지난 1주일 업데이트" → `updated_after` seven days before now; "오늘 업데이트한" → `updated_after` today 00:00; a group name → `group`; otherwise `query`. Keep the call's arguments as `select_where`.
- The ids in the result are the only ids the job may carry. If the result is empty, say so and stop.
- "실패한 것만 다시 돌려 / 어제 실패한 API 재실행" → `select_where.failed_since` (the operator's window; 24 hours ago when none). Send the api_ids you know, but the host resolves the real list and shows its basis on the card.
- "X 고쳤는데 뭘 다시 돌려야 해 / X 관련 전부" → `search_apis` for X, then `select_where.related_to: <that api_id>`. The host picks the same group and the same leading path; the card's "선택 근거" row says so. Do not hand-pick related APIs yourself.

## Environments — a list
- "어느 계에서" is a slot with a list value. '개발' → `dev`; '이관' → `stg`; '양쪽 / 두 계 / 비교 / 동일하게' → `[dev, stg]`.
- Missing from the utterance → ask on the question form, default `[dev]` with `confidence` 0.3 and an assumption naming it. Never `prod`.

## Schedules — a list
- Each distinct time pattern is one entry in `schedules`. "매일 09시 3일간" is one daily entry with `count: 3`; "그리고 다음 주 월요일 한 번 더" is a second `once` entry.
- Empty `schedules` means `run_now`. Two entries that agree on kind, time, timezone and start date are a mistake, not a repetition — merge them by raising `count`.
- `sum(count)` across all entries must stay within this deployment's limit. When the operator asks for more, say the limit held and propose a shorter plan instead of staging something that will be blocked.

## Test data — a list
- When the operator names values, bind them: one `test_data` entry, `label` in the operator's language, `values` keyed by the parameter names.
- When the operator says '임의로 / 아무 값 / 적당히', propose at most 3 sets drawn from the selected APIs' `params` (one normal, one boundary, and one invalid when the rules suggest one). Label each, and list **every** invented value in `assumptions` with `confidence` 0.4 — the operator approves the values on the card.
- Never bind a key that no selected API declares; call `get_api` when you are unsure which parameters exist.
- The same sets apply to every environment: that is what makes the comparison meaningful.

## Fill the slots — never silently
Slots usually missing from speech: `target_envs`, the schedules, `test_data`, `binding`, and whether a
report is wanted. When two or more are missing, ask once with `present_question_form` (id `job-slots`,
≤4 questions, every one with a `default` and a `why`), then end the turn. When one is missing, default
it, set its `confidence` below 0.5, and name it in `assumptions`.
- `target_envs`: default `[dev]`, confidence 0.3.
- Schedule start: "오늘부터" when today's time has passed → tomorrow, confidence 0.4, one assumption per adjusted schedule ("09:00 has passed today; starting tomorrow").
- `test_data`: default none (no binding), confidence 0.4 when the operator implied data but named none.
- `binding`: FROZEN unless the operator says the selection should be re-evaluated each run.
- `report`: true.

## Stage
- `stage_job` with `kind: scheduled_run` when `schedules` is non-empty, else `run_now`; `summary` in the operator's language. The call shows the preview card, which lists every environment, schedule and data set and the total run count.
- Then one sentence: where approval happens, plus the chips (adjust the schedule, change the environments, discard).

## Compare
- Comparison across environments appears in the job's report, one column per environment, with the differing rows highlighted. Say where the report will be; do not compare in prose and do not restate any status.

## After approval
- The operator approves on the Jobs page; you do not call `apply_job` unless the operator asks you to apply a job they already approved there.
