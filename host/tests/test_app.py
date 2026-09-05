from datetime import UTC, datetime
from pathlib import Path

import pytest
from commerce_common.streaming import ToolOutcome
from commerce_common.testing import FakeClient, text_message, tool_calls_message
from httpx import ASGITransport, AsyncClient

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksSessionContext,
    AtworksToolExecutor,
    JobDraft,
    JobKind,
    RuleDraft,
)
from atworks_agent_runtime import AtworksAgent
from atworks_host.app import create_app
from atworks_host.briefing import Briefings
from atworks_host.mock_backend import MockAtworks
from atworks_host.reports import Reports
from atworks_host.scheduler import Scheduler

FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"
SKILLS = Path(__file__).resolve().parents[2] / "atworks-agent" / "skills"


@pytest.fixture
async def client(tmp_path):
    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    agent = AtworksAgent(backend=backend, skills_dir=SKILLS, config=config, client=FakeClient([text_message("ok")]))
    reports = Reports(tmp_path)
    briefings = Briefings(tmp_path / "b", config)
    app = create_app(agent=agent, backend=backend, scheduler=Scheduler(backend, reports, None, briefings=briefings),
                      reports=reports, briefings=briefings)
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
    assert body["flaky"] == 1 and body["regression_suspect"] == 1 and body["window_days"] == 30


async def test_runs_insights_needs_a_session(client):
    assert (await client.get("/api/atworks/runs/insights")).status_code in (401, 422)


async def test_session_and_reads(client):
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    h = {"X-Session-Id": sid}
    assert len((await client.get("/api/atworks/apis", headers=h)).json()["apis"]) == 12
    runs = (await client.get("/api/atworks/runs?status=fail", headers=h)).json()
    assert runs["population"] >= len(runs["runs"]) > 0
    assert (await client.get("/api/atworks/jobs", headers=h)).json()["jobs"] == []


async def test_chat_streams_sse_with_attachments(client):
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    r = await client.post("/api/atworks/chat", headers={"X-Session-Id": sid},
                          json={"message": "이거 왜 실패했어", "attached_items": [{"order": 1, "kind": "run", "ref_id": "run-0001", "label": "환불", "comment": "왜"}]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert "event: text_delta" in r.text and "event: turn_complete" in r.text


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
    runs = await backend.runs_by_ids(session, applied.run_ids)
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
    reports.write(job, runs)
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
    assert [row["rule_id"] for row in r.json()["rules"]] == [rule.rule_id]


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

    rules = (await client.get("/api/atworks/rules", headers=headers)).json()["rules"]
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
