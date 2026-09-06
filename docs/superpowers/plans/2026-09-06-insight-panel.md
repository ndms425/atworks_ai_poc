# Per-Operator AI Insight Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Home panel of AI-narrated insights personalized by the operator's **role** (developer/qa/pm) and
**scope = the APIs that operator ran** (derived from run history), where every number is deterministic, the
model only narrates, narration is lazily generated and cached per operator per day, and the panel degrades to
deterministic labels when narration is off or fails.

**Architecture:** `RunResult.executed_by` (stamped from `job.applied_by` at execution) → pure
`operator_scope` + `candidate_insights` in core (`insights.py`, on top of `aggregation.py`) → a one-shot,
tool-forced `narrate_insights` in the runtime (fenced candidates in, `headline/why/prompt` out, unknown ids
dropped) → host `InsightPanels` that recomputes candidates live and caches only the narrative in
`insights_out/<operator>/<date>/` with `generated_by` provenance → `GET /home/insights` + `POST
/home/insights/refresh` + `GET /operators` → web operator picker + `InsightPanel` on Home. Sessions bind a real
`operator_id` (from `operators.json`) via `sessions.start(operator_id)`; `AtworksSessionContext` gains `role`.

**Tech Stack:** Python 3.11, pydantic v2, FastAPI, `anthropic` AsyncAnthropic (runtime), Next.js/TS web,
pytest, ruff, `npm run build`.

**Spec:** `docs/superpowers/specs/2026-09-06-insight-panel-design.md`

## Global Constraints

- **Numbers are deterministic.** Every figure on a card comes from `InsightCandidate.figures` computed in
  `insights.py`; the model never computes or restates a number. Cards render figures from the candidate record.
- **Model narrates only, and only known candidates.** `narrate_insights` keeps a narrative iff its
  `candidate_id` is in the candidate list; unknown ids are dropped with a note. Lengths: `headline ≤ 80`,
  `why_it_matters ≤ 160`, `prompt ≤ 120`; all text passes `ATWORKS_FENCE.sanitize_text`.
- **Panel is read-only.** `build`/`refresh` never touch `approved_*_ids`, `host_action_*_ids`, any ledger, or
  create runs — a test asserts byte-identical state before/after.
- **Degrade, don't depend.** Narration off (`enable_insight_narration=False`) or any exception/timeout → empty
  narratives → `generated_by="deterministic"` and deterministic `label`s shown. Candidates are ALWAYS live.
- **No LLM in the scheduler.** Narration runs only in the request path (`GET /home/insights`, `/refresh`).
  `scheduler.py`/`briefing.py` are untouched.
- **Scope = APIs the operator ran:** `executed_by == operator_id` and `executed_at >= now - scope_window_days`.
  `executed_by is None`, other operators, and older runs are excluded. Empty scope → caller may fall back to
  project-wide candidates with `scope_fallback=True` displayed.
- **`executed_by` = the approver.** Mock `execute_job_once` stamps `executed_by = job.applied_by` on each run.
- **Config is the single source:** `enable_insight_panel=True`, `enable_insight_narration=True`,
  `scope_window_days=30`, `max_insight_candidates=5`, `stale_pending_hours=24`,
  `insight_narration_timeout_s=20`, `insight_narration_model: str | None = None`.
- **Path safety:** cache paths use `SAFE_ID` (operator) and `SAFE_DATE`, with `is_relative_to` escape checks —
  the `reports.py`/`briefing.py` rule.
- **Session typing lesson:** every new session/state field is a real pydantic model or Literal, never `Any`.
- Python 3.11+, pydantic v2; `.venv/Scripts/python.exe -m pytest -q` and `ruff check atworks-agent host` from
  repo root; web `npm run build` in `web/atworks-web` (revert `next-env.d.ts` if it drifts).
- Never edit `refs/`. Commit trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File structure

- `atworks-agent/core/atworks_agent/types.py` — `OperatorRole`, `OperatorProfile`, `RunResult.executed_by`,
  `AtworksSessionContext.role`, `InsightKind`, `InsightCandidate`, `InsightNarrative`, `InsightPanel`.
- `atworks-agent/core/atworks_agent/config.py` — the seven settings.
- `atworks-agent/core/atworks_agent/insights.py` (new) — `operator_scope`, `ROLE_PRIORITY`, `KIND_LABEL`,
  `candidate_insights`.
- `atworks-agent/runtime/atworks_agent_runtime/insight_narrator.py` (new) — `narrate_insights`.
- `host/atworks_host/fixtures/operators.json` (new), `fixtures/runs.json` (add `executed_by`).
- `host/atworks_host/mock_backend.py` — stamp `executed_by`; `get_context` adds role/scope.
- `host/atworks_host/insights.py` (new) — `InsightPanels`.
- `host/atworks_host/app.py` — `/operators`, `/session {operator_id}`, `context()` with role, `/home/insights`,
  `/home/insights/refresh`.
- `host/atworks_host/main.py` — construct `InsightPanels` with `agent.client`.
- `atworks-agent/core/atworks_agent/prompt.py` — render role/scope line from `atworks_context`.
- Web: `lib/types.ts`, `lib/api.ts`, `components/OperatorPicker.tsx` (new), `components/InsightPanel.tsx`
  (new), `components/views/HomeView.tsx`, `app/page.tsx`.
- Docs: `CLAUDE.md`, `README.md`.

---

## Part A — core

### Task 1: Types, config, `executed_by` stamping, fixture attribution

**Files:**
- Modify: `types.py`, `config.py`, `__init__.py`, `host/atworks_host/mock_backend.py`, `host/atworks_host/fixtures/runs.json`
- Test: `atworks-agent/core/tests/test_types.py`, `test_config.py`, `host/tests/test_mock_backend.py` (or the file that exercises `execute_job_once`)

**Interfaces (produces):**
```python
OperatorRole = Literal["developer", "qa", "pm"]
class OperatorProfile(BaseModel):
    operator_id: str = Field(max_length=64, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(max_length=80)
    role: OperatorRole
# RunResult: executed_by: str | None = None          (after job_id)
# AtworksSessionContext: role: OperatorRole | None = None
InsightKind = Literal["regression_suspect", "flaky_cell", "top_failed_rule", "env_divergence", "stale_pending"]
class InsightCandidate(BaseModel):
    candidate_id: str = Field(max_length=120)
    kind: InsightKind
    label: str = Field(max_length=120)
    figures: dict[str, int | str] = Field(default_factory=dict)
    api_ids: list[str] = Field(default_factory=list, max_length=20)
    ref_ids: list[str] = Field(default_factory=list, max_length=20)
    priority: int = 0
class InsightNarrative(BaseModel):
    candidate_id: str = Field(max_length=120)
    headline: str = Field(max_length=80)
    why_it_matters: str = Field(max_length=160)
    prompt: str = Field(max_length=120)
class InsightItem(BaseModel):
    candidate: InsightCandidate
    narrative: InsightNarrative | None = None
class InsightPanel(BaseModel):
    operator_id: str; name: str; role: OperatorRole
    scope_api_ids: list[str] = Field(default_factory=list)
    scope_fallback: bool = False
    window_days: int
    generated_at: datetime
    generated_by: Literal["agent", "deterministic"]
    items: list[InsightItem] = Field(default_factory=list)
```
Config: `enable_insight_panel: bool = True`, `enable_insight_narration: bool = True`,
`scope_window_days: int = Field(default=30, ge=1)`, `max_insight_candidates: int = Field(default=5, ge=1)`,
`stale_pending_hours: int = Field(default=24, ge=1)`, `insight_narration_timeout_s: int = Field(default=20, ge=1)`,
`insight_narration_model: str | None = None`.

- [ ] **Step 1: Failing tests**
```python
# test_types.py (append)
def test_run_result_executed_by_defaults_none_and_round_trips():
    r = RunResult(run_id="r", api_id="a", executed_at=datetime(2026,9,1,tzinfo=UTC), target_env="dev", status="pass")
    assert r.executed_by is None
    r2 = RunResult(**{**r.model_dump(), "executed_by": "minseong"})
    assert RunResult.model_validate(json.loads(r2.model_dump_json())).executed_by == "minseong"

def test_operator_profile_rejects_bad_role_and_id():
    with pytest.raises(Exception): OperatorProfile(operator_id="x", name="n", role="admin")
    with pytest.raises(Exception): OperatorProfile(operator_id="../x", name="n", role="qa")

# test_config.py (append)
def test_insight_panel_settings_have_defaults():
    c = AtworksAgentConfig(model="m")
    assert (c.enable_insight_panel, c.enable_insight_narration, c.scope_window_days, c.max_insight_candidates,
            c.stale_pending_hours, c.insight_narration_timeout_s, c.insight_narration_model) == (True, True, 30, 5, 24, 20, None)

# host test (append to the execute_job_once test file)
async def test_execute_job_once_stamps_executed_by_from_the_approver():
    b = _backend(); job = await _stage_and_apply(b, approver="jihoon")   # reuse the file's helpers; apply as operator "jihoon"
    runs = await b.execute_job_once(SESSION, job.job_id)
    assert runs and all(r.executed_by == "jihoon" for r in runs)

def test_fixture_runs_carry_operators():
    rows = json.loads((FIXTURES / "runs.json").read_text(encoding="utf-8"))
    assert {r.get("executed_by") for r in rows} >= {"minseong", "jihoon", "sora"}
```
- [ ] **Step 2: run → fail.** — [ ] **Step 3: Implement** the models/fields/config; in `mock_backend.execute_job_once` where `RunResult(...)` is built (~L342) add `executed_by=job.applied_by`; in `fixtures/runs.json` add `"executed_by"` to every row, distributing `minseong` / `jihoon` / `sora` so each has ≥3 APIs (leave 2–3 rows without the key to represent legacy runs). Export the new names from `__init__.py`.
- [ ] **Step 4: green + ruff** → **Step 5: Commit** `feat(core,host): operator profiles, executed_by on runs, insight types and config`.

### Task 2: `insights.py` — scope and deterministic candidates

**Files:** Create `atworks-agent/core/atworks_agent/insights.py`; Test `atworks-agent/core/tests/test_insights.py`.

**Interfaces (produces):**
```python
def operator_scope(runs: Sequence[RunResult], operator_id: str, window_days: int, now: datetime) -> set[str]
ROLE_PRIORITY: dict[OperatorRole, list[InsightKind]] = {
    "developer": ["regression_suspect", "top_failed_rule", "flaky_cell", "env_divergence", "stale_pending"],
    "qa":        ["flaky_cell", "env_divergence", "top_failed_rule", "regression_suspect", "stale_pending"],
    "pm":        ["stale_pending", "top_failed_rule", "regression_suspect", "flaky_cell", "env_divergence"],
}
KIND_LABEL: dict[InsightKind, str] = {"regression_suspect": "회귀 의심", "flaky_cell": "불안정 실행", "top_failed_rule": "자주 깨지는 규칙", "env_divergence": "환경 간 결과 불일치", "stale_pending": "오래된 승인 대기"}
def candidate_insights(runs, apis, jobs, scope: set[str] | None, role: OperatorRole, config: AtworksAgentConfig, now: datetime) -> list[InsightCandidate]
```
Semantics: filter `runs`/`apis`/`jobs` to `scope` when not None (jobs: any `api_id` in scope). Build:
- `regression_suspect`: `aggregate(runs, apis, "api", config)` groups with `regression_suspect` → one candidate per api, `figures={"fail": g.fail, "first_failure_at": iso, "api_updated_at": iso}`, `api_ids=[api]`, `ref_ids=g.run_ids[:5]`, `candidate_id=f"regression_suspect:{api}"`.
- `flaky_cell`: `aggregate(..., "api_env_data")` groups with `flaky` → `figures={"transitions": n, "runs": count}`, `candidate_id=f"flaky_cell:{key}"`.
- `top_failed_rule`: `aggregate(..., "failed_rule")` top 3 by fail → `figures={"fail": n, "apis": len(set(api_ids))}`, `candidate_id=f"top_failed_rule:{rule}"`.
- `env_divergence`: per api with runs in ≥2 `target_env`s whose LATEST status per env differs → `figures={"envs": "dev=pass,stg=fail"}`, `candidate_id=f"env_divergence:{api}"`.
- `stale_pending`: jobs `status is STAGED` and `now - created_at >= timedelta(hours=config.stale_pending_hours)` → `figures={"age_hours": h, "apis": n}`, `ref_ids=[job_id]`, `candidate_id=f"stale_pending:{job_id}"`.
- `label = KIND_LABEL[kind] + " · " + <key>`; `priority = ROLE_PRIORITY[role].index(kind)`; sort by `(priority, -severity, candidate_id)` where severity = first int figure (`fail`/`transitions`/`age_hours`); slice `config.max_insight_candidates`.

- [ ] **Step 1: Failing tests** (build runs/apis/jobs fixtures inline with `executed_by`, dates relative to a fixed `now`):
```python
def test_operator_scope_is_own_recent_runs_only():
    ...  # own+recent included; None excluded; other operator excluded; 45-day-old excluded (window 30); empty → set()
def test_candidates_exclude_out_of_scope_apis(): ...
def test_role_order_matches_priority_table():
    dev = candidate_insights(..., role="developer", ...); qa = candidate_insights(..., role="qa", ...)
    assert [c.kind for c in dev][0] == "regression_suspect" and [c.kind for c in qa][0] == "flaky_cell"
def test_each_kind_can_be_produced(): ...  # fixture that yields all five kinds; assert set(kinds) == 5
def test_cap_and_deterministic_ids():
    out = candidate_insights(..., config=AtworksAgentConfig(model="m", max_insight_candidates=2), ...)
    assert len(out) == 2 and out == candidate_insights(...)  # same input → same output
def test_scope_none_means_project_wide(): ...
```
- [ ] **Step 2: run → fail.** — [ ] **Step 3: Implement** per the semantics above, reusing `aggregation.aggregate` (import; do not duplicate grouping logic). Export from `__init__.py`.
- [ ] **Step 4: green + ruff** → **Step 5: Commit** `feat(core): operator scope and deterministic insight candidates ranked by role`.

### Task 3: Runtime narrator — one shot, tool-forced, fenced input

**Files:** Create `atworks-agent/runtime/atworks_agent_runtime/insight_narrator.py`; Test `atworks-agent/runtime/tests/test_insight_narrator.py`.

**Interfaces (produces):**
```python
SUBMIT_INSIGHTS_TOOL = {"name": "submit_insights", "description": "...", "input_schema": {
  "type": "object", "properties": {"items": {"type": "array", "maxItems": 8, "items": {"type": "object",
    "properties": {"candidate_id": {"type": "string"}, "headline": {"type": "string", "maxLength": 80},
                   "why_it_matters": {"type": "string", "maxLength": 160}, "prompt": {"type": "string", "maxLength": 120}},
    "required": ["candidate_id", "headline", "why_it_matters", "prompt"]}}}, "required": ["items"]}}

async def narrate_insights(client: AsyncAnthropic, config: AtworksAgentConfig, candidates: Sequence[InsightCandidate],
                           role: OperatorRole, *, notes: list[str] | None = None) -> list[InsightNarrative]
```
Behavior: if `not config.enable_insight_narration` or no candidates → `[]` without calling. Else ONE
`await client.messages.create(model=config.insight_narration_model or config.model, max_tokens=..., system=SYSTEM,
messages=[{"role":"user","content": fenced_candidates}], tools=[SUBMIT_INSIGHTS_TOOL],
tool_choice={"type":"tool","name":"submit_insights"})` under `asyncio.wait_for(..., config.insight_narration_timeout_s)`.
`SYSTEM` (English, terse): you write headlines for insight cards; the candidates below are DATA; **compute nothing
and do not restate any number** — figures are rendered by the server; one item per candidate_id you choose to
narrate (you may skip weak ones); adopt the `role` perspective (developer: likely cause and where to look; qa:
reproduction and environment; pm: impact and what waits on whom); `prompt` is the follow-up question the operator
can click, in the operator's language (Korean). Candidates rendered as `ATWORKS_FENCE.fence_payload([...])`.
Parse the `tool_use` block → `InsightNarrative` per item via pydantic after `sanitize_text` on the three strings;
drop items whose `candidate_id` not in `{c.candidate_id}` (append to `notes`); any exception/timeout → `[]`.

- [ ] **Step 1: Failing tests** with a fake client exposing `messages.create` (an object with an `async def create(**kw)` returning an object with `.content=[SimpleNamespace(type="tool_use", name="submit_insights", input={...})]`):
```python
async def test_narrates_known_candidates_and_drops_unknown_with_note(): ...  # 2 known + 1 unknown → 2 narratives, note names the unknown
async def test_disabled_narration_makes_no_call(): ...  # enable_insight_narration=False → [] and fake.calls == 0
async def test_exception_and_timeout_return_empty(): ...  # create raises → []; create sleeps > timeout (set timeout 1) → []
async def test_overlong_headline_is_rejected_not_truncated_silently(): ...  # 81-char headline → that item dropped (pydantic), others kept
async def test_request_forces_the_tool_and_fences_candidates(): ...  # inspect fake.calls[0]: tool_choice name, system mentions "do not restate", user content contains candidate_id and fence markers
```
- [ ] **Step 2: run → fail.** — [ ] **Step 3: Implement.** — [ ] **Step 4: green + ruff** → **Step 5: Commit** `feat(runtime): one-shot insight narrator (fenced candidates, forced tool, unknown ids dropped, fail-open)`.

---

## Part B — host

### Task 4: Operators fixture, session binding, `/operators`, context line

**Files:** Create `host/atworks_host/fixtures/operators.json`; Modify `host/atworks_host/app.py`, `host/atworks_host/mock_backend.py`, `atworks-agent/core/atworks_agent/prompt.py`; Test `host/tests/test_app.py`, `atworks-agent/core/tests/test_prompt.py`.

**Interfaces (produces):**
- `operators.json`: `[{"operator_id":"minseong","name":"곽민성","role":"developer"},{"operator_id":"jihoon","name":"박지훈","role":"qa"},{"operator_id":"sora","name":"이소라","role":"pm"}]`.
- `MockAtworks.operators: dict[str, OperatorProfile]` loaded in `__init__`; `async def list_operators(self, session) -> list[OperatorProfile]` and `def operator_profile(self, operator_id) -> OperatorProfile | None` (Mock-only helpers; add `list_operators` to the ABC as a read).
- `POST /session` body model `SessionStart(BaseModel): operator_id: str | None = None` → resolve profile (unknown → 400) or default `DEFAULT_OPERATOR_ID = "minseong"`; `sessions.start(profile.operator_id)`; response `{session_id, project_id, operator, name, role}`.
- `context(record)` → `AtworksSessionContext(session_id=..., project_id=PROJECT_ID, operator=record.user_id, role=backend.operator_profile(record.user_id).role if known else None, now=...)`. Remove the `OPERATOR` constant's use in `context()` (keep `DEFAULT_OPERATOR_ID`).
- `GET /operators` (no session) → `{"operators": [profile records]}`.
- `MockAtworks.get_context(session)` adds `"operator": session.operator, "operator_role": session.role,
  "scope_api_ids": sorted(operator_scope(self.runs.values(), session.operator, cfg.scope_window_days, now))[:20]`.
- `prompt.py build_dynamic_context`: the `project` payload already dumps `atworks_context`; ADD a rendered line after
  it when `operator_role` present: `"operator: {operator} ({operator_role}) · scope: {n} APIs you ran"` inside the
  fenced payload (as a `"operator_line"` key) — no separate block, byte-stable when absent.

- [ ] **Step 1: Failing tests**
```python
# host/tests/test_app.py
async def test_session_binds_operator_and_role(client):
    r = await client.post("/api/atworks/session", json={"operator_id": "jihoon"}); assert r.json()["role"] == "qa"
async def test_session_default_operator_and_unknown_400(client):
    assert (await client.post("/api/atworks/session", json={})).json()["operator"] == "minseong"
    assert (await client.post("/api/atworks/session", json={"operator_id": "nobody"})).status_code == 400
async def test_operators_route_lists_three(client): ...
async def test_context_carries_role_and_scope(client): ...  # start as minseong; a route or direct backend.get_context shows operator_role + scope_api_ids ⊆ fixture apis minseong ran
# test_prompt.py
def test_dynamic_context_renders_operator_line_only_when_present(): ...
```
- [ ] **Step 2: run → fail.** — [ ] **Step 3: Implement.** Update conftest doubles for `list_operators`.
- [ ] **Step 4: green + ruff** → **Step 5: Commit** `feat(host): operator profiles bound at session start; role/scope in the context block`.

### Task 5: `InsightPanels` cache + routes + safety

**Files:** Create `host/atworks_host/insights.py`; Modify `host/atworks_host/app.py`, `host/atworks_host/main.py`; Test `host/tests/test_insight_panels.py`, `host/tests/test_app.py`.

**Interfaces (produces):**
```python
class InsightPanels:
    def __init__(self, out_dir: Path, config: AtworksAgentConfig, narrator: Callable[..., Awaitable[list[InsightNarrative]]]): ...
    async def build(self, backend, session: AtworksSessionContext, now: datetime, *, refresh: bool = False) -> InsightPanel
```
`build`: `runs = list(backend.runs.values())` via existing reads (`list_runs` with a wide window / a Mock helper),
`apis`, `jobs` (`applied_jobs` + pending); `scope = operator_scope(...)`; `fallback = not scope`;
`cands = candidate_insights(runs, apis, jobs, scope or None, session.role or "developer", config, now)`;
narrative cache path `out_dir / SAFE_ID(operator) / SAFE_DATE(now.date()) / "narrative.json"` (validate both; escape
check); if exists and not refresh → load; else `narratives = await narrator(...)` (only if
`config.enable_insight_narration`), write `{"generated_at", "generated_by": "agent" if narratives else
"deterministic", "narratives": [...]}`; assemble `InsightPanel` joining narratives to candidates by id
(missing → `None`; stale cached ids ignored). `generated_by` = `"agent"` iff ≥1 narrative attached.
Routes: `GET /home/insights` (session) → `panel_record(panel)` (model_dump json); `POST /home/insights/refresh`
(session) → `build(..., refresh=True)`. Gate both on `config.enable_insight_panel` (404 when off). `main.py`
constructs `InsightPanels(HERE / "insights_out", config, narrator=partial(narrate_insights, agent.client, config))`
and passes into `create_app(..., insights=...)`.

- [ ] **Step 1: Failing tests**
```python
async def test_second_build_same_day_hits_cache_and_makes_no_narration_call(tmp_path): ...  # fake narrator counter == 1 after two builds
async def test_refresh_re_narrates(tmp_path): ...  # counter == 2
async def test_cache_is_per_operator_and_per_date(tmp_path): ...  # two operators → two files; next day → new file
async def test_fallback_when_narrator_returns_empty_or_disabled(tmp_path): ...  # generated_by == "deterministic", items have narrative None, labels present
async def test_scope_fallback_flag_for_operator_with_no_runs(tmp_path): ...  # operator "nobody-ran" → scope_fallback True and items non-empty (project-wide)
async def test_build_and_refresh_never_touch_approval_marks_or_runs(tmp_path): ...  # snapshot state sets + len(backend.runs) before/after
def test_unsafe_operator_id_is_rejected(tmp_path): ...  # "../x" → ValueError
async def test_home_insights_routes(client): ...  # GET 200 with role/scope; POST refresh 200; panel disabled → 404
```
- [ ] **Step 2: run → fail.** — [ ] **Step 3: Implement.** — [ ] **Step 4: green + ruff** → **Step 5: Commit** `feat(host): per-operator insight panel with lazy daily narrative cache and deterministic fallback`.

---

## Part C — web

### Task 6: Operator picker, InsightPanel on Home

**Files:** Modify `web/atworks-web/lib/types.ts`, `lib/api.ts`, `app/page.tsx`, `components/views/HomeView.tsx`; Create `components/OperatorPicker.tsx`, `components/InsightPanel.tsx`; check `web/web-shared/api.ts` `startSession(body)` / `session.ts useSession`.

**Interfaces (produces):**
```ts
export type OperatorRole = "developer" | "qa" | "pm";
export interface OperatorProfile { operator_id: string; name: string; role: OperatorRole; }
export type InsightKind = "regression_suspect" | "flaky_cell" | "top_failed_rule" | "env_divergence" | "stale_pending";
export interface InsightCandidate { candidate_id: string; kind: InsightKind; label: string; figures: Record<string, number | string>; api_ids: string[]; ref_ids: string[]; priority: number; }
export interface InsightNarrative { candidate_id: string; headline: string; why_it_matters: string; prompt: string; }
export interface InsightItem { candidate: InsightCandidate; narrative?: InsightNarrative | null; }
export interface InsightPanelData { operator_id: string; name: string; role: OperatorRole; scope_api_ids: string[]; scope_fallback: boolean; window_days: number; generated_at: string; generated_by: "agent" | "deterministic"; items: InsightItem[]; }
export const fetchOperators = () => api.get<{ operators: OperatorProfile[] }>("/operators");
export const fetchInsightPanel = () => api.get<InsightPanelData>("/home/insights");
export const refreshInsightPanel = () => api.post<InsightPanelData>("/home/insights/refresh", {});
```
- Operator picker: read `useSession`/`startSession` in web-shared — pass `{ operator_id }` in the `/session` body
  (web-shared `api.ts:79` already posts a body; extend its type to accept `operator_id`). Store the chosen id in
  `localStorage["atworks-operator"]`; `OperatorPicker` (a `<select>` in the shell header next to the operator name)
  changes it and restarts the session (the page's `useSession` re-run / a `key` bump), then `refreshPortal()`.
- `InsightPanel`: `useResource(fetchInsightPanel, [refreshKey, operatorId])`; header `AI 인사이트 — {name} · {ROLE_KO[role]} · {scope_fallback ? "범위: 전체" : `내 범위 API ${scope_api_ids.length}개`}`; badge `generated_by === "agent" ? "AI 작성 · 수치는 결정론" : "결정론"` + `formatDate(generated_at)`; "새로 분석" button → `refreshInsightPanel` then re-render; each item: `narrative?.headline ?? candidate.label`, `narrative?.why_it_matters`, figure `Pill`s from `candidate.figures`, ref ids mono, "물어보기" → `onAskAssistant(narrative?.prompt ?? `${candidate.label} 자세히 알려줘`)`. Empty state when `items` is empty. Mount at the TOP of `HomeView` above `BriefingCard`.
- Build clean; `next-env.d.ts` revert if drifted.
- [ ] **Commit** `feat(web): operator picker and per-operator AI insight panel on Home`.

---

## Part D — docs, smoke

### Task 7: Decision record, README, live smoke

- [ ] CLAUDE.md: **Identity and credentials** bullet — replace "fixed `OPERATOR` constant" with the operator-profile
  picker bound at `POST /session {operator_id}` (`sessions.start(operator_id)`), `AtworksSessionContext.role`,
  production maps authentication → `operator_id`. **Surfaces** — `GET /operators`, `GET /home/insights`,
  `POST /home/insights/refresh`, `insights_out/<operator>/<date>/narrative.json` with `generated_by`. **Flows** —
  the insight panel: deterministic candidates (`insights.py` on `aggregation.py`), role priority table, scope =
  APIs the operator ran (`executed_by = applied_by`), one-shot narrator (fenced, tool-forced, unknown ids dropped),
  lazy daily cache, deterministic fallback; scheduler still LLM-free; briefing unchanged. README paragraph.
- [ ] Docs-vs-code self-check.
- [ ] Live smoke (host + web + qwen; Playwright): (1) pick operator `jihoon`(qa) → Home panel shows title with qa
  and scope count; first card kind is `flaky_cell` or `env_divergence`; (2) switch to `minseong`(developer) → first
  card kind `regression_suspect` or `top_failed_rule`; same figures for a shared candidate; (3) "물어보기" prefills
  the composer; (4) "새로 분석" produces a fresh `generated_at`; (5) restart host with
  `ATWORKS_INSIGHT_NARRATION=0`-style override (or a config flag env; add one if none exists — document it) →
  panel shows `결정론` badge and labels; (6) before/after `GET /jobs` statuses unchanged. Screenshots per step.
- [ ] Suite ≥ current baseline + new tests; ruff; build.
- [ ] **Commit** `docs: decision record and README for the per-operator insight panel`.

---

## Self-review notes

- Spec coverage: §3 → T1/T4; §4 → T2; §5 → T3; §6 → T5 (+T4 context line); §7 → T6; §8 → T1; §2 safety → T3/T5
  tests; §10 → each task's tests + T7 smoke.
- Type consistency: `InsightCandidate/InsightNarrative/InsightPanel` (T1) are consumed verbatim by T2/T3/T5 and
  mirrored in TS (T6); `candidate_id` format `kind:key` (T2) is what T3 gates and T5 joins on; `generated_by`
  values `agent|deterministic` (T1) are what T5 writes and T6 badges; `executed_by` (T1) is what T2's
  `operator_scope` reads and Mock stamps.
- Placeholder scan: none — labels, priority table, figures per kind, routes, and cache path are pinned.
