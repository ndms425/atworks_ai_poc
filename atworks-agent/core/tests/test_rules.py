from datetime import UTC, datetime

import pytest

from atworks_agent.rules import NAMED_FORMATS, RuleDraft, ValidationRule, evaluate, render_message
from atworks_agent.types import (  # noqa: F401 -- interface check: importable from types
    ActorKind,
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
