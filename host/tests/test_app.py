from datetime import UTC, datetime
from pathlib import Path

import pytest
from commerce_common.streaming import ToolOutcome
from commerce_common.testing import FakeClient, text_message, tool_calls_message
from httpx import ASGITransport, AsyncClient

from atworks_agent import (
    ActorKind,
    AggregateQuery,
    AtworksAgentConfig,
    AtworksSessionContext,
    AtworksSessionState,
    AtworksToolExecutor,
    FormatBatchDraft,
    JobDraft,
    JobKind,
    ProfileDraft,
    RuleDraft,
)
from atworks_agent_runtime import AtworksAgent
from atworks_host.app import create_app
from atworks_host.briefing import Briefings
from atworks_host.insights import InsightPanels
from atworks_host.mock_backend import MockAtworks
from atworks_host.reports import Reports
from atworks_host.scheduler import Scheduler

FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"
SKILLS = Path(__file__).resolve().parents[2] / "atworks-agent" / "skills"


async def _fake_narrator(candidates, role, *, notes=None):
    del candidates, role, notes
    return []


@pytest.fixture
async def client(tmp_path):
    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    agent = AtworksAgent(backend=backend, skills_dir=SKILLS, config=config, client=FakeClient([text_message("ok")]))
    reports = Reports(tmp_path)
    briefings = Briefings(tmp_path / "b", config)
    insights = InsightPanels(tmp_path / "insights", config, narrator=_fake_narrator)
    app = create_app(agent=agent, backend=backend, scheduler=Scheduler(backend, reports, None, briefings=briefings),
                      reports=reports, briefings=briefings, insights=insights)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        c.backend = backend
        c.reports = reports
        yield c


@pytest.fixture
async def client_backend(tmp_path):
    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    agent = AtworksAgent(backend=backend, skills_dir=SKILLS, config=config, client=FakeClient([text_message("ok")]))
    reports = Reports(tmp_path)
    briefings = Briefings(tmp_path / "b", config)
    app = create_app(agent=agent, backend=backend, scheduler=Scheduler(backend, reports, None, briefings=briefings),
                      reports=reports, briefings=briefings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        yield c, backend


def _redate_fixture_runs(backend):
    now = datetime.now(UTC)
    for run in backend.runs.values():
        delta = datetime(2026, 9, 4, tzinfo=UTC) - run.executed_at
        backend.runs[run.run_id] = run.model_copy(update={"executed_at": now - delta})
    for api in backend.apis.values():
        delta = datetime(2026, 9, 4, tzinfo=UTC) - api.updated_at
        backend.apis[api.api_id] = api.model_copy(update={"updated_at": now - delta})


async def test_runs_insights_counts_flaky_and_regression_from_fixtures(client):
    _redate_fixture_runs(client.backend)
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.get("/api/atworks/runs/insights", headers={"X-Session-Id": sid})
    assert r.status_code == 200
    body = r.json()
    # regression_suspect is now spec §3's watermark rule (last_pass_at < api.updated_at <=
    # first_non_pass_at), which the fixture's api-004 no longer satisfies: it failed once and
    # then PASSED again, so its last pass is after its first failure -- an API that recovered is
    # not a standing regression. flaky (a cell-level count) is unchanged.
    assert body["flaky"] == 1 and body["regression_suspect"] == 0 and body["window_days"] == 30


async def test_runs_insights_needs_a_session(client):
    assert (await client.get("/api/atworks/runs/insights")).status_code in (401, 422)


async def test_session_and_reads(client):
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    h = {"X-Session-Id": sid}
    apis = (await client.get("/api/atworks/apis", headers=h)).json()
    assert apis["total"] == 12 and len(apis["items"]) == 12
    runs = (await client.get("/api/atworks/runs?status=fail", headers=h)).json()
    assert runs["total"] >= len(runs["items"]) > 0
    assert (await client.get("/api/atworks/jobs", headers=h)).json() == {"items": [], "next_cursor": None, "total": 0}


async def test_list_routes_answer_only_in_the_paged_envelope(client):
    # Task 10 (spec 2026-09-06 §10): every list route answers in ONE shape,
    # {items, next_cursor, total}. The legacy keys Task 8 kept for the un-migrated portal
    # (`apis`, `runs`, `population`) are GONE -- the web reads the envelope now.
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    h = {"X-Session-Id": sid}

    apis = (await client.get("/api/atworks/apis?limit=5", headers=h)).json()
    assert set(apis) == {"items", "next_cursor", "total"} and len(apis["items"]) == 5
    assert apis["total"] == 12 and apis["next_cursor"]
    page2 = (await client.get(f"/api/atworks/apis?limit=5&cursor={apis['next_cursor']}", headers=h)).json()
    assert page2["total"] == 12
    first = {a["api_id"] for a in apis["items"]}
    assert first.isdisjoint({a["api_id"] for a in page2["items"]})

    runs = (await client.get("/api/atworks/runs?limit=4", headers=h)).json()
    assert set(runs) == {"items", "next_cursor", "total"} and len(runs["items"]) == 4
    assert runs["total"] > 4 and runs["next_cursor"]
    runs2 = (await client.get(f"/api/atworks/runs?limit=4&cursor={runs['next_cursor']}", headers=h)).json()
    assert {r["run_id"] for r in runs2["items"]}.isdisjoint({r["run_id"] for r in runs["items"]})

    # The three ledger routes gained cursor/limit in the same shape.
    for path in ("/api/atworks/jobs", "/api/atworks/rules", "/api/atworks/profiles"):
        body = (await client.get(f"{path}?limit=2", headers=h)).json()
        assert set(body) == {"items", "next_cursor", "total"}, path
        assert len(body["items"]) <= 2, path

    # The route cap is 200 whatever the caller asks for (RunsQuery.limit's own bound), and the
    # FLOOR is 1 -- `/runs` was the one route missing `ge=1`, so `limit=0` fell through to
    # RunsQuery's own validator and surfaced as a 500 instead of a 422 (Task 10 review minor).
    for path in ("/api/atworks/runs", "/api/atworks/apis", "/api/atworks/jobs",
                 "/api/atworks/rules", "/api/atworks/profiles"):
        assert (await client.get(f"{path}?limit=500", headers=h)).status_code == 422, path
        assert (await client.get(f"{path}?limit=0", headers=h)).status_code == 422, path
        assert (await client.get(f"{path}?limit=-1", headers=h)).status_code == 422, path


async def test_runs_status_all_means_no_filter_and_a_bogus_status_is_422(client):
    """Final review I4. `all` is a MEMBER of RunStatusFilter -- the portal's own default segment
    sends it, and the model's tool schema offers it -- but every reader took it as a status VALUE,
    so `?status=all` matched no row and answered an empty page with `total: 0`. Nothing looked
    wrong; the runs simply were not there. And an unknown status reached `RunsQuery`'s validator
    from inside the route and surfaced as a 500 rather than the 422 it plainly is."""
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    h = {"X-Session-Id": sid}

    unfiltered = (await client.get("/api/atworks/runs?limit=5", headers=h)).json()
    every = (await client.get("/api/atworks/runs?limit=5&status=all", headers=h)).json()
    assert every["total"] == unfiltered["total"] > 0
    assert [r["run_id"] for r in every["items"]] == [r["run_id"] for r in unfiltered["items"]]
    # ...and a real filter still narrows
    failed = (await client.get("/api/atworks/runs?limit=5&status=fail", headers=h)).json()
    assert 0 < failed["total"] < every["total"]

    assert (await client.get("/api/atworks/runs?status=bogus", headers=h)).status_code == 422


async def test_every_paged_route_answers_400_for_a_malformed_cursor(client):
    """Final review I5. `decode_cursor` raises `ValueError`, and half the things that go wrong
    inside it (binascii.Error, UnicodeDecodeError, JSONDecodeError) ARE ValueErrors, so the old
    `except ValueError: raise` re-raised them untouched and all six routes answered 500 to a
    client-supplied string."""
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    h = {"X-Session-Id": sid}
    for path in ("/api/atworks/apis", "/api/atworks/runs", "/api/atworks/jobs",
                 "/api/atworks/rules", "/api/atworks/profiles", "/api/atworks/audit"):
        for bad in ("zzz", "!!!!", "eyJ0Ijog"):          # bad padding, bad base64, truncated JSON
            response = await client.get(f"{path}?cursor={bad}", headers=h)
            assert response.status_code == 400, (path, bad, response.status_code)
            assert "cursor" in response.json()["detail"], path


async def test_aggregate_status_all_is_the_same_as_no_status(client_backend):
    """The same `all` normalization on the aggregate side, where taking it as a value did more
    than empty a page: it zeroed every group's `transitions` (the rank's leading term for the
    flaky order) as well as its counts."""
    _client, backend = client_backend
    session = AtworksSessionContext(session_id="s", project_id="mes", operator="minseong")
    plain = await backend.aggregate_runs(session, AggregateQuery(group_by="api", limit=50))
    everything = await backend.aggregate_runs(
        session, AggregateQuery(group_by="api", status="all", limit=50))
    assert everything == plain and plain


async def test_rules_route_filters_by_status_server_side(client_backend):
    """Task 10 review carry-forward: the Rules page used to fetch one page and bucket it by
    status client-side, so an applied rule beyond page one was invisible and each group's count
    was the page's share. `?status=` pushes the filter to the ledger; `total` is that status's
    real count."""
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    staged = await backend.stage_rule(
        session, RuleDraft(api_id="api-001", param="contractNo", kind="required"), ActorKind.AGENT)
    to_apply = await backend.stage_rule(
        session, RuleDraft(api_id="api-002", param="id", kind="required"), ActorKind.AGENT)
    to_discard = await backend.stage_rule(
        session, RuleDraft(api_id="api-006", param="amount", kind="required"), ActorKind.AGENT)
    await backend.apply_rule(session, to_apply.rule_id)
    await backend.discard_rule(session, to_discard.rule_id, ActorKind.OPERATOR)

    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    h = {"X-Session-Id": sid}
    all_rules = (await client.get("/api/atworks/rules", headers=h)).json()
    assert all_rules["total"] == 3

    expected = {"staged": [staged.rule_id], "applied": [to_apply.rule_id],
                "discarded": [to_discard.rule_id]}
    for status, ids in expected.items():
        body = (await client.get(f"/api/atworks/rules?status={status}", headers=h)).json()
        assert set(body) == {"items", "next_cursor", "total"}
        assert [row["rule_id"] for row in body["items"]] == ids, status
        # `total` is the ledger's count for THIS status, not the page's share of a mixed page
        assert body["total"] == 1, status

    # M18: an unknown status is a BAD REQUEST, not an empty page. It used to reach the backend as
    # a plain string, match nothing, and answer `{"items": [], "total": 0}` -- which reads exactly
    # like "there are no rules in that state", so a typo in a client looked like data.
    unknown = await client.get("/api/atworks/rules?status=nonsense", headers=h)
    assert unknown.status_code == 422


async def test_run_records_carry_the_api_label(client):
    # Task 10: run rows render "GET /v1/..." from the record itself -- RunsView no longer
    # downloads every API spec to look the label up client-side.
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    h = {"X-Session-Id": sid}
    runs = (await client.get("/api/atworks/runs?limit=5", headers=h)).json()["items"]
    assert runs and all(row["api_method"] and row["api_path"].startswith("/") for row in runs)


async def test_home_summary_answers_every_tile_in_one_call(client):
    # Task 10: Home used to fire /runs?status=fail, /runs?status=error, /jobs and /runs/insights
    # in parallel and count their LISTS. This is four count queries and a briefing header.
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    h = {"X-Session-Id": sid}
    body = (await client.get("/api/atworks/home/summary", headers=h)).json()
    assert set(body) == {"counts", "insights", "briefing_header"}
    assert set(body["counts"]) == {"fail", "error", "pending_jobs"}
    assert all(isinstance(v, int) for v in body["counts"].values())
    assert body["insights"]["window_days"] == 30
    # The tiles agree with the routes they replaced (same window, same predicate).
    insights = (await client.get("/api/atworks/runs/insights", headers=h)).json()
    assert body["insights"] == insights
    fails = (await client.get("/api/atworks/runs?status=fail", headers=h)).json()["total"]
    assert body["counts"]["fail"] <= fails  # /runs is unwindowed; the tile is scope_window_days
    # No briefing has been generated in this fixture host, so the header is null (not an error).
    assert body["briefing_header"] is None


async def test_home_summary_needs_a_session(client):
    assert (await client.get("/api/atworks/home/summary")).status_code in (401, 422)


async def test_session_binds_operator_and_role(client):
    r = await client.post("/api/atworks/session", json={"operator_id": "jihoon"})
    body = r.json()
    assert body["role"] == "qa"
    assert body["operator"] == "jihoon"
    assert body["operator_name"] == "박지훈"


async def test_session_default_operator_and_unknown_400(client):
    default = await client.post("/api/atworks/session", json={})
    assert default.json()["operator"] == "minseong"
    assert default.json()["role"] == "developer"
    unknown = await client.post("/api/atworks/session", json={"operator_id": "nobody"})
    assert unknown.status_code == 400


async def test_session_with_no_body_defaults_to_minseong(client):
    r = await client.post("/api/atworks/session")
    assert r.json()["operator"] == "minseong"
    assert r.json()["role"] == "developer"


async def test_operators_route_lists_three(client):
    r = await client.get("/api/atworks/operators")
    assert r.status_code == 200
    ids = {o["operator_id"] for o in r.json()["operators"]}
    assert ids == {"minseong", "jihoon", "sora"}


async def test_context_carries_role_and_scope(client):
    from atworks_agent import AtworksSessionContext

    started = await client.post("/api/atworks/session", json={"operator_id": "minseong"})
    sid = started.json()["session_id"]
    session = AtworksSessionContext(session_id=sid, project_id="mes-demo", operator="minseong", role="developer")
    ctx = await client.backend.get_context(session)
    assert ctx["operator"] == "minseong"
    assert ctx["operator_role"] == "developer"
    fixture_apis_minseong_ran = {"api-001", "api-003", "api-004", "api-005", "api-009"}
    assert set(ctx["scope_api_ids"])
    assert set(ctx["scope_api_ids"]) <= fixture_apis_minseong_ran
    # scope_api_count is the true count (FIX #2); with this fixture it happens to equal the
    # (unbounded here) sample length, but the two fields are read independently downstream.
    assert ctx["scope_api_count"] == len(ctx["scope_api_ids"])


async def test_chat_streams_sse_with_attachments(client):
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post("/api/atworks/chat", headers={"X-Session-Id": sid},
                          json={"message": "이거 왜 실패했어", "attached_items": [{"order": 1, "kind": "run", "ref_id": "run-0001", "label": "환불", "comment": "왜"}]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert "event: text_delta" in r.text and "event: turn_complete" in r.text


async def test_chat_request_accepts_screen_state_and_caps_visible(client):
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    too_many = [{"kind": "run", "ref_id": f"run-{i:04d}"} for i in range(41)]
    r = await client.post("/api/atworks/chat", headers={"X-Session-Id": sid},
                          json={"message": "hi", "screen_state": {"view": "runs", "visible": too_many}})
    assert r.status_code == 422


async def test_apply_route_marks_then_consumes_approval(client):
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post("/api/atworks/changes/job-9999/apply", headers={"X-Session-Id": sid})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False and "not staged" in body["reason"]


async def test_host_click_applies_job_staged_outside_the_session(client_backend):
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    job = await backend.stage_job(
        session, JobDraft(kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT
    )
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post(f"/api/atworks/changes/{job.job_id}/apply", headers={"X-Session-Id": sid})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and body["change"]["status"] == "applied"

    r2 = await client.post(f"/api/atworks/changes/{job.job_id}/apply", headers={"X-Session-Id": sid})
    if r2.status_code == 200:
        assert r2.json()["ok"] is False and "not staged" in r2.json()["reason"]
    else:
        assert r2.status_code == 400 and "not staged" in r2.json()["detail"]


async def test_apply_then_tick_executes_the_whole_matrix(client_backend):
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    job = await backend.stage_job(
        session, JobDraft(kind=JobKind.RUN_NOW, summary="two envs", api_ids=["api-001", "api-003"],
                          target_envs=["dev", "stg"]), ActorKind.AGENT
    )
    expected_runs = job.matrix_size

    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post(f"/api/atworks/changes/{job.job_id}/apply", headers={"X-Session-Id": sid})
    assert r.status_code == 200 and r.json()["ok"] is True

    tr = await client.post("/api/atworks/scheduler/tick", params={"now": "2026-09-03T14:00:00+09:00"})
    assert tr.status_code == 200 and tr.json()["executed"] == [job.job_id]

    applied = backend.ledger.get(job.job_id)
    assert applied.run_count == expected_runs
    runs = await backend.runs_by_ids(session, applied.recent_run_ids)
    assert len(runs) == expected_runs
    assert {r.target_env for r in runs} == set(job.target_envs)


async def test_report_404_before_run(client):
    assert (await client.get("/api/atworks/reports/job-0001")).status_code == 404


async def test_report_opens_without_session_header(client):
    backend = client.backend
    reports = client.reports
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    job = await backend.stage_job(
        session, JobDraft(kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT
    )
    await backend.apply_job(session, job.job_id)
    runs = await backend.execute_job_once(session, job.job_id, None)
    await reports.write(job, runs, body_loader=lambda rid: backend.get_body(session, rid))
    r = await client.get(f"/api/atworks/reports/{job.job_id}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert "report-data" in r.text


async def test_runs_since_naive_date_does_not_500(client):
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.get("/api/atworks/runs?since=2026-09-01", headers={"X-Session-Id": sid})
    assert r.status_code == 200


async def test_scheduler_tick_naive_now_does_not_500(client):
    r = await client.post("/api/atworks/scheduler/tick?now=2026-09-03T14:00:00")
    assert r.status_code == 200


async def test_runs_since_garbage_is_400(client):
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.get("/api/atworks/runs?since=garbage", headers={"X-Session-Id": sid})
    assert r.status_code == 400


async def test_memory_route_returns_empty_facts(client):
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.get("/api/atworks/memory", headers={"X-Session-Id": sid})
    assert r.status_code == 200
    assert r.json() == {"facts": []}


async def test_report_route_rejects_backslash_traversal(client):
    reports = client.reports
    secret_dir = reports.out_dir.parent / "secret-atworks-test"
    secret_dir.mkdir(parents=True, exist_ok=True)
    (secret_dir / "index.html").write_text("TOP SECRET", encoding="utf-8")
    try:
        r = await client.get(f"/api/atworks/reports/..%5C{secret_dir.name}")
        assert r.status_code == 404
        assert "TOP SECRET" not in r.text
    finally:
        (secret_dir / "index.html").unlink()
        secret_dir.rmdir()


async def test_report_route_dotdot_forward_slash_is_not_200(client):
    r = await client.get("/api/atworks/reports/../secret")
    assert r.status_code != 200
    assert "TOP SECRET" not in r.text


async def test_scheduler_tick_accepts_offset_via_params(client):
    r = await client.post("/api/atworks/scheduler/tick", params={"now": "2026-09-05T09:00:00+09:00"})
    assert r.status_code == 200


async def test_scheduler_tick_accepts_offset_sent_as_a_raw_plus(client):
    r = await client.post("/api/atworks/scheduler/tick?now=2026-09-05T09:00:00+09:00")
    assert r.status_code == 200


async def test_scheduler_tick_accepts_percent_encoded_offset(client):
    r = await client.post("/api/atworks/scheduler/tick?now=2026-09-05T09:00:00%2B09:00")
    assert r.status_code == 200


async def test_apply_click_consumes_the_mark(client_backend):
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    job = await backend.stage_job(
        session, JobDraft(kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT
    )
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post(f"/api/atworks/changes/{job.job_id}/apply", headers={"X-Session-Id": sid})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True

    r2 = await client.post(f"/api/atworks/changes/{job.job_id}/apply", headers={"X-Session-Id": sid})
    if r2.status_code == 200:
        assert r2.json()["ok"] is False and "not staged" in r2.json()["reason"]
    else:
        assert r2.status_code == 400 and "not staged" in r2.json()["detail"]


async def test_route_discard_records_operator(client_backend):
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    job = await backend.stage_job(
        session, JobDraft(kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT
    )
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post(f"/api/atworks/changes/{job.job_id}/discard", headers={"X-Session-Id": sid})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["change"]["discarded_by_kind"] == "operator"


async def test_rules_route_needs_a_session(client):
    assert (await client.get("/api/atworks/rules")).status_code in (401, 422)


async def test_rules_route_lists_staged_rules(client_backend):
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    rule = await backend.stage_rule(
        session, RuleDraft(api_id="api-001", param="contractNo", kind="required"), ActorKind.AGENT
    )
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.get("/api/atworks/rules", headers={"X-Session-Id": sid})
    assert r.status_code == 200
    assert [row["rule_id"] for row in r.json()["items"]] == [rule.rule_id]


async def test_rule_and_profile_actions_look_the_id_up_directly(client_backend, monkeypatch):
    """Task 10 review carry-forward: the approve/discard routes used to re-learn an unseen
    rule/profile by scanning `list_rules(limit=1000)`, which silently stopped finding anything
    older than that page. They now call the ABC's `get_rule`/`get_profile`, so a LISTING call on
    this path is a bug -- these stubs make it a loud one."""
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    rule = await backend.stage_rule(
        session, RuleDraft(api_id="api-001", param="contractNo", kind="required"), ActorKind.AGENT)
    job = await backend.stage_job(
        session, JobDraft(kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"],
                          target_envs=["legacy", "renewed"]), ActorKind.AGENT)
    profile = await backend.stage_profile(
        session, ProfileDraft(job_id=job.job_id, summary="serverTime 무시", ignore_paths=["$.serverTime"]), ActorKind.AGENT)

    async def _boom(*args, **kwargs):
        raise AssertionError("the action path must not page the ledger to find one id")

    monkeypatch.setattr(backend, "list_rules", _boom)
    monkeypatch.setattr(backend, "list_profiles", _boom)

    # a brand-new session that has never seen either id -- the re-learn branch is what runs
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    h = {"X-Session-Id": sid}
    r = await client.post(f"/api/atworks/rules/{rule.rule_id}/apply", headers=h)
    assert r.status_code == 200 and r.json()["change"]["status"] == "applied"
    p = await client.post(f"/api/atworks/profiles/{profile.profile_id}/apply", headers=h)
    assert p.status_code == 200 and p.json()["change"]["status"] == "applied"


async def test_format_batch_action_looks_the_id_up_directly(client_backend, monkeypatch):
    """Final review I9. The format-batch route re-learned an unseen batch by scanning
    `get_pending_format_batches()` -- the PENDING list -- so a batch that was already applied or
    discarded could not be found at all, and the click failed the provenance gate instead of
    reporting what actually happened to it. It uses the ABC's `get_format_batch` now, which is
    status-blind, like `get_rule`/`get_profile`."""
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    batch = await backend.stage_format_batch(
        session, FormatBatchDraft(summary="사내 코드 포맷", formats=[
            {"name": "contract-no", "pattern": r"^C-\d{6}$", "pass_examples": ["C-123456"],
             "fail_examples": ["C-12345"]}]), ActorKind.AGENT)

    async def _boom(*args, **kwargs):
        raise AssertionError("the action path must not scan the pending queue to find one id")

    monkeypatch.setattr(backend, "get_pending_format_batches", _boom)

    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    h = {"X-Session-Id": sid}
    r = await client.post(f"/api/atworks/format-batches/{batch.batch_id}/apply", headers=h)
    assert r.status_code == 200 and r.json()["change"]["status"] == "applied"


async def test_the_four_host_surfaces_share_one_helper_and_keep_their_wording(client_backend):
    """Final review I8: four 40-line copies became one `host_action` + four one-line wrappers.
    Behaviour is what must not move -- the audit pair, the mark lifecycle, and the operator-facing
    sentence each surface queues for the next turn."""
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    job = await backend.stage_job(
        session, JobDraft(kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"],
                          target_envs=["dev"]), ActorKind.AGENT)
    rule = await backend.stage_rule(
        session, RuleDraft(api_id="api-001", param="contractNo", kind="required"), ActorKind.AGENT)

    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    h = {"X-Session-Id": sid}
    assert (await client.post(f"/api/atworks/changes/{job.job_id}/apply", headers=h)).status_code == 200
    assert (await client.post(f"/api/atworks/rules/{rule.rule_id}/discard", headers=h)).status_code == 200

    entries = [e.model_dump(mode="json") for e in (await backend.audit(session, limit=50)).items]
    pairs = {(e["action"], e["target_kind"]) for e in entries}
    assert ("apply_job", "job") in pairs and ("apply_job:ok", "job") in pairs
    assert ("discard_rule", "rule") in pairs and ("discard_rule:ok", "rule") in pairs
    # the ledgers really moved (the helper is a refactor, not a stub)
    assert (await backend.get_job(session, job.job_id)).status.value == "applied"
    assert (await backend.get_rule(session, rule.rule_id)).status.value == "discarded"


async def test_apply_rule_route_marks_then_consumes_approval(client_backend):
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    rule = await backend.stage_rule(
        session, RuleDraft(api_id="api-001", param="contractNo", kind="required"), ActorKind.AGENT
    )
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post(f"/api/atworks/rules/{rule.rule_id}/apply", headers={"X-Session-Id": sid})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and body["change"]["status"] == "applied"

    # the mark is spent on the first click — a second click (or a chat turn) finds no mark left.
    r2 = await client.post(f"/api/atworks/rules/{rule.rule_id}/apply", headers={"X-Session-Id": sid})
    if r2.status_code == 200:
        assert r2.json()["ok"] is False
    else:
        assert r2.status_code == 400


@pytest.fixture
async def client_for_chat_staged_rule(tmp_path):
    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    scripted = FakeClient([
        tool_calls_message(("get_api", {"api_id": "api-001"}, "tu-get")),
        tool_calls_message(("stage_rule", {"api_id": "api-001", "param": "contractNo", "kind": "required"}, "tu-stage")),
        text_message("규칙을 스테이징했습니다."),
    ])
    agent = AtworksAgent(backend=backend, skills_dir=SKILLS, config=config, client=scripted)
    reports = Reports(tmp_path)
    briefings = Briefings(tmp_path / "b", config)
    app = create_app(agent=agent, backend=backend, scheduler=Scheduler(backend, reports, None, briefings=briefings),
                      reports=reports, briefings=briefings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        yield c


async def test_apply_rule_after_chat_stage_survives_session_reload(client_for_chat_staged_rule):
    # Regression for the apply_rule 400: stage the rule through a real chat turn (so
    # seen_rules is populated the way the bug actually happened, not via a direct backend
    # call), let the session state round-trip through the store's JSON serialization
    # between requests (as the real HTTP flow does), then approve it from a fresh request —
    # exactly the "approve on the Rules page" path that used to raise
    # AttributeError: 'dict' object has no attribute 'api_id'.
    client = client_for_chat_staged_rule
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    headers = {"X-Session-Id": sid}

    chat = await client.post("/api/atworks/chat", headers=headers, json={"message": "contractNo 필수값 규칙 추가해줘"})
    assert chat.status_code == 200

    rules = (await client.get("/api/atworks/rules", headers=headers)).json()["items"]
    assert len(rules) == 1
    rule_id = rules[0]["rule_id"]

    # A brand-new request: CurrentSession reloads AtworksSessionState from the store's
    # JSON document here, the same reload that used to turn seen_rules entries into
    # plain dicts.
    r = await client.post(f"/api/atworks/rules/{rule_id}/apply", headers=headers)
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["change"]["status"] == "applied"
    assert body["change"]["effective_from"] is not None


async def test_apply_unknown_rule_returns_ok_false(client):
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post("/api/atworks/rules/rule-9999/apply", headers={"X-Session-Id": sid})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False


async def test_rule_route_discard_records_operator(client_backend):
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    rule = await backend.stage_rule(
        session, RuleDraft(api_id="api-001", param="contractNo", kind="required"), ActorKind.AGENT
    )
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post(f"/api/atworks/rules/{rule.rule_id}/discard", headers={"X-Session-Id": sid})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["change"]["discarded_by_kind"] == "operator"


async def test_formats_route_needs_a_session(client):
    assert (await client.get("/api/atworks/formats")).status_code in (401, 422)


async def test_formats_route_lists_the_library(client):
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.get("/api/atworks/formats", headers={"X-Session-Id": sid})
    assert r.status_code == 200
    names = [row["name"] for row in r.json()["formats"]]
    assert "email" in names


async def test_format_batches_route_needs_a_session(client):
    assert (await client.get("/api/atworks/format-batches")).status_code in (401, 422)


async def test_format_batches_route_lists_staged_batches(client_backend):
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    batch = await backend.stage_format_batch(
        session, FormatBatchDraft(formats=[{
            "name": "phone-digits", "pattern": r"^\d{3}-\d{4}$",
            "pass_examples": ["123-4567"], "fail_examples": ["abc"],
        }]), ActorKind.AGENT
    )
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.get("/api/atworks/format-batches", headers={"X-Session-Id": sid})
    assert r.status_code == 200
    assert [row["batch_id"] for row in r.json()["format_batches"]] == [batch.batch_id]


async def test_apply_format_batch_route_marks_then_consumes_approval(client_backend):
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    batch = await backend.stage_format_batch(
        session, FormatBatchDraft(formats=[{
            "name": "phone-digits", "pattern": r"^\d{3}-\d{4}$",
            "pass_examples": ["123-4567"], "fail_examples": ["abc"],
        }]), ActorKind.AGENT
    )
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post(f"/api/atworks/format-batches/{batch.batch_id}/apply", headers={"X-Session-Id": sid})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and body["change"]["status"] == "applied"
    assert body["change"]["new_count"] == 1

    names = [row["name"] for row in (await client.get(
        "/api/atworks/formats", headers={"X-Session-Id": sid}
    )).json()["formats"]]
    assert "phone-digits" in names

    # the mark is spent on the first click — a second click (or a chat turn) finds no mark left.
    r2 = await client.post(f"/api/atworks/format-batches/{batch.batch_id}/apply", headers={"X-Session-Id": sid})
    if r2.status_code == 200:
        assert r2.json()["ok"] is False
    else:
        assert r2.status_code == 400


async def test_apply_unknown_format_batch_returns_ok_false(client):
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post("/api/atworks/format-batches/batch-9999/apply", headers={"X-Session-Id": sid})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False


async def test_format_batch_route_discard_records_operator(client_backend):
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    batch = await backend.stage_format_batch(
        session, FormatBatchDraft(formats=[{
            "name": "phone-digits", "pattern": r"^\d{3}-\d{4}$",
            "pass_examples": ["123-4567"], "fail_examples": ["abc"],
        }]), ActorKind.AGENT
    )
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post(f"/api/atworks/format-batches/{batch.batch_id}/discard", headers={"X-Session-Id": sid})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["change"]["discarded_by_kind"] == "operator"


async def test_profiles_route_needs_a_session(client):
    assert (await client.get("/api/atworks/profiles")).status_code in (401, 422)


async def test_profiles_route_lists_staged_profiles_and_filters_by_job(client_backend):
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    profile = await backend.stage_profile(
        session, ProfileDraft(job_id="job-0001", ignore_paths=["$.updatedAt"], summary="ignore timestamp noise"),
        ActorKind.AGENT,
    )
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.get("/api/atworks/profiles", headers={"X-Session-Id": sid})
    assert r.status_code == 200
    assert [row["profile_id"] for row in r.json()["items"]] == [profile.profile_id]

    matching = await client.get("/api/atworks/profiles", headers={"X-Session-Id": sid}, params={"job_id": "job-0001"})
    assert [row["profile_id"] for row in matching.json()["items"]] == [profile.profile_id]

    other_job = await client.get("/api/atworks/profiles", headers={"X-Session-Id": sid}, params={"job_id": "job-9999"})
    assert other_job.json()["items"] == []


async def test_apply_profile_route_marks_then_consumes_approval(client_backend):
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    profile = await backend.stage_profile(
        session, ProfileDraft(job_id="job-0001", ignore_paths=["$.updatedAt"], summary="ignore timestamp noise"),
        ActorKind.AGENT,
    )
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post(f"/api/atworks/profiles/{profile.profile_id}/apply", headers={"X-Session-Id": sid})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and body["change"]["status"] == "applied"

    listed = (await client.get("/api/atworks/profiles", headers={"X-Session-Id": sid})).json()["items"]
    assert listed[0]["status"] == "applied"

    # the mark is spent on the first click — a second click (or a chat turn) finds no mark left.
    r2 = await client.post(f"/api/atworks/profiles/{profile.profile_id}/apply", headers={"X-Session-Id": sid})
    if r2.status_code == 200:
        assert r2.json()["ok"] is False
    else:
        assert r2.status_code == 400


async def test_apply_profile_is_held_without_the_host_route(client_backend):
    # approved_profile_ids may ONLY be set by profile_action (the host route), immediately
    # before the executor call and cleared immediately after. Building the executor exactly
    # the way the route does but skipping the route's own approval mark — the way a model's
    # own tool call would — proves the mark can't be self-granted from chat.
    client, backend = client_backend
    del client
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    profile = await backend.stage_profile(
        session, ProfileDraft(job_id="job-0001", ignore_paths=["$.updatedAt"], summary="ignore timestamp noise"),
        ActorKind.AGENT,
    )
    config = AtworksAgentConfig(model="m")
    agent = AtworksAgent(backend=backend, skills_dir=SKILLS, config=config, client=FakeClient([text_message("ok")]))
    state = AtworksSessionState()
    state.remember_profile(profile)
    executor = agent.executor_class(backend=backend, config=agent.config, skills=agent.skills,
                                    session=session, state=state, memory=agent.memory)
    outcome = await executor.execute("apply_profile", {"profile_id": profile.profile_id})
    assert outcome.blocked == "approval"


async def test_apply_unknown_profile_returns_ok_false(client):
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post("/api/atworks/profiles/profile-9999/apply", headers={"X-Session-Id": sid})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False


async def test_profile_route_discard_records_operator(client_backend):
    client, backend = client_backend
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    profile = await backend.stage_profile(
        session, ProfileDraft(job_id="job-0001", ignore_paths=["$.updatedAt"], summary="ignore timestamp noise"),
        ActorKind.AGENT,
    )
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post(f"/api/atworks/profiles/{profile.profile_id}/discard", headers={"X-Session-Id": sid})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["change"]["discarded_by_kind"] == "operator"


CUSTOM_EXECUTOR_MARKER = "held-by-a-deployments-own-executor-subclass"


class _TaggingExecutor(AtworksToolExecutor):
    async def _discard_job(self, tool_input):
        return ToolOutcome.held("guardrail", CUSTOM_EXECUTOR_MARKER)


@pytest.fixture
async def client_with_custom_executor(tmp_path):
    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    agent = AtworksAgent(backend=backend, skills_dir=SKILLS, config=config,
                         client=FakeClient([text_message("ok")]), executor_class=_TaggingExecutor)
    reports = Reports(tmp_path)
    briefings = Briefings(tmp_path / "b", config)
    app = create_app(agent=agent, backend=backend, scheduler=Scheduler(backend, reports, None, briefings=briefings),
                      reports=reports, briefings=briefings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        yield c, backend


async def test_job_action_route_uses_the_agents_executor_class(client_with_custom_executor):
    client, backend = client_with_custom_executor
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    job = await backend.stage_job(
        session, JobDraft(kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT
    )
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post(f"/api/atworks/changes/{job.job_id}/discard", headers={"X-Session-Id": sid})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False
    assert body["reason"] == CUSTOM_EXECUTOR_MARKER


async def test_briefing_routes(client):
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    assert (await client.get("/api/atworks/briefings/latest", headers={"X-Session-Id": sid})).status_code == 404
    await client.post("/api/atworks/scheduler/tick?now=2026-09-03T09:01:00%2B09:00")
    r = await client.get("/api/atworks/briefings/latest", headers={"X-Session-Id": sid})
    assert r.status_code == 200 and r.json()["date"] == "2026-09-03"
    page = await client.get("/api/atworks/briefings/2026-09-03")
    assert page.status_code == 200 and "briefing-data" in page.text
    assert (await client.get("/api/atworks/briefings/..%2F2026")).status_code in (404, 422)


async def test_home_insights_routes(client):
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    headers = {"X-Session-Id": sid}

    r = await client.get("/api/atworks/home/insights", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["role"] in ("developer", "qa", "pm")
    assert "scope_fallback" in body and isinstance(body["items"], list)

    r2 = await client.post("/api/atworks/home/insights/refresh", headers=headers)
    assert r2.status_code == 200


async def test_home_insights_without_insights_kwarg_returns_404(client_backend):
    client, _backend = client_backend
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    headers = {"X-Session-Id": sid}
    assert (await client.get("/api/atworks/home/insights", headers=headers)).status_code == 404
    assert (await client.post("/api/atworks/home/insights/refresh", headers=headers)).status_code == 404


async def test_home_insights_returns_404_when_panel_disabled(tmp_path):
    config = AtworksAgentConfig(model="m", enable_insight_panel=False)
    backend = MockAtworks(config, FIXTURES)
    agent = AtworksAgent(backend=backend, skills_dir=SKILLS, config=config, client=FakeClient([text_message("ok")]))
    reports = Reports(tmp_path)
    briefings = Briefings(tmp_path / "b", config)
    insights = InsightPanels(tmp_path / "insights", config, narrator=_fake_narrator)
    app = create_app(agent=agent, backend=backend, scheduler=Scheduler(backend, reports, None, briefings=briefings),
                      reports=reports, briefings=briefings, insights=insights)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        sid = (await c.post("/api/atworks/session")).json()["session_id"]
        assert (await c.get("/api/atworks/home/insights", headers={"X-Session-Id": sid})).status_code == 404
