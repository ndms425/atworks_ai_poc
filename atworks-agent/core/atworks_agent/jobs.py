"""JobSpec guardrail과 인메모리 JobLedger. merchant_agent/changes.py 미러.
guardrail은 stage 시점과 apply 시점에 두 번 돈다 — apply 때 config가 더 엄격해졌을 수 있다."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from commerce_common.fencing import truncate_display
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import AtworksAgentConfig
from .types import (
    ActorKind,
    ApiSpec,
    Binding,
    JobKind,
    JobSchedule,
    JobSpec,
    JobStatus,
    TestDataSet,
)


class GuardrailViolation(ValueError):
    def __init__(self, violations: list[str]):
        super().__init__("; ".join(violations))
        self.violations = violations


class JobNotApplicable(ValueError):
    """id를 모르거나 상태 전이가 불가능. 백엔드가 지원하지 않는 작업에도 이 예외를 던진다."""


# The only slots a job's confidence dict may name; the tool schema documents the same set
# (registry.py's stage_job "confidence" description) and executor._stage_job filters to it
# before the draft is even built. JobDraft's validator is the second gate, for any caller
# (a backend, a test) that constructs a draft directly.
CONFIDENCE_KEYS = ("target_envs", "schedules", "test_data", "binding", "api_ids", "report")


def _listed(items: Sequence[str], limit: int = 5, width: int = 40) -> str:
    """Model-authored strings joined for a guardrail message, bounded so the message can never
    grow with the model's input — R18: a violation string is fed back to the model verbatim
    (ToolOutcome.held is not fence-truncated), so an unbounded list here is a cheap prompt-bloat
    and cache-bust primitive."""
    shown = [truncate_display(str(item), width) for item in items[:limit]]
    text = ", ".join(shown)
    if len(items) > limit:
        text += f" … and {len(items) - limit} more"
    return text


class SelectWhere(BaseModel):
    """LATE binding의 재평가 질의. stage_job이 원한 값만 받는다 — search_apis 인자를 그대로
    받아들이면 모델이 임의 필드를 실어 보낼 수 있다."""
    model_config = ConfigDict(extra="forbid")
    query: str = Field(default="", max_length=120)
    group: str | None = Field(default=None, max_length=60)
    updated_after: datetime | None = None
    failed_since: datetime | None = None    # keep APIs with a non_pass run at/after this time
    related_to: str | None = Field(default=None, max_length=64)   # same group or same leading path segments as this api_id

    @field_validator("updated_after", "failed_since")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        return v if v is None or v.tzinfo is not None else v.replace(tzinfo=UTC)


class JobDraft(BaseModel):
    """stage_job 툴 입력이 검증·정규화된 뒤의 모양. 백엔드는 이걸 받아 JobSpec을 만든다."""
    kind: JobKind
    summary: str = Field(max_length=200)
    api_ids: list[str]
    target_envs: list[str] = Field(min_length=1, max_length=10)
    schedules: list[JobSchedule] = Field(default_factory=list, max_length=10)
    test_data: list[TestDataSet] = Field(default_factory=list, max_length=10)
    select_where: dict[str, Any] | None = None
    binding: Binding = Binding.FROZEN
    report: bool = True
    confidence: dict[str, float] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    selection_basis: str | None = Field(default=None, max_length=160)

    @field_validator("target_envs")
    @classmethod
    def _each_env_is_short(cls, value: list[str]) -> list[str]:
        for env in value:
            if len(env) > 32:
                raise ValueError(f"each target env is at most 32 chars, got {len(env)}")
        return value

    @field_validator("confidence")
    @classmethod
    def _confidence_keys_are_closed(cls, value: dict[str, float]) -> dict[str, float]:
        unknown = sorted(k for k in value if k not in CONFIDENCE_KEYS)
        if unknown:
            raise ValueError(
                f"confidence keys must be one of {', '.join(CONFIDENCE_KEYS)}; got unknown key(s) {_listed(unknown)}"
            )
        for key, score in value.items():
            if not 0 <= score <= 1:
                raise ValueError(f"confidence[{key!r}] must be between 0 and 1, got {score}")
        return value


def check_job_guardrails(
    draft: JobDraft,
    config: AtworksAgentConfig,
    apis: Mapping[str, ApiSpec] | None = None,
) -> list[str]:
    """Every rule a staged job must satisfy, checked at stage time and again at apply time —
    apply-time config may have tightened. Adding a rule here is the only way to add one:
    both phases and both call sites (``JobLedger``, ``gates.check_apply_job``) read this list.

    ``apis`` is the catalogue rule 7 (test-data keys must be parameters of a selected API)
    needs. ``None`` skips that one rule, and a caller that cannot see the catalogue must pass
    ``None`` rather than an empty mapping — a session that knows a job but not its APIs would
    otherwise turn every binding into a violation."""
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
            f"target_envs {_listed(bad_envs)} are not allowed targets "
            f"({', '.join(config.allowed_target_envs)}); the assistant may never target them"
        )
    # Rule 2 — per-dimension counts.
    if len(draft.target_envs) > config.max_target_envs_per_job:
        violations.append(
            f"job targets {len(draft.target_envs)} environments and the limit is "
            f"{config.max_target_envs_per_job} per job; narrow the selection"
        )
    if len(draft.schedules) > config.max_schedules_per_job:
        violations.append(
            f"job carries {len(draft.schedules)} schedules and the limit is "
            f"{config.max_schedules_per_job} per job; combine or drop some"
        )
    if len(draft.test_data) > config.max_test_data_sets:
        violations.append(
            f"job carries {len(draft.test_data)} test data sets and the limit is "
            f"{config.max_test_data_sets} per job; drop some"
        )
    # Rule 3 — the product. The dimensions multiply rather than concatenate, so no
    # per-dimension cap bounds the total work on its own (50 APIs × 2 envs × 5 data sets
    # passes every cap above and is 500 runs an execution).
    data_sets = max(1, len(draft.test_data))
    matrix = len(draft.api_ids) * len(draft.target_envs) * data_sets
    if matrix > config.max_matrix_size:
        violations.append(
            f"job expands to {matrix} runs per execution ({len(draft.api_ids)} APIs × "
            f"{len(draft.target_envs)} envs × {data_sets} data sets) and the limit is "
            f"{config.max_matrix_size}; narrow the selection"
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
    # Rule 6 — a repeated schedule is a mistake, unlike a repeated api id (which the
    # executor de-duplicates): two identical entries would double every occurrence silently.
    # Every duplicate is collected into a single violation (via _listed) rather than one
    # message per repeat, so a model that sends many copies cannot inflate the message count.
    seen_schedules: set[tuple[str, str, str, str]] = set()
    duplicate_schedules: list[str] = []
    for s in draft.schedules:
        key = (s.kind, s.at, s.tz, s.from_date)
        if key in seen_schedules:
            duplicate_schedules.append(f"{s.kind} {s.at} {s.tz} {s.from_date}")
        seen_schedules.add(key)
    if duplicate_schedules:
        violations.append(
            f"duplicate schedule(s): {_listed(duplicate_schedules)} — a repeated schedule is a "
            "mistake; drop one or raise its count"
        )
    # Rule 7 — a data set may only bind parameters the selected APIs actually declare. Every
    # offending set is collected into a single violation (via _listed), for the same reason.
    if apis is not None:
        known = {p for i in draft.api_ids for p in (apis[i].params if i in apis else [])}
        offending = [data.label for data in draft.test_data if any(k not in known for k in data.values)]
        if offending:
            violations.append(
                f"test data set(s) {_listed(offending)} bind parameters that none of the "
                "selected APIs declares"
            )
    if draft.binding is Binding.LATE and not draft.select_where:
        violations.append(
            "LATE binding needs the select_where that produced the selection — pass the search_apis arguments"
        )
    return violations


def enforce_execution_matrix(
    config: AtworksAgentConfig, job: JobSpec, resolved_api_ids: Sequence[str]
) -> list[str]:
    """Re-derive the size caps — and, since apply-time config may have tightened the
    allow-list since this job was applied, the target-env rule — at execution time (a LATE
    selection may also have grown or shrunk since staging)."""
    violations: list[str] = []
    if len(resolved_api_ids) == 0:
        violations.append("execution skipped: selection resolved to no APIs")
    if len(resolved_api_ids) > config.max_apis_per_job:
        violations.append(
            f"execution skipped: selection resolved to {len(resolved_api_ids)} APIs, "
            f"above the limit of {config.max_apis_per_job}"
        )
    size = len(resolved_api_ids) * len(job.target_envs) * max(1, len(job.test_data))
    if size > config.max_matrix_size:
        violations.append(
            f"execution skipped: matrix resolved to {size} runs per execution, "
            f"above the limit of {config.max_matrix_size}"
        )
    bad_envs = [e for e in job.target_envs if e not in config.allowed_target_envs]
    if bad_envs:
        violations.append(
            f"execution skipped: target_envs {_listed(bad_envs)} are not allowed targets "
            f"({', '.join(config.allowed_target_envs)})"
        )
    return violations


class JobLedger:
    """백엔드가 얹어 쓸 수 있는 인메모리 생명주기. 적용·폐기된 job도 감사 이력으로 남는다."""

    def __init__(self, config: AtworksAgentConfig, apis: Mapping[str, ApiSpec] | None = None):
        self._config = config
        self._apis = apis          # rule 7's catalogue; None (no catalogue) skips that rule
        self._jobs: dict[str, JobSpec] = {}
        self._sequence = 0

    def stage(self, draft: JobDraft, *, actor: str, actor_kind: ActorKind = ActorKind.OPERATOR) -> JobSpec:
        violations = check_job_guardrails(draft, self._config, self._apis)
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
            selection_basis=draft.selection_basis,
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
        violations = check_job_guardrails(draft, self._config, self._apis)
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
        if schedule_index is not None and not (0 <= schedule_index < len(job.schedules)):
            raise JobNotApplicable(
                f"schedule_index {schedule_index} is out of range for job {job_id} "
                f"({len(job.schedules)} schedules)"
            )
        update: dict[str, Any] = {
            "run_ids": [*job.run_ids, *run_ids],
            "executions": job.executions + 1,
        }
        if schedule_index is not None:
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
