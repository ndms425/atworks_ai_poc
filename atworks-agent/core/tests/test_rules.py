from datetime import UTC, datetime

import pytest

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.rules import (
    FORMAT_EXAMPLES,
    NAMED_FORMATS,
    RuleDraft,
    RuleGuardrailViolation,
    RuleLedger,
    ValidationRule,
    check_rule_guardrails,
    evaluate,
    render_message,
)
from atworks_agent.types import (  # noqa: F401 -- interface check: importable from types
    ActorKind,
    ApiSpec,
    RuleStatus,
)


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


def test_format_examples_match_their_named_format_pattern():
    import re

    for name, example in FORMAT_EXAMPLES.items():
        assert re.fullmatch(NAMED_FORMATS[name], example) is not None, f"{name}: {example!r} does not match its pattern"


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


def test_rule_draft_rejects_unrecognized_named_format():
    with pytest.raises(ValueError):
        RuleDraft(api_id="api-1", param="p", kind="format", format="phone")


def test_evaluate_never_raises_on_unrecognized_format_bypassing_draft():
    # Constructed directly (bypassing RuleDraft's validation) so evaluate() must stay total.
    bad = rule(kind="format", op=None, value=None, format="phone", message="x")
    assert evaluate(bad, "555") is False


def test_evaluate_never_raises_on_unrecognized_compare_or_membership_op():
    # Constructed directly (bypassing RuleDraft's validation) so evaluate() must stay total,
    # same stance as the format case above: skip (pass) when we cannot evaluate.
    bad_op = rule(kind="compare", op="=>", value="0", message="x")
    assert evaluate(bad_op, "5") is True
    none_op = rule(kind="compare", op=None, value="0", message="x")
    assert evaluate(none_op, "5") is True
    bad_membership_op = rule(kind="membership", op="contains", value=None, values=["A"], message="x")
    assert evaluate(bad_membership_op, "A") is True
    none_membership_op = rule(kind="membership", op=None, value=None, values=["A"], message="x")
    assert evaluate(none_membership_op, "A") is True


# -- Task 2: check_rule_guardrails and RuleLedger ------------------------------------

CFG = AtworksAgentConfig(model="m")


def _draft(**over):
    base = dict(api_id="api-1", param="amount", kind="compare", op=">=", value="0")
    base.update(over)
    return RuleDraft(**base)


def _api(api_id="api-1", params=("amount",)):
    return ApiSpec(api_id=api_id, method="POST", path=f"/v1/{api_id}", name=api_id,
                   updated_at=datetime.now(UTC), params=list(params))


def test_check_rule_guardrails_param_not_in_api():
    v = check_rule_guardrails(_draft(param="unknown"), CFG, _api(params=["amount"]))
    assert any("unknown" in m and "not a parameter" in m for m in v)


def test_check_rule_guardrails_allows_known_param():
    assert check_rule_guardrails(_draft(param="amount"), CFG, _api(params=["amount"])) == []


def test_check_rule_guardrails_skips_param_check_without_api():
    assert check_rule_guardrails(_draft(param="unknown"), CFG, None) == []


def test_check_rule_guardrails_membership_over_limit():
    cfg = AtworksAgentConfig(model="m", max_membership_values=2)
    draft = _draft(kind="membership", op="in", value=None, values=["A", "B", "C"])
    v = check_rule_guardrails(draft, cfg, None)
    assert any("3 values" in m and "limit is 2" in m for m in v)


def test_check_rule_guardrails_named_format_not_allowed():
    cfg = AtworksAgentConfig(model="m", allowed_named_formats=("email",))
    draft = _draft(kind="format", op=None, value=None, format="uuid")
    v = check_rule_guardrails(draft, cfg, None)
    assert any("uuid" in m and "not one of" in m for m in v)


def test_ledger_stage_apply_discard():
    ledger = RuleLedger(CFG)
    rule_ = ledger.stage(_draft(), actor="op")
    assert rule_.rule_id == "rule-0001" and rule_.status is RuleStatus.STAGED
    applied = ledger.apply(rule_.rule_id, actor="op")
    assert applied.status is RuleStatus.APPLIED and applied.effective_from is not None
    with pytest.raises(RuleGuardrailViolation):
        ledger.discard(rule_.rule_id, actor="op")


def test_ledger_apply_refuses_non_staged_id():
    ledger = RuleLedger(CFG)
    with pytest.raises(RuleGuardrailViolation):
        ledger.apply("rule-9999", actor="op")


def test_ledger_discard_records_actor_kind():
    ledger = RuleLedger(CFG)
    rule_ = ledger.stage(_draft(), actor="op")
    discarded = ledger.discard(rule_.rule_id, actor="assistant", actor_kind=ActorKind.AGENT)
    assert discarded.status is RuleStatus.DISCARDED
    assert discarded.discarded_by_kind is ActorKind.AGENT
    assert discarded.discarded_by == "assistant"


def test_ledger_pending_applied_and_list_filter_by_api():
    ledger = RuleLedger(CFG)
    r1 = ledger.stage(_draft(api_id="api-1"), actor="op")
    r2 = ledger.stage(_draft(api_id="api-2"), actor="op")
    ledger.apply(r1.rule_id, actor="op")
    assert [r.rule_id for r in ledger.pending()] == [r2.rule_id]
    assert [r.rule_id for r in ledger.applied()] == [r1.rule_id]
    assert [r.rule_id for r in ledger.list(api_id="api-1")] == [r1.rule_id]
    assert {r.rule_id for r in ledger.list()} == {r1.rule_id, r2.rule_id}


def test_ledger_rejects_more_than_max_rules_per_api_applied():
    cfg = AtworksAgentConfig(model="m", max_rules_per_api=1)
    ledger = RuleLedger(cfg)
    first = ledger.stage(_draft(api_id="api-1", param="amount"), actor="op")
    ledger.apply(first.rule_id, actor="op")
    with pytest.raises(RuleGuardrailViolation):
        ledger.stage(_draft(api_id="api-1", param="status", kind="required"), actor="op")


def test_ledger_stage_uses_apis_mapping_for_param_guardrail():
    ledger = RuleLedger(CFG, apis={"api-1": _api(params=["amount"])})
    with pytest.raises(RuleGuardrailViolation):
        ledger.stage(_draft(api_id="api-1", param="unknown"), actor="op")


def test_ledger_apply_rejects_second_rule_once_cap_reached_by_a_prior_apply():
    # Both stage fine (each sees 0 APPLIED rules for api-1 at stage time); the cap must be
    # enforced again at apply, or two staged rules for one API can both reach APPLIED.
    cfg = AtworksAgentConfig(model="m", max_rules_per_api=1)
    ledger = RuleLedger(cfg)
    a = ledger.stage(_draft(api_id="api-1", param="amount"), actor="op")
    b = ledger.stage(_draft(api_id="api-1", param="amount", kind="required", op=None, value=None), actor="op")
    ledger.apply(a.rule_id, actor="op")
    with pytest.raises(RuleGuardrailViolation):
        ledger.apply(b.rule_id, actor="op")


def test_ledger_apply_rechecks_guardrails_under_current_config():
    # A rule that is valid at stage time but violates a param guardrail against the ledger's
    # own catalogue (added after staging) must still be caught at apply.
    ledger = RuleLedger(CFG)
    staged = ledger.stage(_draft(api_id="api-1", param="amount"), actor="op")
    ledger._apis = {"api-1": _api(api_id="api-1", params=["other"])}
    with pytest.raises(RuleGuardrailViolation):
        ledger.apply(staged.rule_id, actor="op")
