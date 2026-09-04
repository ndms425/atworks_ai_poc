from datetime import UTC, datetime

import pytest

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
    TestDataSet,
)


def _api(api_id="api-001"):
    return ApiSpec(api_id=api_id, method="GET", path="/v1/contracts/{id}", name="계약 조회",
                   group="contract", updated_at=datetime(2026, 8, 30, tzinfo=UTC),
                   has_rules=True, params=["id"])


def test_state_remembers_api_for_provenance():
    state = AtworksSessionState()
    state.remember_api(_api())
    assert "api-001" in state.seen_apis


def test_state_has_no_dead_attached_items_field():
    # attached_items is per-turn context (ChatRequest.attached_items in the host, and the
    # stream_turn parameter) — nothing reads it off the persisted session state, so it
    # should not be one of its fields.
    assert "attached_items" not in AtworksSessionState.model_fields


def test_jobspec_defaults_are_staged_and_frozen():
    job = JobSpec(job_id="job-0001", kind=JobKind.SCHEDULED_RUN, summary="s",
                  api_ids=["api-001"], target_envs=["dev"], created_at=datetime.now(UTC),
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


def test_schedule_tz_must_be_a_real_iana_zone():
    with pytest.raises(ValueError, match="Nowhere/Bogus"):
        JobSchedule(kind="once", at="09:00", tz="Nowhere/Bogus", from_date="2026-09-05", count=1)


def test_run_status_values():
    assert {s.value for s in RunStatus} == {"pass", "fail", "error"}
    r = RunResult(run_id="run-1", api_id="api-001", executed_at=datetime.now(UTC),
                  target_env="dev", status=RunStatus.FAIL, failed_rules=["amount>=0"],
                  http_status=200, duration_ms=120)
    assert r.status is RunStatus.FAIL


def test_test_data_set_bounds_its_label_and_values():
    ok = TestDataSet(label="S1 정상", values={"amount": "1000"})
    assert ok.values["amount"] == "1000"
    with pytest.raises(ValueError):
        TestDataSet(label="", values={"amount": "1"})
    with pytest.raises(ValueError):
        TestDataSet(label="S1 정상", values={})
    with pytest.raises(ValueError):
        TestDataSet(label="<script>", values={"amount": "1"})


def test_test_data_set_rejects_an_oversized_key_or_value():
    with pytest.raises(ValueError):
        TestDataSet(label="S1", values={"k" * 61: "1"})
    with pytest.raises(ValueError):
        TestDataSet(label="S1", values={"amount": "9" * 201})


def test_test_data_set_caps_the_number_of_keys():
    with pytest.raises(ValueError):
        TestDataSet(label="S1", values={f"k{i}": "1" for i in range(21)})


def test_jobspec_matrix_properties_multiply_the_dimensions():
    job = JobSpec(job_id="job-0001", kind=JobKind.SCHEDULED_RUN, summary="s",
                  api_ids=["api-001", "api-002"], target_envs=["dev", "stg"],
                  schedules=[JobSchedule(kind="daily", at="09:00", from_date="2026-09-05", count=3),
                             JobSchedule(kind="once", at="09:00", from_date="2026-09-08", count=1)],
                  test_data=[TestDataSet(label="S1", values={"amount": "1"}),
                             TestDataSet(label="S2", values={"amount": "-1"})],
                  created_at=datetime.now(UTC), created_by="op")
    assert job.matrix_size == 8          # 2 apis × 2 envs × 2 data sets
    assert job.total_executions == 4     # 3 + 1
    assert job.remaining_executions == 4
    assert job.runs_total == 32


def test_jobspec_without_schedules_or_data_runs_its_matrix_once():
    job = JobSpec(job_id="job-0002", kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"],
                  target_envs=["dev"], created_at=datetime.now(UTC), created_by="op", executions=1)
    assert job.matrix_size == 1 and job.total_executions == 1
    assert job.remaining_executions == 0 and job.runs_total == 1


def test_remaining_executions_never_goes_below_zero():
    job = JobSpec(job_id="job-0003", kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"],
                  target_envs=["dev"], created_at=datetime.now(UTC), created_by="op", executions=5)
    assert job.remaining_executions == 0


def test_run_result_carries_the_data_set_it_was_bound_to():
    bound = RunResult(run_id="run-1", api_id="api-001", executed_at=datetime.now(UTC),
                      target_env="stg", status=RunStatus.PASS, test_data_label="S2 음수 금액")
    assert bound.test_data_label == "S2 음수 금액"
    unbound = RunResult(run_id="run-2", api_id="api-001", executed_at=datetime.now(UTC),
                        target_env="dev", status=RunStatus.PASS)
    assert unbound.test_data_label is None
