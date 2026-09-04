from datetime import UTC, datetime

import pytest

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.jobs import (
    GuardrailViolation,
    JobDraft,
    JobLedger,
    JobNotApplicable,
    check_job_guardrails,
)
from atworks_agent.types import (
    ActorKind,
    ApiSpec,
    Binding,
    JobKind,
    JobSchedule,
    JobStatus,
    TestDataSet,
)

CFG = AtworksAgentConfig(model="m", max_apis_per_job=3, allowed_target_envs=("dev", "stg"))


def _draft(**over):
    base = dict(kind=JobKind.RUN_NOW, summary="run", api_ids=["a", "b"], target_envs=["dev"],
                schedules=[], test_data=[], report=True)
    base.update(over)
    return JobDraft(**base)


def test_guardrail_api_count():
    v = check_job_guardrails(_draft(api_ids=["a", "b", "c", "d"]), CFG)
    assert any("4 APIs" in m and "limit is 3" in m for m in v)


def test_guardrail_target_env_protected():
    v = check_job_guardrails(_draft(target_envs=["prod"]), CFG)
    assert any("prod" in m and "not allowed targets" in m for m in v)


def test_guardrail_flags_every_bad_env_not_just_the_first():
    v = check_job_guardrails(_draft(target_envs=["dev", "prod", "qa"]), CFG)
    assert any("prod" in m and "qa" in m and "not allowed targets" in m for m in v)


def test_guardrail_schedule_count():
    cfg = AtworksAgentConfig(model="m", max_schedule_count=3)
    sched = JobSchedule(kind="daily", at="09:00", from_date="2026-09-04", count=5)
    v = check_job_guardrails(_draft(kind=JobKind.SCHEDULED_RUN, schedules=[sched]), cfg)
    assert any("5 runs" in m and "limit is 3" in m for m in v)


def test_guardrail_late_requires_select_where():
    v = check_job_guardrails(_draft(binding=Binding.LATE, select_where=None), CFG)
    assert any("LATE binding" in m for m in v)
    v2 = check_job_guardrails(_draft(binding=Binding.LATE, select_where={"group": "contract"}), CFG)
    assert not any("LATE binding" in m for m in v2)


def test_ledger_stage_apply_discard():
    ledger = JobLedger(CFG)
    job = ledger.stage(_draft(), actor="op")
    assert job.job_id == "job-0001" and job.status is JobStatus.STAGED
    applied = ledger.apply(job.job_id, actor="op")
    assert applied.status is JobStatus.APPLIED and applied.applied_by == "op"
    with pytest.raises(JobNotApplicable):
        ledger.discard(job.job_id, actor="op")


def test_ledger_rejects_guardrail_at_stage():
    with pytest.raises(GuardrailViolation):
        JobLedger(CFG).stage(_draft(target_envs=["prod"]), actor="op")


def test_discard_records_actor_kind():
    ledger = JobLedger(CFG)
    job = ledger.stage(_draft(), actor="op")
    discarded = ledger.discard(job.job_id, actor="assistant", actor_kind=ActorKind.AGENT)
    assert discarded.discarded_by_kind is ActorKind.AGENT
    assert discarded.discarded_by == "assistant"


def test_add_guardrail_note_appends():
    ledger = JobLedger(CFG)
    job = ledger.stage(_draft(), actor="op")
    updated = ledger.add_guardrail_note(job.job_id, "execution skipped: LATE selection resolved to 5 APIs, above the limit of 3")
    assert updated.guardrail_notes == ["execution skipped: LATE selection resolved to 5 APIs, above the limit of 3"]
    updated2 = ledger.add_guardrail_note(job.job_id, "second note")
    assert updated2.guardrail_notes == [
        "execution skipped: LATE selection resolved to 5 APIs, above the limit of 3",
        "second note",
    ]


def test_record_execution_counts_executions_not_runs():
    cfg = AtworksAgentConfig(model="m", max_schedule_count=3)
    ledger = JobLedger(cfg)
    sched = JobSchedule(kind="daily", at="09:00", from_date="2026-09-04", count=3)
    job = ledger.stage(_draft(kind=JobKind.SCHEDULED_RUN, schedules=[sched]), actor="op")
    assert job.remaining_executions == 3
    ledger.apply(job.job_id, actor="op")
    after_first = ledger.record_execution(job.job_id, ["run-0031", "run-0032"], 0)
    assert after_first.run_ids == ["run-0031", "run-0032"] and after_first.remaining_executions == 2
    after_second = ledger.record_execution(job.job_id, ["run-0033", "run-0034"], 0)
    assert len(after_second.run_ids) == 4 and after_second.remaining_executions == 1


def test_record_execution_advances_only_the_schedule_it_consumed():
    ledger = JobLedger(CFG)
    # `done=2` on the draft is the model's invention; the ledger owns that counter and resets it.
    daily = JobSchedule(kind="daily", at="09:00", from_date="2026-09-05", count=3, done=2)
    once = JobSchedule(kind="once", at="09:00", from_date="2026-09-08", count=1)
    job = ledger.stage(_draft(kind=JobKind.SCHEDULED_RUN, schedules=[daily, once]), actor="op")
    assert job.executions == 0 and [s.done for s in job.schedules] == [0, 0]
    assert job.total_executions == 4
    ledger.apply(job.job_id, actor="op")
    after = ledger.record_execution(job.job_id, ["run-0031", "run-0032"], 1)
    assert after.executions == 1 and after.remaining_executions == 3
    assert [s.done for s in after.schedules] == [0, 1]
    after2 = ledger.record_execution(job.job_id, ["run-0033"], 0)
    assert after2.executions == 2 and [s.done for s in after2.schedules] == [1, 1]


def _apis(**params: list[str]) -> dict[str, ApiSpec]:
    return {
        api_id: ApiSpec(api_id=api_id, method="POST", path=f"/v1/{api_id}", name=api_id,
                        updated_at=datetime(2026, 9, 1, tzinfo=UTC), params=list(names))
        for api_id, names in params.items()
    }


def test_guardrail_env_count_cap():
    cfg = AtworksAgentConfig(model="m", allowed_target_envs=("dev", "stg", "qa"),
                             max_target_envs_per_job=2)
    v = check_job_guardrails(_draft(target_envs=["dev", "stg", "qa"]), cfg)
    assert any("3 environments" in m and "limit is 2" in m for m in v)


def test_guardrail_schedule_list_and_data_set_caps():
    cfg = AtworksAgentConfig(model="m", max_schedules_per_job=2, max_test_data_sets=1,
                             max_schedule_count=30)
    scheds = [JobSchedule(kind="daily", at="09:00", from_date=f"2026-09-0{d}", count=1) for d in (4, 5, 6)]
    data = [TestDataSet(label="S1", values={"amount": "1"}),
            TestDataSet(label="S2", values={"amount": "2"})]
    v = check_job_guardrails(_draft(kind=JobKind.SCHEDULED_RUN, schedules=scheds, test_data=data), cfg)
    assert any("3 schedules" in m and "limit is 2" in m for m in v)
    assert any("2 test data sets" in m and "limit is 1" in m for m in v)


def test_guardrail_matrix_cap_when_each_dimension_is_within_its_own_limit():
    cfg = AtworksAgentConfig(model="m", max_apis_per_job=100, max_target_envs_per_job=2,
                             max_test_data_sets=5, max_matrix_size=20)
    data = [TestDataSet(label=f"S{i}", values={"amount": "1"}) for i in range(3)]
    v = check_job_guardrails(_draft(api_ids=[f"api-{i}" for i in range(5)],
                                    target_envs=["dev", "stg"], test_data=data), cfg)
    assert any("30 runs per execution" in m and "5 APIs × 2 envs × 3 data sets" in m
               and "limit is 20" in m for m in v)


def test_guardrail_duplicate_schedule():
    s = JobSchedule(kind="daily", at="09:00", tz="Asia/Seoul", from_date="2026-09-04", count=2)
    v = check_job_guardrails(_draft(kind=JobKind.SCHEDULED_RUN, schedules=[s, s.model_copy()]), CFG)
    assert any("duplicate schedule" in m for m in v)
    v2 = check_job_guardrails(
        _draft(kind=JobKind.SCHEDULED_RUN, schedules=[s, s.model_copy(update={"at": "18:00"})]), CFG)
    assert not any("duplicate schedule" in m for m in v2)


def test_guardrail_test_data_key_must_be_a_param_of_a_selected_api():
    data = [TestDataSet(label="S1 정상", values={"amount": "1000"})]
    ok = check_job_guardrails(_draft(test_data=data), CFG, _apis(a=["amount"], b=["id"]))
    assert not any("test data set" in m for m in ok)

    bad = check_job_guardrails(_draft(test_data=data), CFG, _apis(a=["id"], b=["id"]))
    assert any("test data set 'S1 정상' binds parameters (amount)" in m for m in bad)


def test_guardrail_skips_the_test_data_rule_without_a_catalogue():
    data = [TestDataSet(label="S1 정상", values={"amount": "1000"})]
    assert not any("test data set" in m for m in check_job_guardrails(_draft(test_data=data), CFG))
