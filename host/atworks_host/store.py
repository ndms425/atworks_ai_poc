"""SQLite-backed storage for MockAtworks -- the schema blueprint the Java aTworks side will
implement. Task 4 (spec 2026-09-06) moves runs/bodies/audit off python dicts and onto indexed SQL;
`apis` mirrors into a table too so `search_apis` pages over SQL, but the authoritative in-memory
index for ledger guardrails stays `MockAtworks.apis` (a plain dict) per the controller ruling.

``current_state`` / ``rollup_day`` / ``api_watermark`` / ``operator_api`` are materialized at
**ingest** (Task 5, spec §4): ``ingest()`` is the single write path for runs -- one transaction
inserts the new runs and their (optionally masked) bodies and folds the same batch into all four
tables via the pure ``atworks_agent.materialize.rollup_delta``. ``current_state()`` /
``watermarks()`` / ``operator_scope_ids()`` read those tables by primary key; the Task 4
on-demand scans survive as ``recompute_*`` **oracles for tests only**. ``aggregate_runs`` still
reads base ``runs`` through ``fetch_runs`` (Task 8 decides rollup-fed aggregation reads).

NULL sentinel: ``test_data_label`` is written as ``''`` (never NULL) in the four materialized
tables and mapped back to ``None`` on every read. SQLite treats NULLs as distinct inside a
non-rowid PRIMARY KEY, so an ``ON CONFLICT`` upsert on a NULL label would never match its own
previous row and the cell would fan out into one row per ingest.

Timestamp invariant: every ``datetime`` this module writes to a TEXT column is normalized to UTC
with a fixed-width, always-6-fractional-digit ``strftime("%Y-%m-%dT%H:%M:%S.%f")`` plus a literal
``Z`` suffix (see ``_iso``/``_parse_iso``). Every query parameter compared against such a column is
normalized the same way. Because every stored value has identical width and a fixed UTC offset,
plain SQL string comparison (``<``, ``>=``, ``ORDER BY``) is exactly chronological comparison --
this is what lets ``executed_at``/``updated_at``/``at`` keyset paging and range filters run as
ordinary indexed text predicates.
"""
from __future__ import annotations

import heapq
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from atworks_agent import (
    AggregateQuery,
    ApiSpec,
    ApiWatermark,
    AuditEntry,
    CellKey,
    CellState,
    KeyCounts,
    Page,
    RunGroup,
    RunResult,
    RunsQuery,
    RunStatus,
    decode_cursor,
    encode_cursor,
    merge_watermark,
    rollup_delta,
)
from atworks_agent.aggregation import MAX_RUN_IDS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS apis (
    api_id TEXT PRIMARY KEY,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    "group" TEXT,
    updated_at TEXT NOT NULL,
    has_rules INTEGER NOT NULL DEFAULT 0,
    params JSON NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_apis_updated_at ON apis(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_apis_group ON apis("group");
CREATE INDEX IF NOT EXISTS idx_apis_path ON apis(path);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    api_id TEXT NOT NULL,
    job_id TEXT,
    target_env TEXT NOT NULL,
    test_data_label TEXT,
    status TEXT NOT NULL,
    http_status INTEGER,
    duration_ms INTEGER,
    executed_at TEXT NOT NULL,
    executed_by TEXT,
    failed_rules JSON NOT NULL DEFAULT '[]',
    day TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_executed_at ON runs(executed_at DESC, run_id DESC);
CREATE INDEX IF NOT EXISTS idx_runs_api_executed_at ON runs(api_id, executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status_executed_at ON runs(status, executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_executed_by_executed_at ON runs(executed_by, executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_day ON runs(day);

CREATE TABLE IF NOT EXISTS runs_archive (
    run_id TEXT PRIMARY KEY,
    api_id TEXT NOT NULL,
    job_id TEXT,
    target_env TEXT NOT NULL,
    test_data_label TEXT,
    status TEXT NOT NULL,
    http_status INTEGER,
    duration_ms INTEGER,
    executed_at TEXT NOT NULL,
    executed_by TEXT,
    failed_rules JSON NOT NULL DEFAULT '[]',
    day TEXT,
    archived_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bodies (
    run_id TEXT PRIMARY KEY,
    body JSON,
    captured_at TEXT
);

CREATE TABLE IF NOT EXISTS current_state (
    api_id TEXT NOT NULL,
    target_env TEXT NOT NULL,
    test_data_label TEXT,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    executed_at TEXT NOT NULL,
    transitions_total INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (api_id, target_env, test_data_label)
);

CREATE TABLE IF NOT EXISTS rollup_day (
    day TEXT NOT NULL,
    api_id TEXT NOT NULL,
    target_env TEXT NOT NULL,
    test_data_label TEXT,
    count INTEGER NOT NULL DEFAULT 0,
    pass INTEGER NOT NULL DEFAULT 0,
    fail INTEGER NOT NULL DEFAULT 0,
    error INTEGER NOT NULL DEFAULT 0,
    transitions INTEGER NOT NULL DEFAULT 0,
    p95_duration_ms INTEGER,
    -- both maps are {group key: {"count": n, "fail": n, "error": n}} (see materialize.KeyCounts):
    -- a group keyed by a rule or an HTTP status reports its own fail/error/passed split, which a
    -- flat key->count map cannot produce. http_status_counts (Task 8) is what keeps group_by=
    -- "http_status" on the rollup instead of falling back to a run scan.
    failed_rule_counts JSON,
    http_status_counts JSON,
    PRIMARY KEY (day, api_id, target_env, test_data_label)
);
CREATE INDEX IF NOT EXISTS idx_rollup_day_day ON rollup_day(day);
CREATE INDEX IF NOT EXISTS idx_rollup_day_api_day ON rollup_day(api_id, day);

CREATE TABLE IF NOT EXISTS api_watermark (
    api_id TEXT PRIMARY KEY,
    last_pass_at TEXT,
    first_non_pass_at TEXT,
    last_non_pass_at TEXT,
    latest_status TEXT,
    api_updated_at TEXT
);

CREATE TABLE IF NOT EXISTS operator_api (
    operator_id TEXT NOT NULL,
    api_id TEXT NOT NULL,
    last_executed_at TEXT,
    run_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (operator_id, api_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    operator TEXT NOT NULL,
    action TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    session_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_at_seq ON audit_log(at, seq);

CREATE TABLE IF NOT EXISTS retention_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _iso(dt: datetime) -> str:
    """UTC, fixed-width (always 6 fractional digits), ``Z``-suffixed -- see module docstring."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _day_for(executed_at: datetime, briefing_tz: str) -> str:
    return executed_at.astimezone(ZoneInfo(briefing_tz)).date().isoformat()


def _label_in(label: str | None) -> str:
    """``test_data_label`` on the way into a materialized table: NULL becomes ``''`` -- see the
    module docstring's NULL-sentinel note (a NULL inside a non-rowid PK never conflicts with
    itself, so upserts on an unbound cell would fan out into one row per ingest)."""
    return label if label is not None else ""


def _label_out(label: str | None) -> str | None:
    """...and back: ``''`` (or a NULL left by an older row) reads as ``None``."""
    return label if label else None


_RUN_ID_SCAN_CAP = 5000     # bounded newest-first scan that fills run_ids for the map-keyed axes
_API_SAMPLE_CAP = 20        # RunGroup.api_sample -- InsightCandidate.api_ids' own schema cap

# The group key, as SQL, per aggregation._keys -- once over rollup_day's cell columns and once over
# raw runs (the exact edge-day path). The two map-keyed axes live in their own JSON columns.
_LABEL_SQL = "CASE WHEN test_data_label IS NULL OR test_data_label = '' THEN '-' ELSE test_data_label END"
_ROLLUP_KEY_SQL = {
    "api": "api_id",
    "env": "target_env",
    "api_env_data": f"api_id || '|' || target_env || '|' || {_LABEL_SQL}",
}
_RUNS_KEY_SQL = dict(_ROLLUP_KEY_SQL)
_MAP_COLUMN = {"failed_rule": "failed_rule_counts", "http_status": "http_status_counts"}


# One accumulator row, as a plain list (a dict per group is measurable when a query returns one
# group per cell in the project): [count, passed, fail, error, transitions, p95, api_count].
_COUNT, _PASSED, _FAIL, _ERROR, _TRANSITIONS, _P95, _API_COUNT = range(7)


def _acc_for(acc: dict[str, list], key: str) -> list:
    row = acc.get(key)
    if row is None:
        row = [0, 0, 0, 0, 0, None, None]
        acc[key] = row
    return row


def _max_or_none(a: int | None, b: int | None) -> int | None:
    return b if a is None else (a if b is None else max(a, b))


def _window_partitions(
    since: datetime | None, until: datetime | None, briefing_tz: str,
) -> tuple[tuple[str | None, str | None] | None, list[tuple[datetime, datetime]]]:
    """Split ``[since, until)`` into the whole local days the rollup can answer and the (at most
    two) partial edge windows it cannot. A window that contains no whole day at all -- the daily
    briefing's 09:00→09:00, say -- comes back as one edge and no rollup range."""
    tz = ZoneInfo(briefing_tz)
    edges: list[tuple[datetime, datetime]] = []
    day_from: str | None = None
    day_to: str | None = None
    lower_edge: tuple[datetime, datetime] | None = None
    upper_edge: tuple[datetime, datetime] | None = None
    if since is not None:
        local = since.astimezone(tz)
        midnight = datetime.combine(local.date(), datetime.min.time(), tzinfo=tz)
        if local == midnight:
            day_from = local.date().isoformat()
        else:
            next_midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=tz)
            day_from = next_midnight.date().isoformat()
            lower_edge = (since, next_midnight)
    if until is not None:
        local = until.astimezone(tz)
        midnight = datetime.combine(local.date(), datetime.min.time(), tzinfo=tz)
        day_to = (local.date() - timedelta(days=1)).isoformat()
        if local != midnight:
            upper_edge = (midnight, until)
    if day_from is not None and day_to is not None and day_from > day_to:
        # no whole day inside the window: one exact query over runs covers all of it
        return None, [(since, until)]   # type: ignore[list-item]
    if lower_edge is not None:
        edges.append(lower_edge)
    if upper_edge is not None:
        edges.append(upper_edge)
    return (day_from, day_to), edges


def _counts(row: list, status: str | None) -> tuple[int, int, int, int]:
    """``(count, passed, fail, error)`` a group reports under ``status``. A rollup row keeps the
    three verdict counters side by side, so a status filter is a choice of counters, never a scan."""
    count, passed, fail, error = row[_COUNT], row[_PASSED], row[_FAIL], row[_ERROR]
    if status == "pass":
        return passed, passed, 0, 0
    if status == "fail":
        return fail, 0, fail, 0
    if status == "error":
        return error, 0, 0, error
    if status == "non_pass":
        return fail + error, 0, fail, error
    return count, passed, fail, error


def _group_from_acc(
    key: str, row: list, counts: tuple[int, int, int, int], q: AggregateQuery,
    apis: Mapping[str, ApiSpec], flaky_min: int,
) -> RunGroup:
    count, passed, fail, error = counts
    # A status filter leaves a single-verdict population, whose pass<->non-pass transition count is
    # zero by construction -- the same number the pre-rollup scan produced over a filtered run list.
    transitions = 0 if q.status else row[_TRANSITIONS]
    api = apis.get(key) if q.group_by == "api" else None
    return RunGroup(
        key=key, label=f"{api.method} {api.path}" if api is not None else key,
        count=count, fail=fail, error=error, passed=passed,
        transitions=transitions, flaky=transitions >= flaky_min,
        p95_duration_ms=row[_P95],
        api_count=row[_API_COUNT] if q.group_by in _MAP_COLUMN else None,
    )


def _merge_key_counts(stored: str | None, delta: Mapping[str, KeyCounts]) -> str:
    """Fold a batch's ``{key: KeyCounts}`` map into the JSON already on the rollup row and return
    the JSON to store back. Values are ``{"count","fail","error"}`` objects, not bare ints --
    see ``materialize.KeyCounts``."""
    merged: dict[str, dict[str, int]] = json.loads(stored) if stored else {}
    for key, counts in delta.items():
        row = merged.get(key) or {"count": 0, "fail": 0, "error": 0}
        merged[key] = {
            "count": row.get("count", 0) + counts.count,
            "fail": row.get("fail", 0) + counts.fail,
            "error": row.get("error", 0) + counts.error,
        }
    return json.dumps(merged, ensure_ascii=False)


class Store:
    """Owns the sqlite3 connection and every SQL query MockAtworks needs. Runs/bodies/audit are
    the only state that actually lives here in Task 4 -- job/rule/profile/format ledgers stay
    python objects on MockAtworks (controller ruling); ``apis`` is mirrored here for
    ``search_apis`` but ``MockAtworks.apis`` (a dict) remains the source of truth read elsewhere."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL is a no-op on ":memory:" (sqlite silently keeps "memory" journal mode) -- harmless.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    def conn(self) -> sqlite3.Connection:
        return self._conn

    def init_schema(self) -> None:
        """CREATE TABLE/INDEX IF NOT EXISTS throughout -- safe to call on an already-initialized
        connection (constructor calls it once; tests call it again to assert idempotency)."""
        self._conn.executescript(_SCHEMA)
        self._migrate_columns()
        self._conn.commit()

    def _migrate_columns(self) -> None:
        """Columns added to a table that already exists on disk. ``CREATE TABLE IF NOT EXISTS``
        never widens an existing table, so a store written before Task 8 keeps its old
        ``rollup_day`` shape until this ALTER runs; the new column stays NULL (read as an empty
        map) until ``rebuild_materialized`` refills it."""
        for table, column, decl in (("rollup_day", "http_status_counts", "JSON"),):
            existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if existing and column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # -- fixtures ----------------------------------------------------------------------------

    def load_fixtures(self, fixtures_dir: Path, briefing_tz: str = "Asia/Seoul") -> None:
        """Loads apis.json and runs.json (computing ``day`` from ``executed_at``). operators.json
        stays a plain dict on MockAtworks -- no SQL table mirrors OperatorProfile in this task."""
        apis = [ApiSpec(**row) for row in json.loads((fixtures_dir / "apis.json").read_text(encoding="utf-8"))]
        runs = [RunResult(**row) for row in json.loads((fixtures_dir / "runs.json").read_text(encoding="utf-8"))]
        self.load_apis(apis)
        self.ingest(runs, briefing_tz)

    # -- apis ----------------------------------------------------------------------------------

    def replace_all_apis(self, apis: Iterable[ApiSpec]) -> None:
        """Wholesale replacement -- backs ``MockAtworks.apis``'s property setter, so a test that
        does ``backend.apis = {...}`` from scratch leaves the mirrored ``apis`` table containing
        exactly that new set, not the old rows plus the new ones."""
        self._conn.execute("DELETE FROM apis")
        self._conn.commit()
        self.load_apis(apis)

    def load_apis(self, apis: Iterable[ApiSpec]) -> None:
        rows = [
            (a.api_id, a.method, a.path, a.name, a.group, _iso(a.updated_at), int(a.has_rules), json.dumps(a.params))
            for a in apis
        ]
        self._conn.executemany(
            'INSERT OR REPLACE INTO apis (api_id, method, path, name, "group", updated_at, has_rules, params) '
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        self._refresh_api_updated_at([a[0] for a in rows])
        self._conn.commit()

    @staticmethod
    def _row_to_api(row: sqlite3.Row) -> ApiSpec:
        return ApiSpec(
            api_id=row["api_id"], method=row["method"], path=row["path"], name=row["name"],
            group=row["group"], updated_at=_parse_iso(row["updated_at"]),
            has_rules=bool(row["has_rules"]), params=json.loads(row["params"]) if row["params"] else [],
        )

    def search_apis(
        self, query: str = "", group: str | None = None, updated_after: datetime | None = None,
        cursor: str | None = None, limit: int = 20, path_prefix: str | None = None,
    ) -> Page[ApiSpec]:
        clauses: list[str] = []
        params: list = []
        q = (query or "").lower()
        if q:
            clauses.append("LOWER(method || ' ' || path || ' ' || name) LIKE ?")
            params.append(f"%{q}%")
        if group is not None:
            clauses.append('"group" = ?')
            params.append(group)
        if path_prefix is not None:
            # A separate predicate, never folded into `query`: `query` is a free-text LIKE over
            # method+path+name, so a prefix pushed through it would also match the middle of a
            # path or a name. This one is anchored and index-backed (idx_apis_path).
            clauses.append("(path = ? OR path LIKE ?)")
            params += [path_prefix, f"{path_prefix}/%"]
        if updated_after is not None:
            clauses.append("updated_at >= ?")
            params.append(_iso(updated_after))
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = self._conn.execute(f"SELECT COUNT(*) FROM apis {where_sql}", params).fetchone()[0]

        keyset_clauses = list(clauses)
        keyset_params = list(params)
        if cursor:
            after_dt, after_id = decode_cursor(cursor)
            keyset_clauses.append("(updated_at < ? OR (updated_at = ? AND api_id < ?))")
            keyset_params += [_iso(after_dt), _iso(after_dt), after_id]
        keyset_where = f"WHERE {' AND '.join(keyset_clauses)}" if keyset_clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM apis {keyset_where} ORDER BY updated_at DESC, api_id DESC LIMIT ?",
            [*keyset_params, limit + 1],
        ).fetchall()
        items = [self._row_to_api(r) for r in rows[:limit]]
        next_cursor = encode_cursor(items[-1].updated_at, items[-1].api_id) if len(rows) > limit else None
        return Page(items=items, next_cursor=next_cursor, total=total)

    # -- runs / bodies ---------------------------------------------------------------------------

    def _run_row(self, run: RunResult, briefing_tz: str) -> tuple:
        day = run.day if run.day is not None else _day_for(run.executed_at, briefing_tz)
        return (
            run.run_id, run.api_id, run.job_id, run.target_env, run.test_data_label, run.status.value,
            run.http_status, run.duration_ms, _iso(run.executed_at), run.executed_by,
            json.dumps(run.failed_rules), day,
        )

    def _write_run_rows(
        self, runs: Sequence[RunResult], briefing_tz: str, mask: Callable[[dict], dict] | None,
        *, replace: bool = False,
    ) -> None:
        """Raw row writer -- runs + bodies only, no materialization, no commit. Only ``ingest``
        and ``upsert_run`` call this. ``mask`` is the Task 6 hook: when given it rewrites each
        response body on its way into ``bodies`` (this task passes nothing, so bodies are stored
        unchanged -- but through the same call)."""
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        self._conn.executemany(
            f"{verb} INTO runs (run_id, api_id, job_id, target_env, test_data_label, status, "
            "http_status, duration_ms, executed_at, executed_by, failed_rules, day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [self._run_row(r, briefing_tz) for r in runs],
        )
        body_rows = [
            (r.run_id, json.dumps(mask(r.response_body) if mask is not None else r.response_body),
             _iso(r.executed_at))
            for r in runs if r.response_body is not None
        ]
        if body_rows:
            self._conn.executemany(
                f"{verb} INTO bodies (run_id, body, captured_at) VALUES (?,?,?)", body_rows,
            )

    def ingest(
        self, runs: Iterable[RunResult], briefing_tz: str = "Asia/Seoul", *,
        mask: Callable[[dict], dict] | None = None,
    ) -> int:
        """**The** write path for runs (spec §4): one transaction inserts the batch and folds it
        into ``current_state`` / ``rollup_day`` / ``api_watermark`` / ``operator_api``. Returns the
        number of runs that were actually new.

        Idempotent by construction: run_ids already present are filtered out up front (and the
        insert itself is ``INSERT OR IGNORE``), so re-ingesting a batch inserts nothing and adds
        no delta anywhere -- a retried ``record_execution`` cannot double-count a rollup."""
        batch: dict[str, RunResult] = {}
        for run in runs:
            batch.setdefault(run.run_id, run)
        if not batch:
            return 0
        known = self._existing_run_ids(list(batch))
        fresh = [
            r if r.day else r.model_copy(update={"day": _day_for(r.executed_at, briefing_tz)})
            for run_id, r in batch.items() if run_id not in known
        ]
        if not fresh:
            return 0
        self._write_run_rows(fresh, briefing_tz, mask)
        self._materialize(fresh)
        self._conn.commit()
        return len(fresh)

    def _existing_run_ids(self, run_ids: Sequence[str]) -> set[str]:
        found: set[str] = set()
        for start in range(0, len(run_ids), 500):   # stay well under SQLite's bound-variable cap
            chunk = run_ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"SELECT run_id FROM runs WHERE run_id IN ({placeholders})", list(chunk)
            ).fetchall()
            found.update(r["run_id"] for r in rows)
        return found

    def upsert_run(self, run: RunResult, briefing_tz: str = "Asia/Seoul") -> None:
        """Single-row write for the ``MockAtworks.runs`` dict-compat view's ``__setitem__``
        (tests still poke ``backend.runs[run_id] = run`` to redate a fixture run). A *new* run_id
        goes through ``ingest`` (incremental materialization); overwriting an *existing* run
        mutates history under the already-folded deltas, which no increment can undo, so that
        path replaces the row and rebuilds the four materialized tables from scratch. Execution
        never takes it -- ``execute_job_once`` ingests its whole batch at once."""
        if run.run_id not in self._existing_run_ids([run.run_id]):
            self.ingest([run], briefing_tz)
            return
        self._write_run_rows([run], briefing_tz, None, replace=True)
        if run.response_body is None:
            self._conn.execute("DELETE FROM bodies WHERE run_id = ?", (run.run_id,))
        self.rebuild_materialized()
        self._conn.commit()

    def replace_all_runs(self, runs: Mapping[str, RunResult] | Iterable[RunResult], briefing_tz: str = "Asia/Seoul") -> None:
        """Wholesale replacement -- backs ``backend.runs = {...}`` in tests that build a
        purpose-built run set from scratch, discarding the fixtures entirely. The four
        materialized tables are cleared with the runs, then rebuilt by the ``ingest`` below."""
        values = list(runs.values()) if isinstance(runs, Mapping) else list(runs)
        self._conn.execute("DELETE FROM runs")
        self._conn.execute("DELETE FROM bodies")
        self._clear_materialized()
        self._conn.commit()
        self.ingest(values, briefing_tz)

    # -- ingest-time materialization (spec §4) ---------------------------------------------------

    def _clear_materialized(self) -> None:
        for table in ("current_state", "rollup_day", "api_watermark", "operator_api"):
            self._conn.execute(f"DELETE FROM {table}")

    def rebuild_materialized(self, briefing_tz: str = "Asia/Seoul") -> None:
        """Drop the four materialized tables and fold every stored run back in, in one pass. Used
        when a run already folded in is overwritten (``upsert_run``) -- an increment cannot undo
        history. No commit here; the caller owns the transaction."""
        self._clear_materialized()
        runs = [
            r if r.day else r.model_copy(update={"day": _day_for(r.executed_at, briefing_tz)})
            for r in self.fetch_runs()
        ]
        if runs:
            self._materialize(runs)

    def _materialize(self, fresh: Sequence[RunResult]) -> None:
        """Fold a batch of *new* runs into the four materialized tables. Pure arithmetic lives in
        ``atworks_agent.materialize.rollup_delta``; this method only reads the rows the batch
        touches, merges, and writes them back inside the caller's transaction."""
        prev_state = self._current_state_for(sorted({r.api_id for r in fresh}))
        rollups, cells, marks, operators = rollup_delta(fresh, prev_state)

        self._conn.executemany(
            "INSERT OR REPLACE INTO current_state "
            "(api_id, target_env, test_data_label, run_id, status, executed_at, transitions_total) "
            "VALUES (?,?,?,?,?,?,?)",
            [(c.api_id, c.target_env, _label_in(c.test_data_label), c.run_id, c.status.value,
              _iso(c.executed_at), c.transitions_total) for c in cells],
        )

        for row in rollups:
            label = _label_in(row.test_data_label)
            existing = self._conn.execute(
                'SELECT "count", "pass", fail, error, transitions, p95_duration_ms, failed_rule_counts, '
                "http_status_counts "
                "FROM rollup_day WHERE day = ? AND api_id = ? AND target_env = ? AND test_data_label = ?",
                (row.day, row.api_id, row.target_env, label),
            ).fetchone()
            rules = _merge_key_counts(existing["failed_rule_counts"] if existing else None, row.failed_rule_counts)
            https = _merge_key_counts(existing["http_status_counts"] if existing else None, row.http_status_counts)
            # p95 across merged batches is the documented §4 approximation (max of the per-batch
            # p95s), NOT an exact percentile over the union -- rollup_day never keeps raw durations.
            p95 = row.p95_duration_ms
            if existing is not None and existing["p95_duration_ms"] is not None:
                p95 = existing["p95_duration_ms"] if p95 is None else max(p95, existing["p95_duration_ms"])
            self._conn.execute(
                'INSERT OR REPLACE INTO rollup_day (day, api_id, target_env, test_data_label, "count", '
                '"pass", fail, error, transitions, p95_duration_ms, failed_rule_counts, http_status_counts) '
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (row.day, row.api_id, row.target_env, label,
                 (existing["count"] if existing else 0) + row.count,
                 (existing["pass"] if existing else 0) + row.passed,
                 (existing["fail"] if existing else 0) + row.fail,
                 (existing["error"] if existing else 0) + row.error,
                 (existing["transitions"] if existing else 0) + row.transitions,
                 p95, rules, https),
            )

        for mark in marks:
            row = self._conn.execute(
                "SELECT * FROM api_watermark WHERE api_id = ?", (mark.api_id,)
            ).fetchone()
            merged = merge_watermark(self._row_to_watermark(row) if row else None, mark)
            self._conn.execute(
                "INSERT OR REPLACE INTO api_watermark "
                "(api_id, last_pass_at, first_non_pass_at, last_non_pass_at, latest_status, api_updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (merged.api_id,
                 _iso(merged.last_pass_at) if merged.last_pass_at else None,
                 _iso(merged.first_non_pass_at) if merged.first_non_pass_at else None,
                 _iso(merged.last_non_pass_at) if merged.last_non_pass_at else None,
                 merged.latest_status.value if merged.latest_status else None,
                 row["api_updated_at"] if row is not None else None),
            )
        # spec §3's ``api_watermark ... updated_at(api)``: the catalogue's own updated_at is
        # denormalized onto the watermark row at ingest (and refreshed by ``load_apis``) so the
        # regression rule ``last_pass_at < api.updated_at <= first_non_pass_at`` is one indexed
        # read with no join for a REST adapter to reproduce.
        self._refresh_api_updated_at([m.api_id for m in marks])

        for delta in operators:
            row = self._conn.execute(
                "SELECT last_executed_at, run_count FROM operator_api WHERE operator_id = ? AND api_id = ?",
                (delta.operator_id, delta.api_id),
            ).fetchone()
            last = _iso(delta.last_executed_at)
            if row is not None and row["last_executed_at"] is not None:
                last = max(last, row["last_executed_at"])   # fixed-width UTC strings sort chronologically
            self._conn.execute(
                "INSERT OR REPLACE INTO operator_api (operator_id, api_id, last_executed_at, run_count) "
                "VALUES (?,?,?,?)",
                (delta.operator_id, delta.api_id, last,
                 (row["run_count"] if row is not None else 0) + delta.run_count),
            )

    def _refresh_api_updated_at(self, api_ids: Sequence[str] | None = None) -> None:
        """Copy ``apis.updated_at`` onto ``api_watermark.api_updated_at`` for the given APIs (all
        of them when None). Called from ``_materialize`` (new marks) and ``load_apis`` (a spec
        whose updated_at moved -- tests redate fixture APIs through ``backend.apis[id] = ...``),
        so the denormalized copy can never drift from the catalogue."""
        if api_ids is not None and not api_ids:
            return
        sql = (
            "UPDATE api_watermark SET api_updated_at = "
            "(SELECT updated_at FROM apis WHERE apis.api_id = api_watermark.api_id)"
        )
        if api_ids is None:
            self._conn.execute(sql)
            return
        for start in range(0, len(api_ids), 500):
            chunk = list(api_ids[start:start + 500])
            placeholders = ",".join("?" for _ in chunk)
            self._conn.execute(f"{sql} WHERE api_watermark.api_id IN ({placeholders})", chunk)

    def _current_state_for(self, api_ids: Sequence[str]) -> dict[CellKey, CellState]:
        if not api_ids:
            return {}
        placeholders = ",".join("?" for _ in api_ids)
        rows = self._conn.execute(
            f"SELECT * FROM current_state WHERE api_id IN ({placeholders})", list(api_ids)
        ).fetchall()
        cells = [self._row_to_cell(r) for r in rows]
        return {(c.api_id, c.target_env, c.test_data_label): c for c in cells}

    @staticmethod
    def _row_to_cell(row: sqlite3.Row) -> CellState:
        return CellState(
            api_id=row["api_id"], target_env=row["target_env"],
            test_data_label=_label_out(row["test_data_label"]), run_id=row["run_id"],
            status=RunStatus(row["status"]), executed_at=_parse_iso(row["executed_at"]),
            transitions_total=row["transitions_total"],
        )

    @staticmethod
    def _row_to_watermark(row: sqlite3.Row) -> ApiWatermark:
        return ApiWatermark(
            api_id=row["api_id"],
            last_pass_at=_parse_iso(row["last_pass_at"]) if row["last_pass_at"] else None,
            first_non_pass_at=_parse_iso(row["first_non_pass_at"]) if row["first_non_pass_at"] else None,
            last_non_pass_at=_parse_iso(row["last_non_pass_at"]) if row["last_non_pass_at"] else None,
            latest_status=RunStatus(row["latest_status"]) if row["latest_status"] else None,
            api_updated_at=_parse_iso(row["api_updated_at"]) if row["api_updated_at"] else None,
        )

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunResult:
        # sqlite3.Row's `in` operator tests *values*, not column names (unlike a dict) -- so this
        # must stay row.keys(), not `"body" in row`.
        body = row["body"] if "body" in row.keys() else None  # noqa: SIM118
        return RunResult(
            run_id=row["run_id"], api_id=row["api_id"], job_id=row["job_id"],
            target_env=row["target_env"], test_data_label=row["test_data_label"],
            status=RunStatus(row["status"]), http_status=row["http_status"], duration_ms=row["duration_ms"],
            executed_at=_parse_iso(row["executed_at"]), executed_by=row["executed_by"],
            failed_rules=json.loads(row["failed_rules"]) if row["failed_rules"] else [],
            response_body=json.loads(body) if body is not None else None,
        )

    @staticmethod
    def _runs_predicates(
        *, since: datetime | None = None, until: datetime | None = None, status: str | None = None,
        api_id: str | None = None, api_ids: Sequence[str] | None = None, executed_by: str | None = None,
        job_id: str | None = None,
    ) -> tuple[list[str], list]:
        clauses: list[str] = []
        params: list = []
        if since is not None:
            clauses.append("executed_at >= ?")
            params.append(_iso(since))
        if until is not None:
            clauses.append("executed_at < ?")
            params.append(_iso(until))
        if status is not None:
            if status == "non_pass":
                clauses.append("status != ?")
                params.append("pass")
            else:
                clauses.append("status = ?")
                params.append(status)
        if api_id is not None:
            clauses.append("api_id = ?")
            params.append(api_id)
        if api_ids is not None:
            placeholders = ",".join("?" for _ in api_ids)
            clauses.append(f"api_id IN ({placeholders})" if api_ids else "0")
            params.extend(api_ids)
        if executed_by is not None:
            clauses.append("executed_by = ?")
            params.append(executed_by)
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(job_id)
        return clauses, params

    def list_runs(self, q: RunsQuery) -> Page[RunResult]:
        clauses, params = self._runs_predicates(
            since=q.since, until=q.until, status=q.status, api_id=q.api_id,
            executed_by=q.executed_by, job_id=q.job_id,
        )
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = self._conn.execute(f"SELECT COUNT(*) FROM runs {where_sql}", params).fetchone()[0]

        keyset_clauses = list(clauses)
        keyset_params = list(params)
        if q.cursor:
            after_dt, after_id = decode_cursor(q.cursor)
            # qualified runs.run_id -- the LEFT JOIN below makes a bare "run_id" ambiguous
            # (bodies has its own run_id column).
            keyset_clauses.append("(runs.executed_at < ? OR (runs.executed_at = ? AND runs.run_id < ?))")
            keyset_params += [_iso(after_dt), _iso(after_dt), after_id]
        keyset_where = f"WHERE {' AND '.join(keyset_clauses)}" if keyset_clauses else ""
        rows = self._conn.execute(
            f"SELECT runs.*, bodies.body AS body FROM runs LEFT JOIN bodies ON bodies.run_id = runs.run_id "
            f"{keyset_where} ORDER BY executed_at DESC, run_id DESC LIMIT ?",
            [*keyset_params, q.limit + 1],
        ).fetchall()
        items = [self._row_to_run(r) for r in rows[: q.limit]]
        next_cursor = encode_cursor(items[-1].executed_at, items[-1].run_id) if len(rows) > q.limit else None
        return Page(items=items, next_cursor=next_cursor, total=total)

    def count_runs(
        self, since: datetime | None = None, until: datetime | None = None,
        status: str | None = None, api_id: str | None = None,
    ) -> int:
        clauses, params = self._runs_predicates(since=since, until=until, status=status, api_id=api_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._conn.execute(f"SELECT COUNT(*) FROM runs {where_sql}", params).fetchone()[0]

    def count_runs_by_job(self, since: datetime | None = None, until: datetime | None = None) -> dict[str, int]:
        """``{job_id: runs in the window}`` in one indexed GROUP BY -- the briefing's "which jobs
        executed last night, and how many runs each" without ever materializing a run."""
        clauses, params = self._runs_predicates(since=since, until=until)
        clauses.append("job_id IS NOT NULL")
        where_sql = f"WHERE {' AND '.join(clauses)}"
        rows = self._conn.execute(
            f"SELECT job_id, COUNT(*) AS n FROM runs {where_sql} GROUP BY job_id", params,
        ).fetchall()
        return {r["job_id"]: r["n"] for r in rows}

    def get_run(self, run_id: str) -> RunResult | None:
        row = self._conn.execute(
            "SELECT runs.*, bodies.body AS body FROM runs LEFT JOIN bodies ON bodies.run_id = runs.run_id "
            "WHERE runs.run_id = ?",
            (run_id,),
        ).fetchone()
        return self._row_to_run(row) if row is not None else None

    def runs_by_ids(self, run_ids: Sequence[str]) -> list[RunResult]:
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        rows = self._conn.execute(
            f"SELECT runs.*, bodies.body AS body FROM runs LEFT JOIN bodies ON bodies.run_id = runs.run_id "
            f"WHERE runs.run_id IN ({placeholders})",
            list(run_ids),
        ).fetchall()
        by_id = {r["run_id"]: self._row_to_run(r) for r in rows}
        return [by_id[i] for i in run_ids if i in by_id]

    def fetch_runs(
        self, *, since: datetime | None = None, until: datetime | None = None,
        api_id: str | None = None, api_ids: Sequence[str] | None = None,
        executed_by: str | None = None, job_id: str | None = None,
    ) -> list[RunResult]:
        """Windowed/scoped fetch (no body join -- callers here never need response_body) used by
        ``aggregate_runs`` and ``simulate_rule``: the SQL WHERE clause is the index-backed window,
        the caller does whatever bounded python pass it needs over the returned rows."""
        clauses, params = self._runs_predicates(
            since=since, until=until, api_id=api_id, api_ids=api_ids, executed_by=executed_by, job_id=job_id,
        )
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(f"SELECT * FROM runs {where_sql}", params).fetchall()
        return [self._row_to_run(r) for r in rows]

    def all_runs(self) -> list[RunResult]:
        rows = self._conn.execute(
            "SELECT runs.*, bodies.body AS body FROM runs LEFT JOIN bodies ON bodies.run_id = runs.run_id"
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def run_ids(self) -> list[str]:
        return [r["run_id"] for r in self._conn.execute("SELECT run_id FROM runs").fetchall()]

    def run_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    def get_body(self, run_id: str) -> dict | None:
        row = self._conn.execute("SELECT body FROM bodies WHERE run_id = ?", (run_id,)).fetchone()
        if row is None or row["body"] is None:
            return None
        return json.loads(row["body"])

    # -- rollup-fed aggregation (Task 8, spec §9) -------------------------------------------

    def aggregate_rollups(
        self, q: AggregateQuery, *, flaky_min: int, apis: Mapping[str, ApiSpec],
        briefing_tz: str = "Asia/Seoul",
    ) -> list[RunGroup]:
        """``aggregate_runs`` without a run scan: the group SET and every count come from
        ``rollup_day`` (whole local days) plus at most two exact edge queries over ``runs`` for the
        partial days a window starts/ends inside, so **every** API in the window is represented --
        no 2000-run sample. The derived fields come from the other materialized tables
        (``api_watermark`` for the api axis, ``current_state`` for the cell axis).

        Documented semantic deltas vs a from-scratch scan of every run in the window:

        * ``p95_duration_ms`` -- MAX of the per-day rollup p95s over the window's WHOLE days
          (§4's documented approximation; rollup_day never keeps raw durations), not a percentile
          over the union, and edge days do not contribute.
        * ``transitions`` -- the ingest-time per-cell counter summed over the window's whole days:
          the first run of a window is compared with the run before it (a window edge can add or
          drop one), and the sum is per CELL, so on the ``api``/``env`` axes it no longer counts
          flips of the interleaved multi-cell sequence (which was never what flaky_v1 means). The
          ``failed_rule``/``http_status`` axes have no per-key counter at all and report 0.
        * ``last_pass_before`` -- the watermark's ``last_pass_at`` (the API's last pass EVER), not
          "the last pass before the first failure inside the window"; with it, ``regression_suspect``
          is spec §3's rule ``last_pass_at < api.updated_at <= first_non_pass_at``, which an API
          that has since recovered no longer satisfies.
        * ``latest_status`` / ``first_non_pass_at`` -- from ``api_watermark`` (api axis) and
          ``current_state`` (cell axis), i.e. all-time, not clipped to the window; None on the
          ``env`` / ``failed_rule`` / ``http_status`` axes, which have no materialized source.
        * ``run_ids`` -- exact newest-50 for the api/env/cell axes (one indexed query per returned
          group); for the two map-keyed axes they come from one bounded newest-first scan
          (``_RUN_ID_SCAN_CAP`` rows), so a rare key in a huge window can return fewer than 50.
        * the window is exact: whole days come from the rollup, the partial first/last day from an
          indexed GROUP BY over ``runs`` (pure SQL, no RunResult is ever built).
        """
        interior, edges = _window_partitions(q.since, q.until, briefing_tz)
        acc: dict[str, list] = {}
        if interior is not None:
            self._fold_rollup_days(acc, q, interior)
        for lo, hi in edges:
            self._fold_edge_runs(acc, q, lo, hi)
        # Rank on plain lists and keep only the top `limit`, THEN build models: the cell axis can
        # reach one accumulator per (api, env, data) in the project (73k on the mid-scale set),
        # and both a full sort and a RunGroup per accumulator cost more than the SQL pass itself.
        # A zero-count group (possible only under a status filter) always sorts last, so trimming
        # them after the heap selection cannot drop a real group.
        status = q.status

        def rank(item: tuple[str, list]) -> tuple[int, int, str]:
            count, _passed, fail, error = _counts(item[1], status)
            return (-(fail + error), -count, item[0])

        top = heapq.nsmallest(q.limit, acc.items(), key=rank)
        groups = [
            _group_from_acc(key, row, counts, q, apis, flaky_min)
            for key, row, counts in ((k, r, _counts(r, status)) for k, r in top)
            if counts[0] > 0
        ]
        self._fill_run_ids(groups, q)
        self._fill_derived(groups, q, apis)
        return groups

    @staticmethod
    def _rollup_scope(q: AggregateQuery, interior: tuple[str | None, str | None]) -> tuple[list[str], list]:
        day_from, day_to = interior
        clauses: list[str] = []
        params: list = []
        if day_from is not None:
            clauses.append("day >= ?")
            params.append(day_from)
        if day_to is not None:
            clauses.append("day <= ?")
            params.append(day_to)
        if q.scope_api_ids is not None:
            placeholders = ",".join("?" for _ in q.scope_api_ids)
            clauses.append(f"api_id IN ({placeholders})" if q.scope_api_ids else "0")
            params.extend(q.scope_api_ids)
        return clauses, params

    def _tuples(self, sql: str, params: Sequence) -> list[tuple]:
        """Run a hot aggregate query with the plain-tuple row factory. The connection's default
        is ``sqlite3.Row``, whose per-row construction is a measurable share of a query that can
        return one row per cell in the project (73k on the mid-scale set)."""
        cursor = self._conn.cursor()
        cursor.row_factory = None
        return cursor.execute(sql, params).fetchall()

    def _fold_rollup_days(self, acc: dict[str, list], q: AggregateQuery, interior: tuple[str | None, str | None]) -> None:
        clauses, params = self._rollup_scope(q, interior)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        if q.group_by in _MAP_COLUMN:
            column = _MAP_COLUMN[q.group_by]
            rows = self._tuples(
                "SELECT je.key AS gkey, "
                "SUM(json_extract(je.value, '$.count')), "
                "SUM(json_extract(je.value, '$.fail')), "
                "SUM(json_extract(je.value, '$.error')), "
                "MAX(p95_duration_ms), COUNT(DISTINCT api_id) "
                f"FROM rollup_day, json_each(rollup_day.{column}) je {where_sql} GROUP BY gkey",
                params,
            )
            for key, count, fail, error, p95, api_count in rows:
                count, fail, error = count or 0, fail or 0, error or 0
                cell = acc.get(key)
                if cell is None:
                    # [count, passed, fail, error, transitions, p95, api_count] -- see _COUNT/_P95
                    acc[key] = [count, count - fail - error, fail, error, 0, p95, api_count or 0]
                    continue
                cell[_COUNT] += count
                cell[_PASSED] += count - fail - error
                cell[_FAIL] += fail
                cell[_ERROR] += error
                cell[_P95] = _max_or_none(cell[_P95], p95)
                cell[_API_COUNT] = max(cell[_API_COUNT] or 0, api_count or 0)
            return
        rows = self._tuples(
            f"SELECT {_ROLLUP_KEY_SQL[q.group_by]} AS gkey, "
            'SUM("count"), SUM("pass"), SUM(fail), SUM(error), SUM(transitions), MAX(p95_duration_ms) '
            f"FROM rollup_day {where_sql} GROUP BY gkey",
            params,
        )
        for key, count, passed, fail, error, transitions, p95 in rows:
            cell = acc.get(key)
            if cell is None:
                acc[key] = [count or 0, passed or 0, fail or 0, error or 0, transitions or 0, p95, None]
                continue
            cell[_COUNT] += count or 0
            cell[_PASSED] += passed or 0
            cell[_FAIL] += fail or 0
            cell[_ERROR] += error or 0
            cell[_TRANSITIONS] += transitions or 0
            cell[_P95] = _max_or_none(cell[_P95], p95)

    def _fold_edge_runs(self, acc: dict[str, list], q: AggregateQuery, lo: datetime, hi: datetime) -> None:
        """The partial day(s) a window starts/ends inside, counted EXACTLY from ``runs`` with an
        indexed range predicate -- one grouped SQL statement, no RunResult built. This is what
        keeps a 09:00→09:00 briefing window from being rounded out to two whole local days."""
        clauses, params = self._runs_predicates(since=lo, until=hi, api_ids=q.scope_api_ids)
        where_sql = " AND ".join(clauses)
        counters = "COUNT(*) AS c, SUM(status = 'fail') AS f, SUM(status = 'error') AS e"
        if q.group_by == "failed_rule":
            rows = self._conn.execute(
                "SELECT je.value AS gkey, COUNT(DISTINCT runs.run_id) AS c, "
                "COUNT(DISTINCT CASE WHEN status = 'fail' THEN runs.run_id END) AS f, "
                "COUNT(DISTINCT CASE WHEN status = 'error' THEN runs.run_id END) AS e "
                f"FROM runs, json_each(runs.failed_rules) je WHERE {where_sql} AND status != 'pass' GROUP BY gkey",
                params,
            ).fetchall()
            rows = [*rows, *self._conn.execute(
                "SELECT '(error) HTTP ' || COALESCE(CAST(http_status AS TEXT), '(none)') AS gkey, "
                f"{counters} FROM runs WHERE {where_sql} AND status != 'pass' "
                "AND json_array_length(COALESCE(failed_rules, '[]')) = 0 GROUP BY gkey",
                params,
            ).fetchall()]
        elif q.group_by == "http_status":
            rows = self._conn.execute(
                "SELECT COALESCE(CAST(http_status AS TEXT), '(none)') AS gkey, "
                f"{counters} FROM runs WHERE {where_sql} GROUP BY gkey", params,
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {_RUNS_KEY_SQL[q.group_by]} AS gkey, {counters} "
                f"FROM runs WHERE {where_sql} GROUP BY gkey", params,
            ).fetchall()
        for row in rows:
            cell = _acc_for(acc, row["gkey"])
            count, fail, error = row["c"] or 0, row["f"] or 0, row["e"] or 0
            cell[_COUNT] += count
            cell[_FAIL] += fail
            cell[_ERROR] += error
            cell[_PASSED] += count - fail - error

    def _fill_run_ids(self, groups: list[RunGroup], q: AggregateQuery) -> None:
        """Newest-first run ids (<= MAX_RUN_IDS) for each returned group. The api/env/cell axes get
        one indexed query per group (at most ``q.limit`` of them); the two map-keyed axes cannot be
        expressed as an indexed predicate, so they share ONE bounded newest-first scan."""
        if not groups:
            return
        base_clauses, base_params = self._runs_predicates(
            since=q.since, until=q.until, status=q.status, api_ids=q.scope_api_ids,
        )
        if q.group_by in _MAP_COLUMN:
            self._fill_run_ids_by_scan(groups, q, base_clauses, base_params)
            return
        for group in groups:
            clauses, params = list(base_clauses), list(base_params)
            if q.group_by == "api":
                clauses.append("api_id = ?")
                params.append(group.key)
            elif q.group_by == "env":
                clauses.append("target_env = ?")
                params.append(group.key)
            else:
                api_id, env, label = group.key.split("|", 2)
                clauses.append("api_id = ? AND target_env = ?")
                params += [api_id, env]
                if label == "-":
                    clauses.append("(test_data_label IS NULL OR test_data_label = '')")
                else:
                    clauses.append("test_data_label = ?")
                    params.append(label)
            rows = self._conn.execute(
                f"SELECT run_id FROM runs WHERE {' AND '.join(clauses)} "
                "ORDER BY executed_at DESC, run_id DESC LIMIT ?",
                [*params, MAX_RUN_IDS],
            ).fetchall()
            group.run_ids = [r["run_id"] for r in rows]

    def _fill_run_ids_by_scan(
        self, groups: list[RunGroup], q: AggregateQuery, clauses: list[str], params: list,
    ) -> None:
        wanted = {g.key: g for g in groups}
        buckets: dict[str, list[str]] = {key: [] for key in wanted}
        api_samples: dict[str, list[str]] = {key: [] for key in wanted}
        scan = list(clauses)
        if q.group_by == "failed_rule":
            scan.append("status != 'pass'")     # a pass run yields no failed_rule key (aggregation._keys)
        where_sql = f"WHERE {' AND '.join(scan)}" if scan else ""
        rows = self._conn.execute(
            f"SELECT run_id, api_id, status, http_status, failed_rules FROM runs {where_sql} "
            "ORDER BY executed_at DESC, run_id DESC LIMIT ?",
            [*params, _RUN_ID_SCAN_CAP],
        ).fetchall()
        filled = 0
        for row in rows:
            if q.group_by == "http_status":
                keys = [str(row["http_status"]) if row["http_status"] is not None else "(none)"]
            else:
                rules = json.loads(row["failed_rules"]) if row["failed_rules"] else []
                keys = list(dict.fromkeys(rules)) if rules else [
                    f"(error) HTTP {row['http_status'] if row['http_status'] is not None else '(none)'}"
                ]
            for key in keys:
                bucket = buckets.get(key)
                if bucket is None:
                    continue
                sample = api_samples[key]
                if len(sample) < _API_SAMPLE_CAP and row["api_id"] not in sample:
                    sample.append(row["api_id"])
                if len(bucket) >= MAX_RUN_IDS:
                    continue
                bucket.append(row["run_id"])
                if len(bucket) == MAX_RUN_IDS:
                    filled += 1
            if filled == len(buckets):
                break
        for key, ids in buckets.items():
            wanted[key].run_ids = ids
            wanted[key].api_sample = api_samples[key]

    def _fill_derived(self, groups: list[RunGroup], q: AggregateQuery, apis: Mapping[str, ApiSpec]) -> None:
        """Fields no counter can carry: the api axis reads ``api_watermark`` (+ the catalogue's
        updated_at) for spec §3's regression rule, the cell axis reads ``current_state``."""
        if q.group_by == "api":
            marks = {w.api_id: w for w in self.watermarks([g.key for g in groups])}
            for group in groups:
                mark = marks.get(group.key)
                if mark is None:
                    continue
                api = apis.get(group.key)
                updated_at = api.updated_at if api is not None else mark.api_updated_at
                group.latest_status = mark.latest_status
                group.first_non_pass_at = mark.first_non_pass_at
                group.last_pass_before = mark.last_pass_at
                group.api_updated_at = updated_at
                group.regression_suspect = bool(
                    updated_at is not None and mark.last_pass_at is not None
                    and mark.first_non_pass_at is not None
                    and mark.last_pass_at < updated_at <= mark.first_non_pass_at
                )
        elif q.group_by == "api_env_data":
            cells = {
                f"{c.api_id}|{c.target_env}|{c.test_data_label or '-'}": c
                for c in self.current_state([g.key.split("|", 2)[0] for g in groups])
            }
            for group in groups:
                cell = cells.get(group.key)
                if cell is not None:
                    group.latest_status = cell.status

    # -- current_state / watermarks / operator_scope: materialized reads (Task 5) ----------------

    def current_state(self, scope_api_ids: Sequence[str] | None = None) -> list[CellState]:
        """One indexed read of the materialized ``current_state`` table (PK
        ``(api_id, target_env, test_data_label)``) -- no scan over ``runs``, no python pass."""
        clauses: list[str] = []
        params: list = []
        if scope_api_ids is not None:
            placeholders = ",".join("?" for _ in scope_api_ids)
            clauses.append(f"api_id IN ({placeholders})" if scope_api_ids else "0")
            params.extend(scope_api_ids)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM current_state {where_sql} ORDER BY api_id, target_env, test_data_label",
            params,
        ).fetchall()
        return [self._row_to_cell(r) for r in rows]

    def watermarks(
        self, api_ids: Sequence[str] | None = None, first_non_pass_since: datetime | None = None,
        last_non_pass_since: datetime | None = None,
    ) -> list[ApiWatermark]:
        """One indexed read of ``api_watermark`` (PK ``api_id``). Both ``*_since`` filters are
        plain SQL predicates: a NULL column fails them, which is exactly the recompute oracle's
        "no non-pass run at all -> not a candidate".

        ``first_non_pass_since`` = the API's FIRST-EVER failure is at/after the cutoff (a newly
        broken API). ``last_non_pass_since`` = the API has SOME failure at/after the cutoff --
        the exact set a ``failed_since`` selection means, and the reason both exist."""
        clauses: list[str] = []
        params: list = []
        if api_ids is not None:
            placeholders = ",".join("?" for _ in api_ids)
            clauses.append(f"api_id IN ({placeholders})" if api_ids else "0")
            params.extend(api_ids)
        if first_non_pass_since is not None:
            clauses.append("first_non_pass_at >= ?")
            params.append(_iso(first_non_pass_since))
        if last_non_pass_since is not None:
            clauses.append("last_non_pass_at >= ?")
            params.append(_iso(last_non_pass_since))
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM api_watermark {where_sql} ORDER BY api_id", params,
        ).fetchall()
        return [self._row_to_watermark(r) for r in rows]

    def operator_scope_ids(self, operator_id: str, window_days: int, now: datetime) -> set[str]:
        """One indexed read of ``operator_api`` (PK ``(operator_id, api_id)`` -- the leading
        column makes this a range scan of one operator's rows, never a scan of ``runs``). Every
        id: use ``operator_scope_summary`` on a request path, where a heavy operator's scope can
        be tens of thousands of APIs and only a bounded sample is ever displayed."""
        cutoff = _iso(now - timedelta(days=window_days))
        rows = self._conn.execute(
            "SELECT api_id FROM operator_api WHERE operator_id = ? AND last_executed_at >= ?",
            (operator_id, cutoff),
        ).fetchall()
        return {r["api_id"] for r in rows}

    def operator_scope_summary(
        self, operator_id: str, window_days: int, now: datetime, limit: int = 100,
    ) -> tuple[list[str], int]:
        """``(sample, total)``: the first ``limit`` api_ids in api_id order plus the true count,
        as two indexed reads. The full id set is never materialized -- an operator who ran 16,000
        APIs would otherwise cost a 16,000-row fetch and a sort on every ``get_context``."""
        cutoff = _iso(now - timedelta(days=window_days))
        total = self._conn.execute(
            "SELECT COUNT(*) FROM operator_api WHERE operator_id = ? AND last_executed_at >= ?",
            (operator_id, cutoff),
        ).fetchone()[0]
        rows = self._conn.execute(
            "SELECT api_id FROM operator_api WHERE operator_id = ? AND last_executed_at >= ? "
            "ORDER BY api_id LIMIT ?",
            (operator_id, cutoff, limit),
        ).fetchall()
        return [r["api_id"] for r in rows], total

    # -- recompute oracles: the Task 4 on-demand implementations, kept ONLY so tests can assert
    #    the materialized tables above agree with a from-scratch scan of ``runs``. Nothing in the
    #    host or the agent calls these. ----------------------------------------------------------

    def recompute_current_state(self, scope_api_ids: Sequence[str] | None = None) -> list[CellState]:
        """Oracle (tests only): current_state recomputed by scanning ``runs``."""
        clauses, params = self._runs_predicates(api_ids=scope_api_ids)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cells = self._conn.execute(
            f"SELECT DISTINCT api_id, target_env, test_data_label FROM runs {where_sql}", params,
        ).fetchall()
        result: list[CellState] = []
        for cell in cells:
            api_id, env, label = cell["api_id"], cell["target_env"], cell["test_data_label"]
            if label is None:
                rows = self._conn.execute(
                    "SELECT run_id, status, executed_at FROM runs WHERE api_id = ? AND target_env = ? "
                    "AND test_data_label IS NULL ORDER BY executed_at ASC, run_id ASC",
                    (api_id, env),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT run_id, status, executed_at FROM runs WHERE api_id = ? AND target_env = ? "
                    "AND test_data_label = ? ORDER BY executed_at ASC, run_id ASC",
                    (api_id, env, label),
                ).fetchall()
            transitions = 0
            previous: bool | None = None
            for row in rows:
                is_pass = row["status"] == "pass"
                if previous is not None and is_pass != previous:
                    transitions += 1
                previous = is_pass
            latest = rows[-1]
            result.append(CellState(
                api_id=api_id, target_env=env, test_data_label=label, run_id=latest["run_id"],
                status=RunStatus(latest["status"]), executed_at=_parse_iso(latest["executed_at"]),
                transitions_total=transitions,
            ))
        return result

    def recompute_watermarks(
        self, api_ids: Sequence[str] | None = None, first_non_pass_since: datetime | None = None,
        last_non_pass_since: datetime | None = None,
    ) -> list[ApiWatermark]:
        """Oracle (tests only): watermarks recomputed by scanning ``runs``."""
        clauses, params = self._runs_predicates(api_ids=api_ids)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        present = self._conn.execute(f"SELECT DISTINCT api_id FROM runs {where_sql}", params).fetchall()
        catalogue = {
            r["api_id"]: _parse_iso(r["updated_at"])
            for r in self._conn.execute("SELECT api_id, updated_at FROM apis").fetchall()
        }
        result: list[ApiWatermark] = []
        for row in present:
            api_id = row["api_id"]
            rows = self._conn.execute(
                "SELECT status, executed_at FROM runs WHERE api_id = ? ORDER BY executed_at ASC, run_id ASC",
                (api_id,),
            ).fetchall()
            passes = [r["executed_at"] for r in rows if r["status"] == "pass"]
            non_passes = [r for r in rows if r["status"] != "pass"]
            first_non_pass = _parse_iso(non_passes[0]["executed_at"]) if non_passes else None
            last_non_pass = _parse_iso(non_passes[-1]["executed_at"]) if non_passes else None
            if first_non_pass_since is not None and (first_non_pass is None or first_non_pass < first_non_pass_since):
                continue
            if last_non_pass_since is not None and (last_non_pass is None or last_non_pass < last_non_pass_since):
                continue
            result.append(ApiWatermark(
                api_id=api_id,
                last_pass_at=_parse_iso(passes[-1]) if passes else None,
                first_non_pass_at=first_non_pass,
                last_non_pass_at=last_non_pass,
                latest_status=RunStatus(rows[-1]["status"]) if rows else None,
                api_updated_at=catalogue.get(api_id),
            ))
        return result

    def recompute_operator_scope_ids(self, operator_id: str, window_days: int, now: datetime) -> set[str]:
        """Oracle (tests only): operator scope recomputed by scanning ``runs``."""
        cutoff = _iso(now - timedelta(days=window_days))
        rows = self._conn.execute(
            "SELECT DISTINCT api_id FROM runs WHERE executed_by = ? AND executed_at >= ?",
            (operator_id, cutoff),
        ).fetchall()
        return {r["api_id"] for r in rows}

    # -- audit log ---------------------------------------------------------------------------

    def append_audit(
        self, at: datetime, operator: str, action: str, target_kind: str, target_id: str, session_id: str,
    ) -> AuditEntry:
        cur = self._conn.execute(
            "INSERT INTO audit_log (at, operator, action, target_kind, target_id, session_id) VALUES (?,?,?,?,?,?)",
            (_iso(at), operator, action, target_kind, target_id, session_id),
        )
        self._conn.commit()
        return AuditEntry(
            seq=cur.lastrowid, at=at, operator=operator, action=action,
            target_kind=target_kind, target_id=target_id, session_id=session_id,
        )

    def insert_audit_entries(self, entries: Iterable[AuditEntry]) -> None:
        """Explicit-seq insert -- for tests that need to control seq/at directly (e.g. a same-``at``
        seq 9/10 tie-break); MockAtworks.append_audit goes through ``append_audit`` above instead."""
        self._conn.executemany(
            "INSERT OR REPLACE INTO audit_log (seq, at, operator, action, target_kind, target_id, session_id) "
            "VALUES (?,?,?,?,?,?,?)",
            [(e.seq, _iso(e.at), e.operator, e.action, e.target_kind, e.target_id, e.session_id) for e in entries],
        )
        self._conn.commit()

    @staticmethod
    def _row_to_audit(row: sqlite3.Row) -> AuditEntry:
        return AuditEntry(
            seq=row["seq"], at=_parse_iso(row["at"]), operator=row["operator"], action=row["action"],
            target_kind=row["target_kind"], target_id=row["target_id"], session_id=row["session_id"],
        )

    def audit(self, cursor: str | None = None, limit: int = 50) -> Page[AuditEntry]:
        total = self._conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        clauses: list[str] = []
        params: list = []
        if cursor:
            after_dt, after_seq = decode_cursor(cursor)
            clauses.append("(at < ? OR (at = ? AND seq < ?))")
            params += [_iso(after_dt), _iso(after_dt), int(after_seq)]
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM audit_log {where_sql} ORDER BY at DESC, seq DESC LIMIT ?", [*params, limit + 1],
        ).fetchall()
        items = [self._row_to_audit(r) for r in rows[:limit]]
        # seq is zero-padded to a fixed width in the cursor id so the tie-break orders
        # numerically, not lexicographically (mirrors the pre-SQL _page() key).
        next_cursor = encode_cursor(items[-1].at, f"{items[-1].seq:020d}") if len(rows) > limit else None
        return Page(items=items, next_cursor=next_cursor, total=total)
