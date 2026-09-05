---
name: parity-compare
description: Comparing two servers' API response VALUES (legacy vs renewed, or any two named targets) with identical test data, then proposing ignore paths to clear volatile-field noise (timestamps, request ids) from the real diffs. / 서로 다른 두 서버(예 legacy·renewed)의 API 응답 값을 동일한 테스트 데이터로 비교하고, 타임스탬프 같은 변동 필드 노이즈를 무시 경로로 정리한다.
---

# Compare values across two targets

A parity comparison never judges equivalence in chat. The comparison engine diffs stored response
bodies; you only read its clusters and propose which paths are noise. A person approves every
ignore-spec, and applying one re-diffs stored bodies — it never re-runs the APIs.

## Resolve the API set, the two targets, and identical data
- Turn the operator's words into a `search_apis` call the same way `schedule-run` does (date window, group, or text) — keep the arguments as `select_where`.
- The two named targets ("노후·신규", "legacy·renewed", "구버전·신버전") go in the job's `target_envs`, both entries, e.g. `[legacy, renewed]`. Confirm both are in this deployment's allowed target list; if only one is, say which is missing and stop.
- Test data must be identical across both targets — that is what makes the diff meaningful. Bind explicit values the operator names, or propose at most 3 sets the same way `schedule-run` does when the operator says '임의로', and list every invented value in `assumptions`.

## Stage the parity run
- `stage_job` with `target_envs` holding both targets and the same `test_data` for both; `summary` names the comparison in the operator's language. The preview card lists both targets, the api count, and the total runs — the operator approves the whole matrix.
- The operator approves on the Jobs page; the scheduler executes the matrix, and the parity report is written once the runs land — never claim the comparison is done before that.

## Read the noise
- `recommend_ignore_paths` with the job_id reads the parity report's diff clusters, ranked biggest-first — every path and count is what the report already computed; never invent a path it did not surface.
- `present_parity_summary` shows the clusters plus the value/status mismatch counts. Introduce it with one sentence; do not restate any count or path in prose.

## Propose an ignore-spec
- For the biggest noise clusters (a timestamp field, a request id, anything the operator agrees is not a real difference), `stage_profile` with `job_id`, `ignore_paths` (the shared $-rooted paths), optional `per_api_ignore` for paths scoped to one API, and a `summary` naming what noise this clears.
- `present_profile_preview` shows the staged ignore-spec. A person approves it on the Jobs/Profiles page — do not call `apply_profile` unprompted.
- `apply_profile` re-diffs the job's stored response bodies with the approved ignore paths; it makes no new runs. The real diffs — the ones not covered by an ignore path — remain and are never suppressed by a profile that hasn't been approved.

## Hard rules to restate
- You never judge value equivalence; the comparison engine does, from stored bodies.
- You never re-run APIs to clear noise; applying a profile only re-diffs what is already stored.
- Every ignore-spec is staged, previewed, and approved by a person before it changes anything.
- A profile's `effective_from` is stamped at apply; past comparison results are never re-judged or rewritten.
