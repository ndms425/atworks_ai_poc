"""검증 규칙 DSL과 순수 평가기. 모델은 자연어를 이 구조로 번역만 하고, 판정은 여기(그리고 실제
aTworks 엔진)가 한다. 규칙은 승인 시점(effective_from) 이후 실행에만 적용된다 — 과거는 안 건드린다."""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .types import ActorKind, ApiSpec, RuleStatus

if TYPE_CHECKING:
    from .config import AtworksAgentConfig

RuleKind = Literal["compare", "membership", "required", "format"]
CompareOp = Literal[">=", ">", "<=", "<", "==", "!="]
MembershipOp = Literal["in", "not_in"]
NamedFormat = Literal["email", "date", "iso8601", "uuid", "number"]

NAMED_FORMATS: dict[str, str] = {
    "email": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    "date": r"^\d{4}-\d{2}-\d{2}$",
    "iso8601": r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?([.,]\d+)?(Z|[+-]\d{2}:?\d{2})?$",
    "uuid": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    "number": r"^-?\d+(\.\d+)?$",
}

_COMPARE = {
    ">=": lambda a, b: a >= b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
}


def render_message(kind: str, param: str, op: str | None, value: str | None,
                   values: list[str], format: str | None, pattern: str | None) -> str:
    if kind == "compare":
        return f"{param} {op} {value}"
    if kind == "membership":
        return f"{param} {op} [{', '.join(values)}]"
    if kind == "required":
        return f"{param} required"
    if kind == "format":
        return f"{param} matches {format}" if format else f"{param} matches /{pattern}/"
    return param


def _as_number(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


class ValidationRule(BaseModel):
    """스테이징/저장되는 규칙. message는 서버 렌더값이며 위반 시 failed_rules에 그대로 실린다."""
    rule_id: str
    api_id: str
    param: str = Field(max_length=80)
    kind: RuleKind
    op: str | None = None
    value: str | None = Field(default=None, max_length=120)
    values: list[str] = Field(default_factory=list)
    format: str | None = None
    pattern: str | None = Field(default=None, max_length=200)
    review_required: bool = False
    message: str = Field(max_length=200)
    status: RuleStatus = RuleStatus.STAGED
    effective_from: datetime | None = None
    confidence: dict[str, float] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    created_at: datetime
    created_by: str
    created_by_kind: ActorKind = ActorKind.OPERATOR
    applied_at: datetime | None = None
    applied_by: str | None = None
    discarded_at: datetime | None = None
    discarded_by: str | None = None
    discarded_by_kind: ActorKind | None = None


class RuleDraft(BaseModel):
    """stage_rule 입력이 검증·정규화된 뒤의 모양. 백엔드는 이걸 받아 ValidationRule을 만든다."""
    model_config = ConfigDict(extra="forbid")
    api_id: str
    param: str = Field(max_length=80)
    kind: RuleKind
    op: str | None = None
    value: str | None = Field(default=None, max_length=120)
    values: list[str] = Field(default_factory=list, max_length=200)
    format: str | None = None
    pattern: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=200)
    confidence: dict[str, float] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    created_by_kind: ActorKind = ActorKind.OPERATOR

    @property
    def review_required(self) -> bool:
        return self.kind == "format" and self.pattern is not None and self.format is None

    @model_validator(mode="after")
    def _kind_fields(self) -> RuleDraft:
        if self.kind == "compare":
            if self.op not in (">=", ">", "<=", "<", "==", "!=") or self.value is None:
                raise ValueError("compare rule needs op (one of >=,>,<=,<,==,!=) and value")
        elif self.kind == "membership":
            if self.op not in ("in", "not_in") or not self.values:
                raise ValueError("membership rule needs op (in|not_in) and a non-empty values list")
        elif self.kind == "format":
            if not (self.format or self.pattern):
                raise ValueError("format rule needs a named format or a pattern")
            if self.format is not None and self.format not in NAMED_FORMATS:
                raise ValueError(
                    f"named format {self.format!r} must be one of {', '.join(NAMED_FORMATS)}"
                )
            if self.pattern is not None:
                try:
                    re.compile(self.pattern)
                except re.error as error:
                    raise ValueError(f"pattern is not a valid regex: {error}") from error
        return self

    def message(self) -> str:
        return render_message(self.kind, self.param, self.op, self.value, self.values, self.format, self.pattern)


class RuleImpact(BaseModel):
    """simulate_rule의 읽기 전용 결과 — 아무것도 저장하지 않는다."""
    window_runs: int = 0        # recent runs of the API considered
    known_inputs: int = 0       # of those, how many have reconstructable input values
    would_fail: int = 0         # of known, how many the drafted rule would fail
    excluded_unknown: int = 0   # window_runs - known_inputs


def evaluate(rule: ValidationRule, value: str | None) -> bool:
    """True = 통과. required만 값 부재를 실패로 본다; 나머지는 값이 없으면 건너뛴다(통과)."""
    if rule.kind == "required":
        return value is not None and value != ""
    if value is None or value == "":
        return True
    if rule.kind == "compare":
        a, b = _as_number(value), _as_number(rule.value or "")
        if a is not None and b is not None:
            return _COMPARE[rule.op](a, b)
        return _COMPARE[rule.op](value, rule.value or "")
    if rule.kind == "membership":
        return (value in rule.values) if rule.op == "in" else (value not in rule.values)
    if rule.kind == "format":
        pattern = NAMED_FORMATS.get(rule.format, "") if rule.format else (rule.pattern or "")
        try:
            return re.fullmatch(pattern, value) is not None
        except re.error:
            return False
    return True


class RuleGuardrailViolation(ValueError):
    def __init__(self, violations: list[str]):
        super().__init__("; ".join(violations))
        self.violations = violations


def check_rule_guardrails(draft: RuleDraft, config: AtworksAgentConfig, api: ApiSpec | None) -> list[str]:
    violations: list[str] = []
    if api is not None and draft.param not in api.params:
        violations.append(f"param {draft.param!r} is not a parameter of {draft.api_id} "
                          f"({', '.join(api.params) or 'none declared'})")
    if draft.kind == "membership" and len(draft.values) > config.max_membership_values:
        violations.append(f"membership list has {len(draft.values)} values; the limit is {config.max_membership_values}")
    if draft.kind == "format" and draft.format is not None and draft.format not in config.allowed_named_formats:
        violations.append(f"named format {draft.format!r} is not one of {', '.join(config.allowed_named_formats)}")
    return violations


class RuleLedger:
    def __init__(self, config: AtworksAgentConfig, apis: Mapping[str, ApiSpec] | None = None):
        self._config = config
        self._apis = apis
        self._rules: dict[str, ValidationRule] = {}
        self._sequence = 0

    def stage(self, draft: RuleDraft, *, actor: str, actor_kind: ActorKind = ActorKind.OPERATOR) -> ValidationRule:
        api = self._apis.get(draft.api_id) if self._apis else None
        if v := check_rule_guardrails(draft, self._config, api):
            raise RuleGuardrailViolation(v)
        applied_for_api = sum(1 for r in self._rules.values()
                              if r.api_id == draft.api_id and r.status is RuleStatus.APPLIED)
        if applied_for_api >= self._config.max_rules_per_api:
            raise RuleGuardrailViolation([f"{draft.api_id} already has {applied_for_api} applied rules; "
                                          f"the limit is {self._config.max_rules_per_api}"])
        self._sequence += 1
        rule = ValidationRule(
            rule_id=f"rule-{self._sequence:04d}", api_id=draft.api_id, param=draft.param, kind=draft.kind,
            op=draft.op, value=draft.value, values=list(draft.values), format=draft.format, pattern=draft.pattern,
            review_required=draft.review_required, message=draft.message(),
            confidence=dict(draft.confidence), assumptions=list(draft.assumptions),
            created_at=datetime.now(UTC), created_by=actor, created_by_kind=actor_kind)
        self._rules[rule.rule_id] = rule
        return rule

    def get(self, rule_id): return self._rules.get(rule_id)
    def pending(self): return [r for r in self._rules.values() if r.status is RuleStatus.STAGED]
    def applied(self): return [r for r in self._rules.values() if r.status is RuleStatus.APPLIED]
    def list(self, api_id: str | None = None):
        return [r for r in self._rules.values() if api_id is None or r.api_id == api_id]

    def apply(self, rule_id: str, *, actor: str) -> ValidationRule:
        rule = self._require_staged(rule_id, "apply")
        updated = rule.model_copy(update={"status": RuleStatus.APPLIED, "applied_at": datetime.now(UTC),
                                          "applied_by": actor, "effective_from": datetime.now(UTC)})
        self._rules[rule_id] = updated
        return updated

    def discard(self, rule_id: str, *, actor: str, actor_kind: ActorKind = ActorKind.OPERATOR) -> ValidationRule:
        rule = self._require_staged(rule_id, "discard")
        updated = rule.model_copy(update={"status": RuleStatus.DISCARDED, "discarded_at": datetime.now(UTC),
                                          "discarded_by": actor, "discarded_by_kind": actor_kind})
        self._rules[rule_id] = updated
        return updated

    def _require_staged(self, rule_id: str, action: str) -> ValidationRule:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise RuleGuardrailViolation([f"no rule {rule_id} to {action}"])
        if rule.status is not RuleStatus.STAGED:
            raise RuleGuardrailViolation([f"rule {rule_id} is {rule.status.value}, cannot {action}"])
        return rule
