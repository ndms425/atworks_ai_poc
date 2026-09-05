"""aTworks 도메인 타입. merchant_agent/types.py의 구조를 따른다: 레코드(ApiSpec·RunResult),
스코어 결과(FailedRank), 스테이징 레코드(JobSpec), 세션 컨텍스트/상태. 상태의 seen_* 맵은
provenance 기록이다: 쓰기 게이트는 여기 있는 id만 받고, presentation은 여기서 값을 조인한다."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo

from commerce_common.types import ClockContext, remember
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# -- 레코드 ---------------------------------------------------------------------------

class ApiSpec(BaseModel):
    api_id: str
    method: str
    path: str
    name: str
    group: str | None = None
    updated_at: datetime
    has_rules: bool = False
    params: list[str] = Field(default_factory=list)


class RunStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


class RunResult(BaseModel):
    """실행 1건. status는 결정론 DSL(Mock에선 스텁)이 낸 값이고 LLM은 이 값을 바꾸지 못한다."""
    run_id: str
    api_id: str
    executed_at: datetime
    target_env: str
    test_data_label: str | None = None   # which TestDataSet was bound; None = no binding
    status: RunStatus
    failed_rules: list[str] = Field(default_factory=list)
    http_status: int | None = None
    duration_ms: int | None = None
    job_id: str | None = None


class FailedRank(BaseModel):
    """스코어러 출력 1건. score와 reasons는 scoring.py의 결정론 함수가 만든다."""
    run_id: str
    api_id: str
    scorer: str
    score: float
    reasons: list[str] = Field(default_factory=list)


class RunGroup(BaseModel):
    """One bucket of aggregate_runs. Every figure is computed by the host (aggregation.py); the
    model only chooses which groups a card shows."""
    key: str
    label: str
    count: int
    fail: int
    error: int
    passed: int
    run_ids: list[str] = Field(default_factory=list)          # newest first, at most 50
    first_non_pass_at: datetime | None = None
    last_pass_before: datetime | None = None
    latest_status: RunStatus | None = None
    transitions: int = 0
    flaky: bool = False
    p95_duration_ms: int | None = None
    regression_suspect: bool = False
    api_updated_at: datetime | None = None


class Insights(BaseModel):
    flaky: int = 0
    regression_suspect: int = 0


# -- 실행 계획(JobSpec) ---------------------------------------------------------------

class JobKind(StrEnum):
    RUN_NOW = "run_now"
    SCHEDULED_RUN = "scheduled_run"


class JobStatus(StrEnum):
    STAGED = "staged"
    APPLIED = "applied"
    DISCARDED = "discarded"


class RuleStatus(StrEnum):
    STAGED = "staged"
    APPLIED = "applied"
    DISCARDED = "discarded"


class ActorKind(StrEnum):
    OPERATOR = "operator"
    AGENT = "agent"


class Binding(StrEnum):
    FROZEN = "FROZEN"   # stage 시점에 풀린 api_ids를 그대로 쓴다
    LATE = "LATE"       # 실행 때마다 select_where를 다시 평가한다


class JobSchedule(BaseModel):
    kind: Literal["once", "daily"]
    at: str = Field(pattern=r"^\d{2}:\d{2}$")   # "09:00"
    tz: str = Field(default="Asia/Seoul", max_length=64)   # bounded: rule 6's message interpolates it
    from_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    count: int = Field(ge=1, le=30)
    done: int = Field(default=0, ge=0)   # occurrences already executed; the ledger owns it

    @field_validator("at")
    @classmethod
    def _at_is_a_real_time(cls, value: str) -> str:
        hh, _, mm = value.partition(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError(f"at must be a valid 24h time (00:00-23:59), got {value!r}")
        return value

    @field_validator("from_date")
    @classmethod
    def _from_date_is_a_real_date(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"from_date must be a valid calendar date, got {value!r}") from error
        return value

    @field_validator("tz")
    @classmethod
    def _tz_is_a_real_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as error:
            raise ValueError(f"unknown IANA timezone: {value!r}") from error
        return value

    @model_validator(mode="after")
    def _once_runs_exactly_once(self) -> JobSchedule:
        if self.kind == "once" and self.count != 1:
            raise ValueError("a 'once' schedule must have count == 1")
        return self


class TestDataSet(BaseModel):
    """One named parameter binding, applied identically to every environment in the job's
    matrix. These values are inputs the operator approves on the preview card — never facts
    about the system. The model may propose a set, but every invented value belongs in
    ``JobSpec.assumptions`` with a low confidence."""
    model_config = ConfigDict(extra="forbid")
    __test__ = False  # pytest: a domain type, not a test class
    label: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.\-가-힣 ]+$")
    values: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def _keys_and_values_are_bounded(self) -> TestDataSet:
        if len(self.values) > 20:
            raise ValueError(f"test data set binds too many parameters ({len(self.values)} > 20)")
        for key, value in self.values.items():
            if not 1 <= len(key) <= 60:
                raise ValueError(f"test data key must be 1-60 chars, got {len(key)} for {key[:20]!r}")
            if len(value) > 200:
                raise ValueError(f"test data value for {key!r} must be at most 200 chars, got {len(value)}")
        return self


class JobSpec(BaseModel):
    """LLM이 초안을 잡고 사람이 승인하는 실행 계획. StagedChange 미러.
    ``confidence``는 슬롯별 확신도(0~1)로 승인 카드가 낮은 항목을 강조하는 데 쓴다.
    ``assumptions``는 LLM이 기본값으로 채운 슬롯의 설명이다.
    한 job은 매트릭스다: 스케줄 1회마다 ``api_ids × target_envs × test_data`` 전체를 실행한다."""
    job_id: str
    kind: JobKind
    status: JobStatus = JobStatus.STAGED
    summary: str = Field(max_length=200)
    api_ids: list[str] = Field(default_factory=list)
    select_where: dict[str, Any] | None = None
    binding: Binding = Binding.FROZEN
    selection_basis: str | None = Field(default=None, max_length=160)
    target_envs: list[str] = Field(min_length=1)
    schedules: list[JobSchedule] = Field(default_factory=list)   # empty ⇒ run_now, one execution
    test_data: list[TestDataSet] = Field(default_factory=list)   # empty ⇒ no binding
    report: bool = True
    confidence: dict[str, float] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    guardrail_notes: list[str] = Field(default_factory=list)
    created_at: datetime
    created_by: str
    created_by_kind: ActorKind = ActorKind.OPERATOR
    applied_at: datetime | None = None
    applied_by: str | None = None
    discarded_at: datetime | None = None
    discarded_by: str | None = None
    discarded_by_kind: ActorKind | None = None
    run_ids: list[str] = Field(default_factory=list)
    executions: int = Field(default=0, ge=0)   # executions performed; run_now completes at 1

    # -- derived: pure functions of the stored fields, never persisted -------------------

    @property
    def matrix_size(self) -> int:
        """Runs one execution produces."""
        return len(self.api_ids) * len(self.target_envs) * max(1, len(self.test_data))

    @property
    def total_executions(self) -> int:
        """Executions the job is approved for; a job with no schedules runs its matrix once."""
        return sum(s.count for s in self.schedules) if self.schedules else 1

    @property
    def remaining_executions(self) -> int:
        return max(self.total_executions - self.executions, 0)

    @property
    def runs_total(self) -> int:
        return self.matrix_size * self.total_executions


# -- 화면→채팅 첨부 (open-design ChatCommentAttachment 계약) ---------------------------

class AttachedItem(BaseModel):
    order: int
    kind: Literal["run", "api", "job"]
    ref_id: str
    label: str = Field(max_length=120)
    field: str | None = Field(default=None, max_length=80)
    actual: str | None = Field(default=None, max_length=200)
    expected: str | None = Field(default=None, max_length=200)
    comment: str | None = Field(default=None, max_length=300)
    details: dict[str, str] = Field(default_factory=dict)

    @field_validator("details")
    @classmethod
    def _details_bounded(cls, value: dict[str, str]) -> dict[str, str]:
        return {str(k)[:40]: str(v)[:120] for k, v in list(value.items())[:8]}


# -- 세션 ------------------------------------------------------------------------------

class AtworksSessionContext(ClockContext):
    session_id: str
    project_id: str
    operator: str


class AtworksSessionState(BaseModel):
    seen_apis: dict[str, ApiSpec] = Field(default_factory=dict)
    seen_runs: dict[str, RunResult] = Field(default_factory=dict)
    seen_ranks: dict[str, FailedRank] = Field(default_factory=dict)
    seen_jobs: dict[str, JobSpec] = Field(default_factory=dict)
    last_population: int | None = None
    last_listed_run_ids: list[str] = Field(default_factory=list)
    last_listed_filter: str = "all"
    seen_groups: dict[str, RunGroup] = Field(default_factory=dict)   # key f"{group_by}:{group.key}"
    last_group_by: str | None = None
    last_aggregate_since: datetime | None = None
    approved_job_ids: set[str] = Field(default_factory=set)
    host_action_job_ids: set[str] = Field(default_factory=set)

    def remember_api(self, api: ApiSpec) -> None:
        remember(self.seen_apis, api.api_id, api)

    def remember_run(self, run: RunResult) -> None:
        remember(self.seen_runs, run.run_id, run)

    def remember_rank(self, rank: FailedRank) -> None:
        remember(self.seen_ranks, rank.run_id, rank)

    def remember_job(self, job: JobSpec) -> None:
        remember(self.seen_jobs, job.job_id, job)

    def remember_groups(self, group_by: str, groups: list[RunGroup], since: datetime | None) -> None:
        for group in groups:
            remember(self.seen_groups, f"{group_by}:{group.key}", group)
        self.last_group_by = group_by
        self.last_aggregate_since = since
