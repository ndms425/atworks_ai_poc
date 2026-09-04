import pytest

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.jobs import (
    GuardrailViolation,
    JobDraft,
    JobLedger,
    JobNotApplicable,
    check_job_guardrails,
)
from atworks_agent.types import ActorKind, Binding, JobKind, JobSchedule, JobStatus

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
