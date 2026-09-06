"""Golden-parity + schema tests for the SQLite-backed Store (scale spec 2026-09-06, Task 4).

The golden tests keep a copy of the *pre-Task-4* dict-based filtering/paging code (the oracle
functions below, copied verbatim from the mock_backend.py this task replaced) and assert that
Store's SQL-backed reads return exactly what that code returned, for the same fixture data and the
same query shapes. `current_state`/`watermarks` compare as dict-by-key (their list order was never
a documented contract of the old dict code -- it fell out of python dict iteration order) while
every ordered read (search_apis/list_runs/aggregate_runs/runs_by_ids) compares list-for-list.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from atworks_agent import (
    AggregateQuery,
    ApiSpec,
    ApiWatermark,
    AuditEntry,
    CellState,
    Page,
    RunResult,
    RunsQuery,
    RunStatus,
    ScopeSummary,
    decode_cursor,
    encode_cursor,
    operator_scope,
)
from atworks_agent.aggregation import GROUP_BY, _keys, aggregate
from atworks_agent.serialization import run_record
from atworks_host.store import Store

KST = timezone(timedelta(hours=9))
FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"


def _load_fixture_apis() -> dict[str, ApiSpec]:
    return {row["api_id"]: ApiSpec(**row) for row in json.loads((FIXTURES / "apis.json").read_text(encoding="utf-8"))}


def _load_fixture_runs() -> dict[str, RunResult]:
    """The fixture rows plus the display label the read path joins on (Task 10): every Store SELECT
    that returns a renderable run LEFT JOINs the apis mirror for `api_method`/`api_path`, so the
    oracle has to carry the same two fields or every list-for-list comparison below trips on them.
    A run whose api_id is not in the apis fixture keeps them None, exactly as the LEFT JOIN does.

    `has_body=False` for the same reason (final review I3): the read paths no longer join
    `bodies` at all, they carry an EXISTS probe instead, and no fixture run has a body."""
    apis = _load_fixture_apis()
    runs = {}
    for row in json.loads((FIXTURES / "runs.json").read_text(encoding="utf-8")):
        spec = apis.get(row["api_id"])
        runs[row["run_id"]] = RunResult(
            **row,
            api_method=spec.method if spec else None,
            api_path=spec.path if spec else None,
            has_body=False,
        )
    return runs


def _store_with_fixtures() -> Store:
    store = Store(":memory:")
    store.load_fixtures(FIXTURES, briefing_tz="Asia/Seoul")
    return store


# -- oracle: the pre-Task-4 dict implementation, unchanged -------------------------------------


def _oracle_page(rows: list, cursor, limit: int, *, key) -> Page:
    ordered = sorted(rows, key=key, reverse=True)
    total = len(ordered)
    if cursor:
        after = decode_cursor(cursor)
        ordered = [row for row in ordered if key(row) < after]
    items = ordered[:limit]
    next_cursor = encode_cursor(*key(items[-1])) if len(ordered) > limit else None
    return Page(items=items, next_cursor=next_cursor, total=total)


def _oracle_search_apis(apis, query="", group=None, updated_after=None, cursor=None, limit=20) -> Page[ApiSpec]:
    q = (query or "").lower()
    rows = [a for a in apis.values()
            if (not q or q in f"{a.method} {a.path} {a.name}".lower())
            and (group is None or a.group == group)
            and (updated_after is None or a.updated_at >= updated_after)]
    return _oracle_page(rows, cursor, limit, key=lambda a: (a.updated_at, a.api_id))


def _oracle_filter_runs(runs, since, status, api_id, *, until=None, executed_by=None, job_id=None) -> list[RunResult]:
    rows = [r for r in runs.values()
            if (since is None or r.executed_at >= since)
            and (until is None or r.executed_at < until)
            and (status is None or (r.status.value != "pass" if status == "non_pass" else r.status.value == status))
            and (api_id is None or r.api_id == api_id)
            and (executed_by is None or r.executed_by == executed_by)
            and (job_id is None or r.job_id == job_id)]
    return sorted(rows, key=lambda r: r.executed_at, reverse=True)


def _oracle_list_runs(runs, q: RunsQuery) -> Page[RunResult]:
    rows = _oracle_filter_runs(runs, q.since, q.status, q.api_id, until=q.until, executed_by=q.executed_by, job_id=q.job_id)
    return _oracle_page(rows, q.cursor, q.limit, key=lambda r: (r.executed_at, r.run_id))


def _oracle_count_runs(runs, since=None, until=None, status=None, api_id=None) -> int:
    return sum(
        1 for r in runs.values()
        if (since is None or r.executed_at >= since)
        and (until is None or r.executed_at < until)
        and (status is None or (r.status.value != "pass" if status == "non_pass" else r.status.value == status))
        and (api_id is None or r.api_id == api_id)
    )


def _oracle_aggregate_runs(runs, apis, q: AggregateQuery, flaky_min: int):
    rows = [r for r in runs.values()
            if (q.since is None or r.executed_at >= q.since)
            and (q.until is None or r.executed_at < q.until)
            and (q.scope_api_ids is None or r.api_id in q.scope_api_ids)]
    groups = aggregate(rows, apis, q.group_by, flaky_min_transitions=flaky_min)
    return groups[: q.limit]


def _oracle_current_state(runs, scope_api_ids=None) -> list[CellState]:
    cells: dict[tuple[str, str, str | None], list[RunResult]] = defaultdict(list)
    for r in runs.values():
        if scope_api_ids is not None and r.api_id not in scope_api_ids:
            continue
        cells[(r.api_id, r.target_env, r.test_data_label)].append(r)
    result: list[CellState] = []
    for (api_id, env, data_label), rows in cells.items():
        rows.sort(key=lambda r: r.executed_at)
        transitions = sum(
            1 for prev, cur in zip(rows, rows[1:], strict=False)
            if (prev.status.value != "pass") != (cur.status.value != "pass")
        )
        latest = rows[-1]
        result.append(CellState(
            api_id=api_id, target_env=env, test_data_label=data_label, run_id=latest.run_id,
            status=latest.status, executed_at=latest.executed_at, transitions_total=transitions,
        ))
    return result


def _oracle_watermarks(runs, api_ids=None, first_non_pass_since=None, last_non_pass_since=None,
                       apis=None) -> list[ApiWatermark]:
    by_api: dict[str, list[RunResult]] = defaultdict(list)
    for r in runs.values():
        if api_ids is not None and r.api_id not in api_ids:
            continue
        by_api[r.api_id].append(r)
    result: list[ApiWatermark] = []
    for api_id, rows in by_api.items():
        rows.sort(key=lambda r: r.executed_at)
        passes = [r.executed_at for r in rows if r.status.value == "pass"]
        non_passes = [r for r in rows if r.status.value != "pass"]
        first_non_pass = non_passes[0].executed_at if non_passes else None
        last_non_pass = non_passes[-1].executed_at if non_passes else None
        if first_non_pass_since is not None and (first_non_pass is None or first_non_pass < first_non_pass_since):
            continue
        if last_non_pass_since is not None and (last_non_pass is None or last_non_pass < last_non_pass_since):
            continue
        catalogue = apis or _load_fixture_apis()
        api = catalogue.get(api_id)
        result.append(ApiWatermark(
            api_id=api_id, last_pass_at=passes[-1] if passes else None,
            first_non_pass_at=first_non_pass,
            last_non_pass_at=last_non_pass,
            latest_status=rows[-1].status if rows else None,
            # Task 8: the catalogue's own updated_at rides on the watermark row so the regression
            # rule needs no join (spec §3's ``api_watermark ... updated_at(api)``).
            api_updated_at=api.updated_at if api is not None else None,
        ))
    return result


def _oracle_operator_scope(runs, operator_id: str, window_days: int, now: datetime) -> ScopeSummary:
    scope = operator_scope(list(runs.values()), operator_id, window_days, now)
    return ScopeSummary(api_ids=sorted(scope)[:100], total=len(scope))


def _by_key(cells: list[CellState]) -> dict:
    return {(c.api_id, c.target_env, c.test_data_label): c for c in cells}


def _by_api(marks: list[ApiWatermark]) -> dict:
    return {m.api_id: m for m in marks}


# -- schema / fixtures --------------------------------------------------------------------------


def test_init_schema_is_idempotent():
    store = Store(":memory:")
    store.init_schema()
    store.init_schema()
    store.init_schema()
    tables = {r["name"] for r in store.conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert tables >= {
        "apis", "runs", "runs_archive", "bodies", "current_state", "rollup_day",
        "api_watermark", "operator_api", "audit_log", "retention_state",
    }


def test_fixtures_load_12_apis_and_30_runs():
    store = _store_with_fixtures()
    apis_count = store.conn().execute("SELECT COUNT(*) FROM apis").fetchone()[0]
    runs_count = store.conn().execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert apis_count == 12
    assert runs_count == 30


# -- golden: search_apis ---------------------------------------------------------------------


def test_search_apis_matches_oracle_across_shapes():
    apis = _load_fixture_apis()
    store = _store_with_fixtures()
    shapes = [
        {},
        {"query": "contract"},
        {"query": "환불"},
        {"group": "payment"},
        {"updated_after": datetime(2026, 8, 27, tzinfo=KST)},
        {"limit": 3},
    ]
    for shape in shapes:
        expected = _oracle_search_apis(apis, **shape)
        actual = store.search_apis(**shape)
        assert actual.total == expected.total
        assert [a.api_id for a in actual.items] == [a.api_id for a in expected.items]
        # Cursors are opaque to callers (cursor.py's own contract) -- the Store normalizes every
        # stored timestamp to UTC (module docstring), so its raw cursor bytes legitimately differ
        # from the oracle's (which round-trips the fixture's original +09:00 offset unchanged);
        # what must match is what the cursor *means*, so compare decoded (instant, id) pairs.
        assert (decode_cursor(actual.next_cursor) if actual.next_cursor else None) == (
            decode_cursor(expected.next_cursor) if expected.next_cursor else None)
        assert actual.items == expected.items


# -- golden: list_runs / count_runs -------------------------------------------------------------


def test_list_runs_matches_oracle_across_query_shapes():
    runs = _load_fixture_runs()
    store = _store_with_fixtures()
    shapes = [
        RunsQuery(limit=50),
        RunsQuery(status="fail", limit=50),
        RunsQuery(status="non_pass", limit=50),
        RunsQuery(api_id="api-003", limit=50),
        RunsQuery(executed_by="jihoon", limit=50),
        RunsQuery(job_id="job-does-not-exist", limit=50),
        RunsQuery(since=datetime(2026, 9, 2, tzinfo=KST), limit=50),
        RunsQuery(until=datetime(2026, 9, 2, tzinfo=KST), limit=50),
        RunsQuery(limit=5),
    ]
    for q in shapes:
        expected = _oracle_list_runs(runs, q)
        actual = store.list_runs(q)
        assert [r.run_id for r in actual.items] == [r.run_id for r in expected.items], q
        assert actual.total == expected.total, q
        assert (decode_cursor(actual.next_cursor) if actual.next_cursor else None) == (
            decode_cursor(expected.next_cursor) if expected.next_cursor else None), q
        assert actual.items == expected.items, q


def test_count_runs_matches_oracle():
    runs = _load_fixture_runs()
    store = _store_with_fixtures()
    for kwargs in [
        {},
        {"status": "fail"},
        {"status": "error"},
        {"status": "non_pass"},
        {"api_id": "api-001"},
        {"since": datetime(2026, 9, 2, tzinfo=KST)},
        {"until": datetime(2026, 9, 2, tzinfo=KST)},
    ]:
        assert store.count_runs(**kwargs) == _oracle_count_runs(runs, **kwargs)


def test_get_run_and_runs_by_ids_match_oracle():
    runs = _load_fixture_runs()
    store = _store_with_fixtures()
    some_ids = ["run-0001", "run-0005", "run-0099-missing", "run-0002"]
    assert store.get_run("run-0001") == runs["run-0001"]
    assert store.get_run("nope") is None
    assert store.runs_by_ids(some_ids) == [runs[i] for i in some_ids if i in runs]


def test_get_body_roundtrips_and_is_none_for_fixture_runs():
    store = _store_with_fixtures()
    assert store.get_body("run-0001") is None   # fixture runs carry no response_body
    assert store.get_body("nope") is None
    run = RunResult(run_id="run-body-1", api_id="api-001", executed_at=datetime.now(UTC),
                     target_env="dev", status=RunStatus.PASS, response_body={"limit": 42})
    store.upsert_run(run)
    assert store.get_body("run-body-1") == {"limit": 42}


def test_no_read_path_carries_a_body_only_the_flag():
    """Final review I3. `list_runs`/`get_run`/`runs_by_ids`/`all_runs` used to LEFT JOIN `bodies`
    and hydrate `response_body` on every record, so a 200-row page pulled 200 response bodies into
    process memory on the model-facing path and the whole safety line was one `exclude=` in
    `run_record`. Now `get_body` is the ONLY method that reads the column at all -- the read paths
    carry `has_body`, a PK EXISTS probe."""
    store = _store_with_fixtures()
    run = RunResult(run_id="run-body-2", api_id="api-001", executed_at=datetime.now(UTC),
                    target_env="dev", status=RunStatus.PASS, response_body={"limit": 42})
    store.upsert_run(run)

    read = [store.get_run("run-body-2"),
            store.runs_by_ids(["run-body-2"])[0],
            store.list_runs(RunsQuery(api_id="api-001", limit=50)).items[0],
            next(r for r in store.all_runs() if r.run_id == "run-body-2")]
    for record in read:
        assert record.response_body is None                # never hydrated
        assert record.has_body is True                     # ...but the fact survives
    assert run_record(read[0])["has_body"] is True
    assert "response_body" not in run_record(read[0])
    # a run with no body reads False, not None: the probe ran and said no
    assert store.get_run("run-0001").has_body is False
    # and the content is still exactly one call away
    assert store.get_body("run-body-2") == {"limit": 42}

    # the SQL itself never mentions the body column outside `get_body`
    for sql in [store.list_runs_sql(RunsQuery(limit=10))[1][0],
                Store._RUN_SELECT, Store._RUN_JOINS]:
        assert "bodies.body" not in sql, sql


# -- golden: aggregate_runs (all 5 group_bys) ---------------------------------------------------


def test_aggregate_runs_matches_oracle_for_every_group_by():
    runs = _load_fixture_runs()
    apis = _load_fixture_apis()
    store = _store_with_fixtures()
    for group_by in GROUP_BY:
        q = AggregateQuery(group_by=group_by, limit=500)
        expected = _oracle_aggregate_runs(runs, apis, q, flaky_min=2)
        actual = store.fetch_runs(since=q.since, until=q.until, api_ids=q.scope_api_ids)
        actual_groups = aggregate(actual, apis, q.group_by, flaky_min_transitions=2)[: q.limit]
        assert actual_groups == expected, group_by


def test_aggregate_runs_respects_scope_api_ids_and_since():
    runs = _load_fixture_runs()
    apis = _load_fixture_apis()
    store = _store_with_fixtures()
    q = AggregateQuery(group_by="api", scope_api_ids=["api-003", "api-007"], limit=500)
    expected = _oracle_aggregate_runs(runs, apis, q, flaky_min=2)
    actual = store.fetch_runs(since=q.since, until=q.until, api_ids=q.scope_api_ids)
    actual_groups = aggregate(actual, apis, q.group_by, flaky_min_transitions=2)[: q.limit]
    assert actual_groups == expected


# -- golden: rollup-fed aggregate_rollups (Task 8) vs the same run-scan oracle -------------------

_SAME = ("count", "fail", "error", "passed", "run_ids", "regression_suspect", "flaky", "transitions")


def _rollup_groups(store: Store, apis, q: AggregateQuery) -> list:
    return store.aggregate_rollups(q, flaky_min=2, apis=apis, briefing_tz="Asia/Seoul")


def test_aggregate_rollups_matches_the_run_scan_oracle_for_every_group_by():
    """The rollup-fed read returns the same groups, in the same order, with the same counts and
    the same evidence ids as a from-scratch scan of every run in the window.

    Two fields carry documented deltas (Store.aggregate_rollups' docstring lists all of them):
    ``transitions`` on the axes whose key spans several cells, and the watermark-derived
    ``regression_suspect``/``last_pass_before`` on the api axis. Each is asserted below against
    its NEW definition, with the delta named.
    """
    runs = _load_fixture_runs()
    apis = _load_fixture_apis()
    store = _store_with_fixtures()
    for group_by in GROUP_BY:
        q = AggregateQuery(group_by=group_by, limit=500)
        expected = _oracle_aggregate_runs(runs, apis, q, flaky_min=2)
        actual = _rollup_groups(store, apis, q)
        assert [g.key for g in actual] == [g.key for g in expected], group_by
        assert [g.label for g in actual] == [g.label for g in expected], group_by
        for got, want in zip(actual, expected, strict=True):
            fields = _SAME
            if group_by in ("env", "http_status"):
                # DELTA: transitions is the sum of the ingest-time PER-CELL counters, so a key
                # that spans several cells (every env / http_status bucket) no longer counts
                # flips of the interleaved multi-cell sequence -- which was never what flaky_v1
                # means (it is defined on the api_env_data cell, where the two still agree).
                fields = tuple(f for f in _SAME if f not in ("transitions", "flaky"))
            if group_by == "api" and want.regression_suspect != got.regression_suspect:
                # DELTA: spec §3's rule reads the watermark (last_pass_at < api.updated_at <=
                # first_non_pass_at), so an API that has since RECOVERED -- its last pass now
                # later than its first failure -- is no longer a standing regression suspect.
                # The fixture's api-004 is exactly that: fail once, then pass again.
                assert got.key == "api-004" and want.regression_suspect and not got.regression_suspect
                fields = tuple(f for f in fields if f != "regression_suspect")
            for field in fields:
                assert getattr(got, field) == getattr(want, field), (group_by, got.key, field)


def test_aggregate_rollups_derived_fields_come_from_the_materialized_tables():
    apis = _load_fixture_apis()
    store = _store_with_fixtures()
    marks = {w.api_id: w for w in store.watermarks()}
    for group in _rollup_groups(store, apis, AggregateQuery(group_by="api", limit=500)):
        mark = marks[group.key]
        assert group.latest_status == mark.latest_status
        assert group.first_non_pass_at == mark.first_non_pass_at
        # DELTA: last_pass_before is the watermark's last_pass_at (the API's last pass EVER),
        # not "the last pass before the first failure inside the window".
        assert group.last_pass_before == mark.last_pass_at
        assert group.api_updated_at == apis[group.key].updated_at
    cells = {f"{c.api_id}|{c.target_env}|{c.test_data_label or '-'}": c for c in store.current_state()}
    for group in _rollup_groups(store, apis, AggregateQuery(group_by="api_env_data", limit=500)):
        assert group.latest_status == cells[group.key].status
    # api_count is the TRUE distinct-API count for the two map-keyed axes, and only for those
    for group in _rollup_groups(store, apis, AggregateQuery(group_by="failed_rule", limit=500)):
        rule_runs = {r.api_id for r in _load_fixture_runs().values()
                     if r.status.value != "pass" and (
                         group.key in r.failed_rules
                         or (not r.failed_rules and group.key.startswith("(error) HTTP")))}
        assert group.api_count == len(rule_runs), group.key
    for group in _rollup_groups(store, apis, AggregateQuery(group_by="api", limit=500)):
        assert group.api_count is None


def test_aggregate_rollups_window_is_exact_even_when_it_contains_no_whole_day():
    """The daily briefing's window is 09:00 -> 09:00: it touches two local days and contains none
    of them whole. Every count still matches the exact run scan (the partial days are answered
    from `runs` with an indexed range query, not rounded out to whole rollup partitions)."""
    runs = _load_fixture_runs()
    apis = _load_fixture_apis()
    store = _store_with_fixtures()
    start = datetime(2026, 9, 2, 9, tzinfo=KST)
    end = datetime(2026, 9, 3, 9, tzinfo=KST)
    for group_by in GROUP_BY:
        q = AggregateQuery(group_by=group_by, since=start, until=end, limit=500)
        expected = _oracle_aggregate_runs(runs, apis, q, flaky_min=2)
        actual = _rollup_groups(store, apis, q)
        assert [g.key for g in actual] == [g.key for g in expected], group_by
        for got, want in zip(actual, expected, strict=True):
            for field in ("count", "fail", "error", "passed", "run_ids"):
                assert getattr(got, field) == getattr(want, field), (group_by, got.key, field)


def test_aggregate_rollups_status_filter_reports_only_that_verdict():
    runs = _load_fixture_runs()
    apis = _load_fixture_apis()
    store = _store_with_fixtures()
    for status in ("pass", "fail", "error", "non_pass"):
        for group_by in GROUP_BY:
            q = AggregateQuery(group_by=group_by, status=status, limit=500)
            filtered = {
                run_id: r for run_id, r in runs.items()
                if (r.status.value != "pass" if status == "non_pass" else r.status.value == status)
            }
            expected = _oracle_aggregate_runs(filtered, apis, AggregateQuery(group_by=group_by, limit=500), flaky_min=2)
            actual = _rollup_groups(store, apis, q)
            assert [g.key for g in actual] == [g.key for g in expected], (status, group_by)
            for got, want in zip(actual, expected, strict=True):
                for field in ("count", "fail", "error", "passed", "run_ids"):
                    assert getattr(got, field) == getattr(want, field), (status, group_by, got.key, field)


def test_aggregate_rollups_respects_scope_api_ids_and_since():
    runs = _load_fixture_runs()
    apis = _load_fixture_apis()
    store = _store_with_fixtures()
    q = AggregateQuery(group_by="api", scope_api_ids=["api-003", "api-007"], limit=500)
    expected = _oracle_aggregate_runs(runs, apis, q, flaky_min=2)
    actual = _rollup_groups(store, apis, q)
    assert [g.key for g in actual] == [g.key for g in expected]
    for got, want in zip(actual, expected, strict=True):
        for field in ("count", "fail", "error", "passed", "run_ids", "transitions"):
            assert getattr(got, field) == getattr(want, field), (got.key, field)

# -- Task 8 fix round 2: the rank and the cut live in the SQL -------------------------------------


def test_aggregate_rollups_transitions_order_matches_a_python_sort_of_the_oracle():
    """``order_by="transitions"`` returns exactly what sorting the run-scan oracle's own groups by
    (transitions desc, fail+error desc, key asc) and cutting to ``limit`` returns. The cut is the
    only thing the field moves: every counter is identical to the ``failures`` order's."""
    runs = _load_fixture_runs()
    apis = _load_fixture_apis()
    store = _store_with_fixtures()
    for group_by in GROUP_BY:
        oracle = _oracle_aggregate_runs(runs, apis, AggregateQuery(group_by=group_by, limit=500), flaky_min=2)
        expected = sorted(oracle, key=lambda g: (-g.transitions, -(g.fail + g.error), g.key))
        actual = _rollup_groups(store, apis, AggregateQuery(group_by=group_by, order_by="transitions", limit=500))
        if group_by in ("env", "http_status", "failed_rule"):
            # These axes carry the documented per-cell `transitions` delta (aggregate_rollups'
            # docstring), so only the FAILURES order is oracle-comparable key-for-key. What the
            # transitions order must still hold here is its own contract: the returned list is
            # sorted by the rank, and it is the same SET of groups as the failures order.
            failures = _rollup_groups(store, apis, AggregateQuery(group_by=group_by, limit=500))
            assert {g.key for g in actual} == {g.key for g in failures}, group_by
            keys = [(-g.transitions, -(g.fail + g.error), g.key) for g in actual]
            assert keys == sorted(keys), group_by
            continue
        assert [g.key for g in actual] == [g.key for g in expected], group_by
        for got, want in zip(actual, expected, strict=True):
            for field in ("count", "fail", "error", "passed", "transitions", "flaky", "run_ids"):
                assert getattr(got, field) == getattr(want, field), (group_by, got.key, field)


def test_transitions_order_reaches_a_quiet_flaky_cell_a_failure_ranked_cut_never_does():
    """The saturation the tile fix removed, removed from the CANDIDATES too: 60 cells that fail
    loudly and never flip, plus one cell that flips twice and fails once. ``limit=10`` under
    ``failures`` cannot see it; under ``transitions`` it is the first row."""
    now = datetime(2026, 9, 3, 12, tzinfo=KST)
    store = Store(":memory:")
    base = now - timedelta(days=2)
    runs = [RunResult(run_id=f"loud-{i:03d}-{k}", api_id=f"api-loud-{i:03d}", executed_at=base + timedelta(minutes=k),
                      target_env="dev", status=RunStatus.FAIL, failed_rules=["loud >= 0"])
            for i in range(60) for k in range(5)]
    runs += [RunResult(run_id=f"quiet-{k}", api_id="api-quiet", executed_at=base + timedelta(hours=k),
                       target_env="dev", status=status,
                       failed_rules=["quiet >= 0"] if status is RunStatus.FAIL else [])
             for k, status in enumerate((RunStatus.PASS, RunStatus.FAIL, RunStatus.PASS))]
    store.replace_all_runs(runs)
    since = now - timedelta(days=30)

    def keys(order_by: str) -> list[str]:
        return [g.key for g in store.aggregate_rollups(
            AggregateQuery(since=since, group_by="api_env_data", order_by=order_by, limit=10),
            flaky_min=2, apis={}, briefing_tz="Asia/Seoul")]

    assert "api-quiet|dev|-" not in keys("failures")
    assert keys("transitions")[0] == "api-quiet|dev|-"


def test_the_sql_key_tie_break_is_the_python_one_for_tied_groups():
    """Every group tied on failures and count, so the rank falls through to the KEY -- which is now
    SQLite's BINARY collation instead of python's string compare, on a key the cell axis builds by
    concatenation. The labels are chosen to separate the two collations if they ever differ
    (case, ``/`` vs letters, a non-ASCII code point)."""
    now = datetime(2026, 9, 3, 12, tzinfo=KST)
    store = Store(":memory:")
    base = now - timedelta(days=1)
    labels = ["a", "A", "z", "Z", "가", "basic", "b/asic", "b-asic", "b_asic", "0"]
    runs = [RunResult(run_id=f"tie-{i}", api_id=f"api-{i % 3:03d}", executed_at=base + timedelta(minutes=i),
                      target_env=("dev", "stg")[i % 2], test_data_label=label, status=RunStatus.FAIL,
                      failed_rules=["x >= 0"])
            for i, label in enumerate(labels)]
    store.replace_all_runs(runs)
    since = now - timedelta(days=30)
    apis: dict[str, ApiSpec] = {}
    for order_by in ("failures", "transitions"):
        groups = store.aggregate_rollups(
            AggregateQuery(since=since, group_by="api_env_data", order_by=order_by, limit=500),
            flaky_min=2, apis=apis, briefing_tz="Asia/Seoul")
        expected = aggregate(runs, apis, "api_env_data", flaky_min_transitions=2)
        # every group is one failing run, so both orders collapse onto the key tie-break
        assert [g.key for g in groups] == [g.key for g in expected], order_by
        assert [g.count for g in groups] == [g.count for g in expected], order_by


def test_the_unbound_test_data_label_is_one_group_in_sql_and_in_python():
    """The ``''`` sentinel: ``_label_in`` writes '' where the run carried None, and an older row
    may still hold NULL. All of NULL / '' / an explicit '-' are ONE cell whose key ends in ``|-``
    -- the SQL key expression and ``aggregation._keys`` must agree, or the SQL-side GROUP BY would
    split a cell python merges and the ranked cut would hand back a duplicate key."""
    now = datetime(2026, 9, 3, 12, tzinfo=KST)
    store = Store(":memory:")
    base = now - timedelta(days=1)
    runs = [RunResult(run_id=f"s-{i}", api_id="api-000", executed_at=base + timedelta(minutes=i),
                      target_env="dev", test_data_label=label, status=RunStatus.FAIL,
                      failed_rules=["x >= 0"])
            for i, label in enumerate([None, "", "-"])]
    store.replace_all_runs(runs)
    # and one row left NULL by a pre-sentinel writer
    store.conn().execute("UPDATE rollup_day SET test_data_label = NULL WHERE test_data_label = ''")
    store.conn().commit()
    groups = store.aggregate_rollups(
        AggregateQuery(since=now - timedelta(days=30), group_by="api_env_data", limit=500),
        flaky_min=2, apis={}, briefing_tz="Asia/Seoul")
    assert [(g.key, g.count) for g in groups] == [("api-000|dev|-", 3)]


def test_only_limit_rows_ever_cross_out_of_sql(monkeypatch):
    """The point of the round: with 200 groups in the window and ``limit=5``, SQL returns 5 rows.
    Before, every group in the window was fetched and ranked in python -- one accumulator per cell
    in the project on a real dataset."""
    now = datetime(2026, 9, 3, 12, tzinfo=KST)
    store = Store(":memory:")
    base = now - timedelta(days=1)
    store.replace_all_runs([
        RunResult(run_id=f"r-{i:03d}", api_id=f"api-{i:03d}", executed_at=base + timedelta(minutes=i),
                  target_env="dev", status=RunStatus.FAIL, failed_rules=["x >= 0"])
        for i in range(200)])
    sizes: list[int] = []
    original = Store._tuples

    def spy(self, sql, params):
        rows = original(self, sql, params)
        sizes.append(len(rows))
        return rows

    monkeypatch.setattr(Store, "_tuples", spy)
    groups = store.aggregate_rollups(AggregateQuery(since=now - timedelta(days=30), group_by="api", limit=5),
                                     flaky_min=2, apis={}, briefing_tz="Asia/Seoul")
    assert len(groups) == 5
    assert sizes == [5]



def test_http_status_counts_is_added_and_refilled_on_a_pre_task_8_store(tmp_path):
    """A store written before Task 8 has a rollup_day without http_status_counts. CREATE TABLE IF
    NOT EXISTS never widens it, so init_schema ALTERs the column in and rebuild_materialized
    refills it from the runs already stored -- no regeneration, no lost rollups."""
    path = tmp_path / "old.sqlite"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE rollup_day (day TEXT NOT NULL, api_id TEXT NOT NULL, target_env TEXT NOT NULL, "
        'test_data_label TEXT, count INTEGER NOT NULL DEFAULT 0, "pass" INTEGER NOT NULL DEFAULT 0, '
        "fail INTEGER NOT NULL DEFAULT 0, error INTEGER NOT NULL DEFAULT 0, "
        "transitions INTEGER NOT NULL DEFAULT 0, p95_duration_ms INTEGER, failed_rule_counts JSON, "
        "PRIMARY KEY (day, api_id, target_env, test_data_label));"
    )
    legacy.commit()
    legacy.close()

    store = Store(path)      # init_schema runs the ALTER
    columns = {r["name"] for r in store.conn().execute("PRAGMA table_info(rollup_day)").fetchall()}
    assert "http_status_counts" in columns
    store.load_fixtures(FIXTURES, briefing_tz="Asia/Seoul")
    store.rebuild_materialized()
    store.conn().commit()

    apis = _load_fixture_apis()
    expected = {g.key: g for g in aggregate(list(_load_fixture_runs().values()), apis, "http_status",
                                            flaky_min_transitions=2)}
    actual = {g.key: g for g in _rollup_groups(store, apis, AggregateQuery(group_by="http_status", limit=500))}
    assert set(actual) == set(expected)
    for key, group in actual.items():
        assert (group.count, group.fail, group.error) == (
            expected[key].count, expected[key].fail, expected[key].error)


def test_count_runs_by_job_counts_the_window_exactly():
    runs = _load_fixture_runs()
    store = _store_with_fixtures()
    since, until = datetime(2026, 9, 2, tzinfo=KST), datetime(2026, 9, 4, tzinfo=KST)
    expected: dict[str, int] = {}
    for r in runs.values():
        if r.job_id and since <= r.executed_at < until:
            expected[r.job_id] = expected.get(r.job_id, 0) + 1
    assert store.count_runs_by_job(since=since, until=until) == expected


def test_search_apis_path_prefix_is_anchored_and_does_not_widen_query():
    store = _store_with_fixtures()
    apis = _load_fixture_apis()
    hit = store.search_apis(path_prefix="/v1/payments", limit=500)
    assert {a.api_id for a in hit.items} == {
        a.api_id for a in apis.values() if a.path == "/v1/payments" or a.path.startswith("/v1/payments/")
    }
    assert hit.items and hit.total == len(hit.items)
    # anchored: a prefix that only appears mid-path matches nothing (a free-text query would)
    assert store.search_apis(path_prefix="payments", limit=500).items == []


# -- golden: current_state / watermarks / operator_scope -----------------------------------------


def test_current_state_matches_oracle():
    runs = _load_fixture_runs()
    store = _store_with_fixtures()
    expected = _by_key(_oracle_current_state(runs))
    actual = _by_key(store.current_state())
    assert actual == expected


def test_current_state_respects_scope_api_ids():
    runs = _load_fixture_runs()
    store = _store_with_fixtures()
    scope = ["api-003", "api-007"]
    expected = _by_key(_oracle_current_state(runs, scope_api_ids=scope))
    actual = _by_key(store.current_state(scope))
    assert actual == expected
    assert set(k[0] for k in actual) <= set(scope)


def test_watermarks_matches_oracle():
    runs = _load_fixture_runs()
    store = _store_with_fixtures()
    expected = _by_api(_oracle_watermarks(runs))
    actual = _by_api(store.watermarks())
    assert actual == expected


def test_watermarks_respects_first_non_pass_since():
    runs = _load_fixture_runs()
    store = _store_with_fixtures()
    since = datetime(2026, 9, 3, tzinfo=KST)
    expected = _by_api(_oracle_watermarks(runs, first_non_pass_since=since))
    actual = _by_api(store.watermarks(first_non_pass_since=since))
    assert actual == expected


# -- fix round 1: server-side operator scope + the two insight counts ---------------------------


def _scoped_store() -> tuple[Store, datetime]:
    """Two operators, 120 APIs each with no overlap, one shared API they BOTH ran. The scope
    predicate has to answer over all 240 without anyone shipping an id list."""
    now = datetime(2026, 9, 3, 12, tzinfo=KST)
    store = Store(":memory:")
    store.init_schema()
    apis = [ApiSpec(api_id=f"api-{i:04d}", method="GET", path=f"/v1/x/{i}", name=str(i),
                    updated_at=now - timedelta(days=300)) for i in range(240)]
    apis.append(ApiSpec(api_id="api-shared", method="GET", path="/v1/shared", name="shared",
                        updated_at=now - timedelta(days=300)))
    store.load_apis(apis)
    runs = []
    for i in range(240):
        operator = "minseong" if i < 120 else "jihoon"
        runs.append(RunResult(run_id=f"run-{i:04d}", api_id=f"api-{i:04d}",
                              executed_at=now - timedelta(days=2), target_env="dev",
                              status=RunStatus.FAIL, failed_rules=["x >= 0"], executed_by=operator))
    for who in ("minseong", "jihoon"):
        runs.append(RunResult(run_id=f"run-shared-{who}", api_id="api-shared",
                              executed_at=now - timedelta(days=2), target_env="dev",
                              status=RunStatus.PASS, executed_by=who))
    # an execution OUTSIDE any 30-day window: in operator_api but not in scope
    store.load_apis([ApiSpec(api_id="api-old", method="GET", path="/v1/old", name="old",
                             updated_at=now - timedelta(days=400))])
    runs.append(RunResult(run_id="run-old", api_id="api-old", executed_at=now - timedelta(days=200),
                          target_env="dev", status=RunStatus.FAIL, failed_rules=["x >= 0"],
                          executed_by="minseong"))
    store.replace_all_runs(runs)
    return store, now


def test_operator_scope_is_a_join_predicate_with_no_id_list_ceiling():
    """The whole point of fix (A): the scope has no cap, so all 121 of an operator's APIs are in
    it — an id list would have stopped at ``operator_scope_summary``'s 100."""
    store, now = _scoped_store()
    since = now - timedelta(days=30)
    sample, total = store.operator_scope_summary("minseong", 30, now)
    assert total == 121 and len(sample) == 100          # the sample really is short

    groups = store.aggregate_rollups(
        AggregateQuery(since=since, group_by="api", scope_operator="minseong", limit=500),
        flaky_min=2, apis={}, briefing_tz="Asia/Seoul")
    # all 121 of minseong's in-window APIs, not the 100 an id list would have carried
    assert {g.key for g in groups} == {f"api-{i:04d}" for i in range(120)} | {"api-shared"}
    assert "api-0200" not in {g.key for g in groups}    # jihoon's, correctly excluded

    marks = store.watermarks(scope_operator="minseong", scope_since=since)
    assert {w.api_id for w in marks} == {f"api-{i:04d}" for i in range(120)} | {"api-shared"}
    cells = store.current_state(scope_operator="minseong", scope_since=since)
    assert {c.api_id for c in cells} == {f"api-{i:04d}" for i in range(120)} | {"api-shared"}


def test_operator_scope_window_lower_bound_drops_an_old_execution():
    """``operator_api`` remembers forever; the scope does not. The aggregate's bound is the
    query's own ``since``, and ``current_state``/``watermarks`` take the caller's scope window."""
    store, now = _scoped_store()
    since = now - timedelta(days=30)
    assert "api-old" in store.operator_scope_ids("minseong", 365, now)      # 200 days back
    assert "api-old" not in store.operator_scope_ids("minseong", 30, now)

    groups = store.aggregate_rollups(
        AggregateQuery(since=since, group_by="api", scope_operator="minseong", limit=500),
        flaky_min=2, apis={}, briefing_tz="Asia/Seoul")
    assert "api-old" not in {g.key for g in groups}
    assert "api-old" not in {w.api_id for w in store.watermarks(scope_operator="minseong", scope_since=since)}
    assert "api-old" in {w.api_id for w in store.watermarks(scope_operator="minseong", scope_since=None)}


def test_summarize_insights_flaky_count_has_no_group_limit():
    """>500 loud cells plus one quiet flaky cell: the count over a ranked group list saturates,
    the SQL count does not."""
    now = datetime(2026, 9, 3, 12, tzinfo=KST)
    store = Store(":memory:")
    store.init_schema()
    store.load_apis([ApiSpec(api_id=f"api-{i:04d}", method="GET", path=f"/v1/x/{i}", name=str(i),
                             updated_at=now - timedelta(days=300)) for i in range(600)]
                    + [ApiSpec(api_id="api-flaky", method="GET", path="/v1/f", name="f",
                               updated_at=now - timedelta(days=300))])
    base = now - timedelta(days=2)
    runs = [RunResult(run_id=f"run-l-{i:04d}-{k}", api_id=f"api-{i:04d}",
                      executed_at=base + timedelta(minutes=k), target_env="dev", status=RunStatus.FAIL,
                      failed_rules=["loud >= 0"])
            for i in range(600) for k in range(3)]
    runs += [RunResult(run_id=f"run-f-{k}", api_id="api-flaky", executed_at=base + timedelta(hours=k),
                       target_env="dev", status=status,
                       failed_rules=["quiet >= 0"] if status is RunStatus.FAIL else [])
             for k, status in enumerate((RunStatus.PASS, RunStatus.FAIL, RunStatus.PASS))]
    store.replace_all_runs(runs)
    since = now - timedelta(days=30)

    groups = store.aggregate_rollups(AggregateQuery(since=since, group_by="api_env_data", limit=500),
                                     flaky_min=2, apis={}, briefing_tz="Asia/Seoul")
    assert len(groups) == 500 and not any(g.flaky for g in groups)   # the old tile really read 0
    assert store.summarize_insights(since, flaky_min=2, briefing_tz="Asia/Seoul").flaky == 1


def test_regression_definition_is_windowed_in_both_the_group_and_the_count():
    """Fix (D): one definition. An API whose first failure predates the window is not that
    window's regression suspect — for ``_fill_derived`` and for ``summarize_insights`` alike."""
    now = datetime(2026, 9, 3, 12, tzinfo=KST)
    store = Store(":memory:")
    store.init_schema()
    broke_recently = now - timedelta(days=5)
    broke_long_ago = now - timedelta(days=200)
    store.load_apis([
        ApiSpec(api_id="api-new", method="GET", path="/v1/n", name="n",
                updated_at=broke_recently - timedelta(hours=1)),
        ApiSpec(api_id="api-old", method="GET", path="/v1/o", name="o",
                updated_at=broke_long_ago - timedelta(hours=1)),
    ])
    runs = []
    for api_id, broke_at in (("api-new", broke_recently), ("api-old", broke_long_ago)):
        runs.append(RunResult(run_id=f"{api_id}-pass", api_id=api_id,
                              executed_at=broke_at - timedelta(hours=2), target_env="dev",
                              status=RunStatus.PASS))
        runs.append(RunResult(run_id=f"{api_id}-fail", api_id=api_id, executed_at=broke_at,
                              target_env="dev", status=RunStatus.FAIL, failed_rules=["x >= 0"]))
    store.replace_all_runs(runs)

    since = now - timedelta(days=30)
    groups = {g.key: g for g in store.aggregate_rollups(
        AggregateQuery(since=since, group_by="api", limit=500), flaky_min=2, apis={},
        briefing_tz="Asia/Seoul")}
    assert groups["api-new"].regression_suspect and "api-old" not in groups   # no runs in window
    assert store.summarize_insights(since, flaky_min=2, briefing_tz="Asia/Seoul").regression_suspect == 1
    # widen the window and both the group flag and the count agree again
    wide = now - timedelta(days=365)
    wide_groups = {g.key: g for g in store.aggregate_rollups(
        AggregateQuery(since=wide, group_by="api", limit=500), flaky_min=2, apis={},
        briefing_tz="Asia/Seoul")}
    assert wide_groups["api-new"].regression_suspect and wide_groups["api-old"].regression_suspect
    assert store.summarize_insights(wide, flaky_min=2, briefing_tz="Asia/Seoul").regression_suspect == 2


def test_summarize_insights_respects_the_operator_scope():
    store, now = _scoped_store()
    since = now - timedelta(days=30)
    everyone = store.summarize_insights(since, flaky_min=2, briefing_tz="Asia/Seoul")
    mine = store.summarize_insights(since, flaky_min=2, scope_operator="minseong", briefing_tz="Asia/Seoul")
    assert everyone.flaky == 0 and mine.flaky == 0          # nothing flips in this fixture
    assert everyone.regression_suspect == mine.regression_suspect == 0


def test_operator_scope_ids_matches_oracle():
    runs = _load_fixture_runs()
    store = _store_with_fixtures()
    now = datetime(2026, 9, 3, 14, tzinfo=KST)
    for operator_id, window_days in [("minseong", 30), ("jihoon", 30), ("sora", 1), ("nobody", 30)]:
        expected = _oracle_operator_scope(runs, operator_id, window_days, now)
        actual_ids = store.operator_scope_ids(operator_id, window_days, now)
        actual = ScopeSummary(api_ids=sorted(actual_ids)[:100], total=len(actual_ids))
        assert actual == expected, (operator_id, window_days)


# -- keyset paging exhaustiveness (300-run synthetic insert) -------------------------------------


def test_list_runs_keyset_paging_is_exhaustive_over_300_synthetic_runs():
    store = Store(":memory:")
    base = datetime(2026, 1, 1, tzinfo=UTC)
    synthetic = [
        RunResult(
            run_id=f"synthetic-{i:04d}", api_id=f"api-{i % 7}",
            executed_at=base + timedelta(minutes=i), target_env="dev",
            status=RunStatus.PASS if i % 3 else RunStatus.FAIL,
        )
        for i in range(300)
    ]
    store.ingest(synthetic, briefing_tz="UTC")

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = store.list_runs(RunsQuery(limit=37, cursor=cursor))
        assert page.total == 300
        seen.extend(r.run_id for r in page.items)
        pages += 1
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        assert pages <= 20
    assert pages >= 3
    assert len(seen) == 300
    assert len(set(seen)) == 300   # no duplicates
    assert set(seen) == {r.run_id for r in synthetic}   # no gaps


# -- audit: keyset, same-`at` seq tie-break -------------------------------------------------------


def test_audit_pages_same_timestamp_rows_by_seq_numerically():
    store = Store(":memory:")
    at = datetime(2026, 9, 3, 12, tzinfo=UTC)
    store.insert_audit_entries([
        AuditEntry(seq=9, at=at, operator="minseong", action="apply_job", target_kind="job",
                   target_id="job-9", session_id="s"),
        AuditEntry(seq=10, at=at, operator="minseong", action="apply_job", target_kind="job",
                   target_id="job-10", session_id="s"),
    ])
    page = store.audit(limit=50)
    assert [e.seq for e in page.items] == [10, 9]


def test_audit_keyset_paging_exhaustive():
    store = Store(":memory:")
    at = datetime(2026, 9, 3, 12, tzinfo=UTC)
    for i in range(25):
        store.append_audit(at + timedelta(seconds=i), "minseong", "apply_job", "job", f"job-{i}", "s")
    seen: list[int] = []
    cursor = None
    pages = 0
    while True:
        page = store.audit(cursor=cursor, limit=7)
        assert page.total == 25
        seen.extend(e.seq for e in page.items)
        pages += 1
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        assert pages <= 10
    assert pages >= 3
    assert len(seen) == 25 and len(set(seen)) == 25


# -- EXPLAIN QUERY PLAN: list_runs / count_runs(api_id) use an index --------------------------


def _assert_no_unindexed_scan(rows, table: str) -> None:
    pattern = re.compile(rf"\b(SCAN|SEARCH)\s+{table}\b")
    for row in rows:
        detail = row["detail"]
        if pattern.search(detail):
            assert "INDEX" in detail, f"unindexed access on {table!r}: {detail!r}"


def _explain_list_runs(store, q: RunsQuery):
    """EXPLAIN the page statement `list_runs` ACTUALLY runs, taken from `Store.list_runs_sql`.
    Task 10 review minor: this test used to EXPLAIN a hand-copied SQL string, so when Task 10 added
    the api-label join to `_RUN_SELECT`/`_RUN_JOINS` the test kept happily explaining the old,
    join-less query and proved nothing about the shipped one."""
    (_, _), (page_sql, page_params) = store.list_runs_sql(q)
    return store.conn().execute(f"EXPLAIN QUERY PLAN {page_sql}", page_params).fetchall()


def _runs_access(plan) -> str:
    """The one plan line that touches `runs`. A BARE `SCAN runs` (no `USING INDEX`) is the failure
    this test exists for -- it means the page read the whole table. `SCAN runs USING INDEX
    idx_runs_executed_at` is NOT that failure: it is the keyset order being served straight off the
    index, which is exactly what an unfiltered first page should do under `LIMIT`."""
    for row in plan:
        if re.match(r"^(SCAN|SEARCH) runs\b", row["detail"]):
            return row["detail"]
    raise AssertionError(f"no access line for `runs` in the plan: {[r['detail'] for r in plan]}")


def test_list_runs_query_plan_uses_an_index():
    store = _store_with_fixtures()

    # 1. filtered page -> an index SEEK, not a table walk.
    plan = _explain_list_runs(store, RunsQuery(api_id="api-001", limit=10))
    _assert_no_unindexed_scan(plan, "runs")
    assert _runs_access(plan).startswith("SEARCH runs USING INDEX"), _runs_access(plan)

    # 2. unfiltered first page -> the ORDER BY is served by an index, never a sort of the table.
    plan_no_filter = _explain_list_runs(store, RunsQuery(limit=10))
    _assert_no_unindexed_scan(plan_no_filter, "runs")
    assert "USING INDEX" in _runs_access(plan_no_filter), _runs_access(plan_no_filter)
    assert not any(row["detail"].startswith("USE TEMP B-TREE") for row in plan_no_filter), \
        [r["detail"] for r in plan_no_filter]

    # 3. the cursor page, which adds the keyset clause on top of the same statement.
    cursored = _explain_list_runs(
        store, RunsQuery(api_id="api-001", limit=10,
                         cursor=encode_cursor(datetime(2026, 9, 3, tzinfo=UTC), "run-999")))
    _assert_no_unindexed_scan(cursored, "runs")
    assert "USING INDEX" in _runs_access(cursored), _runs_access(cursored)

    # 4. what the SELECT list still needs is Task 10's api label (a PK lookup) and the `has_body`
    #    EXISTS probe -- a correlated scalar subquery answered from the bodies PK index, never a
    #    join that carries the body itself (final review I3).
    details = " | ".join(row["detail"] for row in plan)
    assert "SEARCH apis USING INDEX" in details, details
    assert "SCALAR SUBQUERY" in details, details
    assert re.search(r"SEARCH bodies USING (COVERING )?INDEX", details), details


def test_watermark_since_filters_use_an_index():
    """Final review I7. `first_non_pass_since` (every insight-panel build) and
    `last_non_pass_since` (`select_where.failed_since`, on the stage AND the LATE execution path)
    are the only two watermark reads that are PREDICATES rather than primary-key lookups, and
    `api_watermark` had no index for either -- so both scanned one row per API, 50,000 of them on
    the full set, on a request path, to return a handful."""
    store = _store_with_fixtures()
    for column, kwargs in (("first_non_pass_at", {"first_non_pass_since": datetime(2026, 9, 1, tzinfo=UTC)}),
                           ("last_non_pass_at", {"last_non_pass_since": datetime(2026, 9, 1, tzinfo=UTC)})):
        sql, params = store.watermarks_sql(**kwargs)        # the REAL statement, not a copy
        plan = store.conn().execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
        detail = " | ".join(row["detail"] for row in plan)
        assert "SEARCH api_watermark USING INDEX" in detail and column in detail, (column, detail)
        # ...and the read itself still answers exactly what the run-scan oracle does
        assert ({w.api_id for w in store.watermarks(**kwargs)}
                == {w.api_id for w in store.recompute_watermarks(**kwargs)})

    # with no since-filter the primary key IS the right access path, and the api_id order stays
    unfiltered, _ = store.watermarks_sql()
    assert unfiltered.endswith("ORDER BY api_id")
    assert [w.api_id for w in store.watermarks()] == sorted(w.api_id for w in store.watermarks())


# -- rollup_key_day: the transposed key axis ----------------------------------------------------


def _statements(store: Store, run) -> list[str]:
    seen: list[str] = []
    store.conn().set_trace_callback(seen.append)
    try:
        run()
    finally:
        store.conn().set_trace_callback(None)
    return seen


def test_the_two_map_axes_read_rollup_key_day_unless_the_query_is_api_scoped():
    """Pre-ruled after the full-set bench: a key-axis group used to be derived by `json_each`-ing
    EVERY cell rollup row in the window (170k rows -> a quarter-million tuples at 2M runs, 0.53s
    of the 0.62s `group_by=http_status` took). `rollup_key_day` holds the same fold transposed, so
    the window is (days x keys) rows.

    A query scoped to a SET OF APIs cannot use it -- a key row is already summed across every API
    that touched the key, and there is no api column left to filter -- so those keep the exact
    `json_each` arm. Same numbers either way; that is what the second half asserts."""
    store = _store_with_fixtures()
    apis = _load_fixture_apis()

    def aggregate_with(q):
        return store.aggregate_rollups(q, flaky_min=2, apis=apis)

    for group_by in ("failed_rule", "http_status"):
        plain = AggregateQuery(group_by=group_by, limit=50)
        sql = " ".join(_statements(store, lambda q=plain: aggregate_with(q)))
        assert "rollup_key_day" in sql, group_by
        assert "json_each" not in sql, group_by

        scoped = AggregateQuery(group_by=group_by, limit=50, scope_api_ids=["api-004", "api-007"])
        scoped_sql = " ".join(_statements(store, lambda q=scoped: aggregate_with(q)))
        assert "json_each" in scoped_sql, group_by          # falls back, exactly and knowingly

        # the two paths agree on the keys they share -- the scope only removes APIs
        fast = {g.key: g for g in aggregate_with(plain)}
        narrow = {g.key: g for g in aggregate_with(scoped)}
        assert set(narrow) <= set(fast), group_by
        for key, group in narrow.items():
            assert group.count <= fast[key].count, key

    # the cell axes are untouched by any of this
    cell_sql = " ".join(_statements(
        store, lambda: aggregate_with(AggregateQuery(group_by="api", limit=50))))
    assert "rollup_key_day" not in cell_sql


def test_api_count_is_the_windowed_union_not_a_per_day_sum():
    """`api_count` promises the TRUE distinct API count behind a key. Summing each day's count
    would double-count an API that appears on several days, so the union is taken over the stored
    `api_ids` for the returned keys only."""
    store = _store_with_fixtures()
    apis = _load_fixture_apis()
    runs = _load_fixture_runs()
    groups = store.aggregate_rollups(
        AggregateQuery(group_by="failed_rule", limit=50), flaky_min=2, apis=apis)
    assert groups
    for group in groups:
        expected = {r.api_id for r in runs.values() if group.key in _keys(r, "failed_rule")}
        assert group.api_count == len(expected), group.key


def test_rollup_key_day_is_backfilled_on_a_store_written_before_it_existed(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` leaves a NEW table empty on an existing file, and an empty
    `rollup_key_day` would make the key axes answer "no groups" -- silently wrong, not merely
    slow. So `init_schema` derives it once from the JSON maps `rollup_day` already carries."""
    path = tmp_path / "old.sqlite"
    store = Store(path)
    store.load_fixtures(FIXTURES, briefing_tz="Asia/Seoul")
    at_ingest = [dict(r) for r in store.conn().execute(
        'SELECT * FROM rollup_key_day ORDER BY axis, day, "key"').fetchall()]
    assert at_ingest
    store.conn().execute("DELETE FROM rollup_key_day")      # a store written before the table
    store.conn().commit()
    store.conn().close()

    reopened = Store(path)                                  # init_schema backfills
    after = [dict(r) for r in reopened.conn().execute(
        'SELECT * FROM rollup_key_day ORDER BY axis, day, "key"').fetchall()]
    for row in after:
        row["api_ids"] = json.dumps(sorted(json.loads(row["api_ids"])), ensure_ascii=False)
    assert after == at_ingest


def test_count_runs_by_api_id_query_plan_uses_an_index():
    # M19: EXPLAIN the REAL statement (`Store.count_runs_sql`), not a hand-copied string -- the
    # copy stayed green through every predicate change and proved nothing about the shipped query.
    store = _store_with_fixtures()
    sql, params = store.count_runs_sql(api_id="api-001")
    plan = store.conn().execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    _assert_no_unindexed_scan(plan, "runs")
    detail = " | ".join(row["detail"] for row in plan)
    assert "INDEX" in detail


# -- SLO smoke: every fixture-scale read completes well under 10ms ------------------------------


def test_slo_smoke_fixture_reads_are_fast():
    store = _store_with_fixtures()
    checks = [
        lambda: store.search_apis(query="v1", limit=20),
        lambda: store.list_runs(RunsQuery(limit=50)),
        lambda: store.count_runs(status="fail"),
        lambda: store.fetch_runs(api_id="api-001"),
        lambda: store.current_state(),
        lambda: store.watermarks(),
        lambda: store.operator_scope_ids("minseong", 30, datetime(2026, 9, 3, 14, tzinfo=KST)),
        lambda: store.runs_by_ids(["run-0001", "run-0002"]),
        lambda: store.get_body("run-0001"),
        lambda: store.audit(limit=50),
    ]
    for check in checks:
        start = time.perf_counter()
        check()
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 10, f"{check} took {elapsed_ms:.2f}ms"
