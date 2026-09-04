import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksSessionContext,
    JobDraft,
    JobKind,
    JobSchedule,
    RunResult,
    RunStatus,
)
from atworks_host.mock_backend import MockAtworks
from atworks_host.reports import TEMPLATE, Reports
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


async def test_report_html_escapes_json_and_uses_client_side_escaping(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    job = await backend.stage_job(
        SESSION,
        JobDraft(kind=JobKind.RUN_NOW, summary="</script><img src=x onerror=alert(1)>",
                 api_ids=["api-001"], target_env="dev"),
        ActorKind.AGENT,
    )
    await backend.apply_job(SESSION, job.job_id)
    run = RunResult(run_id="run-9001", api_id="api-001", executed_at=datetime.now(UTC), target_env="dev",
                    status=RunStatus.FAIL, failed_rules=["<b>x</b>"], http_status=200, job_id=job.job_id)
    backend.runs[run.run_id] = run
    applied = backend.ledger.record_execution(job.job_id, [run.run_id])

    path = reports.write(applied, [run])
    html = path.read_text(encoding="utf-8")

    assert "<\\/script>" in html
    assert "</script><img" not in html
    assert html.count("</script>") == 2

    template_source = TEMPLATE.read_text(encoding="utf-8")
    assert "esc(" in template_source


async def test_tick_survives_one_jobs_execution_error(tmp_path):
    class FlakyBackend(MockAtworks):
        bad_job_id: str | None = None

        async def execute_job_once(self, session, job_id):
            if job_id == self.bad_job_id:
                raise RuntimeError("boom")
            return await super().execute_job_once(session, job_id)

    backend = FlakyBackend(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    sched = Scheduler(backend, reports, SESSION)
    bad = await backend.stage_job(
        SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="bad", api_ids=["api-001"], target_env="dev"), ActorKind.AGENT
    )
    good = await backend.stage_job(
        SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="good", api_ids=["api-002"], target_env="dev"), ActorKind.AGENT
    )
    backend.bad_job_id = bad.job_id
    await backend.apply_job(SESSION, bad.job_id)
    await backend.apply_job(SESSION, good.job_id)

    executed = await sched.tick(datetime(2026, 9, 3, 14, tzinfo=KST))

    assert executed == [good.job_id]
    bad_job = backend.ledger.get(bad.job_id)
    assert bad_job.runs_remaining == 0
    assert len(bad_job.guardrail_notes) == 1 and "execution failed" in bad_job.guardrail_notes[0]
