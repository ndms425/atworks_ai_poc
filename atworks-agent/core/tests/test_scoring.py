from datetime import UTC, datetime, timedelta

import pytest

from atworks_agent.scoring import SCORERS, UnknownScorer, rank_runs
from atworks_agent.types import ApiSpec, RunResult, RunStatus

T0 = datetime(2026, 9, 1, 9, tzinfo=UTC)


def _run(run_id, api_id, status, minutes, http=200, rules=()):
    return RunResult(run_id=run_id, api_id=api_id, executed_at=T0 + timedelta(minutes=minutes),
                     target_env="dev", status=status, failed_rules=list(rules), http_status=http)


def _api(api_id, has_rules=True):
    return ApiSpec(api_id=api_id, method="POST", path=f"/{api_id}", name=api_id,
                   updated_at=T0, has_rules=has_rules)


def test_risk_v1_orders_repeated_5xx_first():
    runs = [
        _run("r1", "a", RunStatus.FAIL, 0, rules=["x"]),
        _run("r2", "a", RunStatus.FAIL, 10, rules=["x"]),
        _run("r3", "b", RunStatus.ERROR, 20, http=500),
        _run("r4", "b", RunStatus.ERROR, 30, http=503),
        _run("r5", "c", RunStatus.FAIL, 40, rules=["y"]),
        _run("r6", "d", RunStatus.PASS, 50),
    ]
    apis = {i: _api(i) for i in "abcd"}
    ranked = rank_runs("risk_v1", runs, apis, limit=3)
    assert [r.api_id for r in ranked] == ["b", "a", "c"]
    assert all(r.reasons for r in ranked)
    assert all(r.scorer == "risk_v1" for r in ranked)


def test_pass_runs_are_never_ranked():
    ranked = rank_runs("risk_v1", [_run("r1", "a", RunStatus.PASS, 0)], {"a": _api("a")}, limit=5)
    assert ranked == []


def test_unknown_scorer_raises():
    with pytest.raises(UnknownScorer):
        rank_runs("vibes", [], {}, limit=5)


def test_repeated_non_pass_reason_does_not_claim_consecutive():
    runs = [
        _run("r1", "a", RunStatus.FAIL, 0, rules=["x"]),
        _run("r2", "a", RunStatus.FAIL, 10, rules=["x"]),
    ]
    ranked = rank_runs("risk_v1", runs, {"a": _api("a")}, limit=5)
    assert any("non-pass runs on this API in the window" in r for r in ranked[0].reasons)
    assert not any("consecutive" in r for r in ranked[0].reasons)


def test_deterministic():
    runs = [_run("r1", "a", RunStatus.FAIL, 0, rules=["x"]), _run("r2", "b", RunStatus.FAIL, 1, rules=["x"])]
    apis = {"a": _api("a"), "b": _api("b")}
    assert rank_runs("risk_v1", runs, apis, 5) == rank_runs("risk_v1", runs, apis, 5)
    assert "risk_v1" in SCORERS
