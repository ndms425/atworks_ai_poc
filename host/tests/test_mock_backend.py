from datetime import datetime, timedelta, timezone
from pathlib import Path

from atworks_agent import ActorKind, AtworksAgentConfig, AtworksSessionContext, JobDraft, JobKind
from atworks_host.mock_backend import MockAtworks

KST = timezone(timedelta(hours=9))
SESSION = AtworksSessionContext(session_id="s", project_id="mes", operator="minseong", now=datetime(2026, 9, 3, 14, tzinfo=KST))


def _backend():
    return MockAtworks(AtworksAgentConfig(model="m"), Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures")


async def test_fixtures_load_and_search_by_updated_after():
    b = _backend()
    recent = await b.search_apis(SESSION, updated_after=datetime(2026, 8, 27, tzinfo=KST), limit=100)
    assert 0 < len(recent) < 12 and all(a.updated_at >= datetime(2026, 8, 27, tzinfo=KST) for a in recent)


async def test_count_runs_is_population_not_limit():
    b = _backend()
    shown = await b.list_runs(SESSION, status="fail", limit=2)
    total = await b.count_runs(SESSION, since=None, status="fail")
    assert len(shown) == 2 and total > 2


async def test_execute_job_once_is_deterministic_and_records_runs():
    b = _backend()
    job = await b.stage_job(SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001", "api-003"], target_envs=["dev"]), ActorKind.AGENT)
    await b.apply_job(SESSION, job.job_id)
    first = await b.execute_job_once(SESSION, job.job_id)
    statuses = {r.api_id: r.status.value for r in first}
    assert statuses == {"api-001": "pass", "api-003": "fail"}
    assert all(r.job_id == job.job_id for r in first)
    assert (await b.get_run(SESSION, first[0].run_id)) is not None


async def test_late_binding_over_limit_skips_execution_and_notes_guardrail():
    config = AtworksAgentConfig(model="m", max_apis_per_job=3)
    b = MockAtworks(config, Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures")
    job = await b.stage_job(
        SESSION,
        JobDraft(kind=JobKind.RUN_NOW, summary="late", api_ids=["api-001", "api-002", "api-003"],
                 target_envs=["dev"], binding="LATE", select_where={"query": ""}),
        ActorKind.AGENT,
    )
    await b.apply_job(SESSION, job.job_id)
    before = b.ledger.get(job.job_id).remaining_executions
    produced = await b.execute_job_once(SESSION, job.job_id)
    assert produced == []
    updated = b.ledger.get(job.job_id)
    assert len(updated.guardrail_notes) == 1
    assert "LATE" in updated.guardrail_notes[0]
    # the slot is consumed even though nothing ran, so the schedule does not spin forever
    assert updated.remaining_executions == before - 1

    # no slots remain: execute_job_once must not add a second note or consume another slot
    again = await b.execute_job_once(SESSION, job.job_id)
    assert again == []
    final = b.ledger.get(job.job_id)
    assert len(final.guardrail_notes) == 1
    assert final.remaining_executions == 0
