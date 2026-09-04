import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksBackend,
    AtworksSessionContext,
    JobDraft,
    JobKind,
    JobSchedule,
    JobSpec,
    JobStatus,
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
        kind=JobKind.SCHEDULED_RUN, summary="3일 09시", api_ids=["api-001", "api-003"], target_envs=["dev"],
        schedules=[JobSchedule(kind="daily", at="09:00", tz="Asia/Seoul", from_date="2026-09-04", count=3)]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)

    assert await sched.tick(datetime(2026, 9, 4, 8, 59, tzinfo=KST)) == []
    assert await sched.tick(datetime(2026, 9, 4, 9, 0, tzinfo=KST)) == [job.job_id]
    assert await sched.tick(datetime(2026, 9, 4, 9, 30, tzinfo=KST)) == []          # 같은 날 두 번 안 돈다
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    assert backend.ledger.get(job.job_id).remaining_executions == 1

    data = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    assert data["provenance"]["generator"] == "refresh_runner"
    assert data["summary"]["total"] == 4 and data["summary"]["fail"] == 2
    html = reports.read_html(job.job_id)
    assert "refundAmount >= 0" in html and "<script id=\"report-data\"" in html


async def test_run_now_is_due_immediately(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    sched = Scheduler(backend, Reports(tmp_path), SESSION)
    job = await backend.stage_job(SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="now", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    assert await sched.tick(datetime(2026, 9, 3, 14, tzinfo=KST)) == [job.job_id]
    assert await sched.tick(datetime(2026, 9, 3, 15, tzinfo=KST)) == []


async def test_report_html_escapes_json_and_uses_client_side_escaping(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    job = await backend.stage_job(
        SESSION,
        JobDraft(kind=JobKind.RUN_NOW, summary="</script><img src=x onerror=alert(1)>",
                 api_ids=["api-001"], target_envs=["dev"]),
        ActorKind.AGENT,
    )
    await backend.apply_job(SESSION, job.job_id)
    run = RunResult(run_id="run-9001", api_id="api-001", executed_at=datetime.now(UTC), target_env="dev",
                    status=RunStatus.FAIL, failed_rules=["<b>x</b>"], http_status=200, job_id=job.job_id)
    backend.runs[run.run_id] = run
    applied = backend.ledger.record_execution(job.job_id, [run.run_id], None)

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

        async def execute_job_once(self, session, job_id, schedule_index=None):
            if job_id == self.bad_job_id:
                raise RuntimeError("boom")
            return await super().execute_job_once(session, job_id, schedule_index)

    backend = FlakyBackend(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    sched = Scheduler(backend, reports, SESSION)
    bad = await backend.stage_job(
        SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="bad", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT
    )
    good = await backend.stage_job(
        SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="good", api_ids=["api-002"], target_envs=["dev"]), ActorKind.AGENT
    )
    backend.bad_job_id = bad.job_id
    await backend.apply_job(SESSION, bad.job_id)
    await backend.apply_job(SESSION, good.job_id)

    executed = await sched.tick(datetime(2026, 9, 3, 14, tzinfo=KST))

    assert executed == [good.job_id]
    bad_job = backend.ledger.get(bad.job_id)
    assert bad_job.remaining_executions == 0
    assert len(bad_job.guardrail_notes) == 1 and "execution failed" in bad_job.guardrail_notes[0]


async def test_report_failure_does_not_consume_a_second_slot(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)

    class BrokenReports(Reports):
        def write(self, job, runs, *, generator="refresh_runner"):
            raise OSError("disk full")

    sched = Scheduler(backend, BrokenReports(tmp_path), SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.SCHEDULED_RUN, summary="3일 09시", api_ids=["api-001"], target_envs=["dev"],
        schedules=[JobSchedule(kind="daily", at="09:00", tz="Asia/Seoul", from_date="2026-09-04", count=3)]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    assert await sched.tick(datetime(2026, 9, 4, 9, 0, tzinfo=KST)) == [job.job_id]
    after = backend.ledger.get(job.job_id)
    assert after.remaining_executions == 2                      # exactly one slot consumed
    assert len(after.run_ids) == 1                        # the run was produced
    assert any(n.startswith("report failed") for n in after.guardrail_notes)
    assert not any(n.startswith("execution failed") for n in after.guardrail_notes)


def test_read_html_rejects_an_unsafe_id(tmp_path):
    reports = Reports(tmp_path)
    with pytest.raises(ValueError):
        reports.read_html("..\\x")


def test_all_runs_rejects_an_unsafe_id(tmp_path):
    reports = Reports(tmp_path)
    with pytest.raises(ValueError):
        reports.all_runs("../secret")


class RecordingBackend(AtworksBackend):
    """A minimal AtworksBackend the scheduler must be able to run against without ever
    reaching for a `ledger` or `runs` attribute (R44/I3) — the seam a REST backend
    would implement."""

    def __init__(self, job: JobSpec, run: RunResult):
        self.job = job
        self.run = run
        self.calls: list[str] = []

    async def search_apis(self, session, query="", updated_after=None, group=None, limit=20):
        raise NotImplementedError

    async def get_api(self, session, api_id):
        raise NotImplementedError

    async def list_runs(self, session, since=None, status=None, api_id=None, limit=50):
        raise NotImplementedError

    async def get_run(self, session, run_id):
        raise NotImplementedError

    async def count_runs(self, session, since, status):
        raise NotImplementedError

    async def stage_job(self, session, draft, actor_kind):
        raise NotImplementedError

    async def get_pending_jobs(self, session):
        raise NotImplementedError

    async def apply_job(self, session, job_id):
        raise NotImplementedError

    async def discard_job(self, session, job_id, actor_kind):
        raise NotImplementedError

    async def execute_job_once(self, session, job_id, schedule_index=None):
        self.calls.append("execute_job_once")
        self.job = self.job.model_copy(update={"run_ids": [self.run.run_id], "executions": 1})
        return [self.run]

    async def get_job(self, session, job_id):
        self.calls.append("get_job")
        return self.job

    async def applied_jobs(self, session):
        self.calls.append("applied_jobs")
        return [self.job]

    async def all_jobs(self, session):
        self.calls.append("all_jobs")
        return [self.job]

    async def runs_by_ids(self, session, run_ids):
        self.calls.append("runs_by_ids")
        return [self.run] if run_ids else []

    async def record_execution(self, session, job_id, run_ids, schedule_index):
        self.calls.append("record_execution")
        return self.job

    async def add_guardrail_note(self, session, job_id, note):
        self.calls.append("add_guardrail_note")
        return self.job


async def test_scheduler_uses_only_the_backend_abc(tmp_path):
    now = datetime.now(UTC)
    job = JobSpec(job_id="job-01", kind=JobKind.RUN_NOW, status=JobStatus.APPLIED, summary="s",
                 api_ids=["api-1"], target_envs=["dev"], report=True, created_at=now, created_by="op",
                 executions=0)
    run = RunResult(run_id="run-x", api_id="api-1", executed_at=now, target_env="dev",
                    status=RunStatus.PASS, job_id="job-01")
    backend = RecordingBackend(job, run)
    assert not hasattr(backend, "ledger")
    assert not hasattr(backend, "runs")

    sched = Scheduler(backend, Reports(tmp_path), SESSION)
    executed = await sched.tick(now)

    assert executed == ["job-01"]
    assert "execute_job_once" in backend.calls
    assert "applied_jobs" in backend.calls
    assert not hasattr(backend, "ledger")
    assert not hasattr(backend, "runs")
