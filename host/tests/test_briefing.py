import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from atworks_agent import ActorKind, AtworksAgentConfig, AtworksSessionContext, JobDraft, JobKind
from atworks_host.briefing import Briefings
from atworks_host.mock_backend import MockAtworks
from atworks_host.reports import Reports
from atworks_host.scheduler import Scheduler

KST = timezone(timedelta(hours=9))
SESSION = AtworksSessionContext(session_id="b", project_id="mes", operator="scheduler")
FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"


def _stack(tmp_path):
    cfg = AtworksAgentConfig(model="m")
    backend = MockAtworks(cfg, FIXTURES)
    briefings = Briefings(tmp_path / "b", cfg, portal_origin="http://localhost:3110")
    return cfg, backend, briefings


async def test_generate_writes_data_and_html_for_the_window(tmp_path):
    _, backend, briefings = _stack(tmp_path)
    now = datetime(2026, 9, 3, 9, 5, tzinfo=KST)          # window: 09-02 09:00 → 09-03 09:00 KST
    path = await briefings.generate(backend, SESSION, now)
    data = json.loads((path.parent / "data.json").read_text(encoding="utf-8"))
    assert data["date"] == "2026-09-03"
    assert data["counts"]["total"] == 12 and data["counts"]["fail"] == 4 and data["counts"]["error"] == 1
    assert data["top_groups"][0]["key"] == "refundAmount >= 0"
    assert "flaky" in data["insights"] and "pending" in data["jobs"]
    html = briefings.read_html("2026-09-03")
    assert "refundAmount" in html


async def test_is_due_only_after_briefing_at_and_once_per_day(tmp_path):
    _, backend, briefings = _stack(tmp_path)
    assert briefings.is_due(datetime(2026, 9, 3, 8, 59, tzinfo=KST)) is False
    assert briefings.is_due(datetime(2026, 9, 3, 9, 0, tzinfo=KST)) is True
    assert await briefings.maybe_generate(backend, SESSION, datetime(2026, 9, 3, 9, 0, tzinfo=KST)) == "2026-09-03"
    assert await briefings.maybe_generate(backend, SESSION, datetime(2026, 9, 3, 15, 0, tzinfo=KST)) is None
    assert briefings.latest()["date"] == "2026-09-03"


async def test_scheduler_tick_generates_the_briefing_and_keeps_running_jobs(tmp_path):
    _, backend, briefings = _stack(tmp_path)
    sched = Scheduler(backend, Reports(tmp_path / "r"), SESSION, briefings=briefings)
    job = await backend.stage_job(SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="now", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    assert await sched.tick(datetime(2026, 9, 3, 9, 1, tzinfo=KST)) == [job.job_id]
    assert briefings.latest()["date"] == "2026-09-03"


async def test_briefing_failure_does_not_stop_the_tick(tmp_path, monkeypatch):
    _, backend, briefings = _stack(tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("disk")

    monkeypatch.setattr(briefings, "generate", boom)
    sched = Scheduler(backend, Reports(tmp_path / "r"), SESSION, briefings=briefings)
    assert await sched.tick(datetime(2026, 9, 3, 9, 1, tzinfo=KST)) == []


def test_briefing_modules_import_no_model_client():
    import importlib

    for name in ("atworks_host.briefing", "atworks_host.scheduler"):
        importlib.import_module(name)
    src = (Path(__file__).resolve().parents[1] / "atworks_host" / "briefing.py").read_text(encoding="utf-8")
    # "AtworksAgent" alone would false-positive on the plain-pydantic AtworksAgentConfig import
    # that build_briefing's signature requires; atworks_agent_runtime is the module that actually
    # carries the model client (AtworksAgent, orchestrator.py) and pulls in anthropic.
    assert "anthropic" not in src and "atworks_agent_runtime" not in src
