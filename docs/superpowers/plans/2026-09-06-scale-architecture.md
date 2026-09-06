# Incremental-Accumulation Scale Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every chat tool, card, panel, report and page correct and fast when runs accumulate at
~10k/day for months over a 50k-API catalogue: paged ABC contract with totals, ingest-time
materialization (current state, daily rollups, watermarks, operator index), SQLite-backed Mock as the
Java schema blueprint, retention tiers + capture-time masking, append-only audit log, web paging, and a
synthetic 2M-run harness that asserts SLOs.

**Architecture:** The single ingest point (`record_execution`, once per slot) writes runs + masked
bodies and updates `current_state` / `rollup_day` / `api_watermark` / `operator_api` in one
transaction. All reads go through a paged/aggregate ABC (`Page[T]{items,next_cursor,total}`, keyset
cursors, `aggregate_runs` over rollups, `watermarks`, `current_state`, `operator_scope`,
`count_runs(api_id)`, windowed `simulate_rule`). The Mock becomes SQLite (stdlib `sqlite3`, indexes,
GROUP BY) loaded from the same fixtures. Model-facing caps are unchanged; `run_record` drops bodies;
list results carry `total`. Retention (hot 180d → cold archive, bodies 90d, insights cache 30d, session
TTL 24h) runs LLM-free at the scheduler tail. Web pages with "N개 중 M개 · 다음".

**Tech Stack:** Python 3.11, pydantic v2, FastAPI, stdlib `sqlite3` (3.49), Next.js/TS web, pytest
(`-m scale` marker), ruff.

**Spec:** `docs/superpowers/specs/2026-09-06-scale-architecture-design.md`
**Branch:** `feature/scale-architecture` — every commit here; push the BRANCH at the end, never merge to main.

## Global Constraints

- **Numbers are deterministic and now materialized at ingest**: every figure a card/panel/briefing shows
  comes from `rollup_day` / `current_state` / `api_watermark` / `count_runs` — never from a 2000-run
  sample. A property test must prove rollups equal a from-scratch recomputation.
- **Bodies never reach the model.** `run_record` has no `response_body`; it has `has_body: bool`. Bodies
  are stored **masked** (`mask_body`) and only readable via `get_body` (parity path, 90-day window).
- **No silent truncation.** Every list read returns `Page[T]` with a true `total` (filter-applied) and an
  opaque `next_cursor`; aggregates are exact sums. `aggregate_runs`'s `truncated` flag is removed; the
  card population = `total`.
- **Model caps unchanged**: fence 12k, tool `limit` ≤200 (`clamp_limit`), digest 8, groups 12, highlight 8,
  screen 40, `PROVENANCE_CAP` 200 — and `last_listed_run_ids` is capped at 200 too.
- **Keyset cursors only** (`executed_at DESC, run_id`), opaque base64 JSON; no OFFSET anywhere.
- **Retention values (config):** `retention_hot_days=180`, `retention_body_days=90`,
  `retention_cold_until: date | None = None` (None = keep archive forever), `insights_cache_days=30`,
  `session_idle_hours=24`. Cold = archive table, never delete runs; delete only bodies and derived caches.
- **Masking** at capture: default rules for 계좌번호/주민등록번호/카드번호/전화/이메일 (reuse
  `NAMED_FORMATS` patterns where they exist); `disabled_groups` skip body capture → parity falls back to
  status compare with a note.
- **Scheduler stays LLM-free**; ingest, retention and briefing run there. `execute_job_once` must never
  block the event loop for a whole matrix: batches with `await` + `asyncio.Semaphore(max_concurrency)`.
- **Audit log** is append-only; written only by host approval routes and `apply_*`; never by chat.
- **Scale targets / SLOs (config, asserted by `pytest -m scale`):** 50,000 APIs; 2,000,000 hot runs
  (180d, avg 10k/day, one 50k peak day); 500 operators. `get_context` <50ms, `list_runs` page <200ms,
  `/home/insights` deterministic part <500ms, `aggregate_runs` <300ms, `simulate_rule(30d)` <1s, briefing
  <5s, SSE latency during a 400-run execution <100ms, daily retention <30s.
- Python 3.11+; `.venv/Scripts/python.exe -m pytest -q` and `ruff check atworks-agent host` from repo root;
  `pytest -m scale` is opt-in (excluded by default via marker config); web `npm run build` in
  `web/atworks-web`. Never edit `refs/`. Commit trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File structure

- `atworks-agent/core/atworks_agent/types.py` — `Page`, `RunsQuery`, `AggregateQuery`, `CellState`,
  `ApiWatermark`, `ScopeSummary`, `AuditEntry`, `MaskingRule`/`MaskingPolicy`, `RunResult.day`,
  `RunResult.api_method/api_path` (optional labels), `JobSpec.run_count/recent_run_ids`.
- `atworks-agent/core/atworks_agent/config.py` — retention, masking, scale/SLO settings.
- `atworks-agent/core/atworks_agent/backend.py` — new/changed ABC reads with REST obligations.
- `atworks-agent/core/atworks_agent/cursor.py` (new) — keyset cursor encode/decode.
- `atworks-agent/core/atworks_agent/masking.py` (new) — `mask_body`, default rules.
- `atworks-agent/core/atworks_agent/materialize.py` (new) — pure functions that compute rollup/current-state/
  watermark deltas from a batch of runs (used by the Mock and by the property test).
- `atworks-agent/core/atworks_agent/{executor.py,serialization.py,insights.py,selection.py,aggregation.py}` — adapt.
- `host/atworks_host/store.py` (new) — SQLite DDL, indexes, loaders, query helpers.
- `host/atworks_host/mock_backend.py` — SQLite-backed reads + ingest transaction.
- `host/atworks_host/{scheduler.py,reports.py,briefing.py,insights.py,app.py,main.py,retention.py (new),audit.py (new)}`.
- `scripts/scale/generate.py`, `scripts/scale/bench.py` (new); `pyproject`/`pytest.ini` marker `scale`.
- Web: `web/web-shared/api.ts`, `web/atworks-web/lib/{api.ts,types.ts}`, `components/views/*`, `components/Pager.tsx` (new), `app/page.tsx`.
- Docs: `CLAUDE.md`, `README.md`.

---

## Part A — P0 contract

### Task 1: Core types and config

**Files:** Modify `types.py`, `config.py`, `__init__.py`; Test `atworks-agent/core/tests/test_types.py`, `test_config.py`.

**Interfaces (produces):**
```python
T = TypeVar("T")
class Page(BaseModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int = 0
RunStatusFilter = Literal["all", "pass", "fail", "error", "non_pass"]
GroupBy = Literal["api", "failed_rule", "http_status", "env", "api_env_data"]   # already exists in aggregation — reuse
class RunsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    since: datetime | None = None; until: datetime | None = None
    status: RunStatusFilter | None = None; api_id: str | None = None; executed_by: str | None = None
    cursor: str | None = None; limit: int = Field(default=50, ge=1, le=200)
class AggregateQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    since: datetime | None = None; until: datetime | None = None
    group_by: GroupBy; scope_api_ids: list[str] | None = None; limit: int = Field(default=50, ge=1, le=500)
class CellState(BaseModel): api_id: str; target_env: str; test_data_label: str | None; run_id: str; status: RunStatus; executed_at: datetime; transitions_total: int
class ApiWatermark(BaseModel): api_id: str; last_pass_at: datetime | None; first_non_pass_at: datetime | None; last_non_pass_at: datetime | None; latest_status: RunStatus | None
class ScopeSummary(BaseModel): api_ids: list[str] = Field(default_factory=list, max_length=100); total: int = 0
class AuditEntry(BaseModel): seq: int; at: datetime; operator: str; action: str; target_kind: str; target_id: str; session_id: str
class MaskingRule(BaseModel): name: str = Field(max_length=40); pattern: str = Field(max_length=200); replacement: str = Field(default="***", max_length=20)
class MaskingPolicy(BaseModel): rules: list[MaskingRule] = Field(default_factory=list, max_length=50); disabled_groups: list[str] = Field(default_factory=list, max_length=100)
# RunResult: + day: str | None = None (YYYY-MM-DD local), api_method: str | None = None, api_path: str | None = None
# JobSpec: + run_count: int = 0, recent_run_ids: list[str] = Field(default_factory=list, max_length=50)  (keep run_ids for compatibility this task; Task 5 stops growing it)
```
Config: `retention_hot_days=180`, `retention_body_days=90`, `retention_cold_until: date | None = None`,
`insights_cache_days=30`, `session_idle_hours=24`, `masking_enabled=True`, `masking_disabled_groups: tuple[str,...]=()`,
`scale_apis=50_000`, `scale_runs=2_000_000`, `scale_operators=500`, `slo_get_context_ms=50`,
`slo_list_runs_ms=200`, `slo_insights_ms=500`, `slo_aggregate_ms=300`, `slo_simulate_rule_ms=1000`,
`slo_briefing_ms=5000`, `slo_sse_latency_ms=100`, `slo_retention_ms=30_000`. `max_concurrency` stays (now used).

- [ ] Tests: `Page[RunResult]` round-trips; `RunsQuery(limit=201)` rejected, `extra` rejected; config defaults; `RunResult.day` optional.
- [ ] Implement; export. **Commit** `feat(core): paged/aggregate query types, retention+masking+scale config`.

### Task 2: Keyset cursor + ABC signature change + doubles + dict-Mock adaptation (suite stays green)

**Files:** Create `cursor.py`; Modify `backend.py`, `host/atworks_host/mock_backend.py`, conftest doubles (`atworks-agent/core/tests/conftest.py`, `atworks-agent/runtime/tests/conftest.py`, `host/tests/test_scheduler_reports.py`); Test `test_cursor.py`, `host/tests/test_mock_backend.py`.

**Interfaces (produces):**
```python
# cursor.py
def encode_cursor(executed_at: datetime, run_id: str) -> str          # base64url(json {"t": iso, "id": run_id})
def decode_cursor(cursor: str) -> tuple[datetime, str]                 # ValueError on garbage
# backend.py — new/changed abstractmethods (docstrings state REST obligations: server-made opaque cursor,
# total = filter-applied count, order executed_at DESC, run_id DESC)
async def search_apis(self, session, query="", group=None, updated_after=None, cursor=None, limit=20) -> Page[ApiSpec]
async def list_runs(self, session, q: RunsQuery) -> Page[RunResult]
async def count_runs(self, session, since=None, until=None, status=None, api_id=None) -> int
async def aggregate_runs(self, session, q: AggregateQuery) -> list[RunGroup]
async def current_state(self, session, scope_api_ids: list[str] | None = None) -> list[CellState]
async def watermarks(self, session, api_ids: list[str] | None = None, first_non_pass_since: datetime | None = None) -> list[ApiWatermark]
async def operator_scope(self, session, operator_id: str, window_days: int) -> ScopeSummary
async def simulate_rule(self, session, draft, window_days: int) -> RuleImpact
async def find_apis_with_param(self, session, param, cursor=None, limit=20) -> Page[ApiSpec]
async def recommend_rules_for_api(self, session, api_id, limit=20) -> list[RuleRecommendation]
async def all_jobs(self, session, status=None, cursor=None, limit=50) -> Page[JobSpec]
async def list_rules(self, session, api_id=None, status=None, cursor=None, limit=50) -> Page[ValidationRule]
async def list_profiles(self, session, job_id=None, status=None, cursor=None, limit=50) -> Page[ComparisonProfile]
async def active_jobs(self, session) -> list[JobSpec]                  # remaining_executions > 0
async def get_body(self, session, run_id) -> dict | None
async def audit(self, session, cursor=None, limit=50) -> Page[AuditEntry]
async def append_audit(self, session, action, target_kind, target_id) -> None
```
The dict Mock implements these MINIMALLY this task (Python filtering, `total=len(filtered)`, cursor via
`encode_cursor` on the sorted list) — correctness first, SQLite in Task 4. Doubles return empty `Page`s.
Every caller of the old signatures (scheduler, reports, briefing, insights, app routes, executor) is
updated to the envelopes in THIS task so the suite compiles and stays green (behavior unchanged).

- [ ] Tests: cursor round-trip + garbage → ValueError; `list_runs` pages to exhaustion and `sum(len(items)) == total`; `count_runs(api_id)`; `all_jobs(status="staged")`; `active_jobs` excludes exhausted; existing suite ≥590 green.
- [ ] **Commit** `feat(core,host): paged ABC contract with keyset cursors and totals (dict Mock adapted)`.

### Task 3: Model path — envelopes, no bodies, capped window, exact population

**Files:** Modify `executor.py`, `serialization.py`, `tools/registry.py` (descriptions only), `enrichment.py`; Test `test_executor.py`, `test_serialization.py`.

- `run_record`: drop `response_body`, add `has_body: bool`. `api_record` unchanged.
- `_list_runs`: build `RunsQuery` from tool input (`limit` via `_limit(raw, 50, 200)`), fence
  `{"items": [...], "total": page.total, "next_cursor": page.next_cursor}`; set `last_listed_run_ids = ids[:PROVENANCE_CAP]`
  and `last_population = page.total`.
- `_search_apis`: same envelope. `_aggregate_runs`: `backend.aggregate_runs(AggregateQuery(...))`; population =
  `count_runs(since, until, status, api_id)` (now with `api_id`); remove `truncated`.
- `_find_apis_with_param`: envelope; remember only `items` (≤ limit). `_recommend_rules_for_api(limit)`.
- `_stage_rule`: `simulate_rule(draft, window_days=config.max_aggregate_window_days)`.
- Tests: `run_record` has `has_body` and no body; `list_runs` fenced JSON for 50 runs with bodies stays under 12k
  and is valid JSON; `last_listed_run_ids` ≤200 after an aggregate over 2000; population equals `count_runs`
  for `api_id` queries; `compact_history` wiring test (a fake turn with `compact_history_above_tokens=1` yields
  `results_cleared>0` and the host sets `stored_messages=0`).
- [ ] **Commit** `feat(core): tool results carry totals, run records drop bodies, capped provenance window`.

## Part B — P0' SQLite Mock, ingest materialization, masking, harness

### Task 4: `store.py` schema + SQLite-backed Mock reads

**Files:** Create `host/atworks_host/store.py`; Modify `mock_backend.py`; Test `host/tests/test_store.py`.

**Interfaces (produces):**
```python
class Store:
    def __init__(self, path: str | Path = ":memory:"): ...          # opens, PRAGMA journal_mode=WAL, foreign_keys=ON
    def init_schema(self) -> None                                    # DDL below, idempotent
    def load_fixtures(self, fixtures_dir: Path) -> None              # apis.json, runs.json (computes day), operators.json
    def conn(self) -> sqlite3.Connection
DDL (exact):
  apis(api_id TEXT PK, method, path, name, "group", updated_at TEXT, has_rules INT, params JSON)          IDX(updated_at DESC), IDX("group"), IDX(path)
  runs(run_id TEXT PK, api_id, job_id, target_env, test_data_label, status, http_status INT, duration_ms INT,
       executed_at TEXT, executed_by, failed_rules JSON, day TEXT)
       IDX(executed_at DESC, run_id DESC), IDX(api_id, executed_at DESC), IDX(status, executed_at DESC), IDX(executed_by, executed_at DESC), IDX(day)
  runs_archive(same columns, archived_at TEXT)
  bodies(run_id TEXT PK, body JSON, captured_at TEXT)
  current_state(api_id, target_env, test_data_label, run_id, status, executed_at, transitions_total INT, PRIMARY KEY(api_id,target_env,test_data_label))
  rollup_day(day, api_id, target_env, test_data_label, count INT, pass INT, fail INT, error INT, transitions INT, p95_duration_ms INT, failed_rule_counts JSON, PRIMARY KEY(day,api_id,target_env,test_data_label))  IDX(day), IDX(api_id, day)
  api_watermark(api_id TEXT PK, last_pass_at, first_non_pass_at, last_non_pass_at, latest_status, api_updated_at)
  operator_api(operator_id, api_id, last_executed_at, run_count INT, PRIMARY KEY(operator_id, api_id))
  audit_log(seq INTEGER PK AUTOINCREMENT, at, operator, action, target_kind, target_id, session_id)
  retention_state(key TEXT PK, value TEXT)
```
Mock reads become SQL: `search_apis` (LIKE on path/name with `updated_at` order, `LIMIT limit+1` for
`next_cursor`, `total` via COUNT), `list_runs` keyset (`WHERE (executed_at, run_id) < (?, ?)`), `count_runs`
COUNT, `aggregate_runs` = SUM over `rollup_day` grouped by the requested key (for `failed_rule`, expand
`failed_rule_counts` JSON via `json_each`), `current_state`, `watermarks`, `operator_scope`, `active_jobs`
(ledger stays in-memory this task; jobs table optional), `get_body`, `audit`/`append_audit`. Ledgers
(rules/profiles/formats/jobs) remain Python objects; only runs/bodies/rollups/watermarks/operator/audit move to SQL.
`RunGroup` reconstruction from rollups must produce the same fields `aggregation._build` did
(`count,fail,error,run_ids(≤50 newest via runs index),first_non_pass_at,last_pass_before,latest_status,transitions,flaky,p95,regression_suspect,api_updated_at`).

- [ ] Tests: schema idempotent; fixtures load (12 apis / 30 runs) and every existing Mock read returns the same
  results as before (golden comparison against the dict implementation for the fixture data — keep a copy of
  the old filter in the test); keyset paging exhaustive; `aggregate_runs("failed_rule")` matches `aggregation.aggregate`
  over the same runs; SLO smoke on fixtures (<10ms each).
- [ ] **Commit** `feat(host): SQLite-backed Mock store — schema blueprint, keyset paging, rollup aggregates`.

### Task 5: Ingest materialization (`materialize.py` + transaction) and property test

**Files:** Create `atworks-agent/core/atworks_agent/materialize.py`; Modify `mock_backend.py` (`execute_job_once`, `record_execution`), `jobs.py`; Test `test_materialize.py` (property-based with `random`, no hypothesis dependency), `host/tests/test_ingest.py`.

**Interfaces (produces):**
```python
def rollup_delta(runs: Sequence[RunResult], prev_state: Mapping[CellKey, CellState]) -> tuple[list[RollupRow], list[CellState], list[ApiWatermark]]
```
Pure: given a batch (chronological) and prior cell states, return rollup increments (per day/cell), new
cell states (with transitions incremented when status crosses pass↔non-pass vs the previous state), and
watermark updates. The Mock's `record_execution` wraps: insert runs (INSERT OR IGNORE → idempotent),
insert masked bodies (Task 6 hook; this task stores unmasked but through the same call), upsert
current_state/rollup_day/api_watermark/operator_api, then the ledger update with `run_count += n`,
`recent_run_ids = (new + old)[:50]` — **`run_ids` stops growing** (kept for compatibility, no longer appended;
reports use `list_runs(RunsQuery(job_id=...))` — add `job_id` to `RunsQuery`).
`execute_job_once`: iterate `envs × data × apis` in batches of `max_concurrency` with `await asyncio.sleep(0)`
between batches (Mock is CPU-only; the semaphore shape is what a REST adapter parallelizes).

- [ ] Property test: 200 random runs across 5 apis × 2 envs × 2 data over 10 days → materialize incrementally in
  random batch sizes; compare `rollup_day` sums, `current_state`, `api_watermark`, per-cell `transitions` against
  `aggregation.aggregate` recomputed from scratch — equal. Idempotency: re-ingesting the same batch changes nothing.
  Ingest test: a 4-api job executes; `run_count`, `recent_run_ids`, `rollup_day` rows, `operator_api` rows present.
  SSE-nonblocking unit proxy: `execute_job_once` on a 400-cell matrix yields to the loop ≥ ceil(400/max_concurrency) times (count `sleep(0)` via a patched clock).
- [ ] **Commit** `feat(core,host): ingest-time materialization — rollups, current state, watermarks, operator index (idempotent)`.

### Task 6: Masking at capture

**Files:** Create `masking.py`; Modify `mock_backend.py` (body capture), `reports.py` (fallback note when body missing), `config.py` (default rules); Test `test_masking.py`, `host/tests/test_masking_capture.py`.

```python
DEFAULT_MASKING_RULES = [
  MaskingRule(name="krn-resident-id", pattern=r"\b\d{6}-?[1-4]\d{6}\b"),
  MaskingRule(name="card-number", pattern=r"\b(?:\d[ -]?){15,16}\b"),
  MaskingRule(name="account-number", pattern=r"\b\d{3}-?\d{2,6}-?\d{4,8}\b"),
  MaskingRule(name="phone", pattern=r"\b01[016789]-?\d{3,4}-?\d{4}\b"),
  MaskingRule(name="email", pattern=NAMED_FORMATS["email"]),
]
def mask_body(body: Any, policy: MaskingPolicy) -> Any   # recursive; string leaves re.sub per rule; dict keys untouched
def body_capture_enabled(api: ApiSpec, policy: MaskingPolicy) -> bool
```
- Tests: each rule masks its sample and leaves normal text; nested/array leaves; disabled group → `bodies`
  row absent and `_parity` marks the row `status_only` with note "본문 캡처 해제"; `compare_bodies` on two masked
  bodies equals compare on the unmasked pair when the masked substrings are identical on both sides.
- [ ] **Commit** `feat(core,host): capture-time body masking with per-group capture opt-out`.

### Task 7: Scale harness (generator + bench + `scale` marker)

**Files:** Create `scripts/scale/generate.py`, `scripts/scale/bench.py`, `host/tests/test_scale.py` (marked `scale`); Modify `pyproject.toml`/pytest config (`markers = scale`, `addopts = -m "not scale"`).

- `generate.py --apis 50000 --days 180 --per-day 10000 --peak-day 120:50000 --operators 500 --out scale.sqlite`:
  deterministic seed; groups distribution; status mix (pass 88 / fail 9 / error 3), injected patterns (5% flaky cells,
  2% regressions with api.updated_at between last pass and first fail), executed_by distribution over operators;
  writes through `Store` + `materialize` (so rollups are real). Bodies for the last 90 days only (small JSON).
- `bench.py --db scale.sqlite`: times each SLO path via the Mock (`get_context`, `list_runs` page, `count_runs`,
  `aggregate_runs` ×5 group_bys, `simulate_rule` 30d, `InsightPanels.build` deterministic, `build_briefing`,
  retention day job) and prints a table vs `config.slo_*`; exit 1 on any breach.
- `test_scale.py` (`@pytest.mark.scale`): generates a REDUCED set by default (`--apis 5000 --days 30`) unless
  `ATWORKS_SCALE_FULL=1`, asserts every SLO scaled by data size proportion rules documented in the test; full
  set is CI-optional.
- [ ] Baseline run BEFORE Part C rewires (expected: some SLOs fail — record the numbers in the report; Task 8/9
  must turn them green).
- [ ] **Commit** `feat(scale): synthetic 2M-run generator, SLO bench, opt-in scale tests`.

## Part C — P1 host rewiring, retention, audit

### Task 8: Rewire reads to materialized tables

**Files:** Modify `mock_backend.py` (`get_context`), `atworks-agent/core/atworks_agent/insights.py`, `selection.py`, `host/atworks_host/{insights.py,briefing.py,app.py}`; Test existing files + `host/tests/test_rewire.py`.

- `get_context`: `count_runs(since=now-30d, status="fail")`, `count_runs(..., "error")`, `operator_scope(...)`
  (no scans; test asserts no `runs` full-table read via a SQL statement counter/`sqlite3.Connection.set_trace_callback`).
- `insights.candidate_insights` becomes **query-driven**: takes `groups_by_api`, `cells`, `watermarks`, `groups_by_rule`,
  `stale_jobs` inputs produced by `InsightPanels.build` from `aggregate_runs`/`current_state`/`watermarks`/`active_jobs`
  (same `InsightCandidate` output; role table unchanged). Keep a thin compatibility wrapper for the old signature used by tests, or update tests.
- `build_briefing`: `aggregate_runs(day window)` + `count_runs` for the 24h; `summarize_insights` from rollups.
- `selection.resolve_select_where`: `failed_since` → `watermarks(first_non_pass_since=since)`; `related_to` → `search_apis(query=prefix)` paged to `CATALOGUE_SCAN_LIMIT`.
- `/runs/insights` → `aggregate_runs`; `/apis` and `/runs` routes accept `cursor`/`limit` and return `Page` JSON.
- [ ] Tests: regression suspect detected for an API outside the "newest 1000" set (proves full coverage); briefing correct on a synthetic 5k-run day; `get_context` executes zero `SELECT ... FROM runs` without a WHERE bound; bench: `get_context`/`aggregate`/`insights`/`briefing` SLOs green on the reduced scale set.
- [ ] **Commit** `feat(host,core): reads over rollups/watermarks/current state — full coverage, no sampling`.

### Task 9: Reports split, scheduler concurrency + active jobs, retention job, audit log

**Files:** Modify `reports.py`, `scheduler.py`, `app.py`, `main.py`; Create `retention.py`, `audit.py`; Test `test_scheduler_reports.py`, `host/tests/test_retention.py`, `host/tests/test_audit.py`.

- Reports: `runs.jsonl` appended per execution (records without bodies), `parity.json` recomputed from
  `list_runs(job_id)` + `get_body` (missing body → status_only note), `index.html` embeds summary + parity + the
  latest 200 run rows with a "전체 run은 포털에서" link; `rediff` reads `parity.json` inputs only. `data.json` kept as
  a small summary for compatibility (no runs array).
- Scheduler: `active_jobs()` only; `execute_job_once` per Task 5; retention at tick tail once per local day:
  `retention.run(store, config, now)` → move `runs` older than `retention_hot_days` to `runs_archive` (skip if
  `retention_cold_until` passed → delete archive rows only then), delete `bodies` older than `retention_body_days`,
  delete `insights_out/*/<date>` older than `insights_cache_days`, `SessionStore.sweep(idle_hours)`. Each step
  records `retention_state`.
- Audit: `append_audit` from `job_action`/`rule_action`/`format_batch_action`/`profile_action` (before executor call,
  with the outcome appended after) and from Mock `apply_*`; `GET /audit` (session) paged.
- [ ] Tests: report files shape + rediff without runs array; 181-day-old runs archived (count preserved in archive), 91-day bodies gone, cache folders rotated, idle sessions swept; scheduler skips exhausted jobs (query counter); audit rows per approval click, none from a chat `apply_*` attempt (held); bench: SSE latency proxy + retention SLO.
- [ ] **Commit** `feat(host): incremental reports, concurrent execution, retention tiers, append-only audit log`.

## Part D — P2 web

### Task 10: Web paging, labels, home summary

**Files:** Modify `web/web-shared/api.ts` (generic `getPage`), `web/atworks-web/lib/{api.ts,types.ts}`, `components/views/{ApisView,RunsView,JobsView,RulesView,ProfilesView}.tsx`, `HomeView.tsx`, `app/page.tsx`; Create `components/Pager.tsx`; host `app.py` adds `GET /home/summary`.

- `Page<T> { items: T[]; next_cursor: string | null; total: number }`; `fetchRuns(status, cursor)`, `fetchApis(query, cursor)`, etc.
- `Pager`: "N개 중 M개 표시 · 다음 ›" (and "처음으로"); views keep a cursor stack; `screen_state.visible` = current page (≤40 slice kept).
- Run rows render `api_method api_path` from the record (drop the 500-API prefetch).
- Virtualization: tables use `content-visibility: auto` + row height hint (no new dependency); cap rendered rows to the page size anyway.
- `/home/summary` = `{counts, insights_flags, briefing_header}` in one call; HomeView uses it.
- [ ] `npm run build` clean; manual: page to row 51; header shows true totals.
- [ ] **Commit** `feat(web): cursor paging with honest totals, run labels, single home summary call`.

## Part E — P3 docs, SLO green, smoke, push branch

### Task 11: Decision record, README, harness green, three live smokes, branch push

- CLAUDE.md: new **Scale & retention** bullet (ingest-time materialization, paged contract, cursors, totals, retention tiers with the decided values, masking, audit log, SQLite Mock as blueprint, SLOs + `pytest -m scale`); update Backend bullet (new reads), Surfaces (`/audit`, paged `/apis` `/runs`, `/home/summary`), Execution contract (batched concurrency), and the "Mock" line. README paragraph.
- Run the FULL harness (`ATWORKS_SCALE_FULL=1`) once and paste the SLO table into the report; all green or a documented, ruled exception.
- Re-run the three existing live smokes (parity_shots3, screen_shots 3 phases, insight_shots 3 phases) against the SQLite Mock — all must pass as before.
- `git push -u origin feature/scale-architecture` (branch only). **Commit** `docs: decision record and README for the scale architecture`.

---

## Self-review notes

- Spec coverage: §3 → T4; §4 → T5; §5 → T1/T2/T3; §6 → T9; §7 → T6; §8 → T3; §9 → T8/T9; §10 → T10; §11 → T4;
  §12 → T7 (+T11 full run); §13 recorded; §14 → each task's tests; §15 ordering = Parts A–E.
- Type consistency: `Page`/`RunsQuery`/`AggregateQuery` (T1) are the ABC's shapes (T2), consumed by executor (T3),
  implemented in SQL (T4), used by host rewiring (T8) and web (T10 mirrors `Page<T>`); `CellState`/`ApiWatermark`
  (T1) are produced by `materialize.py` (T5) and read by `insights.py` (T8); `MaskingPolicy` (T1) drives T6.
- Placeholder scan: none — DDL, signatures, retention values, SLOs, rule regexes are pinned.
