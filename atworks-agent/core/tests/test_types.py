import json
from datetime import UTC, datetime

import pytest

from atworks_agent.types import (
    ActorKind,
    ApiSpec,
    AtworksSessionState,
    Binding,
    FormatBatch,
    FormatBatchEntry,
    JobKind,
    JobSchedule,
    JobSpec,
    JobStatus,
    OperatorProfile,
    RunResult,
    RunStatus,
    ScreenFilter,
    ScreenState,
    ScreenTarget,
    TestDataSet,
    ValidationRule,
)


def _api(api_id="api-001"):
    return ApiSpec(api_id=api_id, method="GET", path="/v1/contracts/{id}", name="계약 조회",
                   group="contract", updated_at=datetime(2026, 8, 30, tzinfo=UTC),
                   has_rules=True, params=["id"])


def test_state_remembers_api_for_provenance():
    state = AtworksSessionState()
    state.remember_api(_api())
    assert "api-001" in state.seen_apis


def test_state_has_rule_impacts_field_defaulting_empty():
    assert AtworksSessionState().rule_impacts == {}


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


def test_seen_rules_survives_the_session_json_round_trip():
    # Regression for the apply_rule 400: seen_rules used to be typed dict[str, Any], so
    # reloading AtworksSessionState from the session store's JSON document (as the host does
    # between two HTTP requests) left each value a plain dict instead of a ValidationRule,
    # and gates.check_apply_rule's `known.api_id` raised AttributeError. Typing the field
    # with ValidationRule lets pydantic reconstruct real instances on reload.
    state = AtworksSessionState()
    rule = ValidationRule(rule_id="rule-0001", api_id="api-001", param="contractNo", kind="required",
                          message="contractNo required", created_at=datetime.now(UTC), created_by="op")
    state.remember_rule(rule)

    reloaded = AtworksSessionState.model_validate(json.loads(state.model_dump_json()))

    known = reloaded.seen_rules["rule-0001"]
    assert isinstance(known, ValidationRule)
    assert known.api_id == "api-001"


def test_seen_format_batches_survives_the_session_json_round_trip():
    # Same lesson as seen_rules above, applied to FormatBatch (Task 4): typed
    # dict[str, FormatBatch], not dict[str, Any], so gates.check_apply_format_batch's
    # provenance lookup still sees a real FormatBatch (not a plain dict) after a reload.
    state = AtworksSessionState()
    batch = FormatBatch(
        batch_id="format-batch-0001", summary="bulk seed",
        entries=[FormatBatchEntry(name="phone-digits", pattern=r"^\d{3}-\d{4}$",
                                  pass_examples=["123-4567"], fail_examples=["abc"], outcome="new")],
        created_at=datetime.now(UTC), created_by="op")
    state.remember_format_batch(batch)

    reloaded = AtworksSessionState.model_validate(json.loads(state.model_dump_json()))

    known = reloaded.seen_format_batches["format-batch-0001"]
    assert isinstance(known, FormatBatch)
    assert known.entries[0].outcome == "new"
    assert known.new_count == 1


def test_screen_state_round_trips_as_a_model_on_session_state():
    state = AtworksSessionState()
    state.current_screen = ScreenState(view="runs", visible=[ScreenTarget(kind="run", ref_id="run-0031")])
    restored = AtworksSessionState.model_validate(json.loads(state.model_dump_json()))
    assert isinstance(restored.current_screen, ScreenState)
    assert restored.current_screen.visible[0].ref_id == "run-0031"


def test_screen_filter_rejects_unknown_keys_and_bad_status():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ScreenFilter(status="non_pass")
    with pytest.raises(ValidationError):
        ScreenFilter(group="payment")


def test_run_result_executed_by_defaults_none_and_round_trips():
    r = RunResult(run_id="r", api_id="a", executed_at=datetime(2026, 9, 1, tzinfo=UTC), target_env="dev", status="pass")
    assert r.executed_by is None
    r2 = RunResult(**{**r.model_dump(), "executed_by": "minseong"})
    assert RunResult.model_validate(json.loads(r2.model_dump_json())).executed_by == "minseong"


def test_operator_profile_rejects_bad_role_and_id():
    with pytest.raises(ValueError):
        OperatorProfile(operator_id="x", name="n", role="admin")
    with pytest.raises(ValueError):
        OperatorProfile(operator_id="../x", name="n", role="qa")
