# Safety

This page lists what this project's code enforces, what it still asks the model to do, and
what a deployment adds. Format mirrors `refs/commerce-agents/docs/safety.md`.

Paths are package-relative: `atworks_agent/` is `atworks-agent/core/atworks_agent/`;
`atworks_agent_runtime/` is `atworks-agent/runtime/atworks_agent_runtime/`; `atworks_host/`
is `host/atworks_host/`.

A rule enforced inside a tool call holds regardless of which model is behind
`AsyncAnthropic` (see `docs/sllm-seam.md`) — the executor is the same code path either way.
A rule enforced on the turn or the route lives in the host.

## Enforced in code

| Rule | Enforced in |
|---|---|
| **Verdict immutability.** `RunResult.status` is a deterministic verdict — only the backend (mock stub or, in production, the DSL engine) creates it. When a digest item's `kind` is presented, `enrich_run_digest` overwrites it with `run.status.value` so a mislabeled item can never present a pass run as a failure. | `RunResult` in `atworks_agent/types.py`; `enrich_run_digest` in `atworks_agent/enrichment.py` |
| **Population required.** `present_run_digest` refuses without the population it was drawn from. `list_runs` records `last_population` and the exact `last_listed_run_ids` window; digest items outside that window, or already `pass`, are dropped with a note instead of shown. | `enrich_run_digest` in `atworks_agent/enrichment.py`; `last_listed_run_ids`/`last_population` set by `_list_runs` in `atworks_agent/executor.py` |
| **Provenance.** `stage_job` accepts only `api_ids` this session's `search_apis`/`get_api`/`list_runs` returned; `apply_job`/`discard_job` accept only `job_id`s this session staged, listed, or that the host marked approved. | `check_api_provenance`, `check_apply_job`, `check_discard_job` in `atworks_agent/gates.py` |
| **Guardrails twice.** Guardrails (APIs-per-job cap, allowed `target_env`, schedule count, LATE needs `select_where`, …) run at stage time and again at apply time against the config in force then. | `check_job_guardrails` in `atworks_agent/jobs.py`, called from `_stage_job` in `atworks_agent/executor.py` and again inside `JobLedger.apply` |
| **Host approval.** `apply_job`/`discard_job` succeed only for a `job_id` the host's own route marked. The mark is consumed on every path through `job_action` — success, refusal (blocked), and error alike clear `approved_job_ids`/`host_action_job_ids` — so a stale mark never carries into a later call. | `job_action` in `host/atworks_host/app.py` |
| **LATE re-check at execution.** A `LATE`-bound job resolves `select_where` again at execution time, not at stage time; if the re-resolved selection now exceeds `max_apis_per_job` the run is skipped (and the slot is still spent, so a scheduled job does not retry the same over-limit selection forever). | `execute_job_once` in `host/atworks_host/mock_backend.py` |
| **Scheduled execution calls no model.** `Scheduler.tick` walks applied jobs, calls `backend.execute_job_once`, and writes the report directly — it never constructs or calls `AtworksAgent`/`AsyncAnthropic`. | `Scheduler.tick` in `host/atworks_host/scheduler.py` |
| **Attachment fencing.** Attached items (from a comment on a run) are rendered inside a fixed-label fence with a hard-scope instruction; every field is sanitized, and even the fence's own boundary tag (`<attached-result-items>`) is stripped from field values to a fixpoint so a value cannot forge the closing tag early. | `render_attached_items_hint` in `atworks_agent/attachments.py` |
| **Tool surface fixed.** The tool list is built once from config, not from the model's request. `list_runs`'s `filters.status` is validated against a fixed enum before it reaches the backend; an unknown value raises instead of being forwarded. | `build_tools` in `atworks_agent/tools/registry.py`; `filters.status` check in `_list_runs`, `atworks_agent/executor.py` |
| **Report escaping.** A scheduled run's report is written once by code (`Reports.write`), never by the model: `data.json` is embedded into the template with `</` escaped to `<\/` so a job summary or failed rule can't close the data script block early, and the template's own `esc()` HTML-escapes every field before it reaches the DOM. | `Reports.write` in `host/atworks_host/reports.py`; `esc` in `host/atworks_host/report_template.html` |

## Still asked of the model

The prompts and skills carry the other half of these rules:

- A figure or status is stated only from a tool result seen this session, never re-derived
  or restated from memory.
- Attached items are the hard scope of the turn; issues noticed elsewhere are a follow-up
  note, not an action.
- `stage_job` is described as staging — nothing runs or applies until the host approves it.
- Low-confidence fields are flagged (`confidence`/`assumptions`) rather than guessed silently.

When the model breaks one of these, the error is confined to its text. Every verdict,
provenance check, guardrail, and approval behind that text still passed the checks in the
table above, so the failure is a misstatement to correct, not an action to reverse.

These rules hold only as far as the model follows instructions; the table holds on any
model, sLLM included (`docs/sllm-seam.md`). Swapping the model, or turning
`require_host_approval` off, re-runs the manual scenarios and `scripts/smoke_chat.py`
before it ships.

## What a deployment owns

This project stops at the boundary of aTworks itself:

- **Auth.** Authentication and authorization on every host route; the mock host here
  accepts any caller.
- **The real backend.** `MockAtworks` is fixtures and a stub verdict function
  (`stub_verdict`); production points `AtworksBackend` at the real aTworks DSL engine and
  execution history (see `rest_backend.py` in Part F of the plan).
- **Credentials.** The credentials the host's backend calls aTworks with, resolved
  server-side and never shown to the model.
- **Rate limits.** Abuse controls in front of `/api/atworks/chat`.
- **Guardrail values.** The defaults in `atworks_agent/config.py` (`max_apis_per_job`,
  `allowed_target_envs`, `max_schedule_count`, …) are demonstration values; a deployment
  sets its own.
- **Approval surface.** Who may click apply/discard on the host's routes. The gate checks
  only that the host's own code set the mark, not who is allowed to click it.
- **Log hygiene.** Model call logging and request/response body logging at `DEBUG`
  (`build_app` in `host/atworks_host/streaming.py`) may capture the whole conversation; a
  `DEBUG` log needs the retention and access controls that implies.
- **Model endpoint and TLS.** Which model sits behind `ANTHROPIC_BASE_URL` and whether its
  proxy's CA needs `ATWORKS_TRUST_OS_CA` (see `docs/sllm-seam.md`).
