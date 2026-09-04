"""Flaky (flappy pass/fail with no spec change nearby) and regression-suspect (a fail that
follows a spec update) are different signals — flaky_v1's transitions >= flaky_min_transitions
vs. an api.updated_at that lands strictly between the last pass and the first non_pass. This
test builds one api of each shape and checks summarize_insights tells them apart. It replaces
backend.runs and the two apis' updated_at wholesale with a purpose-built scenario — runs.json
stays frozen (its own coverage is test_app.py's
test_runs_insights_counts_flaky_and_regression_from_fixtures)."""
from datetime import UTC, datetime, timedelta
from pathlib import Path

from atworks_agent import AtworksAgentConfig, RunResult, RunStatus
from atworks_agent.aggregation import aggregate, summarize_insights
from atworks_host.mock_backend import MockAtworks

FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"


async def test_flaky_and_regression_are_attributed_to_different_apis():
    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    now = datetime.now(UTC)

    FLAKY_API = "api-001"        # pass -> fail -> pass: 2 transitions, flaky_min_transitions is 2
    REGRESSION_API = "api-002"   # one pass then one fail: 1 transition, below the flaky threshold

    # updated_at sits well outside the last-pass..first-fail window, so this api must not also
    # be picked up as a regression suspect.
    backend.apis[FLAKY_API] = backend.apis[FLAKY_API].model_copy(update={"updated_at": now - timedelta(days=10)})
    # updated_at lands strictly between the pass and the fail below.
    backend.apis[REGRESSION_API] = backend.apis[REGRESSION_API].model_copy(update={"updated_at": now - timedelta(days=1, hours=12)})

    flaky_runs = [
        RunResult(run_id="run-flaky-1", api_id=FLAKY_API, executed_at=now - timedelta(days=2),
                  target_env="dev", status=RunStatus.PASS),
        RunResult(run_id="run-flaky-2", api_id=FLAKY_API, executed_at=now - timedelta(days=2) + timedelta(hours=1),
                  target_env="dev", status=RunStatus.FAIL, failed_rules=["x"]),
        RunResult(run_id="run-flaky-3", api_id=FLAKY_API, executed_at=now - timedelta(days=2) + timedelta(hours=2),
                  target_env="dev", status=RunStatus.PASS),
    ]
    regression_runs = [
        RunResult(run_id="run-regr-1", api_id=REGRESSION_API, executed_at=now - timedelta(days=2),
                  target_env="dev", status=RunStatus.PASS),
        RunResult(run_id="run-regr-2", api_id=REGRESSION_API, executed_at=now - timedelta(days=1),
                  target_env="dev", status=RunStatus.FAIL, failed_rules=["y"]),
    ]
    # Wholesale replacement — runs.json's own fixtures play no part in this scenario.
    backend.runs = {r.run_id: r for r in (*flaky_runs, *regression_runs)}

    found = summarize_insights(list(backend.runs.values()), backend.apis, config)

    assert found.flaky == 1
    assert found.regression_suspect == 1

    by_api = aggregate(list(backend.runs.values()), backend.apis, "api", flaky_min_transitions=config.flaky_min_transitions)
    flaky_keys = {g.key for g in by_api if g.flaky}
    regression_keys = {g.key for g in by_api if g.regression_suspect}
    assert flaky_keys == {FLAKY_API}
    assert regression_keys == {REGRESSION_API}
