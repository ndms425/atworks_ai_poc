# atworks-ai-mvp

An independent AI chat service beside aTworks (a Java-backend, Postman-class API test tool): the
model drafts reads and an execution plan; verdicts, execution and approval stay with deterministic
code and a person. `refs/` is a read-only reference checkout — never edit anything under it.
`commerce_common` (from `refs/commerce-agents/commerce-common/`) is import-only: no line of it is
copied, and the role package `atworks-agent/core/atworks_agent/` mirrors `merchant_agent` on top.

## Commerce agent decision record

- **Role:** merchant-side (operator-facing). The operator is a developer or QA engineer; the
  records are `ApiSpec` and `RunResult`; the staged write is a `JobSpec` execution plan.
- **Language / path / shell:** Python 3.11+, pydantic v2, FastAPI + SSE. Role package
  `atworks-agent/core/atworks_agent/`, skills `atworks-agent/skills/` (4), turn loop
  `atworks-agent/runtime/atworks_agent_runtime/orchestrator.py`, host `host/atworks_host/`,
  web `web/` (Task 15). Windows; Git Bash; interpreter `.venv/Scripts/python.exe`;
  tests `pytest -q` from the repo root.
- **Backend:** one ABC, `AtworksBackend` (`atworks-agent/core/atworks_agent/backend.py`) — the only
  contact with aTworks. MVP runs entirely on a Mock backend; a REST adapter
  (`host/atworks_host/rest_backend.py`) is the whole Java integration.
  Systems behind each method: `search_apis` / `get_api` → the aTworks API-spec registry;
  `list_runs` / `get_run` / `count_runs` → the run-history store (its DSL, not the model, decides
  pass/fail); `stage_job` / `get_pending_jobs` / `apply_job` / `discard_job` → the job queue
  (`stage` records a proposal only; `apply` is the sole state change); `execute_job_once` → the
  aTworks execution engine, called by the scheduler on a path with no LLM in it; `get_context` →
  the project profile that fills the per-request context block.
- **Identity and credentials:** auth mechanism is none in MVP (fixed `OPERATOR` constant in the
  host; production host derives the principal from its authentication). The principal is bound at
  session start and lives on `AtworksSessionContext` (`session_id`, `project_id`, `operator`); no
  request shape names a user. Backend calls carry server-side credentials the model never sees.
- **Sessions:** the principal is bound once at `POST /api/atworks/session`
  (`SessionStore.start`) and read server-side afterwards; every later request carries only
  `X-Session-Id` and no request shape names a user. Per-session state is `AtworksSessionState`
  (seen apis/runs/ranks/jobs, `last_population` + `last_listed_run_ids` + `last_listed_filter`,
  approval marks), saved with the session in `host/atworks_host/sessions.py` — byte-identical to
  `refs/commerce-agents/examples/demo_common/sessions.py`: a versioned state document under a
  compare-and-set plus an appended transcript, written back at request end and again when a
  streamed turn ends (the turn wins a race). Actions taken outside the chat queue as
  `pending_app_events` and reach the next turn as a note.
- **Marketplace posture:** none. Single-tenant internal tool, closed network, no third-party
  sellers, no `web_search`, no external content source.
- **Surfaces and renderer modes:** the host serves the chat turn as SSE (`POST
  /api/atworks/chat`), the portal reads `/apis` `/runs` `/jobs`, the card buttons
  `/changes/{job_id}/apply|discard` (reference route names, so the web-shared hooks bind
  unchanged), plus `/scheduler/tick` (LLM-free) and `/reports/{job_id}`. Components are
  presentation tools filled server-side: `run_digest`, `job_preview`, `question_form`, chips; job
  lifecycle rides `change_update` with a `change_id` alias. Reports are a template rendered once
  over a `data.json` the scheduler refreshes. Next.js portal (`web/atworks-web`, from the reference
  `merchant-web`) is Task 15. No progressive/partial rendering (no component sets
  `enrich_partial`) and no eager input streaming (see the model endpoint line).
- **Approval surface / `require_host_approval`:** `require_host_approval = True`; approval surface
  is **the Jobs page approve button**. `POST /api/atworks/changes/{job_id}/apply` → `job_action`
  in `host/atworks_host/app.py` (mirror of `change_action`, `demo_common/merchant.py:238-283`) is
  the only thing that marks `approved_job_ids`: the mark goes on immediately before the executor
  call and comes off immediately after, whatever the outcome, so no chat turn can spend it.
  `apply_job` is held without it; approval typed in chat approves nothing. Discards take the same
  path through `host_action_job_ids`, which stamps the job `discarded_by_kind = OPERATOR`.
  `job_review_policy = "always"`.
- **Checkout handoff:** not applicable (merchant role).
- **Flows covered:** four skills, loaded on demand over the prompt's index. (1) `failed-triage` —
  `list_runs {filters:{status:non_pass}}` → `rank_failed_runs` (deterministic `risk_v1`, no
  analysis delegate) → `present_run_digest`, population and items bound to one list window;
  (2) `schedule-run` — resolve the selection, fill or ask the missing slots, `stage_job` → preview
  card → host approval → `apply_job`, then the scheduler executes with no LLM in the path;
  (3) `job-approval` — pending queue, apply an already-approved job, discard; (4) `api-lookup` —
  specs, params, rules. Screen attachments (`<attached-result-items>`) scope any turn to the
  ref_ids the operator attached.
- **Memory:** off. `enable_memory = False`, no `save_memory` / `recall_memories` tools are
  registered, `MemoryRuntime` is built with `store=None`. Nothing is remembered across sessions.
  Closed-network SI constraint; Phase 2 may enable field-name aliases only, through
  `commerce_common.memory`'s schema-validated write filter.

Model endpoint: Anthropic Messages format via ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN (OpenRouter in dev, LiteLLM proxy in the closed network); send_thinking_fields=False.
