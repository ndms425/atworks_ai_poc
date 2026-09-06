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
         http: int = 200) -> RunResult:
    return RunResult(run_id=run_id, api_id=api_id, executed_at=at, target_env=env, test_data_label=label,
                     status=status, failed_rules=list(rules), http_status=http, duration_ms=100, job_id=job_id)


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
    assert data["jobs"]["executed"] == oracle["jobs"]["executed"]
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
