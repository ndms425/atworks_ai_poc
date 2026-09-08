"""SQLite-backed storage for MockAtworks -- the schema blueprint the Java aTworks side will
implement. Task 4 (spec 2026-09-06) moves runs/bodies/audit off python dicts and onto indexed SQL;
`apis` mirrors into a table too so `search_apis` pages over SQL, but the authoritative in-memory
index for ledger guardrails stays `MockAtworks.apis` (a plain dict) per the controller ruling.

``current_state`` / ``rollup_day`` / ``api_watermark`` / ``operator_api`` are materialized at
**ingest** (Task 5, spec §4): ``ingest()`` is the single write path for runs -- one transaction
inserts the new runs and their (optionally masked) bodies and folds the same batch into all four
tables (plus ``rollup_key_day`` / ``rollup_operator_day``) via the pure
``atworks_agent.materialize.rollup_delta``. ``current_state()`` /
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
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
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
    CellKeyLevel,
    CellState,
    Insights,
    KeyCounts,
    Page,
    QueryResult,
    QueryRow,
    QuerySpec,
    RunGroup,
    RunResult,
    RunsQuery,
    RunStatus,
    decode_cursor,
    encode_cursor,
    key_rollup_delta,
    merge_watermark,
    rollup_delta,
)
from atworks_agent.aggregation import MAX_RUN_IDS

from .query_sql import (
    API_SAMPLE_CAP,
    RUN_SAMPLE_CAP,
    CompiledQuery,
    compile_query,
    day_bounds,
    path_segments,
    row_key_predicates,
    sample_predicates,
)
from .query_sql import KEY_AXES as QUERY_KEY_AXES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS apis (
    api_id TEXT PRIMARY KEY,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    "group" TEXT,
    updated_at TEXT NOT NULL,
    has_rules INTEGER NOT NULL DEFAULT 0,
    params JSON NOT NULL DEFAULT '[]',
    -- The path, pre-split at ingest (self-growth spec 2026-09-07 §11). `query_runs` groups by
    -- "endpoint family" -- the first or second `/`-separated segment, or the two of them joined --
    -- and a GROUP BY over a SUBSTR/INSTR expression of `path` can use no index at all: every
    -- grouped read would be a full scan of the catalogue (50,000 rows) plus a temp b-tree. Split
    -- once, on the write that already touches the row, and the same read is an index scan.
    -- Segments are kept VERBATIM: `{id}` and a numeric segment are values an operator typed into
    -- the spec, and relabelling them here would invent a grouping nobody asked for.
    path_segment_1 TEXT,
    path_segment_2 TEXT,
    path_prefix_2 TEXT
);
CREATE INDEX IF NOT EXISTS idx_apis_updated_at ON apis(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_apis_group ON apis("group");
CREATE INDEX IF NOT EXISTS idx_apis_path ON apis(path);
CREATE INDEX IF NOT EXISTS idx_apis_seg1 ON apis(path_segment_1);
CREATE INDEX IF NOT EXISTS idx_apis_seg2 ON apis(path_segment_2);
CREATE INDEX IF NOT EXISTS idx_apis_prefix2 ON apis(path_prefix_2);
CREATE INDEX IF NOT EXISTS idx_apis_method ON apis(method);

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
    -- The JSON paths capture-time masking actually rewrote in THIS body ($.a.b[2] form, the
    -- shape parity speaks). Masking is one-way, so two bodies differing only inside a masked
    -- leaf both read "***" and compare equal -- the parity block reported exactly that as
    -- `equal` with `basis: "body"`. Recorded here, at capture, because it is the only moment
    -- anything knows: by the time the report compares, the original value is gone for good.
    masked_paths JSON,
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

-- The two JSON maps above, transposed onto their own key axis (final review, pre-ruled). Reading
-- a key-axis group off `rollup_day` means `json_each`-ing EVERY cell row in the window: 170k rows
-- became a quarter-million tuples on the 2M-run set, 0.53s of the 0.62s `aggregate_runs
-- group_by=http_status` took. Here the same window is (days x keys) rows -- three orders of
-- magnitude fewer -- and the fold is exactly the same arithmetic, computed once at ingest.
-- `api_ids` is the set of APIs that contributed to that (day, key): a per-day COUNT cannot be
-- summed across the window without double-counting, and `RunGroup.api_count` promises the TRUE
-- distinct total, so the union is taken over the ids themselves for the returned keys only.
-- `p95_duration_ms` is carried for the same reason `api_ids` is: without it the fast arm answered
-- NULL while the `json_each` arm answered `MAX(rollup_day.p95_duration_ms)` for the same logical
-- query. It is a LEVEL, not a counter -- the max over the cell rows of that day carrying the key,
-- the same documented max-merge approximation `rollup_day.p95_duration_ms` itself uses (spec §4);
-- see `materialize.CellKeyLevel` for why the merge reads the POST-MERGE cell row.
-- The CHECK pins the axis vocabulary to `materialize.KEY_AXES`.
CREATE TABLE IF NOT EXISTS rollup_key_day (
    day TEXT NOT NULL,
    axis TEXT NOT NULL CHECK (axis IN ('failed_rule', 'http_status')),
    key TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    fail INTEGER NOT NULL DEFAULT 0,
    error INTEGER NOT NULL DEFAULT 0,
    p95_duration_ms INTEGER,
    api_ids JSON NOT NULL DEFAULT '[]',
    PRIMARY KEY (day, axis, key)
);
-- Every read here is "one axis, a day range": axis leads so the range scan starts inside the
-- right half of the table instead of straddling both.
CREATE INDEX IF NOT EXISTS idx_rollup_key_day_axis_day ON rollup_key_day(axis, day);

-- (day, operator) counters, materialized at ingest (self-growth spec 2026-09-07 §4/§11). The
-- "executed_by" axis has no other home: `rollup_day` does not know who ran a cell (it is keyed by
-- api/env/data), and `operator_api.run_count` has no day in it, so "내가 어제 몇 건 돌렸나" was a
-- `runs` scan. One row per (day, operator) makes it a range scan of at most (days x operators).
-- Runs with `executed_by IS NULL` contribute NOTHING (same rule as `operator_api`), so the sum of
-- this table over a window can be SMALLER than the same window's `rollup_day` sum -- that is the
-- honest answer, not a discrepancy: an unattributed run belongs to no operator.
CREATE TABLE IF NOT EXISTS rollup_operator_day (
    day TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    pass INTEGER NOT NULL DEFAULT 0,
    fail INTEGER NOT NULL DEFAULT 0,
    error INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, operator_id)
);
-- The PK is (day, operator); the read shape "this operator, this window" wants the other order.
CREATE INDEX IF NOT EXISTS idx_rollup_operator_day_op ON rollup_operator_day(operator_id, day);

CREATE TABLE IF NOT EXISTS api_watermark (
    api_id TEXT PRIMARY KEY,
    last_pass_at TEXT,
    first_non_pass_at TEXT,
    last_non_pass_at TEXT,
    latest_status TEXT,
    api_updated_at TEXT
);
-- The two watermark columns that are READ AS PREDICATES, not by primary key (final review I7):
-- `first_non_pass_since` (the insight panel's regression candidates, every Home build) and
-- `last_non_pass_since` (`select_where.failed_since`, on the stage AND the LATE re-resolution
-- path). Without these the filter was a full scan of one row per API -- 50,000 of them on the
-- full set, on a request path, to return a handful.
CREATE INDEX IF NOT EXISTS idx_api_watermark_first_non_pass ON api_watermark(first_non_pass_at);
CREATE INDEX IF NOT EXISTS idx_api_watermark_last_non_pass ON api_watermark(last_non_pass_at);

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


#: The capture-time masking hook (``masking.mask_body_paths`` bound to a policy): a response body
#: in, ``(masked body, the JSON paths it rewrote)`` out. Both halves are stored.
MaskFn = Callable[[dict], tuple[Any, list[str]]]


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


#: ``query_sql.KEY_AXES``, re-exported under a local name so this module reads one vocabulary.
_QUERY_KEY_AXES = QUERY_KEY_AXES

#: Measures that are a LEVEL rather than a counter. A group the previous window did not return
#: had no runs, which is an honest 0 for a counter -- but "the p95 was 0 ms" and "there were 0
#: pass/fail flips we can point at" are claims about a measurement that was never taken, so these
#: two read ``None`` on a missing key (see ``Store._join_previous_window``).
_LEVEL_MEASURES = ("p95_duration_ms", "transitions")


def _measure_out(name: str, value) -> float | int | None:
    """One measure cell, out of SQLite and into ``QueryRow.measures``. ``fail_rate`` is the only
    float; every other measure is a count, a level or NULL (a source that does not carry it --
    ``transitions`` off anything but ``rollup_day``, ``p95``/``apis`` off
    ``rollup_operator_day``). NULL stays NULL: a source that cannot measure something must not
    report a 0, which reads as a measurement."""
    if value is None:
        return None
    if name == "fail_rate":
        return float(value)
    return int(value)


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


def _status_filter(status: str | None) -> str | None:
    """``"all"`` is a MEMBER of ``RunStatusFilter`` and it means "no status filter" -- it is the
    portal's own default segment, and the model's tool schema offers it. Every reader used to take
    it as a status VALUE, so ``status=all`` matched no row: ``/runs?status=all`` answered an empty
    page (with ``total: 0``, so nothing looked wrong) over a project full of runs, and an
    aggregate under it reported zero transitions. Normalized in one place, next to the two
    readers, so a route can never disagree with a predicate builder (final review I4)."""
    return None if status in (None, "all") else status


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
    status = _status_filter(q.status)
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
    status = _status_filter(status)
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
    transitions = 0 if _status_filter(q.status) else row[_TRANSITIONS]
    api = apis.get(key) if q.group_by == "api" else None
    return RunGroup(
        key=key, label=f"{api.method} {api.path}" if api is not None else key,
        count=count, fail=fail, error=error, passed=passed,
        transitions=transitions, flaky=transitions >= flaky_min,
        p95_duration_ms=row[_P95],
        api_count=row[_API_COUNT] if q.group_by in _MAP_COLUMN else None,
    )


def _merge_key_counts(stored: str | None, delta: Mapping[str, KeyCounts]) -> dict[str, dict[str, int]]:
    """Fold a batch's ``{key: KeyCounts}`` map into the JSON already on the rollup row and return
    the merged map. Values are ``{"count","fail","error"}`` objects, not bare ints --
    see ``materialize.KeyCounts``. The caller dumps it back to JSON *and* reads its key set: the
    keys of the POST-MERGE map are what ``rollup_key_day``'s p95 level flows to (``CellKeyLevel``)."""
    merged: dict[str, dict[str, int]] = json.loads(stored) if stored else {}
    for key, counts in delta.items():
        row = merged.get(key) or {"count": 0, "fail": 0, "error": 0}
        merged[key] = {
            "count": row.get("count", 0) + counts.count,
            "fail": row.get("fail", 0) + counts.fail,
            "error": row.get("error", 0) + counts.error,
        }
    return merged


class Store:
    """Owns the sqlite3 connection and every SQL query MockAtworks needs. Runs/bodies/audit are
    the only state that actually lives here in Task 4 -- job/rule/profile/format ledgers stay
    python objects on MockAtworks (controller ruling); ``apis`` is mirrored here for
    ``search_apis`` but ``MockAtworks.apis`` (a dict) remains the source of truth read elsewhere."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._txn_depth = 0             # `_transaction` is re-entrant; only depth 0 commits
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL is a no-op on ":memory:" (sqlite silently keeps "memory" journal mode) -- harmless.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    def conn(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """EVERY writer that issues more than one statement runs inside this: commit on success,
        ``ROLLBACK`` on **any** exception (``BaseException`` -- a ``KeyboardInterrupt`` or a
        cancelled task must not leave half a batch pending either), then re-raise.

        Without it a failure between two statements left the first one PENDING on the connection
        rather than undone: sqlite3 opens the implicit transaction at the first DML and only ends
        it at an explicit ``commit()``/``rollback()``, so the next unrelated writer's ``commit()``
        silently persisted the abandoned rows -- runs in ``runs`` with nothing folded into
        ``rollup_day``/``current_state``/``api_watermark``, which no later read can detect and no
        re-ingest can repair (``ingest`` skips run_ids it already sees). Rolling back here is what
        makes "one ingest = one all-or-nothing transaction" (spec §4) true rather than intended.

        RE-ENTRANT: nesting only the depth counter, so a writer composed of other writers is ONE
        transaction. ``replace_all_runs`` is the case -- its three DELETEs used to commit on their
        own before ``ingest`` opened a second transaction, so a failure inside that ingest rolled
        back only the reload and left the store EMPTY. Any exception rolls the whole outermost
        transaction back and resets the depth; the outer ``with`` then rolls back a no-op."""
        self._txn_depth += 1
        try:
            yield
        except BaseException:
            self._txn_depth = 0
            self._conn.rollback()
            raise
        self._txn_depth -= 1
        if self._txn_depth == 0:
            self._conn.commit()

    def init_schema(self) -> None:
        """CREATE TABLE/INDEX IF NOT EXISTS throughout -- safe to call on an already-initialized
        connection (constructor calls it once; tests call it again to assert idempotency)."""
        with self._transaction():
            added = self._widen_columns()
            self._conn.executescript(_SCHEMA)
            self._migrate_columns(added)

    def _widen_columns(self) -> set[tuple[str, str]]:
        """Columns added to a table that already exists on disk. ``CREATE TABLE IF NOT EXISTS``
        never widens an existing table, so a store written before Task 8 keeps its old
        ``rollup_day`` shape until this ALTER runs; the new column stays NULL (read as an empty
        map) until ``rebuild_materialized`` refills it.

        Runs BEFORE the schema script, not after (self-growth): ``_SCHEMA`` now creates indexes ON
        the new ``apis`` columns, and an index over a column an old file does not have yet is an
        ``OperationalError`` at open time -- the store would not boot at all."""
        added: set[tuple[str, str]] = set()
        for table, column, decl in (("rollup_day", "http_status_counts", "JSON"),
                                    ("bodies", "masked_paths", "JSON"),
                                    ("rollup_key_day", "p95_duration_ms", "INTEGER"),
                                    ("apis", "path_segment_1", "TEXT"),
                                    ("apis", "path_segment_2", "TEXT"),
                                    ("apis", "path_prefix_2", "TEXT")):
            existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if existing and column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                added.add((table, column))
        return added

    def _migrate_columns(self, added: set[tuple[str, str]]) -> None:
        """Everything a widened (or brand-new) table needs AFTER the schema script has run: the
        reshape of a table whose columns changed meaning, and the backfills that turn a NULL
        column or an empty new table into the values every read assumes are there."""
        if ("apis", "path_segment_1") in added:
            self._backfill_path_segments()
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
        self._backfill_key_rollup(rebuild=("rollup_key_day", "p95_duration_ms") in added)
        self._backfill_operator_rollup()

    def _backfill_path_segments(self) -> None:
        """Fill the three new ``apis`` columns on a store written before they existed. The split
        is python (``path_segments``), not SQL: the SQL for "the second `/`-separated segment, or
        NULL when there isn't one" is a nest of SUBSTR/INSTR that would then have to agree
        CHARACTER FOR CHARACTER with the python the write path uses -- two definitions of one
        grouping. A catalogue is at most tens of thousands of rows and this runs once per file."""
        rows = self._conn.execute("SELECT api_id, path FROM apis").fetchall()
        self._conn.executemany(
            "UPDATE apis SET path_segment_1 = ?, path_segment_2 = ?, path_prefix_2 = ? WHERE api_id = ?",
            [(*path_segments(r["path"]), r["api_id"]) for r in rows],
        )

    def _backfill_operator_rollup(self) -> None:
        """``rollup_operator_day`` is a NEW TABLE (self-growth spec §11), and
        ``CREATE TABLE IF NOT EXISTS`` leaves it EMPTY on a store written before it existed -- so
        the ``executed_by`` axis would answer "no groups" over a store full of runs. Derived here,
        once, from the run rows themselves (both partitions: the archive is moved, never deleted,
        and its days are as real as the hot ones), which is the same fold ``rollup_delta`` does at
        ingest expressed in SQL. Runs with no ``executed_by`` are skipped, exactly as the delta
        skips them."""
        if self._conn.execute("SELECT EXISTS(SELECT 1 FROM rollup_operator_day)").fetchone()[0]:
            return
        for table in ("runs", "runs_archive"):
            self._conn.execute(
                'INSERT INTO rollup_operator_day (day, operator_id, "count", "pass", fail, error) '
                'SELECT day, executed_by, COUNT(*), SUM(status = \'pass\'), SUM(status = \'fail\'), '
                f"SUM(status = 'error') FROM {table} "
                "WHERE executed_by IS NOT NULL AND day IS NOT NULL GROUP BY day, executed_by "
                'ON CONFLICT(day, operator_id) DO UPDATE SET '
                '"count" = "count" + excluded."count", "pass" = "pass" + excluded."pass", '
                "fail = fail + excluded.fail, error = error + excluded.error"
            )

    def _backfill_key_rollup(self, *, rebuild: bool = False) -> None:
        """``rollup_key_day`` is a NEW TABLE, and `CREATE TABLE IF NOT EXISTS` leaves it empty on a
        store written before it existed -- which would make the two key axes answer "no groups"
        rather than merely answer slowly. So a store that has cell rollups but no key rollups
        derives them here, once, from the JSON maps those cell rows already carry: the same fold
        `key_rollup_delta` does at ingest, expressed in SQL, needing no run rows at all.

        ``rebuild`` is for the OTHER shape of the same problem: a file whose ``rollup_key_day``
        already has rows but predates the ``p95_duration_ms`` column. ``ALTER TABLE ADD COLUMN``
        leaves it NULL on every existing row and no ingest ever repairs it (the fold only touches
        the days a new batch lands on), so the migration re-derives the whole table -- it is a
        pure function of ``rollup_day``, so throwing it away and rebuilding loses nothing."""
        if not rebuild and self._conn.execute("SELECT EXISTS(SELECT 1 FROM rollup_key_day)").fetchone()[0]:
            return
        if not self._conn.execute("SELECT EXISTS(SELECT 1 FROM rollup_day)").fetchone()[0]:
            return
        if rebuild:
            self._conn.execute("DELETE FROM rollup_key_day")
        for axis, column in _MAP_COLUMN.items():
            self._conn.execute(
                'INSERT OR REPLACE INTO rollup_key_day '
                '(day, axis, "key", "count", fail, error, p95_duration_ms, api_ids) '
                f"SELECT day, '{axis}', je.key, "
                "SUM(json_extract(je.value, '$.count')), SUM(json_extract(je.value, '$.fail')), "
                "SUM(json_extract(je.value, '$.error')), MAX(p95_duration_ms), "
                "json_group_array(DISTINCT api_id) "
                f"FROM rollup_day, json_each(rollup_day.{column}) je GROUP BY day, je.key"
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
        exactly that new set, not the old rows plus the new ones. The DELETE and the reload share
        ONE transaction: committing the DELETE on its own meant a failure in the reload left the
        catalogue empty."""
        rows = self._api_rows(apis)
        with self._transaction():
            self._conn.execute("DELETE FROM apis")
            self._write_api_rows(rows)

    @staticmethod
    def _api_rows(apis: Iterable[ApiSpec]) -> list[tuple]:
        """One tuple per spec, with the path pre-split (``query_sql.path_segments``) -- see the
        ``apis`` DDL for why the split happens on the write rather than inside a GROUP BY."""
        return [
            (a.api_id, a.method, a.path, a.name, a.group, _iso(a.updated_at), int(a.has_rules),
             json.dumps(a.params), *path_segments(a.path))
            for a in apis
        ]

    def _write_api_rows(self, rows: Sequence[tuple]) -> None:
        """Rows in, no commit -- the caller owns the transaction."""
        self._conn.executemany(
            'INSERT OR REPLACE INTO apis (api_id, method, path, name, "group", updated_at, has_rules, params, '
            "path_segment_1, path_segment_2, path_prefix_2) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self._refresh_api_updated_at([a[0] for a in rows])

    def load_apis(self, apis: Iterable[ApiSpec]) -> None:
        with self._transaction():
            self._write_api_rows(self._api_rows(apis))

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
        self, runs: Sequence[RunResult], briefing_tz: str, mask: MaskFn | None,
        *, replace: bool = False,
    ) -> None:
        """Raw row writer -- runs + bodies only, no materialization, no commit. Only ``ingest``
        and ``upsert_run`` call this. ``mask`` is the Task 6 hook, now returning the masked body
        AND the paths it rewrote (``masking.mask_body_paths``): both go into ``bodies`` in the
        same INSERT, because capture is the only moment anything still knows which leaves were
        replaced -- afterwards the original value is gone and the ``***`` is indistinguishable
        from a real one."""
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        self._conn.executemany(
            f"{verb} INTO runs (run_id, api_id, job_id, target_env, test_data_label, status, "
            "http_status, duration_ms, executed_at, executed_by, failed_rules, day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [self._run_row(r, briefing_tz) for r in runs],
        )
        body_rows: list[tuple] = []
        for run in runs:
            if run.response_body is None:
                continue
            body, masked_paths = mask(run.response_body) if mask is not None else (run.response_body, [])
            body_rows.append((run.run_id, json.dumps(body), json.dumps(masked_paths),
                              _iso(run.executed_at)))
        if body_rows:
            self._conn.executemany(
                f"{verb} INTO bodies (run_id, body, masked_paths, captured_at) VALUES (?,?,?,?)",
                body_rows,
            )

    def ingest(
        self, runs: Iterable[RunResult], briefing_tz: str = "Asia/Seoul", *,
        mask: MaskFn | None = None,
    ) -> int:
        """**The** write path for runs (spec §4): one transaction inserts the batch and folds it
        into ``current_state`` / ``rollup_day`` / ``rollup_key_day`` / ``api_watermark`` /
        ``operator_api`` / ``rollup_operator_day``. Returns the
        number of runs that were actually new.

        Idempotent by construction: run_ids already present are filtered out up front (and the
        insert itself is ``INSERT OR IGNORE``), so re-ingesting a batch inserts nothing and adds
        no delta anywhere -- a retried ``record_execution`` cannot double-count a rollup.

        ALL-OR-NOTHING: the run rows, their bodies and every materialized fold share one
        ``_transaction``. A failure anywhere inside rolls the whole batch back, so the store never
        holds a run that no rollup counted."""
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
        with self._transaction():
            self._write_run_rows(fresh, briefing_tz, mask)
            self._materialize(fresh)
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
        with self._transaction():
            self._write_run_rows([run], briefing_tz, None, replace=True)
            if run.response_body is None:
                self._conn.execute("DELETE FROM bodies WHERE run_id = ?", (run.run_id,))
            self.rebuild_materialized()

    def replace_all_runs(self, runs: Mapping[str, RunResult] | Iterable[RunResult], briefing_tz: str = "Asia/Seoul") -> None:
        """Wholesale replacement -- backs ``backend.runs = {...}`` in tests that build a
        purpose-built run set from scratch, discarding the fixtures entirely. The materialized
        tables are cleared with the runs, then rebuilt by the ``ingest`` below -- all inside ONE
        transaction (``_transaction`` is re-entrant, so the ``ingest`` nests): committing the
        DELETEs first meant a failing reload left the store empty rather than untouched."""
        values = list(runs.values()) if isinstance(runs, Mapping) else list(runs)
        with self._transaction():
            self._conn.execute("DELETE FROM runs")
            self._conn.execute("DELETE FROM bodies")
            self._clear_materialized()
            self.ingest(values, briefing_tz)

    # -- ingest-time materialization (spec §4) ---------------------------------------------------

    def _clear_materialized(self) -> None:
        for table in ("current_state", "rollup_day", "rollup_key_day", "api_watermark",
                      "operator_api", "rollup_operator_day"):
            self._conn.execute(f"DELETE FROM {table}")

    def rebuild_materialized(self, briefing_tz: str = "Asia/Seoul") -> None:
        """Drop every materialized table and fold every stored run back in, in one pass. Used
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
        """Fold a batch of *new* runs into the materialized tables. Pure arithmetic lives in
        ``atworks_agent.materialize.rollup_delta``; this method only reads the rows the batch
        touches, merges, and writes them back inside the caller's transaction."""
        prev_state = self._current_state_for(sorted({r.api_id for r in fresh}))
        rollups, cells, marks, operators, operator_days = rollup_delta(fresh, prev_state)

        self._conn.executemany(
            "INSERT OR REPLACE INTO current_state "
            "(api_id, target_env, test_data_label, run_id, status, executed_at, transitions_total) "
            "VALUES (?,?,?,?,?,?,?)",
            [(c.api_id, c.target_env, _label_in(c.test_data_label), c.run_id, c.status.value,
              _iso(c.executed_at), c.transitions_total) for c in cells],
        )

        # The POST-MERGE p95 + key set of every cell this batch touched, handed to
        # `key_rollup_delta` so the key-axis p95 is exactly `MAX(rollup_day.p95_duration_ms)` over
        # the cells carrying that key -- what the `json_each` arm computes. See `CellKeyLevel`.
        levels: list[CellKeyLevel] = []
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
            levels.append(CellKeyLevel(
                day=row.day, p95_duration_ms=p95,
                keys={"failed_rule": tuple(rules), "http_status": tuple(https)}))
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
                 p95, json.dumps(rules, ensure_ascii=False), json.dumps(https, ensure_ascii=False)),
            )

        # The same batch, transposed onto the key axis (see rollup_key_day's DDL comment). Counts
        # add; api_ids UNION -- a per-day distinct count cannot be summed across a window; p95 is
        # a level and merges with MAX, the same approximation the cell row uses.
        for key_row in key_rollup_delta(rollups, levels):
            existing = self._conn.execute(
                'SELECT "count", fail, error, p95_duration_ms, api_ids FROM rollup_key_day '
                'WHERE day = ? AND axis = ? AND "key" = ?',
                (key_row.day, key_row.axis, key_row.key),
            ).fetchone()
            api_ids = set(json.loads(existing["api_ids"]) if existing and existing["api_ids"] else [])
            api_ids.update(key_row.api_ids)
            key_p95 = key_row.p95_duration_ms
            if existing is not None and existing["p95_duration_ms"] is not None:
                key_p95 = (existing["p95_duration_ms"] if key_p95 is None
                           else max(key_p95, existing["p95_duration_ms"]))
            self._conn.execute(
                'INSERT OR REPLACE INTO rollup_key_day '
                '(day, axis, "key", "count", fail, error, p95_duration_ms, api_ids) '
                "VALUES (?,?,?,?,?,?,?,?)",
                (key_row.day, key_row.axis, key_row.key,
                 (existing["count"] if existing else 0) + key_row.count,
                 (existing["fail"] if existing else 0) + key_row.fail,
                 (existing["error"] if existing else 0) + key_row.error,
                 key_p95, json.dumps(sorted(api_ids), ensure_ascii=False)),
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

        # (day, operator) counters -- pure addition, so ON CONFLICT does the merge in one
        # statement (no read-modify-write): nothing here is a level or a set.
        self._conn.executemany(
            'INSERT INTO rollup_operator_day (day, operator_id, "count", "pass", fail, error) '
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(day, operator_id) DO UPDATE SET "
            '"count" = "count" + excluded."count", "pass" = "pass" + excluded."pass", '
            "fail = fail + excluded.fail, error = error + excluded.error",
            [(d.day, d.operator_id, d.count, d.passed, d.fail, d.error) for d in operator_days],
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
    # mirror (Task 10). It is a PK lookup applied to the rows the keyset LIMIT already chose, and
    # `apis` contributes no `executed_at`/`run_id` of its own to the ORDER BY, so `list_runs`
    # keeps riding idx_runs_executed_at. The aggregation-only SELECT (`fetch_runs`) stays
    # label-free -- it never renders a row.
    # The label join goes through a projecting subquery rather than `LEFT JOIN apis`: `apis` has
    # an `api_id` column of its own, and the run predicates below are written unqualified (they
    # also run against `runs_archive`, which has no `runs` alias), so a bare join would make
    # `api_id = ?` ambiguous. Renaming the joined columns keeps every predicate untouched; SQLite
    # flattens the subquery, so the join is still a PK lookup on apis.
    #
    # BODIES ARE NOT HERE (final review I3). These SELECTs used to `LEFT JOIN bodies` and carry
    # `bodies.body` into every RunResult, so a page of 200 runs pulled 200 response bodies into
    # process memory on the model-facing path and the entire safety line was one `exclude=
    # {"response_body"}` in `serialization.run_record`. All the model ever needs is WHETHER a body
    # exists, which is one EXISTS probe on the bodies PK per returned row -- and the content is
    # reachable only through `get_body`, the single method that reads the column at all.
    _RUN_SELECT = ("runs.*, EXISTS(SELECT 1 FROM bodies WHERE bodies.run_id = runs.run_id) AS has_body, "
                   "label.api_method AS api_method, label.api_path AS api_path")
    _RUN_JOINS = ("LEFT JOIN (SELECT api_id AS label_id, method AS api_method, path AS api_path FROM apis) "
                  "AS label ON label.label_id = runs.api_id")

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunResult:
        # sqlite3.Row's `in` operator tests *values*, not column names (unlike a dict) -- so this
        # must stay row.keys(), not `"has_body" in row`.
        columns = row.keys()
        # api_method/api_path are the row's DISPLAY LABEL, joined from the apis mirror by the
        # SELECTs that feed `run_record` (Task 10: RunsView renders "GET /orders" instead of
        # downloading 500 API specs to look the label up client-side). The aggregation-only
        # SELECTs don't join, so both stay None there -- exclude_none drops them from the record.
        # `response_body` is ALWAYS None off a read: no read path joins `bodies` any more, and
        # `has_body` carries the one fact about it that a record needs.
        return RunResult(
            run_id=row["run_id"], api_id=row["api_id"], job_id=row["job_id"],
            target_env=row["target_env"], test_data_label=row["test_data_label"],
            status=RunStatus(row["status"]), http_status=row["http_status"], duration_ms=row["duration_ms"],
            executed_at=_parse_iso(row["executed_at"]), executed_by=row["executed_by"],
            failed_rules=json.loads(row["failed_rules"]) if row["failed_rules"] else [],
            response_body=None,
            has_body=bool(row["has_body"]) if "has_body" in columns else None,
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
        status = _status_filter(status)
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
        # `runs` keeps every predicate, the keyset clause and the label join byte-identical, so
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

    def count_runs_sql(
        self, since: datetime | None = None, until: datetime | None = None,
        status: str | None = None, api_id: str | None = None, archived: bool = False,
    ) -> tuple[str, list]:
        """``count_runs``' statement, verbatim -- built here for the same reason as
        ``list_runs_sql`` (M19): a query-plan test that hand-copies the SQL proves nothing about
        the query that actually runs, and rots silently the first time a predicate changes."""
        clauses, params = self._runs_predicates(since=since, until=until, status=status, api_id=api_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        source = "runs_archive" if archived else "runs"
        return f"SELECT COUNT(*) FROM {source} {where_sql}", params

    def count_runs(
        self, since: datetime | None = None, until: datetime | None = None,
        status: str | None = None, api_id: str | None = None, archived: bool = False,
    ) -> int:
        sql, params = self.count_runs_sql(since, until, status, api_id, archived)
        return self._conn.execute(sql, params).fetchone()[0]

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
        """Ids in, records out, in the caller's order, missing ids skipped. The IN-list is
        CHUNKED at 500 (M12): SQLite's default bound-variable cap is 999, and the callers here
        pass whatever list they hold -- `job.recent_run_ids` is 50 today, but a union of group
        run_ids is not bounded by anything as small, and blowing that cap is an
        `OperationalError`, not a slow query. Same chunk size as `_existing_run_ids`."""
        if not run_ids:
            return []
        by_id: dict[str, RunResult] = {}
        for start in range(0, len(run_ids), 500):
            chunk = list(run_ids[start:start + 500])
            placeholders = ",".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"SELECT {self._RUN_SELECT} FROM runs {self._RUN_JOINS} "
                f"WHERE runs.run_id IN ({placeholders})",
                chunk,
            ).fetchall()
            by_id.update({r["run_id"]: self._row_to_run(r) for r in rows})
        return [by_id[i] for i in run_ids if i in by_id]

    def apis_by_ids(self, api_ids: Sequence[str]) -> list[ApiSpec]:
        """The batch read behind ABC ``get_apis`` (M16). ``resolve_select_where``'s
        ``failed_since`` branch used to await ``get_api`` once per watermark it kept -- up to
        ``max_apis_per_job + 1`` round trips to fetch specs it already knew the ids of. Chunked at
        500 for the same reason as ``runs_by_ids``."""
        if not api_ids:
            return []
        found: dict[str, ApiSpec] = {}
        for start in range(0, len(api_ids), 500):
            chunk = list(api_ids[start:start + 500])
            placeholders = ",".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"SELECT * FROM apis WHERE api_id IN ({placeholders})", chunk).fetchall()
            found.update({r["api_id"]: self._row_to_api(r) for r in rows})
        return [found[i] for i in api_ids if i in found]

    def max_run_seq(self) -> int:
        """The highest N in a ``run-N`` id across BOTH partitions, or 0 when neither holds one.

        ``MockAtworks`` seeds its run counter from this (M17). It used to seed from
        ``run_count()``, which is the number of runs in the HOT partition -- so once retention
        moved a day into ``runs_archive`` the counter jumped BACKWARDS and the next execution
        re-issued run ids that already exist in the archive. `INSERT OR IGNORE` would then
        silently drop the new run as a duplicate. The archive must be counted precisely because
        it is the part that is invisible to every ordinary read."""
        best = 0
        for table in ("runs", "runs_archive"):
            row = self._conn.execute(
                f"SELECT MAX(CAST(SUBSTR(run_id, 5) AS INTEGER)) FROM {table} "
                "WHERE run_id LIKE 'run-%'"
            ).fetchone()
            if row is not None and row[0] is not None:
                best = max(best, int(row[0]))
        return best

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

    def get_body_masked_paths(self, run_id: str) -> list[str]:
        """The JSON paths capture-time masking rewrote in this run's stored body -- empty when
        nothing was masked, when no body was captured, or on a row written before this column
        existed (which is honest: nothing recorded it, so nothing is claimed)."""
        row = self._conn.execute(
            "SELECT masked_paths FROM bodies WHERE run_id = ?", (run_id,)).fetchone()
        if row is None or row["masked_paths"] is None:
            return []
        return json.loads(row["masked_paths"])

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

    def oldest_run_day(self) -> str | None:
        """The oldest ``day`` partition holding runs (local ``YYYY-MM-DD``), or None when the hot
        partition is empty. Public for the same reason as ``newest_run_at``: the bench times ONE
        day partition ageing out (spec §6 "일 단위 이관"), and it needs to know which day that is
        without reaching into the connection."""
        return self._conn.execute(
            "SELECT MIN(day) FROM runs WHERE day IS NOT NULL").fetchone()[0]

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
        # INSERT then DELETE inside one `_transaction`: a failure between them would otherwise
        # leave the copy pending and the originals still in `runs`, and the next writer's commit
        # would make that half-move permanent.
        with self._transaction():
            self._conn.execute(
                f"INSERT OR IGNORE INTO runs_archive ({self._RUN_COLUMNS}, archived_at) "
                f"SELECT {self._RUN_COLUMNS}, ? FROM runs WHERE executed_at < ?{scope}",
                (_iso(archived_at), _iso(cutoff), *params),
            )
            moved = self._conn.execute(
                f"DELETE FROM runs WHERE executed_at < ?{scope}", (_iso(cutoff), *params)
            ).rowcount
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
        by_key = self._uses_key_rollup(q)
        arms = [self._rollup_arm(q, interior, by_key)] if interior is not None else []
        arms += [self._edge_arm(q, lo, hi) for lo, hi in edges]
        groups = self._top_groups(q, arms, apis, flaky_min)
        if by_key and interior is not None:
            self._fill_key_api_count(groups, q, interior)
        if q.include_run_ids:
            self._fill_run_ids(groups, q)
        self._fill_derived(groups, q, apis)
        return groups

    @staticmethod
    def _uses_key_rollup(q: AggregateQuery) -> bool:
        """Can this query's whole-day arm come off ``rollup_key_day``?

        Only for the two map-keyed axes, and only when nothing scopes the query to a SET OF APIs.
        A key-rollup row is already summed ACROSS every API that touched that key, so an
        ``api_id`` predicate cannot be applied to it after the fact -- there is no api column left
        to filter, and the counters cannot be split back apart. A scoped map-axis read (the
        insight panel's ``failed_rule`` groups, which carry ``scope_operator``) therefore keeps
        the ``rollup_day`` + ``json_each`` arm, which is exact under a scope and merely slower.
        The unscoped reads -- the briefing's cause groups, the Home tiles, every `aggregate_runs`
        the model asks for -- are the ones that were breaching, and they take the fast path."""
        return (q.group_by in _MAP_COLUMN
                and q.scope_api_ids is None and q.scope_operator is None)

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

    def _rollup_arm(self, q: AggregateQuery, interior: tuple[str | None, str | None],
                    by_key: bool = False) -> tuple[str, list]:
        """The whole local days of the window, shaped to the union's column list
        (``gkey, c, p, f, e, t, q95[, n]``).

        ``by_key`` takes the map axes off ``rollup_key_day``, where one row already IS one
        (day, key) group: the window becomes (days x keys) rows instead of every cell row in it
        exploded by ``json_each``. ``n`` (api_count) comes back as 0 and is filled afterwards from
        the ``api_ids`` union -- see ``_fill_key_api_count``; the counters are exact either way.

        The api/env/cell axes emit one raw rollup ROW and let the outer GROUP BY do all the work:
        the cell axis has roughly one group per row anyway, so grouping twice only adds a second
        temp b-tree (measured 239ms -> 273ms on the mid bench set). The two map axes GROUP HERE:
        their key lives inside a JSON column, so ``json_each`` multiplies every rollup row by its
        key count -- a quarter-million tuples on the mid set -- which collapse to a dozen groups.
        Grouping them in the arm keeps that collapse where it was before the union, and keeps
        ``api_count`` what it always was: ``COUNT(DISTINCT api_id)`` over the WHOLE DAYS, carried
        out as ``n`` and merged with MAX (an edge day contributes no api_count, as before)."""
        if by_key:
            day_from, day_to = interior
            clauses, params = ["axis = ?"], [q.group_by]
            if day_from is not None:
                clauses.append("day >= ?")
                params.append(day_from)
            if day_to is not None:
                clauses.append("day <= ?")
                params.append(day_to)
            return (
                'SELECT "key" AS gkey, SUM("count") AS c, SUM("count" - fail - error) AS p, '
                "SUM(fail) AS f, SUM(error) AS e, 0 AS t, MAX(p95_duration_ms) AS q95, 0 AS n "
                f"FROM rollup_key_day WHERE {' AND '.join(clauses)} GROUP BY gkey"
            ), params
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

    def _fill_key_api_count(
        self, groups: list[RunGroup], q: AggregateQuery, interior: tuple[str | None, str | None],
    ) -> None:
        """``api_count`` for the keys ``_top_groups`` actually returned: the union of each key's
        stored ``api_ids`` across the window's whole days -- the same TRUE distinct count the
        ``COUNT(DISTINCT api_id)`` over ``rollup_day`` produced, and never ``len(run_ids)``.

        A stored per-day count would be cheaper and WRONG: the same API appears on many days of
        the window, so summing days double-counts it. The union is taken in python over at most
        (days x q.limit) rows, and only after the cut -- never over every key in the window."""
        if not groups:
            return
        day_from, day_to = interior
        clauses, params = ["axis = ?"], [q.group_by]
        if day_from is not None:
            clauses.append("day >= ?")
            params.append(day_from)
        if day_to is not None:
            clauses.append("day <= ?")
            params.append(day_to)
        placeholders = ",".join("?" for _ in groups)
        clauses.append(f'"key" IN ({placeholders})')
        params.extend(g.key for g in groups)
        unions: dict[str, set[str]] = {g.key: set() for g in groups}
        for key, api_ids in self._tuples(
            f'SELECT "key", api_ids FROM rollup_key_day WHERE {" AND ".join(clauses)}', params,
        ):
            if api_ids:
                unions[key].update(json.loads(api_ids))
        for group in groups:
            group.api_count = len(unions[group.key])

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

    # -- the self-growth query engine (spec 2026-09-07 §3/§4) ------------------------------------

    def query(self, spec: QuerySpec, *, now: datetime, tz: str = "Asia/Seoul",
              default_window_days: int = 30) -> QueryResult:
        """Execute one ``QuerySpec``. Compilation (and with it source selection, the window rule
        and every measure's arithmetic) lives in ``query_sql``; this method owns only the round
        trips: the ranked page, the totals, the two fills a source cannot answer in SQL, the
        evidence samples, and the previous-window join.

        Python sees at most ``spec.limit`` rows -- the rank and the cut are in the statement, the
        scale branch's standing rule."""
        compiled = compile_query(spec, now=now, tz=tz, default_days=default_window_days)
        rows, raw_keys = self._query_rows(spec, compiled)
        total_groups, population = self._conn.execute(
            compiled.totals_sql, compiled.totals_params).fetchone()
        if compiled.p95_sql:
            self._fill_query_p95(rows, raw_keys, compiled)
        if compiled.apis_from_key_rows:
            self._fill_query_key_apis(rows, raw_keys, spec, compiled)
        if spec.include_samples:
            self._fill_query_samples(rows, raw_keys, spec, compiled, tz)
        if spec.compare_previous_window:
            previous = compile_query(spec, now=now, tz=tz, previous=True,
                                     default_days=default_window_days)
            self._join_previous_window(rows, raw_keys, spec, previous)
        return QueryResult(
            spec=spec, rows=rows, total_groups=total_groups, population=population,
            window=day_bounds(compiled.day_from, compiled.day_to, tz), source=compiled.source,
        )

    def _query_rows(self, spec: QuerySpec, compiled: CompiledQuery) -> tuple[list[QueryRow], list[tuple]]:
        """The page, plus the RAW key tuple of each row. The raw tuple is what every follow-up
        statement matches on: ``QueryRow.keys`` has already mapped the ``''`` label sentinel back
        to ``None``, and a fill that matched on the mapped value would silently miss that row."""
        rows: list[QueryRow] = []
        raw_keys: list[tuple] = []
        for row in self._conn.execute(compiled.sql, compiled.params).fetchall():
            raw = tuple(row[f"k{i}"] for i in range(len(spec.dimensions)))
            raw_keys.append(raw)
            keys = {
                dim: (_label_out(value) if dim == "test_data_label" else value)
                for dim, value in zip(spec.dimensions, raw, strict=True)
            }
            rows.append(QueryRow(keys=keys, measures={
                name: _measure_out(name, row[f"m_{name}"]) for name in spec.measures}))
        return rows, raw_keys

    def _fill_query_p95(self, rows: list[QueryRow], raw_keys: list[tuple],
                        compiled: CompiledQuery) -> None:
        """The ``runs`` source's exact p95 (``query_sql._runs_p95``), matched onto the returned
        rows. One extra statement, never one per row."""
        levels = {
            tuple(row[i] for i in range(len(compiled.group_columns))): row[-1]
            for row in self._tuples(compiled.p95_sql, compiled.p95_params)
        }
        for row, key in zip(rows, raw_keys, strict=True):
            row.measures["p95_duration_ms"] = levels.get(key)

    def _fill_query_key_apis(self, rows: list[QueryRow], raw_keys: list[tuple], spec: QuerySpec,
                             compiled: CompiledQuery) -> None:
        """``apis`` on the ``rollup_key_day`` arm: the union of the returned keys' stored
        ``api_ids`` across the window's days -- the TRUE distinct count, exactly as
        ``_fill_key_api_count`` computes it for ``aggregate_runs``. A stored per-day COUNT would
        be cheaper and wrong: the same API appears on many days of a window."""
        if not rows:
            return
        axis = next(d for d in spec.dimensions if d in _QUERY_KEY_AXES)
        placeholders = ",".join("?" for _ in rows)
        unions: dict[str, set[str]] = {key[0]: set() for key in raw_keys}
        for key, api_ids in self._tuples(
            f'SELECT "key", api_ids FROM rollup_key_day WHERE axis = ? AND day >= ? AND day <= ? '
            f'AND "key" IN ({placeholders})',
            [axis, compiled.day_from, compiled.day_to, *(k[0] for k in raw_keys)],
        ):
            if api_ids:
                unions[key].update(json.loads(api_ids))
        for row, key in zip(rows, raw_keys, strict=True):
            row.measures["apis"] = len(unions[key[0]])

    def _fill_query_samples(self, rows: list[QueryRow], raw_keys: list[tuple], spec: QuerySpec,
                           compiled: CompiledQuery, tz: str) -> None:
        """Evidence for each returned row: up to 20 api ids and the 5 newest run ids, from
        ``runs`` under the spec's own filters plus the row's key -- two indexed statements per
        row, never a scan per row and never more than ``2 * limit`` statements per query.

        These ids are what the executor remembers in ``seen_apis``/``seen_runs``, so the card's
        provenance discipline (an id the session has seen) holds for a query result exactly as it
        does for a digest."""
        base, base_params = sample_predicates(spec, compiled.day_from, compiled.day_to, tz)
        for row, raw in zip(rows, raw_keys, strict=True):
            clauses, params = list(base), list(base_params)
            for dimension, value in zip(spec.dimensions, raw, strict=True):
                clauses += row_key_predicates(dimension, value, params)
            where_sql = " AND ".join(clauses)
            row.api_ids = [r[0] for r in self._tuples(
                f"SELECT DISTINCT api_id FROM runs WHERE {where_sql} ORDER BY api_id LIMIT ?",
                [*params, API_SAMPLE_CAP])]
            row.run_ids = [r[0] for r in self._tuples(
                f"SELECT run_id FROM runs WHERE {where_sql} "
                "ORDER BY executed_at DESC, run_id DESC LIMIT ?",
                [*params, RUN_SAMPLE_CAP])]

    def _join_previous_window(self, rows: list[QueryRow], raw_keys: list[tuple], spec: QuerySpec,
                              previous: CompiledQuery) -> None:
        """``compare_previous_window``: the same compiled query over the window immediately
        before, joined on the group key. A key the previous window did not return counts as 0
        (``None`` for the two measures that are levels rather than counters, where 0 would be a
        claim about a duration nobody measured).

        The previous query carries the SAME ``LIMIT``, so a key that exists in both windows but
        ranked below the cut in the earlier one reads ``_prev = 0``. That is the brief's bound --
        two bounded statements per query, never an unbounded second pass -- and it is why the
        card labels the column as a comparison of the two TOP lists, not of two populations.

        **The previous window goes through the SAME fills as the current one** (fix round 1). The
        earlier code read the raw statement, whose ``m_apis`` is a literal ``NULL`` on the two
        arms that have no api column -- so on the key arm every ``apis_prev`` came back 0 and
        every ``apis_delta`` equalled the current count, a fabricated "the previous window touched
        no API". ``rollup_key_day``'s ``apis`` is filled in python from the stored ``api_ids``
        union and the ``runs`` arm's p95 by its own statement, and both now run on the previous
        rows before the join. Whatever is left ``NULL`` after that is left ``NULL``:
        ``previous.unavailable_measures`` names the measures this source never measured, and they
        get ``None``/``None`` rather than a comparison against a number nobody computed."""
        prev_rows, prev_keys = self._query_rows(spec, previous)
        if previous.p95_sql:
            self._fill_query_p95(prev_rows, prev_keys, previous)
        if previous.apis_from_key_rows:
            self._fill_query_key_apis(prev_rows, prev_keys, spec, previous)
        before = {key: row.measures for row, key in zip(prev_rows, prev_keys, strict=True)}
        unavailable = set(previous.unavailable_measures)
        for row, key in zip(rows, raw_keys, strict=True):
            other = before.get(key)
            for name in spec.measures:
                current = row.measures.get(name)
                prior = None if other is None else other.get(name)
                # None for three different reasons, all of them "no number": the source cannot
                # measure this at all; the previous window returned this key but no value for it;
                # or the key is absent and the measure is a LEVEL, where 0 would be a claim about
                # a duration nobody timed. A count on an absent key is a real 0.
                if (name in unavailable
                        or (prior is None
                            and (other is not None or name in _LEVEL_MEASURES))):
                    row.measures[f"{name}_prev"] = None
                    row.measures[f"{name}_delta"] = None
                    continue
                prior = 0 if prior is None else prior
                row.measures[f"{name}_prev"] = prior
                if current is None:
                    row.measures[f"{name}_delta"] = None
                elif name == "fail_rate":
                    row.measures[f"{name}_delta"] = round(current - prior, 4)
                else:
                    row.measures[f"{name}_delta"] = current - prior

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
        sql, params = self.watermarks_sql(
            api_ids, first_non_pass_since, last_non_pass_since,
            scope_operator=scope_operator, scope_since=scope_since)
        return [self._row_to_watermark(r) for r in self._conn.execute(sql, params).fetchall()]

    @staticmethod
    def watermarks_sql(
        api_ids: Sequence[str] | None = None, first_non_pass_since: datetime | None = None,
        last_non_pass_since: datetime | None = None, *,
        scope_operator: str | None = None, scope_since: datetime | None = None,
    ) -> tuple[str, list]:
        """``watermarks``' statement, verbatim -- built here so the query-plan test can EXPLAIN the
        REAL query rather than a hand-copied string that rots the first time this changes.

        The ORDER BY is CONDITIONAL, and that is the whole of final review I7's fix. `ORDER BY
        api_id` is served for free by the primary key, so SQLite always chose to scan the PK in
        api_id order -- one row per API, 50,000 of them on the full set -- rather than seek the
        `first_non_pass_at` index and then sort a handful of rows; the index existed and was never
        used. When a ``*_since`` predicate is present the order becomes that column (api_id still
        breaking ties), which is both deterministic and index-served. No caller depends on api_id
        order: every one of them re-sorts (`insights` by first_non_pass_at, `resolve_select_where`
        by api_updated_at) or dict-ifies by api_id."""
        # `keep_outer_index` when a since-predicate is present, for the reason
        # `_operator_scope_clause` documents: the scope subquery is an indexable term, and left
        # indexable the planner drives the whole query off `operator_api` and probes the
        # watermark PK once per scoped API -- 33,801 probes for the busiest operator on the full
        # bench set, to return 279 rows. Marked non-indexable, it seeks
        # `idx_api_watermark_first_non_pass` instead and filters the handful that survive:
        # measured 103ms -> 34ms at 2M runs. With no since-predicate there is no outer index to
        # protect, so the scope stays indexable and drives, which is right for that shape.
        anchored = first_non_pass_since is not None or last_non_pass_since is not None
        clauses, params = _operator_scope_clause(
            scope_operator, scope_since, keep_outer_index=anchored)
        if api_ids is not None:
            placeholders = ",".join("?" for _ in api_ids)
            clauses.append(f"api_id IN ({placeholders})" if api_ids else "0")
            params.extend(api_ids)
        order = "api_id"
        if first_non_pass_since is not None:
            clauses.append("first_non_pass_at >= ?")
            params.append(_iso(first_non_pass_since))
            order = "first_non_pass_at, api_id"
        if last_non_pass_since is not None:
            clauses.append("last_non_pass_at >= ?")
            params.append(_iso(last_non_pass_since))
            order = "last_non_pass_at, api_id"
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return f"SELECT * FROM api_watermark {where_sql} ORDER BY {order}", params

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
