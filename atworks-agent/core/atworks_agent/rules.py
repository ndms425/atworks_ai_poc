"""검증 규칙 DSL과 순수 평가기. 모델은 자연어를 이 구조로 번역만 하고, 판정은 여기(그리고 실제
aTworks 엔진)가 한다. 규칙은 승인 시점(effective_from) 이후 실행에만 적용된다 — 과거는 안 건드린다."""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .types import ActorKind, ApiSpec, FormatBatch, FormatBatchEntry, RuleStatus, ValidationRule

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

FORMAT_EXAMPLES: dict[str, str] = {
    "email": "user@example.com",
    "date": "2026-09-05",
    "iso8601": "2026-09-05T09:00:00+09:00",
    "uuid": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
    "number": "1234",
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


def verify_examples(pattern: str, pass_examples: list[str], fail_examples: list[str]) -> list[str]:
    """Empty result = the pattern classifies every example correctly. Never raises."""
    try:
        compiled = re.compile(pattern)
    except re.error as error:
        return [f"pattern does not compile: {error}"]
    bad: list[str] = []
    for value in pass_examples:
        if compiled.fullmatch(value) is None:
            bad.append(f"pass example {value!r} does not match the pattern")
    for value in fail_examples:
        if compiled.fullmatch(value) is not None:
            bad.append(f"fail example {value!r} unexpectedly matches the pattern")
    return bad


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
    pass_examples: list[str] = Field(default_factory=list)
    fail_examples: list[str] = Field(default_factory=list)
    save_format_as: str | None = Field(default=None, max_length=60, pattern=r"^[a-z0-9][a-z0-9-]{0,59}$")
    # Set only by the executor when `format` named a library entry it resolved into `pattern`
    # above (Task 3 ruling): display-only, carried through to ValidationRule.format_name.
    # evaluate() never reads it -- it stays pattern-based and backend-independent.
    format_name: str | None = Field(default=None, max_length=60, pattern=r"^[a-z0-9][a-z0-9-]{0,59}$")
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
            if self.pattern is not None and self.format is None:
                if not self.pass_examples or not self.fail_examples:
                    raise ValueError(
                        "a raw pattern needs at least one pass example and one fail example"
                    )
                if bad := verify_examples(self.pattern, self.pass_examples, self.fail_examples):
                    raise ValueError(bad[0])
        return self

    def message(self) -> str:
        return render_message(self.kind, self.param, self.op, self.value, self.values, self.format, self.pattern)


class FormatDefinition(BaseModel):
    """이름 붙은 포맷 1건. 내장 5개는 builtin=True, read-only 씨앗. 저장된 항목은 operator/agent가
    save_format_as로 추가한 raw-pattern 규칙에서 온다."""
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,59}$")
    pattern: str = Field(max_length=200)
    pass_examples: list[str] = Field(default_factory=list)
    fail_examples: list[str] = Field(default_factory=list)
    builtin: bool = False
    created_at: datetime | None = None
    created_by: str | None = None


class FormatLibrary:
    """이름 붙은 포맷 저장소. 내장 5개는 read-only 씨앗. dedup: 이름 또는 동일 패턴; max_size 도달 시
    거부(skip_reason "library full") — config.max_format_library가 채워 넣는 상한."""

    def __init__(self, max_size: int = 200) -> None:
        self._max_size = max_size
        self._formats: dict[str, FormatDefinition] = {
            name: FormatDefinition(name=name, pattern=pattern, builtin=True,
                                   pass_examples=[FORMAT_EXAMPLES.get(name)] if FORMAT_EXAMPLES.get(name) else [])
            for name, pattern in NAMED_FORMATS.items()
        }

    def get(self, name: str) -> FormatDefinition | None:
        return self._formats.get(name)

    def list(self) -> list[FormatDefinition]:
        return list(self._formats.values())

    def has_pattern(self, pattern: str) -> str | None:
        return next((f.name for f in self._formats.values() if f.pattern == pattern), None)

    def add(self, defn: FormatDefinition) -> tuple[bool, str | None]:
        """Returns (added, skip_reason). Dedup by name then by identical pattern, then the size cap."""
        if defn.name in self._formats:
            return (False, "name exists")
        if (dup := self.has_pattern(defn.pattern)) is not None:
            return (False, f"same pattern as {dup}")
        if len(self._formats) >= self._max_size:
            return (False, "library full")
        self._formats[defn.name] = defn
        return (True, None)


class FormatBatchFormatInput(BaseModel):
    """stage_format_batch 입력의 포맷 1건, outcome 계산 이전 모양."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,59}$")
    pattern: str = Field(max_length=200)
    pass_examples: list[str] = Field(default_factory=list)
    fail_examples: list[str] = Field(default_factory=list)


class FormatBatchDraft(BaseModel):
    """stage_format_batch 입력이 검증·정규화된 뒤의 모양. 백엔드는 이걸 받아 FormatBatch를 만든다."""
    model_config = ConfigDict(extra="forbid")
    formats: list[FormatBatchFormatInput] = Field(min_length=1)
    summary: str | None = Field(default=None, max_length=200)
    created_by_kind: ActorKind = ActorKind.OPERATOR


class FormatBatchGuardrailViolation(ValueError):
    def __init__(self, violations: list[str]):
        super().__init__("; ".join(violations))
        self.violations = violations


def check_format_batch_guardrails(draft: FormatBatchDraft, config: AtworksAgentConfig) -> list[str]:
    violations: list[str] = []
    if len(draft.formats) > config.max_format_batch:
        violations.append(f"batch has {len(draft.formats)} formats; the limit is {config.max_format_batch}")
    return violations


def compute_batch_entries(
    formats: list[FormatBatchFormatInput], library: FormatLibrary, config: AtworksAgentConfig
) -> list[FormatBatchEntry]:
    """각 항목의 outcome을 계산한다: 이름/패턴이 라이브러리에 이미 있거나 이 배치의 앞선 항목과
    겹치면 duplicate(예제 품질과 무관 — 어차피 더해지지 않는다); 그 외 예제 수가
    config.min_format_examples 미만이거나 verify_examples가 실패를 보고하면 invalid; 그 외 new.
    배치 내부 중복도 라이브러리 중복과 같은 규칙으로 잡는다 — apply가 순서대로 library.add를 부를
    때 실제로 일어날 일과 맞춘다."""
    entries: list[FormatBatchEntry] = []
    seen_names: set[str] = set()
    seen_patterns: dict[str, str] = {}
    for item in formats:
        base = {"name": item.name, "pattern": item.pattern,
                "pass_examples": list(item.pass_examples), "fail_examples": list(item.fail_examples)}
        if item.name in seen_names or library.get(item.name) is not None:
            entries.append(FormatBatchEntry(**base, outcome="duplicate", reason="name exists"))
            continue
        if (dup := seen_patterns.get(item.pattern) or library.has_pattern(item.pattern)) is not None:
            entries.append(FormatBatchEntry(**base, outcome="duplicate", reason=f"same pattern as {dup}"))
            continue
        if (len(item.pass_examples) < config.min_format_examples
                or len(item.fail_examples) < config.min_format_examples):
            entries.append(FormatBatchEntry(**base, outcome="invalid",
                reason=f"needs at least {config.min_format_examples} pass and fail example(s)"))
            continue
        if bad := verify_examples(item.pattern, item.pass_examples, item.fail_examples):
            entries.append(FormatBatchEntry(**base, outcome="invalid", reason=bad[0]))
            continue
        entries.append(FormatBatchEntry(**base, outcome="new"))
        seen_names.add(item.name)
        seen_patterns[item.pattern] = item.name
    return entries


class FormatBatchLedger:
    """FormatBatch의 stage/apply/discard. RuleLedger를 미러한다. apply는 outcome이 new인 항목만
    library.add로 실제 라이브러리에 더한다 — duplicate/invalid는 절대 더하지 않는다."""

    def __init__(self, config: AtworksAgentConfig, library: FormatLibrary):
        self._config = config
        self._library = library
        self._batches: dict[str, FormatBatch] = {}
        self._sequence = 0

    def stage(self, draft: FormatBatchDraft, *, actor: str, actor_kind: ActorKind = ActorKind.OPERATOR) -> FormatBatch:
        if v := check_format_batch_guardrails(draft, self._config):
            raise FormatBatchGuardrailViolation(v)
        entries = compute_batch_entries(draft.formats, self._library, self._config)
        self._sequence += 1
        batch = FormatBatch(
            batch_id=f"format-batch-{self._sequence:04d}", summary=draft.summary, entries=entries,
            created_at=datetime.now(UTC), created_by=actor, created_by_kind=actor_kind)
        self._batches[batch.batch_id] = batch
        return batch

    def get(self, batch_id): return self._batches.get(batch_id)
    def pending(self): return [b for b in self._batches.values() if b.status is RuleStatus.STAGED]
    def applied(self): return [b for b in self._batches.values() if b.status is RuleStatus.APPLIED]

    def apply(self, batch_id: str, *, actor: str) -> FormatBatch:
        batch = self._require_staged(batch_id, "apply")
        entries = list(batch.entries)
        for i, entry in enumerate(entries):
            if entry.outcome != "new":
                continue
            added, skip_reason = self._library.add(FormatDefinition(
                name=entry.name, pattern=entry.pattern, pass_examples=list(entry.pass_examples),
                fail_examples=list(entry.fail_examples), created_at=datetime.now(UTC), created_by=actor,
            ))
            if not added:
                entries[i] = entry.model_copy(update={"outcome": "duplicate", "reason": skip_reason})
        updated = batch.model_copy(update={"status": RuleStatus.APPLIED, "applied_at": datetime.now(UTC),
                                           "applied_by": actor, "entries": entries})
        self._batches[batch_id] = updated
        return updated

    def discard(self, batch_id: str, *, actor: str, actor_kind: ActorKind = ActorKind.OPERATOR) -> FormatBatch:
        batch = self._require_staged(batch_id, "discard")
        updated = batch.model_copy(update={"status": RuleStatus.DISCARDED, "discarded_at": datetime.now(UTC),
                                           "discarded_by": actor, "discarded_by_kind": actor_kind})
        self._batches[batch_id] = updated
        return updated

    def _require_staged(self, batch_id: str, action: str) -> FormatBatch:
        batch = self._batches.get(batch_id)
        if batch is None:
            raise FormatBatchGuardrailViolation([f"no format batch {batch_id} to {action}"])
        if batch.status is not RuleStatus.STAGED:
            raise FormatBatchGuardrailViolation([f"format batch {batch_id} is {batch.status.value}, cannot {action}"])
        return batch


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
        op_fn = _COMPARE.get(rule.op)
        if op_fn is None:
            return True
        a, b = _as_number(value), _as_number(rule.value or "")
        if a is not None and b is not None:
            return op_fn(a, b)
        return op_fn(value, rule.value or "")
    if rule.kind == "membership":
        if rule.op == "in":
            return value in rule.values
        if rule.op == "not_in":
            return value not in rule.values
        return True
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
    if draft.kind == "format":
        if len(draft.pass_examples) > config.max_format_examples:
            violations.append(f"format rule has {len(draft.pass_examples)} pass examples; the limit is {config.max_format_examples}")
        if len(draft.fail_examples) > config.max_format_examples:
            violations.append(f"format rule has {len(draft.fail_examples)} fail examples; the limit is {config.max_format_examples}")
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
            pass_examples=list(draft.pass_examples), fail_examples=list(draft.fail_examples),
            save_format_as=draft.save_format_as, format_name=draft.format_name,
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
        applied_for_api = sum(1 for r in self._rules.values()
                              if r.api_id == rule.api_id and r.status is RuleStatus.APPLIED)
        if applied_for_api >= self._config.max_rules_per_api:
            raise RuleGuardrailViolation([f"{rule.api_id} already has {applied_for_api} applied rules; "
                                          f"the limit is {self._config.max_rules_per_api}"])
        draft = RuleDraft(api_id=rule.api_id, param=rule.param, kind=rule.kind, op=rule.op, value=rule.value,
                          values=list(rule.values), format=rule.format, pattern=rule.pattern,
                          pass_examples=list(rule.pass_examples), fail_examples=list(rule.fail_examples))
        api = self._apis.get(rule.api_id) if self._apis else None
        if violations := check_rule_guardrails(draft, self._config, api):
            raise RuleGuardrailViolation(violations)
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
