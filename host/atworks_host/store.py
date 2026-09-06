"""SQLite-backed storage for MockAtworks -- the schema blueprint the Java aTworks side will
implement. Task 4 (spec 2026-09-06) moves runs/bodies/audit off python dicts and onto indexed SQL;
`apis` mirrors into a table too so `search_apis` pages over SQL, but the authoritative in-memory
index for ledger guardrails stays `MockAtworks.apis` (a plain dict) per the controller ruling.

``current_state`` / ``rollup_day`` / ``api_watermark`` / ``operator_api`` / ``runs_archive`` /
``retention_state`` are all created here (the full DDL is the blueprint) but Task 4 does not
populate the materialized ones (``rollup_day``, ``api_watermark`` as a table, ``operator_api``,
``current_state`` as a table) -- ``current_state()`` / ``watermarks()`` / ``aggregate_runs()`` /
``operator_scope_ids()`` below compute on demand from the base ``runs`` table with indexed,
windowed SQL. Task 5 materializes at ingest and switches these reads over.

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
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from atworks_agent import (
    ApiSpec,
    ApiWatermark,
    AuditEntry,
    CellState,
    Page,
    RunResult,
    RunsQuery,
    RunStatus,
    decode_cursor,
    encode_cursor,
)

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
    failed_rule_counts JSON,
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
        self._conn.commit()

    # -- fixtures ----------------------------------------------------------------------------

    def load_fixtures(self, fixtures_dir: Path, briefing_tz: str = "Asia/Seoul") -> None:
        """Loads apis.json and runs.json (computing ``day`` from ``executed_at``). operators.json
        stays a plain dict on MockAtworks -- no SQL table mirrors OperatorProfile in this task."""
        apis = [ApiSpec(**row) for row in json.loads((fixtures_dir / "apis.json").read_text(encoding="utf-8"))]
        runs = [RunResult(**row) for row in json.loads((fixtures_dir / "runs.json").read_text(encoding="utf-8"))]
        self.load_apis(apis)
        self.insert_runs(runs, briefing_tz)

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
        cursor: str | None = None, limit: int = 20,
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

    def insert_runs(self, runs: Iterable[RunResult], briefing_tz: str = "Asia/Seoul") -> None:
        runs = list(runs)
        if not runs:
            return
        self._conn.executemany(
            "INSERT OR REPLACE INTO runs (run_id, api_id, job_id, target_env, test_data_label, status, "
            "http_status, duration_ms, executed_at, executed_by, failed_rules, day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [self._run_row(r, briefing_tz) for r in runs],
        )
        body_rows = [(r.run_id, json.dumps(r.response_body), _iso(r.executed_at))
                     for r in runs if r.response_body is not None]
        if body_rows:
            self._conn.executemany(
                "INSERT OR REPLACE INTO bodies (run_id, body, captured_at) VALUES (?,?,?)", body_rows,
            )
        self._conn.commit()

    def upsert_run(self, run: RunResult, briefing_tz: str = "Asia/Seoul") -> None:
        """Single-row insert-or-replace -- used by execute_job_once and by the ``MockAtworks.runs``
        dict-compat view's ``__setitem__`` (tests still poke ``backend.runs[run_id] = run``)."""
        self.insert_runs([run], briefing_tz)
        if run.response_body is None:
            self._conn.execute("DELETE FROM bodies WHERE run_id = ?", (run.run_id,))
            self._conn.commit()

    def replace_all_runs(self, runs: Mapping[str, RunResult] | Iterable[RunResult], briefing_tz: str = "Asia/Seoul") -> None:
        """Wholesale replacement -- backs ``backend.runs = {...}`` in tests that build a
        purpose-built run set from scratch, discarding the fixtures entirely."""
        values = list(runs.values()) if isinstance(runs, Mapping) else list(runs)
        self._conn.execute("DELETE FROM runs")
        self._conn.execute("DELETE FROM bodies")
        self._conn.commit()
        self.insert_runs(values, briefing_tz)

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

    # -- current_state / watermarks / operator_scope (windowed SQL + a bounded python pass per
    #    group -- see module docstring: Task 5 materializes these, Task 4 computes on demand) ----

    def current_state(self, scope_api_ids: Sequence[str] | None = None) -> list[CellState]:
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

    def watermarks(
        self, api_ids: Sequence[str] | None = None, first_non_pass_since: datetime | None = None,
    ) -> list[ApiWatermark]:
        clauses, params = self._runs_predicates(api_ids=api_ids)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        present = self._conn.execute(f"SELECT DISTINCT api_id FROM runs {where_sql}", params).fetchall()
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
            if first_non_pass_since is not None and (first_non_pass is None or first_non_pass < first_non_pass_since):
                continue
            result.append(ApiWatermark(
                api_id=api_id,
                last_pass_at=_parse_iso(passes[-1]) if passes else None,
                first_non_pass_at=first_non_pass,
                last_non_pass_at=_parse_iso(non_passes[-1]["executed_at"]) if non_passes else None,
                latest_status=RunStatus(rows[-1]["status"]) if rows else None,
            ))
        return result

    def operator_scope_ids(self, operator_id: str, window_days: int, now: datetime) -> set[str]:
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
