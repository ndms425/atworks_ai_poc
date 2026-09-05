from datetime import UTC, datetime

import pytest

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.rules import (
    FORMAT_EXAMPLES,
    NAMED_FORMATS,
    FormatBatchDraft,
    FormatBatchGuardrailViolation,
    FormatBatchLedger,
    FormatDefinition,
    FormatLibrary,
    RuleDraft,
    RuleGuardrailViolation,
    RuleLedger,
    ValidationRule,
    check_format_batch_guardrails,
    check_rule_guardrails,
    evaluate,
    render_message,
    verify_examples,
)
from atworks_agent.types import (  # noqa: F401 -- interface check: importable from types
    ActorKind,
    ApiSpec,
    FormatBatch,
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


def test_verify_examples_empty_when_pattern_classifies_everything_correctly():
    assert verify_examples(r"^\d+$", ["123", "0"], ["12a", "abc"]) == []


def test_verify_examples_reports_pass_example_that_does_not_match():
    bad = verify_examples(r"^\d+$", ["123", "12a"], ["abc"])
    assert any("12a" in m and "does not match" in m for m in bad)


def test_verify_examples_reports_fail_example_that_matches():
    bad = verify_examples(r"^\d+$", ["123"], ["999"])
    assert any("999" in m and "unexpectedly matches" in m for m in bad)


def test_verify_examples_reports_uncompilable_pattern_without_raising():
    bad = verify_examples("([", ["x"], ["y"])
    assert any("does not compile" in m for m in bad)


def test_rule_draft_with_raw_pattern_stages_with_good_examples():
    draft = RuleDraft(api_id="api-1", param="p", kind="format", pattern=r"^\d+$",
                      pass_examples=["123"], fail_examples=["12a"])
    assert draft.pass_examples == ["123"] and draft.fail_examples == ["12a"]


def test_rule_draft_with_raw_pattern_raises_naming_the_bad_fail_example():
    with pytest.raises(ValueError, match="999"):
        RuleDraft(api_id="api-1", param="p", kind="format", pattern=r"^\d+$",
                 pass_examples=["123"], fail_examples=["999"])


def test_rule_draft_with_raw_pattern_and_no_examples_raises():
    with pytest.raises(ValueError):
        RuleDraft(api_id="api-1", param="p", kind="format", pattern=r"^\d+$")


def test_rule_draft_review_required_true_for_fresh_raw_pattern_false_for_library_resolved():
    # Fix round 2 ruling: a raw pattern authored fresh still needs review; a format_name means
    # the executor resolved this against a trusted, already-verified library entry, so it must
    # NOT read as an unvetted raw regex.
    fresh = RuleDraft(api_id="api-1", param="p", kind="format", pattern=r"^\d+$",
                      pass_examples=["123"], fail_examples=["12a"])
    assert fresh.review_required is True
    resolved = RuleDraft(api_id="api-1", param="p", kind="format", pattern=r"^\d{3}-\d{4}$",
                         pass_examples=["123-4567"], fail_examples=["abc"], format_name="phone-digits")
    assert resolved.review_required is False


def test_rule_draft_with_named_format_needs_no_examples():
    draft = RuleDraft(api_id="api-1", param="email", kind="format", format="email")
    assert draft.pass_examples == [] and draft.fail_examples == []


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


def test_check_rule_guardrails_format_examples_over_limit():
    cfg = AtworksAgentConfig(model="m", max_format_examples=2)
    draft = _draft(kind="format", op=None, value=None, pattern=r"^\d+$",
                   pass_examples=["1", "2", "3"], fail_examples=["a"])
    v = check_rule_guardrails(draft, cfg, None)
    assert any("pass examples" in m and "limit is 2" in m for m in v)


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


def test_ledger_apply_rebuilds_raw_pattern_format_rule_with_examples():
    # apply() rebuilds a RuleDraft from the stored ValidationRule to recheck guardrails; a
    # raw-pattern FORMAT rule's RuleDraft needs pass_examples/fail_examples carried through
    # or its _kind_fields validator raises (the same class of bug check_apply_rule had).
    ledger = RuleLedger(CFG)
    staged = ledger.stage(_draft(kind="format", op=None, value=None, pattern=r"^\d+$",
                                  pass_examples=["123"], fail_examples=["12a"]), actor="op")
    applied = ledger.apply(staged.rule_id, actor="op")
    assert applied.status is RuleStatus.APPLIED


# -- Task 3: FormatDefinition / FormatLibrary ----------------------------------------


def test_format_library_seeded_with_five_builtins():
    lib = FormatLibrary()
    names = {f.name for f in lib.list()}
    assert names == {"email", "date", "iso8601", "uuid", "number"}
    assert all(f.builtin for f in lib.list())
    for name, pattern in NAMED_FORMATS.items():
        found = lib.get(name)
        assert found is not None and found.pattern == pattern


def test_format_library_add_stores_a_new_saved_format():
    lib = FormatLibrary()
    defn = FormatDefinition(name="phone-digits", pattern=r"^\d{3}-\d{4}$",
                            pass_examples=["123-4567"], fail_examples=["abc"], created_by="op")
    added, reason = lib.add(defn)
    assert added is True and reason is None
    assert lib.get("phone-digits") is defn
    assert lib.get("phone-digits").builtin is False


def test_format_library_add_dedups_by_name():
    lib = FormatLibrary()
    added, reason = lib.add(FormatDefinition(name="email", pattern=r"^x$"))
    assert added is False and reason == "name exists"


def test_format_library_add_dedups_by_identical_pattern():
    lib = FormatLibrary()
    added, reason = lib.add(FormatDefinition(name="mail-like", pattern=NAMED_FORMATS["email"]))
    assert added is False and reason == "same pattern as email"


def test_format_library_get_unknown_name_returns_none():
    assert FormatLibrary().get("nope") is None


def test_ledger_apply_rechecks_guardrails_under_current_config():
    # A rule that is valid at stage time but violates a param guardrail against the ledger's
    # own catalogue (added after staging) must still be caught at apply.
    ledger = RuleLedger(CFG)
    staged = ledger.stage(_draft(api_id="api-1", param="amount"), actor="op")
    ledger._apis = {"api-1": _api(api_id="api-1", params=["other"])}
    with pytest.raises(RuleGuardrailViolation):
        ledger.apply(staged.rule_id, actor="op")


# -- Task 4: FormatBatch / FormatBatchLedger -----------------------------------------


def _format_batch_ledger(cfg=CFG):
    return FormatBatchLedger(cfg, FormatLibrary(max_size=cfg.max_format_library))


def test_format_library_add_returns_library_full_at_the_cap():
    lib = FormatLibrary(max_size=5)   # 5 builtins already fill it
    added, reason = lib.add(FormatDefinition(name="phone-digits", pattern=r"^\d{3}-\d{4}$"))
    assert added is False and reason == "library full"


def test_stage_format_batch_computes_outcomes_duplicate_name_duplicate_pattern_new():
    ledger = _format_batch_ledger()
    batch = ledger.stage(FormatBatchDraft(formats=[
        {"name": "email", "pattern": r"^y$"},                              # duplicate name (builtin)
        {"name": "mail-like", "pattern": NAMED_FORMATS["email"]},          # duplicate pattern (builtin)
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$",
         "pass_examples": ["123-4567"], "fail_examples": ["abc"]},          # new
    ]), actor="op")
    assert batch.status is RuleStatus.STAGED
    outcomes = [e.outcome for e in batch.entries]
    assert outcomes == ["duplicate", "duplicate", "new"]
    assert batch.entries[0].reason == "name exists"
    assert batch.entries[1].reason == "same pattern as email"
    assert batch.new_count == 1 and batch.duplicate_count == 2 and batch.invalid_count == 0


def test_stage_format_batch_marks_a_misclassified_example_invalid():
    ledger = _format_batch_ledger()
    batch = ledger.stage(FormatBatchDraft(formats=[
        {"name": "bad-one", "pattern": r"^\d+$", "pass_examples": ["123"], "fail_examples": ["999"]},
    ]), actor="op")
    assert batch.entries[0].outcome == "invalid"
    assert "999" in batch.entries[0].reason and "unexpectedly matches" in batch.entries[0].reason
    assert batch.invalid_count == 1 and batch.new_count == 0


def test_stage_format_batch_intra_batch_duplicate_name_is_caught():
    ledger = _format_batch_ledger()
    batch = ledger.stage(FormatBatchDraft(formats=[
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$", "pass_examples": ["123-4567"], "fail_examples": ["abc"]},
        {"name": "phone-digits", "pattern": r"^\d{4}-\d{4}$", "pass_examples": ["1234-4567"], "fail_examples": ["abc"]},
    ]), actor="op")
    assert [e.outcome for e in batch.entries] == ["new", "duplicate"]


def test_apply_format_batch_adds_only_the_new_entry_to_the_library():
    lib = FormatLibrary(max_size=CFG.max_format_library)
    ledger = FormatBatchLedger(CFG, lib)
    batch = ledger.stage(FormatBatchDraft(formats=[
        {"name": "email", "pattern": r"^y$"},
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$", "pass_examples": ["123-4567"], "fail_examples": ["abc"]},
    ]), actor="op")
    applied = ledger.apply(batch.batch_id, actor="op")
    assert applied.status is RuleStatus.APPLIED
    assert lib.get("phone-digits") is not None
    assert {f.name for f in lib.list()} == {"email", "date", "iso8601", "uuid", "number", "phone-digits"}


def test_apply_format_batch_with_zero_new_entries_still_applies_and_adds_nothing():
    lib = FormatLibrary(max_size=CFG.max_format_library)
    before = {f.name for f in lib.list()}
    ledger = FormatBatchLedger(CFG, lib)
    batch = ledger.stage(FormatBatchDraft(formats=[{"name": "email", "pattern": r"^y$"}]), actor="op")
    assert batch.new_count == 0
    applied = ledger.apply(batch.batch_id, actor="op")
    assert applied.status is RuleStatus.APPLIED
    assert {f.name for f in lib.list()} == before


def test_apply_format_batch_at_the_cap_marks_the_overflow_entry_not_added():
    # 5 builtins seed the library; cap of 6 leaves room for exactly one more.
    cap_cfg = AtworksAgentConfig(model="m", max_format_library=6)
    lib = FormatLibrary(max_size=cap_cfg.max_format_library)
    ledger = FormatBatchLedger(cap_cfg, lib)
    batch = ledger.stage(FormatBatchDraft(formats=[
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$", "pass_examples": ["123-4567"], "fail_examples": ["abc"]},
        {"name": "zip-code", "pattern": r"^\d{5}$", "pass_examples": ["12345"], "fail_examples": ["abc"]},
    ]), actor="op")
    assert [e.outcome for e in batch.entries] == ["new", "new"]  # both look addable at stage time

    applied = ledger.apply(batch.batch_id, actor="op")

    assert applied.status is RuleStatus.APPLIED
    outcomes = [e.outcome for e in applied.entries]
    assert outcomes.count("new") == 1 and outcomes.count("duplicate") == 1
    overflow = applied.entries[outcomes.index("duplicate")]
    assert overflow.reason is not None and "full" in overflow.reason
    assert applied.new_count == 1
    # exactly one of the two actually landed in the library
    assert sum(1 for name in ("phone-digits", "zip-code") if lib.get(name) is not None) == 1


def test_apply_format_batch_refuses_non_staged_id():
    ledger = _format_batch_ledger()
    with pytest.raises(FormatBatchGuardrailViolation):
        ledger.apply("format-batch-9999", actor="op")


def test_discard_format_batch_records_actor_kind():
    ledger = _format_batch_ledger()
    batch = ledger.stage(FormatBatchDraft(formats=[
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$", "pass_examples": ["123-4567"], "fail_examples": ["abc"]},
    ]), actor="op")
    discarded = ledger.discard(batch.batch_id, actor="assistant", actor_kind=ActorKind.AGENT)
    assert discarded.status is RuleStatus.DISCARDED
    assert discarded.discarded_by_kind is ActorKind.AGENT
    assert discarded.discarded_by == "assistant"


def test_check_format_batch_guardrails_over_the_batch_size_limit():
    cfg = AtworksAgentConfig(model="m", max_format_batch=1)
    draft = FormatBatchDraft(formats=[
        {"name": "a", "pattern": r"^a$"}, {"name": "b", "pattern": r"^b$"},
    ])
    v = check_format_batch_guardrails(draft, cfg)
    assert any("2 formats" in m and "limit is 1" in m for m in v)


def test_stage_format_batch_over_the_batch_size_limit_raises():
    cfg = AtworksAgentConfig(model="m", max_format_batch=1)
    ledger = _format_batch_ledger(cfg)
    with pytest.raises(FormatBatchGuardrailViolation):
        ledger.stage(FormatBatchDraft(formats=[
            {"name": "a", "pattern": r"^a$"}, {"name": "b", "pattern": r"^b$"},
        ]), actor="op")


def test_format_batch_below_min_format_examples_is_invalid():
    cfg = AtworksAgentConfig(model="m", min_format_examples=2)
    ledger = _format_batch_ledger(cfg)
    batch = ledger.stage(FormatBatchDraft(formats=[
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$", "pass_examples": ["123-4567"], "fail_examples": ["abc"]},
    ]), actor="op")
    assert batch.entries[0].outcome == "invalid"
    assert "at least 2" in batch.entries[0].reason


def test_format_batch_raw_pattern_with_no_examples_is_invalid_even_when_min_format_examples_is_zero():
    # Fix round 2 ruling: the batch path must not let a 0-example unverified raw pattern into
    # the library just because config.min_format_examples is 0 -- align with the >=1/>=1 floor
    # RuleDraft._kind_fields enforces on the single-rule path.
    cfg = AtworksAgentConfig(model="m", min_format_examples=0)
    lib = FormatLibrary(max_size=cfg.max_format_library)
    ledger = FormatBatchLedger(cfg, lib)
    batch = ledger.stage(FormatBatchDraft(formats=[
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$"},
    ]), actor="op")
    assert batch.entries[0].outcome == "invalid"
    assert batch.new_count == 0 and batch.invalid_count == 1
    ledger.apply(batch.batch_id, actor="op")
    assert lib.get("phone-digits") is None


def test_apply_format_batch_never_touches_runs_or_a_rule_ledger():
    # Immutability guarantee: a bulk format add is a library-only write. Building a rule ledger
    # alongside and applying a format batch must not create or mutate any rule/run record.
    rule_ledger = RuleLedger(CFG)
    ledger = _format_batch_ledger()
    batch = ledger.stage(FormatBatchDraft(formats=[
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$", "pass_examples": ["123-4567"], "fail_examples": ["abc"]},
    ]), actor="op")
    ledger.apply(batch.batch_id, actor="op")
    assert rule_ledger.list() == []
