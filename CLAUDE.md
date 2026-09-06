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
  states both obligations for REST. Inside one call the matrix is walked in `max_concurrency`(4)
  batches with an `await` between batches (the target server sees at most that many cells at once),
  and the produced runs are handed to the store in `ingest_chunk_size`(50) chunks — two different
  dials, generation vs. write. The ingest is **failure-safe and partial**: a chunk that commits is
  committed, and `record_execution` is still called exactly once with whatever landed, so a crash
  mid-matrix spends the slot and never double-runs it. A `JobSpec` no longer accumulates every run
  id — `run_count` plus `recent_run_ids` (newest first, ≤50) replace the unbounded `run_ids` list,
  and a reader that needs a job's whole history queries `list_runs(job_id=…)` / `count_runs_by_job`.
  The scheduler iterates `active_jobs` (`remaining_executions > 0`) rather than the whole ledger.
  `Scheduler.tick` also generates the daily briefing at its tail (`briefings.maybe_generate(now)`,
  LLM-free, idempotent per date — file existence is the guard) and then runs the retention job
  (`retention.maybe_run(now)`, also LLM-free, once per local day).
- **Language / path / shell:** Python 3.11+, pydantic v2, FastAPI + SSE. Role package
  `atworks-agent/core/atworks_agent/`, skills `atworks-agent/skills/` (5), turn loop
  `atworks-agent/runtime/atworks_agent_runtime/orchestrator.py`, host `host/atworks_host/`, web
  `web/` (Task 15). Windows; Git Bash; `.venv/Scripts/python.exe`; `pytest -q` from the repo root.
- **Backend:** one ABC, `AtworksBackend` (`atworks-agent/core/atworks_agent/backend.py`) — the only
  contact with aTworks. MVP runs on a Mock backend — `MockAtworks` over a **SQLite `Store`**
  (`host/atworks_host/store.py`, `ATWORKS_STORE_PATH`, default `:memory:`), not dicts, so every
  read is real SQL over real indexes and the DDL doubles as the Java-side schema blueprint; a REST
  adapter (`host/atworks_host/rest_backend.py`) is the whole Java integration. Behind each method:
  `search_apis` / `get_api` / `get_apis(session, api_ids)` (the BATCH spec read — one call for a
  whole id list, used by `resolve_select_where`'s `failed_since` branch and by any path that must
  not fan out into N `get_api` awaits) → the API-spec registry; `list_runs` / `get_run` / `count_runs` /
  `count_runs_by_job` / `runs_by_ids` / `get_body` → the run-history store (its DSL, not the model,
  decides pass/fail); `aggregate_runs` / `summarize_insights` / `current_state` / `watermarks` /
  `operator_scope` → the **materialized** reads (Scale bullet below): rollups, per-cell latest
  state, per-API watermarks and the operator↔API index, none of them a run scan and none of them a
  sample; `stage_job` / `get_pending_jobs` / `apply_job` / `discard_job` / `get_job` /
  `applied_jobs` / `all_jobs` / `active_jobs` → the job queue (`stage` proposes, `apply` is the
  sole state change); `audit` / `append_audit` → the append-only audit log;
  `execute_job_once` → the execution engine, called by the scheduler with no LLM in it;
  `get_context` → the project profile that fills the per-request context block;
  `stage_rule` / `apply_rule` / `discard_rule` / `get_pending_rules` / `get_rule` / `list_rules` /
  `simulate_rule`
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
  `get_profile` / `list_profiles` / `get_parity_report` → the comparison-profile ledger and the
  parity report
  (below): `execute_job_once` now captures `RunResult.response_body: dict | None` at execution
  time (Mock's `stub_response` injects a volatile `serverTime` and a real `api-004` `$.limit`
  difference between targets); `stage_profile` drafts an ignore-spec (`ProfileDraft` → the
  `ProfileLedger`, mirroring the rule ledger), `apply_profile` stamps `effective_from` and, if a
  parity report already exists for the profile's job, re-diffs it from the STORED response
  bodies with no new backend call and no new run.
- **Scale & retention** (`docs/superpowers/specs/2026-09-06-scale-architecture-design.md`): the
  deployment is months of accumulation — 50k APIs, ~10k runs/day (rehearsal peak 50k), ~2M hot
  runs, 500 operators. The one decision behind all of it is **query-time scanning →
  ingest-time materialization**. There is exactly one ingest point (`record_execution`, once per
  schedule occurrence), so `Store.ingest` folds five derived tables into the same transaction as
  the `runs`/`bodies` insert — one all-or-nothing transaction, rolled back on any failure
  (`Store._transaction` wraps every multi-statement writer; a half-written batch used to sit
  PENDING until the next unrelated commit made it permanent): `current_state` (latest run per `(api, env, data)` cell plus a
  `transitions_total` counter bumped on every pass↔non-pass flip), `rollup_day` (per day × cell
  `count/pass/fail/error/transitions/p95` plus `failed_rule_counts` and `http_status_counts`, each
  a `{key: {count, fail, error}}` map so a grouped read keeps its own split), `rollup_key_day`
  (those two maps TRANSPOSED onto `(day, axis, key)` with the `api_ids` that fed them and the
  `p95_duration_ms` of the cells carrying that key — the same max-merge approximation `rollup_day`
  uses, so the transposed arm answers what `MAX(p95_duration_ms)` on the cell rows would — so a
  key-axis group reads (days × keys) rows instead of `json_each`-ing every cell row in the
  window; a query scoped to a set of APIs keeps the exact `json_each` arm, since a key row is
  already summed across APIs), `api_watermark`
  (`last_pass_at`, `first_non_pass_at`, `last_non_pass_at`, `latest_status`, the API's
  `updated_at`) and `operator_api` (`(operator, api)` → last executed, run count). Ingest runs in
  `ingest_chunk_size` chunks with an `await` between them, which is what keeps a chat SSE turn
  responsive while a 400-cell matrix is being written. "Numbers are computed by deterministic
  code" is unchanged — it just moved to ingest time. Nothing here is a sample: the old 2000-run
  sampling is gone, and so is every silent truncation.
  **The paged contract.** Every list read returns `Page[T] {items, next_cursor, total}`;
  `RunsQuery` (since/until/status/api_id/executed_by/job_id/archived/cursor/`limit ≤ 200`) and
  `AggregateQuery` (since/until/group_by/`scope_api_ids`/`scope_operator`/`order_by`
  (`"failures" | "transitions"`)/`include_run_ids`/limit) are the two query shapes. Cursors are
  **keyset** over `(executed_at DESC, run_id DESC)`, server-made and opaque
  (`atworks_agent/cursor.py` — base64url JSON; no offsets, so a deep page still costs O(page)).
  `summarize_insights(since, until, scope_operator)` answers the flaky/regression tiles as SQL
  COUNTs rather than by counting a ranked group list (which saturated: a cell that flips once
  ranks last by failure volume and never entered the top 500). `operator_scope` is EXACT via the
  `operator_api` join, not an id list the caller ships (that list capped a 16k-API operator at 100
  alphabetically-first APIs). `count_runs_by_job` gives the briefing "which jobs ran, how many
  runs" without materializing a run.
  **Retention tiers**, all `AtworksAgentConfig` fields, run by `Retention.maybe_run(now)` at the
  scheduler tick's tail, once per local day, LLM-free, each step stamping a `retention_state` row
  (`<step>:<YYYY-MM-DD>`) so a repeat tick is a no-op: hot `runs` **180 d**
  (`retention_hot_days`) → moved per-day into `runs_archive` (**moved, never deleted**; read back
  only through an explicit `RunsQuery.archived=True`, same predicates, same keyset order, same
  envelope), the archive purged only once `retention_cold_until` has actually passed (None =
  kept forever, which is the default); `bodies` **90 d**
  (`retention_body_days`) and bodies are the ONLY data ever deleted; the insight narration cache
  **30 d** (`insights_cache_days`); sessions **24 h idle** (`session_idle_hours`, swept by
  `TimestampedSessionStore`); rollups, watermarks, current state, reports, briefings and the audit
  log are **permanent** — they are small and they are the evidence. Response bodies are masked
  **at capture**, before they reach `bodies` (`atworks_agent/masking.py`, five default rules —
  Korean resident id, card, account, phone, email — with `masking_disabled_groups` letting an API
  group opt out of body capture entirely). A parity row that has no body to compare says which
  reason: `본문 캡처 해제` (the group opted out) or `본문 만료` (outside the 90-day window), and
  falls back to a status-only verdict rather than inventing one. A row whose compared bodies carry
  any **masked** path is `basis: "masked"` with a note naming those paths: masking is one-way, so
  two bodies differing only inside a masked leaf both read `***` and would otherwise be published
  as `equal` on `basis: "body"` — a value-equality claim over a value nobody was shown. The paths
  are recorded at capture (`bodies.masked_paths`, `mask_body_paths`) and read back through ABC
  `get_body_masked_paths`; the verdict vocabulary is unchanged, and a real `value_diff` on the
  unmasked remainder still reports. A body never reaches the model at all — and now it never
  reaches the PROCESS on a read path either: no read joins `bodies`, `_RUN_SELECT` carries an
  EXISTS probe into `RunResult.has_body`, `get_body` is the only method that reads the column,
  `run_record` carries `has_body: bool` and not the body, and `last_listed_run_ids` is capped
  at `PROVENANCE_CAP` (200) while the card's population comes from the envelope's `total`.
  **Audit log.** Every host approval surface writes TWO append-only rows — the bare action before
  the executor call and `<action>:<ok|blocked|error>` after — so a route that dies mid-flight still
  leaves the attempt on the record; the backend's own `apply_*` adds an `applied` row. Read at
  `GET /audit`. The model never reaches this path: it is written around the approval mark, which
  only an authenticated host click can set. Chat turns write zero rows.
  **SLOs** live in config (`slo_get_context_ms` 50, `slo_list_runs_ms` 200, `slo_aggregate_ms` 300,
  `slo_insights_ms` 500, `slo_simulate_rule_ms` 1000, `slo_briefing_ms` 5000, `slo_sse_latency_ms`
  100, `slo_retention_ms` 30 000) and are measured by `scripts/scale/bench.py` against a dataset
  built by `scripts/scale/generate.py`; `pytest -m scale` (deselected by default via `pytest.ini`)
  runs a reduced set, `ATWORKS_SCALE_FULL=1` the spec's numbers. The full set the branch was
  proved on: `--apis 50000 --days 180 --per-day 11000 --peak-day 120:50000 --operators 500`
  (2,019,000 runs, ~1.3 GB SQLite) — 13 of 14 measured rows inside their limit, the exception
  being `insights.build` (688 ms of 500) for the bench's deliberately WORST operator, whose
  30-day scope is 33,838 of the 50,000 APIs; a median operator's panel is 199 ms. The named
  follow-up for that one red row is a **per-API key rollup** (`rollup_key_api_day`, ~650k rows at
  2M runs) so an operator-scoped `failed_rule`/`http_status` axis can leave the `json_each` arm
  too — today only the UNSCOPED key axes read `rollup_key_day`, because a key row is already
  summed across APIs. Documented with the per-read profile in README and the fix-wave report,
  never by moving the limit. The
  bench defaults to `--no-mutate`: only the retention probe would destroy the dataset it
  measures, so that one runs on a copy, and it ages out ONE day partition rather than the whole
  set. `SLO_ASSERT` in `host/tests/test_scale.py` now asserts EVERY row the bench measures.
  **REST-adapter obligations** (all stated in the ABC docstrings, because they are the contract a
  Java implementation must honour, not Mock trivia): `record_execution` receives runs **in
  chronological order** and must materialize in that order (the watermark/transition counters are
  order-dependent); run ids are derived by the execution engine, not by the caller; cursors are
  produced and interpreted by the server only; `total` is the count after the filter and
  independent of `limit`; list order is `executed_at DESC, run_id DESC`; and operator scope is a
  server-side join, never an id list.
- **Identity and credentials:** auth mechanism is none in MVP. Operator profiles
  (`host/atworks_host/fixtures/operators.json`, roles `developer`/`qa`/`pm`) are bound once at
  `POST /api/atworks/session {operator_id}` via `sessions.start(operator_id)`; unknown id → 400,
  omitted → the default operator. `AtworksSessionContext.operator`/`.role` are read server-side
  after; later requests carry only `X-Session-Id`. A production host maps its own authentication →
  `operator_id` (the reference `demo_common/sessions.py` seam, unchanged). Real auth is out of MVP
  scope. Backend calls carry server-side credentials the model never sees.
- **Sessions:** the principal is bound once at `POST /api/atworks/session` (`SessionStore.start`)
  onto `AtworksSessionContext` (`session_id`, `project_id`, `operator`) and read server-side after;
  later requests carry only `X-Session-Id` and no request shape names a user. Per-session state is
  `AtworksSessionState` (seen apis/runs/ranks/jobs, `last_population` + `last_listed_run_ids` +
  `last_listed_filter`, approval marks), saved in `host/atworks_host/sessions.py` — identical to
  `refs/.../demo_common/sessions.py` **modulo line endings** (the reference is CRLF, this repo is
  LF by `.gitattributes`; the invariant is that no character of the content differs): a versioned document under a compare-and-set plus an
  appended transcript, written at request end and again when a streamed turn ends (the turn wins
  the race). Actions taken outside the chat queue as `pending_app_events` for the next turn.
- **Marketplace posture:** none. Single-tenant internal tool, closed network, no third-party
  sellers, no `web_search`, no external content source. **Checkout handoff:** n/a (merchant role).
- **Surfaces and renderer modes:** the host serves the chat turn as SSE (`POST /api/atworks/chat`),
  the portal reads `/apis` `/runs` `/jobs`, the card buttons `/changes/{job_id}/apply|discard`
  (reference route names, so the web-shared hooks bind unchanged), plus `/scheduler/tick`
  (LLM-free) and `/reports/{job_id}`. **Every list route answers in one paged envelope**
  `{items, next_cursor, total}` — `/apis` `/runs` `/jobs` `/rules` `/profiles` `/audit`, default
  `limit` 50, `ge=1, le=200`, `cursor` an opaque server string the web only echoes back, `total`
  the count AFTER the filter and independent of `limit` (the legacy `apis`/`runs`/`population` keys
  are gone). `/rules` also takes `?status=` (staged/applied/discarded), served by the ledger, not by
  splitting a page. `GET /audit` is the read-only append-only log. `GET /home/summary` (session)
  fills Home's tiles + briefing header in ONE call from `count_runs`×2 + `get_pending_jobs` +
  `summarize_insights` + `Briefings.latest()`'s header — no run list crosses it. A report is now
  three files, not one growing blob: `runs.jsonl` (appended per execution, bodies never in it),
  `parity.json` (the value block plus the run-id pairs it was computed from, so a re-diff needs no
  new run) and `data.json` (job/summary/matrix/parity/provenance), with `index.html` embedding the
  summary plus the newest 200 rows. Also `GET /runs/insights` (session; the
  flaky/regression_suspect counts as a standalone read — Home itself now takes them from
  `/home/summary`, and the web has no client for this route) and the briefing pair `GET /briefings/latest` (session, JSON) /
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
  no eager input streaming (see the model endpoint line). Two more session-free/session reads
  round out Home: `GET /operators` (no session) lists the operator-profile fixture; `GET
  /home/insights` (session) and `POST /home/insights/refresh` (session) serve the per-operator
  insight panel, 404 when `enable_insight_panel=False` — the panel is read-only, touching no
  approval mark or ledger. Its narration is cached per operator per local day at
  `insights_out/<operator>/<YYYY-MM-DD>/narrative.json`, tagged `generated_by: agent|deterministic`
  for provenance.
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
  All four approval surfaces (job / rule / profile / format batch) run through ONE
  `host_action` helper in `app.py` with a `_Surface` table of the four names that differ — the
  contract it enforces (mark on, audit pair, try/finally, mark off) must not live in four
  copies — and each looks its target up by id (`get_job`/`get_rule`/`get_profile`/
  `get_format_batch`), never by scanning a list page or the pending queue.
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
  **The per-operator insight panel** is likewise not a skill — a read-only Home feature, no tool,
  no chat turn. Scope = the APIs the operator ran (`RunResult.executed_by = job.applied_by`,
  stamped at `execute_job_once`) within `scope_window_days`; an operator who ran nothing falls back
  to project-wide scope (`scope_fallback=True`). Deterministic candidates
  (`atworks_agent/insights.py` `candidate_insights`, on top of `aggregation.py`) are ranked by a
  fixed per-role priority table (`ROLE_PRIORITY`), capped `max_insight_candidates`. Exactly one
  narration call per cache miss (`atworks_agent_runtime/insight_narrator.py` `narrate_insights`) —
  fenced candidates as data, a forced tool, headline/why-it-matters/prompt only, never a number the
  model computed itself; any unknown `candidate_id` in the model's answer is dropped; any exception
  or timeout fails open to `[]`, and the host then serves the deterministic labels with
  `generated_by: "deterministic"` — `enable_insight_narration=False` (`ATWORKS_INSIGHT_NARRATION=0`)
  forces that fallback path for the demo without touching a candidate. The narration is a lazy,
  per-operator-per-day cache (`host/atworks_host/insights.py`); candidates and scope themselves are
  always recomputed live, never cached. The scheduler tick and the daily briefing stay untouched —
  still LLM-free. `get_context` also adds `operator_role`/`scope_api_ids` (capped 20) to the
  per-request context block, rendered as one `operator_line` in chat only when a role is present;
  this is informational for the model's answers, not a filter — tool defaults and grounding rules
  are unchanged, so scope is convenience, never authorization.
- **Validation rules SI guarantee:** a rule's `effective_from` is stamped at `apply_rule`;
  evaluation in `execute_job_once` is additive over the legacy stub and only ever considers runs
  with `executed_at >= effective_from` — past `RunResult`s, success rates, reports and briefings are
  never re-judged or rewritten.
- **Memory:** off. `enable_memory = False`, no `save_memory` / `recall_memories` tools, `store=None`;
  nothing crosses sessions. Phase 2 may add field-name aliases via `commerce_common.memory`'s filter.

Model endpoint: Anthropic Messages format via ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN (OpenRouter in dev, LiteLLM proxy in the closed network); send_thinking_fields=False.
