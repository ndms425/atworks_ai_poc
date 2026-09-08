"""질문 승격(자가발전 spec §8): 문턱, 👎 제외, 재승격 없음, 하루 1회 가드, 스케줄러의 자리,
그리고 저장 질문의 세 라우트.

승격기에는 모델이 없다 — 여기서 검증하는 것은 전부 결정론이다: 같은 ask_log 앞에서 같은 id와
같은 제목이 나오고, 같은 날 두 번 돌지 않으며, 사람이 숨긴 질문은 링크로도 돌지 않는다.
"""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from commerce_common.testing import FakeClient, text_message
from httpx import ASGITransport, AsyncClient

from atworks_agent import AskEntry, AtworksAgentConfig, AtworksBackend, QueryFilters, QuerySpec
from atworks_agent.catalog import cluster_key_for_spec, title_for_spec
from atworks_agent_runtime import AtworksAgent
from atworks_host.app import create_app
from atworks_host.briefing import Briefings
from atworks_host.mock_backend import MockAtworks
from atworks_host.promoter import Promoter, saved_question_id
from atworks_host.reports import Reports
from atworks_host.retention import Retention
from atworks_host.scheduler import Scheduler
from atworks_host.store import Store

FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"
SKILLS = Path(__file__).resolve().parents[2] / "atworks-agent" / "skills"
T0 = datetime(2026, 9, 8, 9, tzinfo=UTC)

SPEC = QuerySpec(dimensions=["api"], measures=["non_pass"], filters=QueryFilters(window_days=30))
CLUSTER = cluster_key_for_spec(SPEC)


def _config(**overrides) -> AtworksAgentConfig:
    return AtworksAgentConfig(model="m", **overrides)


def _ask(turn_id: str, *, operator: str, at: datetime | None = None, spec: QuerySpec | None = SPEC,
         outcome: str = "answered", cluster_key: str | None = None) -> AskEntry:
    return AskEntry(
        at=at or T0, session_id="s-1", operator=operator, role="qa", question="실패 보여줘",
        intent="aggregate", spec=spec, outcome=outcome, wanted=None, tool_calls=2, cards=1,
        cluster_key=cluster_key or (cluster_key_for_spec(spec) if spec else "unmet:x"),
        turn_id=turn_id,
    )


def _seed(store: Store, *, users: int, asks: int, at: datetime | None = None) -> list[str]:
    """`asks`건을 `users`명에게 라운드로빈으로 나눠 심는다. 돌려주는 것은 turn_id들 —
    투표(👍/👎)를 나중에 그 위에 찍기 위해서다."""
    turn_ids = []
    for i in range(asks):
        turn_id = f"t-{i}-{users}-{asks}"
        store.insert_ask(_ask(turn_id, operator=f"op-{i % users}",
                              at=(at or T0) - timedelta(minutes=i)))
        turn_ids.append(turn_id)
    return turn_ids


# -- 문턱 -----------------------------------------------------------------------------------

@pytest.mark.parametrize(("users", "asks", "promoted"), [
    (2, 5, 0),   # 사람이 모자란다 — 한 사람의 습관은 팀의 질문이 아니다
    (3, 4, 0),   # 건수가 모자란다
    (3, 5, 1),   # 둘 다 넘었다
    (5, 9, 1),   # 넉넉히 넘었다
])
async def test_the_promotion_thresholds(users, asks, promoted):
    store = Store(":memory:")
    _seed(store, users=users, asks=asks)
    counts = await Promoter(store, _config()).run(T0)
    assert counts["promoted"] == promoted
    assert store.list_saved_questions().total == promoted


async def test_only_answered_rows_with_a_spec_count():
    store = Store(":memory:")
    for i in range(5):
        # 답이 나오지 않은 턴은 승격 재료가 아니다 — 실행할 스펙이 없다.
        store.insert_ask(_ask(f"p-{i}", operator=f"op-{i}", outcome="partial", spec=None,
                              cluster_key=CLUSTER))
    counts = await Promoter(store, _config()).run(T0)
    assert counts == {"candidates": 0, "promoted": 0, "skipped_downvoted": 0, "invalid": 0}


async def test_rows_outside_the_window_do_not_count():
    store = Store(":memory:")
    _seed(store, users=3, asks=5, at=T0 - timedelta(days=30))
    assert (await Promoter(store, _config()).run(T0))["candidates"] == 0
    # 창을 넓히면 같은 행들이 그대로 후보가 된다 — 문턱이 아니라 창의 문제였다.
    assert (await Promoter(store, _config(promote_window_days=60)).run(T0))["promoted"] == 1


# -- 👎 -------------------------------------------------------------------------------------

async def test_a_mostly_downvoted_cluster_is_not_promoted():
    store = Store(":memory:")
    turn_ids = _seed(store, users=3, asks=5)
    for turn_id in turn_ids[:3]:
        store.set_feedback(turn_id, "down")
    store.set_feedback(turn_ids[3], "up")
    counts = await Promoter(store, _config()).run(T0)   # 3/4 = 0.75 > 0.5
    assert counts == {"candidates": 1, "promoted": 0, "skipped_downvoted": 1, "invalid": 0}
    assert store.list_saved_questions().total == 0


async def test_an_even_split_still_promotes():
    # 정확히 절반은 통과한다: 반반인 군집을 나쁜 평가로 단정하지 않는다.
    store = Store(":memory:")
    turn_ids = _seed(store, users=3, asks=5)
    store.set_feedback(turn_ids[0], "down")
    store.set_feedback(turn_ids[1], "up")
    assert (await Promoter(store, _config()).run(T0))["promoted"] == 1


async def test_a_cluster_whose_stored_spec_the_catalogue_now_rejects_is_skipped_alone():
    """카탈로그가 바뀌기 전에 저장된 스펙은 지금의 pydantic이 거부한다. 그 군집 하나만 `invalid`로
    건너뛰고, 같은 실행의 다른 군집은 그대로 승격돼야 한다 — 하루치 승격 전체를 예외 하나로 날리면
    카탈로그를 넓힌 날 아무 질문도 올라오지 않는다."""
    store = Store(":memory:")
    stale = _seed(store, users=3, asks=5)
    other = QuerySpec(dimensions=["method"], measures=["runs"],
                      filters=QueryFilters(window_days=7))
    for i in range(5):
        store.insert_ask(_ask(f"ok-{i}", operator=f"op-{i % 3}", spec=other))
    # 저장된 JSON을 직접 낡게 만든다 — AskEntry로는 만들 수 없는 상태(그때는 유효했던 차원)다.
    store._conn.executemany(
        "UPDATE ask_log SET spec_json = ? WHERE turn_id = ?",
        [('{"dimensions": ["retired_dimension"], "measures": ["non_pass"], "filters": {}}', t)
         for t in stale])
    store._conn.commit()

    counts = await Promoter(store, _config()).run(T0)
    assert counts["candidates"] == 2 and counts["invalid"] == 1 and counts["promoted"] == 1
    survived = store.list_saved_questions().items
    assert len(survived) == 1 and survived[0].spec.dimensions == ["method"]


async def test_no_votes_is_not_a_bad_rating():
    store = Store(":memory:")
    _seed(store, users=3, asks=5)
    assert (await Promoter(store, _config()).run(T0))["skipped_downvoted"] == 0


# -- 재승격 없음 / 결정론 ---------------------------------------------------------------------

async def test_a_second_run_promotes_nothing():
    store = Store(":memory:")
    _seed(store, users=3, asks=5)
    promoter = Promoter(store, _config())
    assert (await promoter.run(T0))["promoted"] == 1
    again = await promoter.run(T0 + timedelta(hours=1))
    assert again["candidates"] == 1 and again["promoted"] == 0
    assert store.list_saved_questions().total == 1


async def test_a_hidden_question_is_not_recreated_by_the_next_run():
    store = Store(":memory:")
    _seed(store, users=3, asks=5)
    promoter = Promoter(store, _config())
    await promoter.run(T0)
    saved = store.list_saved_questions().items[0]
    store.set_saved_status(saved.id, "hidden")
    await promoter.run(T0 + timedelta(days=1))
    rows = store.list_saved_questions(status=None).items
    assert len(rows) == 1 and rows[0].status == "hidden"


async def test_the_id_and_title_are_deterministic():
    store = Store(":memory:")
    _seed(store, users=3, asks=5)
    await Promoter(store, _config()).run(T0)
    saved = store.list_saved_questions().items[0]
    assert saved.id == saved_question_id(CLUSTER) and saved.id.startswith("sq-")
    # 제목은 카탈로그 라벨의 산출물이지 모델의 문장이 아니다.
    assert saved.title == title_for_spec(SPEC, default_window_days=30)
    assert saved.title == "전체 · API별 · 30일 · 상위 20"
    assert saved.source_users == 3 and saved.source_asks == 5 and saved.spec == SPEC

    # 저장소를 새로 만들어 같은 군집을 승격해도 같은 id가 나온다.
    other = Store(":memory:")
    _seed(other, users=3, asks=5)
    await Promoter(other, _config()).run(T0 + timedelta(days=3))
    assert other.list_saved_questions().items[0].id == saved.id


async def test_the_representative_spec_is_the_clusters_newest_row():
    # 같은 cluster_key라도 필터 VALUE는 다를 수 있다 — 대표는 가장 최근 행이다.
    store = Store(":memory:")
    old = QuerySpec(dimensions=["api"], measures=["non_pass"], filters=QueryFilters(window_days=30))
    new = QuerySpec(dimensions=["api"], measures=["non_pass"], filters=QueryFilters(window_days=7))
    assert cluster_key_for_spec(old) == cluster_key_for_spec(new)
    for i in range(4):
        store.insert_ask(_ask(f"old-{i}", operator=f"op-{i}", at=T0 - timedelta(hours=5), spec=old))
    store.insert_ask(_ask("new-1", operator="op-9", at=T0, spec=new))
    await Promoter(store, _config()).run(T0)
    assert store.list_saved_questions().items[0].spec == new


# -- 하루 1회 가드 ---------------------------------------------------------------------------

async def test_maybe_run_is_once_per_local_day():
    store = Store(":memory:")
    _seed(store, users=3, asks=5)
    promoter = Promoter(store, _config())
    first = await promoter.maybe_run(T0)
    assert first is not None and first["promoted"] == 1
    assert await promoter.maybe_run(T0 + timedelta(hours=2)) is None
    assert store.get_retention_state("promote:2026-09-08") is not None
    # 다음 로컬 날짜(Asia/Seoul)에는 다시 돈다.
    assert await promoter.maybe_run(T0 + timedelta(days=1)) is not None


async def test_maybe_run_falls_back_to_its_clock():
    store = Store(":memory:")
    promoter = Promoter(store, _config(), clock=lambda: T0)
    assert await promoter.maybe_run() == {"candidates": 0, "promoted": 0,
                                          "skipped_downvoted": 0, "invalid": 0}
    assert store.get_retention_state("promote:2026-09-08") is not None


# -- Store 목록/커서 -------------------------------------------------------------------------

async def test_list_saved_questions_orders_by_uses_and_pages():
    store = Store(":memory:")
    for i in range(4):
        spec = QuerySpec(dimensions=["api"], measures=["non_pass"],
                         filters=QueryFilters(window_days=7 + i))
        for j in range(5):
            store.insert_ask(_ask(f"t{i}-{j}", operator=f"op-{j % 3}", spec=spec,
                                  cluster_key=f"cluster-{i}"))
    await Promoter(store, _config()).run(T0)
    ids = [q.id for q in store.list_saved_questions().items]
    assert len(ids) == 4
    store.bump_saved_use(ids[2], T0)
    store.bump_saved_use(ids[2], T0)
    store.bump_saved_use(ids[1], T0)
    ordered = [q.id for q in store.list_saved_questions().items]
    assert ordered[:2] == [ids[2], ids[1]]

    page = store.list_saved_questions(limit=2)
    assert page.total == 4 and page.next_cursor is not None
    rest = store.list_saved_questions(cursor=page.next_cursor, limit=2)
    assert [q.id for q in rest.items] == ordered[2:]
    assert rest.next_cursor is None


async def test_status_filter_and_total_follow_each_other():
    store = Store(":memory:")
    _seed(store, users=3, asks=5)
    await Promoter(store, _config()).run(T0)
    saved = store.list_saved_questions().items[0]
    assert store.set_saved_status(saved.id, "hidden").status == "hidden"
    assert store.list_saved_questions(status="active").total == 0
    assert store.list_saved_questions(status="hidden").total == 1
    assert store.set_saved_status("sq-nope", "hidden") is None
    assert store.bump_saved_use("sq-nope", T0) is None


# -- 스케줄러의 자리 -------------------------------------------------------------------------

async def test_the_tick_runs_retention_then_the_promoter(tmp_path):
    order: list[str] = []

    class RecordingRetention(Retention):
        async def maybe_run(self, now):
            order.append("retention")
            return await super().maybe_run(now)

    class RecordingPromoter(Promoter):
        async def maybe_run(self, now=None):
            order.append("promoter")
            return await super().maybe_run(now)

    config = _config()
    backend = MockAtworks(config, FIXTURES)
    store = backend.store
    _seed(store, users=3, asks=5)
    scheduler = Scheduler(backend, Reports(tmp_path), None,
                          retention=RecordingRetention(store, config),
                          promoter=RecordingPromoter(store, config))
    await scheduler.tick(T0)
    assert order == ["retention", "promoter"]
    assert store.list_saved_questions().total == 1


async def test_a_failing_promoter_never_stalls_the_tick(tmp_path):
    class BrokenPromoter(Promoter):
        async def maybe_run(self, now=None):
            raise RuntimeError("boom")

    config = _config()
    backend = MockAtworks(config, FIXTURES)
    scheduler = Scheduler(backend, Reports(tmp_path), None,
                          promoter=BrokenPromoter(backend.store, config))
    assert await scheduler.tick(T0) == []


# -- 라우트 ---------------------------------------------------------------------------------

def _app(tmp_path, config):
    backend = MockAtworks(config, FIXTURES)
    agent = AtworksAgent(backend=backend, skills_dir=SKILLS, config=config,
                         client=FakeClient([text_message("ok")]))
    reports = Reports(tmp_path)
    briefings = Briefings(tmp_path / "b", config)
    app = create_app(agent=agent, backend=backend,
                     scheduler=Scheduler(backend, reports, None, briefings=briefings),
                     reports=reports, briefings=briefings)
    return app, backend


@pytest.fixture
async def client(tmp_path):
    app, backend = _app(tmp_path, _config())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        c.backend = backend
        yield c


async def _sid(client) -> str:
    return (await client.post("/api/atworks/session")).json()["session_id"]


async def _promote(client) -> str:
    _seed(client.backend.store, users=3, asks=5)
    await Promoter(client.backend.store, _config()).run(datetime.now(UTC))
    return client.backend.store.list_saved_questions().items[0].id


async def test_the_list_route_answers_in_the_paged_envelope(client):
    headers = {"X-Session-Id": await _sid(client)}
    saved_id = await _promote(client)
    body = (await client.get("/api/atworks/saved-questions", headers=headers)).json()
    assert set(body) == {"items", "next_cursor", "total"}
    assert body["total"] == 1 and body["items"][0]["id"] == saved_id
    assert body["items"][0]["source_users"] == 3 and body["items"][0]["source_asks"] == 5
    assert body["items"][0]["title"] and body["items"][0]["status"] == "active"


async def test_hide_removes_it_from_the_default_list_and_unhide_brings_it_back(client):
    headers = {"X-Session-Id": await _sid(client)}
    saved_id = await _promote(client)
    hidden = await client.post(f"/api/atworks/saved-questions/{saved_id}/hide", headers=headers)
    assert hidden.status_code == 200 and hidden.json()["saved_question"]["status"] == "hidden"
    assert (await client.get("/api/atworks/saved-questions", headers=headers)).json()["total"] == 0
    assert (await client.get("/api/atworks/saved-questions?status=hidden",
                             headers=headers)).json()["total"] == 1
    assert (await client.get("/api/atworks/saved-questions?status=all",
                             headers=headers)).json()["total"] == 1
    await client.post(f"/api/atworks/saved-questions/{saved_id}/unhide", headers=headers)
    assert (await client.get("/api/atworks/saved-questions", headers=headers)).json()["total"] == 1


async def test_hide_writes_the_audit_pair_and_no_approval_mark(client):
    headers = {"X-Session-Id": await _sid(client)}
    saved_id = await _promote(client)
    await client.post(f"/api/atworks/saved-questions/{saved_id}/hide", headers=headers)
    rows = (await client.get("/api/atworks/audit", headers=headers)).json()["items"]
    actions = [r["action"] for r in rows if r["target_id"] == saved_id]
    assert actions == ["saved_question_hide:ok", "saved_question_hide"]
    assert all(r["target_kind"] == "saved_question" for r in rows if r["target_id"] == saved_id)


async def test_hiding_an_unknown_question_is_a_404_recorded_as_blocked(client):
    """404는 `growth_action`의 try 안에서 난다 — 밖에서 던지면 감사 로그에 `:ok`가 먼저 찍히고
    아무 일도 일어나지 않은 클릭이 성공으로 남는다(어휘 라우트와 같은 자리, T7 리뷰 minor)."""
    headers = {"X-Session-Id": await _sid(client)}
    assert (await client.post("/api/atworks/saved-questions/sq-nope/hide",
                              headers=headers)).status_code == 404
    rows = (await client.get("/api/atworks/audit", headers=headers)).json()["items"]
    actions = [r["action"] for r in rows if r["target_id"] == "sq-nope"]
    assert set(actions) == {"saved_question_hide", "saved_question_hide:blocked"}


async def test_running_a_saved_question_computes_it_live_and_bumps_uses(client):
    headers = {"X-Session-Id": await _sid(client)}
    saved_id = await _promote(client)
    body = (await client.get(f"/api/atworks/saved-questions/{saved_id}/run",
                             headers=headers)).json()
    # QueryResult 그대로: 저장된 답이 아니라 지금 계산한 결과다.
    assert body["source"] in {"rollup_day", "rollup_key_day", "rollup_operator_day", "runs"}
    assert body["spec"]["dimensions"] == ["api"] and "population" in body and "rows" in body
    assert len(body["window"]) == 2
    after = client.backend.store.get_saved_question(saved_id)
    assert after.uses == 1 and after.last_used_at is not None


async def test_running_an_unknown_or_hidden_question_is_404(client):
    headers = {"X-Session-Id": await _sid(client)}
    saved_id = await _promote(client)
    assert (await client.get("/api/atworks/saved-questions/sq-nope/run",
                             headers=headers)).status_code == 404
    await client.post(f"/api/atworks/saved-questions/{saved_id}/hide", headers=headers)
    assert (await client.get(f"/api/atworks/saved-questions/{saved_id}/run",
                             headers=headers)).status_code == 404
    assert (await client.post("/api/atworks/saved-questions/sq-nope/hide",
                              headers=headers)).status_code == 404


async def test_the_routes_need_a_session(client):
    assert (await client.get("/api/atworks/saved-questions")).status_code == 401
    assert (await client.get("/api/atworks/saved-questions/sq-1/run")).status_code == 401
    assert (await client.post("/api/atworks/saved-questions/sq-1/hide")).status_code == 401


async def test_the_routes_404_when_growth_is_off(tmp_path):
    app, _ = _app(tmp_path, _config(enable_growth=False))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        headers = {"X-Session-Id": await _sid(c)}
        assert (await c.get("/api/atworks/saved-questions", headers=headers)).status_code == 404
        assert (await c.get("/api/atworks/saved-questions/sq-1/run",
                            headers=headers)).status_code == 404
        assert (await c.post("/api/atworks/saved-questions/sq-1/hide",
                             headers=headers)).status_code == 404


async def test_growth_summary_carries_the_thresholds_and_the_new_saved_count(client):
    headers = {"X-Session-Id": await _sid(client)}
    await _promote(client)
    body = (await client.get("/api/atworks/growth/summary?days=7", headers=headers)).json()
    assert body["new_saved"] == 1
    # 카드의 빈 상태 문구가 읽는 숫자 — config의 값이지 화면의 상수가 아니다.
    assert body["thresholds"] == {"min_users": 3, "min_asks": 5, "window_days": 7}


def test_every_backend_subclass_implements_the_saved_question_methods():
    """세 메서드는 ABC에서 abstract이고, 이 실행에 로드된 모든 서브클래스가 자기 구현을 갖는다 —
    추상 스텁을 조용히 물려받은 double은 자기 테스트만 통과하고 진짜 라우트에서 터진다."""
    for name in ("list_saved_questions", "run_saved_question", "set_saved_question_status"):
        assert name in AtworksBackend.__abstractmethods__
        for subclass in AtworksBackend.__subclasses__():
            assert name in vars(subclass), f"{subclass.__name__} is missing {name}"
