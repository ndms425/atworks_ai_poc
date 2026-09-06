import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from atworks_agent import AtworksAgentConfig, AtworksSessionContext, InsightNarrative
from atworks_host.insights import InsightPanels
from atworks_host.mock_backend import MockAtworks

FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"


def _redate_fixture_runs(backend: MockAtworks, now: datetime) -> None:
    # Same trick as test_app.py: shift every fixture run/api timestamp by the delta from the
    # fixture's own reference date, so "now" always sees fresh, in-window data regardless of
    # when the suite actually runs.
    for run in list(backend.runs.values()):
        delta = datetime(2026, 9, 4, tzinfo=UTC) - run.executed_at
        backend.runs[run.run_id] = run.model_copy(update={"executed_at": now - delta})
    for api in list(backend.apis.values()):
        delta = datetime(2026, 9, 4, tzinfo=UTC) - api.updated_at
        backend.apis[api.api_id] = api.model_copy(update={"updated_at": now - delta})


def _make_backend(now: datetime) -> tuple[AtworksAgentConfig, MockAtworks]:
    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    _redate_fixture_runs(backend, now)
    return config, backend


def _session(operator: str, now: datetime, role: str = "developer") -> AtworksSessionContext:
    return AtworksSessionContext(session_id="s", project_id="mes", operator=operator, role=role, now=now)


def _counting_narrator(counter: dict, narrate_all: bool = True):
    async def narrator(candidates, role, *, notes=None):
        del role, notes
        counter["calls"] = counter.get("calls", 0) + 1
        if not narrate_all:
            return []
        return [
            InsightNarrative(candidate_id=c.candidate_id, headline="h", why_it_matters="w", prompt="p")
            for c in candidates
        ]
    return narrator


async def test_second_build_same_day_hits_cache_and_makes_no_narration_call(tmp_path):
    now = datetime.now(UTC)
    config, backend = _make_backend(now)
    counter: dict = {}
    panels = InsightPanels(tmp_path, config, narrator=_counting_narrator(counter))
    session = _session("minseong", now)

    p1 = await panels.build(backend, session, now)
    assert p1.items
    p2 = await panels.build(backend, session, now)
    assert counter["calls"] == 1
    assert p2.generated_by == "agent"
    assert any(item.narrative is not None for item in p2.items)


async def test_refresh_re_narrates(tmp_path):
    now = datetime.now(UTC)
    config, backend = _make_backend(now)
    counter: dict = {}
    panels = InsightPanels(tmp_path, config, narrator=_counting_narrator(counter))
    session = _session("minseong", now)

    await panels.build(backend, session, now)
    await panels.build(backend, session, now, refresh=True)
    assert counter["calls"] == 2


async def test_cache_is_per_operator_and_per_date(tmp_path):
    now = datetime.now(UTC)
    config, backend = _make_backend(now)
    counter: dict = {}
    panels = InsightPanels(tmp_path, config, narrator=_counting_narrator(counter))

    await panels.build(backend, _session("minseong", now), now)
    await panels.build(backend, _session("jihoon", now), now)
    assert counter["calls"] == 2

    date = panels._date_for(now)
    assert (tmp_path / "minseong" / date / "narrative.json").exists()
    assert (tmp_path / "jihoon" / date / "narrative.json").exists()

    tomorrow = now + timedelta(days=1)
    await panels.build(backend, _session("minseong", tomorrow), tomorrow)
    assert counter["calls"] == 3
    tomorrow_date = panels._date_for(tomorrow)
    assert tomorrow_date != date
    assert (tmp_path / "minseong" / tomorrow_date / "narrative.json").exists()
    # yesterday's file for minseong is untouched
    assert (tmp_path / "minseong" / date / "narrative.json").exists()


async def test_fallback_when_narrator_returns_empty_or_disabled(tmp_path):
    now = datetime.now(UTC)

    # (a) narrator returns []
    config, backend = _make_backend(now)
    panels = InsightPanels(tmp_path / "a", config, narrator=_counting_narrator({}, narrate_all=False))
    session = _session("minseong", now)
    panel = await panels.build(backend, session, now)
    assert panel.generated_by == "deterministic"
    assert panel.items
    assert all(item.narrative is None for item in panel.items)
    assert all(item.candidate.label for item in panel.items)

    # (b) narration disabled outright: narrator must never be called
    counter: dict = {}
    config2, backend2 = _make_backend(now)
    config2 = config2.model_copy(update={"enable_insight_narration": False})
    panels2 = InsightPanels(tmp_path / "b", config2, narrator=_counting_narrator(counter))
    panel2 = await panels2.build(backend2, _session("minseong", now), now)
    assert counter.get("calls", 0) == 0
    assert panel2.generated_by == "deterministic"
    assert all(item.narrative is None for item in panel2.items)


async def test_scope_fallback_flag_for_operator_with_no_runs(tmp_path):
    now = datetime.now(UTC)
    config, backend = _make_backend(now)
    panels = InsightPanels(tmp_path, config, narrator=_counting_narrator({}, narrate_all=False))
    session = _session("nobody", now)
    panel = await panels.build(backend, session, now)
    assert panel.scope_fallback is True
    assert panel.items
    assert panel.name == "nobody"  # list_operators doesn't know this id; falls back to the id itself


async def test_build_and_refresh_never_touch_approval_marks_or_runs(tmp_path):
    now = datetime.now(UTC)
    config, backend = _make_backend(now)
    panels = InsightPanels(tmp_path, config, narrator=_counting_narrator({}))
    session = _session("minseong", now)

    runs_before = len(backend.runs)
    ledger_pending_before = len(backend.ledger.pending())
    ledger_applied_before = len(backend.ledger.applied())
    rule_pending_before = len(backend.rule_ledger.pending())
    rule_applied_before = len(backend.rule_ledger.applied())

    await panels.build(backend, session, now)
    await panels.build(backend, session, now, refresh=True)

    assert len(backend.runs) == runs_before
    assert len(backend.ledger.pending()) == ledger_pending_before
    assert len(backend.ledger.applied()) == ledger_applied_before
    assert len(backend.rule_ledger.pending()) == rule_pending_before
    assert len(backend.rule_ledger.applied()) == rule_applied_before


def test_unsafe_operator_id_is_rejected(tmp_path):
    config = AtworksAgentConfig(model="m")
    panels = InsightPanels(tmp_path, config, narrator=_counting_narrator({}))
    with pytest.raises(ValueError):
        panels._folder("../x", "2026-09-06")


async def test_cached_narrative_for_stale_candidate_id_is_ignored(tmp_path):
    now = datetime.now(UTC)
    config, backend = _make_backend(now)
    panels = InsightPanels(tmp_path, config, narrator=_counting_narrator({}, narrate_all=False))
    session = _session("minseong", now)
    date = panels._date_for(now)
    folder = tmp_path / "minseong" / date
    folder.mkdir(parents=True)
    (folder / "narrative.json").write_text(
        json.dumps({
            "generated_at": now.isoformat(),
            "generated_by": "agent",
            "narratives": [
                {"candidate_id": "not-a-real-candidate", "headline": "h", "why_it_matters": "w", "prompt": "p"}
            ],
        }),
        encoding="utf-8",
    )
    panel = await panels.build(backend, session, now)
    assert panel.generated_by == "deterministic"
    assert all(item.narrative is None for item in panel.items)
