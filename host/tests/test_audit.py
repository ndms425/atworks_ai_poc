"""Task 9 (scale spec §2/§3): the append-only audit log. Every host approval surface writes two
rows -- the attempt, then its outcome -- and every ledger state change writes one of its own, so
"AI가 무엇을 바꿨나 → 아무것도; 사람이 이 시각에 승인했다" is evidence, not a claim. A chat turn
whose ``apply_*`` is HELD (no host approval mark) writes nothing at all."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from commerce_common.testing import FakeClient, text_message
from httpx import ASGITransport, AsyncClient

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksSessionContext,
    AtworksSessionState,
    FormatBatchDraft,
    JobDraft,
    JobKind,
    JobStatus,
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
STAGING = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")


async def _narrator(candidates, role, *, notes=None):
    del candidates, role, notes
    return []


@pytest.fixture
async def client(tmp_path):
    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    agent = AtworksAgent(backend=backend, skills_dir=SKILLS, config=config,
                         client=FakeClient([text_message("ok")]))
    reports = Reports(tmp_path)
    briefings = Briefings(tmp_path / "b", config)
    insights = InsightPanels(tmp_path / "insights", config, narrator=_narrator)
    app = create_app(agent=agent, backend=backend, briefings=briefings, reports=reports,
                     scheduler=Scheduler(backend, reports, None, briefings=briefings), insights=insights)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        c.backend = backend
        c.agent = agent
        yield c


async def _rows(client, sid) -> list[dict]:
    """Oldest-first, so a test reads the sequence in the order it happened."""
    body = (await client.get("/api/atworks/audit?limit=200", headers={"X-Session-Id": sid})).json()
    return list(reversed(body["items"]))


async def test_a_job_approval_click_writes_the_attempt_the_effect_and_the_outcome(client):
    backend = client.backend
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    job = await backend.stage_job(STAGING, JobDraft(
        kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
    before = len(await _rows(client, sid))

    r = await client.post(f"/api/atworks/changes/{job.job_id}/apply", headers={"X-Session-Id": sid})
    assert r.status_code == 200 and r.json()["ok"] is True

    rows = (await _rows(client, sid))[before:]
    assert [row["action"] for row in rows] == ["apply_job", "applied", "apply_job:ok"]
    assert {row["target_kind"] for row in rows} == {"job"}
    assert {row["target_id"] for row in rows} == {job.job_id}
    assert {row["operator"] for row in rows} == {"minseong"}
    assert all(row["session_id"] == sid for row in rows)
    assert [row["seq"] for row in rows] == sorted(row["seq"] for row in rows)


async def test_a_discard_click_writes_two_rows_and_no_applied_row(client):
    backend = client.backend
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    job = await backend.stage_job(STAGING, JobDraft(
        kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
    before = len(await _rows(client, sid))

    await client.post(f"/api/atworks/changes/{job.job_id}/discard", headers={"X-Session-Id": sid})

    rows = (await _rows(client, sid))[before:]
    assert [row["action"] for row in rows] == ["discard_job", "discard_job:ok"]


async def test_a_blocked_click_still_leaves_the_attempt_on_the_record(client):
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    before = len(await _rows(client, sid))

    r = await client.post("/api/atworks/changes/job-9999/apply", headers={"X-Session-Id": sid})
    assert r.status_code == 200 and r.json()["ok"] is False

    rows = (await _rows(client, sid))[before:]
    assert [row["action"] for row in rows] == ["apply_job", "apply_job:blocked"]


async def test_rule_profile_and_format_batch_clicks_write_their_own_kinds(client):
    backend = client.backend
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    h = {"X-Session-Id": sid}

    rule = await backend.stage_rule(STAGING, RuleDraft(
        api_id="api-001", param="amount", kind="compare", op=">=", value="0"), ActorKind.AGENT)
    profile = await backend.stage_profile(STAGING, ProfileDraft(
        job_id="job-0001", ignore_paths=["$.updatedAt"], summary="ignore timestamp noise"), ActorKind.AGENT)
    batch = await backend.stage_format_batch(STAGING, FormatBatchDraft(formats=[
        {"name": "zipcode", "pattern": r"^\d{5}$",
         "pass_examples": ["12345"], "fail_examples": ["1234"]}]), ActorKind.AGENT)
    before = len(await _rows(client, sid))

    assert (await client.post(f"/api/atworks/rules/{rule.rule_id}/apply", headers=h)).json()["ok"]
    assert (await client.post(f"/api/atworks/profiles/{profile.profile_id}/apply", headers=h)).json()["ok"]
    assert (await client.post(f"/api/atworks/format-batches/{batch.batch_id}/apply", headers=h)).json()["ok"]

    rows = (await _rows(client, sid))[before:]
    assert [(row["action"], row["target_kind"]) for row in rows] == [
        ("apply_rule", "rule"), ("applied", "rule"), ("apply_rule:ok", "rule"),
        ("apply_profile", "profile"), ("applied", "profile"), ("apply_profile:ok", "profile"),
        ("apply_format_batch", "format_batch"), ("applied", "format_batch"),
        ("apply_format_batch:ok", "format_batch"),
    ]


async def test_a_held_chat_apply_writes_no_audit_row(client):
    """The executor path a model's own tool call takes: no host approval mark, so ``apply_job``
    is HELD and never reaches the ledger -- and therefore never reaches the audit log either.
    A row here would mean the log records intentions the system did not act on."""
    backend = client.backend
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    job = await backend.stage_job(STAGING, JobDraft(
        kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
    before = await _rows(client, sid)

    config = AtworksAgentConfig(model="m")
    agent = AtworksAgent(backend=backend, skills_dir=SKILLS, config=config,
                         client=FakeClient([text_message("ok")]))
    state = AtworksSessionState()
    state.remember_job(job)
    executor = agent.executor_class(backend=backend, config=config, skills=agent.skills,
                                    session=STAGING, state=state, memory=agent.memory)
    outcome = await executor.execute("apply_job", {"job_id": job.job_id})

    assert outcome.blocked == "approval"
    assert await _rows(client, sid) == before


async def test_the_audit_route_pages_newest_first_and_reports_the_total(client):
    backend = client.backend
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    for _ in range(4):
        job = await backend.stage_job(STAGING, JobDraft(
            kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
        await client.post(f"/api/atworks/changes/{job.job_id}/apply", headers={"X-Session-Id": sid})

    first = (await client.get("/api/atworks/audit?limit=5", headers={"X-Session-Id": sid})).json()
    assert first["total"] == 12 and len(first["items"]) == 5 and first["next_cursor"]
    assert [row["seq"] for row in first["items"]] == sorted(
        (row["seq"] for row in first["items"]), reverse=True)

    second = (await client.get(f"/api/atworks/audit?limit=5&cursor={first['next_cursor']}",
                               headers={"X-Session-Id": sid})).json()
    assert second["total"] == 12
    assert {row["seq"] for row in first["items"]}.isdisjoint(row["seq"] for row in second["items"])


async def test_the_audit_route_needs_a_session(client):
    assert (await client.get("/api/atworks/audit")).status_code in (401, 422)


# -- 승인 마크는 어떤 경로에서도 남지 않는다 (Task 9 리뷰 Minor 6) --------------------------------


class _Boom:
    """An executor that dies inside the route, between the mark going on and coming off."""

    def __init__(self, **kwargs):
        _Boom.state = kwargs["state"]

    async def execute(self, action, args):
        raise RuntimeError("executor exploded")


class _Errored:
    """An executor whose turn ends in an error result rather than an exception."""

    def __init__(self, **kwargs):
        self.state = kwargs["state"]

    async def execute(self, action, args):
        return SimpleNamespace(is_error=True, blocked=None, result_text="tool failed", events=[])


async def test_an_exploding_executor_leaves_no_approval_mark_and_both_audit_rows(client):
    """CLAUDE.md: "마크는 클릭 직전에 붙고, 결과와 무관하게 직후에 떨어진다." Without the
    try/finally an executor that raised left `approved_job_ids` set on the session state -- a host
    approval nobody clicked, waiting for the next chat turn to spend."""
    backend = client.backend
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    job = await backend.stage_job(STAGING, JobDraft(
        kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"], target_envs=["dev"]), ActorKind.AGENT)
    before = len(await _rows(client, sid))
    client.agent.executor_class = _Boom

    with pytest.raises(RuntimeError, match="executor exploded"):
        await client.post(f"/api/atworks/changes/{job.job_id}/apply", headers={"X-Session-Id": sid})

    assert _Boom.state.approved_job_ids == set()               # the mark came off anyway
    assert _Boom.state.host_action_job_ids == set()
    rows = (await _rows(client, sid))[before:]
    assert [row["action"] for row in rows] == ["apply_job", "apply_job:error"]
    assert backend.ledger.get(job.job_id).status is JobStatus.STAGED    # nothing was applied


async def test_an_error_result_writes_the_error_row_and_still_clears_the_mark(client):
    """Minor 5: the `:error` outcome row had no test. It precedes the 400 the route raises."""
    backend = client.backend
    sid = (await client.post("/api/atworks/session")).json()["session_id"]
    rule = await backend.stage_rule(STAGING, RuleDraft(
        api_id="api-001", param="amount", kind="compare", op=">=", value="0"), ActorKind.AGENT)
    before = len(await _rows(client, sid))
    client.agent.executor_class = _Errored

    r = await client.post(f"/api/atworks/rules/{rule.rule_id}/apply", headers={"X-Session-Id": sid})

    assert r.status_code == 400 and r.json()["detail"] == "tool failed"
    rows = (await _rows(client, sid))[before:]
    assert [row["action"] for row in rows] == ["apply_rule", "apply_rule:error"]
    assert rows[-1]["target_kind"] == "rule" and rows[-1]["target_id"] == rule.rule_id
