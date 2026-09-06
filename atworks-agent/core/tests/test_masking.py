from datetime import datetime

from atworks_agent import AtworksAgentConfig
from atworks_agent.masking import (
    DEFAULT_MASKING_RULES,
    body_capture_enabled,
    mask_body,
    policy_from_config,
)
from atworks_agent.parity import compare_bodies
from atworks_agent.types import ApiSpec, MaskingPolicy, MaskingRule

DEFAULT_POLICY = MaskingPolicy(rules=DEFAULT_MASKING_RULES)


def _rule(name: str) -> MaskingRule:
    return next(r for r in DEFAULT_MASKING_RULES if r.name == name)


def _api(api_id: str, group: str | None) -> ApiSpec:
    return ApiSpec(api_id=api_id, method="GET", path="/x", name="x", group=group,
                    updated_at=datetime(2026, 9, 1))


# -- five default rules: positive sample masked, negative sample left alone ----------------------
# Each rule is exercised through a policy carrying ONLY that rule -- the five patterns overlap
# by design (e.g. a bare 13-digit run matches both krn-resident-id and account-number), so a
# rule-specific negative sample is only meaningful in isolation from its siblings.

def _only(name: str) -> MaskingPolicy:
    return MaskingPolicy(rules=[_rule(name)])


def test_krn_resident_id_rule_masks_a_resident_id_and_leaves_an_unrelated_number():
    rule = _rule("krn-resident-id")
    assert rule.pattern == r"\b\d{6}-?[1-4]\d{6}\b"
    policy = _only("krn-resident-id")
    assert mask_body("900101-1234567", policy) == "***"
    # 7th digit outside 1-4 -- not resident-id shaped
    assert mask_body("order 1234567890123", policy) == "order 1234567890123"


def test_card_number_rule_masks_a_card_number_and_leaves_a_short_number():
    rule = _rule("card-number")
    assert rule.pattern == r"\b(?:\d[ -]?){15,16}\b"
    policy = _only("card-number")
    assert mask_body("4111-1111-1111-1111", policy) == "***"
    # fewer than 15 digits -- not card-number shaped
    assert mask_body("order 12345678", policy) == "order 12345678"


def test_account_number_rule_masks_an_account_number_and_leaves_an_8_digit_order_number():
    rule = _rule("account-number")
    assert rule.pattern == r"\b\d{3}-?\d{2,6}-?\d{4,8}\b"
    policy = _only("account-number")
    assert mask_body("123-45-6789012", policy) == "***"
    # an 8-digit order number must NOT be caught by account-number (too few digits for the
    # 3+2+4=9 digit minimum split even with no dashes)
    assert mask_body("12345678", policy) == "12345678"


def test_phone_rule_masks_a_mobile_number_and_leaves_a_landline():
    rule = _rule("phone")
    assert rule.pattern == r"\b01[016789]-?\d{3,4}-?\d{4}\b"
    policy = _only("phone")
    assert mask_body("010-1234-5678", policy) == "***"
    # a landline (02-xxx-xxxx) does not start with 01[016789] -- left alone
    assert mask_body("call 02-123-4567", policy) == "call 02-123-4567"


def test_email_rule_masks_a_bare_email_and_leaves_plain_text():
    rule = _rule("email")
    from atworks_agent.rules import NAMED_FORMATS
    # the format-library asset is reused minus its whole-value anchors: a validation rule must
    # match the whole value, a masking rule must find the shape inside prose
    assert rule.pattern == NAMED_FORMATS["email"][1:-1]
    assert not rule.pattern.startswith("^") and not rule.pattern.endswith("$")
    policy = _only("email")
    assert mask_body("hong@example.com", policy) == "***"
    assert mask_body("not an email", policy) == "not an email"


def test_email_rule_masks_an_email_embedded_in_prose():
    policy = _only("email")
    assert mask_body("문의: hong@example.com 으로 연락주세요", policy) == "문의: *** 으로 연락주세요"
    assert mask_body({"note": "cc a@b.co and c@d.org"}, policy) == {"note": "cc *** and ***"}


# -- recursion: nested dict/array leaves masked, keys untouched, non-strings untouched -----------

def test_mask_body_recurses_over_nested_dicts_and_arrays():
    body = {
        "user": {"contact": "hong@example.com", "id": "900101-1234567"},
        "tags": ["safe", "990101-2345678", "also-safe"],
        "count": 5,
        "active": True,
        "rate": 1.5,
        "note": None,
    }
    masked = mask_body(body, DEFAULT_POLICY)
    assert masked == {
        "user": {"contact": "***", "id": "***"},
        "tags": ["safe", "***", "also-safe"],
        "count": 5,
        "active": True,
        "rate": 1.5,
        "note": None,
    }
    # dict keys themselves are never touched even if they look PII-shaped
    weird = {"900101-1234567": "value"}
    assert mask_body(weird, DEFAULT_POLICY) == {"900101-1234567": "value"}


def test_mask_body_leaves_ints_and_bools_untouched_even_when_default_rules_would_match_a_string():
    assert mask_body(9001011234567, DEFAULT_POLICY) == 9001011234567
    assert mask_body(True, DEFAULT_POLICY) is True


# -- policy_from_config ----------------------------------------------------------------------

def test_policy_from_config_masking_enabled_false_empties_rules_but_keeps_disabled_groups():
    cfg = AtworksAgentConfig(model="m", masking_enabled=False, masking_disabled_groups=("payment",))
    policy = policy_from_config(cfg)
    assert policy.rules == []
    assert policy.disabled_groups == ["payment"]
    body = {"contact": "hong@example.com"}
    assert mask_body(body, policy) == body   # no rules -> no-op


def test_policy_from_config_masking_enabled_true_carries_the_five_default_rules():
    cfg = AtworksAgentConfig(model="m")
    policy = policy_from_config(cfg)
    assert {r.name for r in policy.rules} == {r.name for r in DEFAULT_MASKING_RULES}
    assert policy.disabled_groups == []


# -- body_capture_enabled: per-group opt-out -----------------------------------------------------

def test_body_capture_enabled_is_false_only_for_a_disabled_group():
    policy = MaskingPolicy(disabled_groups=["payment"])
    payment_api = _api("api-004", "payment")
    contract_api = _api("api-001", "contract")
    ungrouped_api = _api("api-099", None)
    assert body_capture_enabled(payment_api, policy) is False
    assert body_capture_enabled(contract_api, policy) is True
    assert body_capture_enabled(ungrouped_api, policy) is True


# -- compare_bodies over masked pairs --------------------------------------------------------

def test_compare_bodies_on_masked_pair_matches_the_unmasked_pair_when_pii_is_identical():
    """The masked PII substrings are identical on both sides, so masking removes nothing the
    comparison relied on -- the real (non-PII) diff on `amount` survives unchanged."""
    a = {"contact": "same@example.com", "amount": 100}
    b = {"contact": "same@example.com", "amount": 200}
    unmasked = compare_bodies(a, b, [])
    masked = compare_bodies(mask_body(a, DEFAULT_POLICY), mask_body(b, DEFAULT_POLICY), [])
    assert masked == unmasked
    assert masked.diff_paths == ["$.amount"]


def test_compare_bodies_on_masked_pair_that_differs_only_in_pii_becomes_equal_by_design():
    """Two bodies that differ ONLY in PII-shaped values become indistinguishable once both are
    masked to the same replacement -- this is intentional (masking is a pure, one-way, capture
    -time transform; parity judges only what a person is allowed to see), not a bug."""
    a = {"contact": "hong@example.com", "id": "900101-1234567", "amount": 100}
    b = {"contact": "kim@example.com", "id": "900101-7654321", "amount": 100}
    unmasked = compare_bodies(a, b, [])
    assert unmasked.equal is False
    assert set(unmasked.diff_paths) == {"$.contact", "$.id"}
    masked = compare_bodies(mask_body(a, DEFAULT_POLICY), mask_body(b, DEFAULT_POLICY), [])
    assert masked.equal is True
    assert masked.diff_paths == []
