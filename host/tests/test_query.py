"""``Store.query`` -- the self-growth query engine (spec 2026-09-07 §3/§4), held to a PYTHON
ORACLE.

The engine's whole point is that one ``QuerySpec`` can land on any of four sources
(``rollup_day`` / ``rollup_key_day`` / ``rollup_operator_day`` / ``runs``) and must answer the
same numbers on all of them. So the tests here never assert a hand-written figure: ``_oracle``
re-derives ``runs/pass/fail/error/non_pass/fail_rate/apis`` from ``Store.fetch_runs()`` in plain
python, implementing spec §3 and nothing else, and every spec in ``SPECS`` is compared against it
on BOTH the tiny fixture store and a 2,000-run synthetic one whose paths, methods, groups,
operators, environments and data labels are varied enough that every source is actually reached.

``transitions`` and ``p95_duration_ms`` are the two measures the oracle cannot produce -- they are
defined by the ROLLUP (a materialization-time counter, a max-merged level), not by a run scan --
so they get their own test against those definitions, and are asserted ``None`` wherever the
chosen source does not carry them.
"""
from __future__ import annotations

import json
import math
import random
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from atworks_agent import ApiSpec, QueryFilters, QuerySpec, RunResult, RunStatus
from atworks_agent.aggregation import _keys as _group_keys
from atworks_host.query_sql import (
    align_days,
    compile_query,
    day_bounds,
    path_segments,
    resolve_window,
    select_source,
    week_key,
)
from atworks_host.store import Store

KST = timezone(timedelta(hours=9))
TZ = "Asia/Seoul"
FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"
NOW = datetime(2026, 9, 4, 9, 30, tzinfo=KST)
DEFAULT_DAYS = 30

MEASURES = ("runs", "pass", "fail", "error", "non_pass", "fail_rate", "apis")
#: measures every source can produce -- the oracle's own vocabulary
ORACLE_MEASURES = set(MEASURES)


# -- the store under test ------------------------------------------------------------------------


def _fixture_store() -> Store:
    store = Store(":memory:")
    store.load_fixtures(FIXTURES, TZ)
    return store


_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_PATHS = (
    "/v1/payments/approve", "/v1/payments/cancel", "/v1/payments/{id}", "/v1/payments",
    "/v1/users/{id}/status", "/v1/users/{id}", "/v1/users", "/v2/users/{id}",
    "/v1/contracts/{id}", "/v1/contracts", "/v2/contracts/{id}/lines", "/internal/health",
    "/v1/settlements/daily", "/v2/settlements", "/v1", "/v2/orders/{id}/items",
)
_GROUPS = ("payment", "user", "contract", "settlement", None)
_OPERATORS = ("minseong", "jiwon", "sora", "jihoon", "taeho", None)
_ENVS = ("dev", "stg", "legacy")
_LABELS = ("S1", "S2", None)
_RULES = ("amount >= 0", "status in [OK]", "refundAmount >= 0", "userId required",
          "settledAt format")


def _synthetic() -> tuple[Store, list[ApiSpec], list[RunResult]]:
    """40 APIs x 2,000 runs over 30 local days, seeded -- built THROUGH ``Store.ingest`` so every
    materialized table is the real one the engine reads, not a fixture written by hand."""
    rng = random.Random(20260907)
    apis = [
        ApiSpec(
            api_id=f"api-{i:03d}", method=_PATHS[i % len(_PATHS)] and _METHODS[i % len(_METHODS)],
            path=_PATHS[i % len(_PATHS)], name=f"api {i}", group=_GROUPS[i % len(_GROUPS)],
            updated_at=datetime(2026, 8, 1, tzinfo=UTC), has_rules=bool(i % 2),
            params=["amount"],
        )
        for i in range(40)
    ]
    base = datetime(2026, 8, 6, 0, 0, tzinfo=KST)
    runs: list[RunResult] = []
    for i in range(2000):
        status = rng.choices([RunStatus.PASS, RunStatus.FAIL, RunStatus.ERROR], weights=[6, 3, 1])[0]
        moment = base + timedelta(minutes=i * 21, seconds=rng.randint(0, 59))
        rules: list[str] = []
        if status is RunStatus.FAIL:
            rules = rng.sample(_RULES, rng.randint(1, 2))
        elif status is RunStatus.ERROR and rng.random() < 0.3:
            rules = [rng.choice(_RULES)]
        runs.append(RunResult(
            run_id=f"run-{i:05d}", api_id=f"api-{rng.randrange(40):03d}", executed_at=moment,
            target_env=rng.choice(_ENVS), test_data_label=rng.choice(_LABELS), status=status,
            failed_rules=rules,
            http_status=rng.choice([200, 200, 400, 500]) if status is not RunStatus.ERROR
            else rng.choice([503, None]),
            duration_ms=rng.randint(30, 900), executed_by=rng.choice(_OPERATORS),
            job_id=f"job-{i % 7:04d}",
        ))
    store = Store(":memory:")
    store.load_apis(apis)
    for start in range(0, len(runs), 137):
        store.ingest(runs[start:start + 137], TZ)
    return store, apis, runs


@pytest.fixture(scope="module")
def synthetic() -> Store:
    store, _apis, _runs = _synthetic()
    return store


@pytest.fixture(scope="module")
def synthetic_data(synthetic) -> tuple[list, dict]:
    """The store's own runs and catalogue, read ONCE for the whole module -- the oracle needs
    them for every one of the ~140 parametrized specs, and re-materializing 2,000 ``RunResult``
    objects per case is the difference between a 90-second module and a 10-second one."""
    return synthetic.fetch_runs(), {a.api_id: a for a in synthetic.search_apis(limit=200).items}


@pytest.fixture(scope="module")
def fixture_store() -> Store:
    return _fixture_store()


@pytest.fixture(scope="module")
def fixture_data(fixture_store) -> tuple[list, dict]:
    return (fixture_store.fetch_runs(),
            {a.api_id: a for a in fixture_store.search_apis(limit=200).items})


# -- the oracle ----------------------------------------------------------------------------------


def _run_day(run: RunResult) -> str:
    return run.day or run.executed_at.astimezone(KST).date().isoformat()


def _keys_for(run: RunResult, dimension: str, apis: dict[str, ApiSpec]) -> list[str | None]:
    """The group key(s) one run contributes on one dimension -- spec §3, and for the two key axes
    exactly ``aggregation._keys`` (a run can carry SEVERAL failed_rule keys, and a pass run
    carries none)."""
    api = apis.get(run.api_id)
    if dimension == "api":
        return [run.api_id]
    if dimension in ("path_segment_1", "path_segment_2", "path_segment_3", "path_prefix_2"):
        index = {"path_segment_1": 0, "path_segment_2": 1, "path_segment_3": 2,
                 "path_prefix_2": 3}[dimension]
        return [path_segments(api.path)[index] if api else None]
    if dimension == "method":
        return [api.method if api else None]
    if dimension == "api_group":
        return [api.group if api else None]
    if dimension == "target_env":
        return [run.target_env]
    if dimension == "test_data_label":
        return [run.test_data_label]
    if dimension == "executed_by":
        # an unattributed run belongs to no operator -- it produces no key at all, exactly as a
        # pass run produces no failed_rule key
        return [run.executed_by] if run.executed_by else []
    if dimension == "day":
        return [_run_day(run)]
    if dimension == "week":
        return [week_key(_run_day(run))]
    if dimension in ("failed_rule", "http_status"):
        return list(_group_keys(run, dimension))
    raise AssertionError(dimension)


def _matches_filters(run: RunResult, spec: QuerySpec, apis: dict[str, ApiSpec],
                     scope: set[str] | None) -> bool:
    f = spec.filters
    api = apis.get(run.api_id)
    status = None if f.status in (None, "all") else f.status
    if status == "non_pass" and run.status is RunStatus.PASS:
        return False
    if status in ("pass", "fail", "error") and run.status.value != status:
        return False
    if f.api_ids and run.api_id not in f.api_ids:
        return False
    if f.target_env and run.target_env not in f.target_env:
        return False
    if f.test_data_label and (run.test_data_label or "") not in [v or "" for v in f.test_data_label]:
        return False
    if f.executed_by and run.executed_by not in f.executed_by:
        return False
    if f.path_contains and not any(n.lower() in (api.path.lower() if api else "")
                                   for n in f.path_contains):
        return False
    if f.path_prefix is not None:
        path = api.path if api else ""
        if not (path == f.path_prefix or path.startswith(f.path_prefix + "/")):
            return False
    if f.method and (api.method if api else None) not in f.method:
        return False
    if f.api_group and (api.group if api else None) not in f.api_group:
        return False
    if scope is not None and run.api_id not in scope:
        return False
    # a key filter on an axis this query does NOT group by is a RUN filter; on an axis it DOES
    # group by it restricts the keys instead (handled in `_oracle`)
    for axis in ("failed_rule", "http_status"):
        wanted = getattr(f, axis)
        if (wanted and axis not in spec.dimensions
                and not ({str(v) for v in wanted} & set(_group_keys(run, axis)))):
            return False
    return True


def _scope_apis(runs: list[RunResult], operator: str, since: datetime) -> set[str]:
    """``operator_api`` recomputed: the APIs whose LAST run by this operator (all time) is at or
    after the window start -- the same predicate ``_scope_operator_clause`` writes in SQL."""
    last: dict[str, datetime] = {}
    for run in runs:
        if run.executed_by == operator:
            key = run.api_id
            if key not in last or run.executed_at > last[key]:
                last[key] = run.executed_at
    return {api_id for api_id, moment in last.items() if moment >= since}


def _round4(value: float) -> float:
    """SQLite's ``ROUND`` is half-AWAY-FROM-ZERO; python's ``round`` is half-to-even, so
    ``round(0.53125, 4)`` is 0.5312 while ``ROUND(0.53125, 4)`` is 0.5313. The oracle has to mean
    what the engine means, so it rounds SQLite's way."""
    return math.floor(value * 10000 + 0.5) / 10000


def _sort_key(value) -> tuple:
    """SQLite's ASC ordering for a possibly-NULL value: NULLs first, then the value."""
    return (0, "") if value is None else (1, value)


def _oracle(spec: QuerySpec, runs: list[RunResult], apis: dict[str, ApiSpec], now: datetime,
            tz: str, previous: bool = False, apply_limit: bool = True) -> list[dict]:
    """Spec §3 in plain python over every run in the store. Returns the rows the engine must
    return: ``{"keys": (...), "measures": {...}}``, already ordered and cut.

    ``apply_limit=False`` returns EVERY group instead -- what ``total_groups`` counts and what
    ``population`` sums, neither of which the limit may touch."""
    since, until = resolve_window(spec, now, DEFAULT_DAYS)
    day_from, day_to = align_days(since, until, tz)
    if previous:
        from atworks_host.query_sql import shift_days
        day_from, day_to = shift_days(day_from, day_to)
    window_start, _ = day_bounds(day_from, day_to, tz)
    scope = (_scope_apis(runs, spec.filters.scope_operator, window_start)
             if spec.filters.scope_operator else None)

    buckets: dict[tuple, list[RunResult]] = {}
    for run in runs:
        if not (day_from <= _run_day(run) <= day_to):
            continue
        if not _matches_filters(run, spec, apis, scope):
            continue
        per_dimension = []
        for dimension in spec.dimensions:
            values = _keys_for(run, dimension, apis)
            wanted = getattr(spec.filters, dimension, None) if dimension in (
                "failed_rule", "http_status") else None
            if wanted:
                values = [v for v in values if v in {str(w) for w in wanted}]
            per_dimension.append(values)
        combos: list[tuple] = [()]
        for values in per_dimension:
            combos = [(*combo, value) for combo in combos for value in values]
        for combo in combos:
            buckets.setdefault(combo, []).append(run)

    rows = []
    for key, members in buckets.items():
        total = len(members)
        passed = sum(1 for r in members if r.status is RunStatus.PASS)
        failed = sum(1 for r in members if r.status is RunStatus.FAIL)
        errored = sum(1 for r in members if r.status is RunStatus.ERROR)
        rows.append({"keys": key, "measures": {
            "runs": total, "pass": passed, "fail": failed, "error": errored,
            "non_pass": failed + errored,
            "fail_rate": _round4((failed + errored) / total) if total else None,
            "apis": len({r.api_id for r in members}),
        }})

    key_order = lambda row: tuple(_sort_key(v) for v in row["keys"])   # noqa: E731
    if spec.order_by == "key":
        rows.sort(key=key_order)
    else:
        measure = spec.order_by

        def rank(row):
            value = row["measures"][measure]
            if spec.descending:
                head = (1, 0) if value is None else (0, -value)
            else:
                head = (0, 0) if value is None else (1, value)
            return (head, key_order(row))

        rows.sort(key=rank)
    return rows[:spec.limit] if apply_limit else rows


def _assert_matches_oracle(store: Store, spec: QuerySpec, runs: list[RunResult],
                           apis: dict[str, ApiSpec], *, label: str = "") -> None:
    result = store.query(spec, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    expected = _oracle(spec, runs, apis, NOW, TZ)
    assert result.source == select_source(spec), label
    assert [tuple(r.keys[d] for d in spec.dimensions) for r in result.rows] == \
        [row["keys"] for row in expected], f"{label}: keys/order"
    for got, want in zip(result.rows, expected, strict=True):
        for measure in spec.measures:
            if measure not in ORACLE_MEASURES:
                continue
            if measure == "apis" and result.source == "rollup_operator_day":
                # the operator-day rollup has no api column at all -- honestly NULL
                assert got.measures[measure] is None, label
                continue
            assert got.measures[measure] == want["measures"][measure], \
                f"{label}: {measure} at {want['keys']}"
    # `total_groups` is the group count BEFORE the limit and `population` the summed `runs`
    # measure over ALL of them -- so both are checked against the UNLIMITED oracle, on every case.
    # (Re-deriving them from a limit-50 oracle and skipping the assert when it filled up meant the
    # two figures went unverified on exactly the wide specs where they matter -- review minor #6.)
    full = _oracle(spec, runs, apis, NOW, TZ, apply_limit=False)
    assert result.total_groups == len(full), f"{label}: total_groups"
    assert result.population == sum(r["measures"]["runs"] for r in full), f"{label}: population"
    if spec.compare_previous_window:
        _assert_compare_matches_oracle(result, spec, runs, apis, label=label)


def _assert_compare_matches_oracle(result, spec: QuerySpec, runs: list[RunResult],
                                   apis: dict[str, ApiSpec], *, label: str) -> None:
    """``*_prev`` / ``*_delta`` against the oracle run over the SHIFTED window -- computed here
    from run rows alone, never by importing ``Store._join_previous_window``.

    The previous half carries the same ``limit`` and the same rank as the current one (the
    implementer's documented decision, accepted at review), so the oracle applies them too. A key
    the previous half did not return is a real 0 for a counter; a measure the chosen SOURCE cannot
    produce at all (``apis`` off ``rollup_operator_day``) is ``None`` on both sides -- the fix this
    assertion exists for, since the raw statement's ``NULL`` used to be read as 0."""
    before = {row["keys"]: row["measures"]
              for row in _oracle(spec, runs, apis, NOW, TZ, previous=True)}
    for got in result.rows:
        key = tuple(got.keys[d] for d in spec.dimensions)
        prior = before.get(key)
        for measure in spec.measures:
            if measure not in ORACLE_MEASURES:
                continue
            where = f"{label}: {measure} at {key}"
            if measure == "apis" and result.source == "rollup_operator_day":
                assert got.measures[measure] is None, where
                assert got.measures[f"{measure}_prev"] is None, where
                assert got.measures[f"{measure}_delta"] is None, where
                continue
            expected = 0 if prior is None else prior[measure]
            assert got.measures[f"{measure}_prev"] == pytest.approx(expected), where
            assert got.measures[f"{measure}_delta"] == pytest.approx(
                round(got.measures[measure] - expected, 4)), where


# -- the spec matrix -----------------------------------------------------------------------------

DIMENSIONS = ("api", "path_segment_1", "path_segment_2", "path_segment_3", "path_prefix_2",
              "method", "api_group", "target_env", "test_data_label", "failed_rule",
              "http_status", "executed_by", "day", "week")

PAIRS = (("path_segment_2", "target_env"), ("method", "http_status"), ("executed_by", "api"),
         ("day", "target_env"), ("failed_rule", "api_group"),
         # the third segment is where the demo catalogue puts the endpoint family
         # (`/v1/product/history/001796`), and several fixture paths have no third segment at
         # all -- so this pair also pins the NULL key against the oracle.
         ("path_segment_3", "failed_rule"))

FILTER_CASES = (
    ("status_pass", QueryFilters(status="pass")),
    ("status_fail", QueryFilters(status="fail")),
    ("status_error", QueryFilters(status="error")),
    ("status_non_pass", QueryFilters(status="non_pass")),
    ("status_all", QueryFilters(status="all")),
    ("path_contains", QueryFilters(path_contains=["payments", "users"])),
    ("path_prefix", QueryFilters(path_prefix="/v1/payments")),
    ("method", QueryFilters(method=["GET", "POST"])),
    ("api_group", QueryFilters(api_group=["payment", "user"])),
    ("executed_by", QueryFilters(executed_by=["minseong", "jiwon"])),
    ("scope_operator", QueryFilters(scope_operator="minseong")),
    ("api_ids", QueryFilters(api_ids=["api-001", "api-002", "api-003"])),
    ("target_env", QueryFilters(target_env=["dev"])),
    # "" is the unbound-label sentinel every source normalizes a NULL label to
    ("test_data_label", QueryFilters(test_data_label=["S1", ""])),
    ("window_days", QueryFilters(window_days=7)),
    ("since_until", QueryFilters(since=datetime(2026, 8, 20, tzinfo=KST),
                                 until=datetime(2026, 8, 27, tzinfo=KST))),
    ("failed_rule_filter", QueryFilters(failed_rule=["amount >= 0"])),
    ("http_status_filter", QueryFilters(http_status=[500, 503])),
)


def _specs() -> list[tuple[str, QuerySpec]]:
    measures = ["runs", "pass", "fail", "non_pass", "fail_rate"]
    cases: list[tuple[str, QuerySpec]] = [("no-dimension", QuerySpec(measures=measures, order_by="runs"))]
    for dimension in DIMENSIONS:
        cases.append((f"dim:{dimension}",
                      QuerySpec(dimensions=[dimension], measures=measures, order_by="runs")))
        cases.append((f"dim:{dimension}+apis",
                      QuerySpec(dimensions=[dimension], measures=["runs", "apis"], order_by="runs")))
    for pair in PAIRS:
        cases.append((f"pair:{'+'.join(pair)}",
                      QuerySpec(dimensions=list(pair), measures=measures, order_by="non_pass")))
    for name, filters in FILTER_CASES:
        # fail_rate is refused together with a status filter (QuerySpec validator: the ratio over
        # a status-filtered population is 1.0 or 0.0 on every row), so those cases keep the counters.
        status = filters.get("status") if isinstance(filters, dict) else getattr(filters, "status", None)
        filtered_measures = (["runs", "fail", "error", "apis"] if status not in (None, "all")
                             else ["runs", "fail", "error", "fail_rate", "apis"])
        for dimension in ("api", "path_segment_2", "failed_rule", "executed_by"):
            cases.append((f"filter:{name}/{dimension}", QuerySpec(
                dimensions=[dimension], filters=filters, measures=filtered_measures, order_by="runs")))
    # The third segment under a filter that itself needs the `apis` join (`path_prefix`): both the
    # grouping key and the predicate resolve through the same joined catalogue row, which is the
    # one way this dimension can go wrong that a bare `dim:` case would not show.
    cases.append(("filter:path_prefix/path_segment_3", QuerySpec(
        dimensions=["path_segment_3"], filters=QueryFilters(path_prefix="/v1/payments"),
        measures=["runs", "fail", "error", "fail_rate", "apis"], order_by="runs")))
    # `compare_previous_window` on ONE spec per source: the prev/delta join is a different code
    # path per arm (a python `apis` fill on the key rollup, a NULL `apis` column on the operator
    # rollup, a real COUNT(DISTINCT) on the other two), and it was the arm-specific halves that
    # were wrong.
    for dimensions, order in ((["failed_rule"], "runs"), (["executed_by"], "runs"),
                              (["executed_by", "api"], "runs"),
                              (["path_segment_2", "target_env"], "non_pass")):
        cases.append((f"compare:{'+'.join(dimensions)}", QuerySpec(
            dimensions=dimensions, measures=["runs", "non_pass", "fail_rate", "apis"],
            order_by=order, limit=15, compare_previous_window=True,
            filters=QueryFilters(window_days=7))))
    for order in ("runs", "pass", "fail", "error", "non_pass", "fail_rate", "apis", "key"):
        for descending in (True, False):
            cases.append((f"order:{order}/{descending}", QuerySpec(
                dimensions=["api"], measures=["runs", "pass", "fail", "error", "non_pass"]
                if order in ("runs", "pass", "fail", "error", "non_pass") else ["runs", order]
                if order != "key" else ["runs"],
                order_by=order, descending=descending, limit=7)))
    return cases


SPECS = _specs()


@pytest.mark.parametrize("label,spec", SPECS, ids=[label for label, _ in SPECS])
def test_query_matches_the_oracle_on_the_fixture_store(label, spec, fixture_store, fixture_data):
    runs, apis = fixture_data
    _assert_matches_oracle(fixture_store, spec, runs, apis, label=f"fixtures/{label}")


@pytest.mark.parametrize("label,spec", SPECS, ids=[label for label, _ in SPECS])
def test_query_matches_the_oracle_on_a_synthetic_store(label, spec, synthetic, synthetic_data):
    runs, apis = synthetic_data
    _assert_matches_oracle(synthetic, spec, runs, apis, label=f"synthetic/{label}")


def test_every_source_is_actually_reached_by_the_matrix():
    """A golden suite that never leaves ``rollup_day`` would prove nothing about the other three
    arms, and source selection is the design."""
    reached = {select_source(spec) for _label, spec in SPECS}
    assert reached == {"rollup_day", "rollup_key_day", "rollup_operator_day", "runs"}


# -- source selection (spec §4's table) ----------------------------------------------------------


@pytest.mark.parametrize("spec,expected", [
    (QuerySpec(measures=["runs"]), "rollup_day"),
    (QuerySpec(dimensions=["api"], measures=["runs"]), "rollup_day"),
    (QuerySpec(dimensions=["path_segment_2"], measures=["runs"]), "rollup_day"),
    (QuerySpec(dimensions=["day", "week"], measures=["runs"]), "rollup_day"),
    (QuerySpec(dimensions=["failed_rule"], measures=["runs"]), "rollup_key_day"),
    (QuerySpec(dimensions=["http_status"], measures=["runs"]), "rollup_key_day"),
    # ...but any api-scoped filter takes the key axis back to the exact json_each arm
    (QuerySpec(dimensions=["failed_rule"], measures=["runs"],
               filters=QueryFilters(api_group=["payment"])), "rollup_day"),
    (QuerySpec(dimensions=["http_status"], measures=["runs"],
               filters=QueryFilters(scope_operator="minseong")), "rollup_day"),
    (QuerySpec(dimensions=["http_status"], measures=["runs"],
               filters=QueryFilters(target_env=["dev"])), "rollup_day"),
    (QuerySpec(dimensions=["failed_rule", "api_group"], measures=["runs"]), "rollup_day"),
    # a status-filtered COUNT(DISTINCT api) cannot come off the key rollup's api_ids set
    (QuerySpec(dimensions=["failed_rule"], measures=["runs", "apis"],
               filters=QueryFilters(status="error")), "rollup_day"),
    (QuerySpec(dimensions=["executed_by"], measures=["runs"]), "rollup_operator_day"),
    (QuerySpec(dimensions=["executed_by", "day"], measures=["runs"]), "rollup_operator_day"),
    (QuerySpec(dimensions=["executed_by", "week"], measures=["runs"]), "rollup_operator_day"),
    (QuerySpec(dimensions=["executed_by", "api"], measures=["runs"]), "runs"),
    (QuerySpec(dimensions=["executed_by"], measures=["runs"],
               filters=QueryFilters(api_group=["payment"])), "runs"),
    (QuerySpec(dimensions=["executed_by"], measures=["runs"],
               filters=QueryFilters(target_env=["dev"])), "runs"),
    # a filter on the executor forces the run table whatever is grouped -- no rollup carries it
    (QuerySpec(dimensions=["api"], measures=["runs"],
               filters=QueryFilters(executed_by=["minseong"])), "runs"),
    (QuerySpec(measures=["runs"], filters=QueryFilters(executed_by=["minseong"])), "runs"),
    (QuerySpec(dimensions=["executed_by", "api"], measures=["runs"],
               filters=QueryFilters(executed_by=["minseong"])), "runs"),
    # ...except ON the operator axis itself, where the filter IS `operator_id IN (...)` on the
    # key column the query already groups by (fix round 1's refinement of the §4 table)
    (QuerySpec(dimensions=["executed_by"], measures=["runs"],
               filters=QueryFilters(executed_by=["minseong"])), "rollup_operator_day"),
    (QuerySpec(dimensions=["executed_by", "day"], measures=["runs"],
               filters=QueryFilters(executed_by=["minseong", "jiwon"])), "rollup_operator_day"),
    # ...and an api-scoped filter alongside it still has no column there
    (QuerySpec(dimensions=["executed_by"], measures=["runs"],
               filters=QueryFilters(executed_by=["minseong"], api_group=["payment"])), "runs"),
    # a key filter on an axis this query does not group by is not derivable from either map
    (QuerySpec(dimensions=["api"], measures=["runs"],
               filters=QueryFilters(http_status=[500])), "runs"),
    (QuerySpec(dimensions=["failed_rule"], measures=["runs"],
               filters=QueryFilters(http_status=[500])), "runs"),
    # ...and the cross product of the two maps is not derivable from them either
    (QuerySpec(dimensions=["failed_rule", "http_status"], measures=["runs"]), "runs"),
])
def test_source_selection_follows_the_spec_table(spec, expected):
    assert select_source(spec) == expected


# -- window / week / path semantics ---------------------------------------------------------------


def test_path_segments():
    # (segment 1, segment 2, segment 3, prefix of the first two) -- the `apis` column order
    assert path_segments("/v1/items/{id}") == ("v1", "items", "{id}", "/v1/items")
    assert path_segments("/v1/product/history/001796") == ("v1", "product", "history", "/v1/product")
    assert path_segments("/v1/items") == ("v1", "items", None, "/v1/items")
    assert path_segments("/v1") == ("v1", None, None, "/v1")
    assert path_segments("/") == (None, None, None, None)
    assert path_segments("") == (None, None, None, None)
    # segments are kept VERBATIM -- {id} and a numeric segment are not relabelled
    assert path_segments("/v1/{id}/42") == ("v1", "{id}", "42", "/v1/{id}")


def test_week_sql_equals_python_isocalendar(synthetic):
    """The ISO week is computed twice -- once in python (``week_key``) for the oracle and the
    sample fill, once in SQLite (``week_sql``) for the GROUP BY. They must be the same function,
    including across a year boundary (2026-12-28 is 2026-W53; 2027-01-01 is still 2026-W53)."""
    conn = synthetic.conn()
    day = datetime(2024, 1, 1).date()
    days = [(day + timedelta(days=i)).isoformat() for i in range(1500)]
    from atworks_host.query_sql import week_sql
    for start in range(0, len(days), 400):
        chunk = days[start:start + 400]
        rows = conn.execute(
            f"SELECT value, {week_sql('value')} FROM (SELECT column1 AS value FROM "
            f"(VALUES {','.join('(?)' for _ in chunk)}))", chunk).fetchall()
        assert len(rows) == len(chunk)
        for value, computed in rows:
            assert computed == week_key(value), value
    assert week_key("2026-12-28") == "2026-W53"
    assert week_key("2027-01-01") == "2026-W53"


def test_window_alignment_is_whole_local_days_and_the_previous_window_does_not_overlap():
    spec = QuerySpec(measures=["runs"], filters=QueryFilters(window_days=7))
    since, until = resolve_window(spec, NOW, DEFAULT_DAYS)
    day_from, day_to = align_days(since, until, TZ)
    assert (datetime.fromisoformat(day_to) - datetime.fromisoformat(day_from)).days == 6
    assert day_to == NOW.date().isoformat()      # the window ends with today, inclusive
    from atworks_host.query_sql import shift_days
    prev_from, prev_to = shift_days(day_from, day_to)
    assert prev_to < day_from                    # no shared day
    assert (datetime.fromisoformat(prev_to) - datetime.fromisoformat(prev_from)).days == 6

    # an explicit midnight..midnight window keeps exactly the days it names
    explicit = QuerySpec(measures=["runs"], filters=QueryFilters(
        since=datetime(2026, 9, 1, tzinfo=KST), until=datetime(2026, 9, 5, tzinfo=KST)))
    assert align_days(*resolve_window(explicit, NOW, DEFAULT_DAYS), TZ) == ("2026-09-01", "2026-09-04")


HOT_DAYS = 180


def test_resolve_window_clamps_since_to_the_hot_partition():
    """`window_days`는 이미 `le=180`이라 이 선을 넘을 수 없다 — 명시적 `since`만 넘는다.
    클램프하지 않으면 롤업(영구)과 `runs` arm(보존이 archive로 옮긴다)이 같은 질문에 다른 답을
    낸다. `until`은 건드리지 않는다: 미래를 자르는 규칙은 여기 없다."""
    ancient = QuerySpec(measures=["runs"], filters=QueryFilters(
        since=NOW - timedelta(days=4 * 365), until=NOW))
    since, until = resolve_window(ancient, NOW, DEFAULT_DAYS, HOT_DAYS)
    assert since == NOW - timedelta(days=HOT_DAYS) and until == NOW
    # 이미 지평선 안이면 그대로 둔다.
    recent = QuerySpec(measures=["runs"], filters=QueryFilters(since=NOW - timedelta(days=3)))
    assert resolve_window(recent, NOW, DEFAULT_DAYS, HOT_DAYS)[0] == NOW - timedelta(days=3)
    # 클램프를 끄면(hot_days=None) 옛 동작 그대로 — 오라클 300케이스가 이 경로를 쓴다.
    assert resolve_window(ancient, NOW, DEFAULT_DAYS)[0] == NOW - timedelta(days=4 * 365)


@pytest.mark.parametrize("label,dimensions,filters_extra", [
    ("rollup_day", ["api"], {}),
    ("rollup_key_day", ["failed_rule"], {}),
    ("runs", ["api"], {"executed_by": ["minseong"]}),
])
def test_a_window_reaching_past_the_hot_horizon_answers_the_clamped_window_on_every_arm(
        synthetic, label, dimensions, filters_extra):
    """4년 전부터 물어도 답은 hot 파티션(180일)까지다 — **모든 arm에서 같은 창으로**. 이 클램프가
    없으면 롤업 arm은 4년을 답하고 run arm과 증거 표본은 180일을 답한다: 같은 질문, 소스에 따라
    다른 답. `QueryResult.window`는 클램프된 쌍을 보고하므로 답이 자기가 덮은 창을 말한다."""
    ancient = QuerySpec(dimensions=dimensions, measures=["runs", "non_pass"], limit=20,
                        filters=QueryFilters(since=NOW - timedelta(days=4 * 365), **filters_extra))
    bounded = QuerySpec(dimensions=dimensions, measures=["runs", "non_pass"], limit=20,
                        filters=QueryFilters(since=NOW - timedelta(days=HOT_DAYS), **filters_extra))
    got = synthetic.query(ancient, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS,
                          hot_days=HOT_DAYS)
    want = synthetic.query(bounded, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS,
                           hot_days=HOT_DAYS)
    assert got.source == select_source(ancient) == label
    assert got.window == want.window
    assert got.window[0] == day_bounds(*align_days(NOW - timedelta(days=HOT_DAYS), NOW, TZ), TZ)[0]
    assert got.total_groups == want.total_groups and got.population == want.population
    assert [(r.keys, r.measures) for r in got.rows] == [(r.keys, r.measures) for r in want.rows]
    # 증거 표본은 이 창 안의 실제 run이다 — 클램프가 표본을 말려 죽이지 않는다.
    assert got.rows and any(r.run_ids for r in got.rows)


def test_an_empty_window_answers_nothing_rather_than_scanning(synthetic):
    spec = QuerySpec(measures=["runs"], dimensions=["api"], filters=QueryFilters(
        since=datetime(2026, 9, 1, 1, tzinfo=KST), until=datetime(2026, 9, 1, 2, tzinfo=KST)))
    result = synthetic.query(spec, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    assert result.rows == [] and result.total_groups == 0 and result.population == 0
    assert result.window[0] == result.window[1]


# -- transitions / p95: the two rollup-defined measures --------------------------------------------


def test_transitions_and_p95_follow_the_rollup_definitions(synthetic):
    """``transitions`` is the ingest-time per-cell counter SUMMED over the window's days, and
    ``p95_duration_ms`` the documented MAX-merge of the per-cell-day p95s. Both are asserted
    against ``rollup_day`` itself -- a run scan cannot produce either."""
    spec = QuerySpec(dimensions=["api"], measures=["runs", "transitions", "p95_duration_ms"],
                     order_by="runs", limit=50)
    result = synthetic.query(spec, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    day_from, day_to = align_days(*resolve_window(spec, NOW, DEFAULT_DAYS), TZ)
    expected = {
        row[0]: (row[1], row[2]) for row in synthetic.conn().execute(
            'SELECT api_id, SUM(transitions), MAX(p95_duration_ms) FROM rollup_day '
            "WHERE day >= ? AND day <= ? GROUP BY api_id", (day_from, day_to)).fetchall()
    }
    assert result.rows
    for row in result.rows:
        transitions, p95 = expected[row.keys["api"]]
        assert row.measures["transitions"] == transitions
        assert row.measures["p95_duration_ms"] == p95


def test_sources_without_a_measure_report_none_rather_than_zero(synthetic):
    """``rollup_operator_day`` carries neither an api column nor a duration; the key rollup and
    the run table carry no per-key transition counter. A 0 there would read as a measurement."""
    operator = synthetic.query(
        QuerySpec(dimensions=["executed_by"], measures=["runs", "apis", "p95_duration_ms",
                                                        "transitions"], order_by="runs"),
        now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    assert operator.source == "rollup_operator_day" and operator.rows
    for row in operator.rows:
        assert row.measures["apis"] is None
        assert row.measures["p95_duration_ms"] is None
        assert row.measures["transitions"] is None

    keyed = synthetic.query(
        QuerySpec(dimensions=["failed_rule"], measures=["runs", "transitions"], order_by="runs"),
        now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    assert keyed.source == "rollup_key_day" and keyed.rows
    assert all(row.measures["transitions"] is None for row in keyed.rows)


def test_the_runs_source_computes_a_real_p95(synthetic):
    """The rollups report a max-merged level because they never kept raw durations. The ``runs``
    arm HAS them, so it reports ``aggregation._p95``'s own definition -- checked against a python
    recompute of the same population."""
    spec = QuerySpec(dimensions=["executed_by", "api"],
                     measures=["runs", "p95_duration_ms"], order_by="runs", limit=20)
    result = synthetic.query(spec, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    assert result.source == "runs" and result.rows
    day_from, day_to = align_days(*resolve_window(spec, NOW, DEFAULT_DAYS), TZ)
    buckets: dict[tuple, list[int]] = {}
    for run in synthetic.fetch_runs():
        if day_from <= _run_day(run) <= day_to and run.duration_ms is not None:
            buckets.setdefault((run.executed_by, run.api_id), []).append(run.duration_ms)
    for row in result.rows:
        values = sorted(buckets[(row.keys["executed_by"], row.keys["api"])])
        assert row.measures["p95_duration_ms"] == values[max(0, math.ceil(0.95 * len(values)) - 1)]


# -- compare_previous_window ----------------------------------------------------------------------


def test_compare_previous_window_adds_prev_and_delta(synthetic):
    spec = QuerySpec(dimensions=["api"], measures=["runs", "non_pass", "fail_rate"],
                     filters=QueryFilters(window_days=7), order_by="runs",
                     compare_previous_window=True, limit=50)
    result = synthetic.query(spec, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    apis = {a.api_id: a for a in synthetic.search_apis(limit=200).items}
    runs = synthetic.fetch_runs()
    before = {row["keys"]: row["measures"]
              for row in _oracle(spec.model_copy(update={"limit": 50}), runs, apis, NOW, TZ,
                                 previous=True)}
    assert result.rows
    for row in result.rows:
        key = (row.keys["api"],)
        prior = before.get(key, {})
        for measure in ("runs", "non_pass", "fail_rate"):
            expected = prior.get(measure) or 0
            assert row.measures[f"{measure}_prev"] == pytest.approx(expected), (key, measure)
            assert row.measures[f"{measure}_delta"] == pytest.approx(
                round(row.measures[measure] - expected, 4)), (key, measure)


def test_compare_previous_window_reads_a_window_that_shares_no_day(synthetic):
    """Both halves must be the same length and disjoint, or a day would be counted twice."""
    spec = QuerySpec(measures=["runs"], filters=QueryFilters(window_days=10),
                     compare_previous_window=True, order_by="runs")
    current = compile_query(spec, now=NOW, tz=TZ, default_days=DEFAULT_DAYS)
    previous = compile_query(spec, now=NOW, tz=TZ, previous=True, default_days=DEFAULT_DAYS)
    assert previous.day_to < current.day_from
    assert (datetime.fromisoformat(current.day_to) - datetime.fromisoformat(current.day_from)) == \
        (datetime.fromisoformat(previous.day_to) - datetime.fromisoformat(previous.day_from))
    result = synthetic.query(spec, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    row = result.rows[0]
    assert row.measures["runs_prev"] > 0 and "runs_delta" in row.measures


def _mirrored_store() -> Store:
    """A store whose previous 7-day window is an exact copy of its current one: 4 APIs x
    (one pass, one fail, one error) on 2026-09-01 and again on 2026-08-25.

    Every ``*_prev`` must therefore equal its own measure and every ``*_delta`` must be 0 -- a
    statement about the compare column that does not restate the engine's arithmetic."""
    apis = [
        ApiSpec(api_id=f"api-{i:03d}", method="GET", path=f"/v1/items/{i}", name=f"api {i}",
                group="payment", updated_at=datetime(2026, 8, 1, tzinfo=UTC), has_rules=True,
                params=["amount"])
        for i in range(4)
    ]
    runs: list[RunResult] = []
    for days_back, tag in ((0, "cur"), (7, "prev")):
        base = datetime(2026, 9, 1, 10, 0, tzinfo=KST) - timedelta(days=days_back)
        for i in range(4):
            for j, status in enumerate((RunStatus.PASS, RunStatus.FAIL, RunStatus.ERROR)):
                runs.append(RunResult(
                    run_id=f"run-{tag}-{i}-{j}", api_id=f"api-{i:03d}",
                    executed_at=base + timedelta(minutes=j), target_env="dev",
                    test_data_label="S1", status=status,
                    failed_rules=["amount >= 0"] if status is RunStatus.FAIL else [],
                    http_status={0: 200, 1: 500}.get(j), duration_ms=100 + j,
                    executed_by="minseong", job_id="job-0001"))
    store = Store(":memory:")
    store.load_apis(apis)
    store.ingest(sorted(runs, key=lambda r: r.executed_at), TZ)   # ingest is order-dependent
    return store


def test_compare_on_the_key_axis_reports_a_real_previous_api_count():
    """The critical fix. ``rollup_key_day`` has no api column, so ``m_apis`` is a literal ``NULL``
    on that arm and the current window fills it in python from the rows' stored ``api_ids``. The
    join used to read the RAW previous statement, so ``apis_prev`` came back 0 and ``apis_delta``
    equalled the whole current count -- a fabricated "the previous week touched no API", on a
    store where the two windows are identical."""
    store = _mirrored_store()
    spec = QuerySpec(dimensions=["http_status"], measures=["runs", "apis"], order_by="runs",
                     filters=QueryFilters(window_days=7), compare_previous_window=True, limit=20)
    result = store.query(spec, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    assert result.source == "rollup_key_day" and result.rows
    for row in result.rows:
        assert row.measures["apis"] == 4, row.keys
        assert row.measures["apis_prev"] == row.measures["apis"], row.keys
        assert row.measures["apis_delta"] == 0, row.keys
        assert row.measures["runs_prev"] == row.measures["runs"], row.keys
        assert row.measures["runs_delta"] == 0, row.keys


def test_compare_on_the_operator_axis_reports_no_api_count_at_all(synthetic):
    """``rollup_operator_day`` never had an api column, and no fill can invent one. So ``apis``
    is ``None`` on BOTH sides and there is no delta -- not a 0, which would read as "these two
    windows touched the same APIs"."""
    spec = QuerySpec(dimensions=["executed_by"], measures=["runs", "apis"], order_by="runs",
                     filters=QueryFilters(window_days=7), compare_previous_window=True, limit=20)
    result = synthetic.query(spec, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    assert result.source == "rollup_operator_day" and result.rows
    for row in result.rows:
        assert row.measures["apis"] is None
        assert row.measures["apis_prev"] is None
        assert row.measures["apis_delta"] is None
        assert row.measures["runs_prev"] > 0 and row.measures["runs_delta"] is not None


def test_compare_on_the_runs_arm_reports_real_values(synthetic):
    """The arm that CAN count APIs still does, on both sides -- the None-guard must not have
    swallowed a measure the source actually measures."""
    spec = QuerySpec(dimensions=["executed_by", "api"], measures=["runs", "apis"],
                     order_by="runs", filters=QueryFilters(window_days=7),
                     compare_previous_window=True, limit=20)
    result = synthetic.query(spec, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    assert result.source == "runs" and result.rows
    assert any(row.measures["apis_prev"] for row in result.rows)
    for row in result.rows:
        # one row is one (operator, api) pair, so the api count is 1 here and 0 or 1 before
        assert row.measures["apis"] == 1
        assert row.measures["apis_prev"] in (0, 1)
        assert row.measures["apis_delta"] == 1 - row.measures["apis_prev"]


# -- scope_operator's threshold --------------------------------------------------------------------


def test_scope_operator_threshold_is_utc_not_a_local_wall_clock():
    """``operator_api.last_executed_at`` is written by ``Store._iso`` in UTC; the window start is
    a LOCAL midnight. Formatting that local wall clock and gluing a "Z" on put the threshold nine
    hours late in Asia/Seoul, so every API whose last run by the operator fell in the window's
    first nine hours silently dropped out of scope.

    ``api-000``'s only run by ``minseong`` is at 03:00 KST on the window's FIRST day -- inside the
    window by any honest reading, and excluded by the bug."""
    apis = [
        ApiSpec(api_id=f"api-{i:03d}", method="GET", path=f"/v1/items/{i}", name=f"api {i}",
                group="payment", updated_at=datetime(2026, 8, 1, tzinfo=UTC), has_rules=True,
                params=["amount"])
        for i in range(2)
    ]
    runs = [
        RunResult(run_id="run-0", api_id="api-000",
                  executed_at=datetime(2026, 8, 29, 3, 0, tzinfo=KST), target_env="dev",
                  test_data_label="S1", status=RunStatus.FAIL, failed_rules=["amount >= 0"],
                  http_status=500, duration_ms=120, executed_by="minseong", job_id="job-0001"),
        RunResult(run_id="run-1", api_id="api-001",
                  executed_at=datetime(2026, 9, 1, 12, 0, tzinfo=KST), target_env="dev",
                  test_data_label="S1", status=RunStatus.PASS, failed_rules=[],
                  http_status=200, duration_ms=90, executed_by="jiwon", job_id="job-0002"),
    ]
    store = Store(":memory:")
    store.load_apis(apis)
    store.ingest(runs, TZ)

    spec = QuerySpec(dimensions=["api"], measures=["runs"], order_by="runs",
                     filters=QueryFilters(window_days=7, scope_operator="minseong"))
    result = store.query(spec, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    # the window opens at 2026-08-29 00:00 KST == 2026-08-28 15:00Z, and the run is 18:00Z
    assert compile_query(spec, now=NOW, tz=TZ, default_days=DEFAULT_DAYS).day_from == "2026-08-29"
    assert [row.keys["api"] for row in result.rows] == ["api-000"]
    assert result.population == 1
    # ...and the oracle, which compares real datetimes, says exactly the same thing
    _assert_matches_oracle(store, spec, store.fetch_runs(),
                           {a.api_id: a for a in store.search_apis(limit=10).items},
                           label="scope-threshold")


# -- limit, tie-break, samples --------------------------------------------------------------------


def test_limit_and_tie_break(synthetic):
    """Ties break on the group key ASCENDING, so a limit cut is deterministic -- two runs of the
    same query must not return different rows."""
    spec = QuerySpec(dimensions=["api"], measures=["runs", "apis"], order_by="apis", limit=5)
    first = synthetic.query(spec, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    second = synthetic.query(spec, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    keys = [row.keys["api"] for row in first.rows]
    assert keys == [row.keys["api"] for row in second.rows]
    assert len(keys) == 5
    # every returned row ties at api_count 1, so the cut is purely the key order
    assert all(row.measures["apis"] == 1 for row in first.rows)
    assert keys == sorted(keys)
    assert first.total_groups > 5


def test_samples_are_bounded_and_belong_to_the_row(synthetic):
    spec = QuerySpec(dimensions=["path_segment_2"], measures=["runs"], order_by="runs", limit=10)
    result = synthetic.query(spec, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    day_from, day_to = align_days(*resolve_window(spec, NOW, DEFAULT_DAYS), TZ)
    by_id = {r.run_id: r for r in synthetic.fetch_runs()}
    apis = {a.api_id: a for a in synthetic.search_apis(limit=200).items}
    assert result.rows
    for row in result.rows:
        assert len(row.api_ids) <= 20 and len(row.run_ids) <= 5 and row.run_ids
        for run_id in row.run_ids:
            run = by_id[run_id]
            assert day_from <= _run_day(run) <= day_to
            assert path_segments(apis[run.api_id].path)[1] == row.keys["path_segment_2"]
        for api_id in row.api_ids:
            assert path_segments(apis[api_id].path)[1] == row.keys["path_segment_2"]
        # newest first, the same order every other evidence list uses
        moments = [by_id[i].executed_at for i in row.run_ids]
        assert moments == sorted(moments, reverse=True)


def test_include_samples_false_skips_the_fill(synthetic):
    spec = QuerySpec(dimensions=["api"], measures=["runs"], order_by="runs", include_samples=False)
    result = synthetic.query(spec, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    assert result.rows and all(not row.api_ids and not row.run_ids for row in result.rows)


def test_samples_for_a_key_axis_row_carry_that_key(synthetic):
    spec = QuerySpec(dimensions=["failed_rule"], measures=["runs"], order_by="runs", limit=5)
    result = synthetic.query(spec, now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    by_id = {r.run_id: r for r in synthetic.fetch_runs()}
    assert result.rows
    for row in result.rows:
        assert row.run_ids
        for run_id in row.run_ids:
            assert row.keys["failed_rule"] in _group_keys(by_id[run_id], "failed_rule")


# -- EXPLAIN QUERY PLAN ---------------------------------------------------------------------------


def _plan(store: Store, sql: str, params) -> str:
    return " | ".join(row["detail"] for row in
                      store.conn().execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall())


def test_path_segment_grouping_uses_an_apis_index_and_never_scans_runs(synthetic):
    """The whole reason ``path_segment_*`` is a stored column: a GROUP BY over a SUBSTR of `path`
    could use no index, and the run table must not be touched at all on this path."""
    compiled = compile_query(QuerySpec(dimensions=["path_segment_2"], measures=["runs"],
                                       order_by="runs"), now=NOW, tz=TZ, default_days=DEFAULT_DAYS)
    detail = _plan(synthetic, compiled.sql, compiled.params)
    assert compiled.source == "rollup_day"
    assert not re.search(r"\b(SCAN|SEARCH) runs\b", detail), detail
    # `a` is the apis join, `r` the rollup -- both reached by an index (the apis PK for the
    # join, the day-leading rollup index for the window), neither by a table walk.
    assert re.search(r"SEARCH a USING (COVERING )?INDEX", detail), detail
    assert re.search(r"SEARCH r USING (COVERING )?INDEX idx_rollup_day", detail), detail


def test_the_executed_by_run_path_uses_the_executed_by_index(synthetic):
    """spec §4's only run-table row. ``idx_runs_executed_by_executed_at`` is the index that makes
    it affordable; without it the arm is a full table scan inside a 180-day window."""
    compiled = compile_query(
        QuerySpec(dimensions=["executed_by", "api"], measures=["runs"], order_by="runs",
                  filters=QueryFilters(executed_by=["minseong"])),
        now=NOW, tz=TZ, default_days=DEFAULT_DAYS)
    detail = _plan(synthetic, compiled.sql, compiled.params)
    assert compiled.source == "runs"
    assert "SEARCH runs USING INDEX idx_runs_executed_by_executed_at" in detail, detail

    # ...and with `executed_by` only as a DIMENSION the same index still drives the read
    dimension_only = compile_query(
        QuerySpec(dimensions=["executed_by", "api"], measures=["runs"], order_by="runs"),
        now=NOW, tz=TZ, default_days=DEFAULT_DAYS)
    detail = _plan(synthetic, dimension_only.sql, dimension_only.params)
    access = next(part for part in detail.split(" | ") if re.match(r"^(SCAN|SEARCH) runs\b", part))
    assert "USING INDEX" in access or "USING COVERING INDEX" in access, detail


def test_the_key_axis_reads_the_transposed_rollup(synthetic):
    compiled = compile_query(QuerySpec(dimensions=["http_status"], measures=["runs"],
                                       order_by="runs"), now=NOW, tz=TZ, default_days=DEFAULT_DAYS)
    detail = _plan(synthetic, compiled.sql, compiled.params)
    assert compiled.source == "rollup_key_day"
    assert "SEARCH k USING INDEX idx_rollup_key_day_axis_day" in detail, detail
    assert not re.search(r"\b(SCAN|SEARCH) runs\b", detail), detail


def test_the_operator_day_rollup_reads_its_own_index(synthetic):
    compiled = compile_query(QuerySpec(dimensions=["executed_by"], measures=["runs"],
                                       order_by="runs"), now=NOW, tz=TZ, default_days=DEFAULT_DAYS)
    detail = _plan(synthetic, compiled.sql, compiled.params)
    assert compiled.source == "rollup_operator_day"
    # the (day, operator_id) primary key IS the day-range access path here
    assert "SEARCH o USING INDEX sqlite_autoindex_rollup_operator_day_1" in detail, detail
    assert not re.search(r"\b(SCAN|SEARCH) runs\b", detail), detail

    # ...and an `executed_by` FILTER on this axis stays here too (fix round 1): the filter is
    # `operator_id IN (...)` on the table's own key column, so "이 사람 최근 얼마나 돌렸나"
    # reads (days x operators) rows rather than the run table.
    filtered = compile_query(
        QuerySpec(dimensions=["executed_by"], measures=["runs"], order_by="runs",
                  filters=QueryFilters(executed_by=["minseong", "jiwon"])),
        now=NOW, tz=TZ, default_days=DEFAULT_DAYS)
    detail = _plan(synthetic, filtered.sql, filtered.params)
    assert filtered.source == "rollup_operator_day"
    assert not re.search(r"\b(SCAN|SEARCH) runs\b", detail), detail
    assert "o.operator_id IN (?,?)" in filtered.sql


# -- the operator-day rollup is what makes the executed_by axis cheap ------------------------------


def test_operator_day_rollup_equals_a_recompute(synthetic):
    """Belt and braces next to the property test in `test_ingest.py`: the table the
    ``executed_by`` axis reads must equal a from-scratch fold of the run rows."""
    expected: dict[tuple[str, str], list[int]] = {}
    for run in synthetic.fetch_runs():
        if not run.executed_by:
            continue
        acc = expected.setdefault((_run_day(run), run.executed_by), [0, 0, 0, 0])
        acc[0] += 1
        acc[1 if run.status is RunStatus.PASS else 2 if run.status is RunStatus.FAIL else 3] += 1
    stored = {
        (r["day"], r["operator_id"]): [r["count"], r["pass"], r["fail"], r["error"]]
        for r in synthetic.conn().execute("SELECT * FROM rollup_operator_day").fetchall()
    }
    assert stored == expected
    assert sum(v[0] for v in stored.values()) == sum(
        1 for r in synthetic.fetch_runs() if r.executed_by)


def test_unattributed_runs_belong_to_no_operator(synthetic):
    """A run with no ``executed_by`` is counted by ``rollup_day`` and by NO operator row, so the
    operator axis' population is legitimately smaller than the project's. Stated as a test so a
    future reader does not "fix" the gap."""
    total = synthetic.query(QuerySpec(measures=["runs"], order_by="runs"),
                            now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS).population
    by_operator = synthetic.query(QuerySpec(dimensions=["executed_by"], measures=["runs"],
                                            order_by="runs", limit=50),
                                  now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    assert by_operator.population < total
    assert all(row.keys["executed_by"] is not None for row in by_operator.rows)


def test_the_compiled_sql_never_carries_a_model_string():
    """Nothing the model wrote reaches the statement: the values ride bound parameters and the
    identifiers come from the catalogue's fixed literals."""
    compiled = compile_query(
        QuerySpec(dimensions=["api"], measures=["runs"], order_by="runs",
                  filters=QueryFilters(path_contains=["'; DROP TABLE runs; --"])),
        now=NOW, tz=TZ, default_days=DEFAULT_DAYS)
    assert "DROP TABLE" not in compiled.sql
    assert "%'; drop table runs; --%" in [p for p in compiled.params if isinstance(p, str)]


def test_json_round_trip_of_a_result(synthetic):
    """The result is a pydantic model the tool layer (Task 3) serializes -- no sqlite type leaks
    (``Decimal``, ``bytes``, a naive datetime) may survive into it."""
    result = synthetic.query(
        QuerySpec(dimensions=["api_group"], measures=["runs", "fail_rate", "apis"],
                  order_by="fail_rate", compare_previous_window=True),
        now=NOW, tz=TZ, default_window_days=DEFAULT_DAYS)
    assert json.loads(result.model_dump_json())["source"] == "rollup_day"
