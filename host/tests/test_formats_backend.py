from datetime import datetime, timedelta, timezone
from pathlib import Path

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksSessionContext,
    FormatBatchDraft,
    FormatDefinition,
    RuleDraft,
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
    before = [r.model_dump(mode="json") for r in await b.list_runs(SESSION, limit=1000)]
    staged = await b.stage_format_batch(SESSION, FormatBatchDraft(formats=[
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$",
         "pass_examples": ["123-4567"], "fail_examples": ["abc"]},
    ]), ActorKind.AGENT)
    await b.apply_format_batch(SESSION, staged.batch_id)
    after = [r.model_dump(mode="json") for r in await b.list_runs(SESSION, limit=1000)]
    assert before == after


async def test_discard_format_batch():
    b = _backend()
    staged = await b.stage_format_batch(SESSION, FormatBatchDraft(formats=[
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$",
         "pass_examples": ["123-4567"], "fail_examples": ["abc"]},
    ]), ActorKind.AGENT)
    discarded = await b.discard_format_batch(SESSION, staged.batch_id, ActorKind.OPERATOR)
    assert discarded.status.value == "discarded"
    assert "phone-digits" not in {f.name for f in await b.list_formats(SESSION)}
