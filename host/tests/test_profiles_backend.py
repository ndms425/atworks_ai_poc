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


async def test_apply_profile_per_api_ignore_does_not_leak_to_other_apis(tmp_path):
    """M4.4 regression: a per-API ignore path scoped to one api_id must not suppress the same
    path's real diff on a different api_id. api-002 and api-008 both differ only on the volatile
    $.serverTime field between legacy/renewed (per fixtures/apis.json + MockAtworks.stub_response);
    applying a profile whose per_api_ignore only names api-002 must clear api-002's diff while
    leaving api-008's $.serverTime value_diff untouched."""
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    backend.reports = reports

    job = await _parity_job(backend, reports)

    initial = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    initial_rows = {r["api_id"]: r for r in initial["parity"]["rows"]}
    assert initial_rows["api-002"]["verdict"] == "value_diff"
    assert initial_rows["api-002"]["diff_paths"] == ["$.serverTime"]
    assert initial_rows["api-008"]["verdict"] == "value_diff"
    assert initial_rows["api-008"]["diff_paths"] == ["$.serverTime"]

    run_count_before = len(backend.runs)

    draft = ProfileDraft(
        job_id=job.job_id, ignore_paths=[], per_api_ignore={"api-002": ["$.serverTime"]},
        summary="ignore serverTime on api-002 only")
    staged = await backend.stage_profile(SESSION, draft, ActorKind.OPERATOR)
    applied = await backend.apply_profile(SESSION, staged.profile_id)

    assert applied.status is ProfileStatus.APPLIED
    assert applied.effective_from is not None
    assert len(backend.runs) == run_count_before   # no new runs

    after = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    rows = {r["api_id"]: r for r in after["parity"]["rows"]}

    # api-002: its own per-API ignore path clears its only diff.
    assert rows["api-002"]["verdict"] == "equal"
    assert "$.serverTime" not in rows["api-002"]["diff_paths"]

    # api-008: the per-API path was scoped to api-002 and must NOT leak here.
    assert rows["api-008"]["verdict"] == "value_diff"
    assert "$.serverTime" in rows["api-008"]["diff_paths"]

    assert after["parity"]["ignore_paths"] == []
    assert after["parity"]["per_api_ignore"] == {"api-002": ["$.serverTime"]}


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


async def test_scheduled_rewrite_honors_an_applied_profile(tmp_path):
    """M-final regression: a SECOND scheduled occurrence must still honor an APPLIED comparison
    profile. Before the fix, `Scheduler._execute_one` called `reports.write` with the default
    empty ignore paths, so the noise cluster the profile suppressed came back on the next
    scheduled write even though the profile was already APPLIED (and can't be re-staged)."""
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    backend.reports = reports
    sched = Scheduler(backend, reports, SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.SCHEDULED_RUN, summary="legacy/renewed 반복 비교",
        api_ids=["api-002", "api-004", "api-008"], target_envs=["legacy", "renewed"],
        schedules=[JobSchedule(kind="daily", at="09:00", tz="Asia/Seoul", from_date="2026-09-05", count=2)]),
        ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)

    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]

    draft = ProfileDraft(job_id=job.job_id, ignore_paths=["$.serverTime"], summary="ignore volatile serverTime")
    staged = await backend.stage_profile(SESSION, draft, ActorKind.OPERATOR)
    applied = await backend.apply_profile(SESSION, staged.profile_id)
    assert applied.status is ProfileStatus.APPLIED

    # The SECOND scheduled occurrence: driven through the scheduler exactly as production runs
    # it. Before the fix, this write reverted to the unfiltered parity block.
    assert await sched.tick(datetime(2026, 9, 6, 9, 0, tzinfo=KST)) == [job.job_id]

    data = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    rows = {r["api_id"]: r for r in data["parity"]["rows"]}
    assert rows["api-002"]["verdict"] == "equal"
    assert rows["api-008"]["verdict"] == "equal"
    assert rows["api-004"]["verdict"] == "value_diff"
    assert rows["api-004"]["diff_paths"] == ["$.limit"]
    assert data["parity"]["ignore_paths"] == ["$.serverTime"]


async def test_profile_applied_before_first_run_is_honored_by_the_first_scheduled_write(tmp_path):
    """A profile approved before the job ever ran (no report exists yet, so `apply_profile`'s
    one-shot rediff is a no-op) must still be honored once the FIRST scheduled write happens."""
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    reports = Reports(tmp_path)
    backend.reports = reports
    sched = Scheduler(backend, reports, SESSION)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.SCHEDULED_RUN, summary="approve before first run",
        api_ids=["api-002", "api-004", "api-008"], target_envs=["legacy", "renewed"],
        schedules=[JobSchedule(kind="daily", at="09:00", tz="Asia/Seoul", from_date="2026-09-05", count=1)]),
        ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)

    draft = ProfileDraft(job_id=job.job_id, ignore_paths=["$.serverTime"], summary="ignore volatile serverTime")
    staged = await backend.stage_profile(SESSION, draft, ActorKind.OPERATOR)
    applied = await backend.apply_profile(SESSION, staged.profile_id)
    assert applied.status is ProfileStatus.APPLIED

    assert await sched.tick(datetime(2026, 9, 5, 9, 0, tzinfo=KST)) == [job.job_id]

    data = json.loads((tmp_path / job.job_id / "data.json").read_text(encoding="utf-8"))
    rows = {r["api_id"]: r for r in data["parity"]["rows"]}
    assert rows["api-002"]["verdict"] == "equal"
    assert rows["api-008"]["verdict"] == "equal"
    assert rows["api-004"]["verdict"] == "value_diff"
    assert rows["api-004"]["diff_paths"] == ["$.limit"]


async def test_list_and_pending_profiles(tmp_path):
    backend = MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)
    job = await backend.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="j", api_ids=["api-002"], target_envs=["legacy", "renewed"]), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)

    draft = ProfileDraft(job_id=job.job_id, ignore_paths=["$.serverTime"], summary="s")
    staged = await backend.stage_profile(SESSION, draft, ActorKind.OPERATOR)

    assert [p.profile_id for p in await backend.get_pending_profiles(SESSION)] == [staged.profile_id]
    assert [p.profile_id for p in (await backend.list_profiles(SESSION, job_id=job.job_id)).items] == [staged.profile_id]
    assert (await backend.list_profiles(SESSION, job_id="no-such-job")).items == []

    discarded = await backend.discard_profile(SESSION, staged.profile_id, ActorKind.OPERATOR)
    assert discarded.status is ProfileStatus.DISCARDED
    assert await backend.get_pending_profiles(SESSION) == []


async def test_get_context_surfaces_the_configured_named_targets():
    """Regression (live-smoke find): get_context must reflect config.allowed_target_envs so the
    model learns legacy/renewed are allowed targets — a hardcoded ["dev","stg"] made the model
    refuse to stage a parity job on the named targets."""
    from pathlib import Path

    from atworks_agent import AtworksAgentConfig, AtworksSessionContext
    from atworks_host.mock_backend import MockAtworks

    cfg = AtworksAgentConfig(model="m")
    backend = MockAtworks(cfg, Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures")
    session = AtworksSessionContext(session_id="s", project_id="mes", operator="op")
    ctx = await backend.get_context(session)
    assert ctx["allowed_targets"] == list(cfg.allowed_target_envs)
    assert "legacy" in ctx["allowed_targets"] and "renewed" in ctx["allowed_targets"]
