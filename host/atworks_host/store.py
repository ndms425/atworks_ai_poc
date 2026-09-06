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

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from atworks_agent import (
    AggregateQuery,
    ApiSpec,
    ApiWatermark,
    AuditEntry,
    CellKey,
    CellState,
    Insights,
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
-- The archive is READ through the same `list_runs`/`count_runs` code path as the hot partition
-- (`FROM runs_archive AS runs`), so it needs the same two access shapes, or a cold page degrades
-- into a full scan + temp sort exactly where the hot one is a seek.
CREATE INDEX IF NOT EXISTS idx_runs_archive_executed ON runs_archive(executed_at DESC, run_id DESC);
CREATE INDEX IF NOT EXISTS idx_runs_archive_api_executed ON runs_archive(api_id, executed_at DESC);

CREATE TABLE IF NOT EXISTS bodies (
    run_id TEXT PRIMARY KEY,
    body JSON,
    captured_at TEXT
);
-- Retention deletes bodies in bounded slices (`delete_bodies_before(limit=...)`); without this
-- index every slice re-scans the whole table to find the next N expired rows.
CREATE INDEX IF NOT EXISTS idx_bodies_captured_at ON bodies(captured_at);

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
-- The whole-day fold (`Store._rollup_arm`) reads the group key AND every counter of every rollup
-- row in the window, so this index carries them all: on the api/env/cell axes SQLite answers the
-- aggregate from an index-only scan instead of one table seek per in-window row. Measured on the
-- 450k-run bench set (225,270 rollup rows, 73,926 of them in a 30-day window): group_by=api
-- 116ms -> 59ms, group_by=api_env_data 246ms -> 117ms.
-- `idx_rollup_day_day` above STAYS: the two map axes read `failed_rule_counts` /
-- `http_status_counts`, which no reasonable index can cover, and there the planner rightly wants
-- the NARROW day index -- forced onto the wide one (or onto the PK autoindex, its fallback when
-- the narrow one is gone) group_by=http_status measured 151ms -> 197ms. Two day-leading indexes,
-- one per shape of read.
CREATE INDEX IF NOT EXISTS idx_rollup_day_covering ON rollup_day(
    day, api_id, target_env, test_data_label,
    "count", "pass", fail, error, transitions, p95_duration_ms);
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
-- The operator scope predicate (`_operator_scope_clause`) is
-- `api_id IN (SELECT api_id FROM operator_api WHERE operator_id = ? AND last_executed_at >= ?)`,
-- and it rides EVERY scoped read. Without this index the PK autoindex gives the operator's rows
-- but `last_executed_at` costs a table seek per row (19,878 of them for the bench set's busiest
-- operator, on every scoped query); with it the subquery is one covering range scan.
-- Measured: `operator_scope_summary` 53ms -> 8ms, `watermarks(scope_operator=…)` 83ms -> 32ms.
CREATE INDEX IF NOT EXISTS idx_operator_api_window ON operator_api(operator_id, last_executed_at);

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

-- One row per retention STEP per local day (spec §3/§6): `partition_key` is `<step>:<YYYY-MM-DD>`
-- (plus the `daily:<YYYY-MM-DD>` guard row that makes the tick-tail job run once a day), and
-- `archived_at` is when that step ran. Append/overwrite only; nothing reads it but the day guard.
CREATE TABLE IF NOT EXISTS retention_state (
    partition_key TEXT PRIMARY KEY,
    archived_at TEXT NOT NULL
);
"""


class _AllDays:
    """Sentinel type for ``archive_runs_before(day=...)``. A plain ``None`` default is taken: a
    ``day`` column CAN be NULL, and "the partition whose day is NULL" has to stay expressible."""


#: ``archive_runs_before``'s default -- "every day partition, one statement".
ALL_DAYS = _AllDays()


def _iso(dt: datetime) -> str:
    """UTC, fixed-width (always 6 fractional digits), ``Z``-suffixed -- see module docstring."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _parse_iso(value: str) -> datetime:
    """Inverse of ``_iso``. ``fromisoformat`` is the C fast path and understands both the trailing
    ``Z`` and the 6-digit fraction ``_iso`` always writes; ``strptime`` on the same string costs
    ~11us each and was 74ms of one insight-panel build alone (6,708 timestamps). ``strptime`` stays
    as the fallback so a row written by an older, differently-shaped writer still parses."""
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
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


def _rank_exprs(q: AggregateQuery) -> tuple[str, str]:
    """``(ORDER BY, the HAVING count expression)`` for ``q``, over the outer SELECT's aliases
    (``c`` count, ``p`` passed, ``f`` fail, ``e`` error, ``t`` transitions).

    This is the rank that used to run in python over EVERY group in the window; it now sits next
    to the GROUP BY, so only ``q.limit`` rows ever cross into python (the cell axis reaches one
    group per (api, env, data) in the project -- 62,748 of them for one operator on the 450k-run
    bench set). ``q.status`` picks which counters the rank reads, exactly as ``_counts`` does:
    a status filter leaves a single-verdict population, so its transition count is 0 by
    construction (``_group_from_acc``) and the ``transitions`` order degenerates into the
    ``failures`` one -- the same thing the python rank did.

    ``HAVING`` drops the zero-count groups a status filter can produce. Those always sorted LAST
    in the python rank (a 0 in the leading term, then a 0 count), so they were only ever kept when
    fewer than ``limit`` real groups existed -- dropping them in SQL returns the same groups, and
    frees the slot for a real one instead of a placeholder that was trimmed after the cut.

    A term that is constant-zero for every group under this query is written as ``None`` and left
    OUT of the ORDER BY rather than emitted as a literal ``0`` -- a bare integer literal in an
    SQLite ORDER BY is a column ORDINAL, not a value. Dropping it is exactly equivalent: a
    constant leading term ranks nothing.
    """
    status = q.status
    failures: str | None
    if status == "pass":
        failures, count = None, "SUM(p)"
    elif status == "fail":
        failures = count = "SUM(f)"
    elif status == "error":
        failures = count = "SUM(e)"
    elif status == "non_pass":
        failures = count = "SUM(f) + SUM(e)"
    else:
        failures, count = "SUM(f) + SUM(e)", "SUM(c)"
    transitions = None if status else "SUM(t)"
    terms = [transitions, failures] if q.order_by == "transitions" else [failures, count]
    order = "".join(f"{term} DESC, " for term in terms if term is not None) + "gkey ASC"
    return order, count


def _operator_scope_clause(
    operator_id: str | None, since: datetime | None, column: str = "api_id",
    *, keep_outer_index: bool = False,
) -> tuple[list[str], list]:
    """The server-side operator scope, as a SQL predicate rather than an id list (Task 8 fix
    round 1). ``operator_api`` is keyed ``(operator_id, api_id)`` and carries that pair's
    ``last_executed_at``, so "the APIs this operator executed at or after ``since``" is a range
    scan of one operator's rows -- and the caller never has to materialize, cap, or ship those ids.
    A capped id list was the bug: 100 ids meant a 16k-API operator's panel covered 100 APIs.

    ``keep_outer_index`` writes the term as ``+api_id IN (...)``. The unary ``+`` is SQLite's
    "do not treat this as an indexable term" marker, and it is what the callers whose outer query
    already NEEDS an index want: without it the planner drives ``rollup_day`` off
    ``idx_rollup_day_api_day`` (one seek per scoped API) and ``runs`` off
    ``idx_runs_api_executed_at``, losing the day range and the newest-first ORDER BY to a temp
    b-tree. Measured on the 450k-run mid set: the evidence scan goes 190ms -> 66ms and the rollup
    fold 147ms -> 113ms for a 16k-API scope, at a bounded ~50ms cost for a tiny one (whose plan
    then matches the unscoped query's, which is inside its own SLO by construction).
    """
    if operator_id is None:
        return [], []
    prefix = "+" if keep_outer_index else ""
    sql = f"{prefix}{column} IN (SELECT api_id FROM operator_api WHERE operator_id = ?"
    params: list = [operator_id]
    if since is not None:
        sql += " AND last_executed_at >= ?"
        params.append(_iso(since))
    return [sql + ")"], params


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
        # `retention_state` was created in Task 4 as a generic (key, value) pair table and never
        # written by anything; Task 9 gives it the spec's own columns. A store written before
        # this keeps the old shape (CREATE TABLE IF NOT EXISTS never reshapes), so drop it --
        # there is by construction nothing in it to lose.
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(retention_state)").fetchall()}
        if columns and "partition_key" not in columns:
            self._conn.execute("DROP TABLE retention_state")
            self._conn.execute(
                "CREATE TABLE retention_state (partition_key TEXT PRIMARY KEY, archived_at TEXT NOT NULL)"
            )

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

    # The run rows the portal and the model read carry a display label joined from the `apis`
    # mirror (Task 10). Both joins are PK lookups applied to the rows the keyset LIMIT already
    # chose, and neither table contributes an `executed_at`/`run_id` of its own to the ORDER BY,
    # so `list_runs` keeps riding idx_runs_executed_at exactly as it did with the body join alone.
    # The aggregation-only SELECT (`fetch_runs`) stays label-free -- it never renders a row.
    # The label join goes through a projecting subquery rather than `LEFT JOIN apis`: `apis` has
    # an `api_id` column of its own, and the run predicates below are written unqualified (they
    # also run against `runs_archive`, which has no `runs` alias), so a bare join would make
    # `api_id = ?` ambiguous. Renaming the joined columns keeps every predicate untouched; SQLite
    # flattens the subquery, so the join is still a PK lookup on apis.
    _RUN_SELECT = "runs.*, bodies.body AS body, label.api_method AS api_method, label.api_path AS api_path"
    _RUN_JOINS = ("LEFT JOIN bodies ON bodies.run_id = runs.run_id "
                  "LEFT JOIN (SELECT api_id AS label_id, method AS api_method, path AS api_path FROM apis) "
                  "AS label ON label.label_id = runs.api_id")

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunResult:
        # sqlite3.Row's `in` operator tests *values*, not column names (unlike a dict) -- so this
        # must stay row.keys(), not `"body" in row`.
        columns = row.keys()
        body = row["body"] if "body" in columns else None
        # api_method/api_path are the row's DISPLAY LABEL, joined from the apis mirror by the
        # SELECTs that feed `run_record` (Task 10: RunsView renders "GET /orders" instead of
        # downloading 500 API specs to look the label up client-side). The aggregation-only
        # SELECTs don't join, so both stay None there -- exclude_none drops them from the record.
        return RunResult(
            run_id=row["run_id"], api_id=row["api_id"], job_id=row["job_id"],
            target_env=row["target_env"], test_data_label=row["test_data_label"],
            status=RunStatus(row["status"]), http_status=row["http_status"], duration_ms=row["duration_ms"],
            executed_at=_parse_iso(row["executed_at"]), executed_by=row["executed_by"],
            failed_rules=json.loads(row["failed_rules"]) if row["failed_rules"] else [],
            response_body=json.loads(body) if body is not None else None,
            api_method=row["api_method"] if "api_method" in columns else None,
            api_path=row["api_path"] if "api_path" in columns else None,
        )

    @staticmethod
    def _runs_predicates(
        *, since: datetime | None = None, until: datetime | None = None, status: str | None = None,
        api_id: str | None = None, api_ids: Sequence[str] | None = None, executed_by: str | None = None,
        job_id: str | None = None, scope_operator: str | None = None, scope_since: datetime | None = None,
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
        scope_clauses, scope_params = _operator_scope_clause(
            scope_operator, scope_since, keep_outer_index=True)
        clauses.extend(scope_clauses)
        params.extend(scope_params)
        return clauses, params

    def list_runs_sql(self, q: RunsQuery) -> tuple[tuple[str, list], tuple[str, list]]:
        """``((count_sql, count_params), (page_sql, page_params))`` -- the two statements
        ``list_runs`` executes, verbatim. Built here rather than inline so the query-plan tests can
        EXPLAIN the REAL query: a hand-copied SQL string in a test rots silently the first time the
        select list, the joins or the keyset clause change, and then it proves nothing."""
        clauses, params = self._runs_predicates(
            since=q.since, until=q.until, status=q.status, api_id=q.api_id,
            executed_by=q.executed_by, job_id=q.job_id,
        )
        # The cold partition is the SAME contract read against another table: aliasing it to
        # `runs` keeps every predicate, the keyset clause and the body join byte-identical, so
        # an archived page can never drift from a hot one (spec §6 "조회는 명시적 archived=true").
        source = "runs_archive AS runs" if q.archived else "runs"
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        count = (f"SELECT COUNT(*) FROM {source} {where_sql}", list(params))

        keyset_clauses = list(clauses)
        keyset_params = list(params)
        if q.cursor:
            after_dt, after_id = decode_cursor(q.cursor)
            # qualified runs.run_id -- the LEFT JOIN below makes a bare "run_id" ambiguous
            # (bodies has its own run_id column).
            keyset_clauses.append("(runs.executed_at < ? OR (runs.executed_at = ? AND runs.run_id < ?))")
            keyset_params += [_iso(after_dt), _iso(after_dt), after_id]
        keyset_where = f"WHERE {' AND '.join(keyset_clauses)}" if keyset_clauses else ""
        page = (
            f"SELECT {self._RUN_SELECT} FROM {source} {self._RUN_JOINS} "
            f"{keyset_where} ORDER BY runs.executed_at DESC, runs.run_id DESC LIMIT ?",
            [*keyset_params, q.limit + 1],
        )
        return count, page

    def list_runs(self, q: RunsQuery) -> Page[RunResult]:
        (count_sql, count_params), (page_sql, page_params) = self.list_runs_sql(q)
        total = self._conn.execute(count_sql, count_params).fetchone()[0]
        rows = self._conn.execute(page_sql, page_params).fetchall()
        items = [self._row_to_run(r) for r in rows[: q.limit]]
        next_cursor = encode_cursor(items[-1].executed_at, items[-1].run_id) if len(rows) > q.limit else None
        return Page(items=items, next_cursor=next_cursor, total=total)

    def count_runs(
        self, since: datetime | None = None, until: datetime | None = None,
        status: str | None = None, api_id: str | None = None, archived: bool = False,
    ) -> int:
        clauses, params = self._runs_predicates(since=since, until=until, status=status, api_id=api_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        source = "runs_archive" if archived else "runs"
        return self._conn.execute(f"SELECT COUNT(*) FROM {source} {where_sql}", params).fetchone()[0]

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
            f"SELECT {self._RUN_SELECT} FROM runs {self._RUN_JOINS} WHERE runs.run_id = ?",
            (run_id,),
        ).fetchone()
        return self._row_to_run(row) if row is not None else None

    def runs_by_ids(self, run_ids: Sequence[str]) -> list[RunResult]:
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        rows = self._conn.execute(
            f"SELECT {self._RUN_SELECT} FROM runs {self._RUN_JOINS} "
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
            f"SELECT {self._RUN_SELECT} FROM runs {self._RUN_JOINS}"
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

    # -- retention (Task 9, spec §6) ----------------------------------------------------------

    #: the `runs` columns `runs_archive` mirrors, in order -- the archive adds only `archived_at`.
    _RUN_COLUMNS = (
        "run_id, api_id, job_id, target_env, test_data_label, status, http_status, duration_ms, "
        "executed_at, executed_by, failed_rules, day"
    )

    def newest_run_at(self) -> datetime | None:
        """The newest ``executed_at`` in the hot partition, or None when it is empty. Public
        because the bench needs it to age a whole dataset out in one retention pass without
        reaching into the connection or a private parser."""
        newest = self._conn.execute("SELECT MAX(executed_at) FROM runs").fetchone()[0]
        return _parse_iso(newest) if newest is not None else None

    def run_days_before(self, cutoff: datetime) -> list[str | None]:
        """The distinct ``day`` partitions holding runs older than ``cutoff``, oldest first —
        the unit retention archives in (spec §6 "일 단위 이관"). One day per transaction is what
        lets the daily job hand the event loop back between partitions instead of holding it for
        the length of one whole-dataset statement."""
        return [
            row[0] for row in self._conn.execute(
                "SELECT DISTINCT day FROM runs WHERE executed_at < ? ORDER BY day", (_iso(cutoff),)
            ).fetchall()
        ]

    def archive_runs_before(self, cutoff: datetime, archived_at: datetime,
                            day: str | None | _AllDays = ALL_DAYS) -> int:
        """Move (never delete) runs older than ``cutoff`` from the hot partition into
        ``runs_archive``, in ONE transaction. ``day`` scopes the move to a single ``day``
        partition (``ALL_DAYS``, the default, moves every one of them at once); ``day IS ?`` is
        null-safe, so a row whose partition was never stamped is still reachable.

        Returns how many rows were **deleted from ``runs``** — the true moved count. The INSERT's
        rowcount would under-report a re-run over rows already sitting in the archive
        (``INSERT OR IGNORE``), and the archive is exactly the place where a re-run is expected.
        Rollups, watermarks, cell state and the report artifacts are permanent and are
        deliberately untouched: the aggregate reads keep answering identically after a cold move
        (spec §6)."""
        scope = "" if day is ALL_DAYS else " AND day IS ?"
        params: tuple[Any, ...] = () if day is ALL_DAYS else (day,)
        self._conn.execute(
            f"INSERT OR IGNORE INTO runs_archive ({self._RUN_COLUMNS}, archived_at) "
            f"SELECT {self._RUN_COLUMNS}, ? FROM runs WHERE executed_at < ?{scope}",
            (_iso(archived_at), _iso(cutoff), *params),
        )
        moved = self._conn.execute(
            f"DELETE FROM runs WHERE executed_at < ?{scope}", (_iso(cutoff), *params)
        ).rowcount
        self._conn.commit()
        return moved

    def delete_bodies_before(self, cutoff: datetime, limit: int | None = None) -> int:
        """The ONE tier that is actually deleted (spec §6): a captured response body is the only
        data that can carry personal or financial values, so it is dropped at
        ``retention_body_days`` and a parity re-diff outside that window falls back to a
        status-only verdict with a "본문 만료" note.

        ``limit`` bounds ONE statement to that many rows (the caller loops until it returns 0), so
        the daily job never holds the event loop inside a single multi-hundred-thousand-row
        DELETE. ``DELETE ... LIMIT`` is a compile-time SQLite option, hence the ``run_id IN
        (SELECT ... LIMIT ?)`` shape, which every build has."""
        if limit is None:
            deleted = self._conn.execute(
                "DELETE FROM bodies WHERE captured_at < ?", (_iso(cutoff),)).rowcount
        else:
            deleted = self._conn.execute(
                "DELETE FROM bodies WHERE run_id IN "
                "(SELECT run_id FROM bodies WHERE captured_at < ? LIMIT ?)",
                (_iso(cutoff), limit),
            ).rowcount
        self._conn.commit()
        return deleted

    def purge_runs_archive(self) -> int:
        """Cold DELETE -- only ever called once ``retention_cold_until`` is both set and past
        (project end + 1 year). Retention.run is the only caller, and it is the only place in
        this codebase that removes a run record at all."""
        deleted = self._conn.execute("DELETE FROM runs_archive").rowcount
        self._conn.commit()
        return deleted

    def get_retention_state(self, partition_key: str) -> datetime | None:
        row = self._conn.execute(
            "SELECT archived_at FROM retention_state WHERE partition_key = ?", (partition_key,)
        ).fetchone()
        return _parse_iso(row["archived_at"]) if row is not None else None

    def set_retention_state(self, partition_key: str, archived_at: datetime) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO retention_state (partition_key, archived_at) VALUES (?,?)",
            (partition_key, _iso(archived_at)),
        )
        self._conn.commit()

    def retention_state(self) -> dict[str, datetime]:
        return {
            r["partition_key"]: _parse_iso(r["archived_at"])
            for r in self._conn.execute("SELECT * FROM retention_state").fetchall()
        }

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
          (``_RUN_ID_SCAN_CAP`` rows), so a rare key in a huge window can return fewer than 50 --
          or, if every run of that key is older than the newest ``_RUN_ID_SCAN_CAP``, NONE at all
          (``run_ids == []`` with a non-zero ``count``). ``q.include_run_ids=False`` skips the fill
          entirely (and with it ``api_sample``, which the same scan produces).
        * ``regression_suspect`` on the api axis is additionally windowed: the watermark's
          ``first_non_pass_at`` must fall at or after ``q.since``, the same predicate
          ``summarize_insights`` counts with -- one definition of "regression suspect", not two.
        * the window is exact: whole days come from the rollup, the partial first/last day from an
          indexed read of ``runs`` in the SAME statement (pure SQL, no RunResult is ever built).

        Task 8 fix round 2: the rank and the cut are **in the SQL**. One statement per call --
        a ``UNION ALL`` of the rollup arm and the (at most two) edge arms, one GROUP BY over it,
        ``ORDER BY`` per ``q.order_by`` and ``LIMIT q.limit`` -- so python receives ``q.limit``
        rows instead of one accumulator per group in the window.
        """
        interior, edges = _window_partitions(q.since, q.until, briefing_tz)
        arms = [self._rollup_arm(q, interior)] if interior is not None else []
        arms += [self._edge_arm(q, lo, hi) for lo, hi in edges]
        groups = self._top_groups(q, arms, apis, flaky_min)
        if q.include_run_ids:
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
        # The operator scope's window lower bound is the QUERY's own `since` -- "the APIs this
        # operator executed inside the window we are aggregating", which is the only bound that
        # makes the scoped aggregate self-consistent.
        scope_clauses, scope_params = _operator_scope_clause(
            q.scope_operator, q.since, keep_outer_index=True)
        clauses.extend(scope_clauses)
        params.extend(scope_params)
        return clauses, params

    def _tuples(self, sql: str, params: Sequence) -> list[tuple]:
        """Run a hot aggregate query with the plain-tuple row factory. The connection's default
        is ``sqlite3.Row``, whose per-row construction is a measurable share of a query that can
        return one row per cell in the project (73k on the mid-scale set)."""
        cursor = self._conn.cursor()
        cursor.row_factory = None
        return cursor.execute(sql, params).fetchall()

    def _rollup_arm(self, q: AggregateQuery, interior: tuple[str | None, str | None]) -> tuple[str, list]:
        """The whole local days of the window, shaped to the union's column list
        (``gkey, c, p, f, e, t, q95[, n]``).

        The api/env/cell axes emit one raw rollup ROW and let the outer GROUP BY do all the work:
        the cell axis has roughly one group per row anyway, so grouping twice only adds a second
        temp b-tree (measured 239ms -> 273ms on the mid bench set). The two map axes GROUP HERE:
        their key lives inside a JSON column, so ``json_each`` multiplies every rollup row by its
        key count -- a quarter-million tuples on the mid set -- which collapse to a dozen groups.
        Grouping them in the arm keeps that collapse where it was before the union, and keeps
        ``api_count`` what it always was: ``COUNT(DISTINCT api_id)`` over the WHOLE DAYS, carried
        out as ``n`` and merged with MAX (an edge day contributes no api_count, as before)."""
        clauses, params = self._rollup_scope(q, interior)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        if q.group_by in _MAP_COLUMN:
            column = _MAP_COLUMN[q.group_by]
            count, fail, error = (f"json_extract(je.value, '$.{name}')" for name in ("count", "fail", "error"))
            return (
                f"SELECT je.key AS gkey, SUM({count}) AS c, SUM({count} - {fail} - {error}) AS p, "
                f"SUM({fail}) AS f, SUM({error}) AS e, 0 AS t, MAX(p95_duration_ms) AS q95, "
                "COUNT(DISTINCT api_id) AS n "
                f"FROM rollup_day, json_each(rollup_day.{column}) je {where_sql} GROUP BY gkey"
            ), params
        return (
            f'SELECT {_ROLLUP_KEY_SQL[q.group_by]} AS gkey, "count" AS c, "pass" AS p, fail AS f, '
            f"error AS e, transitions AS t, p95_duration_ms AS q95 FROM rollup_day {where_sql}"
        ), params

    def _edge_arm(self, q: AggregateQuery, lo: datetime, hi: datetime) -> tuple[str, list]:
        """One row per RUN of a partial day the window starts/ends inside, in the same column
        shape -- counted exactly from ``runs`` with an indexed range predicate, no RunResult built.
        This is what keeps a 09:00→09:00 briefing window from being rounded out to two whole local
        days. Edge rows carry no transitions and no p95 (the rollup owns both).

        ``failed_rule`` needs both halves of ``aggregation._keys`` -- one row per DISTINCT rule
        name of a non-pass run, and the synthetic ``(error) HTTP …`` key for a non-pass run that
        named no rule. ``LEFT JOIN json_each`` gives exactly that in one pass: a run with an empty
        (or NULL) ``failed_rules`` produces a single NULL-padded row, which the COALESCE turns into
        the synthetic key, and ``SELECT DISTINCT`` over ``(key, run_id)`` collapses a rule the run
        listed twice.

        Map-axis edges group in the arm, for the reason ``_rollup_arm`` gives, and report ``n``
        (api_count) as 0: an edge day never contributed to that figure and still does not."""
        clauses, params = self._runs_predicates(
            since=lo, until=hi, api_ids=q.scope_api_ids,
            scope_operator=q.scope_operator, scope_since=q.since,
        )
        where_sql = " AND ".join(clauses)
        verdicts = "(status = 'pass') AS p, (status = 'fail') AS f, (status = 'error') AS e"
        sums = "SUM(status = 'fail') AS f, SUM(status = 'error') AS e"
        if q.group_by == "failed_rule":
            return (
                "SELECT gkey, COUNT(*) AS c, 0 AS p, SUM(st = 'fail') AS f, SUM(st = 'error') AS e, "
                "0 AS t, NULL AS q95, 0 AS n "
                "FROM (SELECT DISTINCT COALESCE(je.value, '(error) HTTP ' || "
                "COALESCE(CAST(runs.http_status AS TEXT), '(none)')) AS gkey, runs.run_id AS rid, "
                "runs.status AS st FROM runs "
                f"LEFT JOIN json_each(runs.failed_rules) je WHERE {where_sql} AND status != 'pass') "
                "GROUP BY gkey"
            ), params
        if q.group_by == "http_status":
            return (
                "SELECT COALESCE(CAST(http_status AS TEXT), '(none)') AS gkey, COUNT(*) AS c, "
                f"SUM(status = 'pass') AS p, {sums}, 0 AS t, NULL AS q95, 0 AS n "
                f"FROM runs WHERE {where_sql} GROUP BY gkey"
            ), params
        return (
            f"SELECT {_RUNS_KEY_SQL[q.group_by]} AS gkey, 1 AS c, {verdicts}, 0 AS t, "
            f"NULL AS q95 FROM runs WHERE {where_sql}"
        ), params

    def _top_groups(
        self, q: AggregateQuery, arms: Sequence[tuple[str, list]], apis: Mapping[str, ApiSpec],
        flaky_min: int,
    ) -> list[RunGroup]:
        """Fold every arm in ONE statement and let SQLite do the rank and the cut. Python sees
        ``q.limit`` rows; the counters it reads out of them still go through ``_counts`` /
        ``_group_from_acc``, so a status filter means the same thing here as everywhere else."""
        is_map = q.group_by in _MAP_COLUMN
        order_sql, count_sql = _rank_exprs(q)
        columns = "SUM(c) AS c, SUM(p) AS p, SUM(f) AS f, SUM(e) AS e, SUM(t) AS t, MAX(q95) AS q95"
        if is_map:
            # api_count comes pre-counted per arm (COUNT(DISTINCT api_id) over the whole days,
            # 0 on an edge) and merges with MAX -- exactly the pre-union fold's rule.
            columns += ", MAX(n) AS n"
        union = " UNION ALL ".join(sql for sql, _ in arms)
        params = [value for _, arm_params in arms for value in arm_params]
        rows = self._tuples(
            f"SELECT gkey, {columns} FROM ({union}) GROUP BY gkey "
            f"HAVING {count_sql} > 0 ORDER BY {order_sql} LIMIT ?",
            [*params, q.limit],
        )
        groups: list[RunGroup] = []
        for row in rows:
            key, count, passed, fail, error, transitions, p95 = row[:7]
            # [count, passed, fail, error, transitions, p95, api_count] -- see _COUNT/_API_COUNT
            acc = [count or 0, passed or 0, fail or 0, error or 0, transitions or 0, p95,
                   (row[7] or 0) if is_map else None]
            groups.append(_group_from_acc(key, acc, _counts(acc, q.status), q, apis, flaky_min))
        return groups

    def _fill_run_ids(self, groups: list[RunGroup], q: AggregateQuery) -> None:
        """Newest-first run ids (<= MAX_RUN_IDS) for each returned group. The api/env/cell axes get
        one indexed query per group (at most ``q.limit`` of them); the two map-keyed axes cannot be
        expressed as an indexed predicate, so they share ONE bounded newest-first scan.

        The operator scope is applied only where the group key does NOT already pin ``api_id``:
        on the ``api`` and ``api_env_data`` axes the key IS an api_id that the rollup fold already
        filtered through the scope, so repeating the ``operator_api`` subquery in each of the up-to
        ``q.limit`` per-group queries would only re-derive a decided fact -- at measurable cost."""
        if not groups:
            return
        key_pins_api = q.group_by in ("api", "api_env_data")
        base_clauses, base_params = self._runs_predicates(
            since=q.since, until=q.until, status=q.status, api_ids=q.scope_api_ids,
            scope_operator=None if key_pins_api else q.scope_operator, scope_since=q.since,
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
        """Evidence ids for the two map-keyed axes (``failed_rule`` / ``http_status``), whose key
        lives inside a JSON column and so has no indexed predicate to query per group. ONE
        newest-first scan of at most ``_RUN_ID_SCAN_CAP`` runs buckets every returned group at once.

        **The bound is honest, not hidden:** a group whose runs are ALL older than the newest
        ``_RUN_ID_SCAN_CAP`` in the window gets ``run_ids == []`` (and an empty ``api_sample``)
        while still reporting its true ``count``/``fail``/``error`` from the rollup -- the counters
        are exact, the evidence sample is best-effort. A rarer key that appears only near the end of
        the scan gets fewer than ``MAX_RUN_IDS`` ids for the same reason. Consumers must tolerate an
        empty list: the ``run_groups`` card renders the row and simply omits its "attach" button,
        and ``InsightCandidate.ref_ids`` is an optional evidence field."""
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
        updated_at) for spec §3's regression rule, the cell axis reads ``current_state``.

        The regression rule carries the QUERY's window: an API whose first failure predates
        ``q.since`` broke before the window opened and is not this window's regression. That is the
        same predicate ``summarize_insights`` counts with and the same one the insight panel
        selects suspects with -- one definition, three call sites."""
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
                    and (q.since is None or mark.first_non_pass_at >= q.since)
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

    def summarize_insights(
        self, since: datetime, until: datetime | None = None, *, flaky_min: int,
        scope_operator: str | None = None, briefing_tz: str = "Asia/Seoul",
    ) -> Insights:
        """The two Home/briefing tiles as two SQL COUNTs -- **not** a count over a ranked group
        list (Task 8 fix round 1).

        The old path counted ``g.flaky`` / ``g.regression_suspect`` over
        ``aggregate_runs(limit=500)``, whose groups are ranked by ``(fail+error, count)``. A cell
        that flips pass/fail/pass with ONE failure has the lowest possible rank, so at any real
        size it never entered the top 500 and the flaky tile read 0 while the project was full of
        flaky cells. Neither count below has a limit at all.

        * flaky -- cells (api × env × test_data) whose ``transitions`` summed over the window's
          WHOLE local days reach ``flaky_min``. Whole days only, exactly matching
          ``aggregate_rollups``: ``_fold_edge_runs`` contributes no transitions either, so the tile
          and a group's own ``flaky`` flag can never disagree. A window containing no whole day
          (the 09:00→09:00 briefing window) therefore reports 0 flaky, as the groups do.
        * regression -- spec §3's rule on ``api_watermark`` joined to the catalogue, with the
          window predicate ``first_non_pass_at >= since`` that ``_fill_derived`` now also applies.
          ``until`` does not bound it: the rule is anchored at the window's opening, so "broke
          inside this window and is still broken" stays one predicate in all three call sites.
        """
        interior, _edges = _window_partitions(since, until, briefing_tz)
        flaky = 0
        if interior is not None:
            clauses, params = _operator_scope_clause(scope_operator, since)
            day_from, day_to = interior
            if day_from is not None:
                clauses.append("day >= ?")
                params.append(day_from)
            if day_to is not None:
                clauses.append("day <= ?")
                params.append(day_to)
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            flaky = self._conn.execute(
                "SELECT COUNT(*) FROM (SELECT api_id, target_env, test_data_label FROM rollup_day "
                f"{where_sql} GROUP BY api_id, target_env, test_data_label "
                "HAVING SUM(transitions) >= ?)",
                [*params, flaky_min],
            ).fetchone()[0]

        clauses, params = _operator_scope_clause(scope_operator, since, column="w.api_id")
        clauses.append("w.last_pass_at IS NOT NULL AND w.first_non_pass_at IS NOT NULL")
        clauses.append("w.last_pass_at < a.updated_at AND a.updated_at <= w.first_non_pass_at")
        clauses.append("w.first_non_pass_at >= ?")
        params.append(_iso(since))
        regression = self._conn.execute(
            "SELECT COUNT(*) FROM api_watermark w JOIN apis a ON a.api_id = w.api_id "
            f"WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()[0]
        return Insights(flaky=flaky, regression_suspect=regression)

    # -- current_state / watermarks / operator_scope: materialized reads (Task 5) ----------------

    def current_state(
        self, scope_api_ids: Sequence[str] | None = None, *,
        scope_operator: str | None = None, scope_since: datetime | None = None,
    ) -> list[CellState]:
        """One indexed read of the materialized ``current_state`` table (PK
        ``(api_id, target_env, test_data_label)``) -- no scan over ``runs``, no python pass.

        ``scope_operator`` is the server-side operator scope (a subquery on ``operator_api``),
        with ``scope_since`` as its window lower bound -- the caller (``MockAtworks``) derives it
        from ``scope_window_days`` because this read has no window of its own."""
        clauses, params = _operator_scope_clause(scope_operator, scope_since)
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
        last_non_pass_since: datetime | None = None, *,
        scope_operator: str | None = None, scope_since: datetime | None = None,
    ) -> list[ApiWatermark]:
        """One indexed read of ``api_watermark`` (PK ``api_id``). Both ``*_since`` filters are
        plain SQL predicates: a NULL column fails them, which is exactly the recompute oracle's
        "no non-pass run at all -> not a candidate".

        ``first_non_pass_since`` = the API's FIRST-EVER failure is at/after the cutoff (a newly
        broken API). ``last_non_pass_since`` = the API has SOME failure at/after the cutoff --
        the exact set a ``failed_since`` selection means, and the reason both exist.

        ``scope_operator``/``scope_since`` add the server-side operator scope as a subquery on
        ``operator_api`` -- never an id list on ``api_ids``, which has a cap and therefore a hole."""
        clauses, params = _operator_scope_clause(scope_operator, scope_since)
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
