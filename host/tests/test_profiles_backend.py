import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksSessionContext,
    JobDraft,
    JobKind,
    ProfileDraft,
    ProfileStatus,
)
from atworks_host.mock_backend import MockAtworks
from atworks_host.reports import Reports
from atworks_host.scheduler import Scheduler

KST = timezone(timedelta(hours=9))
SESSION = AtworksSessionContext(session_id="prof", project_id="mes", operator="operator")
FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"


async def _parity_job(backend, reports):
    sched = Scheduler(backend, reports, SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="legacy/renewed 값 비교",
        api_ids=["api-002", "api-004", "api-008"], target_envs=["legacy", "renewed"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]
    return backend.ledger.get(job.job_id)


async def test_apply_profile_rediffs_stored_report_without_new_runs(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    backend.reports = reports

    job = await _parity_job(backend, reports)   # scheduler.tick already wrote the initial report

    initial = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    initial_rows = {r["api_id"]: r for r in initial["parity"]["rows"]}
    assert initial_rows["api-002"]["verdict"] == "value_diff"
    assert initial_rows["api-002"]["diff_paths"] == ["$.serverTime"]
    assert initial_rows["api-008"]["verdict"] == "value_diff"
    assert initial_rows["api-008"]["diff_paths"] == ["$.serverTime"]
    assert initial_rows["api-004"]["verdict"] == "value_diff"
    assert set(initial_rows["api-004"]["diff_paths"]) == {"$.limit", "$.serverTime"}

    run_count_before = len(backend.runs)
    statuses_before = {run_id: run.status for run_id, run in backend.runs.items()}

    draft = ProfileDraft(job_id=job.job_id, ignore_paths=["$.serverTime"], summary="ignore volatile serverTime")
    staged = await backend.stage_profile(SESSION, draft, ActorKind.OPERATOR)
    assert staged.status is ProfileStatus.STAGED

    applied = await backend.apply_profile(SESSION, staged.profile_id)

    assert applied.status is ProfileStatus.APPLIED
    assert applied.effective_from is not None

    assert len(backend.runs) == run_count_before   # no new runs
    assert {run_id: run.status for run_id, run in backend.runs.items()} == statuses_before   # past runs untouched

    after = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    rows = {r["api_id"]: r for r in after["parity"]["rows"]}
    assert rows["api-002"]["verdict"] == "equal"
    assert rows["api-008"]["verdict"] == "equal"
    assert rows["api-004"]["verdict"] == "value_diff"
    assert rows["api-004"]["diff_paths"] == ["$.limit"]
    assert after["parity"]["ignore_paths"] == ["$.serverTime"]


async def test_apply_profile_does_not_crash_when_no_report_exists(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    backend.reports = reports

    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="no report yet",
        api_ids=["api-002"], target_envs=["legacy", "renewed"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)

    draft = ProfileDraft(job_id=job.job_id, ignore_paths=["$.serverTime"], summary="never rendered")
    staged = await backend.stage_profile(SESSION, draft, ActorKind.OPERATOR)

    applied = await backend.apply_profile(SESSION, staged.profile_id)

    assert applied.status is ProfileStatus.APPLIED
    assert applied.effective_from is not None


async def test_apply_profile_skips_rediff_when_backend_has_no_reports_handle(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    assert backend.reports is None

    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="no reports handle wired",
        api_ids=["api-002"], target_envs=["legacy", "renewed"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)

    draft = ProfileDraft(job_id=job.job_id, ignore_paths=["$.serverTime"], summary="s")
    staged = await backend.stage_profile(SESSION, draft, ActorKind.OPERATOR)

    applied = await backend.apply_profile(SESSION, staged.profile_id)

    assert applied.status is ProfileStatus.APPLIED


async def test_list_and_pending_profiles(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="j", api_ids=["api-002"], target_envs=["legacy", "renewed"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)

    draft = ProfileDraft(job_id=job.job_id, ignore_paths=["$.serverTime"], summary="s")
    staged = await backend.stage_profile(SESSION, draft, ActorKind.OPERATOR)

    assert [p.profile_id for p in await backend.get_pending_profiles(SESSION)] == [staged.profile_id]
    assert [p.profile_id for p in await backend.list_profiles(SESSION, job_id=job.job_id)] == [staged.profile_id]
    assert await backend.list_profiles(SESSION, job_id="no-such-job") == []

    discarded = await backend.discard_profile(SESSION, staged.profile_id, ActorKind.OPERATOR)
    assert discarded.status is ProfileStatus.DISCARDED
    assert await backend.get_pending_profiles(SESSION) == []
