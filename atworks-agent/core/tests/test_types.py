from datetime import UTC, datetime

from atworks_agent.types import (
    ActorKind,
    ApiSpec,
    AtworksSessionState,
    Binding,
    JobKind,
    JobSchedule,
    JobSpec,
    JobStatus,
    RunResult,
    RunStatus,
)


def _api(api_id="api-001"):
    return ApiSpec(api_id=api_id, method="GET", path="/v1/contracts/{id}", name="계약 조회",
                   group="contract", updated_at=datetime(2026, 8, 30, tzinfo=UTC),
                   has_rules=True, params=["id"])


def test_state_remembers_api_for_provenance():
    state = AtworksSessionState()
    state.remember_api(_api())
    assert "api-001" in state.seen_apis


def test_jobspec_defaults_are_staged_and_frozen():
    job = JobSpec(job_id="job-0001", kind=JobKind.SCHEDULED_RUN, summary="s",
                  api_ids=["api-001"], target_env="dev", created_at=datetime.now(UTC),
                  created_by="op")
    assert job.status is JobStatus.STAGED
    assert job.binding is Binding.FROZEN
    assert job.created_by_kind is ActorKind.OPERATOR


def test_schedule_count_bounds():
    import pytest
    with pytest.raises(ValueError):
        JobSchedule(kind="daily", at="09:00", tz="Asia/Seoul", from_date="2026-09-04", count=0)


def test_schedule_at_must_be_a_valid_time():
    import pytest
    with pytest.raises(ValueError):
        JobSchedule(kind="daily", at="99:99", from_date="2026-09-04", count=1)


def test_schedule_from_date_must_be_a_valid_date():
    import pytest
    with pytest.raises(ValueError):
        JobSchedule(kind="daily", at="09:00", from_date="2026-13-45", count=1)


def test_schedule_once_requires_count_one():
    import pytest
    with pytest.raises(ValueError):
        JobSchedule(kind="once", at="09:00", from_date="2026-09-04", count=3)
    JobSchedule(kind="once", at="09:00", from_date="2026-09-04", count=1)


def test_run_status_values():
    assert {s.value for s in RunStatus} == {"pass", "fail", "error"}
    r = RunResult(run_id="run-1", api_id="api-001", executed_at=datetime.now(UTC),
                  target_env="dev", status=RunStatus.FAIL, failed_rules=["amount>=0"],
                  http_status=200, duration_ms=120)
    assert r.status is RunStatus.FAIL
