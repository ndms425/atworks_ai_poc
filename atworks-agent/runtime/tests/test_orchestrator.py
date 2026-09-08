"""스크립트된 모델 클라이언트로 턴 루프를 돈다. commerce_common.testing.FakeClient 사용."""
import json
from types import SimpleNamespace
from typing import Any

import pytest
from commerce_common.testing import (
    FakeClient,
    text_block,
    text_message,
    tool_calls_message,
    tool_use_message,
)

from atworks_agent.gates import FIGURES_IN_PROSE_REMINDER, STAGING_FOLLOWTHROUGH_REMINDER
from atworks_agent.types import AttachedItem, QueryFilters, VocabularyEntry
from atworks_agent_runtime import AtworksAgent

from .conftest import T0


@pytest.fixture
def make_agent(backend, skills):
    def _make(responses: list[SimpleNamespace], **config_updates: Any) -> AtworksAgent:
        agent = AtworksAgent(backend=backend, skills=skills, client=FakeClient(responses))
        if config_updates:
            agent.config = agent.config.model_copy(update=config_updates)
        return agent
    return _make


async def run_turn(agent, text, session, state, attached=()):
    messages = [{"role": "user", "content": text}]
    events = []
    async for event in agent.stream_turn(messages, session, state, attached_items=attached):
        events.append(event)
    return events, messages


async def test_failure_question_forces_list_runs_first(make_agent, session, state):
    chips = ("present_suggestions", {"suggestions": ["다음 항목", "전체 목록"]})
    closing = tool_calls_message(("present_run_digest", {"items": [{"kind": "fail", "ref_id": "run-1", "headline": "amount 규칙 위반"}]}), chips)
    closing.content.insert(0, text_block("먼저 살펴볼 항목을 정리했습니다."))
    agent = make_agent([
        tool_use_message("list_runs", {"filters": {"status": "fail"}}),
        tool_use_message("rank_failed_runs", {"scorer": "risk_v1"}),
        closing,
    ])
    events, _ = await run_turn(agent, "최근 실패한 api 중 risk 있는 것 가져와봐", session, state)
    assert agent.client.calls[0]["tool_choice"] == {"type": "tool", "name": "list_runs"}
    comps = [e.data.get("component") for e in events if e.type == "ui"]
    assert comps == ["run_digest", "suggestions"]
    digest = next(e for e in events if e.data.get("component") == "run_digest")
    assert digest.data["payload"]["population"] == 1
    assert events[-1].type == "turn_complete"


async def test_stage_turn_shows_preview_and_change_update(make_agent, session, state):
    chips = ("present_suggestions", {"suggestions": ["대상 계 바꾸기", "취소"]})
    closing = tool_calls_message(chips)
    closing.content.insert(0, text_block("Jobs 페이지에서 승인하면 실행됩니다."))
    agent = make_agent([
        tool_use_message("search_apis", {"query": "", "updated_after": "2026-08-27T00:00:00+09:00"}),
        tool_calls_message(("stage_job", {"kind": "scheduled_run", "summary": "1주일 업데이트분 3일간 09시 dev/stg", "api_ids": ["api-1"], "target_envs": ["dev", "stg"],
                                          "schedules": [{"kind": "daily", "at": "09:00", "from_date": "2026-09-04", "count": 3}],
                                          "test_data": [{"label": "S1 정상", "values": {"amount": "1000"}}],
                                          "confidence": {"target_envs": 0.3}, "assumptions": ["target_envs defaulted to [dev, stg]"]}, "tu-stage")),
        closing,
    ])
    events, _ = await run_turn(agent, "지난 1주일 업데이트된 api 오늘부터 3일간 매일 9시에 실행해줘", session, state)
    kinds = [(e.type, e.data.get("component")) for e in events if e.type in ("ui", "change_update")]
    assert kinds == [("change_update", None), ("ui", "job_preview"), ("ui", "suggestions")]
    preview = next(e for e in events if e.data.get("component") == "job_preview")
    assert preview.data["payload"]["matrix"]["envs"] == ["dev", "stg"]
    assert preview.data["payload"]["matrix"]["data_sets"] == ["S1 정상"]
    assert state.seen_jobs["job-0001"].status.value == "staged" and not state.approved_job_ids


async def test_job_request_without_staging_is_reminded_once(make_agent, session, state):
    agent = make_agent([text_message("실행해드릴까요?"), text_message("먼저 API를 찾아야 합니다.")])
    _, messages = await run_turn(agent, "계약 api 전부 지금 실행해줘", session, state)
    reminders = [m for m in messages if m.get("role") == "user" and isinstance(m.get("content"), list)
                 and m["content"][0].get("text") == STAGING_FOLLOWTHROUGH_REMINDER]
    assert len(reminders) == 1 and len(agent.client.calls) == 2


async def test_tools_do_not_carry_eager_input_streaming(make_agent, session, state):
    agent = make_agent([text_message("ok")])
    await run_turn(agent, "안녕", session, state)
    assert all("eager_input_streaming" not in json.dumps(t) for t in agent.client.calls[0]["tools"])


async def test_prose_restating_figures_is_reminded_once(make_agent, session, state):
    agent = make_agent([
        text_message("| 기간 | 실패율 |\n|---|---|\n| 이번 주 | 100% |"),
        text_message("환불 API는 규칙 위반으로 실패했습니다."),
    ])
    _, messages = await run_turn(agent, "고마워", session, state)
    reminders = [m for m in messages if m.get("role") == "user" and isinstance(m.get("content"), list)
                 and m["content"][0].get("text") == FIGURES_IN_PROSE_REMINDER]
    assert len(reminders) == 1 and len(agent.client.calls) == 2


async def test_prose_without_figures_is_not_reminded(make_agent, session, state):
    agent = make_agent([text_message("run-0012는 pass였습니다.")])
    _, messages = await run_turn(agent, "고마워", session, state)
    assert len(agent.client.calls) == 1
    assert not any(
        m.get("role") == "user" and isinstance(m.get("content"), list)
        and m["content"][0].get("text") == FIGURES_IN_PROSE_REMINDER
        for m in messages
    )


async def test_prose_restating_figures_in_closing_round_is_reminded_once(make_agent, session, state):
    chips = ("present_suggestions", {"suggestions": ["상세 보기"]})
    closing = tool_calls_message(chips)
    closing.content.insert(0, text_block("이번 주 실패율은 100% 입니다."))
    agent = make_agent([
        closing,
        text_message("환불 API는 refundAmount >= 0 규칙 위반으로 실패했습니다."),
    ])
    events, messages = await run_turn(agent, "고마워", session, state)
    reminders = [m for m in messages if m.get("role") == "user" and isinstance(m.get("content"), list)
                 and m["content"][0].get("text") == FIGURES_IN_PROSE_REMINDER]
    assert len(reminders) == 1 and len(agent.client.calls) == 2
    assert events[-1].type == "turn_complete"


async def test_prose_without_figures_in_closing_round_is_not_reminded(make_agent, session, state):
    chips = ("present_suggestions", {"suggestions": ["상세 보기"]})
    closing = tool_calls_message(chips)
    closing.content.insert(0, text_block("환불 API는 규칙 위반으로 실패했습니다."))
    agent = make_agent([closing])
    events, messages = await run_turn(agent, "고마워", session, state)
    assert len(agent.client.calls) == 1
    assert not any(
        m.get("role") == "user" and isinstance(m.get("content"), list)
        and m["content"][0].get("text") == FIGURES_IN_PROSE_REMINDER
        for m in messages
    )
    assert events[-1].type == "turn_complete"


async def test_compact_history_clears_oldest_results_and_leaves_the_stub(make_agent, session, state):
    """Task 3's compaction contract: a turn whose last call was given
    ``compact_history_above_tokens`` or more must clear at least one earlier tool result down
    to ``CLEARED_RESULT`` and report how many on ``turn_complete`` -- the host reads that count
    to know the stored transcript needs a full rewrite (streaming.py)."""
    from commerce_common.turn import CLEARED_RESULT

    agent = make_agent(
        [
            tool_use_message("list_runs", {"filters": {"status": "fail"}}),
            tool_use_message("list_runs", {"filters": {"status": "non_pass"}}),
            text_message("확인했습니다."),
        ],
        compact_history_above_tokens=1,
    )
    events, messages = await run_turn(agent, "안녕", session, state)

    turn_complete = events[-1]
    assert turn_complete.type == "turn_complete"
    assert turn_complete.data["results_cleared"] >= 1

    cleared_blocks = [
        block
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_result" and block.get("content") == CLEARED_RESULT
    ]
    assert len(cleared_blocks) == turn_complete.data["results_cleared"]


async def test_attached_items_reach_the_system_prompt(make_agent, session, state):
    agent = make_agent([text_message("run-1은 amount >= 0 규칙에 걸렸습니다.")])
    item = AttachedItem(order=1, kind="run", ref_id="run-1", label="POST /v1/contracts", field="amount", actual="-300", expected="amount >= 0", comment="왜 실패?")
    await run_turn(agent, "이거 왜 실패했어", session, state, attached=[item])
    system = agent.client.calls[0]["system"]
    assert "<attached-result-items>" in system[1]["text"] and "run-1" in system[1]["text"]
    assert "cache_control" in system[0]


# -- 어휘와 메모리 (self-growth spec §7) ----------------------------------------------------

class _SpyMemory:
    """MemoryRuntime is a frozen dataclass, so the spy replaces the whole runtime."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def extract(self, *args, **kwargs):
        self.calls.append("extract")
        return []


async def test_update_memory_does_not_extract_when_the_switch_is_off(make_agent, session):
    """메모리는 켜져 있어도(어휘가 그 위에 산다) 참조 구현의 자유 사실 추출은 돌지 않는다.
    켜졌다면 한 사람의 문장이 팀 전체의 컨텍스트에 들어간다 -- 아무도 승인하지 않은 쓰기다."""
    agent = make_agent([], enable_memory=True, memory_extract_facts=False)
    agent.memory = _SpyMemory()
    messages = [{"role": "user", "content": "결제 계열 실패"}, {"role": "assistant", "content": "네."}]
    assert await agent.update_memory(messages, session) == []
    assert agent.memory.calls == []


async def test_update_memory_extracts_when_a_deployment_turns_it_on(make_agent, session):
    # The guard is the config flag, not a removed code path: a deployment that wants the
    # reference behaviour still gets it.
    agent = make_agent([], enable_memory=True, memory_extract_facts=True)
    agent.memory = _SpyMemory()
    await agent.update_memory([{"role": "user", "content": "x"}], session)
    assert agent.memory.calls == ["extract"]


async def test_matched_vocabulary_reaches_the_context_block_of_the_request(make_agent, session, state):
    entry = VocabularyEntry(term="결제 계열", fragment=QueryFilters(path_prefix="/v1/payment"),
                            status="confirmed", proposed_by="minseong", proposed_at=T0)
    agent = make_agent([text_message("네.")])
    messages = [{"role": "user", "content": "결제 계열 실패 보여줘"}]
    async for _ in agent.stream_turn(messages, session, state, vocabulary=[entry]):
        pass
    system = json.dumps(agent.client.calls[0]["system"], ensure_ascii=False)
    assert "결제 계열" in system and "경로 접두사 /v1/payment" in system


async def test_pending_aliases_are_cleared_at_the_start_of_every_turn(make_agent, session, state):
    state.pending_aliases.append(VocabularyEntry(
        term="지난 턴", fragment=QueryFilters(path_prefix="/v1/x"), proposed_by="m", proposed_at=T0))
    agent = make_agent([text_message("네.")])
    async for _ in agent.stream_turn([{"role": "user", "content": "안녕"}], session, state):
        pass
    assert state.pending_aliases == []
