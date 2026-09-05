# Chat-authored Validation Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator state a value-validation rule in chat; the agent drafts a structured `ValidationRule`; a person approves it on a Rules page; from approval it applies to future runs only, never re-judging past results.

**Architecture:** A new `rules.py` owns a closed rule DSL (four kinds) and a pure evaluator. A `RuleLedger` (mirror of `JobLedger`) stages/applies/discards, stamping `effective_from` at apply. The backend gains rule methods; the Mock evaluates applied rules additively inside `execute_job_once` (a run fails if the legacy stub fails OR any applied rule whose `effective_from <= run.executed_at` fails) so no existing verdict changes. Approval is host-only via new `/api/atworks/rules/{id}/apply|discard` routes (a mirror of `job_action`). The chat `rule_preview` card and the Rules page both act through a rules-specific web hook, NOT web-shared's `/changes/` path.

**Tech Stack:** Python 3.11+, pydantic v2, FastAPI, `commerce_common` (import-only), pytest; Next.js 16 / React 19 / Tailwind 4 in `web/atworks-web` on copied `web/web-shared`.

**Spec:** `docs/superpowers/specs/2026-09-05-validation-rules-design.md`

## Global Constraints

- `refs/` read-only; `commerce_common` import-only.
- **Immutability (SI):** approving a rule never rewrites a stored `RunResult` or changes any success-rate/report/briefing figure. Rules carry `effective_from` (stamped at apply); a rule applies only to a run with `executed_at >= effective_from`. Evaluation is additive over the legacy stub — existing verdicts are unchanged.
- The model drafts rules as structured objects and never judges a run against them; evaluation lives in the backend.
- `param` must be a parameter of `api_id` as `get_api` declared it this session (provenance); `api_id` must be session-seen. Approval mark is set/cleared by the HTTP route only; chat cannot spend it.
- Caps are `AtworksAgentConfig` fields mirrored as tool-schema `maxItems`/enums (tool bytes a pure function of config; `test_same_config_same_bytes` holds).
- Windows: run from repo root, `.venv/Scripts/python.exe -m pytest -q`, `.venv/Scripts/python.exe -m ruff check atworks-agent host`; web `cd web/atworks-web && npm run build` then `git checkout -- web/atworks-web/next-env.d.ts` if it changed.
- Commit per task; end each commit message with a blank line then `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## File map

| File | Responsibility |
|---|---|
| `atworks-agent/core/atworks_agent/rules.py` (new) | `ValidationRule`, `RuleDraft`, `RuleImpact`, `NAMED_FORMATS`, `render_message`, `evaluate`, `RuleLedger`, `check_rule_guardrails` |
| `atworks-agent/core/atworks_agent/types.py` | `RuleStatus` enum; (ActorKind/RunStatus already exist) |
| `atworks-agent/core/atworks_agent/config.py` | `enable_rules`, `rule_review_policy`, `max_membership_values`, `max_rules_per_api`, allowed kind/op/format sets |
| `atworks-agent/core/atworks_agent/backend.py` | ABC: `stage_rule`/`get_pending_rules`/`apply_rule`/`discard_rule`/`list_rules`/`simulate_rule` |
| `atworks-agent/core/atworks_agent/gates.py` | `check_rule_param_provenance`, `check_apply_rule`, `check_discard_rule`, rule messages |
| `atworks-agent/core/atworks_agent/serialization.py` | `rule_record` (adds `change_id` alias) |
| `atworks-agent/core/atworks_agent/executor.py` | `_stage_rule`, `_apply_rule`, `_discard_rule`, `_get_pending_rules` handlers |
| `atworks-agent/core/atworks_agent/tools/registry.py` | `stage_rule`, `apply_rule`, `discard_rule`, `get_pending_rules`, `present_rule_preview` |
| `atworks-agent/core/atworks_agent/tools/presentation.py` | `RULE_PREVIEW_TOOL`, `PresentRulePreviewPayload` |
| `atworks-agent/core/atworks_agent/enrichment.py` | `enrich_rule_preview` |
| `atworks-agent/core/atworks_agent/prompt.py` | one hard line + rule contract when `enable_rules` |
| `atworks-agent/core/atworks_agent/__init__.py` | export the new public names |
| `atworks-agent/skills/rule-authoring/SKILL.md` (new) | the 5th skill |
| `host/atworks_host/mock_backend.py` | `RuleLedger` wiring, `simulate_rule`, applied-rule evaluation in `execute_job_once` |
| `host/atworks_host/app.py` | `rule_action`, `/rules`, `/rules/{id}/apply|discard` |
| `host/atworks_host/main.py` | (no change if the Mock owns the ledger) |
| `web/atworks-web/lib/types.ts` | `ValidationRule`, `RuleImpact`, `RulePreviewPayload`, `RuleKind` |
| `web/atworks-web/lib/api.ts` | `fetchRules`, `actOnRule` |
| `web/atworks-web/lib/useRuleActions.ts` (new) | local hook mirroring `useChangeActions` but posting to `/rules/` |
| `web/atworks-web/components/generative/RulePreviewCard.tsx` (new), `index.tsx` | chat card |
| `web/atworks-web/components/views/RulesView.tsx` (new) | Rules page |
| `web/atworks-web/app/page.tsx` | 5th nav item + view |
| `scripts/smoke_chat.py` | one rule-authoring turn |
| `CLAUDE.md`, `README.md` | decision record |

---

## Part 1 — Core rule model and evaluator

### Task 1: Rule DSL, evaluator, config, types

**Files:** Create `rules.py`; modify `types.py`, `config.py`, `__init__.py`; Test `atworks-agent/core/tests/test_rules.py`.

**Interfaces:**
- Produces: `RuleKind`, `CompareOp`, `NamedFormat`, `NAMED_FORMATS: dict[str,str]`, `ValidationRule`, `RuleDraft`, `RuleImpact`, `render_message(kind, param, op, value, values, format, pattern) -> str`, `evaluate(rule: ValidationRule, value: str | None) -> bool`, `RuleStatus`.

- [ ] **Step 1: `RuleStatus` enum** — in `types.py`, after `JobStatus`:
```python
class RuleStatus(StrEnum):
    STAGED = "staged"
    APPLIED = "applied"
    DISCARDED = "discarded"
```

- [ ] **Step 2: config fields** — in `config.py`, a new block after the scorer block:
```python
    # -- 검증 규칙 (chat-authored, apply-time effective) ------------------------------
    enable_rules: bool = True
    rule_review_policy: JobReviewPolicy = "always"
    max_membership_values: int = Field(default=50, ge=1)
    max_rules_per_api: int = Field(default=50, ge=1)
    allowed_rule_kinds: tuple[str, ...] = ("compare", "membership", "required", "format")
    allowed_compare_ops: tuple[str, ...] = (">=", ">", "<=", "<", "==", "!=")
    allowed_named_formats: tuple[str, ...] = ("email", "date", "iso8601", "uuid", "number")
```
Add a `stages_rules` property: `return self.enable_rules`. Extend `absent_tools()` so that when `not enable_rules` it removes `{"stage_rule","apply_rule","discard_rule","get_pending_rules","present_rule_preview"}`.

- [ ] **Step 3: Write the failing tests** (`test_rules.py`):
```python
from datetime import UTC, datetime

import pytest

from atworks_agent.rules import NAMED_FORMATS, RuleDraft, ValidationRule, evaluate, render_message
from atworks_agent.types import ActorKind, RuleStatus


def rule(**kw):
    base = dict(rule_id="rule-0001", api_id="api-1", param="amount", kind="compare", op=">=", value="0",
                message="amount >= 0", created_at=datetime.now(UTC), created_by="op")
    base.update(kw)
    return ValidationRule(**base)


def test_render_message_for_each_kind():
    assert render_message("compare", "refundAmount", ">=", "0", [], None, None) == "refundAmount >= 0"
    assert render_message("membership", "status", "in", None, ["PAID", "CANCELLED"], None, None) == "status in [PAID, CANCELLED]"
    assert render_message("required", "contractNo", None, None, [], None, None) == "contractNo required"
    assert render_message("format", "email", None, None, [], "email", None) == "email matches email"
    assert render_message("format", "code", None, None, [], None, "^[A-Z]{3}$") == "code matches /^[A-Z]{3}$/"


def test_compare_numeric_and_string():
    assert evaluate(rule(op=">=", value="0"), "5") is True
    assert evaluate(rule(op=">=", value="0"), "-3") is False
    assert evaluate(rule(op="==", value="PAID"), "PAID") is True     # non-numeric falls back to string
    assert evaluate(rule(op="!=", value="PAID"), "CANCELLED") is True


def test_membership():
    r = rule(kind="membership", op="in", value=None, values=["PAID", "CANCELLED"], message="x")
    assert evaluate(r, "PAID") is True and evaluate(r, "REFUNDED") is False
    r2 = rule(kind="membership", op="not_in", value=None, values=["X"], message="x")
    assert evaluate(r2, "Y") is True and evaluate(r2, "X") is False


def test_required_fails_on_missing_others_skip():
    req = rule(kind="required", op=None, value=None, message="p required")
    assert evaluate(req, None) is False and evaluate(req, "") is False and evaluate(req, "v") is True
    # a non-required rule with no value to test is skipped (passes)
    assert evaluate(rule(op=">=", value="0"), None) is True


def test_named_format_and_raw_pattern():
    email = rule(kind="format", op=None, value=None, format="email", message="email matches email")
    assert evaluate(email, "a@b.com") is True and evaluate(email, "nope") is False
    raw = rule(kind="format", op=None, value=None, pattern="^[A-Z]{3}$", review_required=True, message="x")
    assert evaluate(raw, "ABC") is True and evaluate(raw, "ab") is False
    assert set(NAMED_FORMATS) >= {"email", "date", "iso8601", "uuid", "number"}


def test_rule_draft_rejects_uncompilable_pattern():
    with pytest.raises(ValueError):
        RuleDraft(api_id="api-1", param="p", kind="format", pattern="([", created_by_kind=ActorKind.AGENT)


def test_rule_draft_requires_the_fields_its_kind_needs():
    with pytest.raises(ValueError):
        RuleDraft(api_id="api-1", param="p", kind="compare")            # op+value required
    with pytest.raises(ValueError):
        RuleDraft(api_id="api-1", param="p", kind="membership", op="in", values=[])   # non-empty set
    ok = RuleDraft(api_id="api-1", param="amount", kind="compare", op=">=", value="0")
    assert ok.review_required is False
```

- [ ] **Step 4: Run to verify failure** — `.venv/Scripts/python.exe -m pytest -q atworks-agent/core/tests/test_rules.py` → FAIL (ModuleNotFoundError).

- [ ] **Step 5: Implement `rules.py`** (the part 1 half — the ledger is Task 2, keep it in the same file but add later):
```python
"""검증 규칙 DSL과 순수 평가기. 모델은 자연어를 이 구조로 번역만 하고, 판정은 여기(그리고 실제
aTworks 엔진)가 한다. 규칙은 승인 시점(effective_from) 이후 실행에만 적용된다 — 과거는 안 건드린다."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from commerce_common.fencing import truncate_display
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
        pattern = NAMED_FORMATS[rule.format] if rule.format else (rule.pattern or "")
        try:
            return re.fullmatch(pattern, value) is not None
        except re.error:
            return False
    return True
```
Export from `__init__.py`: add `ValidationRule, RuleDraft, RuleImpact, evaluate, render_message, NAMED_FORMATS, RuleLedger, check_rule_guardrails` (RuleLedger/check_rule_guardrails land in Task 2 — add them to the export list now only if present; simplest: add all in Task 2's step). For Task 1, export `ValidationRule, RuleDraft, RuleImpact, evaluate` and `RuleStatus` (from types).

- [ ] **Step 6: Run tests** — `.venv/Scripts/python.exe -m pytest -q atworks-agent/core/tests/test_rules.py atworks-agent/core/tests/test_config.py` → PASS.

- [ ] **Step 7: Commit**
```bash
git add atworks-agent/core/atworks_agent/rules.py atworks-agent/core/atworks_agent/types.py atworks-agent/core/atworks_agent/config.py atworks-agent/core/atworks_agent/__init__.py atworks-agent/core/tests/test_rules.py
git commit -m "feat(core): validation-rule DSL and pure evaluator (compare/membership/required/format)"
```

### Task 2: `RuleLedger` and guardrails

**Files:** Modify `rules.py`, `gates.py`, `__init__.py`; Test `atworks-agent/core/tests/test_rules.py` (append), `test_gates.py`.

**Interfaces:** Produces `RuleLedger` (`stage/get/pending/applied/discard/apply` — apply stamps `effective_from`; `list(api_id=None)`), `check_rule_guardrails(draft, config, api: ApiSpec | None) -> list[str]`, `RuleGuardrailViolation`.

- [ ] **Step 1: Failing tests** (append to `test_rules.py`): stage returns a `rule-0001` id and STAGED; `apply` stamps `effective_from` and flips to APPLIED and refuses a non-staged id; `discard` records actor kind; `pending()`/`applied()`/`list(api_id)` filter; a guardrail test: `param` not in the API's params → violation; membership over `max_membership_values` → violation; more than `max_rules_per_api` applied for one API → violation. Mirror the shape of `test_jobs.py`'s ledger tests.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — add to `rules.py`:
```python
class RuleGuardrailViolation(ValueError):
    def __init__(self, violations: list[str]):
        super().__init__("; ".join(violations)); self.violations = violations


def check_rule_guardrails(draft: RuleDraft, config: "AtworksAgentConfig", api: "ApiSpec | None") -> list[str]:
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
    def __init__(self, config: "AtworksAgentConfig", apis: "Mapping[str, ApiSpec] | None" = None):
        self._config = config; self._apis = apis
        self._rules: dict[str, ValidationRule] = {}; self._sequence = 0

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
```
Add the needed imports (`Mapping` from collections.abc; `ApiSpec`, `AtworksAgentConfig` under `TYPE_CHECKING` to avoid a cycle, or import directly — `types` is safe to import; `config` import may cycle, so use string annotations + `TYPE_CHECKING`). Export `RuleLedger, RuleGuardrailViolation, check_rule_guardrails` from `__init__.py`.

- [ ] **Step 4: gates** — in `gates.py` add:
```python
def check_rule_param_provenance(state, api_id: str, param: str) -> ToolOutcome | None:
    api = state.seen_apis.get(api_id)
    if api is None:
        return ToolOutcome.held(PROVENANCE_GATE, f"api_id {api_id} was not read this session; call get_api first.")
    if param not in api.params:
        return ToolOutcome.held(PROVENANCE_GATE,
            f"{param!r} is not a parameter of {api_id} ({', '.join(api.params) or 'none'}); pick one get_api lists.")
    return None

def check_apply_rule(state, config, rule_id: str) -> ToolOutcome | None:
    if rule_id not in state.seen_rules:
        return ToolOutcome.held(PROVENANCE_GATE, f"rule {rule_id} was not staged or listed this session.")
    if config.require_host_approval and rule_id not in state.approved_rule_ids:
        return ToolOutcome.held(APPROVAL_GATE,
            f"rule {rule_id} is staged and waiting for approval on the Rules page; approving it there applies it.")
    return None

def check_discard_rule(state, rule_id: str) -> ToolOutcome | None:
    return None if rule_id in state.seen_rules else ToolOutcome.held(
        PROVENANCE_GATE, f"rule {rule_id} was not staged or listed this session.")

def take_rule_discard_actor_kind(state, rule_id: str) -> ActorKind:
    if rule_id in state.host_action_rule_ids:
        state.host_action_rule_ids.discard(rule_id); return ActorKind.OPERATOR
    return ActorKind.AGENT
```
This needs new `AtworksSessionState` fields — add in `types.py`: `seen_rules: dict[str, ValidationRule] = {}`, `approved_rule_ids: set[str] = set()`, `host_action_rule_ids: set[str] = set()`, and a `remember_rule` method (mirror `remember_job`). (Import `ValidationRule` in types.py would cycle — instead type `seen_rules` as `dict[str, Any]` with a comment, mirroring how nothing else in state imports heavy types, OR store rules as `dict[str, "ValidationRule"]` with `from __future__ import annotations` already present. `types.py` already has `from __future__ import annotations`, so a forward ref is fine, but the symbol must resolve for pydantic — pydantic needs the actual type. Simplest: put `ValidationRule` in `rules.py` importing from `types.py` (one direction), and keep `AtworksSessionState.seen_rules: dict[str, Any]`. Use `Any` to avoid the cycle; the executor stores real ValidationRule instances.)

- [ ] **Step 5: Run** full suite + ruff → PASS/clean.

- [ ] **Step 6: Commit** `feat(core): RuleLedger, rule guardrails and approval gates; session rule provenance`.

### Task 3: Backend ABC + Mock (ledger, simulate, additive evaluation)

**Files:** Modify `backend.py`, `serialization.py`, `host/atworks_host/mock_backend.py`; Test `host/tests/test_mock_backend.py`, new `host/tests/test_rules_backend.py`.

**Interfaces:** ABC async methods `stage_rule(session, draft, actor_kind)`, `get_pending_rules(session)`, `apply_rule(session, rule_id)`, `discard_rule(session, rule_id, actor_kind)`, `list_rules(session, api_id=None)`, `simulate_rule(session, draft) -> RuleImpact`. `rule_record(rule) -> dict` with `change_id`.

- [ ] **Step 1: `rule_record`** in `serialization.py` (mirror `job_record`): `model_dump(mode="json", exclude_none=True)` then `record["change_id"] = rule.rule_id`.

- [ ] **Step 2: ABC** — add the six abstract methods to `AtworksBackend` with docstrings; the `apply_rule` docstring states the effective-from obligation for REST, and `list_runs`-style ordering note is not needed. Add `simulate_rule` docstring: read-only, writes nothing, counts only reconstructable inputs.

- [ ] **Step 3: Failing Mock tests** (`test_rules_backend.py`): stage→apply stamps effective_from; **immutability**: capture `backend.runs` verdicts, apply a rule that would fail some, assert every existing `RunResult.status`/`failed_rules` is byte-identical afterward; **effective-from**: a job executed after apply picks the rule up (a new run gets the rule's message in failed_rules when it violates), a pre-existing run does not; `simulate_rule` returns a `RuleImpact` and writes nothing (ledger/run counts unchanged); `list_rules(api_id)` filters.

- [ ] **Step 4: Run to verify failure.**

- [ ] **Step 5: Implement Mock** — construct `self.rule_ledger = RuleLedger(config, self.apis)` in `__init__`. Implement the six methods delegating to the ledger; `simulate_rule` reconstructs inputs from `self.ledger` jobs (for each recent run of `draft.api_id` with a `test_data_label`, find the owning job in `self.ledger` and its `test_data` set by label → the bound value for `draft.param`; count would_fail via `evaluate(rule_from_draft, value)`; runs with no reconstructable value → excluded_unknown). Then in `execute_job_once`, after `stub_verdict` produces `(status, rules, http)`, additionally evaluate every applied rule for that api whose `effective_from <= now` against the binding's value for the rule's param:
```python
now = datetime.now(UTC)
extra_failed: list[str] = []
for r in self.rule_ledger.applied():
    if r.api_id != api_id or r.effective_from is None or r.effective_from > now:
        continue
    bound = data.values.get(r.param) if data is not None else None
    if not evaluate(r, bound):
        extra_failed.append(r.message)
if extra_failed:
    status = RunStatus.FAIL if status is RunStatus.PASS else status
    rules = [*rules, *extra_failed]
```
(Place this before building the `RunResult`. Import `evaluate` from `atworks_agent`.) This is additive — a run only gains failures, and only from applied rules effective at run time, so no fixture verdict changes (fixtures have no applied rules).

- [ ] **Step 6: Run** full suite + ruff.

- [ ] **Step 7: Commit** `feat(host): rule backend methods, read-only simulate, additive apply-time evaluation`.

### Task 4: Executor handlers

**Files:** Modify `executor.py`; Test `test_executor.py`.

**Interfaces:** handlers `_stage_rule`, `_apply_rule`, `_discard_rule`, `_get_pending_rules`; `_remember_and_preview_rule`.

- [ ] **Step 1: Failing tests**: `stage_rule` with an unknown api/param → `provenance` blocked; with a good param → stages, remembers the rule (`state.seen_rules`), emits a `change_update` + a `rule_preview` card (when `stage_shows_preview`); a bad membership (empty values) → `InvalidToolArgument`/error; `apply_rule` without the host mark → `approval` blocked; with `state.approved_rule_ids` set → applies. Mirror the job handler tests.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — register the four handlers. `_stage_rule` sanitizes `param`/`value`/`values`/`pattern`/`summary` through the fence, runs `check_rule_param_provenance`, `parse_argument(RuleDraft, {...})` (catch `ValidationError` → `InvalidToolArgument` named per field, reuse the existing pattern), then `check_rule_guardrails` (held on violations, using the seen API), then `backend.stage_rule`, then `_remember_and_preview_rule` (mirror `_remember_and_preview`, emitting `change_update(rule_record(rule))` + the `present_rule_preview` card). `_apply_rule`/`_discard_rule` mirror `_apply_job`/`_discard_job` using the rule gates and `rule_record`. Add `RULE_PREVIEW_TOOL` to `_previewed` bookkeeping analogous to jobs (a per-turn "previewed rules" set, or reuse the generic idea). Keep the question-form turn guard extended to `stage_rule`/`apply_rule` (add them to the `dispatch` guard's tool set).

- [ ] **Step 4: Run** full suite + ruff.

- [ ] **Step 5: Commit** `feat(core): executor stage_rule/apply_rule/discard_rule/get_pending_rules handlers`.

### Task 5: Tool schema, card, prompt, skill

**Files:** Modify `tools/registry.py`, `tools/presentation.py`, `enrichment.py`, `prompt.py`; new `atworks-agent/skills/rule-authoring/SKILL.md`; `scripts/smoke_chat.py`; Test `test_registry.py`, `test_presentation.py`, `test_prompt.py`, `test_skills_load.py`.

- [ ] **Step 1: Failing tests**: `EXPECTED` tool order gains `stage_rule, apply_rule, discard_rule, get_pending_rules` (after `discard_job`) and `present_rule_preview` (after `present_job_preview`); a config with `enable_rules=False` removes all five; `present_rule_preview` payload joins the staged rule + impact from the session; the prompt carries the rule hard line before `# How you work`; `test_skills_load` now expects 5 skills.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Payload** — `presentation.py`: `RULE_PREVIEW_TOOL = "present_rule_preview"`, `class PresentRulePreviewPayload(PresentationPayload): rule_id: str; headline: str | None; note: str | None`.

- [ ] **Step 4: Enrichment** — `enrich_rule_preview`: look up `context.state.seen_rules[payload.rule_id]` (refuse with `PROVENANCE_GATE` if absent), `enriched["rule"] = rule_record(rule)`, `enriched["change_id"] = rule.rule_id`, `enriched["review_required"] = rule.review_required`, `enriched["low_confidence"] = [k for k,v in rule.confidence if v < LOW_CONFIDENCE]`, `enriched["api"] = _record(seen_apis.get(rule.api_id))`, and `enriched["impact"] = <the RuleImpact the executor stashed>`. **Impact plumbing:** the executor calls `backend.simulate_rule(session, draft)` at stage time and stores the result on the rule-preview payload path — simplest: the executor, in `_stage_rule`, calls `simulate_rule` and passes the `RuleImpact` into the card by stashing it on `state` keyed by rule_id (`state.rule_impacts[rule_id] = impact`), and `enrich_rule_preview` reads it. Add `rule_impacts: dict[str, Any] = {}` to state. Register the component in `PRESENTATION_COMPONENTS` after `present_job_preview`.

- [ ] **Step 5: Registry** — add `stage_rule` (input: `api_id`, `param`, `kind` enum from `allowed_rule_kinds`, `op` enum from compare+membership ops, `value`, `values` (`maxItems: max_membership_values`), `format` enum from `allowed_named_formats`, `pattern`, `summary`, `confidence`, `assumptions`; `additionalProperties:false`), `apply_rule`/`discard_rule` (`rule_id`), `get_pending_rules` (no input), and the `present_rule_preview` card tool. Descriptions state: a rule is a structured proposal, applies only after Rules-page approval, only to future runs; prefer a named `format` over a raw `pattern`; `param` must come from get_api. Gate all five behind `config.stages_rules` via `absent_tools()`.

- [ ] **Step 6: Prompt** — when `config.stages_rules`, append a contract paragraph and one hard line: "You draft validation rules as structured objects; you never judge a run against a rule and you never change a past result. A rule applies only after a person approves it on the Rules page, and only to runs executed after that."

- [ ] **Step 7: Skill** — `rule-authoring/SKILL.md`: front-matter description (Korean+English: 값 검증 규칙을 자연어로 받아 구조화된 규칙 초안을 만든다). Body procedure: resolve the API (`get_api`) → identify the param → classify into compare/membership/required/format → build the structured rule (prefer a named format; a raw regex is flagged for review) → fill or ask missing slots (which param? the code list? the bound?) with confidence<0.5 / question form → `stage_rule` → one sentence: it is staged and applies from approval on the Rules page, past results unchanged.

- [ ] **Step 8: Smoke** — add `("환불 금액 refundAmount는 0 이상이어야 한다는 규칙 만들어줘", {"rule_preview"})` to `scripts/smoke_chat.py` `TURNS`; bump `test_smoke_script.py` length if it asserts a count.

- [ ] **Step 9: Run** full suite + ruff → PASS.

- [ ] **Step 10: Commit** `feat(core): stage_rule/apply_rule tools, rule_preview card, rule hard line, rule-authoring skill`.

---

## Part 2 — Host approval routes

### Task 6: `rule_action`, Rules routes

**Files:** Modify `host/atworks_host/app.py`; Test `host/tests/test_app.py`.

- [ ] **Step 1: Failing tests**: `GET /rules` returns `{rules: [...]}` (session); staging a rule via chat then `POST /rules/{id}/apply` marks-then-consumes the approval and returns `{ok, change}` with the applied rule; `POST /rules/{id}/discard` records operator kind; applying an unknown rule → `{ok: false}`; the mark cannot be spent by a chat turn (mirror the job apply-mark test).

- [ ] **Step 2: Route** — add `rule_action(rule_id, action, record)` mirroring `job_action`: remember the rule from the backend if unseen; set `approved_rule_ids`/`host_action_rule_ids`; build the executor; `await executor.execute("apply_rule"|"discard_rule", {"rule_id": rule_id})`; clear the marks; return `{ok, change}` from the `change_update` event. Add `@router.get("/rules")` → `{"rules": [rule_record(r) for r in await backend.list_rules(context(record))]}`, `@router.post("/rules/{rule_id}/apply")` and `.../discard` calling `rule_action`. Do NOT reuse `/changes/` (that is jobs).

- [ ] **Step 3: Run** full suite + ruff → PASS.

- [ ] **Step 4: Commit** `feat(host): Rules routes and host-only rule approval (rule_action mirror)`.

---

## Part 3 — Web

### Task 7: Rule types, hook, chat card

**Files:** Modify `web/atworks-web/lib/types.ts`, `lib/api.ts`, `components/generative/index.tsx`; new `lib/useRuleActions.ts`, `components/generative/RulePreviewCard.tsx`.

- [ ] **Step 1: Types** — `RuleKind`; `ValidationRule` (mirror the server record incl. `change_id`, `message`, `effective_from`, `review_required`, `status`, audit); `RuleImpact`; `RulePreviewPayload { rule_id; change_id; rule: ValidationRule; change?: ValidationRule; api?: ApiSpec; review_required: boolean; low_confidence: string[]; impact: RuleImpact; headline?; note? }`.

- [ ] **Step 2: api + hook** — `api.ts`: `fetchRules = () => api.get<{rules: ValidationRule[]}>("/rules")`; `actOnRule = (id, action) => api.post<{ok:boolean; change: ValidationRule | null}>('/rules/'+id+'/'+action, {})` (use the AgentApi's post; check its signature in `web-shared` and mirror how `actOnChange` posts). `useRuleActions.ts`: a small hook mirroring web-shared `useChangeActions` — holds `busy`/`error`, `act(action)` calls the passed `onAct(rule.change_id, action)`, updates local rule state from the returned change; `canAct` = status === "staged".

- [ ] **Step 3: Card** — `RulePreviewCard.tsx`: `GenCard`/`GenCardHeader`; rows for 대상 API, 파라미터, 규칙(`rule.message`), 종류, and when `review_required` a prominent 경고 row "직접 검토 필요: 정규식". An impact block: "이 규칙은 승인 시점 이후 실행부터 적용됩니다. 과거 결과·성공률은 그대로입니다." + "최근 {impact.window_runs}건 중 입력 아는 {impact.known_inputs}건, 그중 {impact.would_fail}건 해당" (omit the count line when `known_inputs === 0`). `ApproveBar` (web-shared, presentational) wired to `useRuleActions(payload.change ?? payload.rule, onRuleAct)`.

- [ ] **Step 4: Switch** — `generative/index.tsx`: add `case "rule_preview": return <RulePreviewCard payload={block.payload as RulePreviewPayload} onRuleAct={onRuleAction} />;` and thread an `onRuleAction` prop (bound to `actOnRule`) from `AssistantPanel`/`page.tsx` — NOT `onChangeAction` (that posts to /changes/).

- [ ] **Step 5: Build** `npm run build` clean; revert next-env churn.

- [ ] **Step 6: Commit** `feat(web): rule_preview card and rules action hook (posts to /rules, not /changes)`.

### Task 8: Rules page

**Files:** Modify `web/atworks-web/app/page.tsx`; new `components/views/RulesView.tsx`; optionally `components/views/ApisView.tsx` (rule count).

- [ ] **Step 1: RulesView** — `useResource(fetchRules, [refreshKey])`; group by status; each staged rule row shows `message`, target API, `review_required` flag, and an `ApproveBar` wired to `actOnRule`; applied rows show `effective_from` and who approved. Empty state "등록된 규칙이 없습니다."

- [ ] **Step 2: Nav** — add `{ id: "rules", label: "Rules", icon: "shield" }` (pick an existing web-shared icon name; if none fits, reuse "signal") to the `nav` list and a `{view === "rules" ? <RulesView refreshKey={refreshKey} onAct={actOnRule} /> : null}` branch; widen `PortalView` to include `"rules"`.

- [ ] **Step 3: Build** clean.

- [ ] **Step 4: Commit** `feat(web): Rules page listing staged/applied rules with host approval`.

---

## Part 4 — Docs, review, verification

### Task 9: Decision record, review, live smoke

**Files:** `CLAUDE.md`, `README.md`.

- [ ] **Step 1: CLAUDE.md** — extend "Flows covered" with a 5th skill `rule-authoring`; "Backend" with the rule methods; "Surfaces" with the Rules page and `/rules/{id}/apply|discard` and the `rule_preview` card; add one line stating rules are effective-from-apply and never re-judge past runs (the SI guarantee). Keep every existing line.

- [ ] **Step 2: README** — a short "Validation rules" paragraph: draft in chat, approve on the Rules page, applies to future runs only, four rule kinds.

- [ ] **Step 3: H3 review** — run `/review-commerce-agent` (or follow `.claude/commands/review-commerce-agent.md`); fix any mismatched decision-record rows.

- [ ] **Step 4: Live smoke** — start host+web; `python scripts/smoke_chat.py --turns <rule turn index>`; browser-capture: authoring a compare rule (refundAmount >= 0) → rule_preview card with the immutability note → Rules page Approve → applied with effective_from; a membership rule; a raw-regex rule showing the review flag; confirm `/runs/insights` and a report are byte-unchanged before/after approval (the immutability guarantee, visually).

- [ ] **Step 5: Commit** `docs: decision record and README for chat-authored validation rules`.

---

## Self-review notes

- **`/changes/` vs `/rules/` collision** (Task 6/7): web-shared `useMerchantChat.actOnChange` posts to `/changes/{id}/{action}` and the host's `/changes/{job_id}` is jobs-only. Rules therefore get their own `/rules/{id}/{action}` route and a local `useRuleActions`/`actOnRule`; the chat card and Rules page use that, never `chat.actOnChange`. Verified against `web/web-shared/portal/merchant.ts`.
- **Session-state cycle** (Task 2): `AtworksSessionState.seen_rules` is `dict[str, Any]` to avoid importing `ValidationRule` (in `rules.py`, which imports `types.py`) back into `types.py`. The executor stores real instances.
- **Additive evaluation** (Task 3): a run only ever gains failures from applied rules effective at its run time; fixtures have no applied rules, so `stub_verdict`-based tests and Home counts are unchanged — this is the mechanism behind the immutability guarantee.
- **Effective-from** is stamped in `RuleLedger.apply`, filtered in `execute_job_once`; past runs are never iterated for re-judgement anywhere.
- Type names across tasks: `ValidationRule`/`RuleDraft`/`RuleImpact`/`evaluate`/`render_message`/`NAMED_FORMATS` (Task 1); `RuleLedger`/`check_rule_guardrails`/`RuleGuardrailViolation` + state fields `seen_rules`/`approved_rule_ids`/`host_action_rule_ids`/`rule_impacts` + `remember_rule` (Task 2); backend six methods + `rule_record` (Task 3); handlers (Task 4); `RULE_PREVIEW_TOOL`/`PresentRulePreviewPayload`/`enrich_rule_preview` (Task 5); `rule_action` + routes (Task 6); web `useRuleActions`/`actOnRule`/`RulePreviewCard`/`RulesView` (Tasks 7–8).
