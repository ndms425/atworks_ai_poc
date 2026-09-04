---
name: schedule-run
description: Running or scheduling a set of APIs — a selection by date, group, or text, a target environment, an optional daily schedule, and a report — as a staged job for approval. / API 묶음을 지금 또는 매일 정해진 시각에 실행하고 리포트를 남기는 실행 계획.
---

# Schedule a run

Every run is a staged job. Nothing runs until the operator approves it on the Jobs page.

## Resolve the selection
- Turn the operator's words into a `search_apis` call: "지난 1주일 업데이트" → `updated_after` seven days before now; a group name → `group`; otherwise `query`. Keep the call's arguments as `select_where`.
- The ids in the result are the only ids the job may carry. If the result is empty, say so and stop.

## Fill the slots — never silently
Four slots are usually missing from speech: `target_env`, the first run date, `binding`, and whether a report is wanted. When two or more are missing, ask once with `present_question_form` (id `job-slots`, ≤4 questions, every one with a `default` and a `why`), then end the turn. When one is missing, default it, set its `confidence` below 0.5, and name it in `assumptions`.
- `target_env`: default `dev`, confidence 0.3. Never `prod`.
- Schedule start: "오늘부터" when today's time has passed → tomorrow, confidence 0.4, assumption "09:00 has passed today; starting tomorrow".
- `binding`: FROZEN unless the operator says the selection should be re-evaluated each run.
- `report`: true.

## Stage
- `stage_job` with `kind: scheduled_run` when a schedule is present, else `run_now`; `summary` in the operator's language. The call shows the preview card.
- Then one sentence: where approval happens, plus the chips (adjust the schedule, change the target, discard).

## After approval
- The operator approves on the Jobs page; you do not call `apply_job` unless the operator asks you to apply a job they already approved there.
