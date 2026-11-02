"""Saved conversations: the SQLite session store, the per-turn display record, and the routes
that list, reopen, rename and delete a conversation (one session = one conversation)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from commerce_common.streaming import AgentEvent
from commerce_common.testing import FakeClient, text_message
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from atworks_agent import (
    ActorKind,
    AtworksAgentConfig,
    AtworksSessionContext,
    AtworksSessionState,
    JobDraft,
    JobKind,
)
from atworks_agent_runtime import AtworksAgent
from atworks_host.app import create_app
from atworks_host.briefing import Briefings
from atworks_host.conversations import FAILED_TURN_TEXT, SqliteSessionStore, TurnDisplay, title_from
from atworks_host.mock_backend import MockAtworks
from atworks_host.reports import Reports
from atworks_host.scheduler import Scheduler
from atworks_host.sessions import SessionConflictError, UnknownSessionError

FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"
SKILLS = Path(__file__).resolve().parents[2] / "atworks-agent" / "skills"


class _State(BaseModel):
    seen: list[str] = []


# -- The store ---------------------------------------------------------------------------------


def test_a_session_survives_reopening_the_file(tmp_path):
    path = tmp_path / "c.sqlite"
    store = SqliteSessionStore(path, _State)
    record = store.start("minseong")
    record.state.seen.append("api-001")
    record.messages.append({"role": "user", "content": "안녕"})
    record.pending_app_events.append("job-1 approved")
    store.save(record)

    reopened = SqliteSessionStore(path, _State).require(record.session_id)
    assert reopened.user_id == "minseong"
    assert reopened.state.seen == ["api-001"]
    assert reopened.messages == [{"role": "user", "content": "안녕"}]
    assert reopened.pending_app_events == ["job-1 approved"]


def test_a_stale_version_is_refused_like_the_in_memory_store():
    store = SqliteSessionStore(":memory:", _State)
    record = store.start("minseong")
    first, second = store.require(record.session_id), store.require(record.session_id)
    first.state.seen.append("a")
    store.save(first)
    second.state.seen.append("b")
    with pytest.raises(SessionConflictError):
        store.save(second)


def test_a_rewritten_transcript_replaces_from_start():
    store = SqliteSessionStore(":memory:", _State)
    record = store.start("minseong")
    record.messages.extend([{"role": "user", "content": str(i)} for i in range(3)])
    store.save(record)
    record.messages[1] = {"role": "user", "content": "cleared"}
    record.messages.pop()
    record.stored_messages = 0   # what stream_turn does after compaction
    store.save(record)
    assert [m["content"] for m in store.read_messages(record.session_id)] == ["0", "cleared"]


def test_reset_deletes_and_save_after_reset_writes_nothing():
    store = SqliteSessionStore(":memory:", _State)
    record = store.start("minseong")
    store.reset(record)
    record.messages.append({"role": "user", "content": "x"})
    store.save(record)
    with pytest.raises(UnknownSessionError):
        store.require(record.session_id)


def test_sweep_removes_only_idle_sessions_nobody_chatted_in():
    clock = {"now": datetime(2026, 11, 1, 9, tzinfo=UTC)}
    store = SqliteSessionStore(":memory:", _State, clock=lambda: clock["now"])
    empty = store.start("minseong")
    chatted = store.start("minseong")
    store.record_turn(chatted.session_id, "실패한 API 보여줘", {"segments": [], "suggestions": []})
    clock["now"] += timedelta(hours=30)
    fresh = store.start("minseong")

    assert store.sweep(24, clock["now"]) == 1
    assert store.read_state(empty.session_id) is None
    assert store.read_state(chatted.session_id) is not None
    assert store.read_state(fresh.session_id) is not None


def test_first_turn_names_the_conversation_and_list_is_per_operator():
    store = SqliteSessionStore(":memory:", _State)
    mine, theirs, empty = store.start("minseong"), store.start("jihoon"), store.start("minseong")
    assert store.record_turn(mine.session_id, "  어제   실패한 결제 API\n원인 묶어줘  ", {"segments": [], "suggestions": []}) == 1
    assert store.record_turn(mine.session_id, "두 번째 질문", {"segments": [], "suggestions": []}) == 2
    store.record_turn(theirs.session_id, "다른 사람 질문", {"segments": [], "suggestions": []})

    items, total = store.list_conversations("minseong")
    assert total == 1 and [i["id"] for i in items] == [mine.session_id]
    assert items[0]["title"] == "어제 실패한 결제 API 원인 묶어줘" and items[0]["turn_count"] == 2
    assert store.conversation(theirs.session_id, "minseong") is None
    assert store.conversation(empty.session_id, "minseong") is not None   # exists, just not listed

    assert store.list_conversations("minseong", "결제")[1] == 1
    assert store.list_conversations("minseong", "없는말")[1] == 0
    assert store.list_conversations("minseong", "%")[1] == 0   # LIKE wildcards are literal


def test_title_is_cut_at_forty_characters():
    assert title_from("가" * 60) == "가" * 39 + "…"
    assert title_from("짧은 질문") == "짧은 질문"


# -- The display record ------------------------------------------------------------------------


def _ui(component, payload, stream_id=None):
    data = {"component": component, "payload": payload}
    if stream_id:
        data["stream_id"] = stream_id
    return AgentEvent(type="ui", data=data)


def test_display_folds_the_stream_the_way_the_portal_does():
    display = TurnDisplay()
    for event in [
        AgentEvent(type="text_delta", data={"text": "먼저 "}),
        AgentEvent(type="text_delta", data={"text": "조회합니다."}),
        AgentEvent(type="tool_call", data={"tool": "list_runs", "id": "t1", "input": {}}),
        AgentEvent(type="text_delta", data={"text": "결과입니다."}),
        AgentEvent(type="ui_partial", data={"component": "run_digest", "payload": {"items": [1]}}),
        _ui("run_digest", {"items": [1]}, "s1"),
        _ui("run_digest", {"items": [1, 2]}, "s1"),           # retry of the same stream
        _ui("screen_highlight", {"targets": []}),              # executes, never replayed
        _ui("suggestions", {"suggestions": ["다음 보기", "재실행"]}),
        AgentEvent(type="error", data={"message": "일부 실패"}),
    ]:
        display.observe(event)

    doc = display.document()
    assert doc["suggestions"] == ["다음 보기", "재실행"]
    assert doc["segments"] == [
        {"type": "text", "text": "먼저 조회합니다.\n\n결과입니다."},
        {"type": "ui", "component": "run_digest", "payload": {"items": [1, 2]}},
        {"type": "error", "text": "일부 실패"},
    ]


def test_a_failed_turn_says_so_once():
    display = TurnDisplay()
    display.note_failure()
    display.note_failure()
    assert display.document()["segments"] == [{"type": "error", "text": FAILED_TURN_TEXT}]


# -- The routes --------------------------------------------------------------------------------


@pytest.fixture
async def app_client(tmp_path):
    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    scripted = FakeClient([text_message("첫 답변입니다."), text_message("두 번째 답변입니다."),
                           text_message("세 번째 답변입니다.")])
    agent = AtworksAgent(backend=backend, skills_dir=SKILLS, config=config, client=scripted)
    reports = Reports(tmp_path)
    briefings = Briefings(tmp_path / "b", config)
    sessions = SqliteSessionStore(tmp_path / "conversations.sqlite", AtworksSessionState)
    app = create_app(agent=agent, backend=backend, scheduler=Scheduler(backend, reports, None, briefings=briefings),
                     reports=reports, briefings=briefings, sessions=sessions)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        c.backend, c.sessions, c.fake = backend, sessions, scripted
        yield c


async def _start(client, operator="minseong") -> dict:
    r = await client.post("/api/atworks/session", json={"operator_id": operator})
    return {"X-Session-Id": r.json()["session_id"]}


async def _chat(client, headers, message):
    r = await client.post("/api/atworks/chat", headers=headers, json={"message": message})
    assert r.status_code == 200
    return r.text


async def test_a_chatted_session_is_listed_and_reopens_with_its_turns(app_client):
    first = await _start(app_client)
    await _chat(app_client, first, "실패한 API 알려줘")

    second = await _start(app_client)   # "새 대화": the first one stays
    listing = (await app_client.get("/api/atworks/conversations", headers=second)).json()
    assert listing["total"] == 1
    [item] = listing["items"]
    assert item["id"] == first["X-Session-Id"] and item["title"] == "실패한 API 알려줘"
    assert item["turn_count"] == 1 and item["current"] is False

    opened = (await app_client.get(f"/api/atworks/conversations/{item['id']}", headers=second)).json()
    [turn] = opened["turns"]
    assert turn["turn"] == 1 and turn["user_text"] == "실패한 API 알려줘"
    assert turn["segments"] == [{"type": "text", "text": "첫 답변입니다."}]


async def test_a_reopened_conversation_continues_with_its_transcript(app_client):
    first = await _start(app_client)
    await _chat(app_client, first, "질문 하나")
    await _start(app_client)
    await _chat(app_client, first, "질문 둘")   # the portal switched its header back to `first`

    sent = app_client.fake.calls[-1]["messages"]
    texts = [m["content"] if isinstance(m["content"], str) else m["content"][-1].get("text") for m in sent]
    assert "질문 하나" in texts and "질문 둘" in texts
    turns = app_client.sessions.turns(first["X-Session-Id"])
    assert [t["turn"] for t in turns] == [1, 2]


async def test_another_operators_conversation_reads_as_missing(app_client):
    mine = await _start(app_client, "minseong")
    await _chat(app_client, mine, "내 질문")
    theirs = await _start(app_client, "jihoon")
    cid = mine["X-Session-Id"]
    assert (await app_client.get("/api/atworks/conversations", headers=theirs)).json()["total"] == 0
    assert (await app_client.get(f"/api/atworks/conversations/{cid}", headers=theirs)).status_code == 404
    assert (await app_client.patch(f"/api/atworks/conversations/{cid}", headers=theirs,
                                   json={"title": "x"})).status_code == 404
    assert (await app_client.delete(f"/api/atworks/conversations/{cid}", headers=theirs)).status_code == 404
    assert app_client.sessions.read_state(cid) is not None


async def test_rename_and_delete(app_client):
    old = await _start(app_client)
    await _chat(app_client, old, "이름 바꿀 대화")
    current = await _start(app_client)
    cid = old["X-Session-Id"]

    renamed = await app_client.patch(f"/api/atworks/conversations/{cid}", headers=current,
                                     json={"title": "결제 장애 분석"})
    assert renamed.json()["conversation"]["title"] == "결제 장애 분석"
    assert (await app_client.patch(f"/api/atworks/conversations/{cid}", headers=current,
                                   json={"title": ""})).status_code == 422

    gone = await app_client.delete(f"/api/atworks/conversations/{cid}", headers=current)
    assert gone.json() == {"ok": True, "current_deleted": False}
    assert (await app_client.get("/api/atworks/conversations", headers=current)).json()["total"] == 0


async def test_deleting_the_current_conversation_ends_its_session(app_client):
    current = await _start(app_client)
    await _chat(app_client, current, "지울 대화")
    r = await app_client.delete(f"/api/atworks/conversations/{current['X-Session-Id']}", headers=current)
    assert r.json() == {"ok": True, "current_deleted": True}
    assert (await app_client.get("/api/atworks/conversations", headers=current)).status_code == 401


async def test_a_reopened_job_card_shows_the_jobs_status_now(app_client):
    headers = await _start(app_client)
    session = AtworksSessionContext(session_id="staging", project_id="mes-demo", operator="minseong")
    job = await app_client.backend.stage_job(
        session, JobDraft(kind=JobKind.RUN_NOW, summary="s", api_ids=["api-001"], target_envs=["dev"]),
        ActorKind.AGENT,
    )
    app_client.sessions.record_turn(headers["X-Session-Id"], "실행해줘", {"segments": [
        {"type": "ui", "component": "job_preview",
         "payload": {"change_id": job.job_id, "job": {"job_id": job.job_id, "status": "staged"}}},
    ], "suggestions": []})
    applied = await app_client.post(f"/api/atworks/changes/{job.job_id}/apply", headers=headers)
    assert applied.json()["ok"] is True

    opened = (await app_client.get(f"/api/atworks/conversations/{headers['X-Session-Id']}",
                                   headers=headers)).json()
    assert opened["turns"][0]["segments"][0]["payload"]["job"]["status"] == "applied"


async def test_history_routes_404_on_an_in_memory_store(tmp_path):
    from atworks_host.retention import TimestampedSessionStore

    config = AtworksAgentConfig(model="m")
    backend = MockAtworks(config, FIXTURES)
    agent = AtworksAgent(backend=backend, skills_dir=SKILLS, config=config, client=FakeClient([text_message("ok")]))
    reports = Reports(tmp_path)
    briefings = Briefings(tmp_path / "b", config)
    app = create_app(agent=agent, backend=backend, scheduler=Scheduler(backend, reports, None, briefings=briefings),
                     reports=reports, briefings=briefings, sessions=TimestampedSessionStore(AtworksSessionState))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        headers = await _start(client)
        assert (await client.get("/api/atworks/conversations", headers=headers)).status_code == 404
        await _chat(client, headers, "기록 없이도 채팅은 된다")
