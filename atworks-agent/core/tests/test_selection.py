from datetime import UTC, datetime, timedelta

import pytest

from atworks_agent import selection
from atworks_agent.config import AtworksAgentConfig
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


async def test_failed_since_basis_flags_when_the_scan_hits_the_sample_cap(wide, config, monkeypatch):
    # Fix round 1: a failed_since selection that ALSO carries a catalogue predicate still starts
    # from a catalogue page capped at CATALOGUE_SCAN_LIMIT. When that page comes back at the cap,
    # APIs past it were never considered, so the basis sentence must say so.
    monkeypatch.setattr(selection, "CATALOGUE_SCAN_LIMIT", 1)
    monkeypatch.setattr(selection, "_API_PAGE", 1)

    res = await resolve_select_where(
        wide, SESSION, SelectWhere(group="payment", failed_since=T0 - timedelta(hours=1)), config, now=T0)

    assert res.basis is not None and "표본 상한" in res.basis


async def test_failed_since_alone_is_not_cut_by_the_catalogue_page(wide, config, monkeypatch):
    """Fix round 1 (F): with no catalogue predicate to satisfy, the watermark set IS the answer
    and a 1-row catalogue page must not cut it. The old order intersected the exact watermark
    result INTO a <=CATALOGUE_SCAN_LIMIT catalogue page, so a project with more APIs than that cap
    silently lost every failing API outside the newest-updated 1000."""
    monkeypatch.setattr(selection, "CATALOGUE_SCAN_LIMIT", 1)
    monkeypatch.setattr(selection, "_API_PAGE", 1)

    res = await resolve_select_where(wide, SESSION, SelectWhere(failed_since=T0 - timedelta(hours=1)), config, now=T0)

    assert set(res.api_ids) == {"api-1", "api-2", "api-4"}   # every failing API, catalogue cap or not
    assert res.basis is not None and "표본 상한" not in res.basis


async def test_failed_since_alone_flags_the_per_job_cap_instead(wide, monkeypatch):
    """...and the truncation it CAN hit -- more failing APIs than a job may carry -- is still
    named in the sentence, so the operator never reads a silently short selection."""
    cfg = AtworksAgentConfig(model="m", max_apis_per_job=1)
    monkeypatch.setattr(selection, "CATALOGUE_SCAN_LIMIT", 1)

    res = await resolve_select_where(wide, SESSION, SelectWhere(failed_since=T0 - timedelta(hours=1)), cfg, now=T0)

    assert len(res.api_ids) == cfg.max_apis_per_job + 1 == 2
    assert res.basis is not None and "표본 상한" in res.basis


async def test_related_to_truncation_is_named_in_the_basis_sentence(wide, config, monkeypatch):
    """Fix round 1 (F): a related_to-only selection could hit CATALOGUE_SCAN_LIMIT on either half
    of its union and the sentence said nothing — the one truncation the basis used to swallow."""
    monkeypatch.setattr(selection, "CATALOGUE_SCAN_LIMIT", 1)
    monkeypatch.setattr(selection, "_API_PAGE", 1)

    res = await resolve_select_where(wide, SESSION, SelectWhere(related_to="api-3"), config, now=T0)

    assert res.basis is not None and "표본 상한" in res.basis


async def test_the_sample_cap_is_named_once_even_when_both_clauses_truncate(wide, config, monkeypatch):
    """Task 8 re-review minor: `related_to` and `failed_since` read the SAME truncated catalogue
    scan, so both sentences used to stamp `(표본 상한 도달)` and the basis read like two separate
    caps were hit. One selection, one cap, one note."""
    monkeypatch.setattr(selection, "CATALOGUE_SCAN_LIMIT", 1)
    monkeypatch.setattr(selection, "_API_PAGE", 1)

    res = await resolve_select_where(
        wide, SESSION, SelectWhere(related_to="api-3", failed_since=T0 - timedelta(hours=1)),
        config, now=T0,
    )

    assert res.basis is not None
    assert res.basis.count("표본 상한 도달") == 1


async def test_failed_since_is_exact_and_not_a_run_sample(config):
    # The old path paged max_aggregate_runs non-pass runs and intersected; an API whose only
    # failure fell outside that sample vanished from the selection. The watermark read has no
    # sample at all, so a tiny cap changes nothing.
    tiny = config.model_copy(update={"max_aggregate_runs": 1})
    b = InMemoryBackend(tiny)
    res = await resolve_select_where(b, SESSION, SelectWhere(failed_since=T0 - timedelta(hours=1)), tiny, now=T0)
    assert set(res.api_ids) == {"api-1", "api-2"}


async def test_ordering_breaks_updated_at_ties_by_api_id_for_determinism(config):
    # Two APIs with the same updated_at must sort in a stable, deterministic order between
    # stage-time and a LATE re-resolution — never dependent on dict/insertion order.
    b = InMemoryBackend(config)
    b.apis["api-1"] = b.apis["api-1"].model_copy(update={"updated_at": T0})
    b.apis["api-2"] = b.apis["api-2"].model_copy(update={"updated_at": T0})

    res = await resolve_select_where(b, SESSION, SelectWhere(), config, now=T0)

    assert res.api_ids == ["api-2", "api-1"]  # same updated_at -> api_id descending


async def test_resolution_is_capped_to_max_apis_per_job_plus_one():
    # An oversized selection (here via related_to, which scans the whole catalogue) must still
    # come back bounded so it trips the job guardrail with a short list rather than flushing
    # every match into the provenance map.
    cfg = AtworksAgentConfig(model="m", max_apis_per_job=1)
    b = InMemoryBackend(cfg)
    b.apis["api-3"] = ApiSpec(api_id="api-3", method="GET", path="/v1/contracts/history", name="이력",
                              group="contract", updated_at=T0, has_rules=False)

    res = await resolve_select_where(b, SESSION, SelectWhere(related_to="api-1"), cfg, now=T0)

    assert len(res.api_ids) == cfg.max_apis_per_job + 1 == 2
