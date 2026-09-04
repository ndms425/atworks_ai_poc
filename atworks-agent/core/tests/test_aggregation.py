from datetime import UTC, datetime, timedelta

from atworks_agent.aggregation import GROUP_BY, aggregate, summarize_insights
from atworks_agent.config import AtworksAgentConfig
from atworks_agent.types import ApiSpec, RunResult, RunStatus

T0 = datetime(2026, 9, 1, 0, tzinfo=UTC)


def run(i, api, status, at_h, rules=(), env="dev", data=None, http=200, dur=100):
    return RunResult(run_id=f"run-{i}", api_id=api, executed_at=T0 + timedelta(hours=at_h), target_env=env,
                     test_data_label=data, status=status, failed_rules=list(rules), http_status=http, duration_ms=dur)


APIS = {
    "api-A": ApiSpec(api_id="api-A", method="POST", path="/v1/pay/approve", name="승인", group="pay",
                     updated_at=T0 + timedelta(hours=5), has_rules=True),
    "api-B": ApiSpec(api_id="api-B", method="GET", path="/v1/users", name="목록", group="user",
                     updated_at=T0 - timedelta(days=10), has_rules=False),
}
RUNS = [
    run(1, "api-A", RunStatus.PASS, 1, dur=100),
    run(2, "api-A", RunStatus.FAIL, 10, rules=["amount <= limit"], dur=300),
    run(3, "api-A", RunStatus.PASS, 20, dur=120),
    run(4, "api-A", RunStatus.FAIL, 30, rules=["amount <= limit", "id != null"], dur=110),
    run(5, "api-B", RunStatus.ERROR, 12, http=503, dur=40),
    run(6, "api-B", RunStatus.PASS, 40, dur=60),
]


def test_group_by_api_computes_first_failure_last_pass_and_transitions():
    groups = aggregate(RUNS, APIS, "api", flaky_min_transitions=2)
    assert [g.key for g in groups] == ["api-A", "api-B"]           # fail+error desc, then count desc
    a = groups[0]
    assert a.label == "POST /v1/pay/approve"
    assert (a.count, a.fail, a.error, a.passed) == (4, 2, 0, 2)
    assert a.first_non_pass_at == T0 + timedelta(hours=10)
    assert a.last_pass_before == T0 + timedelta(hours=1)
    assert a.transitions == 3 and a.flaky is True
    assert a.latest_status is RunStatus.FAIL
    assert a.run_ids == ["run-4", "run-3", "run-2", "run-1"]        # newest first
    assert a.p95_duration_ms == 300                                  # nearest-rank of [100,110,120,300]
    assert a.regression_suspect is True and a.api_updated_at == APIS["api-A"].updated_at


def test_regression_suspect_needs_a_pass_before_the_update():
    b = aggregate(RUNS, APIS, "api", flaky_min_transitions=2)[1]
    assert b.last_pass_before is None and b.regression_suspect is False
    assert b.transitions == 1 and b.flaky is False


def test_group_by_failed_rule_puts_a_two_rule_run_in_two_groups_and_errors_by_http():
    groups = aggregate(RUNS, APIS, "failed_rule", flaky_min_transitions=2)
    keys = {g.key: g for g in groups}
    assert keys["amount <= limit"].count == 2 and keys["id != null"].count == 1
    assert keys["(error) HTTP 503"].error == 1
    assert all(g.passed == 0 for g in groups)                        # pass runs excluded on this axis


def test_group_by_http_status_and_env_and_api_env_data():
    by_http = {g.key for g in aggregate(RUNS, APIS, "http_status", flaky_min_transitions=2)}
    assert by_http == {"200", "503"}
    assert [g.key for g in aggregate(RUNS, APIS, "env", flaky_min_transitions=2)] == ["dev"]
    cells = aggregate(RUNS, APIS, "api_env_data", flaky_min_transitions=2)
    assert cells[0].key == "api-A|dev|-"


def test_empty_input_and_unknown_axis():
    assert aggregate([], APIS, "api", flaky_min_transitions=2) == []
    try:
        aggregate(RUNS, APIS, "nope", flaky_min_transitions=2)
    except ValueError as error:
        assert "group_by" in str(error)
    else:
        raise AssertionError("unknown axis must raise")
    assert set(GROUP_BY) == {"api", "failed_rule", "http_status", "env", "api_env_data"}


def test_summarize_insights_counts_flaky_cells_and_regressions():
    ins = summarize_insights(RUNS, APIS, AtworksAgentConfig(model="m"))
    assert ins.flaky == 1 and ins.regression_suspect == 1
