"""JobSpec guardrail과 인메모리 JobLedger. merchant_agent/changes.py 미러.
guardrail은 stage 시점과 apply 시점에 두 번 돈다 — apply 때 config가 더 엄격해졌을 수 있다."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from commerce_common.fencing import truncate_display
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import AtworksAgentConfig
from .types import ActorKind, Binding, JobKind, JobSchedule, JobSpec, JobStatus, TestDataSet


class GuardrailViolation(ValueError):
    def __init__(self, violations: list[str]):
        super().__init__("; ".join(violations))
        self.violations = violations


class JobNotApplicable(ValueError):
    """id를 모르거나 상태 전이가 불가능. 백엔드가 지원하지 않는 작업에도 이 예외를 던진다."""


class SelectWhere(BaseModel):
    """LATE binding의 재평가 질의. stage_job이 원한 값만 받는다 — search_apis 인자를 그대로
    받아들이면 모델이 임의 필드를 실어 보낼 수 있다."""
    model_config = ConfigDict(extra="forbid")
    query: str = Field(default="", max_length=120)
    group: str | None = Field(default=None, max_length=60)
    updated_after: datetime | None = None


class JobDraft(BaseModel):
    """stage_job 툴 입력이 검증·정규화된 뒤의 모양. 백엔드는 이걸 받아 JobSpec을 만든다."""
    kind: JobKind
    summary: str = Field(max_length=200)
    api_ids: list[str]
    target_envs: list[str] = Field(min_length=1)
    schedules: list[JobSchedule] = Field(default_factory=list)
    test_data: list[TestDataSet] = Field(default_factory=list)
    select_where: dict[str, Any] | None = None
    binding: Binding = Binding.FROZEN
    report: bool = True
    confidence: dict[str, float] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)

    @field_validator("target_envs")
    @classmethod
    def _each_env_is_short(cls, value: list[str]) -> list[str]:
        for env in value:
            if len(env) > 32:
                raise ValueError(f"each target env is at most 32 chars, got {len(env)}")
        return value


def check_job_guardrails(draft: JobDraft, config: AtworksAgentConfig) -> list[str]:
    violations: list[str] = []
    if len(draft.api_ids) > config.max_apis_per_job:
        violations.append(
            f"job touches {len(draft.api_ids)} APIs and the limit is {config.max_apis_per_job} "
            "per job; narrow the selection or split it into several jobs, each approved on its own"
        )
    if not draft.api_ids:
        violations.append("job selects no APIs — resolve the selection with search_apis first")
    # Rule 1 — every offending env is named. Never all(...) and never a check of one element:
    # a single prod anywhere in the list must block the whole job.
    bad_envs = [e for e in draft.target_envs if e not in config.allowed_target_envs]
    if bad_envs:
        violations.append(
            f"target_envs {', '.join(bad_envs)} are not allowed targets "
            f"({', '.join(config.allowed_target_envs)}); the assistant may never target them"
        )
    if draft.kind is JobKind.SCHEDULED_RUN:
        if not draft.schedules:
            violations.append("a scheduled_run needs at least one schedule")
        if not config.enable_scheduling:
            violations.append("scheduling is switched off for this deployment")
    if draft.kind is JobKind.RUN_NOW and draft.schedules:
        violations.append("run_now must not carry schedules — use scheduled_run")
    scheduled_runs = sum(s.count for s in draft.schedules)
    if scheduled_runs > config.max_schedule_count:
        violations.append(
            f"schedules total {scheduled_runs} runs and the limit is {config.max_schedule_count} per job; shorten them"
        )
    if draft.binding is Binding.LATE and not draft.select_where:
        violations.append(
            "LATE binding needs the select_where that produced the selection — pass the search_apis arguments"
        )
    return violations


class JobLedger:
    """백엔드가 얹어 쓸 수 있는 인메모리 생명주기. 적용·폐기된 job도 감사 이력으로 남는다."""

    def __init__(self, config: AtworksAgentConfig):
        self._config = config
        self._jobs: dict[str, JobSpec] = {}
        self._sequence = 0

    def stage(self, draft: JobDraft, *, actor: str, actor_kind: ActorKind = ActorKind.OPERATOR) -> JobSpec:
        violations = check_job_guardrails(draft, self._config)
        if violations:
            raise GuardrailViolation(violations)
        self._sequence += 1
        job = JobSpec(
            job_id=f"job-{self._sequence:04d}",
            kind=draft.kind,
            summary=truncate_display(draft.summary, 200),
            api_ids=list(draft.api_ids),
            select_where=draft.select_where,
            binding=draft.binding,
            target_envs=list(draft.target_envs),
            # A draft may carry a `done` the model invented; the ledger owns that counter.
            schedules=[s.model_copy(update={"done": 0}) for s in draft.schedules],
            test_data=[d.model_copy() for d in draft.test_data],
            report=draft.report,
            confidence=dict(draft.confidence),
            assumptions=list(draft.assumptions),
            created_at=datetime.now(UTC),
            created_by=actor,
            created_by_kind=actor_kind,
            executions=0,
        )
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> JobSpec | None:
        return self._jobs.get(job_id)

    def pending(self) -> list[JobSpec]:
        return [j for j in self._jobs.values() if j.status is JobStatus.STAGED]

    def applied(self) -> list[JobSpec]:
        return [j for j in self._jobs.values() if j.status is JobStatus.APPLIED]

    def apply(self, job_id: str, *, actor: str) -> JobSpec:
        job = self._require_staged(job_id, "apply")
        draft = JobDraft(kind=job.kind, summary=job.summary, api_ids=job.api_ids,
                         target_envs=job.target_envs, schedules=job.schedules,
                         test_data=job.test_data, select_where=job.select_where,
                         binding=job.binding, report=job.report)
        violations = check_job_guardrails(draft, self._config)
        if violations:
            raise GuardrailViolation(violations)
        updated = job.model_copy(update={"status": JobStatus.APPLIED,
                                         "applied_at": datetime.now(UTC), "applied_by": actor})
        self._jobs[job_id] = updated
        return updated

    def discard(self, job_id: str, *, actor: str, actor_kind: ActorKind = ActorKind.OPERATOR) -> JobSpec:
        job = self._require_staged(job_id, "discard")
        updated = job.model_copy(update={"status": JobStatus.DISCARDED,
                                         "discarded_at": datetime.now(UTC), "discarded_by": actor,
                                         "discarded_by_kind": actor_kind})
        self._jobs[job_id] = updated
        return updated

    def add_guardrail_note(self, job_id: str, note: str) -> JobSpec:
        job = self._jobs[job_id]
        updated = job.model_copy(update={"guardrail_notes": [*job.guardrail_notes, note]})
        self._jobs[job_id] = updated
        return updated

    def record_execution(self, job_id: str, run_ids: list[str], schedule_index: int | None) -> JobSpec:
        """One execution of the job's matrix happened and produced these runs. ``executions``
        counts executions (the schedules' counts summed, or 1 for run_now), never individual
        runs; ``schedule_index`` names which schedule occurrence was consumed — without it
        several schedules on one job could not advance independently. Exactly one call per
        execution, even when the execution produced nothing."""
        job = self._jobs[job_id]
        update: dict[str, Any] = {
            "run_ids": [*job.run_ids, *run_ids],
            "executions": job.executions + 1,
        }
        if schedule_index is not None and 0 <= schedule_index < len(job.schedules):
            schedules = list(job.schedules)
            consumed = schedules[schedule_index]
            schedules[schedule_index] = consumed.model_copy(update={"done": consumed.done + 1})
            update["schedules"] = schedules
        updated = job.model_copy(update=update)
        self._jobs[job_id] = updated
        return updated

    def _require_staged(self, job_id: str, action: str) -> JobSpec:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotApplicable(f"no job with id {job_id!r} to {action}")
        if job.status is not JobStatus.STAGED:
            raise JobNotApplicable(f"job {job_id} is {job.status.value}, not staged — nothing to {action}")
        return job
