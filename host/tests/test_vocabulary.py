"""조직 공용 어휘 end to end (self-growth spec §7): 사이드카 테이블, 쓰기 필터, 세 라우트,
그리고 확정된 용어가 **다른 오퍼레이터의** 턴 컨텍스트에 실리는지.

이 파일이 지키는 선은 셋이다. (1) 필터가 막은 값은 어느 테이블에도 남지 않는다. (2) pending은
제안한 세션 밖으로 나가지 않는다 -- 확정만 컨텍스트에 들어간다(spec §2 조항 4). (3) 확정·거부·삭제는
사람의 클릭에서만 일어나고 감사 2행을 남긴다.
"""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from commerce_common.testing import FakeClient, text_message
from httpx import ASGITransport, AsyncClient

from atworks_agent import (
    AtworksAgentConfig,
    AtworksSessionContext,
    QueryFilters,
    VocabularyEntry,
)
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


def _session(operator: str = "minseong", session_id: str = "s-1") -> AtworksSessionContext:
    return AtworksSessionContext(session_id=session_id, project_id="mes-demo",
                                 operator=operator, now=T0)


def _entry(term: str = "결제 계열", *, prefix: str = "/v1/payment", **fields) -> VocabularyEntry:
    return VocabularyEntry(term=term, fragment=QueryFilters(path_prefix=prefix),
                           proposed_by="minseong", proposed_at=T0, **fields)


def _backend(config: AtworksAgentConfig | None = None) -> MockAtworks:
    return MockAtworks(config or AtworksAgentConfig(model="m"), FIXTURES, store=Store(":memory:"))


# -- Store: 사이드카 --------------------------------------------------------------------------

def test_propose_never_overwrites_an_existing_row():
    # 확정된 항목이 새 제안으로 pending이 되면 안 되고, 거부가 남긴 쿨다운도 지워지면 안 된다.
    store = Store(":memory:")
    store.propose_vocabulary(_entry())
    store.set_vocabulary_status("결제 계열", "confirmed", by="jihoon", now=T0)
    again = store.propose_vocabulary(_entry(prefix="/v1/other"))
    assert again.status == "confirmed" and again.fragment.path_prefix == "/v1/payment"
    assert store.list_vocabulary().total == 1


def test_confirm_counts_every_operator_that_agrees():
    store = Store(":memory:")
    store.propose_vocabulary(_entry())
    store.set_vocabulary_status("결제 계열", "confirmed", by="minseong", now=T0)
    second = store.set_vocabulary_status("결제 계열", "confirmed", by="jihoon", now=T0)
    assert second.confirmations == 2 and second.confirmed_by == "jihoon"


def test_reject_stamps_a_cooldown_and_keeps_the_row():
    store = Store(":memory:")
    store.propose_vocabulary(_entry())
    rejected = store.set_vocabulary_status("결제 계열", "rejected", by="jihoon", now=T0, cooldown_days=30)
    assert rejected.status == "rejected" and rejected.rejections == 1
    assert rejected.cooldown_until == T0 + timedelta(days=30)
    assert store.get_vocabulary("결제 계열") is not None


def test_list_vocabulary_pages_newest_first_and_filters_by_status():
    store = Store(":memory:")
    for i in range(5):
        store.propose_vocabulary(_entry(f"term{i}", prefix=f"/v{i}").model_copy(
            update={"proposed_at": T0 + timedelta(minutes=i)}))
    store.set_vocabulary_status("term0", "confirmed", by="m", now=T0)
    page = store.list_vocabulary(limit=2)
    assert [e.term for e in page.items] == ["term4", "term3"]
    assert page.total == 5 and page.next_cursor is not None
    assert [e.term for e in store.list_vocabulary(cursor=page.next_cursor, limit=2).items] == \
           ["term2", "term1"]
    confirmed = store.list_vocabulary(status="confirmed")
    assert confirmed.total == 1 and confirmed.items[0].term == "term0"


def test_auto_demotion_needs_three_rejections_and_more_than_the_confirmations():
    store = Store(":memory:")
    store.propose_vocabulary(_entry())
    store.set_vocabulary_status("결제 계열", "confirmed", by="m", now=T0)   # confirmations = 1
    assert store.note_vocabulary_rejection(["결제 계열"] * 2, auto_demote_rejections=3) == []
    assert store.get_vocabulary("결제 계열").status == "confirmed"
    # 세 번째 거부에서 rejections(3) >= 3 이고 confirmations(1)보다 크다 → pending으로 강등.
    assert store.note_vocabulary_rejection(["결제 계열"], auto_demote_rejections=3) == ["결제 계열"]
    demoted = store.get_vocabulary("결제 계열")
    assert demoted.status == "pending" and demoted.rejections == 3 and demoted.confirmations == 1
    assert store.confirmed_vocabulary() == []


def test_a_widely_confirmed_term_is_not_demoted_by_a_few_rejections():
    store = Store(":memory:")
    store.propose_vocabulary(_entry())
    for operator in ("a", "b", "c", "d"):
        store.set_vocabulary_status("결제 계열", "confirmed", by=operator, now=T0)
    assert store.note_vocabulary_rejection(["결제 계열"] * 3, auto_demote_rejections=3) == []
    assert store.get_vocabulary("결제 계열").status == "confirmed"


def test_uses_are_counted_and_new_terms_are_a_count_not_a_list():
    store = Store(":memory:")
    store.propose_vocabulary(_entry())
    store.set_vocabulary_status("결제 계열", "confirmed", by="m", now=T0)
    store.bump_vocabulary_uses(["결제 계열", "없는 용어"])
    assert store.get_vocabulary("결제 계열").uses == 1
    assert store.count_vocabulary_since(T0 - timedelta(days=1)) == 1
    assert store.count_vocabulary_since(T0 + timedelta(days=1)) == 0


# -- 백엔드: 쓰기 필터와 쿨다운 -----------------------------------------------------------------

async def test_a_proposal_stores_both_the_sidecar_row_and_the_fact():
    backend = _backend()
    entry = await backend.propose_alias(_session(), "  결제 계열 ", QueryFilters(path_prefix="/v1/payment"))
    assert entry is not None and entry.term == "결제 계열" and entry.status == "pending"
    (fact,) = await backend.memory_store.get_facts("mes-demo")
    assert fact.key == "결제_계열" and json.loads(fact.value) == {"path_prefix": "/v1/payment"}
    assert fact.category.value == "context"


@pytest.mark.parametrize(("term", "fragment"), [
    ("담당자 메일", QueryFilters(path_contains=["hong@example.com"])),
    ("주민번호 API", QueryFilters(path_contains=["900101-1234567"])),
    ("hong@example.com", QueryFilters(path_prefix="/v1/payment")),
])
async def test_a_pii_shaped_term_or_fragment_stores_nothing_anywhere(term, fragment):
    # MemoryWriteFilter가 막으면 fact도, 사이드카 행도 생기지 않는다 -- 반쯤 저장된 상태가 없다.
    backend = _backend()
    assert await backend.propose_alias(_session(), term, fragment) is None
    assert await backend.memory_store.get_facts("mes-demo") == []
    assert (await backend.list_vocabulary(_session())).total == 0


async def test_a_rejected_term_cannot_be_re_proposed_until_the_cooldown_passes():
    backend = _backend()
    await backend.propose_alias(_session(), "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    await backend.reject_alias(_session("jihoon"), "결제 계열")
    assert await backend.propose_alias(_session(), "결제 계열", QueryFilters(path_prefix="/v1/pay")) is None
    # 쿨다운이 지난 뒤의 세션(= 더 나중의 now)에서는 다시 제안이 통과한다... 는 아니다: 행은
    # 남아 있고 제안은 저장된 행을 그대로 돌려준다. 상태가 조용히 pending으로 되돌아가지 않는다.
    later = AtworksSessionContext(session_id="s-2", project_id="mes-demo", operator="minseong",
                                  now=T0 + timedelta(days=31))
    again = await backend.propose_alias(later, "결제 계열", QueryFilters(path_prefix="/v1/pay"))
    assert again is not None and again.status == "rejected"


async def test_confirmed_vocabulary_is_cached_but_invalidated_by_every_write():
    backend = _backend()
    session = _session()
    assert await backend.confirmed_vocabulary(session) == []
    await backend.propose_alias(session, "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    assert await backend.confirmed_vocabulary(session) == []      # pending은 주입 대상이 아니다
    await backend.confirm_alias(_session("jihoon"), "결제 계열")
    assert [e.term for e in await backend.confirmed_vocabulary(session)] == ["결제 계열"]
    await backend.delete_alias(session, "결제 계열")
    assert await backend.confirmed_vocabulary(session) == []


async def test_delete_removes_the_fact_as_well_as_the_row():
    backend = _backend()
    session = _session()
    await backend.propose_alias(session, "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    assert await backend.delete_alias(session, "결제 계열") is not None
    assert await backend.memory_store.get_facts("mes-demo") == []
    assert await backend.delete_alias(session, "결제 계열") is None


async def test_note_vocabulary_use_counts_uses_and_demotes_on_repeated_thumbs_down():
    backend = _backend()
    session = _session()
    await backend.propose_alias(session, "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    await backend.confirm_alias(session, "결제 계열")
    await backend.note_vocabulary_use(session, ["결제 계열"])
    assert backend.store.get_vocabulary("결제 계열").uses == 1
    for _ in range(3):
        await backend.note_vocabulary_use(session, ["결제 계열"], rejected=True)
    assert backend.store.get_vocabulary("결제 계열").status == "pending"


# -- 라우트 ----------------------------------------------------------------------------------

def _app(tmp_path, config, responses):
    backend = _backend(config)
    agent = AtworksAgent(backend=backend, skills_dir=SKILLS, config=config, client=FakeClient(responses))
    reports = Reports(tmp_path)
    briefings = Briefings(tmp_path / "b", config)
    app = create_app(agent=agent, backend=backend,
                     scheduler=Scheduler(backend, reports, None, briefings=briefings),
                     reports=reports, briefings=briefings)
    return app, backend, agent


@pytest.fixture
async def client(tmp_path):
    app, backend, _agent = _app(tmp_path, AtworksAgentConfig(model="m"), [text_message("ok")] * 4)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        c.backend = backend
        yield c


async def _sid(client, operator: str | None = None) -> str:
    payload = {"operator_id": operator} if operator else {}
    return (await client.post("/api/atworks/session", json=payload)).json()["session_id"]


async def test_vocabulary_route_answers_the_paged_envelope(client):
    headers = {"X-Session-Id": await _sid(client)}
    await client.backend.propose_alias(_session(), "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    body = (await client.get("/api/atworks/vocabulary", headers=headers)).json()
    assert set(body) == {"items", "next_cursor", "total"} and body["total"] == 1
    row = body["items"][0]
    assert row["term"] == "결제 계열" and row["status"] == "pending"
    # 요약은 서버가 카탈로그 라벨로 만든다 -- 포털이 필터 이름을 번역하지 않는다.
    assert row["fragment_summary"] == "경로 접두사 /v1/payment"
    assert (await client.get("/api/atworks/vocabulary?status=confirmed",
                             headers=headers)).json()["total"] == 0


async def test_confirm_and_reject_write_an_audit_pair_each(client):
    headers = {"X-Session-Id": await _sid(client)}
    await client.backend.propose_alias(_session(), "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    confirmed = await client.post("/api/atworks/vocabulary/결제 계열/confirm", headers=headers)
    assert confirmed.status_code == 200
    assert confirmed.json()["entry"]["status"] == "confirmed"
    assert confirmed.json()["entry"]["confirmed_by"] == "minseong"
    rows = (await client.get("/api/atworks/audit", headers=headers)).json()["items"]
    actions = [r["action"] for r in rows if r["target_kind"] == "vocabulary"]
    assert set(actions) == {"vocabulary_confirm", "vocabulary_confirm:ok"}
    assert all(r["target_id"] == "결제 계열" for r in rows if r["target_kind"] == "vocabulary")

    rejected = await client.post("/api/atworks/vocabulary/결제 계열/reject", headers=headers)
    assert rejected.json()["entry"]["status"] == "rejected"
    actions = [r["action"] for r in (await client.get("/api/atworks/audit", headers=headers)).json()["items"]
               if r["target_kind"] == "vocabulary"]
    assert {"vocabulary_reject", "vocabulary_reject:ok"} <= set(actions)


async def test_delete_route_removes_the_term(client):
    headers = {"X-Session-Id": await _sid(client)}
    await client.backend.propose_alias(_session(), "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    assert (await client.post("/api/atworks/vocabulary/결제 계열/delete", headers=headers)).status_code == 200
    assert (await client.get("/api/atworks/vocabulary", headers=headers)).json()["total"] == 0


async def test_acting_on_an_unknown_term_is_a_404(client):
    headers = {"X-Session-Id": await _sid(client)}
    assert (await client.post("/api/atworks/vocabulary/없는 용어/confirm",
                              headers=headers)).status_code == 404


async def test_the_vocabulary_routes_404_when_growth_is_off(tmp_path):
    app, backend, _agent = _app(tmp_path, AtworksAgentConfig(model="m", enable_growth=False), [])
    await backend.propose_alias(_session(), "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        headers = {"X-Session-Id": await _sid(c)}
        assert (await c.get("/api/atworks/vocabulary", headers=headers)).status_code == 404
        assert (await c.post("/api/atworks/vocabulary/결제 계열/confirm",
                             headers=headers)).status_code == 404


async def test_growth_summary_counts_the_new_terms(client):
    headers = {"X-Session-Id": await _sid(client)}
    await client.backend.propose_alias(_session(), "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    await client.post("/api/atworks/vocabulary/결제 계열/confirm", headers=headers)
    assert (await client.get("/api/atworks/growth/summary", headers=headers)).json()["new_terms"] == 1


# -- /chat: 주입 --------------------------------------------------------------------------

def _system_text(agent) -> str:
    return json.dumps(agent.client.calls[0]["system"], ensure_ascii=False)


async def test_a_confirmed_term_reaches_another_operator_s_turn(tmp_path):
    """한 사람이 확인하면 팀이 쓴다 — 이 기능의 전부. 확인은 minseong이, 질문은 jihye가 한다."""
    app, backend, agent = _app(tmp_path, AtworksAgentConfig(model="m"), [text_message("네.")])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        owner = {"X-Session-Id": await _sid(c, "minseong")}
        await backend.propose_alias(_session(), "결제 계열", QueryFilters(path_prefix="/v1/payment"))
        await c.post("/api/atworks/vocabulary/결제 계열/confirm", headers=owner)

        other = {"X-Session-Id": await _sid(c, "jihoon")}
        assert (await c.post("/api/atworks/chat", headers=other,
                             json={"message": "결제 계열 실패 보여줘"})).status_code == 200
    system = _system_text(agent)
    assert "결제 계열" in system and "경로 접두사 /v1/payment" in system
    # 모델이 그대로 쓸 조각도 같이 실린다(json.dumps로 한 번 더 감싼 문자열이라 따옴표가 이스케이프된다).
    assert "path_prefix" in system
    # 쓰인 만큼 세어진다.
    assert backend.store.get_vocabulary("결제 계열").uses == 1


async def test_a_pending_term_never_reaches_another_session_s_context(tmp_path):
    app, backend, agent = _app(tmp_path, AtworksAgentConfig(model="m"), [text_message("네.")])
    await backend.propose_alias(_session(), "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        headers = {"X-Session-Id": await _sid(c, "jihoon")}
        await c.post("/api/atworks/chat", headers=headers, json={"message": "결제 계열 실패 보여줘"})
    system = _system_text(agent)
    assert "경로 접두사 /v1/payment" not in system
    assert backend.store.get_vocabulary("결제 계열").uses == 0


async def test_a_turn_that_names_no_confirmed_term_carries_no_vocabulary_block(tmp_path):
    app, backend, agent = _app(tmp_path, AtworksAgentConfig(model="m"), [text_message("네.")])
    await backend.propose_alias(_session(), "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    await backend.confirm_alias(_session(), "결제 계열")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        headers = {"X-Session-Id": await _sid(c)}
        await c.post("/api/atworks/chat", headers=headers, json={"message": "계약 실패 보여줘"})
    assert "vocabulary" not in _system_text(agent)
