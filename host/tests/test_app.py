from pathlib import Path

import pytest
from commerce_common.testing import FakeClient, text_message
from httpx import ASGITransport, AsyncClient

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksSessionContext,
    JobDraft,
    JobKind,
)
from atworks_agent_runtime import AtworksAgent
from atworks_host.app import create_app
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
    app = create_app(agent=agent, backend=backend, scheduler=Scheduler(backend, reports, None), reports=reports)
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
    app = create_app(agent=agent, backend=backend, scheduler=Scheduler(backend, reports, None), reports=reports)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        yield c, backend


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
        session, JobDraft(kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"], target_env="dev"), ActorKind.AGENT
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


async def test_report_404_before_run(client):
    assert (await client.get("/api/atworks/reports/job-0001")).status_code == 404


async def test_report_opens_without_session_header(client):
    backend = client.backend
    reports = client.reports
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    job = await backend.stage_job(
        session, JobDraft(kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"], target_env="dev"), ActorKind.AGENT
    )
    await backend.apply_job(session, job.job_id)
    runs = await backend.execute_job_once(session, job.job_id)
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
