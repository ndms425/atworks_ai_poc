from datetime import UTC, datetime, timedelta

import pytest

from atworks_agent.jobs import SelectWhere
from atworks_agent.selection import resolve_select_where
from atworks_agent.types import ApiSpec, AtworksSessionContext, RunResult, RunStatus

from .conftest import InMemoryBackend

T0 = datetime(2026, 9, 1, 9, tzinfo=UTC)
SESSION = AtworksSessionContext(session_id="s", project_id="p", operator="o")


@pytest.fixture
def wide(config):
    b = InMemoryBackend(config)
    b.apis["api-3"] = ApiSpec(api_id="api-3", method="POST", path="/v1/payments/refund", name="환불", group="payment", updated_at=T0, has_rules=True)
    b.apis["api-4"] = ApiSpec(api_id="api-4", method="POST", path="/v1/payments/approve", name="승인", group="payment", updated_at=T0, has_rules=True)
    b.apis["api-5"] = ApiSpec(api_id="api-5", method="GET", path="/v1/users", name="목록", group="user", updated_at=T0, has_rules=False)
    b.runs.append(RunResult(run_id="run-9", api_id="api-4", executed_at=T0 + timedelta(hours=1), target_env="dev", status=RunStatus.FAIL, failed_rules=["x"]))
    return b


async def test_related_to_matches_same_group_or_same_path_prefix(wide, config):
    res = await resolve_select_where(wide, SESSION, SelectWhere(related_to="api-3"), config, now=T0 + timedelta(days=1))
    assert set(res.api_ids) == {"api-3", "api-4"}          # payment group / /v1/payments prefix; api-1 (contract) excluded
    assert "api-3" in res.basis and "payment" in res.basis and "/v1/payments/*" in res.basis and "2" in res.basis


async def test_failed_since_keeps_only_apis_with_a_non_pass_run_in_the_window(wide, config):
    res = await resolve_select_where(wide, SESSION, SelectWhere(failed_since=T0 - timedelta(hours=1)), config, now=T0 + timedelta(days=1))
    assert set(res.api_ids) == {"api-1", "api-2", "api-4"}  # run-1 fail, run-2 error, run-9 fail; api-3/5 never failed
    assert "실패" in res.basis or "failed" in res.basis


async def test_conditions_are_anded(wide, config):
    res = await resolve_select_where(wide, SESSION, SelectWhere(related_to="api-3", failed_since=T0 - timedelta(hours=1)), config, now=T0)
    assert res.api_ids == ["api-4"]


async def test_unknown_anchor_resolves_to_nothing(wide, config):
    res = await resolve_select_where(wide, SESSION, SelectWhere(related_to="api-404"), config, now=T0)
    assert res.api_ids == [] and res.basis is None


async def test_plain_query_group_updated_after_still_work_with_no_basis(wide, config):
    res = await resolve_select_where(wide, SESSION, SelectWhere(group="payment"), config, now=T0)
    assert set(res.api_ids) == {"api-3", "api-4"} and res.basis is None
