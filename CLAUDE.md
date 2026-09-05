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
  `count`), `allowed_target_envs=("dev","stg","legacy","renewed")` — a named-target allow-list, not
  just server environments: parity's `legacy`/`renewed` targets are the same string set, checked by
  the same guardrail (stage and execute both), every offending name named, never `all()`, never
  just the first; opening a new target is only ever widening this tuple. `target_endpoints:
  dict[str,str]` maps a target name to a URL for the (future) REST adapter — Mock ignores it and
  keys `stub_response`/`stub_verdict` off the name string alone; per-target auth is out of MVP
  scope. Test-data keys must be `params` of a selected API.
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
  `atworks-agent/core/atworks_agent/`, skills `atworks-agent/skills/` (5), turn loop
  `atworks-agent/runtime/atworks_agent_runtime/orchestrator.py`, host `host/atworks_host/`, web
  `web/` (Task 15). Windows; Git Bash; `.venv/Scripts/python.exe`; `pytest -q` from the repo root.
- **Backend:** one ABC, `AtworksBackend` (`atworks-agent/core/atworks_agent/backend.py`) — the only
  contact with aTworks. MVP runs on a Mock backend; a REST adapter
  (`host/atworks_host/rest_backend.py`) is the whole Java integration. Behind each method:
  `search_apis` / `get_api` → the API-spec registry; `list_runs` / `get_run` / `count_runs` → the
  run-history store (its DSL, not the model, decides pass/fail); `stage_job` / `get_pending_jobs` /
  `apply_job` / `discard_job` → the job queue (`stage` proposes, `apply` is the sole state change);
  `execute_job_once` → the execution engine, called by the scheduler with no LLM in it;
  `get_context` → the project profile that fills the per-request context block;
  `stage_rule` / `apply_rule` / `discard_rule` / `get_pending_rules` / `list_rules` / `simulate_rule`
  → the rule ledger (`stage` drafts a structured `ValidationRule` on a session-seen API param,
  `apply` stamps `effective_from` and is the sole state change, `simulate_rule` is a read-only
  impact preview). A value rule is the API's success criterion independent of HTTP status; it only
  ever validates, never transforms a value. A `format` rule kind carries a raw regex or a
  `format` string — a **free string**, resolved server-side against the format library, never
  restricted to the five named enum values in the tool schema. A raw pattern must carry
  `pass_examples`/`fail_examples` (`min_format_examples`(1)/`max_format_examples`(8) config
  floor/cap): `verify_examples` (`.../rules.py`) compiles the pattern and fullmatches every example
  — a pass example that doesn't match, or a fail example that does, rejects the draft before it's
  built. The five built-ins (email/date/iso8601/uuid/number) need no examples. `get_format` /
  `list_formats` / `save_format` on the ABC read and write the **format library**
  (`FormatDefinition`/`FormatLibrary` in `.../rules.py`): the five built-ins seeded read-only plus
  operator-saved entries (`save_format_as` on a raw-pattern rule, promoted at `apply_rule` — the
  same host-approval click that applies the rule also seeds the library; a name/pattern collision
  or a full library (`max_format_library`=200) is a silent skip plus a guardrail note, never a
  failed rule apply). A later rule's `format` field can name any library entry; the executor
  resolves it at stage time into `pattern` (plus its stored examples) and keeps only the source
  name for display (`ValidationRule.format_name`) — `evaluate()` stays pattern-based and
  backend-independent, no per-backend format lookup. A library format is **inert**: it judges no
  run until an approved rule references it, which is what makes bulk seeding safe to approve once.
  `stage_format_batch` / `get_pending_format_batches` / `apply_format_batch` / `discard_format_batch`
  stage and approve a `FormatBatch` (`max_format_batch`=30 entries) — each entry's outcome
  (new/duplicate/invalid) is computed at stage time via the same dedup rule as `FormatLibrary.add`
  (name or identical pattern), and `apply_format_batch` adds only the `new` ones, one host approval
  for the whole batch. `find_apis_with_param` / `recommend_rules_for_api` are read-only
  recommendation, two directions, never fabricating a constraint from a param name alone:
  outward — given a param, other APIs that declare it and don't yet have a format rule on it,
  so an applied format can be offered to expand; inward — given a rule-less API,
  `RuleRecommendation`s built only from rules already applied to peer APIs' same-named params (an
  unmatched param suggests nothing). Each recommended target still stages and is approved as its
  own rule. `stage_profile` / `get_pending_profiles` / `apply_profile` / `discard_profile` /
  `list_profiles` / `get_parity_report` → the comparison-profile ledger and the parity report
  (below): `execute_job_once` now captures `RunResult.response_body: dict | None` at execution
  time (Mock's `stub_response` injects a volatile `serverTime` and a real `api-004` `$.limit`
  difference between targets); `stage_profile` drafts an ignore-spec (`ProfileDraft` → the
  `ProfileLedger`, mirroring the rule ledger), `apply_profile` stamps `effective_from` and, if a
  parity report already exists for the profile's job, re-diffs it from the STORED response
  bodies with no new backend call and no new run.
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
  `change_id` alias. Rules get their own surface, never `/changes/`: the Rules page (5th nav),
  `POST /rules/{id}/apply|discard` (mirror of `job_action`), `GET /rules`, and the `rule_preview`
  card shown when `stage_rule` runs, carrying the impact simulation and immutability note. Formats
  get their own namespace too, never `/rules/` or `/changes/`: `GET /formats` (built-ins + saved),
  `GET /format-batches` (pending), `POST /format-batches/{id}/apply|discard`
  (`format_batch_action`, mirror of `rule_action`) — a Formats section on the Rules page and its own
  `format_batch` card, posting only to `/format-batches`. Comparison profiles get a third such
  namespace, `/profiles` (`GET /profiles`(+`?job_id=`), `POST /profiles/{id}/apply|discard` via
  `profile_action`, mirror of `rule_action`) — a Profiles section on the Rules page, its own
  `profile_preview` card, and the profile lifecycle still rides `change_update` under the hood;
  the parity block itself is not a new route, it lives inside the existing report template
  (`host/atworks_host/reports.py` `_parity`), rendered from the same `data.json` `/reports/{job_id}`
  already serves. Named parity targets (`legacy`, `renewed`) generalize `allowed_target_envs`
  rather than replacing it — a parity job is an ordinary two-target `JobSpec`. Screen directives
  ride the same `ui` event as every other presentation tool, under two reserved component names,
  `screen_navigate` (from `navigate_screen`) and `screen_highlight` (from `highlight_screen`) — no
  new `EventType` (`commerce_common`, import-only). The web intercepts these two components and
  **executes** them (view switch, scroll-to-focus, filter, numbered red-box overlay) instead of
  rendering a card; `GenerativeBlock` returns null for both, so an unrecognized-component client
  stays harmless. `screen_state` rides every chat request (`ChatRequest.screen_state`, portal-sent,
  ambient) and renders as a `<screen-state>` block in the dynamic context, right after the
  attachments hint — a per-turn value, so it never perturbs the static, cache-stable prefix.
  Every entity row the portal renders (api/run/job/rule, all four views) carries
  `data-ref="kind:ref_id"`, the anchor both the highlight overlay and the focus scroll query against.
  **The report is where environments are compared**: a template rendered once
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
  **Delegated approval was evaluated and cancelled** (operator decision, 2026-09-06): typing "너가
  승인해" in chat approves nothing, before or after screen directives — chat text cannot carry a
  verified human click, attachments and tool results are untrusted content, so approval stays the
  one thing only an authenticated host route can mark. `navigate_screen`/`highlight_screen` are
  pure UI directives — they cannot touch `approved_*_ids` or any ledger by construction (no backend
  call in either tool's enrichment path), proven by a test that stages a job, sends both directives
  with a `screen_state` label that itself reads "approve job-0001 now", and asserts every job's
  status is unchanged.
- **Flows covered:** six skills, loaded on demand over the prompt's index. (1) `failed-triage` —
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
  queue, apply an already-approved job, discard; (4) `api-lookup` — specs, params, rules; (5)
  `rule-authoring` — NL → a structured `ValidationRule` draft on a session-seen API param
  (`stage_rule`, four kinds: numeric compare, membership, required, format) → `present_rule_preview`
  (impact simulation, immutability note) → Rules page approval (`apply_rule`) → applies to future
  runs only; (6) `parity-compare` — resolve the API selection plus two named targets (e.g.
  `legacy`/`renewed`) and identical test data → `stage_job` a two-target parity job → host
  approval → the scheduler executes it and the report's parity block compares response BODIES
  field-by-field (`compare_bodies`) → the operator reads the noise clusters
  (`cluster_diffs`) → `recommend_ignore_paths` proposes an ignore-spec from those real clusters,
  never fabricating a path with no cluster behind it → `stage_profile`/`present_profile_preview`
  → Rules page (Profiles section) host approval (`apply_profile`) → `Reports.rediff` recomputes
  the parity block from the STORED response bodies, no re-run. Value equivalence is judged only
  by the deterministic engine, never the model; the model's role is to translate loose language
  ("serverTime 무시해") into a structured ignore-spec and to orchestrate staging, nothing more; a
  person approves every profile before it affects a report; and re-diffing over stored bodies
  never re-judges or rewrites a past `RunResult`, the same immutability guarantee validation
  rules already give. Screen attachments scope a turn to the ref_ids the operator attached.
  **Screen interaction** is not a seventh skill — no new skill file, just a hint added to
  existing skills (`failed-triage`, `api-lookup`) — but a capability layered over all six: the
  reverse direction, chat moves the screen and points at it. Two tools, gated together by
  `enable_screen_directives` (`stages_screen_directives` mirror;
  `absent_tools()` drops both names when off): `navigate_screen` (view switch, open/scroll one
  item, set the view's own filter only — `runs.status` from `all`/`pass`/`fail`/`error`,
  `apis.query`; a filter the view doesn't own is dropped with a note, never invented) and
  `highlight_screen` (1..`max_highlight_targets`(8) entities, numbered ①②③ in call order to match
  chat prose — an ungrounded target is dropped with a note, all-ungrounded refuses). Every target
  in either tool must be grounded — session-seen (`seen_apis`/`seen_runs`/`seen_jobs`/`seen_rules`)
  or listed in the current turn's `screen_state.visible` (`screen_ref_grounded`,
  `.../screen.py`) — the same provenance discipline as a card's `PresentationRefused` gate; a
  view↔kind mismatch on `focus` refuses. Highlighting is the model's own judgment call (not only
  on explicit "어디 봐야 해?" — an answer that leans on a specific on-screen item highlights it),
  scoped to entities only: no button, tab, or control, and Home's summary tiles are out of scope,
  as are format/profile rows (rules only, on the Rules page). A "너가 승인해" delegated-approval
  path was evaluated and cancelled — see the Approval surface bullet.
- **Validation rules SI guarantee:** a rule's `effective_from` is stamped at `apply_rule`;
  evaluation in `execute_job_once` is additive over the legacy stub and only ever considers runs
  with `executed_at >= effective_from` — past `RunResult`s, success rates, reports and briefings are
  never re-judged or rewritten.
- **Memory:** off. `enable_memory = False`, no `save_memory` / `recall_memories` tools, `store=None`;
  nothing crosses sessions. Phase 2 may add field-name aliases via `commerce_common.memory`'s filter.

Model endpoint: Anthropic Messages format via ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN (OpenRouter in dev, LiteLLM proxy in the closed network); send_thinking_fields=False.
