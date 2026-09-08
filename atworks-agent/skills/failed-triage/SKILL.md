---
name: failed-triage
description: Which non-pass runs to look at first — recent failures or errors, "risky" ones, what went wrong on a run the operator points at. Not needed for a single run the operator names by id. / 최근 실패·에러 실행 중 먼저 볼 것, 특정 실행이 왜 실패했는지, 실패를 원인별로 묶기, 어느 API가 언제부터 깨졌는지.
---

# Failed-run triage

You give the operator a reading order over runs that aTworks already judged non-pass. You never change a verdict.

## Gather
- `list_runs` with `filters: {status: non_pass}` over the window the operator named (`filters.since`); "최근" with no window means the last 24 hours. This records the population — fail and error together — that the digest is drawn from. Use `status: fail` or `status: error` only when the operator asks about one verdict alone.
- When the operator attached items (`<attached-result-items>`), the scope is those items only: `get_run` each one, and skip ranking.

## Rank
- `rank_failed_runs` with `scorer: risk_v1` unless the operator names another scorer. Never invent a scorer or reorder its output.
- Take the scorer's `reasons` as the only reasons you cite.
- Ranking covers only the runs the last `list_runs` returned; list again before ranking a different window.

## Present
- `present_run_digest`: one entry per ranked run, `kind` from the run's status, `headline` from the failed rule or HTTP status, `why_it_matters` from the scorer's reasons. The card carries the population; your sentence before it introduces the card without restating counts or verdicts.
- If the runs are on screen, `highlight_screen` the ones you named (①②③).
- Close with a `note` entry when the population exceeds what is shown, offering to expand.
- Chips: open the next item, re-run one API as a job, show the full list.

## Group by cause
- "원인별로 묶어줘 / 패턴 / cluster" → `aggregate_runs` with `group_by: failed_rule`, `status: non_pass`, `since` from the operator's window (default 7 days). Errors come back as `(error) HTTP <code>` groups.
- Then `present_run_groups` with the keys `aggregate_runs` returned, in its order. Introduce the card in one sentence; the counts stay on the card.

## Since when is it broken
- Resolve the API first (`search_apis`) when the operator names it by words; then `aggregate_runs` with `group_by: api` and that `api_id`.
- The one group carries `first_non_pass_at`, `last_pass_before`, `api_updated_at` and `regression_suspect`. Show it with `present_run_groups`; say in one sentence what the row shows (the card carries the timestamps) and, when `regression_suspect` is true, that the spec changed between the last pass and the first failure — a suspicion, not a verdict.

## Flaky APIs
- "왔다갔다 / 불안정 / flaky" → `aggregate_runs` with `group_by: api_env_data`; groups with `flaky: true` are the answer (flaky_v1: two or more pass↔non-pass transitions in the window). Show only those keys with `present_run_groups`.

## Beyond the fixed axes
- When the grouping or filter the operator asked for is outside `aggregate_runs`' five axes — by endpoint segment, by HTTP method, by API group, by week or by day, by operator, two axes at once, or this window against the one before it — call `query_runs` with that spec and show the result with `present_query_table`. `aggregate_runs` stays the tool for the axes it already covers.
- When the catalogue cannot express it either, call `note_unmet_ask(reason, summary, wanted)` FIRST, then say in one sentence what data or axis would make it answerable. An answer that ends on "지원하지 않습니다 / not supported" without `note_unmet_ask` is a rule violation.
- When you read one of the operator's own words as a filter value ("결제 계열" → a `path_prefix`), say so in one clause so they can correct it.

## Hand-offs
- A re-run request goes to schedule-run. A question about what a rule means goes to api-lookup.
