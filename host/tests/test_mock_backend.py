import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksSessionContext,
    AuditEntry,
    Binding,
    JobDraft,
    JobKind,
    RuleDraft,
    RunsQuery,
    RunStatus,
    TestDataSet,
)
from atworks_host.mock_backend import MockAtworks, stub_response, stub_verdict

KST = timezone(timedelta(hours=9))
SESSION = AtworksSessionContext(session_id="s", project_id="mes", operator="minseong", now=datetime(2026, 9, 3, 14, tzinfo=KST))
FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"


def _backend():
    return MockAtworks(AtworksAgentConfig(model="m"), FIXTURES)


async def test_fixtures_load_and_search_by_updated_after():
    b = _backend()
    recent = (await b.search_apis(SESSION, updated_after=datetime(2026, 8, 27, tzinfo=KST), limit=100)).items
    assert 0 < len(recent) < 12 and all(a.updated_at >= datetime(2026, 8, 27, tzinfo=KST) for a in recent)


async def test_count_runs_is_population_not_limit():
    b = _backend()
    shown = (await b.list_runs(SESSION, RunsQuery(status="fail", limit=2))).items
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
    assert after.run_count == 8 and len(after.recent_run_ids) == 8


async def test_execute_job_once_runs_named_parity_targets_legacy_and_renewed():
    b = _backend()
    job = await b.stage_job(SESSION, JobDraft(
        kind=JobKind.RUN_NOW, summary="parity", api_ids=["api-001", "api-004"],
        target_envs=["legacy", "renewed"]), ActorKind.AGENT)
    await b.apply_job(SESSION, job.job_id)
    produced = await b.execute_job_once(SESSION, job.job_id)

    assert len(produced) == 4                       # 2 apis × 2 named targets
    assert {r.target_env for r in produced} == {"legacy", "renewed"}
    assert all(r.job_id == job.job_id for r in produced)
    # api-004's response body carries a real value diff on the renewed target (stub_response)
    by_env = {(r.api_id, r.target_env): r for r in produced}
    assert by_env[("api-004", "legacy")].response_body["limit"] == 1000
    assert by_env[("api-004", "renewed")].response_body["limit"] == 900


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


async def test_execute_job_once_attaches_a_response_body_to_every_run():
    b = _backend()
    job = await b.stage_job(SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
    await b.apply_job(SESSION, job.job_id)
    produced = await b.execute_job_once(SESSION, job.job_id)
    assert len(produced) == 1
    body = produced[0].response_body
    assert body is not None
    assert body["path"] == b.apis["api-001"].path
    assert body["serverTime"].startswith("2026-09-05T00:00:")


async def test_stub_response_is_deterministic_for_a_fixed_seq():
    b = _backend()
    api = b.apis["api-001"]
    data = TestDataSet(label="S1", values={"amount": "1000"})
    assert stub_response(api, "dev", data, 5) == stub_response(api, "dev", data, 5)


async def test_stub_response_server_time_is_volatile_noise_across_seq():
    b = _backend()
    api = b.apis["api-001"]
    data = TestDataSet(label="S1", values={"amount": "1000"})
    first = stub_response(api, "dev", data, 1)
    second = stub_response(api, "dev", data, 2)
    assert first["serverTime"] != second["serverTime"]
    # everything but the volatile serverTime field is unchanged
    assert {k: v for k, v in first.items() if k != "serverTime"} == {k: v for k, v in second.items() if k != "serverTime"}


async def test_stub_response_api_004_has_a_real_value_diff_on_renewed():
    b = _backend()
    api = b.apis["api-004"]
    data = TestDataSet(label="S1", values={"paymentId": "P1", "amount": "1000"})
    dev_body = stub_response(api, "dev", data, 9)
    renewed_body = stub_response(api, "renewed", data, 9)
    assert dev_body["limit"] == 1000
    assert renewed_body["limit"] == 900
    # same seq -> serverTime and everything else match; only the real diff differs
    diffs = {k for k in dev_body if dev_body[k] != renewed_body.get(k)}
    assert diffs == {"limit"}


async def test_stub_response_normal_api_matches_across_envs_except_server_time():
    b = _backend()
    api = b.apis["api-001"]
    data = TestDataSet(label="S1", values={"amount": "1000"})
    dev_body = stub_response(api, "dev", data, 3)
    stg_body = stub_response(api, "stg", data, 7)   # different seq too
    diffs = {k for k in dev_body if dev_body[k] != stg_body.get(k)}
    assert diffs == {"serverTime"}


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


async def test_find_apis_with_param_excludes_apis_with_an_applied_format_rule():
    b = _backend()
    found = (await b.find_apis_with_param(SESSION, "amount")).items
    assert {a.api_id for a in found} == {"api-001", "api-004", "api-006"}
    rule = b.rule_ledger.stage(
        RuleDraft(api_id="api-001", param="amount", kind="format", format="number"),
        actor=SESSION.operator, actor_kind=ActorKind.AGENT)
    b.rule_ledger.apply(rule.rule_id, actor=SESSION.operator)
    found = (await b.find_apis_with_param(SESSION, "amount")).items
    assert {a.api_id for a in found} == {"api-004", "api-006"}


async def test_find_apis_with_param_unknown_param_finds_nothing():
    b = _backend()
    found = (await b.find_apis_with_param(SESSION, "nonexistentParam")).items
    assert found == []


async def test_recommend_rules_for_api_suggests_a_peers_applied_rule():
    b = _backend()
    rule = b.rule_ledger.stage(
        RuleDraft(api_id="api-006", param="amount", kind="compare", op=">=", value="0"),
        actor=SESSION.operator, actor_kind=ActorKind.AGENT)
    applied = b.rule_ledger.apply(rule.rule_id, actor=SESSION.operator)
    recs = await b.recommend_rules_for_api(SESSION, "api-004")
    assert len(recs) == 1
    assert recs[0].param == "amount"
    assert recs[0].from_api_id == "api-006"
    assert recs[0].rule.rule_id == applied.rule_id


async def test_recommend_rules_for_api_nothing_when_no_peer_has_a_rule():
    b = _backend()
    recs = await b.recommend_rules_for_api(SESSION, "api-004")
    assert recs == []


async def test_recommend_rules_for_api_skips_a_param_the_target_already_has_a_rule_for():
    b = _backend()
    peer = b.rule_ledger.stage(
        RuleDraft(api_id="api-006", param="amount", kind="compare", op=">=", value="0"),
        actor=SESSION.operator, actor_kind=ActorKind.AGENT)
    b.rule_ledger.apply(peer.rule_id, actor=SESSION.operator)
    own = b.rule_ledger.stage(
        RuleDraft(api_id="api-004", param="amount", kind="required"),
        actor=SESSION.operator, actor_kind=ActorKind.AGENT)
    b.rule_ledger.apply(own.rule_id, actor=SESSION.operator)
    recs = await b.recommend_rules_for_api(SESSION, "api-004")
    assert recs == []


async def test_recommend_rules_for_api_unknown_api_returns_nothing():
    b = _backend()
    recs = await b.recommend_rules_for_api(SESSION, "no-such-api")
    assert recs == []


async def test_execute_job_once_stamps_executed_by_from_the_approver():
    b = _backend()
    approver_session = AtworksSessionContext(session_id="s2", project_id="mes", operator="jihoon", now=SESSION.now)
    job = await b.stage_job(SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001", "api-003"], target_envs=["dev"]), ActorKind.AGENT)
    await b.apply_job(approver_session, job.job_id)
    runs = await b.execute_job_once(SESSION, job.job_id)
    assert runs and all(r.executed_by == "jihoon" for r in runs)


def test_fixture_runs_carry_operators():
    rows = json.loads((FIXTURES / "runs.json").read_text(encoding="utf-8"))
    assert {r.get("executed_by") for r in rows} >= {"minseong", "jihoon", "sora"}


# -- Task 2: paged ABC contract (scale, spec 2026-09-06) -----------------------------


async def test_list_runs_pages_to_exhaustion_with_no_duplicates_or_gaps():
    b = _backend()
    total_runs = len(b.runs)
    assert total_runs >= 21   # the fixture set is large enough for >=3 pages at limit=10

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = await b.list_runs(SESSION, RunsQuery(limit=10, cursor=cursor))
        assert page.total == total_runs
        seen.extend(r.run_id for r in page.items)
        pages += 1
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        assert pages <= 10   # guard against an infinite loop if next_cursor never clears

    assert pages >= 3
    assert len(seen) == total_runs
    assert len(set(seen)) == total_runs   # no duplicates
    assert set(seen) == set(b.runs.keys())   # no gaps


async def test_count_runs_by_api_id_matches_filtered_length_with_no_sort():
    b = _backend()
    filtered = [r for r in b.runs.values() if r.api_id == "api-001"]
    total = await b.count_runs(SESSION, api_id="api-001")
    assert total == len(filtered)


async def test_all_jobs_filters_by_status():
    b = _backend()
    staged = await b.stage_job(SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="staged only", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
    applied_job = await b.stage_job(SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="to be applied", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
    await b.apply_job(SESSION, applied_job.job_id)

    staged_page = await b.all_jobs(SESSION, status="staged")
    assert {j.job_id for j in staged_page.items} == {staged.job_id}

    applied_page = await b.all_jobs(SESSION, status="applied")
    assert {j.job_id for j in applied_page.items} == {applied_job.job_id}


async def test_active_jobs_excludes_an_exhausted_applied_job():
    b = _backend()
    job = await b.stage_job(SESSION, JobDraft(kind=JobKind.RUN_NOW, summary="run once", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
    await b.apply_job(SESSION, job.job_id)

    before = await b.active_jobs(SESSION)
    assert job.job_id in {j.job_id for j in before}

    await b.execute_job_once(SESSION, job.job_id)   # run_now: one execution exhausts it

    after = await b.active_jobs(SESSION)
    assert job.job_id not in {j.job_id for j in after}


async def test_audit_pages_same_timestamp_rows_by_seq_numerically_not_lexicographically():
    # A str(seq) tie-break sorts "9" after "10" (lexicographic), mis-paging same-timestamp
    # rows once seq reaches double digits; the fix zero-pads seq so the tie-break is numeric.
    b = _backend()
    at = datetime(2026, 9, 3, 12, tzinfo=UTC)
    # Task 4 (scale spec 2026-09-06): audit lives in the store's audit_log table now, not an
    # in-memory list -- insert_audit_entries lets the test pin explicit seq values directly.
    b.store.insert_audit_entries([
        AuditEntry(seq=9, at=at, operator="minseong", action="apply_job", target_kind="job", target_id="job-9", session_id="s"),
        AuditEntry(seq=10, at=at, operator="minseong", action="apply_job", target_kind="job", target_id="job-10", session_id="s"),
    ])
    page = await b.audit(SESSION, limit=50)
    assert [e.seq for e in page.items] == [10, 9]
