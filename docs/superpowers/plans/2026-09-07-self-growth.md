# Self-growth Query Engine (1·2단계) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any grouping/filter question over runs is answered on the spot by a structured `QuerySpec` the model picks and the host computes; every question is logged; confirmed vocabulary is shared org-wide; repeated questions become saved Home cards; 👍 answers become regression evals; a Growth view shows what the system learned.

**Architecture:** One new read tool `query_runs(QuerySpec)` compiled by the host to SQL over the scale branch's materialized tables (`rollup_day ⋈ apis`, `rollup_key_day`, new `rollup_operator_day`, and a bounded indexed `runs` path only for `executed_by × other`). A turn-end hook classifies the turn into `ask_log`. Vocabulary reuses `commerce_common.memory` (store protocol, write filter, tier-one selection, render) on a SQLite-backed store keyed by project. A daily LLM-free promoter turns recurring `cluster_key`s into `saved_questions`. 👍 writes commerce-evals-shaped cases; a replay runner guards them.

**Tech Stack:** Python 3.11+, pydantic v2, FastAPI, SQLite (`host/atworks_host/store.py`), `commerce_common` (import-only), Next.js portal (`web/atworks-web`, no unit harness — `npm run build` + Playwright smoke).

**Spec:** `docs/superpowers/specs/2026-09-07-self-growth-design.md` — the authority; conflicts resolve against it.

## Global Constraints

- Branch `feature/self-growth` (from `feature/scale-architecture` 3a02f61). Never touch `refs/`. `main` stays `fa31a1a`. Every commit ends with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. All files LF.
- Model never computes numbers or verdicts; every figure on a card comes from `QueryResult`. `query_runs`, `note_unmet_ask`, `propose_alias` are read/record tools — no ledger, no approval mark, no body.
- Tool schemas are a pure function of config (cache-stable): catalog enums are fixed lists, never built from data.
- Every list route returns `{items, next_cursor, total}` (default limit 50, `ge=1, le=200`), cursor opaque, `total` filter-applied.
- Shared-state changes by humans only, via host routes writing TWO audit rows (`<action>` / `<action>:<ok|blocked|error>`): vocabulary confirm/reject/delete, saved-question hide/unhide. Chat turns write zero audit rows.
- `ask_log.question` is `mask_body`-masked, ≤300 chars. Model context receives only CONFIRMED vocabulary (≤ `vocabulary_max_inject`=8). Vocabulary writes pass `MemoryWriteFilter`; `memory_extract_facts=False` (no free-fact extraction). `save_memory`/`recall_memories` stay absent.
- Exact values: `Dimension` = `api, path_segment_1, path_segment_2, path_prefix_2, method, api_group, target_env, test_data_label, failed_rule, http_status, executed_by, day, week`; `Measure` = `runs, pass, fail, error, non_pass, fail_rate, apis, transitions, p95_duration_ms`; `dimensions ≤ 2`, `measures 1..5`, `limit 1..50` default 20, `order_by` default `non_pass` desc; `fail_rate = (fail+error)/runs` 4 dp; config `slo_query_ms=300`, `ask_log_retention_days=365`, `promote_window_days=7`, `promote_min_users=3`, `promote_min_asks=5`, `vocabulary_max_inject=8`, `vocabulary_cooldown_days=30`, `vocabulary_auto_demote_rejections=3`.
- SLO: `query_runs` rows < `slo_query_ms` on the reduced scale set; `SLO_ASSERT` covers them. Default suite (`pytest -q`) excludes `scale` and `evals` marks and stays green (790 at BASE); `ruff check atworks-agent host scripts`; `npm run build` clean.
- Anchors (locate by NAME): executor handlers `atworks-agent/core/atworks_agent/executor.py` (`_aggregate_runs` is the pattern; `build_memory`), tools `atworks-agent/core/atworks_agent/tools/registry.py` (`build_tools`), presentation components `atworks-agent/core/atworks_agent/enrichment.py` (`PresentationComponent(name=…, component=…, payload_model=…, enrich=…)` list ~L357), prompt `atworks-agent/core/atworks_agent/prompt.py` (`build_dynamic_context`), config `absent_tools`, orchestrator `atworks-agent/runtime/atworks_agent_runtime/orchestrator.py` (`stream_turn`, `update_memory`), host `host/atworks_host/app.py` (`create_app`, `/chat` route, `host_action` + `_Surface`), `store.py` (`_transaction`, `_migrate_columns`, `_api_rows`, `_materialize`, `rebuild_materialized`, `audit`), `materialize.py` (`rollup_delta`, `OperatorApiDelta`), `scheduler.py` tick tail (`retention.maybe_run`), `retention.py`, web `web/atworks-web/components/generative/index.tsx` (card switch), `lib/api.ts` (`api.getPage`), `lib/types.ts`, `app/page.tsx` nav.

---

## File Structure

| File | Responsibility |
|---|---|
| `atworks-agent/core/atworks_agent/types.py` | `QueryFilters`, `QuerySpec`, `QueryRow`, `QueryResult`, `AskEntry`, `VocabularyEntry`, `SavedQuestion`, `GrowthSummary` |
| `atworks-agent/core/atworks_agent/catalog.py` (new) | Dimension/measure metadata (Korean labels, source, help), `catalog_hint()`, `title_for_spec()`, `cluster_key_for_spec()` |
| `atworks-agent/core/atworks_agent/asklog.py` (new) | Deterministic turn classification → `AskEntry` |
| `atworks-agent/core/atworks_agent/vocabulary.py` (new) | Term normalization, fragment validation, `match_terms(message, entries)` |
| `host/atworks_host/query_sql.py` (new) | `compile_query(spec, *, now, tz) -> CompiledQuery` (SQL + params + source kind) |
| `host/atworks_host/store.py` | new columns/tables, `query()`, ask_log/vocabulary/saved_questions/memory_facts methods |
| `host/atworks_host/memory_store.py` (new) | `SqliteMemoryStore(MemoryStore)` over the Store |
| `host/atworks_host/promoter.py` (new) | `Promoter.maybe_run(now)` |
| `host/atworks_host/evals_writer.py` (new) | 👍 → `evals/cases/*.json` |
| `evals/run_evals.py` (new), `host/tests/test_evals.py` | replay/live runner, `evals` marker |
| `web/atworks-web/components/generative/QueryTableCard.tsx`, `components/views/GrowthView.tsx`, `components/SavedQuestionsCard.tsx` | UI |

---

### Task 1: Types, catalog, config

**Files:** Modify `atworks-agent/core/atworks_agent/types.py`, `config.py`; Create `atworks-agent/core/atworks_agent/catalog.py`; Test `atworks-agent/core/tests/test_query_types.py`.

**Interfaces (produces):**
```python
HttpMethod = Literal["GET","POST","PUT","PATCH","DELETE"]
Dimension = Literal["api","path_segment_1","path_segment_2","path_prefix_2","method","api_group","target_env","test_data_label","failed_rule","http_status","executed_by","day","week"]
Measure = Literal["runs","pass","fail","error","non_pass","fail_rate","apis","transitions","p95_duration_ms"]
class QueryFilters(BaseModel): ...           # spec §3 verbatim (extra=forbid)
class QuerySpec(BaseModel): ...              # spec §3 verbatim; validator: window_days xor (since/until) allowed both None
class QueryRow(BaseModel):
    keys: dict[str, str | None]              # dimension -> value ('' label sentinel -> None)
    measures: dict[str, float | int | None]  # measure -> value; with compare: measure_prev / measure_delta too
    api_ids: list[str] = Field(default_factory=list, max_length=20)
    run_ids: list[str] = Field(default_factory=list, max_length=5)
class QueryResult(BaseModel):
    spec: QuerySpec; rows: list[QueryRow]; total_groups: int; population: int
    window: tuple[datetime, datetime]; source: Literal["rollup_day","rollup_key_day","rollup_operator_day","runs"]
    turn_id: str | None = None
AskOutcome = Literal["answered","partial","unmet","action"]
UnmetReason = Literal["no_dimension","no_evidence","out_of_scope","refused"]
class AskEntry(BaseModel): seq: int | None; at: datetime; session_id: str; operator: str; role: str | None; question: str(≤300);
    intent: str; spec: QuerySpec | None; outcome: AskOutcome; unmet_reason: UnmetReason | None; wanted: str | None(≤200);
    tool_calls: int; cards: int; feedback: Literal["up","down"] | None; cluster_key: str; turn_id: str
class VocabularyEntry(BaseModel): term: str(≤40); fragment: QueryFilters; status: Literal["pending","confirmed","rejected"];
    proposed_by: str; proposed_at: datetime; confirmed_by: str | None; confirmed_at: datetime | None; confirmations: int; uses: int; rejections: int; cooldown_until: datetime | None
class SavedQuestion(BaseModel): id: str; cluster_key: str; spec: QuerySpec; title: str(≤120); created_at: datetime; status: Literal["active","hidden"]; uses: int; last_used_at: datetime | None; source_users: int; source_asks: int
class GrowthSummary(BaseModel): asks_total: int; answered: int; partial: int; unmet: int; up: int; down: int; new_terms: int; new_saved: int; unmet_clusters: list[dict]  # {cluster_key, reason, count, last_at, example}
```
`catalog.py`: `DIMENSIONS: dict[Dimension, DimInfo(label_ko, help_ko, source)]`, `MEASURES: dict[Measure, MeasureInfo(label_ko, help_ko)]`, `catalog_hint() -> str` (one line per item, deterministic bytes), `title_for_spec(spec) -> str` (e.g. `"실패·에러 · 경로 2조각별 · 30일 · 상위 20"`), `cluster_key_for_spec(spec) -> str` = `"dims=" + ",".join(sorted(dims)) + "|measures=" + … + "|filters=" + ",".join(sorted(set filter field names)) + ("|compare" if …)`.
Config fields (exact defaults in Global Constraints) + `enable_query_runs: bool = True`, `enable_growth: bool = True`, `memory_extract_facts: bool = False`; `absent_tools` drops `query_runs`, `present_query_table`, `note_unmet_ask`, `propose_alias` when `enable_query_runs=False`.

- [ ] Tests: spec round-trip; `extra=forbid`; 3 dimensions → ValidationError; `limit=51` → error; `cluster_key_for_spec` ignores filter VALUES (two specs with different `path_contains` share a key); `title_for_spec` deterministic Korean; `catalog_hint()` lists all 13 dimensions and 9 measures; `absent_tools` gate.
- [ ] **Commit** `feat(core): QuerySpec types, catalog, growth config`.

### Task 2: Store — path segments, `rollup_operator_day`, query compiler

**Files:** Modify `host/atworks_host/store.py`, `atworks-agent/core/atworks_agent/materialize.py`; Create `host/atworks_host/query_sql.py`; Test `host/tests/test_query.py`, extend `host/tests/test_ingest.py`.

**Interfaces (produces):**
```python
# materialize.py
class OperatorDayDelta(BaseModel): day: str; operator_id: str; count: int; passed: int; fail: int; error: int
def rollup_delta(...) -> tuple[list[RollupRow], list[CellState], list[ApiWatermark], list[OperatorApiDelta], list[OperatorDayDelta]]  # 5th element; update all callers/tests
# query_sql.py
@dataclass class CompiledQuery: sql: str; params: list; source: str; group_columns: list[str]
def compile_query(spec: QuerySpec, *, now: datetime, tz: str, previous: bool = False) -> CompiledQuery
def path_segments(path: str) -> tuple[str | None, str | None, str | None]   # seg1, seg2, prefix2
# store.py
def query(self, spec: QuerySpec, *, now: datetime, tz: str) -> QueryResult
```
- `apis` gains `path_segment_1, path_segment_2, path_prefix_2` (computed in `_api_rows`; `_migrate_columns` adds + backfills from `path`); indexes on each + `(method)`.
- `rollup_operator_day(day, operator_id) PK → count, pass, fail, error`, IDX `(operator_id, day)`; folded in `_materialize` (INSERT … ON CONFLICT add), cleared/rebuilt by `rebuild_materialized`; runs with `executed_by IS NULL` skipped.
- Source selection exactly per spec §4 table; `executed_by` FILTER forces `runs` source. `compare_previous_window` → run compiled query twice (previous window = `[since-(until-since), since)`), outer-join in Python on keys (≤ limit rows each), producing `<m>_prev`, `<m>_delta`.
- Samples: per returned row ≤20 `api_ids`, ≤5 newest `run_ids` via indexed queries; `population` = filtered run count (rollup sum or runs COUNT); `total_groups` = COUNT over the grouped subquery.
- [ ] Golden oracle test: `_oracle(spec, runs, apis)` pure-python over `fetch_runs` — assert equality for `runs/pass/fail/error/non_pass/fail_rate/apis` across: no-dim; each single dimension; pairs `(path_segment_2, target_env)`, `(method, http_status)`, `(executed_by, api)`, `(day, target_env)`; filters `path_contains`, `path_prefix`, `method`, `executed_by`, `scope_operator`, `status`; `compare_previous_window` deltas; `order_by`/`limit`/tie-break. `transitions`/`p95` asserted against the rollup definitions. Run on fixtures AND a 2k-run synthetic seed.
- [ ] Property test extension: `rollup_operator_day` sums == per-operator recompute; idempotent re-ingest.
- [ ] EXPLAIN tests: `path_segment_2` path uses the apis index; `executed_by×api` path shows `SEARCH runs USING INDEX idx_runs_executed_by…`.
- [ ] **Commit** `feat(store): QuerySpec compiler over rollups, api path segments, operator-day rollup`.

### Task 3: ABC + Mock + doubles, `query_runs` tool, `query_table` card, web card

**Files:** Modify `backend.py`, `host/atworks_host/mock_backend.py`, `atworks-agent/core/tests/conftest.py`, `atworks-agent/runtime/tests/conftest.py`, `host/tests/test_scheduler_reports.py`, `tools/registry.py`, `executor.py`, `enrichment.py`, `types.py` (`AtworksSessionState.last_query_result`, `current_turn_id`), `web/atworks-web/lib/types.ts`, `components/generative/index.tsx`; Create `components/generative/QueryTableCard.tsx`; Test `atworks-agent/core/tests/test_executor.py`, `test_presentation.py`, `host/tests/test_mock_backend.py`.

**Interfaces (produces):**
```python
# backend.py (ABC)
async def query_runs(self, session, spec: QuerySpec) -> QueryResult   # docstring: REST computes over its own rollups; must honour source rules, samples, total/population semantics
# tool schema (registry.py): "query_runs" input = QuerySpec JSON schema with enums from Dimension/Measure; description carries catalog_hint() + 2 example specs
# executor.py
async def _query_runs(self, tool_input) -> ToolOutcome   # QuerySpec.model_validate → InvalidToolArgument on error; api_ids filter must be seen (provenance); result → state.last_query_result; remember samples via get_apis/runs_by_ids (≤PROVENANCE_CAP); fenced envelope {rows(≤limit, samples trimmed to 3), total_groups, population, source, spec_summary, note}
# enrichment.py
class PresentQueryTablePayload(BaseModel): title: str(≤80); note: str | None(≤200)
component "query_table": payload {title, note, columns[], rows[], total_groups, population, window, source, spec, spec_summary(ko), turn_id, pending_alias: {term, fragment_summary} | None}
```
- Web `QueryTableCard.tsx`: table (dimension columns + measures, `_prev/_delta` when present), footer: spec summary (Korean sentence) + collapsible JSON, `population`/`total_groups`, sample links `?attach=run:`, placeholders for 👍👎 and alias buttons (wired in T6/T8 — render nothing when fields absent). `lib/types.ts` `QueryTablePayload`.
- [ ] Tests: invalid spec → error; unseen `api_ids` → provenance error; samples remembered; `PresentationRefused` when no `last_query_result`; card payload numbers equal result; doubles implement `query_runs` (conformance test).
- [ ] `npm run build` clean.
- [ ] **Commit** `feat(core,host,web): query_runs tool and query_table card over the QuerySpec engine`.

### Task 4: `ask_log` + `note_unmet_ask` + turn hook + routes

**Files:** Create `atworks-agent/core/atworks_agent/asklog.py`; Modify `store.py`, `backend.py`, `mock_backend.py`, doubles, `registry.py`, `executor.py`, `orchestrator.py` (`stream_turn(turn_id=...)`), `app.py`; Test `atworks-agent/core/tests/test_asklog.py`, `host/tests/test_ask_log.py`.

**Interfaces:**
```python
# asklog.py
def classify_turn(*, question: str, tool_names: list[str], cards: int, unmet: tuple[UnmetReason, str, str] | None, spec: QuerySpec | None, policy: MaskingPolicy, session, turn_id) -> AskEntry
# executor: tool note_unmet_ask {reason, summary, wanted} -> records state.turn_unmet; ToolOutcome "recorded"
# state: turn_tool_names: list[str]; turn_cards: int; turn_unmet; last_query_result — reset at stream_turn start
# ABC: record_ask(session, entry) -> AskEntry ; list_asks(session, outcome, cursor, limit) -> Page[AskEntry] ; growth_summary(session, since) -> GrowthSummary
# store: ask_log DDL §6 + insert_ask/list_asks/ask_counts/unmet_clusters/set_feedback(turn_id, vote)
# app.py /chat: turn_id = uuid4().hex; after the stream finishes (same place sessions.save runs) → backend.record_ask(...) unless enable_growth=False; failure logged, never raised
# routes: GET /ask-log?outcome&cursor&limit ; GET /growth/summary?days=7
```
Outcome rules exactly per spec §6 (`action` when any stage_*/apply_*/discard_* tool ran). Intent map: search_apis/get_api→lookup; list_runs/get_run/rank_failed_runs→status; aggregate_runs/query_runs→aggregate (+trend when dims include day/week, compare when compare_previous_window); stage_*/apply_*→action; navigate/highlight→meta; unmet→from tool.
- [ ] Tests: the four outcomes; masked question (`900101-1234567` → `***`); cluster keys deterministic; page envelope; summary counts; hook writes exactly one row per turn even when the stream raises midway (row `partial`).
- [ ] **Commit** `feat(core,host): ask_log with deterministic turn classification and note_unmet_ask`.

### Task 5: Skill rules, prompt catalog block, live smoke 1

**Files:** Modify `atworks-agent/skills/failed-triage/SKILL.md`, `api-lookup/SKILL.md`, `prompt.py` (static section "Query catalog" = `catalog_hint()` — cache-stable), `gates.py` if a reminder is needed; Test `atworks-agent/core/tests/test_prompt.py`, `test_skills_load.py`; Smoke script in scratchpad `growth_shots.py` phase 1.

- Skill rules verbatim from spec §5 (three bullets). Prompt: one static paragraph + catalog list; assert byte-stability across two builds.
- [ ] Live smoke (host restarted on `scripts/scale/out/demo` via `ATWORKS_STORE_PATH` + `ATWORKS_FIXTURES_DIR`, real model, one page/session): the five handover questions → each yields a `query_table`/card (PASS/FAIL per step, screenshots); an impossible question ("서버 로그 보여줘") → `note_unmet_ask` row in `/ask-log`. A model-behaviour miss is retried once with clearer phrasing, then recorded.
- [ ] **Commit** `feat(agent): query catalog in prompt, skills never end on "unsupported"`.

### Task 6: Vocabulary — SQLite memory store, `propose_alias`, confirm/reject, injection

**Files:** Create `host/atworks_host/memory_store.py`, `atworks-agent/core/atworks_agent/vocabulary.py`; Modify `store.py`, `backend.py`, `mock_backend.py`, doubles, `registry.py`, `executor.py`, `prompt.py` (`build_dynamic_context(vocabulary=...)`), `orchestrator.py` (`stream_turn(vocabulary=...)`, skip `update_memory` extraction when `memory_extract_facts=False`), `app.py` (routes + injection), `main.py` (wire `SqliteMemoryStore`, `enable_memory=True`), `enrichment.py` (`pending_alias` on `query_table`), web `QueryTableCard.tsx` (예/아니오 buttons → `POST /vocabulary/{term}/confirm|reject`), `lib/api.ts`; Test `host/tests/test_memory_store.py`, `test_vocabulary.py`, `atworks-agent/core/tests/test_vocabulary.py`.

**Interfaces:**
```python
class SqliteMemoryStore:  # implements commerce_common.memory.MemoryStore; passes check_memory_store
    def __init__(self, store: Store) ...
# vocabulary.py
def normalize_term(term: str) -> str            # NFKC, lower, collapse spaces, ≤40
def fragment_from_tool(input: dict) -> QueryFilters   # partial filters only; rejects empty
def match_terms(message: str, entries: Sequence[VocabularyEntry]) -> list[VocabularyEntry]   # substring on normalized term, longest first, ≤ cap
# ABC
async def propose_alias(session, term, fragment) -> VocabularyEntry     # MemoryWriteRejected → returns None + reason (tool result "rejected: looks like personal data")
async def list_vocabulary(session, status, cursor, limit) -> Page[VocabularyEntry]
async def confirm_alias / reject_alias / delete_alias(session, term) -> VocabularyEntry | None
async def confirmed_vocabulary(session) -> list[VocabularyEntry]      # cached per process, invalidated on write
async def note_vocabulary_use(session, terms: list[str]) -> None       # uses+1 ; on 👎 rejections+1 (T8)
# tool propose_alias {term, fragment(QueryFilters subset), note}
# dynamic context payload["vocabulary"] = [{"term","means": fragment_summary_ko, "fragment": {...}}] ≤ vocabulary_max_inject
# routes: GET /vocabulary?status&cursor&limit ; POST /vocabulary/{term}/confirm|reject|delete  (audit pair via a `growth_action` helper — same shape as host_action minus approval marks)
```
Auto-demotion: `rejections ≥ vocabulary_auto_demote_rejections and rejections > confirmations` → status pending. Reject → `cooldown_until = now + vocabulary_cooldown_days`; a pending proposal for a term in cooldown is ignored.
- [ ] Tests: `check_memory_store(SqliteMemoryStore)` passes; PII-shaped term/fragment rejected (`MemoryWriteRejected`), nothing stored; pending visible only to proposing session's card, not in another session's context; confirm → appears in another operator's `build_dynamic_context` payload; reject → cooldown blocks re-proposal; confirm/reject write 2 audit rows; `update_memory` not called when `memory_extract_facts=False` (spy); `save_memory`/`recall_memories` absent from tool list.
- [ ] `npm run build`.
- [ ] **Commit** `feat(core,host,web): org-level vocabulary on commerce_common.memory — propose, confirm, inject`.

### Task 7: Promoter + saved questions + Home card

**Files:** Create `host/atworks_host/promoter.py`, `web/atworks-web/components/SavedQuestionsCard.tsx`; Modify `store.py`, `backend.py`, `mock_backend.py`, doubles, `scheduler.py` (`promoter.maybe_run(now)` after retention), `main.py`, `app.py`, `components/views/HomeView.tsx`, `lib/api.ts`, `lib/types.ts`; Test `host/tests/test_promoter.py`, `test_app.py`.

**Interfaces:**
```python
class Promoter:
    def __init__(self, store, config, *, clock=None) ...
    async def run(self, now) -> dict[str, int]        # {"promoted": n, "skipped_downvoted": m}
    async def maybe_run(self, now) -> dict | None     # retention_state 'promote:<YYYY-MM-DD>' guard (briefing_tz)
# store: saved_questions DDL §8; promote_candidates(since, min_users, min_asks) -> list[(cluster_key, spec_json, users, asks, down_ratio)]; insert_saved_question; list_saved_questions(status, cursor, limit); set_saved_status; bump_saved_use
# ABC: list_saved_questions(session, status, cursor, limit) -> Page[SavedQuestion]; run_saved_question(session, id) -> QueryResult; set_saved_question_status(session, id, status) -> SavedQuestion
# routes: GET /saved-questions ; GET /saved-questions/{id}/run ; POST /saved-questions/{id}/hide|unhide (audit pair)
```
- Home card "저장 질문" (≤5 by uses) → click runs → renders the result with the same table markup as `QueryTableCard` (share a `QueryTable` presentational component).
- [ ] Tests: thresholds (2 users/5 asks ✗, 3/4 ✗, 3/5 ✓), down-vote ratio > 0.5 skipped, no re-promotion (UNIQUE), deterministic title, daily guard, scheduler order (retention → promoter), route envelopes + audit rows on hide.
- [ ] **Commit** `feat(host,web): promote recurring questions into saved Home cards (LLM-free)`.

### Task 8: Feedback → evals + runner

**Files:** Create `host/atworks_host/evals_writer.py`, `evals/run_evals.py`, `evals/cases/.gitkeep`, `host/tests/test_evals.py`; Modify `store.py` (`set_feedback`), `app.py` (`POST /feedback`), `pytest.ini` (marker `evals`, addopts `-m "not scale and not evals"`), `enrichment.py`/`QueryTableCard.tsx` (👍👎 buttons post `{turn_id, vote}`), `lib/api.ts`; Test `host/tests/test_feedback.py`.

- `POST /feedback {turn_id, vote: up|down}` → `ask_log.feedback` (overwrite); `down` + turn used vocabulary terms → `note_vocabulary_rejection`. `up` + `answered` + spec → `evals_writer.write_case(entry) -> Path` (schema in spec §9; `turns` = masked question; `expected.spec_equals` = dimensions, measures, sorted filter kinds).
- Runner: replay mode validates each case's spec against the catalog (pydantic) and executes it on a fixture Store (shape checks); live mode (`ATWORKS_EVAL_LIVE=1`) runs the turn through `AtworksAgent` with the real client and grades `calls_tool`/`spec_equals`/`ui_components`/`never_calls`/`max_tool_calls`, one retry. `pytest -m evals` wraps replay; live only when env set.
- [ ] Tests: vote overwrite; 👍 writes a file with the exact schema; 👎 writes none; replay passes on a generated case; removing a dimension from the catalog makes replay fail (monkeypatch `Dimension` args) — proves the guard.
- [ ] **Commit** `feat(host,evals): feedback votes, eval cases from upvoted answers, replay runner`.

### Task 9: Growth view

**Files:** Create `web/atworks-web/components/views/GrowthView.tsx`; Modify `app/page.tsx` (nav item `growth`, view switch, screen_state view list), `lib/types.ts` (`PortalViewId` + `"growth"`), `lib/api.ts` (fetchers: `fetchGrowthSummary`, `fetchVocabulary`, `actOnVocabulary`, `fetchSavedQuestions`, `actOnSavedQuestion`, `fetchAskLog`), `lib/useScreenFocus.ts` only if a type union needs `growth`; Test: `npm run build`; host `test_app.py` unchanged.

- Four tabs per spec §10; tables use the paged `Pager`; row `data-ref="vocab:<term>"` / `saved:<id>`; buttons post the T6/T7 routes; "미충족 질문" shows cluster, reason label, count, last_at, masked example.
- [ ] `npm run build` clean; manual: tabs render on the demo dataset host.
- [ ] **Commit** `feat(web): Growth view — vocabulary, saved questions, unmet clusters, weekly summary`.

### Task 10: Bench/SLO rows, retention step, docs, live smoke 2, push

**Files:** Modify `scripts/scale/bench.py` (rows `query_runs path_segment_2`, `query_runs method×env`, `query_runs failed_rule`, `query_runs executed_by×api`, `query_runs compare`), `host/tests/test_scale.py` (`SLO_ASSERT` rows), `retention.py` (`ask_log` older than `ask_log_retention_days` deleted, step row), `CLAUDE.md` (new **Self-growth** bullet: QuerySpec catalog + source rules, ask_log, vocabulary via memory, promoter, evals, Growth view, safety clauses, REST obligations for `query_runs`), `README.md` (paragraph + how to test), `docs/ai-features-overview.html` if it lists tools; Smoke `growth_shots.py` phase 2.

- [ ] Bench on reduced set: all query rows < 300 ms (compare row may be documented at ≤ 600 ms if needed — say so). `pytest -m scale` green with new rows asserted.
- [ ] Live smoke 2 (demo dataset, real model): "결제 계열 실패" → pending alias card → confirm click → second session (another operator) uses it without asking; seed `ask_log` to threshold → run promoter → Home saved card; 👍 → eval file exists and `pytest -m evals` passes; `GET /audit` shows pairs for confirm; job statuses unchanged throughout.
- [ ] Docs-vs-code self-check list in the report.
- [ ] **Commit** `docs: decision record for the self-growth engine` then `git push -u origin feature/self-growth` (controller pushes after the final whole-branch review + fix wave).

---

## Self-review notes

- Spec coverage: §3 → T1/T2; §4 → T2; §5 → T3/T5; §6 → T4; §7 → T6; §8 → T7; §9 → T8; §10 → T9; §11 → T2/T4/T6/T7; §12 → T10; §14 → each task's tests + T5/T10 smokes; §16 → T6 (memory), T8 (evals schema).
- Type consistency: `QuerySpec`/`QueryResult` (T1) are the ABC shape (T3), compiled in T2, logged in T4, replayed in T8, run by saved questions in T7; `AskEntry.turn_id` = `/chat`'s `turn_id` = card `turn_id` = `/feedback` key; `VocabularyEntry.fragment: QueryFilters` is what `propose_alias` validates and what injection renders.
- Placeholder scan: none — enums, defaults, thresholds, DDL, routes, outcome rules pinned.
