from datetime import datetime, timedelta, timezone
from pathlib import Path

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksSessionContext,
    JobDraft,
    JobKind,
    RuleDraft,
    RuleImpact,
    RuleStatus,
    RunStatus,
    TestDataSet,
)
from atworks_host.mock_backend import MockAtworks

KST = timezone(timedelta(hours=9))
SESSION = AtworksSessionContext(session_id="s", project_id="mes", operator="minseong",
                                now=datetime(2026, 9, 3, 14, tzinfo=KST))


def _backend():
    return MockAtworks(AtworksAgentConfig(model="m"), Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures")


async def test_stage_then_apply_rule_stamps_effective_from():
    b = _backend()
    rule = await b.stage_rule(SESSION, RuleDraft(api_id="api-001", param="customerId", kind="required"), ActorKind.AGENT)
    assert rule.status == RuleStatus.STAGED and rule.effective_from is None

    applied = await b.apply_rule(SESSION, rule.rule_id)
    assert applied.status == RuleStatus.APPLIED and applied.effective_from is not None


async def test_apply_rule_does_not_mutate_any_existing_run_verdict():
    b = _backend()
    # snapshot every existing RunResult's verdict before any rule is applied
    before = {run_id: (run.status, list(run.failed_rules)) for run_id, run in b.runs.items()}

    # this rule, if it were retroactively applied, would fail run-0010/11/12 (api-001, no
    # customerId binding recorded) -- but immutability means it must not touch them
    staged = await b.stage_rule(SESSION, RuleDraft(api_id="api-001", param="customerId", kind="required"), ActorKind.AGENT)
    await b.apply_rule(SESSION, staged.rule_id)

    after = {run_id: (run.status, list(run.failed_rules)) for run_id, run in b.runs.items()}
    assert after == before


async def test_effective_from_only_applies_to_runs_executed_after_apply():
    b = _backend()
    job = await b.stage_job(SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="pre", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
    await b.apply_job(SESSION, job.job_id)
    pre_runs = await b.execute_job_once(SESSION, job.job_id)
    assert pre_runs[0].status is RunStatus.PASS and pre_runs[0].failed_rules == []

    staged = await b.stage_rule(SESSION, RuleDraft(api_id="api-001", param="customerId", kind="required"), ActorKind.AGENT)
    await b.apply_rule(SESSION, staged.rule_id)

    # the pre-existing run is untouched by the newly-applied rule
    unchanged = await b.get_run(SESSION, pre_runs[0].run_id)
    assert unchanged.status is RunStatus.PASS and unchanged.failed_rules == []

    job2 = await b.stage_job(SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="post", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
    await b.apply_job(SESSION, job2.job_id)
    post_runs = await b.execute_job_once(SESSION, job2.job_id)
    assert post_runs[0].status is RunStatus.FAIL
    assert "customerId required" in post_runs[0].failed_rules


async def test_simulate_rule_is_read_only_and_counts_would_fail():
    b = _backend()
    job = await b.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="matrix", api_ids=["api-001"], target_envs=["dev"],
        test_data=[TestDataSet(label="S1", values={"amount": "1000", "customerId": "C1"}),
                   TestDataSet(label="S2", values={"amount": "500", "customerId": ""})]), ActorKind.AGENT)
    await b.apply_job(SESSION, job.job_id)
    await b.execute_job_once(SESSION, job.job_id)

    rules_before = len(b.rule_ledger.list())
    runs_before = len(b.runs)

    draft = RuleDraft(api_id="api-001", param="customerId", kind="required")
    # A generous window (the fixtures + this test's own runs all sit within a handful of days
    # of "now"): behaves like the old unbounded scan, but exercises the window_days param.
    impact = await b.simulate_rule(SESSION, draft, window_days=3650)

    assert isinstance(impact, RuleImpact)
    # 3 pre-existing api-001 fixture runs (no job_id / test_data_label) + 2 new runs from
    # the job above = 5 considered; only the 2 new ones have a reconstructable customerId
    # binding, and only S2's empty customerId fails a "required" rule.
    assert impact.window_runs == 5
    assert impact.known_inputs == 2
    assert impact.excluded_unknown == 3
    assert impact.would_fail == 1

    # read-only: nothing written to the rule ledger or the run store
    assert len(b.rule_ledger.list()) == rules_before
    assert len(b.runs) == runs_before


async def test_simulate_rule_window_days_ignores_an_older_run():
    b = _backend()
    job = await b.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="matrix", api_ids=["api-001"], target_envs=["dev"],
        test_data=[TestDataSet(label="S1", values={"customerId": ""})]), ActorKind.AGENT)
    await b.apply_job(SESSION, job.job_id)
    produced = await b.execute_job_once(SESSION, job.job_id)
    run = produced[0]
    # Push this run's executed_at outside a 1-day window relative to the session's own clock
    # (well past the pre-existing api-001 fixture runs too, so only run-0012 -- the one fixture
    # run within a day of SESSION.now -- remains in the window).
    b.runs[run.run_id] = run.model_copy(update={"executed_at": SESSION.now - timedelta(days=10)})

    draft = RuleDraft(api_id="api-001", param="customerId", kind="required")
    impact = await b.simulate_rule(SESSION, draft, window_days=1)

    # Only run-0012 (fixture, within the 1-day window) is counted; the pushed-back run --
    # which DOES have a reconstructable customerId binding and WOULD fail "required" -- is
    # excluded entirely by the window, so it contributes to neither known_inputs nor would_fail.
    assert impact.window_runs == 1
    assert impact.known_inputs == 0
    assert impact.excluded_unknown == 1
    assert impact.would_fail == 0


async def test_list_rules_filters_by_api():
    b = _backend()
    r1 = await b.stage_rule(SESSION, RuleDraft(api_id="api-001", param="customerId", kind="required"), ActorKind.AGENT)
    r2 = await b.stage_rule(SESSION, RuleDraft(api_id="api-003", param="paymentId", kind="required"), ActorKind.AGENT)

    assert [r.rule_id for r in (await b.list_rules(SESSION, api_id="api-001")).items] == [r1.rule_id]
    assert {r.rule_id for r in (await b.list_rules(SESSION)).items} == {r1.rule_id, r2.rule_id}


async def test_get_pending_rules_and_discard_rule():
    b = _backend()
    staged = await b.stage_rule(SESSION, RuleDraft(api_id="api-001", param="customerId", kind="required"), ActorKind.AGENT)
    assert [r.rule_id for r in await b.get_pending_rules(SESSION)] == [staged.rule_id]

    discarded = await b.discard_rule(SESSION, staged.rule_id, ActorKind.OPERATOR)
    assert discarded.status == RuleStatus.DISCARDED
    assert await b.get_pending_rules(SESSION) == []
