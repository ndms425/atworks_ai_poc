from datetime import datetime, timedelta, timezone
from pathlib import Path

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksSessionContext,
    Binding,
    JobDraft,
    JobKind,
    RunStatus,
    TestDataSet,
)
from atworks_host.mock_backend import MockAtworks, stub_verdict

KST = timezone(timedelta(hours=9))
SESSION = AtworksSessionContext(session_id="s", project_id="mes", operator="minseong", now=datetime(2026, 9, 3, 14, tzinfo=KST))


def _backend():
    return MockAtworks(AtworksAgentConfig(model="m"), Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures")


async def test_fixtures_load_and_search_by_updated_after():
    b = _backend()
    recent = await b.search_apis(SESSION, updated_after=datetime(2026, 8, 27, tzinfo=KST), limit=100)
    assert 0 < len(recent) < 12 and all(a.updated_at >= datetime(2026, 8, 27, tzinfo=KST) for a in recent)


async def test_count_runs_is_population_not_limit():
    b = _backend()
    shown = await b.list_runs(SESSION, status="fail", limit=2)
    total = await b.count_runs(SESSION, since=None, status="fail")
    assert len(shown) == 2 and total > 2


async def test_execute_job_once_is_deterministic_and_records_runs():
    b = _backend()
    job = await b.stage_job(SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001", "api-003"], target_envs=["dev"]), ActorKind.AGENT)
    await b.apply_job(SESSION, job.job_id)
    first = await b.execute_job_once(SESSION, job.job_id)
    statuses = {r.api_id: r.status.value for r in first}
    assert statuses == {"api-001": "pass", "api-003": "fail"}
    assert all(r.job_id == job.job_id for r in first)
    assert (await b.get_run(SESSION, first[0].run_id)) is not None


async def test_late_binding_over_limit_skips_execution_and_notes_guardrail():
    config = AtworksAgentConfig(model="m", max_apis_per_job=3)
    b = MockAtworks(config, Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures")
    job = await b.stage_job(
        SESSION,
        JobDraft(kind=JobKind.RUN_NOW, summary="late", api_ids=["api-001", "api-002", "api-003"],
                 target_envs=["dev"], binding="LATE", select_where={"query": ""}),
        ActorKind.AGENT,
    )
    await b.apply_job(SESSION, job.job_id)
    before = b.ledger.get(job.job_id).remaining_executions
    produced = await b.execute_job_once(SESSION, job.job_id)
    assert produced == []
    updated = b.ledger.get(job.job_id)
    assert len(updated.guardrail_notes) == 1
    # M11: the note comes from enforce_execution_matrix, the same size-cap check FROZEN jobs
    # get, so it names the resolved count and the limit rather than singling out LATE.
    assert "resolved to 4 APIs" in updated.guardrail_notes[0]
    assert "limit of 3" in updated.guardrail_notes[0]
    # the slot is consumed even though nothing ran, so the schedule does not spin forever
    assert updated.remaining_executions == before - 1

    # no slots remain: execute_job_once must not add a second note or consume another slot
    again = await b.execute_job_once(SESSION, job.job_id)
    assert again == []
    final = b.ledger.get(job.job_id)
    assert len(final.guardrail_notes) == 1
    assert final.remaining_executions == 0


async def test_late_binding_resolving_to_no_apis_skips_execution_and_notes_guardrail():
    b = _backend()
    job = await b.stage_job(
        SESSION,
        JobDraft(kind=JobKind.RUN_NOW, summary="late empty", api_ids=["api-001"],
                 target_envs=["dev"], binding="LATE", select_where={"query": "zzz-nothing"}),
        ActorKind.AGENT,
    )
    await b.apply_job(SESSION, job.job_id)
    before = b.ledger.get(job.job_id).remaining_executions

    produced = await b.execute_job_once(SESSION, job.job_id)

    assert produced == []
    updated = b.ledger.get(job.job_id)
    assert len(updated.guardrail_notes) == 1
    assert "no APIs" in updated.guardrail_notes[0]
    # the slot is consumed even though nothing ran
    assert updated.remaining_executions == before - 1


async def test_late_binding_re_resolves_related_to_at_execution():
    backend = MockAtworks(AtworksAgentConfig(model="m"), Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures")
    job = await backend.stage_job(SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="late", api_ids=["api-003"], target_envs=["dev"],
                                                    binding=Binding.LATE, select_where={"related_to": "api-003"}), ActorKind.AGENT)
    await backend.apply_job(SESSION, job.job_id)
    produced = await backend.execute_job_once(SESSION, job.job_id)
    assert {r.api_id for r in produced} == {"api-003", "api-004", "api-005", "api-010"}   # payment group / /v1/payments/*


async def test_execute_job_once_produces_the_whole_matrix():
    b = _backend()
    job = await b.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="matrix", api_ids=["api-001", "api-003"],
        target_envs=["dev", "stg"],
        test_data=[TestDataSet(label="S1 정상", values={"amount": "1000"}),
                   TestDataSet(label="S2 음수", values={"amount": "-1"})]), ActorKind.AGENT)
    await b.apply_job(SESSION, job.job_id)
    produced = await b.execute_job_once(SESSION, job.job_id, None)

    assert len(produced) == 8                       # 2 apis × 2 envs × 2 data sets
    keys = {(r.api_id, r.target_env, r.test_data_label) for r in produced}
    assert len(keys) == 8                           # every run in one execution is distinguishable
    assert {r.target_env for r in produced} == {"dev", "stg"}
    assert {r.test_data_label for r in produced} == {"S1 정상", "S2 음수"}
    after = b.ledger.get(job.job_id)
    assert after.executions == 1 and after.remaining_executions == 0
    assert len(after.run_ids) == 8


async def test_stub_verdict_fails_a_negative_amount_binding():
    b = _backend()
    api = b.apis["api-001"]
    assert stub_verdict(api, "dev", TestDataSet(label="S2", values={"amount": "-1"})) == (
        RunStatus.FAIL, ["amount >= 0"], 200)
    assert stub_verdict(api, "dev", TestDataSet(label="S1", values={"amount": "1000"}))[0] is RunStatus.PASS
    # today's refund rule still holds when nothing is bound, so the fixtures keep their meaning
    assert stub_verdict(b.apis["api-003"], "dev", None) == (RunStatus.FAIL, ["refundAmount >= 0"], 200)


async def test_stub_verdict_handles_fractional_and_non_numeric_amounts():
    b = _backend()
    api = b.apis["api-001"]
    # A fractional negative amount still fails the amount rule.
    assert stub_verdict(api, "dev", TestDataSet(label="S", values={"amount": "-1.5"}))[0] is RunStatus.FAIL
    # A non-numeric amount cannot be compared, so the amount rule does not fire; no other rule
    # applies to api-001 with a binding present, so the verdict is PASS.
    assert stub_verdict(api, "dev", TestDataSet(label="S", values={"amount": "abc"}))[0] is RunStatus.PASS
    # An empty amount is likewise not a number, so it does not trip the amount rule either.
    assert stub_verdict(api, "dev", TestDataSet(label="S", values={"amount": ""}))[0] is RunStatus.PASS


async def test_stub_verdict_differs_between_dev_and_stg_for_api_007():
    b = _backend()
    api = b.apis["api-007"]
    assert stub_verdict(api, "dev", None)[0] is RunStatus.PASS
    assert stub_verdict(api, "stg", None) == (RunStatus.ERROR, [], 503)


async def test_execute_job_once_enforces_the_matrix_cap_at_execution_time():
    # Stage under a permissive config (3 apis × 2 envs × 2 data = 12 <= 400 passes staging),
    # then tighten the backend's config before executing — a LATE selection could also grow
    # between staging and execution, but a FROZEN job re-checked under a stricter config is
    # the simplest repro of the same "size caps re-derived at execution time" rule (M11).
    b = MockAtworks(AtworksAgentConfig(model="m", max_matrix_size=400),
                     Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures")
    job = await b.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="over cap", api_ids=["api-001", "api-002", "api-003"],
        target_envs=["dev", "stg"],
        test_data=[TestDataSet(label="S1", values={"contractNo": "C1"}),
                   TestDataSet(label="S2", values={"paymentId": "P1"})]), ActorKind.AGENT)
    await b.apply_job(SESSION, job.job_id)
    before = b.ledger.get(job.job_id).remaining_executions
    b._config = b._config.model_copy(update={"max_matrix_size": 10})

    produced = await b.execute_job_once(SESSION, job.job_id, None)

    assert produced == []
    updated = b.ledger.get(job.job_id)
    assert len(updated.guardrail_notes) == 1
    assert "matrix resolved to 12 runs" in updated.guardrail_notes[0]
    assert updated.remaining_executions == before - 1
