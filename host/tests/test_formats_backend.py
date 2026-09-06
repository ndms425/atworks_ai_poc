from datetime import datetime, timedelta, timezone
from pathlib import Path

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksSessionContext,
    FormatBatchDraft,
    FormatDefinition,
    RuleDraft,
    RuleStatus,
    RunsQuery,
)
from atworks_host.mock_backend import MockAtworks

KST = timezone(timedelta(hours=9))
SESSION = AtworksSessionContext(session_id="s", project_id="mes", operator="minseong",
                                now=datetime(2026, 9, 3, 14, tzinfo=KST))


def _backend():
    return MockAtworks(AtworksAgentConfig(model="m"), Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures")


async def test_list_formats_is_seeded_with_the_five_builtins():
    b = _backend()
    names = {f.name for f in await b.list_formats(SESSION)}
    assert names == {"email", "date", "iso8601", "uuid", "number"}


async def test_get_format_returns_a_builtin_by_name():
    b = _backend()
    found = await b.get_format(SESSION, "email")
    assert found is not None and found.builtin is True


async def test_get_format_returns_none_for_an_unknown_name():
    b = _backend()
    assert await b.get_format(SESSION, "nope") is None


async def test_save_format_adds_a_new_one():
    b = _backend()
    defn = FormatDefinition(name="phone-digits", pattern=r"^\d{3}-\d{4}$",
                            pass_examples=["123-4567"], fail_examples=["abc"],
                            created_by="minseong", created_at=datetime.now(KST))
    added, reason = await b.save_format(SESSION, defn)
    assert added is True and reason is None
    found = await b.get_format(SESSION, "phone-digits")
    assert found is not None and found.pattern == r"^\d{3}-\d{4}$"


async def test_save_format_dedups_by_name():
    b = _backend()
    added, reason = await b.save_format(SESSION, FormatDefinition(name="email", pattern=r"^x$"))
    assert added is False and reason == "name exists"


async def test_save_format_dedups_by_identical_pattern():
    b = _backend()
    from atworks_agent.rules import NAMED_FORMATS
    added, reason = await b.save_format(SESSION, FormatDefinition(name="mail-like", pattern=NAMED_FORMATS["email"]))
    assert added is False and reason == "same pattern as email"


async def test_stage_rule_resolves_a_saved_format_by_name():
    b = _backend()
    await b.save_format(SESSION, FormatDefinition(
        name="phone-digits", pattern=r"^\d{3}-\d{4}$",
        pass_examples=["123-4567"], fail_examples=["abc"], created_by="minseong",
    ))
    defn = await b.get_format(SESSION, "phone-digits")
    assert defn is not None

    # Task 3's ruling: resolution to a pattern rule happens in the executor's _stage_rule,
    # not in RuleDraft/RuleLedger directly -- exercised here by building the draft the way
    # the executor would after resolving `format="phone-digits"` against the library.
    from atworks_agent.rules import evaluate
    draft = RuleDraft(api_id="api-001", param="amount", kind="format", pattern=defn.pattern,
                      pass_examples=defn.pass_examples, fail_examples=defn.fail_examples,
                      format_name="phone-digits", summary="phone format")
    rule = await b.stage_rule(SESSION, draft, ActorKind.AGENT)
    assert rule.format is None
    assert rule.pattern == r"^\d{3}-\d{4}$"
    assert rule.format_name == "phone-digits"
    assert evaluate(rule, "123-4567") is True
    assert evaluate(rule, "abc") is False


# -- Task 4: format-batch lifecycle --------------------------------------------------


async def test_stage_format_batch_then_apply_adds_only_new_entries():
    b = _backend()
    staged = await b.stage_format_batch(SESSION, FormatBatchDraft(formats=[
        {"name": "email", "pattern": r"^y$"},   # duplicate name (builtin)
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$",
         "pass_examples": ["123-4567"], "fail_examples": ["abc"]},   # new
    ]), ActorKind.AGENT)
    assert [e.outcome for e in staged.entries] == ["duplicate", "new"]

    pending = await b.get_pending_format_batches(SESSION)
    assert [p.batch_id for p in pending] == [staged.batch_id]

    applied = await b.apply_format_batch(SESSION, staged.batch_id)
    assert applied.status.value == "applied"
    names = {f.name for f in await b.list_formats(SESSION)}
    assert "phone-digits" in names
    assert len(names) == 6   # 5 builtins + the one new entry; the duplicate never landed


async def test_format_batch_apply_leaves_run_history_and_verdicts_untouched():
    # Immutability guarantee: a library format add is inert until an approved RULE references
    # it, so bulk-seeding the library must change no run's status/failed_rules.
    b = _backend()
    before = [r.model_dump(mode="json") for r in (await b.list_runs(SESSION, RunsQuery(limit=200))).items]
    staged = await b.stage_format_batch(SESSION, FormatBatchDraft(formats=[
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$",
         "pass_examples": ["123-4567"], "fail_examples": ["abc"]},
    ]), ActorKind.AGENT)
    await b.apply_format_batch(SESSION, staged.batch_id)
    after = [r.model_dump(mode="json") for r in (await b.list_runs(SESSION, RunsQuery(limit=200))).items]
    assert before == after


# -- Task 5: applying a rule promotes save_format_as into the format library ----------


async def test_apply_rule_promotes_save_format_as_into_the_format_library():
    b = _backend()
    staged = await b.stage_rule(SESSION, RuleDraft(
        api_id="api-001", param="amount", kind="format", pattern=r"^\d{3}-\d{4}$",
        pass_examples=["123-4567"], fail_examples=["abc"], save_format_as="phone-digits",
    ), ActorKind.AGENT)

    applied = await b.apply_rule(SESSION, staged.rule_id)

    assert applied.status == RuleStatus.APPLIED
    assert applied.guardrail_notes == []
    saved = await b.get_format(SESSION, "phone-digits")
    assert saved is not None
    assert saved.pattern == r"^\d{3}-\d{4}$"
    assert saved.pass_examples == ["123-4567"]
    assert saved.fail_examples == ["abc"]
    assert saved.created_by == "minseong"

    # the newly-saved format can now be referenced by name from a later stage_rule
    from atworks_agent.rules import evaluate
    defn = await b.get_format(SESSION, "phone-digits")
    draft2 = RuleDraft(api_id="api-003", param="paymentId", kind="format", pattern=defn.pattern,
                       pass_examples=defn.pass_examples, fail_examples=defn.fail_examples,
                       format_name="phone-digits", summary="reuse")
    rule2 = await b.stage_rule(SESSION, draft2, ActorKind.AGENT)
    assert rule2.format_name == "phone-digits"
    assert evaluate(rule2, "123-4567") is True


async def test_apply_rule_skips_promotion_on_name_collision_without_failing_apply():
    b = _backend()
    await b.save_format(SESSION, FormatDefinition(
        name="phone-digits", pattern=r"^\d{2}-\d{2}$",
        pass_examples=["12-34"], fail_examples=["x"], created_by="minseong",
    ))
    staged = await b.stage_rule(SESSION, RuleDraft(
        api_id="api-001", param="amount", kind="format", pattern=r"^\d{3}-\d{4}$",
        pass_examples=["123-4567"], fail_examples=["abc"], save_format_as="phone-digits",
    ), ActorKind.AGENT)

    applied = await b.apply_rule(SESSION, staged.rule_id)

    # the rule still applies -- promotion skipping never fails apply_rule
    assert applied.status == RuleStatus.APPLIED
    assert len(applied.guardrail_notes) == 1
    assert "phone-digits" in applied.guardrail_notes[0]
    assert "name exists" in applied.guardrail_notes[0]
    # the pre-existing library entry is untouched
    existing = await b.get_format(SESSION, "phone-digits")
    assert existing.pattern == r"^\d{2}-\d{2}$"


async def test_apply_rule_promotion_does_not_change_any_run_verdict():
    b = _backend()
    before = {run_id: (run.status, list(run.failed_rules)) for run_id, run in b.runs.items()}
    staged = await b.stage_rule(SESSION, RuleDraft(
        api_id="api-001", param="amount", kind="format", pattern=r"^\d{3}-\d{4}$",
        pass_examples=["123-4567"], fail_examples=["abc"], save_format_as="phone-digits",
    ), ActorKind.AGENT)
    await b.apply_rule(SESSION, staged.rule_id)
    after = {run_id: (run.status, list(run.failed_rules)) for run_id, run in b.runs.items()}
    assert after == before


async def test_apply_rule_without_save_format_as_does_not_touch_the_library():
    b = _backend()
    before = {f.name for f in await b.list_formats(SESSION)}
    staged = await b.stage_rule(SESSION, RuleDraft(api_id="api-001", param="customerId", kind="required"), ActorKind.AGENT)
    applied = await b.apply_rule(SESSION, staged.rule_id)
    assert applied.guardrail_notes == []
    after = {f.name for f in await b.list_formats(SESSION)}
    assert after == before


async def test_discard_format_batch():
    b = _backend()
    staged = await b.stage_format_batch(SESSION, FormatBatchDraft(formats=[
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$",
         "pass_examples": ["123-4567"], "fail_examples": ["abc"]},
    ]), ActorKind.AGENT)
    discarded = await b.discard_format_batch(SESSION, staged.batch_id, ActorKind.OPERATOR)
    assert discarded.status.value == "discarded"
    assert "phone-digits" not in {f.name for f in await b.list_formats(SESSION)}
