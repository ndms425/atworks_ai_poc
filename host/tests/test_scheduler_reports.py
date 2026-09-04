import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksSessionContext,
    JobDraft,
    JobKind,
    JobSchedule,
)
from atworks_host.mock_backend import MockAtworks
from atworks_host.reports import Reports
from atworks_host.scheduler import Scheduler

KST = timezone(timedelta(hours=9))
SESSION = AtworksSessionContext(session_id="sched", project_id="mes", operator="scheduler")
FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"


async def test_tick_runs_due_jobs_only_and_writes_report(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    sched = Scheduler(backend, reports, SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.SCHEDULED_RUN, summary="3일 09시", api_ids=["api-001", "api-003"], target_env="dev",
        schedule=JobSchedule(kind="daily", at="09:00", tz="Asia/Seoul", from_date="2026-09-04", count=3)), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)

    assert await sched.tick(datetime(2026, 9, 4, 8, 59, tzinfo=KST)) == []
    assert await sched.tick(datetime(2026, 9, 4, 9, 0, tzinfo=KST)) == [job.job_id]
    assert await sched.tick(datetime(2026, 9, 4, 9, 30, tzinfo=KST)) == []          # 같은 날 두 번 안 돈다
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    assert backend.ledger.get(job.job_id).runs_remaining == 1

    data = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    assert data["provenance"]["generator"] == "refresh_runner"
    assert data["summary"]["total"] == 4 and data["summary"]["fail"] == 2
    html = reports.read_html(job.job_id)
    assert "refundAmount >= 0" in html and "<script id=\"report-data\"" in html


async def test_run_now_is_due_immediately(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    sched = Scheduler(backend, Reports(tmp_path), SESSION)
    job = await backend.stage_job(SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="now", api_ids=["api-001"], target_env="dev"), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    assert await sched.tick(datetime(2026, 9, 3, 14, tzinfo=KST)) == [job.job_id]
    assert await sched.tick(datetime(2026, 9, 3, 15, tzinfo=KST)) == []
