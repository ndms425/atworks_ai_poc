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

from atworks_agent.gates import STAGING_FOLLOWTHROUGH_REMINDER
from atworks_agent.types import AttachedItem
from atworks_agent_runtime import AtworksAgent


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
    closing.content.insert(0, text_block("실패 1건 중 먼저 볼 1건."))
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
        tool_calls_message(("stage_job", {"kind": "scheduled_run", "summary": "1주일 업데이트분 3일간 09시", "api_ids": ["api-1"], "target_envs": ["dev"],
                                          "schedules": [{"kind": "daily", "at": "09:00", "from_date": "2026-09-04", "count": 3}],
                                          "confidence": {"target_envs": 0.3}, "assumptions": ["target_envs defaulted to [dev]"]}, "tu-stage")),
        closing,
    ])
    events, _ = await run_turn(agent, "지난 1주일 업데이트된 api 오늘부터 3일간 매일 9시에 실행해줘", session, state)
    kinds = [(e.type, e.data.get("component")) for e in events if e.type in ("ui", "change_update")]
    assert kinds == [("change_update", None), ("ui", "job_preview"), ("ui", "suggestions")]
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


async def test_attached_items_reach_the_system_prompt(make_agent, session, state):
    agent = make_agent([text_message("run-1은 amount >= 0 규칙에 걸렸습니다.")])
    item = AttachedItem(order=1, kind="run", ref_id="run-1", label="POST /v1/contracts", field="amount", actual="-300", expected="amount >= 0", comment="왜 실패?")
    await run_turn(agent, "이거 왜 실패했어", session, state, attached=[item])
    system = agent.client.calls[0]["system"]
    assert "<attached-result-items>" in system[1]["text"] and "run-1" in system[1]["text"]
    assert "cache_control" in system[0]
