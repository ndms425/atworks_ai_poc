from datetime import UTC, datetime, timedelta

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.insights import KIND_LABEL, ROLE_PRIORITY, candidate_insights, operator_scope
from atworks_agent.types import ApiSpec, JobKind, JobSpec, JobStatus, RunResult, RunStatus

CFG = AtworksAgentConfig(model="m")
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def run(run_id, api_id, executed_at, env="dev", status=RunStatus.PASS, rules=(), executed_by=None, data=None,
        http_status=None):
    return RunResult(run_id=run_id, api_id=api_id, executed_at=executed_at, target_env=env,
                      test_data_label=data, status=status, failed_rules=list(rules), executed_by=executed_by,
                      http_status=http_status)


def api(api_id, updated_at, method="GET", path="/x"):
    return ApiSpec(api_id=api_id, method=method, path=path, name=api_id, updated_at=updated_at)


def job(job_id, api_ids, created_at, status=JobStatus.STAGED):
    return JobSpec(job_id=job_id, kind=JobKind.RUN_NOW, summary="s", api_ids=api_ids, target_envs=["dev"],
                    created_at=created_at, created_by="op", status=status)


def _rich_fixture():
    """One instance of every kind, exactly once each, so the default cap (5) keeps all of them."""
    apis = {
        "api-A": api("api-A", NOW - timedelta(days=5)),     # regression: pass before update, fail after
        "api-B": api("api-B", NOW - timedelta(days=100)),   # flaky: updated_at too early to regress
        "api-C": api("api-C", NOW - timedelta(days=10)),    # env divergence: dev=pass, stg=fail
    }
    runs = [
        # api-A: regression_suspect
        run("rA1", "api-A", NOW - timedelta(days=10), status=RunStatus.PASS),
        run("rA2", "api-A", NOW - timedelta(days=3), status=RunStatus.FAIL, rules=["rule_common"]),
        # api-B: flaky_cell (3 pass/fail transitions on the same api|env|data cell)
        run("rB1", "api-B", NOW - timedelta(days=8), status=RunStatus.PASS),
        run("rB2", "api-B", NOW - timedelta(days=7), status=RunStatus.FAIL, rules=["rule_common"]),
        run("rB3", "api-B", NOW - timedelta(days=6), status=RunStatus.PASS),
        run("rB4", "api-B", NOW - timedelta(days=5), status=RunStatus.FAIL, rules=["rule_common"]),
        # api-C: env_divergence (latest dev=pass, latest stg=fail)
        run("rC1", "api-C", NOW - timedelta(days=4), env="dev", status=RunStatus.PASS),
        run("rC2", "api-C", NOW - timedelta(days=3), env="stg", status=RunStatus.FAIL, rules=["rule_common"]),
    ]
    jobs = [job("job-stale-1", ["api-A"], NOW - timedelta(hours=30))]
    return runs, apis, jobs


def test_operator_scope_is_own_recent_runs_only():
    runs = [
        run("r1", "api-A", NOW - timedelta(days=5), executed_by="dev1"),    # own, recent -> included
        run("r2", "api-A", NOW - timedelta(days=1), executed_by=None),      # unattributed -> excluded
        run("r3", "api-B", NOW - timedelta(days=2), executed_by="dev2"),    # other operator -> excluded
        run("r4", "api-C", NOW - timedelta(days=45), executed_by="dev1"),   # own but past window(30) -> excluded
    ]
    assert operator_scope(runs, "dev1", 30, NOW) == {"api-A"}
    assert operator_scope([], "dev1", 30, NOW) == set()


def test_candidates_exclude_out_of_scope_apis():
    apis = {
        "api-A": api("api-A", NOW - timedelta(days=5)),
        "api-Z": api("api-Z", NOW - timedelta(days=5)),
    }
    runs = [
        run("rA1", "api-A", NOW - timedelta(days=10), status=RunStatus.PASS),
        run("rA2", "api-A", NOW - timedelta(days=3), status=RunStatus.FAIL, rules=["r"]),
        run("rZ1", "api-Z", NOW - timedelta(days=10), status=RunStatus.PASS),
        run("rZ2", "api-Z", NOW - timedelta(days=3), status=RunStatus.FAIL, rules=["r"]),
    ]
    out = candidate_insights(runs, apis, [], {"api-A"}, "developer", CFG, NOW)
    assert any(c.candidate_id == "regression_suspect:api-A" for c in out)
    assert all("api-Z" not in c.api_ids for c in out)
    assert all(c.candidate_id != "regression_suspect:api-Z" for c in out)


def test_role_order_matches_priority_table():
    runs, apis, jobs = _rich_fixture()
    dev = candidate_insights(runs, apis, jobs, None, "developer", CFG, NOW)
    qa = candidate_insights(runs, apis, jobs, None, "qa", CFG, NOW)
    assert [c.kind for c in dev][0] == "regression_suspect"
    assert [c.kind for c in qa][0] == "flaky_cell"
    assert ROLE_PRIORITY["developer"][0] == "regression_suspect"
    assert ROLE_PRIORITY["qa"][0] == "flaky_cell"


def test_each_kind_can_be_produced():
    runs, apis, jobs = _rich_fixture()
    out = candidate_insights(runs, apis, jobs, None, "developer", CFG, NOW)
    assert {c.kind for c in out} == {
        "regression_suspect", "flaky_cell", "top_failed_rule", "env_divergence", "stale_pending",
    }
    for c in out:
        assert c.label.startswith(KIND_LABEL[c.kind] + " · ")


def test_cap_and_deterministic_ids():
    runs, apis, jobs = _rich_fixture()
    small_cfg = AtworksAgentConfig(model="m", max_insight_candidates=2)
    out = candidate_insights(runs, apis, jobs, None, "developer", small_cfg, NOW)
    again = candidate_insights(runs, apis, jobs, None, "developer", small_cfg, NOW)
    assert len(out) == 2
    assert out == again
    ids = [c.candidate_id for c in out]
    assert len(ids) == len(set(ids))


def test_scope_none_means_project_wide():
    apis = {
        "api-A": api("api-A", NOW - timedelta(days=5)),
        "api-Z": api("api-Z", NOW - timedelta(days=5)),
    }
    runs = [
        run("rA1", "api-A", NOW - timedelta(days=10), status=RunStatus.PASS),
        run("rA2", "api-A", NOW - timedelta(days=3), status=RunStatus.FAIL, rules=["r"]),
        run("rZ1", "api-Z", NOW - timedelta(days=10), status=RunStatus.PASS),
        run("rZ2", "api-Z", NOW - timedelta(days=3), status=RunStatus.FAIL, rules=["r"]),
    ]
    out = candidate_insights(runs, apis, [], None, "developer", CFG, NOW)
    ids = {c.candidate_id for c in out}
    assert "regression_suspect:api-A" in ids
    assert "regression_suspect:api-Z" in ids


def test_pm_orders_stale_pending_first_and_role_tables_hold_full_order():
    runs, apis, jobs = _rich_fixture()
    dev = candidate_insights(runs, apis, jobs, None, "developer", CFG, NOW)
    dev_order = ROLE_PRIORITY["developer"]
    priorities = [c.priority for c in dev]
    assert priorities == sorted(priorities)
    for c in dev:
        assert c.priority == dev_order.index(c.kind)

    pm = candidate_insights(runs, apis, jobs, None, "pm", CFG, NOW)
    assert pm[0].kind == "stale_pending"


def test_flaky_cell_label_is_humanized_but_candidate_id_keeps_the_raw_key():
    apis = {"api-004": api("api-004", NOW - timedelta(days=100))}
    # No test_data_label -> raw key "api-004|dev|-"; label must drop the trailing "-".
    no_data_runs = [
        run("n1", "api-004", NOW - timedelta(days=8), status=RunStatus.PASS),
        run("n2", "api-004", NOW - timedelta(days=7), status=RunStatus.FAIL, rules=["r"]),
        run("n3", "api-004", NOW - timedelta(days=6), status=RunStatus.PASS),
        run("n4", "api-004", NOW - timedelta(days=5), status=RunStatus.FAIL, rules=["r"]),
    ]
    out = candidate_insights(no_data_runs, apis, [], None, "developer", CFG, NOW)
    cell = next(c for c in out if c.kind == "flaky_cell")
    assert cell.candidate_id == "flaky_cell:api-004|dev|-"
    assert cell.label == f"{KIND_LABEL['flaky_cell']} · api-004 · dev"

    # With a test_data_label -> raw key "api-004|dev|정상 결제"; label keeps it.
    with_data_runs = [
        run("d1", "api-004", NOW - timedelta(days=8), status=RunStatus.PASS, data="정상 결제"),
        run("d2", "api-004", NOW - timedelta(days=7), status=RunStatus.FAIL, rules=["r"], data="정상 결제"),
        run("d3", "api-004", NOW - timedelta(days=6), status=RunStatus.PASS, data="정상 결제"),
        run("d4", "api-004", NOW - timedelta(days=5), status=RunStatus.FAIL, rules=["r"], data="정상 결제"),
    ]
    out2 = candidate_insights(with_data_runs, apis, [], None, "developer", CFG, NOW)
    cell2 = next(c for c in out2 if c.kind == "flaky_cell")
    assert cell2.candidate_id == "flaky_cell:api-004|dev|정상 결제"
    assert cell2.label == f"{KIND_LABEL['flaky_cell']} · api-004 · dev · 정상 결제"


def test_empty_scope_yields_no_candidates():
    runs, apis, jobs = _rich_fixture()
    empty = candidate_insights(runs, apis, jobs, scope=set(), role="developer", config=CFG, now=NOW)
    assert empty == []
    project_wide = candidate_insights(runs, apis, jobs, scope=None, role="developer", config=CFG, now=NOW)
    assert project_wide != []


def test_many_apis_in_one_bucket_do_not_crash_and_lists_are_capped():
    # 60 APIs, each with exactly one error run, feeding the SAME synthetic "(error) HTTP 500"
    # bucket -- more than aggregation.MAX_RUN_IDS(50), so figures["apis"] can only be a true
    # count if it's recounted over the source runs rather than derived from g.run_ids.
    apis = {f"api-{i}": api(f"api-{i}", NOW - timedelta(days=100)) for i in range(60)}
    runs = [
        run(f"r{i}", f"api-{i}", NOW - timedelta(days=1), status=RunStatus.ERROR, http_status=500)
        for i in range(60)
    ]
    out = candidate_insights(runs, apis, [], None, "developer", CFG, NOW)
    for c in out:
        assert len(c.api_ids) <= 20
        assert len(c.ref_ids) <= 20

    error_bucket = next(c for c in out if c.kind == "top_failed_rule" and c.candidate_id.endswith("HTTP 500"))
    assert len(error_bucket.api_ids) == 20
    assert error_bucket.figures["apis"] == 60
    assert error_bucket.figures["error"] == 60
    assert "error" in error_bucket.figures


def test_top_failed_rule_apis_count_is_the_true_distinct_count_not_a_run_id_sample():
    # 8 APIs each with 20 failing runs on the same rule: 160 runs total, well over
    # aggregation.MAX_RUN_IDS(50), but only 8 distinct APIs -- figures["apis"] must reflect
    # that true distinct count, not len(g.run_ids[:50]).
    apis = {f"api-{i}": api(f"api-{i}", NOW - timedelta(days=100)) for i in range(8)}
    runs = [
        run(f"r{i}-{j}", f"api-{i}", NOW - timedelta(hours=j), status=RunStatus.FAIL, rules=["rule_common"])
        for i in range(8)
        for j in range(20)
    ]
    out = candidate_insights(runs, apis, [], None, "developer", CFG, NOW)
    rule_bucket = next(c for c in out if c.kind == "top_failed_rule" and c.candidate_id.endswith("rule_common"))
    assert rule_bucket.figures["apis"] == 8
    assert rule_bucket.figures["fail"] == 160
    assert rule_bucket.figures["error"] == 0
