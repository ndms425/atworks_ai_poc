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
    base = dict(kind=JobKind.RUN_NOW, summary="run", api_ids=["a", "b"], target_env="dev",
                schedule=None, report=True)
    base.update(over)
    return JobDraft(**base)


def test_guardrail_api_count():
    v = check_job_guardrails(_draft(api_ids=["a", "b", "c", "d"]), CFG)
    assert any("4 APIs" in m and "limit is 3" in m for m in v)


def test_guardrail_target_env_protected():
    v = check_job_guardrails(_draft(target_env="prod"), CFG)
    assert any("prod" in m and "not an allowed target" in m for m in v)


def test_guardrail_schedule_count():
    cfg = AtworksAgentConfig(model="m", max_schedule_count=3)
    sched = JobSchedule(kind="daily", at="09:00", from_date="2026-09-04", count=5)
    v = check_job_guardrails(_draft(kind=JobKind.SCHEDULED_RUN, schedule=sched), cfg)
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
        JobLedger(CFG).stage(_draft(target_env="prod"), actor="op")


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
    job = ledger.stage(_draft(kind=JobKind.SCHEDULED_RUN, schedule=sched), actor="op")
    assert job.runs_remaining == 3
    ledger.apply(job.job_id, actor="op")
    after_first = ledger.record_execution(job.job_id, ["run-0031", "run-0032"])
    assert after_first.run_ids == ["run-0031", "run-0032"] and after_first.runs_remaining == 2
    after_second = ledger.record_execution(job.job_id, ["run-0033", "run-0034"])
    assert len(after_second.run_ids) == 4 and after_second.runs_remaining == 1
