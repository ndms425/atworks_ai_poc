"""Task 8 (scale spec 2026-09-06 §9): the host's read paths run over the materialized tables.

These are the *behavioural* proofs that the rewiring changed more than the cost:

* ``get_context`` never scans ``runs`` unbounded and never touches ``bodies``;
* the insight panel finds a regression suspect whose runs are OLDER than the newest
  ``max_aggregate_runs`` — the exact case the old 2000-run sample could not see;
* the daily briefing over a synthetic 5,000-run day equals the old run-list computation
  (``build_briefing``, kept as the oracle) without ever materializing a run.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from atworks_agent import (
    ActorKind,
    AggregateQuery,
    ApiSpec,
    AtworksAgentConfig,
    AtworksSessionContext,
    JobDraft,
    JobKind,
    RunResult,
    RunStatus,
    collect_runs,
)
from atworks_host.briefing import Briefings, build_briefing
from atworks_host.insights import InsightPanels
from atworks_host.mock_backend import MockAtworks

KST = timezone(timedelta(hours=9))
FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"
SESSION = AtworksSessionContext(session_id="rw", project_id="mes", operator="minseong", role="developer")


def _api(api_id: str, updated_at: datetime, path: str | None = None) -> ApiSpec:
    return ApiSpec(api_id=api_id, method="GET", path=path or f"/v1/x/{api_id}", name=api_id, updated_at=updated_at)


def _run(run_id: str, api_id: str, at: datetime, *, status: RunStatus, env: str = "dev",
         label: str | None = None, rules: tuple[str, ...] = (), job_id: str | None = None,
         http: int = 200, executed_by: str | None = None) -> RunResult:
    return RunResult(run_id=run_id, api_id=api_id, executed_at=at, target_env=env, test_data_label=label,
                     status=status, failed_rules=list(rules), http_status=http, duration_ms=100, job_id=job_id,
                     executed_by=executed_by)


async def _noop_narrator(candidates, role, *, notes=None):
    del candidates, role, notes
    return []


# -- get_context: no unbounded run scan, no body read -------------------------------------------


async def test_get_context_reads_no_unbounded_runs_and_no_bodies():
    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    statements: list[str] = []
    backend.store.conn().set_trace_callback(statements.append)
    try:
        context = await backend.get_context(SESSION)
    finally:
        backend.store.conn().set_trace_callback(None)

    assert context["recent_counts"]["fail"] >= 0 and "scope_api_ids" in context
    assert statements, "the trace callback saw nothing — the assertions below would be vacuous"
    for raw in statements:
        sql = " ".join(raw.lower().split())
        assert "bodies" not in sql, raw
        if "from runs" in sql:
            # every runs read is bound: the window (30 days) plus a status predicate, never a
            # scan of every fail and every error ever recorded
            assert " where " in sql, raw
            assert "executed_at >=" in sql, raw


async def test_get_context_counts_are_windowed_to_the_scope_window():
    config = AtworksAgentConfig(model="m", scope_window_days=30)
    backend = MockAtworks(config, FIXTURES)
    now = datetime.now(UTC)
    backend.runs = {
        "run-old": _run("run-old", "api-001", now - timedelta(days=400), status=RunStatus.FAIL),
        "run-new": _run("run-new", "api-001", now - timedelta(days=1), status=RunStatus.FAIL),
    }
    session = AtworksSessionContext(session_id="s", project_id="mes", operator="minseong", now=now)
    context = await backend.get_context(session)
    assert context["recent_counts"]["fail"] == 1     # the 400-day-old failure is outside the window


# -- insight panel: full coverage, not the newest N runs ----------------------------------------


def _panel_backend(now: datetime, config: AtworksAgentConfig) -> MockAtworks:
    """A project whose regression suspect is the OLDEST thing in it: 2,500 newer runs sit on top
    of it, so the pre-Task-8 ``collect_runs(cap=max_aggregate_runs=2000)`` window stopped short
    of ever seeing it."""
    backend = MockAtworks(config, FIXTURES)
    broke_at = now - timedelta(days=25)
    apis = {"api-reg": _api("api-reg", broke_at - timedelta(hours=1))}   # updated between pass and fail
    apis.update({f"api-{i:03d}": _api(f"api-{i:03d}", now - timedelta(days=200)) for i in range(25)})
    backend.apis = apis

    runs: dict[str, RunResult] = {}
    # the regression API: passed, was updated, then broke and STAYED broken
    runs["run-reg-0"] = _run("run-reg-0", "api-reg", broke_at - timedelta(hours=2), status=RunStatus.PASS)
    for i in range(1, 6):
        runs[f"run-reg-{i}"] = _run(f"run-reg-{i}", "api-reg", broke_at + timedelta(hours=i),
                                    status=RunStatus.FAIL, rules=("regressed >= 0",))
    # ... then 2,500 newer runs on other APIs
    for i in range(2500):
        at = now - timedelta(days=10) + timedelta(minutes=i)
        runs[f"run-n-{i:05d}"] = _run(f"run-n-{i:05d}", f"api-{i % 25:03d}", at,
                                      status=RunStatus.PASS if i % 3 else RunStatus.FAIL,
                                      rules=() if i % 3 else ("noise >= 0",))
    backend.runs = runs
    return backend


async def test_panel_finds_a_regression_suspect_older_than_the_newest_2000_runs(tmp_path):
    now = datetime.now(UTC)
    config = AtworksAgentConfig(model="m")
    backend = _panel_backend(now, config)

    # the old path's window really would have missed it: the regression API's runs are not in the
    # newest max_aggregate_runs runs at all
    sampled = await collect_runs(backend, SESSION, since=now - timedelta(days=config.max_aggregate_window_days),
                                 cap=config.max_aggregate_runs)
    assert len(sampled) == config.max_aggregate_runs
    assert not any(r.api_id == "api-reg" for r in sampled)

    async def narrator(candidates, role, *, notes=None):
        del candidates, role, notes
        return []

    panels = InsightPanels(tmp_path, config, narrator=narrator)
    panel = await panels.build(backend, SESSION, now)
    ids = [item.candidate.candidate_id for item in panel.items]
    assert "regression_suspect:api-reg" in ids
    candidate = next(i.candidate for i in panel.items if i.candidate.candidate_id == "regression_suspect:api-reg")
    assert candidate.figures["fail"] == 5            # the host's own count, over the whole window
    assert candidate.ref_ids and all(r.startswith("run-reg-") for r in candidate.ref_ids)


async def test_panel_top_failed_rule_reports_the_true_api_count(tmp_path):
    now = datetime.now(UTC)
    config = AtworksAgentConfig(model="m")
    backend = _panel_backend(now, config)

    async def narrator(candidates, role, *, notes=None):
        del candidates, role, notes
        return []

    panel = await InsightPanels(tmp_path, config, narrator=narrator).build(backend, SESSION, now)
    rule = next((i.candidate for i in panel.items if i.candidate.candidate_id == "top_failed_rule:noise >= 0"), None)
    assert rule is not None
    # 25 APIs share the rule -- far more than the 50 run ids a group carries, and the figure is
    # the rollup's COUNT(DISTINCT api_id), not a sample
    assert rule.figures["apis"] == 25
    assert len(rule.api_ids) <= 20


# -- fix round 1 (A): exact operator scope, no id list ------------------------------------------


def _wide_operator_backend(now: datetime, config: AtworksAgentConfig) -> MockAtworks:
    """One operator who has executed 150 distinct APIs, whose ONLY regression suspect is
    ``api-149`` — alphabetically last, so it sits far outside ``ScopeSummary.api_ids``' 100-id
    sample (``operator_scope_summary`` orders by api_id and takes the first 100)."""
    backend = MockAtworks(config, FIXTURES)
    broke_at = now - timedelta(days=10)
    apis = {f"api-{i:03d}": _api(f"api-{i:03d}", now - timedelta(days=300)) for i in range(150)}
    # the suspect's spec was updated between its last pass and its first failure
    apis["api-149"] = _api("api-149", broke_at - timedelta(hours=1))
    backend.apis = apis

    runs: dict[str, RunResult] = {}
    for i in range(150):
        runs[f"run-p-{i:03d}"] = _run(f"run-p-{i:03d}", f"api-{i:03d}", broke_at - timedelta(hours=2),
                                      status=RunStatus.PASS, executed_by="minseong")
    for i in range(1, 4):
        runs[f"run-reg-{i}"] = _run(f"run-reg-{i}", "api-149", broke_at + timedelta(hours=i),
                                    status=RunStatus.FAIL, rules=("regressed >= 0",), executed_by="minseong")
    backend.runs = runs
    return backend


async def test_operator_scope_is_a_predicate_not_a_capped_id_list(tmp_path):
    """The panel must cover EVERY API the operator ran, not the 100 that fit in ScopeSummary.

    Before the fix, ``InsightPanels._inputs`` fed ``ScopeSummary.api_ids`` (<=100, api_id order)
    into ``watermarks``/``AggregateQuery.scope_api_ids``/``current_state``, so a suspect on
    ``api-149`` was outside the scope the panel actually queried and never became a candidate."""
    now = datetime.now(UTC)
    config = AtworksAgentConfig(model="m")
    backend = _wide_operator_backend(now, config)

    summary = await backend.operator_scope(SESSION, "minseong", config.scope_window_days)
    assert summary.total == 150 and len(summary.api_ids) == 100     # the sample really is short
    assert "api-149" not in summary.api_ids                          # ...and really does miss it

    panel = await InsightPanels(tmp_path, config, narrator=_noop_narrator).build(backend, SESSION, now)

    assert panel.scope_size == 150 and panel.scope_fallback is False
    assert "regression_suspect:api-149" in [i.candidate.candidate_id for i in panel.items]


async def test_operator_scope_predicate_excludes_another_operators_apis(tmp_path):
    """The predicate narrows as hard as the id list did: a regression on an API this operator
    never ran is not their insight."""
    now = datetime.now(UTC)
    config = AtworksAgentConfig(model="m")
    backend = _wide_operator_backend(now, config)
    broke_at = now - timedelta(days=10)
    apis = dict(backend.apis)
    apis["api-other"] = _api("api-other", broke_at - timedelta(hours=1))
    backend.apis = apis
    runs = dict(backend.runs)
    runs["run-o-0"] = _run("run-o-0", "api-other", broke_at - timedelta(hours=2),
                           status=RunStatus.PASS, executed_by="jiwoo")
    runs["run-o-1"] = _run("run-o-1", "api-other", broke_at + timedelta(hours=1),
                           status=RunStatus.FAIL, rules=("regressed >= 0",), executed_by="jiwoo")
    backend.runs = runs

    panel = await InsightPanels(tmp_path, config, narrator=_noop_narrator).build(backend, SESSION, now)
    ids = [i.candidate.candidate_id for i in panel.items]
    assert "regression_suspect:api-149" in ids and "regression_suspect:api-other" not in ids


async def test_regression_suspects_are_cut_newest_break_first(tmp_path):
    """Fix (D): the panel queries at most ``_GROUP_LIMIT`` suspects. Cutting them in api_id order
    hid the FRESHEST regression behind fifty older ones; the cut is now by first_non_pass_at DESC.
    Here 60 APIs broke and the newest break is on the alphabetically last one."""
    now = datetime.now(UTC)
    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    apis, runs = {}, {}
    for i in range(60):
        api_id = f"api-{i:03d}"
        broke_at = now - timedelta(days=25) + timedelta(days=i * 0.3)
        apis[api_id] = _api(api_id, broke_at - timedelta(hours=1))
        runs[f"run-{api_id}-p"] = _run(f"run-{api_id}-p", api_id, broke_at - timedelta(hours=2),
                                       status=RunStatus.PASS)
        # api-059 (newest break) also fails loudest, so it wins the candidate ranking IF the
        # suspect cut let it through at all — which api_id order never did.
        for k in range(5 if i == 59 else 1):
            runs[f"run-{api_id}-f{k}"] = _run(f"run-{api_id}-f{k}", api_id, broke_at + timedelta(minutes=k),
                                              status=RunStatus.FAIL, rules=("regressed >= 0",))
    backend.apis = apis
    backend.runs = runs

    panel = await InsightPanels(tmp_path, config, narrator=_noop_narrator).build(backend, SESSION, now)
    ids = [i.candidate.candidate_id for i in panel.items]
    assert "regression_suspect:api-059" in ids       # the newest break, 60th in api_id order
    assert "regression_suspect:api-000" not in ids   # the oldest break, cut as it should be


# -- fix round 1 (B): honest flaky / regression tiles --------------------------------------------


def _saturating_backend(now: datetime, config: AtworksAgentConfig) -> MockAtworks:
    """>500 cells carrying failures, plus ONE flaky cell whose failure count is the lowest in the
    project. Every aggregate group is ranked by (fail+error, count), so the flaky cell sorts dead
    last — beyond any limit the old tile counted over."""
    backend = MockAtworks(config, FIXTURES)
    apis = {f"api-{i:04d}": _api(f"api-{i:04d}", now - timedelta(days=300)) for i in range(600)}
    apis["api-flaky"] = _api("api-flaky", now - timedelta(days=300))
    backend.apis = apis

    runs: dict[str, RunResult] = {}
    base = now - timedelta(days=5)
    for i in range(600):                       # 600 loud cells, 3 failures each
        for k in range(3):
            runs[f"run-l-{i:04d}-{k}"] = _run(f"run-l-{i:04d}-{k}", f"api-{i:04d}",
                                              base + timedelta(minutes=k), status=RunStatus.FAIL,
                                              rules=("loud >= 0",))
    # one quiet cell: pass -> fail -> pass = 2 transitions (flaky_min_transitions) on ONE failure
    for k, status in enumerate((RunStatus.PASS, RunStatus.FAIL, RunStatus.PASS)):
        runs[f"run-f-{k}"] = _run(f"run-f-{k}", "api-flaky", base + timedelta(hours=k), status=status,
                                  rules=("quiet >= 0",) if status is RunStatus.FAIL else ())
    backend.runs = runs
    return backend


async def test_flaky_tile_counts_a_quiet_cell_a_ranked_group_list_never_reaches():
    """The tile is two SQL counts now. Counting ``g.flaky`` over ``aggregate_runs(limit=500)``
    could not see this cell: 600 louder cells outrank it, so the old tile reported 0 flaky on a
    project that has one."""
    now = datetime.now(UTC)
    config = AtworksAgentConfig(model="m")
    backend = _saturating_backend(now, config)
    since = now - timedelta(days=config.max_aggregate_window_days)

    groups = await backend.aggregate_runs(SESSION, AggregateQuery(
        since=since, group_by="api_env_data", limit=500))
    assert len(groups) == 500 and not any(g.flaky for g in groups)   # the old count really is 0

    insights = await backend.summarize_insights(SESSION, since=since)
    assert insights.flaky == 1


async def test_summarize_insights_matches_the_aggregate_when_nothing_saturates():
    """On a project small enough for the group list to hold everything, the SQL counts and the
    group-derived counts agree exactly — the fix removes a ceiling, it does not move a number."""
    now = datetime.now(UTC)
    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    since = now - timedelta(days=config.max_aggregate_window_days)
    base = now - timedelta(days=3)
    backend.apis = {"api-a": _api("api-a", base - timedelta(hours=3)),
                    "api-b": _api("api-b", now - timedelta(days=300))}
    backend.runs = {
        "r0": _run("r0", "api-a", base - timedelta(hours=4), status=RunStatus.PASS),
        "r1": _run("r1", "api-a", base, status=RunStatus.FAIL, rules=("x >= 0",)),
        "r2": _run("r2", "api-b", base, status=RunStatus.PASS),
        "r3": _run("r3", "api-b", base + timedelta(hours=1), status=RunStatus.FAIL, rules=("x >= 0",)),
        "r4": _run("r4", "api-b", base + timedelta(hours=2), status=RunStatus.PASS),
    }
    cells = await backend.aggregate_runs(SESSION, AggregateQuery(since=since, group_by="api_env_data", limit=500))
    by_api = await backend.aggregate_runs(SESSION, AggregateQuery(since=since, group_by="api", limit=500))

    insights = await backend.summarize_insights(SESSION, since=since)
    assert insights.flaky == sum(1 for g in cells if g.flaky) == 1            # api-b's cell
    assert insights.regression_suspect == sum(1 for g in by_api if g.regression_suspect) == 1


async def test_briefing_insights_block_equals_the_sql_counts(tmp_path):
    now = datetime(2026, 9, 3, 9, 5, tzinfo=KST)
    config = AtworksAgentConfig(model="m")
    briefings = Briefings(tmp_path / "b", config)
    backend = _saturating_backend(now.astimezone(UTC), config)

    path = await briefings.generate(backend, SESSION, now)
    data = json.loads((path.parent / "data.json").read_text(encoding="utf-8"))

    expected = await backend.summarize_insights(
        SESSION, since=now - timedelta(days=config.max_aggregate_window_days))
    assert data["insights"] == expected.model_dump()
    assert data["insights"]["flaky"] == 1        # and not the 0 a top-500 group count reported


# -- fix round 1 (C): include_run_ids ------------------------------------------------------------


async def test_include_run_ids_false_drops_the_per_group_evidence_queries():
    """``run_ids`` costs one indexed query per returned group on the api/env/cell axes. A caller
    that only wants the counters says so and pays for none of them."""
    now = datetime.now(UTC)
    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    backend.apis = {f"api-{i:03d}": _api(f"api-{i:03d}", now - timedelta(days=300)) for i in range(20)}
    backend.runs = {
        f"run-{i:03d}": _run(f"run-{i:03d}", f"api-{i % 20:03d}", now - timedelta(days=2, minutes=i),
                             status=RunStatus.FAIL, rules=("x >= 0",))
        for i in range(200)
    }
    since = now - timedelta(days=config.max_aggregate_window_days)

    def count(include: bool) -> tuple[int, list]:
        statements: list[str] = []
        backend.store.conn().set_trace_callback(statements.append)
        try:
            groups = backend.store.aggregate_rollups(
                AggregateQuery(since=since, group_by="api", include_run_ids=include, limit=50),
                flaky_min=config.flaky_min_transitions, apis=backend.apis)
        finally:
            backend.store.conn().set_trace_callback(None)
        return len(statements), groups

    with_ids, groups_with = count(True)
    without_ids, groups_without = count(False)

    assert len(groups_with) == len(groups_without) == 20
    assert [(g.key, g.count, g.fail) for g in groups_with] == [(g.key, g.count, g.fail) for g in groups_without]
    assert all(g.run_ids for g in groups_with) and not any(g.run_ids for g in groups_without)
    assert without_ids == with_ids - 20      # exactly one saved query per returned group


# -- daily briefing: query-driven, equal to the old run-list computation -------------------------


def _briefing_backend(config: AtworksAgentConfig, start: datetime, end: datetime) -> tuple[MockAtworks, list[RunResult]]:
    backend = MockAtworks(config, FIXTURES)
    backend.apis = {f"api-{i:03d}": _api(f"api-{i:03d}", start - timedelta(days=300)) for i in range(20)}
    span = (end - start) - timedelta(minutes=1)
    runs: list[RunResult] = []
    for i in range(5000):
        at = start + span * (i / 5000)
        api_id = f"api-{i % 20:03d}"
        env = "dev" if i % 2 else "stg"
        label = "basic" if i % 4 < 2 else "edge"
        if i % 7 == 0:
            status, rules, http = RunStatus.FAIL, ("amount >= 0",), 200
        elif i % 23 == 0:
            status, rules, http = RunStatus.ERROR, (), 503
        else:
            status, rules, http = RunStatus.PASS, (), 200
        runs.append(_run(f"run-b-{i:05d}", api_id, at, status=status, env=env, label=label,
                         rules=rules, http=http, job_id=f"job-{i % 2}"))
    backend.runs = {r.run_id: r for r in runs}
    return backend, runs


async def test_briefing_over_a_5k_run_day_equals_the_run_list_oracle(tmp_path):
    config = AtworksAgentConfig(model="m")
    now = datetime(2026, 9, 3, 9, 5, tzinfo=KST)
    briefings = Briefings(tmp_path / "b", config, portal_origin="http://localhost:3110")
    end = briefings.window_end(now)
    start = end - timedelta(days=1)
    backend, runs = _briefing_backend(config, start, end)

    # two real jobs so the executed list has something to name
    staged = []
    for i in range(2):
        job = await backend.stage_job(SESSION, JobDraft(
            kind=JobKind.RUN_NOW, summary=f"nightly {i}", api_ids=["api-000"], target_envs=["dev"]), ActorKind.AGENT)
        staged.append(await backend.apply_job(SESSION, job.job_id))
    backend.runs = {
        r.run_id: r.model_copy(update={"job_id": staged[int(r.job_id[-1])].job_id}) for r in runs
    }
    runs = list(backend.runs.values())

    path = await briefings.generate(backend, SESSION, now)
    data = json.loads((path.parent / "data.json").read_text(encoding="utf-8"))

    jobs = (await backend.all_jobs(SESSION, limit=1000)).items
    oracle = build_briefing(now, runs, dict(backend.apis), jobs, config,
                            window=(start, end), portal_origin="http://localhost:3110")

    assert data["counts"] == oracle["counts"] and data["counts"]["total"] == 5000
    # Same SET of executed jobs with the same run counts; the ORDER is not a contract and the two
    # paths no longer share one (final review I6: the query-driven path drives off
    # `count_runs_by_job`'s keys -- the jobs that actually ran -- and ranks by run count, while
    # the run-list oracle follows `all_jobs`' created_at order).
    by_id = {j["job_id"]: j for j in data["jobs"]["executed"]}
    assert by_id == {j["job_id"]: j for j in oracle["jobs"]["executed"]}
    assert sorted(j["runs"] for j in data["jobs"]["executed"]) == [2500, 2500]
    assert data["jobs"]["pending"] == oracle["jobs"]["pending"]
    assert data["insights"] == oracle["insights"]
    for got, want in zip(data["top_groups"], oracle["top_groups"], strict=True):
        # count/fail/error/passed and the evidence ids are identical; p95_duration_ms and
        # transitions are the documented rollup deltas (Store.aggregate_rollups' docstring) and
        # are not compared here — a 09:00→09:00 window contains no whole rollup day at all.
        for field in ("key", "label", "count", "fail", "error", "passed", "run_ids"):
            assert got.get(field) == want.get(field), (got["key"], field)


async def test_briefing_generate_never_lists_runs(tmp_path):
    """The proof that the briefing is query-driven: with 5,000 runs in the window, not one
    ``RunResult`` is materialized (``list_runs``/``runs_by_ids``/``fetch_runs`` are untouched)."""
    config = AtworksAgentConfig(model="m")
    now = datetime(2026, 9, 3, 9, 5, tzinfo=KST)
    briefings = Briefings(tmp_path / "b", config)
    end = briefings.window_end(now)
    backend, _ = _briefing_backend(config, end - timedelta(days=1), end)

    calls: list[str] = []
    for name in ("list_runs", "runs_by_ids"):
        original = getattr(backend, name)

        async def spy(*a, _name=name, _original=original, **kw):
            calls.append(_name)
            return await _original(*a, **kw)

        setattr(backend, name, spy)
    original_fetch = backend.store.fetch_runs
    backend.store.fetch_runs = lambda *a, **kw: (calls.append("fetch_runs"), original_fetch(*a, **kw))[1]

    await briefings.generate(backend, SESSION, now)
    assert calls == []


# -- fix round 2: transition-ordered flaky candidates -------------------------------------------


async def test_panel_names_a_quiet_flaky_cell_a_failure_ranked_cut_never_reaches(tmp_path):
    """The last saturation in the panel. ``_GROUP_LIMIT`` is 50, so the cell aggregate returns 50
    of the window's cells; ranked by failure volume those are 50 loud, never-flipping cells and the
    one cell that actually flips is nowhere in the list -- the Home tile could COUNT it
    (``summarize_insights``) while the panel below it could not NAME it. With
    ``order_by="transitions"`` the flaky cell is the first row the panel reads."""
    now = datetime.now(UTC)
    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    session = AtworksSessionContext(session_id="rw", project_id="mes", operator="minseong", role="qa")
    base = now - timedelta(days=5)

    apis = {f"api-loud-{i:03d}": _api(f"api-loud-{i:03d}", now - timedelta(days=300)) for i in range(60)}
    apis["api-quiet"] = _api("api-quiet", now - timedelta(days=300))
    backend.apis = apis
    runs: dict[str, RunResult] = {}
    for i in range(60):
        for k in range(8):      # loud: fails and fails and never flips
            runs[f"run-l-{i:03d}-{k}"] = _run(f"run-l-{i:03d}-{k}", f"api-loud-{i:03d}",
                                              base + timedelta(minutes=k), status=RunStatus.FAIL,
                                              rules=("loud >= 0",), executed_by="minseong")
    for k, status in enumerate((RunStatus.PASS, RunStatus.FAIL, RunStatus.PASS)):
        runs[f"run-q-{k}"] = _run(f"run-q-{k}", "api-quiet", base + timedelta(hours=k), status=status,
                                  rules=("quiet >= 0",) if status is RunStatus.FAIL else (),
                                  executed_by="minseong")
    backend.runs = runs
    since = now - timedelta(days=config.max_aggregate_window_days)

    # the pre-fix read: 50 cells by failure volume, and the flaky one is not among them
    loud_first = await backend.aggregate_runs(session, AggregateQuery(
        since=since, group_by="api_env_data", scope_operator="minseong", limit=50))
    assert len(loud_first) == 50 and not any(g.flaky for g in loud_first)
    # ...while the tile counted it all along
    assert (await backend.summarize_insights(session, since, scope_operator="minseong")).flaky == 1

    panel = await InsightPanels(tmp_path, config, narrator=_noop_narrator).build(backend, session, now)
    assert "flaky_cell:api-quiet|dev|-" in [i.candidate.candidate_id for i in panel.items]
