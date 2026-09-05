"""검증 규칙 DSL과 순수 평가기. 모델은 자연어를 이 구조로 번역만 하고, 판정은 여기(그리고 실제
aTworks 엔진)가 한다. 규칙은 승인 시점(effective_from) 이후 실행에만 적용된다 — 과거는 안 건드린다."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .types import ActorKind, RuleStatus

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
