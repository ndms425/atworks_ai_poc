# atworks-ai-mvp

An independent AI chat service beside aTworks (a Java-backend, Postman-class API test tool): the
model drafts reads and an execution plan; verdicts, execution and approval stay with deterministic
code and a person. `refs/` is a read-only reference checkout — never edit anything under it.
`commerce_common` (from `refs/commerce-agents/commerce-common/`) is import-only: no line of it is
copied, and the role package `atworks-agent/core/atworks_agent/` mirrors `merchant_agent` on top.

## Commerce agent decision record

- **Role:** merchant-side (operator-facing). The operator is a developer or QA engineer; the
  records are `ApiSpec` and `RunResult`; the staged write is a `JobSpec` execution plan.
- **Staged-write shape: a matrix.** One `JobSpec` runs `api_ids × target_envs × test_data` once per
  schedule occurrence (`docs/superpowers/specs/2026-09-04-multi-dimension-jobs-design.md`):
  `target_envs`, `schedules` (empty ⇒ once), `test_data`. `JobLedger` owns `executions` and each
  schedule's `done`; `matrix_size` / `total_executions` / `remaining_executions` / `runs_total` are
  derived. Every cap is an `AtworksAgentConfig` field checked inside `check_job_guardrails`
  (`.../jobs.py`), so stage and apply both get it, and is the schema's `maxItems`, so tool bytes
  stay a pure function of config: apis 100, envs 2, schedules 3, test-data sets 5,
  `max_matrix_size=400` (apis × envs × data), `max_schedule_count=14` (the **sum** of every
  `count`), `allowed_target_envs=("dev","stg")` — every offending env named, never `all()`, never
  just the first. Test-data keys must be `params` of a selected API.
- **Execution contract.** One `execute_job_once` call = **one execution per schedule occurrence**.
  The scheduler (`host/atworks_host/scheduler.py`; no LLM on this path) emits one slot per schedule
  whose `done < count` and whose `due_at` has passed — two schedules advance independently, two due
  in one tick run twice — and the backend runs `envs × data × apis` in one call, calling
  `record_execution(job_id, run_ids, schedule_index)` exactly once whatever the outcome so no slot
  re-runs. Size caps are **re-derived at execution** by `enforce_execution_matrix` after any LATE
  re-resolution (over the limit = a guardrail note, no runs, a spent slot); the ABC docstring
  states both obligations for REST. `Scheduler.tick` also generates the daily briefing at its tail
  (`briefings.maybe_generate(now)`, LLM-free, idempotent per date — file existence is the guard).
- **Language / path / shell:** Python 3.11+, pydantic v2, FastAPI + SSE. Role package
  `atworks-agent/core/atworks_agent/`, skills `atworks-agent/skills/` (4), turn loop
  `atworks-agent/runtime/atworks_agent_runtime/orchestrator.py`, host `host/atworks_host/`, web
  `web/` (Task 15). Windows; Git Bash; `.venv/Scripts/python.exe`; `pytest -q` from the repo root.
- **Backend:** one ABC, `AtworksBackend` (`atworks-agent/core/atworks_agent/backend.py`) — the only
  contact with aTworks. MVP runs on a Mock backend; a REST adapter
  (`host/atworks_host/rest_backend.py`) is the whole Java integration. Behind each method:
  `search_apis` / `get_api` → the API-spec registry; `list_runs` / `get_run` / `count_runs` → the
  run-history store (its DSL, not the model, decides pass/fail); `stage_job` / `get_pending_jobs` /
  `apply_job` / `discard_job` → the job queue (`stage` proposes, `apply` is the sole state change);
  `execute_job_once` → the execution engine, called by the scheduler with no LLM in it;
  `get_context` → the project profile that fills the per-request context block.
- **Identity and credentials:** auth mechanism is none in MVP (fixed `OPERATOR` constant; a
  production host derives the principal from its authentication). Backend calls carry server-side
  credentials the model never sees.
- **Sessions:** the principal is bound once at `POST /api/atworks/session` (`SessionStore.start`)
  onto `AtworksSessionContext` (`session_id`, `project_id`, `operator`) and read server-side after;
  later requests carry only `X-Session-Id` and no request shape names a user. Per-session state is
  `AtworksSessionState` (seen apis/runs/ranks/jobs, `last_population` + `last_listed_run_ids` +
  `last_listed_filter`, approval marks), saved in `host/atworks_host/sessions.py` — byte-identical
  to `refs/.../demo_common/sessions.py`: a versioned document under a compare-and-set plus an
  appended transcript, written at request end and again when a streamed turn ends (the turn wins
  the race). Actions taken outside the chat queue as `pending_app_events` for the next turn.
- **Marketplace posture:** none. Single-tenant internal tool, closed network, no third-party
  sellers, no `web_search`, no external content source. **Checkout handoff:** n/a (merchant role).
- **Surfaces and renderer modes:** the host serves the chat turn as SSE (`POST /api/atworks/chat`),
  the portal reads `/apis` `/runs` `/jobs`, the card buttons `/changes/{job_id}/apply|discard`
  (reference route names, so the web-shared hooks bind unchanged), plus `/scheduler/tick`
  (LLM-free) and `/reports/{job_id}`. Also `GET /runs/insights` (session; flaky/regression_suspect
  counts for the Home tile) and the briefing pair `GET /briefings/latest` (session, JSON) /
  `GET /briefings/{date}` (no session, HTML, `SAFE_DATE`-gated, same shape as `/reports/{job_id}`).
  The report's run rows link back with `?attach=run:{run_id}` (`ATWORKS_PORTAL_ORIGIN`, default
  `http://localhost:3110`); the portal reads that query param on mount into `pendingAttachments`
  and strips it from the URL. Components are presentation tools filled server-side:
  `run_digest`, `job_preview`, `question_form`, `run_groups`, chips; job lifecycle rides `change_update` with a
  `change_id` alias. **The report is where environments are compared**: a template rendered once
  over a `data.json` the scheduler refreshes, no LLM in the path — `summary.by_env` plus an
  api × data grid, one column per env from the latest run per cell with differing rows flagged,
  computed in `host/atworks_host/reports.py` from run records and escaped on the way out. Next.js
  portal (`web/atworks-web`, from `merchant-web`) is Task 15. No progressive/partial rendering and
  no eager input streaming (see the model endpoint line).
- **Approval surface / `require_host_approval`:** `require_host_approval = True`; the surface is
  **the Jobs page approve button**. `POST /api/atworks/changes/{job_id}/apply` → `job_action` in
  `host/atworks_host/app.py` (mirror of `change_action`, `demo_common/merchant.py:238-283`) is the
  only thing that marks `approved_job_ids`: the mark goes on immediately before the executor call
  and comes off immediately after, whatever the outcome, so no chat turn can spend it. `apply_job`
  is held without it; approval typed in chat approves nothing. Discards take the same path through
  `host_action_job_ids`, stamping `discarded_by_kind = OPERATOR`. `job_review_policy = "always"`.
  One click covers the **whole** matrix (no partial approval — stage a narrower job instead), so
  the card carries a server-computed `matrix` block — envs, data-set labels, executions, runs per
  execution, runs total — from `JobSpec` properties, never a model number. Rule layering: one-tool
  rules in `stage_job`'s description, the cross-tool contract in the prompt, procedures in skills.
- **Flows covered:** four skills, loaded on demand over the prompt's index. (1) `failed-triage` —
  `list_runs {filters:{status:non_pass}}` → `rank_failed_runs` (deterministic `risk_v1`, no
  analysis delegate) → `present_run_digest`, population and items bound to one list window; also
  `aggregate_runs` → `present_run_groups` (component `run_groups`) for grouping by cause
  (`group_by: failed_rule`, etc.), since-when for one API, and `flaky_v1` (≥`flaky_min_transitions`
  pass↔non-pass transitions) — counts, first-failure time and flakiness are host-computed, never
  restated by the model; (2) `schedule-run` — resolve the selection, fill or ask the missing list
  slots (environments, schedules, test data), `stage_job` → preview card → host approval →
  `apply_job`; the scheduler then executes the matrix and the env comparison appears in the report
  only, never as chat prose. `select_where` also takes `failed_since` and `related_to`, both
  server-resolved (`resolve_select_where`, same function at LATE re-resolution) — the model's api_id
  list is replaced by the resolved set, and `JobSpec.selection_basis` (a server-written sentence,
  never a model one) shows on the preview card and the Jobs row; (3) `job-approval` — pending
  queue, apply an already-approved job, discard; (4) `api-lookup` — specs, params, rules. Screen
  attachments scope a turn to the ref_ids the operator attached.
- **Memory:** off. `enable_memory = False`, no `save_memory` / `recall_memories` tools, `store=None`;
  nothing crosses sessions. Phase 2 may add field-name aliases via `commerce_common.memory`'s filter.

Model endpoint: Anthropic Messages format via ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN (OpenRouter in dev, LiteLLM proxy in the closed network); send_thinking_fields=False.
