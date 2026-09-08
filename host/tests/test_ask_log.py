"""ask_log end to end (self-growth spec §6): the Store table, the two read routes, and the
turn-end hook that writes exactly one row per chat turn.

The hook is the part worth the setup cost of a real client: it has to fire once per turn whether
the turn completed, raised, or only staged something, and it must never be able to break a turn
that already reached the operator.
"""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from commerce_common.testing import FakeClient, text_message, tool_calls_message
from httpx import ASGITransport, AsyncClient

from atworks_agent import AskEntry, AtworksAgentConfig, QueryFilters, QuerySpec
from atworks_agent_runtime import AtworksAgent
from atworks_host.app import create_app
from atworks_host.briefing import Briefings
from atworks_host.mock_backend import MockAtworks
from atworks_host.reports import Reports
from atworks_host.scheduler import Scheduler
from atworks_host.store import Store

FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"
SKILLS = Path(__file__).resolve().parents[2] / "atworks-agent" / "skills"
T0 = datetime(2026, 9, 8, 9, tzinfo=UTC)


def _entry(turn_id: str, *, at: datetime | None = None, outcome: str = "answered",
           cluster_key: str = "dims=api|measures=non_pass|filters=window_days",
           question: str = "실패 보여줘", unmet_reason: str | None = None,
           operator: str = "minseong", spec: QuerySpec | None = None) -> AskEntry:
    return AskEntry(
        at=at or T0, session_id="s-1", operator=operator, role="qa", question=question,
        intent="aggregate", spec=spec, outcome=outcome, unmet_reason=unmet_reason,
        wanted=None, tool_calls=2, cards=1, cluster_key=cluster_key, turn_id=turn_id,
    )


def _store() -> Store:
    return Store(":memory:")


# -- Store ---------------------------------------------------------------------------------

def test_insert_ask_round_trips_including_the_spec():
    store = _store()
    spec = QuerySpec(dimensions=["api"], measures=["non_pass"], filters=QueryFilters(window_days=30))
    stored = store.insert_ask(_entry("t-1", spec=spec))
    assert stored.seq is not None
    back = store.list_asks().items[0]
    assert back.spec == spec and back.turn_id == "t-1" and back.role == "qa"
    assert back.tool_calls == 2 and back.cards == 1 and back.feedback is None


def test_insert_ask_is_idempotent_on_turn_id():
    # The hook is best-effort and may fire twice for one turn; two rows would skew every ratio
    # the Growth view publishes.
    store = _store()
    first = store.insert_ask(_entry("t-1"))
    second = store.insert_ask(_entry("t-1", question="다른 질문", outcome="partial"))
    assert store.list_asks().total == 1
    assert second.seq == first.seq and second.question == "실패 보여줘" and second.outcome == "answered"


def test_list_asks_pages_newest_first_with_a_working_cursor():
    store = _store()
    for i in range(5):
        store.insert_ask(_entry(f"t-{i}", at=T0 + timedelta(minutes=i)))
    page = store.list_asks(limit=2)
    assert [e.turn_id for e in page.items] == ["t-4", "t-3"]
    assert page.total == 5 and page.next_cursor is not None
    second = store.list_asks(cursor=page.next_cursor, limit=2)
    assert [e.turn_id for e in second.items] == ["t-2", "t-1"]
    last = store.list_asks(cursor=second.next_cursor, limit=2)
    assert [e.turn_id for e in last.items] == ["t-0"] and last.next_cursor is None


def test_list_asks_filters_by_outcome_and_total_follows_the_filter():
    store = _store()
    store.insert_ask(_entry("t-1", outcome="answered"))
    store.insert_ask(_entry("t-2", outcome="unmet", unmet_reason="no_dimension"))
    store.insert_ask(_entry("t-3", outcome="unmet", unmet_reason="no_evidence"))
    page = store.list_asks(outcome="unmet")
    assert page.total == 2 and {e.turn_id for e in page.items} == {"t-2", "t-3"}


def test_ask_counts_splits_outcomes_and_votes_and_always_carries_every_key():
    store = _store()
    empty = store.ask_counts(T0 - timedelta(days=1))
    assert empty == {"total": 0, "answered": 0, "partial": 0, "unmet": 0, "action": 0, "up": 0, "down": 0}
    store.insert_ask(_entry("t-1", outcome="answered"))
    store.insert_ask(_entry("t-2", outcome="partial"))
    store.insert_ask(_entry("t-3", outcome="unmet", unmet_reason="no_dimension"))
    store.insert_ask(_entry("t-4", outcome="action"))
    store.set_feedback("t-1", "up")
    counts = store.ask_counts(T0 - timedelta(days=1))
    assert counts["total"] == 4 and counts["answered"] == 1 and counts["partial"] == 1
    assert counts["unmet"] == 1 and counts["action"] == 1 and counts["up"] == 1 and counts["down"] == 0


def test_ask_counts_respects_the_window():
    store = _store()
    store.insert_ask(_entry("old", at=T0 - timedelta(days=30)))
    store.insert_ask(_entry("new", at=T0))
    assert store.ask_counts(T0 - timedelta(days=7))["total"] == 1


def test_unmet_clusters_rank_by_count_and_carry_one_masked_example():
    store = _store()
    for i in range(3):
        store.insert_ask(_entry(f"h-{i}", at=T0 + timedelta(minutes=i), outcome="unmet",
                                unmet_reason="no_dimension", cluster_key="unmet:no_dimension|헤더",
                                question=f"헤더별로 묶어줘 {i}"))
    store.insert_ask(_entry("o-1", outcome="unmet", unmet_reason="out_of_scope",
                            cluster_key="unmet:out_of_scope|점심", question="점심 뭐 먹지"))
    clusters = store.unmet_clusters(T0 - timedelta(days=1))
    assert [c["cluster_key"] for c in clusters] == ["unmet:no_dimension|헤더", "unmet:out_of_scope|점심"]
    assert clusters[0]["count"] == 3 and clusters[0]["reason"] == "no_dimension"
    # The example is the cluster's NEWEST question — and it is the stored (already masked) value.
    assert clusters[0]["example"] == "헤더별로 묶어줘 2"


def test_unmet_clusters_ignore_answered_rows():
    store = _store()
    store.insert_ask(_entry("t-1", outcome="answered"))
    assert store.unmet_clusters(T0 - timedelta(days=1)) == []
    assert store.count_unmet_clusters(T0 - timedelta(days=1)) == 0


def test_the_cluster_total_counts_clusters_the_top_n_did_not_show():
    """The list is a top-N with no cursor, so the total beside it has to be the window's DISTINCT
    cluster count -- `len(clusters)` would make "상위 N / 총 M" say N == M forever."""
    store = _store()
    for i in range(4):
        store.insert_ask(_entry(f"t-{i}", outcome="unmet", cluster_key=f"unmet:no_dimension|{i}"))
    assert len(store.unmet_clusters(T0 - timedelta(days=1), limit=2)) == 2
    assert store.count_unmet_clusters(T0 - timedelta(days=1)) == 4
    # ...and the window applies to the total exactly as it does to the list.
    store.insert_ask(_entry("old", at=T0 - timedelta(days=30), outcome="unmet",
                            cluster_key="unmet:no_dimension|old"))
    assert store.count_unmet_clusters(T0 - timedelta(days=1)) == 4


def test_set_feedback_overwrites_and_reports_a_missing_turn():
    store = _store()
    store.insert_ask(_entry("t-1"))
    assert store.set_feedback("t-1", "up") is True
    assert store.list_asks().items[0].feedback == "up"
    assert store.set_feedback("t-1", "down") is True
    assert store.list_asks().items[0].feedback == "down"
    assert store.set_feedback("t-nope", "up") is False


def test_delete_asks_before_removes_only_the_old_rows():
    store = _store()
    store.insert_ask(_entry("old", at=T0 - timedelta(days=400)))
    store.insert_ask(_entry("new", at=T0))
    assert store.delete_asks_before(T0 - timedelta(days=365)) == 1
    assert [e.turn_id for e in store.list_asks().items] == ["new"]


# -- routes and the turn-end hook ----------------------------------------------------------

def _app(tmp_path, config, responses):
    backend = MockAtworks(config, FIXTURES)
    agent = AtworksAgent(backend=backend, skills_dir=SKILLS, config=config, client=FakeClient(responses))
    reports = Reports(tmp_path)
    briefings = Briefings(tmp_path / "b", config)
    app = create_app(agent=agent, backend=backend,
                     scheduler=Scheduler(backend, reports, None, briefings=briefings),
                     reports=reports, briefings=briefings)
    return app, backend


@pytest.fixture
async def client(tmp_path):
    app, backend = _app(tmp_path, AtworksAgentConfig(model="m"),
                        [text_message("ok"), text_message("ok")])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        c.backend = backend
        yield c


async def _sid(client) -> str:
    return (await client.post("/api/atworks/session")).json()["session_id"]


async def test_a_chat_turn_leaves_exactly_one_ask_row(client):
    sid = await _sid(client)
    headers = {"X-Session-Id": sid}
    assert (await client.post("/api/atworks/chat", headers=headers,
                              json={"message": "실패 좀 보여줘"})).status_code == 200
    body = (await client.get("/api/atworks/ask-log", headers=headers)).json()
    assert body["total"] == 1
    row = body["items"][0]
    # No data tool ran and no card was drawn: the model answered in prose.
    assert row["outcome"] == "partial" and row["intent"] == "meta" and row["cards"] == 0
    assert row["question"] == "실패 좀 보여줘" and row["turn_id"]


async def test_two_turns_leave_two_rows_with_different_turn_ids(client):
    headers = {"X-Session-Id": await _sid(client)}
    for message in ("첫 질문", "둘째 질문"):
        await client.post("/api/atworks/chat", headers=headers, json={"message": message})
    body = (await client.get("/api/atworks/ask-log", headers=headers)).json()
    assert body["total"] == 2
    assert len({row["turn_id"] for row in body["items"]}) == 2


async def test_the_stored_question_is_masked(client):
    headers = {"X-Session-Id": await _sid(client)}
    await client.post("/api/atworks/chat", headers=headers,
                      json={"message": "900101-1234567 이 사람 실행 찾아줘"})
    row = (await client.get("/api/atworks/ask-log", headers=headers)).json()["items"][0]
    assert "900101-1234567" not in row["question"] and "***" in row["question"]


async def test_a_turn_whose_stream_raises_still_leaves_one_partial_row(tmp_path):
    # FakeClient with nothing scripted raises on the first model call; stream_turn turns that
    # into an error event, and the hook still has to fire — from the `finally`, exactly once.
    app, _ = _app(tmp_path, AtworksAgentConfig(model="m"), [])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        headers = {"X-Session-Id": await _sid(c)}
        chat = await c.post("/api/atworks/chat", headers=headers, json={"message": "무엇이든"})
        assert chat.status_code == 200 and "event: error" in chat.text
        body = (await c.get("/api/atworks/ask-log", headers=headers)).json()
        assert body["total"] == 1 and body["items"][0]["outcome"] == "partial"


async def test_a_staging_turn_is_recorded_as_an_action(tmp_path):
    app, _ = _app(tmp_path, AtworksAgentConfig(model="m"), [
        tool_calls_message(("get_api", {"api_id": "api-001"}, "tu-get")),
        tool_calls_message(("stage_rule", {"api_id": "api-001", "param": "contractNo",
                                           "kind": "required"}, "tu-stage")),
        text_message("규칙을 스테이징했습니다."),
    ])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        headers = {"X-Session-Id": await _sid(c)}
        await c.post("/api/atworks/chat", headers=headers, json={"message": "contractNo 필수 규칙"})
        row = (await c.get("/api/atworks/ask-log", headers=headers)).json()["items"][0]
        assert row["outcome"] == "action" and row["intent"] == "action"
        # get_api + stage_rule + whatever presentation the turn added.
        assert row["tool_calls"] >= 2 and row["cluster_key"] == "action:action"


async def test_ask_log_route_filters_by_outcome_and_rejects_a_bad_cursor(client):
    headers = {"X-Session-Id": await _sid(client)}
    await client.post("/api/atworks/chat", headers=headers, json={"message": "질문"})
    assert (await client.get("/api/atworks/ask-log?outcome=answered", headers=headers)).json()["total"] == 0
    assert (await client.get("/api/atworks/ask-log?outcome=partial", headers=headers)).json()["total"] == 1
    bad = await client.get("/api/atworks/ask-log?cursor=zzz", headers=headers)
    assert bad.status_code == 400
    assert (await client.get("/api/atworks/ask-log?outcome=nonsense", headers=headers)).status_code == 422


async def test_growth_summary_counts_the_window(client):
    headers = {"X-Session-Id": await _sid(client)}
    await client.post("/api/atworks/chat", headers=headers, json={"message": "질문"})
    body = (await client.get("/api/atworks/growth/summary?days=7", headers=headers)).json()
    assert body["asks_total"] == 1 and body["partial"] == 1 and body["answered"] == 0
    assert body["window_days"] == 7 and body["new_terms"] == 0 and body["new_saved"] == 0
    assert body["unmet_clusters"] == [] and body["unmet_clusters_total"] == 0
    # The four outcomes are all on the wire: three tiles whose sum falls short of `asks_total`
    # with no fourth number to explain the gap is what the review found.
    assert body["action"] == 0
    assert body["answered"] + body["partial"] + body["unmet"] + body["action"] == body["asks_total"]


async def test_the_ask_log_wire_carries_no_session_id(client):
    """`/ask-log` is a TEAM read -- every operator sees every row. A live session id in that
    payload is a value another tab can put straight into `X-Session-Id`. The ledger keeps it."""
    headers = {"X-Session-Id": await _sid(client)}
    await client.post("/api/atworks/chat", headers=headers, json={"message": "질문"})
    row = (await client.get("/api/atworks/ask-log", headers=headers)).json()["items"][0]
    assert "session_id" not in row
    assert row["operator"] and row["turn_id"]


async def test_both_growth_routes_404_when_the_feature_is_off(tmp_path):
    app, _ = _app(tmp_path, AtworksAgentConfig(model="m", enable_growth=False), [text_message("ok")])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        headers = {"X-Session-Id": await _sid(c)}
        assert (await c.get("/api/atworks/ask-log", headers=headers)).status_code == 404
        assert (await c.get("/api/atworks/growth/summary", headers=headers)).status_code == 404


async def test_no_row_is_written_when_growth_is_off(tmp_path):
    config = AtworksAgentConfig(model="m", enable_growth=False)
    app, backend = _app(tmp_path, config, [text_message("ok")])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        headers = {"X-Session-Id": await _sid(c)}
        await c.post("/api/atworks/chat", headers=headers, json={"message": "질문"})
    assert backend.store.list_asks().total == 0


async def test_the_ask_log_routes_need_a_session(client):
    assert (await client.get("/api/atworks/ask-log")).status_code == 401
    assert (await client.get("/api/atworks/growth/summary")).status_code == 401
