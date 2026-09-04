---
name: failed-triage
description: Which non-pass runs to look at first — recent failures or errors, "risky" ones, what went wrong on a run the operator points at. Not needed for a single run the operator names by id. / 최근 실패·에러 실행 중 먼저 볼 것, 특정 실행이 왜 실패했는지.
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
- `present_run_digest`: one entry per ranked run, `kind` from the run's status, `headline` from the failed rule or HTTP status, `why_it_matters` from the scorer's reasons. The card shows the population; your one sentence before it states it too ("실패 47건 중 먼저 볼 8건").
- Close with a `note` entry when the population exceeds what is shown, offering to expand.
- Chips: open the next item, re-run one API as a job, show the full list.

## Hand-offs
- A re-run request goes to schedule-run. A question about what a rule means goes to api-lookup.
