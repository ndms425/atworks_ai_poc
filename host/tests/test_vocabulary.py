"""조직 공용 어휘 end to end (self-growth spec §7): 사이드카 테이블, 쓰기 필터, 세 라우트,
그리고 확정된 용어가 **다른 오퍼레이터의** 턴 컨텍스트에 실리는지.

이 파일이 지키는 선은 셋이다. (1) 필터가 막은 값은 어느 테이블에도 남지 않는다. (2) pending은
제안한 세션 밖으로 나가지 않는다 -- 확정만 컨텍스트에 들어간다(spec §2 조항 4). (3) 확정·거부·삭제는
사람의 클릭에서만 일어나고 감사 2행을 남긴다.
"""
import json
import random
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
    _, inserted = store.propose_vocabulary(_entry())
    assert inserted is True
    store.set_vocabulary_status("결제 계열", "confirmed", by="jihoon", now=T0)
    again, inserted = store.propose_vocabulary(_entry(prefix="/v1/other"))
    # `inserted`가 없으면 재제안과 새 제안이 호출자에게 같은 모양이다 -- 그게 카드가 이미 답한
    # 질문을 다시 묻고 fact가 미승인 조각으로 덮이던 자리다.
    assert inserted is False
    assert again.status == "confirmed" and again.fragment.path_prefix == "/v1/payment"
    assert store.list_vocabulary().total == 1


def test_repropose_revives_a_cooled_row_but_keeps_its_history():
    store = Store(":memory:")
    store.propose_vocabulary(_entry())
    store.set_vocabulary_status("결제 계열", "rejected", by="jihoon", now=T0, cooldown_days=30)
    revived = store.repropose_vocabulary(
        _entry(prefix="/v1/pay").model_copy(update={"proposed_by": "jihye"}))
    assert revived.status == "pending" and revived.cooldown_until is None
    assert revived.fragment.path_prefix == "/v1/pay" and revived.proposed_by == "jihye"
    # 이력은 남는다: Growth 뷰가 "이 용어는 한 번 거부됐다"를 계속 보여줘야 한다.
    assert revived.rejections == 1
    assert store.repropose_vocabulary(_entry("없는 용어")) is None


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


def _reject(store: Store, term: str, operator: str, *, threshold: int = 3) -> list[str]:
    return store.note_vocabulary_rejection([term], operator_id=operator, now=T0,
                                           auto_demote_rejections=threshold)


def test_auto_demotion_needs_three_distinct_operators_and_more_than_the_confirmations():
    store = Store(":memory:")
    store.propose_vocabulary(_entry())
    store.set_vocabulary_status("결제 계열", "confirmed", by="m", now=T0)   # confirmations = 1
    for operator in ("a", "b"):
        assert _reject(store, "결제 계열", operator) == []
    assert store.get_vocabulary("결제 계열").status == "confirmed"
    # 세 번째 **다른** 운영자에서 rejections(3) >= 3 이고 confirmations(1)보다 크다 → 강등.
    assert _reject(store, "결제 계열", "c") == ["결제 계열"]
    demoted = store.get_vocabulary("결제 계열")
    assert demoted.status == "pending" and demoted.rejections == 3 and demoted.confirmations == 1
    assert store.confirmed_vocabulary() == []


def test_one_operator_voting_again_and_again_never_reaches_the_threshold():
    """멱등성이 호출자의 조건문이 아니라 `(term, operator_id)` 기본키의 성질이다: 한 사람이 몇
    번을 눌러도 행은 하나라, 팀이 확정한 용어를 혼자 강등시킬 방법이 없다."""
    store = Store(":memory:")
    store.propose_vocabulary(_entry())
    store.set_vocabulary_status("결제 계열", "confirmed", by="m", now=T0)
    for _ in range(9):
        assert _reject(store, "결제 계열", "a") == []
    entry = store.get_vocabulary("결제 계열")
    assert entry.status == "confirmed" and entry.rejections == 1


def test_a_legacy_rejection_count_is_a_floor_the_new_table_cannot_undo():
    """새 테이블이 생기기 전에 쓰인 저장소: `vocabulary.rejections`는 3인데 표 행은 0이다.
    COUNT만 읽으면 이미 강등된 용어가 조용히 0으로 되살아난다 -- 유효 개수는
    MAX(레거시 컬럼, 행 수)이고, 새 표는 그 바닥 위에 쌓인다."""
    store = Store(":memory:")
    store.propose_vocabulary(_entry())
    store.set_vocabulary_status("결제 계열", "confirmed", by="m", now=T0)
    store._conn.execute("UPDATE vocabulary SET rejections = 3 WHERE term = '결제 계열'")
    assert store.get_vocabulary("결제 계열").rejections == 3
    # 새 표 한 장으로도 유효 개수는 3(레거시 바닥) -- 0으로 내려가지 않고, 그래서 강등된다.
    assert _reject(store, "결제 계열", "a") == ["결제 계열"]
    entry = store.get_vocabulary("결제 계열")
    assert entry.status == "pending" and entry.rejections == 3


def test_a_widely_confirmed_term_is_not_demoted_by_a_few_rejections():
    store = Store(":memory:")
    store.propose_vocabulary(_entry())
    for operator in ("a", "b", "c", "d"):
        store.set_vocabulary_status("결제 계열", "confirmed", by=operator, now=T0)
    for operator in ("a", "b", "c"):
        assert _reject(store, "결제 계열", operator) == []
    assert store.get_vocabulary("결제 계열").status == "confirmed"


def test_deleting_a_term_forgets_the_votes_too():
    """`reject`는 행을 남기지만(쿨다운이 곧 거부의 기억) `delete`는 용어를 통째로 잊는다. 표가
    남으면 같은 이름의 새 제안이 아무도 낸 적 없는 거부를 물려받는다."""
    store = Store(":memory:")
    store.propose_vocabulary(_entry())
    store.set_vocabulary_status("결제 계열", "confirmed", by="m", now=T0)
    for operator in ("a", "b"):
        _reject(store, "결제 계열", operator)
    store.delete_vocabulary("결제 계열")
    store.propose_vocabulary(_entry())
    store.set_vocabulary_status("결제 계열", "confirmed", by="m", now=T0)
    assert store.get_vocabulary("결제 계열").rejections == 0
    assert _reject(store, "결제 계열", "c") == []


def test_a_vote_on_a_term_the_sidecar_does_not_have_leaves_nothing_behind():
    store = Store(":memory:")
    assert _reject(store, "없는 용어", "a") == []
    assert store._conn.execute(
        "SELECT COUNT(*) FROM vocabulary_rejection").fetchone()[0] == 0


def test_uses_are_counted_and_new_terms_are_a_count_not_a_list():
    store = Store(":memory:")
    store.propose_vocabulary(_entry())
    store.set_vocabulary_status("결제 계열", "confirmed", by="m", now=T0)
    store.bump_vocabulary_uses(["결제 계열", "없는 용어"])
    assert store.get_vocabulary("결제 계열").uses == 1
    assert store.count_vocabulary_since(T0 - timedelta(days=1)) == 1
    assert store.count_vocabulary_since(T0 + timedelta(days=1)) == 0


# -- 백엔드: 쓰기 필터, 쿨다운, 재제안 -----------------------------------------------------------

async def test_a_proposal_stores_the_sidecar_row_and_the_fact_waits_for_the_click():
    # `memory_facts`는 **확정된** term만 담는다: 제안은 사이드카 1행뿐이고, fact는 사람이 [예]를
    # 누른 순간 쓰인다. 그래서 `get_facts`가 돌려주는 집합이 곧 confirmed 집합이다.
    backend = _backend()
    proposal = await backend.propose_alias(_session(), "  결제 계열 ",
                                           QueryFilters(path_prefix="/v1/payment"))
    assert proposal.outcome == "proposed"
    assert proposal.entry.term == "결제 계열" and proposal.entry.status == "pending"
    assert await backend.memory_store.get_facts("mes-demo") == []
    await backend.confirm_alias(_session("jihoon"), "결제 계열")
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
    assert (await backend.propose_alias(_session(), term, fragment)).outcome == "refused"
    assert await backend.memory_store.get_facts("mes-demo") == []
    assert (await backend.list_vocabulary(_session())).total == 0


@pytest.mark.parametrize("fragment", [
    QueryFilters(path_prefix="/v1/payment", executed_by=["jihoon"]),
    QueryFilters(executed_by=["jihoon"]),
    QueryFilters(path_prefix="/v1/payment", scope_operator="jihoon"),
    QueryFilters(path_prefix="/v1/payment", window_days=30),
])
async def test_an_alias_can_never_bind_an_operator_or_a_window(fragment):
    """확정된 별칭은 팀 전체의 컨텍스트에 실린다 -- 거기 오퍼레이터 id가 굳으면 한 번의 클릭이
    다른 사람의 질문을 조용히 남의 id로 좁힌다. 실행기의 툴 입력 검사와 **별개로** 저장하는
    쪽에도 문이 있어야 하므로, 백엔드를 직접 불러도 어느 테이블에도 아무것도 남지 않는다."""
    backend = _backend()
    assert (await backend.propose_alias(_session(), "지훈 계열", fragment)).outcome == "refused"
    assert (await backend.list_vocabulary(_session())).total == 0
    assert await backend.memory_store.get_facts("mes-demo") == []


async def test_re_proposing_a_confirmed_term_changes_nothing_and_asks_nothing():
    backend = _backend()
    await backend.propose_alias(_session(), "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    await backend.confirm_alias(_session("jihoon"), "결제 계열")
    again = await backend.propose_alias(_session(), "결제 계열", QueryFilters(path_prefix="/v1/other"))
    # 카드가 이미 답한 질문을 다시 묻지 않는다: 새 pending 행이 아니라 저장된 행이 돌아온다.
    assert again.outcome == "existing" and again.entry.status == "confirmed"
    assert again.entry.fragment.path_prefix == "/v1/payment"
    # 그리고 fact가 이번 턴의 미승인 조각으로 덮이지 않는다 -- 두 자리가 갈라지지 않는다.
    (fact,) = await backend.memory_store.get_facts("mes-demo")
    assert json.loads(fact.value) == {"path_prefix": "/v1/payment"}
    assert (await backend.list_vocabulary(_session())).total == 1


async def test_re_proposing_a_pending_term_does_not_duplicate_it():
    backend = _backend()
    await backend.propose_alias(_session(), "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    again = await backend.propose_alias(_session("jihye"), "결제 계열",
                                        QueryFilters(path_prefix="/v1/other"))
    assert again.outcome == "existing" and again.entry.proposed_by == "minseong"
    assert (await backend.list_vocabulary(_session())).total == 1
    assert await backend.memory_store.get_facts("mes-demo") == []


async def test_a_rejected_term_is_refused_in_cooldown_and_proposable_after_it():
    backend = _backend()
    await backend.propose_alias(_session(), "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    await backend.reject_alias(_session("jihoon"), "결제 계열")
    inside = await backend.propose_alias(_session(), "결제 계열", QueryFilters(path_prefix="/v1/pay"))
    assert inside.outcome == "existing" and inside.entry.status == "rejected"
    assert inside.entry.fragment.path_prefix == "/v1/payment"   # 저장된 뜻은 그대로다
    # 끝이 없는 쿨다운은 쿨다운이 아니다: 지나고 나면 새 제안이 fresh pending을 만든다.
    later = AtworksSessionContext(session_id="s-2", project_id="mes-demo", operator="jihye",
                                  now=T0 + timedelta(days=31))
    again = await backend.propose_alias(later, "결제 계열", QueryFilters(path_prefix="/v1/pay"))
    assert again.outcome == "proposed" and again.entry.status == "pending"
    assert again.entry.fragment.path_prefix == "/v1/pay" and again.entry.proposed_by == "jihye"
    assert again.entry.rejections == 1                          # 이력은 남는다
    assert (await backend.list_vocabulary(_session())).total == 1


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


async def test_counting_a_use_keeps_the_cache_and_a_demotion_drops_it():
    # `uses + 1`은 confirmed 집합을 바꾸지 않는다 -- 그때마다 캐시를 버리면 어휘가 실린 모든 턴이
    # 캐시 미스가 되어 캐시가 하는 일이 없어진다. 강등은 집합을 바꾸므로 버린다.
    backend = _backend()
    session = _session()
    await backend.propose_alias(session, "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    await backend.confirm_alias(session, "결제 계열")
    await backend.confirmed_vocabulary(session)                   # 캐시를 채운다
    await backend.note_vocabulary_use(session, ["결제 계열"])
    assert backend._confirmed_cache is not None
    for operator in ("a", "b"):
        await backend.note_vocabulary_use(_session(operator), ["결제 계열"], rejected=True)
    assert backend._confirmed_cache is not None                   # 강등이 없었던 👎는 그대로
    await backend.note_vocabulary_use(_session("c"), ["결제 계열"], rejected=True)
    assert backend._confirmed_cache is None


async def test_delete_removes_the_fact_as_well_as_the_row():
    backend = _backend()
    session = _session()
    await backend.propose_alias(session, "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    await backend.confirm_alias(session, "결제 계열")
    assert await backend.delete_alias(session, "결제 계열") is not None
    assert await backend.memory_store.get_facts("mes-demo") == []
    assert await backend.delete_alias(session, "결제 계열") is None


async def test_note_vocabulary_use_counts_uses_and_demotes_on_three_operators_thumbs_down():
    backend = _backend()
    session = _session()
    await backend.propose_alias(session, "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    await backend.confirm_alias(session, "결제 계열")
    await backend.note_vocabulary_use(session, ["결제 계열"])
    assert backend.store.get_vocabulary("결제 계열").uses == 1
    # 한 사람이 세 번 눌러도 표는 하나다 -- 세션의 operator가 곧 표의 주인이다.
    for _ in range(3):
        await backend.note_vocabulary_use(session, ["결제 계열"], rejected=True)
    assert backend.store.get_vocabulary("결제 계열").status == "confirmed"
    assert await backend.memory_store.get_facts("mes-demo") != []
    for operator in ("jihoon", "sora"):
        await backend.note_vocabulary_use(_session(operator), ["결제 계열"], rejected=True)
    assert backend.store.get_vocabulary("결제 계열").status == "pending"
    # 강등도 확정을 푸는 일이라 fact가 남지 않는다 -- 아래 속성 테스트의 불변식과 같은 규칙.
    assert await backend.memory_store.get_facts("mes-demo") == []


async def test_memory_facts_are_exactly_the_confirmed_terms():
    """불변식 하나를 무작위 순서로 흔든다: `memory_facts`의 key 집합 == confirmed인 term의 집합.
    제안은 아무것도 넣지 않고, 확정이 넣고, 거부·삭제·강등이 민다 -- 그래서 `search_facts`가
    돌려주는 것이 정의상 팀이 승인한 뜻뿐이다."""
    rng = random.Random(20260908)
    backend = _backend()
    session = _session()
    terms = [f"용어 {i}" for i in range(4)]
    demotions = 0
    for step in range(240):
        term = rng.choice(terms)
        # 균등 확률이 아니다: 강등은 확정과 삭제 사이에서 **서로 다른 세 사람**의 👎가 몰려야
        # 일어나므로, 다섯 갈래를 고르게 뽑으면 몇백 스텝을 돌려도 그 갈래가 한 번도 끝까지 가지
        # 않고 불변식의 절반이 검사되지 않은 채 통과한다(아래 `demotions > 0`이 그걸 잡는다).
        action = rng.choices(["propose", "confirm", "reject", "delete", "demote"],
                             weights=[3, 2, 1, 1, 4])[0]
        now = T0 + timedelta(days=40 * step)      # 쿨다운이 지나는 시간축(재제안도 일어난다)
        # 셋이다: 강등은 서로 다른 운영자 3명을 요구하므로, 둘만 흔들면 `demote` 갈래가 한 번도
        # 실제 강등에 이르지 못하고 불변식의 절반이 검사되지 않는다.
        sess = AtworksSessionContext(session_id="s", project_id="mes-demo",
                                     operator=rng.choice(["minseong", "jihoon", "sora"]), now=now)
        if action == "propose":
            await backend.propose_alias(sess, term, QueryFilters(path_prefix=f"/v1/{step}"))
        elif action == "confirm":
            await backend.confirm_alias(sess, term)
        elif action == "reject":
            await backend.reject_alias(sess, term)
        elif action == "delete":
            await backend.delete_alias(sess, term)
        else:
            demotions += len(await backend.note_vocabulary_use(sess, [term], rejected=True))
        confirmed = {e.term.replace(" ", "_") for e in await backend.confirmed_vocabulary(session)}
        stored = {f.key for f in await backend.memory_store.get_facts("mes-demo")}
        assert stored == confirmed, f"step {step}: {action} {term!r}"
    # 시퀀스가 실제로 두 상태를 다 지났는지(빈 집합만 보고 통과한 게 아닌지) 확인한다.
    assert backend.store.list_vocabulary().total > 0
    # 그리고 강등 갈래가 실제로 강등에 이르렀는지도 -- 서로 다른 운영자 3명이 필요해진 뒤로는
    # 이게 0이면 불변식의 절반(강등이 fact를 민다)이 검사되지 않은 채 통과한다.
    assert demotions > 0


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


async def test_acting_on_an_unknown_term_is_a_404_recorded_as_blocked(client):
    # 404는 `growth_action`의 try 안에서 난다: 밖에서 던지면 감사 로그에 `:ok`가 먼저 찍히고
    # 아무 일도 일어나지 않은 클릭이 성공으로 남는다.
    headers = {"X-Session-Id": await _sid(client)}
    assert (await client.post("/api/atworks/vocabulary/없는 용어/confirm",
                              headers=headers)).status_code == 404
    rows = (await client.get("/api/atworks/audit", headers=headers)).json()["items"]
    actions = [r["action"] for r in rows if r["target_kind"] == "vocabulary"]
    assert set(actions) == {"vocabulary_confirm", "vocabulary_confirm:blocked"}


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


# -- 게이트와 인접 라우트 -----------------------------------------------------------------------

def test_propose_alias_is_absent_when_either_gate_is_off():
    """두 게이트 아래 있다: 질의 도구가 꺼져도, Growth 뷰가 꺼져도 이 도구는 없다. 확인 표면
    (카드 푸터·Growth 뷰·세 라우트)이 전부 `enable_growth` 뒤에 있으므로, 그것만 꺼 두고 도구를
    남기면 모델이 아무도 확인할 수 없는 원장에 제안을 쌓는다."""
    assert "propose_alias" not in AtworksAgentConfig(model="m").absent_tools()
    assert "propose_alias" in AtworksAgentConfig(model="m", enable_growth=False).absent_tools()
    assert "propose_alias" in AtworksAgentConfig(model="m", enable_query_runs=False).absent_tools()


async def test_delete_memory_is_inert_and_touches_no_vocabulary(client):
    """`DELETE /memory`는 자리표시다(web-shared가 찾으므로 라우트는 있어야 한다). 어휘를 지우는
    자리는 Growth 뷰의 삭제고, 거기엔 감사 2행이 있다 -- 여기서 지우면 한 사람의 "내 기억
    지우기"가 팀 전체의 어휘를 증적 없이 날린다."""
    headers = {"X-Session-Id": await _sid(client)}
    await client.backend.propose_alias(_session(), "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    await client.post("/api/atworks/vocabulary/결제 계열/confirm", headers=headers)
    assert (await client.request("DELETE", "/api/atworks/memory", headers=headers)).json() == {"ok": True}
    assert (await client.get("/api/atworks/vocabulary", headers=headers)).json()["total"] == 1
    assert len(await client.backend.memory_store.get_facts("mes-demo")) == 1
