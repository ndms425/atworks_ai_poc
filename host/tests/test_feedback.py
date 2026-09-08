"""피드백 → eval 케이스(자가발전 spec §9).

여기서 지키는 선은 넷이다. (1) 한 턴에 한 표 — 다시 누르면 덮어쓴다. (2) 표는 감사 로그를 건드리지
않는다: 감사 2행은 "사람이 공유 상태를 바꿨다"의 증적이고 표는 그런 변경이 아니다. (3) 👍는 **답한**
턴에서만 케이스가 되고, 그 파일의 모양은 spec §9 그대로다(아래 골든 딕트). (4) 👎는 케이스를 만들지
않고, 그 턴이 실제로 컨텍스트에 실었던 어휘에만 거부를 센다 — 그래서 어떤 용어가 실렸는지가
ask_log 행에 남아 있어야 한다.
"""
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from commerce_common.testing import FakeClient, text_message, tool_calls_message
from httpx import ASGITransport, AsyncClient

from atworks_agent import (
    AskEntry,
    AtworksAgentConfig,
    AtworksSessionContext,
    QueryFilters,
    QuerySpec,
)
from atworks_agent_runtime import AtworksAgent
from atworks_host.app import create_app
from atworks_host.briefing import Briefings
from atworks_host.evals_writer import build_case, write_case
from atworks_host.mock_backend import MockAtworks
from atworks_host.reports import Reports
from atworks_host.scheduler import Scheduler
from atworks_host.store import Store

FIXTURES = Path(__file__).resolve().parents[1] / "atworks_host" / "fixtures"
SKILLS = Path(__file__).resolve().parents[2] / "atworks-agent" / "skills"
T0 = datetime(2026, 9, 8, 9, tzinfo=UTC)

SPEC = QuerySpec(dimensions=["path_segment_2"], measures=["non_pass", "apis", "runs"],
                 filters=QueryFilters(status="non_pass", window_days=30))

#: 카드가 답을 낸 턴 하나 — query_runs(데이터 도구) → present_query_table(카드) → 문장.
ANSWERING_TURN = [
    tool_calls_message(("query_runs", {"filters": {"status": "non_pass", "window_days": 30},
                                       "dimensions": ["path_segment_2"],
                                       "measures": ["non_pass", "apis", "runs"]}, "tu-q")),
    tool_calls_message(("present_query_table", {"title": "경로 2조각별 실패"}, "tu-p")),
    text_message("표로 정리했습니다."),
]


def _entry(turn_id: str = "t-1", *, spec: QuerySpec | None = SPEC, outcome: str = "answered",
           operator: str = "minseong", role: str | None = "developer",
           vocabulary_terms: list[str] | None = None) -> AskEntry:
    return AskEntry(
        at=T0, session_id="s-1", operator=operator, role=role,
        question="최근 30일 실패를 endpoint 기준으로 묶어서 보여줘", intent="aggregate", spec=spec,
        outcome=outcome, wanted=None, tool_calls=2, cards=1,
        vocabulary_terms=vocabulary_terms or [], cluster_key="c", turn_id=turn_id,
    )


# -- evals_writer: 케이스 파일의 모양 --------------------------------------------------------

def test_the_case_matches_the_spec_shape_exactly(tmp_path):
    """spec §9의 JSON 그대로. 필드가 하나 늘거나 이름이 바뀌면 여기서 걸린다 — 이 파일은 러너와
    commerce-evals 스키마가 함께 읽는 계약이지 우리 마음대로 넓힐 수 있는 딕트가 아니다."""
    path = write_case(_entry("t-golden"), cases_dir=tmp_path)
    assert path.name == "20260908-001.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "id": "query_runs-20260908-001",
        "priority": "P2",
        "tags": ["query_runs", "path_segment_2"],
        "skip": False,
        "state": {"operator": "minseong", "role": "developer"},
        "turns": ["최근 30일 실패를 endpoint 기준으로 묶어서 보여줘"],
        "expected": {
            "calls_tool": "query_runs",
            "spec_equals": {
                "dimensions": ["path_segment_2"],
                "measures": ["non_pass", "apis", "runs"],
                # 필터의 종류만, 정렬해서. 값(30일이었는지, 어떤 상태였는지)은 케이스에 없다.
                "filters_kinds": ["status", "window_days"],
            },
            "ui_components": ["query_table"],
            "never_calls": ["stage_job", "apply_job"],
            "max_tool_calls": 4,
        },
        "notes": "auto-generated from 👍 on turn t-golden",
    }


def test_the_case_file_is_written_lf_and_utf8(tmp_path):
    path = write_case(_entry("t-lf"), cases_dir=tmp_path)
    assert b"\r\n" not in path.read_bytes()
    assert "endpoint" in path.read_text(encoding="utf-8")


def test_a_second_vote_on_the_same_turn_rewrites_one_file(tmp_path):
    # 재투표가 케이스를 불리면 스위트가 한 사람의 클릭 수만큼 커진다.
    first = write_case(_entry("t-same"), cases_dir=tmp_path)
    again = write_case(_entry("t-same"), cases_dir=tmp_path)
    assert first == again and len(list(tmp_path.glob("*.json"))) == 1


def test_a_different_turn_on_the_same_day_takes_the_next_seq(tmp_path):
    write_case(_entry("t-a"), cases_dir=tmp_path)
    second = write_case(_entry("t-b"), cases_dir=tmp_path)
    assert second.name == "20260908-002.json"
    assert json.loads(second.read_text(encoding="utf-8"))["id"] == "query_runs-20260908-002"


def test_a_turn_without_a_spec_cannot_become_a_case(tmp_path):
    with pytest.raises(ValueError):
        write_case(_entry("t-none", spec=None), cases_dir=tmp_path)


def test_filter_kinds_carry_no_values():
    case = build_case(_entry("t-x"), case_id="c")
    kinds = case["expected"]["spec_equals"]["filters_kinds"]
    assert kinds == ["status", "window_days"]
    # 값이 새어 들어가면 데이터가 바뀔 때마다 케이스가 붉어진다.
    assert "30" not in json.dumps(case["expected"]["spec_equals"])


# -- Store ------------------------------------------------------------------------------------

def test_ask_by_turn_round_trips_the_vocabulary_terms():
    store = Store(":memory:")
    store.insert_ask(_entry("t-v", vocabulary_terms=["결제 계열", "계약 계열"]))
    back = store.ask_by_turn("t-v")
    assert back is not None and back.vocabulary_terms == ["결제 계열", "계약 계열"]
    assert store.ask_by_turn("t-none") is None


def test_a_row_written_before_the_column_existed_reads_as_an_empty_list(tmp_path):
    """`_widen_columns`가 ALTER하는 자리 — 옛 파일의 NULL은 빈 목록이지 추측이 아니다."""
    path = tmp_path / "old.sqlite"
    store = Store(str(path))
    store.insert_ask(_entry("t-old"))
    store._conn.execute("UPDATE ask_log SET vocabulary_terms = NULL")
    store._conn.commit()
    assert store.ask_by_turn("t-old").vocabulary_terms == []


# -- 라우트 ------------------------------------------------------------------------------------

def _app(tmp_path, config, responses):
    backend = MockAtworks(config, FIXTURES, store=Store(":memory:"))
    agent = AtworksAgent(backend=backend, skills_dir=SKILLS, config=config,
                         client=FakeClient(responses))
    reports = Reports(tmp_path)
    briefings = Briefings(tmp_path / "b", config)
    cases_dir = tmp_path / "cases"
    app = create_app(agent=agent, backend=backend,
                     scheduler=Scheduler(backend, reports, None, briefings=briefings),
                     reports=reports, briefings=briefings, evals_cases_dir=cases_dir)
    return app, backend, cases_dir


@pytest.fixture
async def client(tmp_path):
    app, backend, cases_dir = _app(tmp_path, AtworksAgentConfig(model="m"), ANSWERING_TURN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        c.backend = backend
        c.cases_dir = cases_dir
        yield c


async def _sid(client, operator: str | None = None) -> str:
    payload = {"operator_id": operator} if operator else {}
    return (await client.post("/api/atworks/session", json=payload)).json()["session_id"]


async def _answered_turn(client, headers) -> str:
    assert (await client.post("/api/atworks/chat", headers=headers,
                              json={"message": "실패를 endpoint 기준으로 묶어줘"})).status_code == 200
    row = (await client.get("/api/atworks/ask-log", headers=headers)).json()["items"][0]
    assert row["outcome"] == "answered" and row["spec"] is not None
    return row["turn_id"]


async def test_an_upvote_on_an_answered_turn_writes_one_case(client):
    headers = {"X-Session-Id": await _sid(client)}
    turn_id = await _answered_turn(client, headers)
    body = (await client.post("/api/atworks/feedback", headers=headers,
                              json={"turn_id": turn_id, "vote": "up"})).json()
    assert body["ok"] is True and body["entry"]["feedback"] == "up"
    written = list(client.cases_dir.glob("*.json"))
    assert len(written) == 1 and body["case_path"] == str(written[0])
    case = json.loads(written[0].read_text(encoding="utf-8"))
    assert case["expected"]["calls_tool"] == "query_runs"
    assert case["expected"]["spec_equals"]["dimensions"] == ["path_segment_2"]
    assert case["notes"].endswith(turn_id)
    assert case["state"]["operator"] == "minseong"


async def test_re_voting_the_same_turn_keeps_the_last_vote_and_one_case(client):
    headers = {"X-Session-Id": await _sid(client)}
    turn_id = await _answered_turn(client, headers)
    await client.post("/api/atworks/feedback", headers=headers,
                      json={"turn_id": turn_id, "vote": "up"})
    flipped = (await client.post("/api/atworks/feedback", headers=headers,
                                 json={"turn_id": turn_id, "vote": "down"})).json()
    assert flipped["entry"]["feedback"] == "down"
    again = (await client.post("/api/atworks/feedback", headers=headers,
                               json={"turn_id": turn_id, "vote": "up"})).json()
    assert again["entry"]["feedback"] == "up"
    # 표는 덮어써지고, 케이스는 turn_id 하나당 한 장이다.
    assert len(list(client.cases_dir.glob("*.json"))) == 1
    row = (await client.get("/api/atworks/ask-log", headers=headers)).json()["items"][0]
    assert row["feedback"] == "up"


async def test_a_downvote_writes_no_case(client):
    headers = {"X-Session-Id": await _sid(client)}
    turn_id = await _answered_turn(client, headers)
    await client.post("/api/atworks/feedback", headers=headers,
                      json={"turn_id": turn_id, "vote": "down"})
    assert list(client.cases_dir.glob("*.json")) == []


async def test_an_upvote_on_a_turn_that_answered_nothing_writes_no_case(tmp_path):
    # 카드도 데이터 도구도 없던 턴은 `partial`이다 — 고정할 행동이 없으므로 표만 남는다.
    app, _backend, cases_dir = _app(tmp_path, AtworksAgentConfig(model="m"), [text_message("음.")])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        headers = {"X-Session-Id": await _sid(c)}
        await c.post("/api/atworks/chat", headers=headers, json={"message": "안녕"})
        row = (await c.get("/api/atworks/ask-log", headers=headers)).json()["items"][0]
        assert row["outcome"] == "partial"
        body = (await c.post("/api/atworks/feedback", headers=headers,
                             json={"turn_id": row["turn_id"], "vote": "up"})).json()
        assert body["entry"]["feedback"] == "up" and body["case_path"] is None
        assert list(cases_dir.glob("*.json")) == []


async def test_voting_writes_no_audit_row(client):
    """표는 공유 상태의 변경이 아니다 — 승인 마크도, 원장도 움직이지 않는다. 감사 로그에 실으면
    "무엇이 승인됐나"를 한눈에 보여주던 로그가 클릭 기록장이 된다."""
    headers = {"X-Session-Id": await _sid(client)}
    turn_id = await _answered_turn(client, headers)
    before = (await client.get("/api/atworks/audit", headers=headers)).json()["total"]
    await client.post("/api/atworks/feedback", headers=headers,
                      json={"turn_id": turn_id, "vote": "up"})
    await client.post("/api/atworks/feedback", headers=headers,
                      json={"turn_id": turn_id, "vote": "down"})
    after = (await client.get("/api/atworks/audit", headers=headers)).json()
    assert after["total"] == before
    assert not [r for r in after["items"] if "feedback" in r["action"]]


async def test_an_unknown_turn_is_a_404(client):
    headers = {"X-Session-Id": await _sid(client)}
    assert (await client.post("/api/atworks/feedback", headers=headers,
                              json={"turn_id": "t-nope", "vote": "up"})).status_code == 404


async def test_a_bad_vote_value_is_a_422(client):
    headers = {"X-Session-Id": await _sid(client)}
    assert (await client.post("/api/atworks/feedback", headers=headers,
                              json={"turn_id": "t-1", "vote": "maybe"})).status_code == 422


async def test_the_route_needs_a_session(client):
    assert (await client.post("/api/atworks/feedback",
                              json={"turn_id": "t-1", "vote": "up"})).status_code == 401


async def test_the_route_404s_when_growth_is_off(tmp_path):
    app, _backend, _dir = _app(tmp_path, AtworksAgentConfig(model="m", enable_growth=False), [])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        headers = {"X-Session-Id": await _sid(c)}
        assert (await c.post("/api/atworks/feedback", headers=headers,
                             json={"turn_id": "t-1", "vote": "up"})).status_code == 404


# -- 👎 → 어휘 거부 -----------------------------------------------------------------------------

async def test_a_downvote_counts_a_rejection_on_the_terms_that_turn_carried(tmp_path):
    """이 턴의 컨텍스트에 실렸던 용어에만 거부가 붙는다. 그래서 어떤 용어가 실렸는지가 행에 남아야
    한다 — 표는 턴이 끝나고 한참 뒤에 오고, 그때 메시지를 다시 매칭하면 그 사이 바뀐 확정 집합으로
    다른 답이 나온다."""
    config = AtworksAgentConfig(model="m")
    app, backend, _dir = _app(tmp_path, config, [text_message("네.")])
    session = AtworksSessionContext(session_id="s-seed", project_id="mes-demo",
                                    operator="minseong", now=T0)
    await backend.propose_alias(session, "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    await backend.confirm_alias(session, "결제 계열")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        headers = {"X-Session-Id": await _sid(c, "jihoon")}
        await c.post("/api/atworks/chat", headers=headers, json={"message": "결제 계열 실패 보여줘"})
        row = (await c.get("/api/atworks/ask-log", headers=headers)).json()["items"][0]
        assert row["vocabulary_terms"] == ["결제 계열"]
        await c.post("/api/atworks/feedback", headers=headers,
                     json={"turn_id": row["turn_id"], "vote": "down"})
    assert backend.store.get_vocabulary("결제 계열").rejections == 1


async def test_an_upvote_never_counts_a_rejection(tmp_path):
    config = AtworksAgentConfig(model="m")
    app, backend, _dir = _app(tmp_path, config, ANSWERING_TURN)
    session = AtworksSessionContext(session_id="s-seed", project_id="mes-demo",
                                    operator="minseong", now=T0)
    await backend.propose_alias(session, "결제 계열", QueryFilters(path_prefix="/v1/payment"))
    await backend.confirm_alias(session, "결제 계열")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        headers = {"X-Session-Id": await _sid(c, "jihoon")}
        await c.post("/api/atworks/chat", headers=headers,
                     json={"message": "결제 계열 실패를 endpoint로 묶어줘"})
        row = (await c.get("/api/atworks/ask-log", headers=headers)).json()["items"][0]
        assert row["vocabulary_terms"] == ["결제 계열"]
        await c.post("/api/atworks/feedback", headers=headers,
                     json={"turn_id": row["turn_id"], "vote": "up"})
    entry = backend.store.get_vocabulary("결제 계열")
    assert entry.rejections == 0 and entry.status == "confirmed"


async def test_a_downvote_on_a_turn_that_carried_no_vocabulary_touches_nothing(client):
    headers = {"X-Session-Id": await _sid(client)}
    turn_id = await _answered_turn(client, headers)
    assert (await client.post("/api/atworks/feedback", headers=headers,
                              json={"turn_id": turn_id, "vote": "down"})).status_code == 200
    assert client.backend.store.list_vocabulary().total == 0


# -- 카드 페이로드 -------------------------------------------------------------------------------

async def test_the_card_carries_a_turn_id_and_an_empty_feedback_slot(client):
    """👍/👎가 ask_log 행을 찾는 유일한 열쇠가 turn_id다. 표 자체는 카드가 그려지는 시점에 언제나
    비어 있다 — 사람은 턴이 끝난 뒤에 누른다."""
    headers = {"X-Session-Id": await _sid(client)}
    chat = await client.post("/api/atworks/chat", headers=headers,
                             json={"message": "실패를 endpoint 기준으로 묶어줘"})
    payloads = [json.loads(line[len("data: "):]) for line in chat.text.splitlines()
                if line.startswith("data: ")]
    cards = [p["payload"] for p in payloads
             if isinstance(p, dict) and p.get("component") == "query_table"]
    assert len(cards) == 1
    assert cards[0]["turn_id"] and cards[0]["feedback"] is None
