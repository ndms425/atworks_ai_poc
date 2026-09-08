"""aTworks 도메인 타입. merchant_agent/types.py의 구조를 따른다: 레코드(ApiSpec·RunResult),
스코어 결과(FailedRank), 스테이징 레코드(JobSpec), 세션 컨텍스트/상태. 상태의 seen_* 맵은
provenance 기록이다: 쓰기 게이트는 여기 있는 id만 받고, presentation은 여기서 값을 조인한다."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Generic, Literal, TypeVar
from zoneinfo import ZoneInfo

from commerce_common.types import ClockContext, remember
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# -- 페이지네이션/쿼리/집계 (scale, spec 2026-09-06) -------------------------------------
# Page는 모든 목록형 백엔드 메서드가 공유하는 읽기 봉투다: 커서 기반, 총계는 host 계산.
# RunsQuery/AggregateQuery는 extra="forbid" -- 모델이 낸 미지의 키는 조용히 묵살되지 않고 거부된다.

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int = 0


RunStatusFilter = Literal["all", "pass", "fail", "error", "non_pass"]
# aggregation.GROUP_BY의 다섯 값을 그대로 미러한다 -- aggregation.py가 이 타입에서 값을 가져간다
# (get_args), 그쪽 GROUP_BY 튜플을 손으로 다시 적지 않는다.
GroupBy = Literal["api", "failed_rule", "http_status", "env", "api_env_data"]


class RunsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    since: datetime | None = None
    until: datetime | None = None
    status: RunStatusFilter | None = None
    api_id: str | None = None
    executed_by: str | None = None
    job_id: str | None = None   # controller ruling: later tasks read a job's runs through this
    # Read the COLD partition instead of the hot one (spec §6): runs older than
    # ``retention_hot_days`` are moved to an archive table, never deleted, and are only ever
    # visible through an explicit ``archived=True`` read. Same predicates, same keyset order,
    # same envelope -- the archive is a different partition, not a different contract.
    archived: bool = False
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class AggregateQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    since: datetime | None = None
    until: datetime | None = None
    group_by: GroupBy
    scope_api_ids: list[str] | None = None
    # Server-side operator scope (Task 8 fix round 1): "the APIs this operator executed inside
    # this query's window", resolved by the BACKEND (a join on its operator/API index), never by
    # the caller shipping an id list. An id list is what capped a 16k-API operator's insight panel
    # at 100 alphabetically-first APIs; this predicate has no such ceiling. The window's lower
    # bound is this query's own ``since`` (no ``since`` = the operator's whole history).
    # Composable with ``scope_api_ids``: both narrow, neither widens.
    scope_operator: str | None = None
    # Task 8: the aggregate read is served from rollups, and a rollup row carries pass/fail/error
    # as separate counters -- so a status filter narrows which of those counters a group reports
    # (it is NOT a run-level scan). ``non_pass`` is fail+error, the triage population.
    status: RunStatusFilter | None = None
    # Evidence ids cost one indexed query per returned group (api/env/cell axes) or one bounded
    # scan (the map axes). A caller that only wants the counters sets this False and pays neither.
    include_run_ids: bool = True
    # Which `limit` groups the backend keeps (Task 8 fix round 2). The cut is the ONLY thing this
    # field moves -- counts, labels and derived fields are identical either way.
    #   "failures"    -- (fail+error) desc, count desc, key asc: the triage order, the default.
    #   "transitions" -- transitions desc, (fail+error) desc, key asc: the flaky_v1 order. A cell
    #     that flips pass<->non-pass but fails rarely is exactly what flaky_v1 names, and under
    #     "failures" it sits behind every louder cell in the project -- so a panel that ranks by
    #     failure volume can COUNT a quiet flaky cell (summarize_insights) but never NAME it.
    order_by: Literal["failures", "transitions"] = "failures"
    limit: int = Field(default=50, ge=1, le=500)


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
    response_body: dict[str, Any] | None = None
    job_id: str | None = None
    executed_by: str | None = None   # operator who approved the executing job; None = unattributed/legacy
    day: str | None = None           # local YYYY-MM-DD partition key; None = unpartitioned/legacy
    api_method: str | None = None    # denormalized from ApiSpec at execution time
    api_path: str | None = None      # denormalized from ApiSpec at execution time
    # Whether a response body was captured for this run, WITHOUT carrying it. A record read back
    # from storage never has `response_body` (no read path joins the bodies table -- only
    # `get_body` reads it), so this is the field that answers "is there a body to fetch?".
    # None = unknown/not read from storage; `serialization.run_record` falls back to
    # `response_body is not None`, which is what a freshly EXECUTED run carries.
    has_body: bool | None = None


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
    # Distinct APIs behind this group, counted over the whole window by the host -- filled only
    # where a single key spans several APIs (``failed_rule`` / ``http_status``). It is the TRUE
    # count (COUNT(DISTINCT api_id) over the rollup), never len(run_ids), which is capped at 50.
    api_count: int | None = None
    # A bounded SAMPLE of those api ids (<=20), for the insight panel's api_ids. Host-internal:
    # the model payload excludes it (executor._aggregate_runs), the count above is the figure.
    api_sample: list[str] | None = None


class Insights(BaseModel):
    flaky: int = 0
    regression_suspect: int = 0


# -- 인사이트 패널 (Home, per-operator) --------------------------------------------------

InsightKind = Literal["regression_suspect", "flaky_cell", "top_failed_rule", "env_divergence", "stale_pending"]


class InsightCandidate(BaseModel):
    """host가 결정론적으로 뽑은 후보 1건. figures는 host가 계산한 숫자/문자열만 담고, narration은
    이 값을 바꾸지 않는다."""
    candidate_id: str = Field(max_length=120)
    kind: InsightKind
    label: str = Field(max_length=120)
    figures: dict[str, int | str] = Field(default_factory=dict)
    api_ids: list[str] = Field(default_factory=list, max_length=20)
    ref_ids: list[str] = Field(default_factory=list, max_length=20)
    priority: int = 0


class InsightNarrative(BaseModel):
    """모델이 candidate 위에 얹는 설명 텍스트뿐 — figures/priority 등은 여기서 만들지 않는다."""
    candidate_id: str = Field(max_length=120)
    headline: str = Field(max_length=80)
    why_it_matters: str = Field(max_length=160)
    prompt: str = Field(max_length=120)


class InsightItem(BaseModel):
    candidate: InsightCandidate
    narrative: InsightNarrative | None = None


class InsightPanel(BaseModel):
    operator_id: str
    name: str
    role: OperatorRole
    scope_api_ids: list[str] = Field(default_factory=list)
    scope_size: int = 0   # true count of the operator's in-scope APIs; scope_api_ids is a bounded (<=20) sample
    scope_fallback: bool = False
    window_days: int
    generated_at: datetime
    generated_by: Literal["agent", "deterministic"]
    items: list[InsightItem] = Field(default_factory=list)


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


class ProfileStatus(StrEnum):
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
    # scale (spec 2026-09-06 §4): run_ids grew unbounded across a long-lived scheduled job's
    # lifetime. Task 5 stopped appending to it -- record_execution now advances run_count and
    # recent_run_ids (newest first, <=50) instead, and a reader that needs every run of a job
    # pages list_runs(RunsQuery(job_id=...)). The field stays for compatibility, frozen at
    # whatever it already held.
    run_count: int = 0
    recent_run_ids: list[str] = Field(default_factory=list, max_length=50)

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


class ValidationRule(BaseModel):
    """스테이징/저장되는 규칙. message는 서버 렌더값이며 위반 시 failed_rules에 그대로 실린다."""
    rule_id: str
    api_id: str
    param: str = Field(max_length=80)
    kind: Literal["compare", "membership", "required", "format"]
    op: str | None = None
    value: str | None = Field(default=None, max_length=120)
    values: list[str] = Field(default_factory=list)
    format: str | None = None
    pattern: str | None = Field(default=None, max_length=200)
    pass_examples: list[str] = Field(default_factory=list)
    fail_examples: list[str] = Field(default_factory=list)
    save_format_as: str | None = Field(default=None, max_length=60, pattern=r"^[a-z0-9][a-z0-9-]{0,59}$")
    # Display-only: set when `format` named a library entry (built-in or saved) that the
    # executor resolved into `pattern`/examples above at stage time. evaluate() never reads
    # this field -- it stays pattern-based and backend-independent (Task 3 ruling).
    format_name: str | None = Field(default=None, max_length=60, pattern=r"^[a-z0-9][a-z0-9-]{0,59}$")
    review_required: bool = False
    message: str = Field(max_length=200)
    status: RuleStatus = RuleStatus.STAGED
    effective_from: datetime | None = None
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


class ComparisonProfile(BaseModel):
    """값 동등성 비교의 ignore-spec. ValidationRule과 같은 라이프사이클(stage→host-approve→apply)을
    따른다: apply가 effective_from을 찍고, 과거 비교 결과는 절대 다시 판정하지 않는다. seen_profiles가
    이 모델(딕트가 아니라)로 역직렬화되도록 profiles.py가 아니라 여기 둔다."""
    profile_id: str
    job_id: str   # the parity run this profile targets
    ignore_paths: list[str] = Field(default_factory=list)
    per_api_ignore: dict[str, list[str]] = Field(default_factory=dict)
    status: ProfileStatus = ProfileStatus.STAGED
    effective_from: datetime | None = None
    summary: str = Field(max_length=200)
    created_at: datetime
    created_by: str
    created_by_kind: ActorKind = ActorKind.OPERATOR
    applied_at: datetime | None = None
    applied_by: str | None = None
    discarded_at: datetime | None = None
    discarded_by: str | None = None
    discarded_by_kind: ActorKind | None = None


class RuleRecommendation(BaseModel):
    """recommend_rules_for_api 출력 1건: 대상 API의 규칙 없는 param에 대해, 같은 이름의 param을 가진
    다른 API에 이미 적용된 규칙 하나를 제안으로 보여준다. 대응하는 peer가 없으면 그 param은 아무것도
    제안하지 않는다 — param 이름만으로 제약을 지어내지 않는다(no fabrication). 제안은 개별적으로
    stage_rule을 통해 스테이징되고, 각각 승인받는다."""
    param: str = Field(max_length=80)
    from_api_id: str
    rule: ValidationRule


class FormatBatchEntry(BaseModel):
    """FormatBatch 한 줄. outcome은 stage 시점에 계산된다: verify_examples 실패 → invalid(적용
    제외), 라이브러리에 이름/동일 패턴 이미 있음 → duplicate(건너뜀), 그 외 → new. reason은
    duplicate/invalid를 설명하며 new는 비운다."""
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,59}$")
    pattern: str = Field(max_length=200)
    pass_examples: list[str] = Field(default_factory=list)
    fail_examples: list[str] = Field(default_factory=list)
    outcome: Literal["new", "duplicate", "invalid"]
    reason: str | None = Field(default=None, max_length=200)


class FormatBatch(BaseModel):
    """대량 포맷 라이브러리 씨앗 요청 1건. ValidationRule의 라이프사이클을 미러(RuleStatus 재사용:
    STAGED/APPLIED/DISCARDED)한다. 라이브러리 포맷은 승인된 규칙이 참조하기 전까진 inert이므로
    승인 1회로 안전하다 — apply는 outcome이 new인 항목만 라이브러리에 더하고, duplicate/invalid는
    보고만 하고 절대 더하지 않는다."""
    batch_id: str
    status: RuleStatus = RuleStatus.STAGED
    summary: str | None = Field(default=None, max_length=200)
    entries: list[FormatBatchEntry] = Field(default_factory=list)
    created_at: datetime
    created_by: str
    created_by_kind: ActorKind = ActorKind.OPERATOR
    applied_at: datetime | None = None
    applied_by: str | None = None
    discarded_at: datetime | None = None
    discarded_by: str | None = None
    discarded_by_kind: ActorKind | None = None

    # -- derived: pure functions of the stored entries, never persisted ------------------

    @property
    def new_count(self) -> int:
        return sum(1 for e in self.entries if e.outcome == "new")

    @property
    def duplicate_count(self) -> int:
        return sum(1 for e in self.entries if e.outcome == "duplicate")

    @property
    def invalid_count(self) -> int:
        return sum(1 for e in self.entries if e.outcome == "invalid")


# -- 물질화 상태 / 감사 / 마스킹 (scale, spec 2026-09-06) --------------------------------
# 이 섹션의 모델들은 host-side 물질화 뷰(누적된 run 히스토리 위에서 빠르게 읽기 위한 캐시/집계
# 상태)와 감사 로그, PII 마스킹 정책을 담는다. LLM은 이 값들을 계산하지 않는다 -- host가 채운다.

class CellState(BaseModel):
    """api_id × target_env × test_data_label 한 셀의 최신 상태 (물질화 뷰 1행)."""
    api_id: str
    target_env: str
    test_data_label: str | None
    run_id: str
    status: RunStatus
    executed_at: datetime
    transitions_total: int


class ApiWatermark(BaseModel):
    """API 1개의 pass/non-pass 워터마크. flaky/regression 판정을 전체 히스토리 재스캔 없이 낸다."""
    api_id: str
    last_pass_at: datetime | None
    first_non_pass_at: datetime | None
    last_non_pass_at: datetime | None
    latest_status: RunStatus | None
    # The ApiSpec's own updated_at, denormalized onto the watermark row (spec §3's
    # ``api_watermark ... updated_at(api)``) so the regression rule
    # ``last_pass_at < api.updated_at <= first_non_pass_at`` is answerable from one indexed read
    # with no catalogue join on the caller's side. None when the API is unknown to the catalogue.
    api_updated_at: datetime | None = None


class ScopeSummary(BaseModel):
    """오퍼레이터/스코프 하나의 API 모집단 요약. api_ids는 표본(<=100), total이 실제 개수."""
    api_ids: list[str] = Field(default_factory=list, max_length=100)
    total: int = 0


class AuditEntry(BaseModel):
    """감사 로그 1행 -- append-only, seq가 전역 순서."""
    seq: int
    at: datetime
    operator: str
    action: str
    target_kind: str
    target_id: str
    session_id: str


class MaskingRule(BaseModel):
    name: str = Field(max_length=40)
    pattern: str = Field(max_length=200)
    replacement: str = Field(default="***", max_length=20)


class MaskingPolicy(BaseModel):
    rules: list[MaskingRule] = Field(default_factory=list, max_length=50)
    disabled_groups: list[str] = Field(default_factory=list, max_length=100)


# -- 자가발전 질의 엔진 (self-growth, spec 2026-09-07) -----------------------------------
# QuerySpec은 모델이 직접 채우는 도구 입력이다(query_runs) -- extra="forbid"로 미지의 키를 거부하고,
# 카탈로그(catalog.py)의 고정 enum만 실을 수 있다(캐시 안정 툴 바이트). 실제 SQL 컴파일은 host의
# Store.query()가 한다 -- 여기 있는 건 형태와 의미 규칙뿐, 숫자는 하나도 없다.

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
Dimension = Literal[
    "api", "path_segment_1", "path_segment_2", "path_prefix_2", "method", "api_group",
    "target_env", "test_data_label", "failed_rule", "http_status", "executed_by", "day", "week",
]
Measure = Literal["runs", "pass", "fail", "error", "non_pass", "fail_rate", "apis", "transitions", "p95_duration_ms"]


class QueryFilters(BaseModel):
    """query_runs 한 번의 모집단을 좁히는 조건. window_days와 since/until은 서로 대신하는 관계라
    함께 못 쓴다 -- 아래 검증기가 막는다."""
    model_config = ConfigDict(extra="forbid")
    status: RunStatusFilter | None = None
    since: datetime | None = None
    until: datetime | None = None
    window_days: int | None = Field(default=None, ge=1, le=180)   # since/until 대신
    api_ids: list[str] | None = Field(default=None, max_length=100)
    path_contains: list[str] | None = Field(default=None, max_length=5)   # 소문자 부분일치, OR
    path_prefix: str | None = Field(default=None, max_length=120)
    method: list[HttpMethod] | None = None
    api_group: list[str] | None = Field(default=None, max_length=10)
    target_env: list[str] | None = None
    test_data_label: list[str] | None = None
    executed_by: list[str] | None = Field(default=None, max_length=10)     # 오퍼레이터 id
    scope_operator: str | None = None                # "내가 실행한 API" 범위 (operator_api JOIN)
    failed_rule: list[str] | None = Field(default=None, max_length=10)
    http_status: list[int] | None = Field(default=None, max_length=10)

    @model_validator(mode="after")
    def _window_days_excludes_since_until(self) -> QueryFilters:
        if self.window_days is not None and (self.since is not None or self.until is not None):
            raise ValueError("window_days cannot be combined with since/until -- pick one way to bound the window")
        return self


class QuerySpec(BaseModel):
    """모델이 고르는 구조화 질의. 차원 최대 2개, 측정값 1~5개 -- 카탈로그(catalog.py) 밖의 값은
    pydantic이 거부한다. 정렬·상한·비교는 전부 host가 그대로 SQL로 옮긴다(spec §3)."""
    model_config = ConfigDict(extra="forbid")
    filters: QueryFilters = Field(default_factory=QueryFilters)
    dimensions: list[Dimension] = Field(default_factory=list, min_length=0, max_length=2)   # 0 = 전체 합계 1행
    measures: list[Measure] = Field(min_length=1, max_length=5)
    order_by: Measure | Literal["key"] = "non_pass"
    descending: bool = True
    limit: int = Field(default=20, ge=1, le=50)
    compare_previous_window: bool = False
    include_samples: bool = True

    @model_validator(mode="after")
    def _dimensions_are_unique(self) -> QuerySpec:
        if len(set(self.dimensions)) != len(self.dimensions):
            raise ValueError(f"dimensions must be unique, got {self.dimensions!r}")
        return self

    @model_validator(mode="after")
    def _fail_rate_needs_an_unfiltered_status(self) -> QuerySpec:
        # fail_rate is (fail+error)/runs over the rows the filter kept; under status=non_pass it
        # is 1.0 on every row and under status=pass it is 0.0 -- a denominator nobody asked for.
        # Refuse the combination with a hint instead of publishing a meaningless ratio.
        if "fail_rate" in self.measures and self.filters.status not in (None, "all"):
            raise ValueError(
                "fail_rate는 status 필터 없이 요청하세요 (전체 run 대비 실패·에러 비율). "
                "실패 건수만 필요하면 measures에 non_pass를 쓰고 status 필터를 유지하세요."
            )
        return self

    @model_validator(mode="after")
    def _order_by_is_key_or_a_requested_measure(self) -> QuerySpec:
        if self.order_by != "key" and self.order_by not in self.measures:
            # The DEFAULT ("non_pass") must never reject a spec whose measures simply omit it --
            # a model that asks for measures=["apis"] and says nothing about order gets the first
            # measure. Only an EXPLICIT order_by outside the requested measures is an error.
            if "order_by" not in self.model_fields_set:
                object.__setattr__(self, "order_by", self.measures[0])
                return self
            raise ValueError(f"order_by must be 'key' or one of measures {self.measures!r}, got {self.order_by!r}")
        return self


class QueryRow(BaseModel):
    """QueryResult 한 행. keys는 이번 질의의 dimensions에 대응하는 그룹 키(라벨 없음 sentinel은
    None), measures는 요청한 측정값(비교 시 `_prev`/`_delta`도 같은 딕트에 붙는다)."""
    keys: dict[str, str | None] = Field(default_factory=dict)
    measures: dict[str, float | int | None] = Field(default_factory=dict)
    api_ids: list[str] = Field(default_factory=list, max_length=20)
    run_ids: list[str] = Field(default_factory=list, max_length=5)


class QueryResult(BaseModel):
    """query_runs 실행 1회의 전체 결과. population/total_groups/rows 전부 host 계산 -- 모델은
    어느 행을 카드에 보여줄지만 고른다."""
    spec: QuerySpec
    rows: list[QueryRow] = Field(default_factory=list)
    total_groups: int = 0
    population: int = 0
    window: tuple[datetime, datetime]
    source: Literal["rollup_day", "rollup_key_day", "rollup_operator_day", "runs"]
    turn_id: str | None = None


AskOutcome = Literal["answered", "partial", "unmet", "action"]
UnmetReason = Literal["no_dimension", "no_evidence", "out_of_scope", "refused"]


class AskEntry(BaseModel):
    """ask_log 한 행 -- 한 턴에 하나. outcome/cluster_key는 결정론 규칙(asklog.py)이 낸다, 모델이
    스스로를 채점하지 않는다."""
    seq: int | None = None
    at: datetime
    session_id: str
    operator: str
    role: str | None = None
    question: str = Field(max_length=300)   # mask_body 적용 후 저장
    intent: str
    spec: QuerySpec | None = None
    outcome: AskOutcome
    unmet_reason: UnmetReason | None = None
    wanted: str | None = Field(default=None, max_length=200)
    tool_calls: int = 0
    cards: int = 0
    feedback: Literal["up", "down"] | None = None
    cluster_key: str
    turn_id: str


#: 어휘 항목의 수명주기. pending은 제안한 세션의 카드에서만 보이고, confirmed만 다른 오퍼레이터의
#: 컨텍스트에 들어가며, rejected는 쿨다운이 끝날 때까지 재제안을 막는다(self-growth spec §7).
VocabularyStatus = Literal["pending", "confirmed", "rejected"]


class VocabularyEntry(BaseModel):
    """조직 공유 어휘 한 항목 -- commerce_common.memory 사이드카(vocabulary 테이블)를 미러한다.
    fragment는 QueryFilters의 부분 조각이라 confirmed 상태에서만 컨텍스트에 주입된다."""
    term: str = Field(max_length=40)
    fragment: QueryFilters
    status: VocabularyStatus = "pending"
    proposed_by: str
    proposed_at: datetime
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    confirmations: int = 0
    uses: int = 0
    rejections: int = 0
    cooldown_until: datetime | None = None


#: 저장 질문의 두 상태. hidden은 사람이 Home에서 치운 것이고, 행은 남는다 -- cluster_key가
#: UNIQUE라 지우면 같은 군집이 다음 승격에서 새 카드로 되살아난다(self-growth spec §8).
SavedQuestionStatus = Literal["active", "hidden"]


class SavedQuestion(BaseModel):
    """승격된 저장 질문 1건 -- Promoter가 만들고, title은 catalog.title_for_spec()의 결정론
    산출물이다(모델이 쓴 문장이 아니다)."""
    id: str
    cluster_key: str
    spec: QuerySpec
    title: str = Field(max_length=120)
    created_at: datetime
    status: SavedQuestionStatus = "active"
    uses: int = 0
    last_used_at: datetime | None = None
    source_users: int = 0
    source_asks: int = 0


class GrowthSummary(BaseModel):
    """Growth 뷰 '이번 주' 타일이 쓰는 COUNT 전부. unmet_clusters의 각 원소는
    {cluster_key, reason, count, last_at, example} 모양(마스킹된 예시 1건)."""
    asks_total: int = 0
    answered: int = 0
    partial: int = 0
    unmet: int = 0
    up: int = 0
    down: int = 0
    new_terms: int = 0
    new_saved: int = 0
    unmet_clusters: list[dict[str, Any]] = Field(default_factory=list)
    #: 승격 기준 그대로 (`promote_min_users`/`promote_min_asks`/`promote_window_days`) -- 저장 질문
    #: 카드의 빈 상태가 "3명 이상이 5회 이상"이라고 말할 때 그 숫자는 **호스트 config에서** 온다.
    #: 화면에 상수로 박아 두면 config를 바꾼 배포에서 조용히 거짓말을 하게 된다(spec §8).
    thresholds: dict[str, int] = Field(default_factory=dict)


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


# -- 화면 상태 (navigate_screen/highlight_screen 지시어) --------------------------------

ScreenTargetKind = Literal["api", "run", "job", "rule"]


class ScreenTarget(BaseModel):
    kind: ScreenTargetKind
    ref_id: str = Field(max_length=64)
    label: str | None = Field(default=None, max_length=120)


class ScreenFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["all", "pass", "fail", "error"] | None = None
    query: str | None = Field(default=None, max_length=80)


class ScreenState(BaseModel):
    view: Literal["home", "apis", "runs", "jobs", "rules"]
    focus: ScreenTarget | None = None
    filter: ScreenFilter | None = None
    visible: list[ScreenTarget] = Field(default_factory=list, max_length=40)


# -- 세션 ------------------------------------------------------------------------------

OperatorRole = Literal["developer", "qa", "pm"]


class OperatorProfile(BaseModel):
    operator_id: str = Field(max_length=64, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(max_length=80)
    role: OperatorRole


class AtworksSessionContext(ClockContext):
    session_id: str
    project_id: str
    operator: str
    role: OperatorRole | None = None


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
    seen_rules: dict[str, ValidationRule] = Field(default_factory=dict)
    # Holds the staging turn's RuleImpact for the card; read within the same turn, so a
    # plain-dict reload (after session persistence round-trips it) is fine — it is not typed
    # like seen_rules and is never read again on a later turn.
    rule_impacts: dict[str, Any] = Field(default_factory=dict)
    approved_rule_ids: set[str] = Field(default_factory=set)
    host_action_rule_ids: set[str] = Field(default_factory=set)
    # Typed dict[str, FormatBatch] (not dict[str, Any]) so it survives the session JSON
    # round-trip -- the lesson from the earlier seen_rules bug.
    seen_format_batches: dict[str, FormatBatch] = Field(default_factory=dict)
    approved_format_batch_ids: set[str] = Field(default_factory=set)
    host_action_format_batch_ids: set[str] = Field(default_factory=set)
    # Typed dict[str, ComparisonProfile] (not dict[str, Any]) so it survives the session JSON
    # round-trip -- the same lesson as seen_rules/seen_format_batches.
    seen_profiles: dict[str, ComparisonProfile] = Field(default_factory=dict)
    approved_profile_ids: set[str] = Field(default_factory=set)
    host_action_profile_ids: set[str] = Field(default_factory=set)
    # Typed with the real model so it survives the session JSON round-trip
    current_screen: ScreenState | None = None
    # -- 자가발전 질의 (self-growth) --------------------------------------------------
    # The last query_runs result, typed with the real model (the seen_rules lesson) so a card
    # rendered after a session round-trip still reads real numbers. present_query_table is the
    # only reader: the card's every figure comes from here, never from a model sentence.
    last_query_result: QueryResult | None = None
    # The turn id the runtime stamps on this turn (set by Task 4's stream_turn); the query_table
    # card carries it so a 👍/👎 on the card can be matched back to the ask_log row.
    current_turn_id: str | None = None
    # -- 이번 턴 스크래치 (ask_log 분류의 입력) --------------------------------------
    # stream_turn이 턴 시작마다 비우고, executor가 채운다. `exclude=True`는 필수다: 이 셋은
    # 턴 안에서만 의미가 있는데 세션 문서에 실리면 매 턴 문서가 달라져 compare-and-set 쓰기가
    # 불필요하게 일어나고, 재접속한 세션이 남의 턴 도구 목록을 안고 시작한다.
    turn_tool_names: list[str] = Field(default_factory=list, exclude=True)
    turn_cards: int = Field(default=0, exclude=True)
    turn_unmet: tuple[UnmetReason, str, str] | None = Field(default=None, exclude=True)
    # 이번 턴에 propose_alias가 낸 제안들 (self-growth §7). 카드 푸터의 "‘결제 계열’을 …로
    # 해석했습니다 — 맞나요?"가 여기서 나온다. 세션 문서에 실리지 않는(`exclude=True`) 이유는 위와
    # 같고, 하나 더 있다: pending 별칭은 **제안한 세션 안에서만** 쓰인다(spec §2 조항 4). 다른
    # 오퍼레이터에게 보이는 유일한 경로는 확정 뒤의 컨텍스트 블록이지 이 목록이 아니다.
    pending_aliases: list[VocabularyEntry] = Field(default_factory=list, exclude=True)

    def remember_api(self, api: ApiSpec) -> None:
        remember(self.seen_apis, api.api_id, api)

    def remember_run(self, run: RunResult) -> None:
        remember(self.seen_runs, run.run_id, run)

    def remember_rank(self, rank: FailedRank) -> None:
        remember(self.seen_ranks, rank.run_id, rank)

    def remember_job(self, job: JobSpec) -> None:
        remember(self.seen_jobs, job.job_id, job)

    def remember_rule(self, rule: ValidationRule) -> None:
        remember(self.seen_rules, rule.rule_id, rule)

    def remember_format_batch(self, batch: FormatBatch) -> None:
        remember(self.seen_format_batches, batch.batch_id, batch)

    def remember_profile(self, profile: ComparisonProfile) -> None:
        remember(self.seen_profiles, profile.profile_id, profile)

    def remember_groups(self, group_by: str, groups: list[RunGroup], since: datetime | None) -> None:
        for group in groups:
            remember(self.seen_groups, f"{group_by}:{group.key}", group)
        self.last_group_by = group_by
        self.last_aggregate_since = since
