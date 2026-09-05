# Value Parity Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Compare two servers' (e.g. legacy vs renewed) API RESPONSE VALUES — not just status — flagging real value differences at scale, with a chat-authored ignore-spec that clusters and clears volatile-field noise, re-diffing stored responses without re-calling the servers.

**Architecture:** Builds on the shipped matrix-job + env-comparison report (which compares status only). We add: response-body capture on `RunResult`; a deterministic body-comparison engine + noise clustering; a parity report that renders value-level verdicts; a `ComparisonProfile` (ignore-spec) approved host-only like a validation rule; and — the key property — applying/adjusting a profile RE-DIFFS the stored bodies, never re-running. Chat resolves the parity job and authors the ignore-spec from observed clusters; judging equivalence stays deterministic.

**Tech Stack:** Python 3.11+, pydantic v2, FastAPI, `commerce_common` (import-only), pytest; Next.js 16 in `web/atworks-web`.

**Spec:** `docs/superpowers/specs/2026-09-05-value-parity-comparison-design.md`

## Global Constraints

- `refs/` read-only; `commerce_common` import-only.
- **Equivalence is judged by deterministic code**, never the model: `compare_bodies(a, b, ignore_paths)` after applying an approved ignore-spec. The model proposes ignore paths and orchestrates; it never decides equal/differ.
- **Re-diff without re-run:** applying or changing a `ComparisonProfile` recomputes the parity verdicts from STORED response bodies; it must not create new `RunResult`s or call `execute_job_once`.
- **Past runs immutable:** a new comparison or a profile approval never rewrites a stored `RunResult`.
- **Host-only approval** for comparison profiles (mark set/cleared by the route only; chat cannot spend it), mirroring the rule/format-batch lifecycle. Own route namespace (`/profiles`), never `/changes`, `/rules`, `/format-batches`.
- Scheduler/report paths stay LLM-free. Caps are `AtworksAgentConfig` fields mirrored as tool-schema `maxItems`; tool bytes a pure function of config.
- Windows commands; per-task commit with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; `git checkout -- web/atworks-web/next-env.d.ts` after a web build.

## File map

| File | Responsibility |
|---|---|
| `atworks-agent/core/atworks_agent/types.py` | `RunResult.response_body`; `ComparisonProfile`, `ProfileStatus`; session state fields |
| `atworks-agent/core/atworks_agent/config.py` | named-target allow-list generalization; profile caps |
| `atworks-agent/core/atworks_agent/parity.py` (new) | `compare_bodies`, `cluster_diffs`, `apply_ignore` |
| `atworks-agent/core/atworks_agent/profiles.py` (new) | `ProfileDraft`, `ProfileLedger`, `check_profile_guardrails` |
| `atworks-agent/core/atworks_agent/backend.py` | ABC: profile methods + `list_runs` bodies note |
| `atworks-agent/core/atworks_agent/gates.py` | profile gates (mirror rule gates) |
| `atworks-agent/core/atworks_agent/serialization.py` | `profile_record` |
| `atworks-agent/core/atworks_agent/executor.py` | profile handlers + `recommend_ignore_paths` read |
| `atworks-agent/core/atworks_agent/tools/{registry,presentation}.py`, `enrichment.py` | profile tools + parity/profile cards |
| `atworks-agent/core/atworks_agent/prompt.py` | one hard line |
| `atworks-agent/skills/parity-compare/SKILL.md` (new) | the 6th skill |
| `host/atworks_host/mock_backend.py` | `stub_response`, response-body on runs, profile ledger, re-diff |
| `host/atworks_host/reports.py`, `report_template.html` | parity matrix (value verdicts + clusters), re-diff |
| `host/atworks_host/app.py` | `/profiles`, `/profiles/{id}/apply|discard`, parity re-diff route |
| `web/atworks-web/**` | parity report view, profile card+hook, Profiles surface, parity summary card |
| `scripts/smoke_chat.py`, `CLAUDE.md`, `README.md` | smoke turn, docs |

---

## Part A — the comparison engine (value diff + clustering), no approval object yet

### Task 1: Response-body capture

**Files:** Modify `types.py`, `host/atworks_host/mock_backend.py`; Test `host/tests/test_mock_backend.py`.

- [ ] **Step 1: model** — `RunResult` gains `response_body: dict[str, Any] | None = None` (after `duration_ms`). Keep `exclude_none` serialization so existing fixtures/records are unchanged.
- [ ] **Step 2: Mock `stub_response`** — add a deterministic response stub:
```python
def stub_response(api: ApiSpec, env: str, data: TestDataSet | None, seq: int) -> dict:
    """Deterministic per-target response body for parity demos. A volatile field (serverTime)
    differs every call (noise); a `_renewed`-only value difference on some APIs is a REAL diff."""
    body: dict[str, Any] = {
        "path": api.path, "serverTime": f"2026-09-05T00:00:{seq % 60:02d}",  # volatile → noise
        "echo": {k: v for k, v in (data.values.items() if data else [])},
    }
    # a real, deterministic value difference on the renewed server for one payment API:
    if api.api_id == "api-004":
        body["limit"] = 1000 if env != "renewed" else 900   # renewed changed the value → real diff
    return body
```
In `execute_job_once`, build `response_body=stub_response(api, env, data, self._run_seq)` and pass it to `RunResult(...)`.
- [ ] **Step 3: Tests** — a run now carries `response_body`; two targets for api-004 have different `limit`; every run's `serverTime` varies with seq; a normal api's bodies match except serverTime.
- [ ] **Step 4: run green + ruff → Step 5: Commit** `feat(host): capture response bodies on runs (Mock stub with a real diff and a volatile field)`.

### Task 2: Comparison engine + clustering

**Files:** Create `atworks-agent/core/atworks_agent/parity.py`; Test `atworks-agent/core/tests/test_parity.py`.

**Interfaces:** `apply_ignore(body, ignore_paths) -> dict`; `compare_bodies(a, b, ignore_paths) -> BodyDiff(equal, diff_paths)`; `cluster_diffs(rows) -> list[DiffCluster(paths, count, row_keys)]`.

- [ ] **Step 1: Failing tests** — `compare_bodies({"a":1,"t":"x"},{"a":1,"t":"y"}, ["t"])` is equal; without ignoring `t` it differs with `diff_paths == ["t"]`; nested (`$.a.b`) and array-index (`$.arr[1]`) paths reported with JSONPath-ish dotted keys; `cluster_diffs` groups rows whose `diff_paths` sets are identical, returns "N rows differ only in [serverTime]".
- [ ] **Step 2: implement** — pure functions:
```python
def _paths(obj, prefix="$"):  # yields (path, value) leaves, arrays as [i]
    ...
def compare_bodies(a, b, ignore_paths):
    ap = {p: v for p, v in _paths(a) if p not in ignore_paths}
    bp = {p: v for p, v in _paths(b) if p not in ignore_paths}
    diff = sorted({p for p in ap | bp.keys() if ap.get(p) != bp.get(p)})
    return BodyDiff(equal=not diff, diff_paths=diff)
def cluster_diffs(rows):  # rows: [{row_key, diff_paths}]
    buckets = defaultdict(list)
    for r in rows:
        buckets[tuple(r["diff_paths"])].append(r["row_key"])
    return sorted((DiffCluster(paths=list(k), count=len(v), row_keys=v) for k, v in buckets.items()),
                  key=lambda c: -c.count)
```
(`ignore_paths` match a leaf path exactly OR as a prefix — a path is ignored if it equals an ignore entry or starts with `entry + "."`/`entry + "["`. Cover both in a test.) Export from `__init__.py`.
- [ ] **Step 3: run green + ruff → Step 4: Commit** `feat(core): deterministic body comparison and noise clustering`.

### Task 3: Named targets

**Files:** Modify `config.py`, `host/atworks_host/mock_backend.py`; Test `test_config.py`, `test_mock_backend.py`.

- [ ] **Step 1:** generalize `allowed_target_envs` — keep the field but document it is an arbitrary named-target allow-list; add a default that includes parity targets, e.g. `allowed_target_envs: tuple[str, ...] = ("dev", "stg", "legacy", "renewed")`, and a `max_target_envs_per_job` stays. (The guardrail already checks `target_envs ⊆ allowed`.) The Mock's `stub_response`/`stub_verdict` already key off the env name, so `legacy`/`renewed` flow through. Add a `target_endpoints: dict[str,str]` config note (Mock only uses names; a REST adapter maps names→URLs). Optional per-target auth is out of MVP (spec §7) — leave a comment.
- [ ] **Step 2: Tests** — a job with `target_envs=["legacy","renewed"]` stages and executes, producing runs on both; the existing dev/stg tests still pass (default allow-list is a superset).
- [ ] **Step 3: run green + ruff → Step 4: Commit** `feat(core,host): named targets (legacy/renewed) for parity runs`.

### Task 4: Parity report (value verdicts + clusters), re-diff-ready

**Files:** Modify `host/atworks_host/reports.py`, `report_template.html`; Test `host/tests/test_scheduler_reports.py`.

**Interfaces:** `Reports.write(...)` gains a `parity` block when the job has exactly two targets; `Reports.rediff(job_id, ignore_paths)` recomputes the parity block from stored `data.json` bodies without new runs.

- [ ] **Step 1: Failing tests** — a job on `[legacy, renewed]` with api-004 (real `limit` diff) and a volatile `serverTime`: the parity block has per-(api,data) rows with verdict `equal`/`status_diff`/`value_diff`, api-004 is `value_diff` with `diff_paths` containing `$.limit` AND `$.serverTime` (before any ignore); `clusters` groups the serverTime-only rows; `rediff(job_id, ["$.serverTime"])` recomputes so api-004 stays `value_diff` (still differs on `$.limit`) but the serverTime-only rows flip to `equal`, and NO new runs are created (`backend.runs` count unchanged).
- [ ] **Step 2: implement** — extend `_matrix` (or add `_parity`): when `len(job.target_envs) == 2` and both cells have `response_body`, compute `compare_bodies(a_body, b_body, ignore_paths)` per row → verdict + `diff_paths`; `status_diff` when statuses differ; `equal` otherwise. Add `clusters = cluster_diffs(...)`. `data["parity"] = {targets, rows, value_diff_count, status_diff_count, clusters, ignore_paths}`. `rediff(job_id, ignore_paths)` reads the folder's `data.json` (which already stores each run incl. `response_body`), recomputes only the `parity` block with the new `ignore_paths`, rewrites `data.json` + `index.html`. Store `response_body` in the per-run `data["runs"]` (it already dumps runs; ensure the body is included — it is via `model_dump`). Template: a parity section (rows with verdict, diff paths; a clusters summary "N rows differ only in <paths>").
- [ ] **Step 3: run green + ruff → Step 4: Commit** `feat(host): value-parity report block with clusters and re-diff-without-re-run`.

---

## Part B — comparison profile (ignore-spec) as an approved object + chat

### Task 5: `ComparisonProfile` model, ledger, guardrails, gates

**Files:** Create `profiles.py`; modify `types.py`, `gates.py`, `serialization.py`, `__init__.py`; Test `test_profiles.py`, `test_gates.py`.

Mirror the rule lifecycle exactly (`RuleLedger`, `check_apply_rule`, session marks). Concretely:
- `ProfileStatus` enum (STAGED/APPLIED/DISCARDED) in types.py.
- `ComparisonProfile` (in types.py, so `seen_profiles: dict[str,ComparisonProfile]` round-trips): `profile_id, job_id (the parity run it targets), ignore_paths: list[str], per_api_ignore: dict[str, list[str]] = {}, status, effective_from, summary, created_*, applied_*, discarded_*`.
- `ProfileDraft` (profiles.py, extra="forbid"): `job_id, ignore_paths, per_api_ignore, summary`; validate each path is a non-empty `$`-rooted dotted string; cap by `config.max_ignore_paths`.
- `ProfileLedger` (mirror `RuleLedger`): stage/get/pending/applied/discard/apply(stamps effective_from).
- `check_profile_guardrails(draft, config)`; gates `check_apply_profile`/`check_discard_profile`/`take_profile_discard_actor_kind`; session state `seen_profiles`/`approved_profile_ids`/`host_action_profile_ids` + `remember_profile`. `profile_record` (adds `change_id`).
- Test: stage→apply stamps effective_from; host-mark required; unknown apply refused; caps.

- [ ] TDD each; **Commit** `feat(core): ComparisonProfile lifecycle, guardrails and host-approval gates`.

### Task 6: Backend + Mock — profiles drive re-diff

**Files:** Modify `backend.py`, `host/atworks_host/mock_backend.py`, `host/atworks_host/app.py` (wire re-diff); Test `host/tests/test_profiles_backend.py`.

- [ ] Backend ABC: `stage_profile`/`get_pending_profiles`/`apply_profile`/`discard_profile`/`list_profiles`. Mock: a `ProfileLedger`; **`apply_profile` triggers `reports.rediff(profile.job_id, profile.ignore_paths + flattened per_api)` — recompute from stored bodies, NO new runs** (assert run count unchanged in a test). `list_profiles(job_id=None)`.
- [ ] Test: applying a profile with `["$.serverTime"]` re-diffs the parity report so serverTime-only rows become `equal`, run count unchanged, past run verdicts unchanged (immutability).
- [ ] **Commit** `feat(host): applying a comparison profile re-diffs stored bodies without re-running`.

### Task 7: Executor handlers + ignore-path recommendation

**Files:** Modify `executor.py`, `tools/registry.py`; Test `test_executor.py`.

- [ ] `_stage_profile`/`_apply_profile`/`_discard_profile`/`_get_pending_profiles` (mirror the rule handlers, provenance = the parity job seen this session, gates, `profile_record`). `_recommend_ignore_paths(job_id)` — a read that returns the parity report's clusters ranked (the biggest noise clusters first) so the model can propose "ignore these to clear N rows". Deterministic (reads the report), no fabrication.
- [ ] Extend the `dispatch` question-form guard tool set with `stage_profile`/`apply_profile`.
- [ ] Test: stage a profile provenance-gated on the job; apply requires the host mark; `recommend_ignore_paths` returns the clusters.
- [ ] **Commit** `feat(core): executor comparison-profile handlers and ignore-path recommendation`.

### Task 8: Tools, cards, prompt, skill

**Files:** Modify `tools/registry.py`, `tools/presentation.py`, `enrichment.py`, `prompt.py`; new `skills/parity-compare/SKILL.md`; `scripts/smoke_chat.py`; Test `test_registry.py`, `test_presentation.py`, `test_prompt.py`, `test_skills_load.py`.

- [ ] Tools (after the format-batch tools): `stage_profile`, `apply_profile`, `discard_profile`, `get_pending_profiles`, `recommend_ignore_paths`; cards `present_parity_summary` (reads the parity report's clusters/counts) and `present_profile_preview` (the staged ignore-spec). Gate behind `config.stages_rules` (or a new `enable_parity`) via `absent_tools()`.
- [ ] `enrich_parity_summary` (join the report's clusters + counts) and `enrich_profile_preview` (the staged profile). Prompt hard line: "Value equivalence is judged by the comparison engine, never by you; you propose ignore paths from the diff clusters, and a person approves them; applying a profile re-diffs stored responses, it never re-runs."
- [ ] Skill `parity-compare`: resolve APIs + two targets + identical data → stage the parity run → read clusters → propose an ignore-spec for the biggest noise clusters → approve → the real diffs remain.
- [ ] Smoke: a parity turn expecting `{"parity_summary"}` (or `job_preview` for the stage).
- [ ] **Commit** `feat(core): parity summary + profile tools/cards, prompt, parity-compare skill`.

---

## Part C — host routes + web

### Task 9: Host routes

**Files:** Modify `host/atworks_host/app.py`; Test `host/tests/test_app.py`.

- [ ] `profile_action` (mirror `rule_action`, marks `approved_profile_ids`/`host_action_profile_ids`); `GET /profiles`, `GET /profiles?job_id=`, `POST /profiles/{id}/apply|discard`. Own namespace. The parity report is served by the existing `/reports/{job_id}` (now carrying the `parity` block). Applying a profile re-diffs (Task 6), so the report route reflects it on next read.
- [ ] Test: `GET /profiles`, stage+approve marks-then-consumes, unknown → ok:false, mark not spendable by chat.
- [ ] **Commit** `feat(host): comparison-profile routes with host-only approval`.

### Task 10: Web — parity report, profile card + Profiles surface

**Files:** `web/atworks-web/**`.

- [ ] Types (`ComparisonProfile`, `ParityRow`, `DiffCluster`, `ParityPayload`); `fetchProfiles`, `actOnProfile`, `lib/useProfileActions.ts` (posts to `/profiles`). `ParitySummaryCard` (clusters + "이 필드 빼면 N개 정리" + counts) and `ProfilePreviewCard` (ignore paths; `ApproveBar` via `useProfileActions`); `index.tsx` `parity_summary`/`profile_preview` cases with a new `onProfileAction` prop (never `/changes`, `/rules`, `/format-batches`). The parity report renders in the existing report HTML (host template) — no portal view needed for the grid; a "Profiles" section on the Rules page lists applied ignore-specs.
- [ ] `npm run build` clean; revert next-env. **Commit** `feat(web): parity summary + comparison-profile card and Profiles surface`.

---

## Part D — docs, review, verification

### Task 11: Decision record, review, live smoke

- [ ] CLAUDE.md: extend flows/backend/surfaces with value parity (response-body capture, the comparison engine, comparison profiles approved host-only, re-diff-without-re-run, the parity report block, named targets), the 6th skill; reaffirm equivalence is judged by code, not the model, and past runs immutable. README paragraph.
- [ ] H3 review (`/review-commerce-agent`); fix mismatched decision-record rows.
- [ ] Live smoke: stage a parity run on `[legacy, renewed]` for a set including api-004 → parity report shows value_diff on api-004 + serverTime noise; "serverTime 무시해" → stage+approve a profile → report re-diffs (serverTime rows clear, api-004 real diff remains) with NO new runs; confirm `/runs` count and past verdicts unchanged before/after.
- [ ] **Commit** `docs: decision record and README for value parity comparison`.

---

## Self-review notes

- **Re-diff-without-re-run** is the load-bearing property (Task 6): applying/adjusting a profile recomputes the parity block from stored `response_body`s in `data.json`; asserted by a run-count-unchanged test.
- **Equivalence is deterministic** (Task 2 `compare_bodies`); the model only proposes ignore paths (Task 7 `recommend_ignore_paths` reads real clusters — no fabrication) and orchestrates.
- **Session-state typing lesson**: `seen_profiles` typed with the real `ComparisonProfile` (in types.py) so it round-trips — same fix as the rules `dict[str,Any]` bug.
- **Route separation**: `/profiles` distinct from `/changes`/`/rules`/`/format-batches`; its own web hook.
- **Immutability**: response bodies are captured once at execution; re-diff never rewrites a run; a profile has `effective_from` for audit but re-diff over stored bodies is a read-time recompute, not a run mutation — past `RunResult`s are byte-stable.
- Type names across tasks: `response_body` (T1); `compare_bodies`/`cluster_diffs`/`apply_ignore`/`BodyDiff`/`DiffCluster` (T2); named targets (T3); parity report block + `rediff` (T4); `ComparisonProfile`/`ProfileDraft`/`ProfileLedger`/gates/`seen_profiles` (T5); backend profile methods + re-diff (T6); handlers + `recommend_ignore_paths` (T7); `present_parity_summary`/`present_profile_preview`/skill (T8); `profile_action`+routes (T9); web hook/cards (T10).
