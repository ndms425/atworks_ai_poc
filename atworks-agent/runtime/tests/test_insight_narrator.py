"""narrate_insights: 단발 tool-forced 호출, fenced candidate, 실패 열림. 가짜 클라이언트로
messages.create를 스크립트한다 — 스트리밍이 아니라 create 한 번이므로 conftest의 FakeClient(스트리밍용)
는 쓰지 않고, 이 파일 전용의 아주 작은 fake를 둔다."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.fencing import ATWORKS_FENCE
from atworks_agent.types import InsightCandidate
from atworks_agent_runtime.insight_narrator import narrate_insights


class FakeCreateClient:
    """A stand-in for AsyncAnthropic exposing only messages.create, scripted per test."""

    def __init__(self, handler):
        self._handler = handler
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return await self._handler(**kwargs)


def _tool_use_response(items: list[dict[str, Any]]) -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="tool_use", name="submit_insights", input={"items": items})
        ]
    )


def _candidate(candidate_id: str) -> InsightCandidate:
    return InsightCandidate(
        candidate_id=candidate_id,
        kind="top_failed_rule",
        label=f"label · {candidate_id}",
        figures={"fail": 3},
        api_ids=["api-1"],
        ref_ids=["run-1"],
    )


@pytest.fixture
def config() -> AtworksAgentConfig:
    return AtworksAgentConfig(model="m")


async def test_narrates_known_candidates_and_drops_unknown_with_note(config):
    known_a = _candidate("top_failed_rule:a")
    known_b = _candidate("top_failed_rule:b")
    candidates = [known_a, known_b]

    async def handler(**kwargs: Any) -> Any:
        return _tool_use_response([
            {"candidate_id": "top_failed_rule:a", "headline": "h1", "why_it_matters": "w1", "prompt": "p1"},
            {"candidate_id": "top_failed_rule:b", "headline": "h2", "why_it_matters": "w2", "prompt": "p2"},
            {"candidate_id": "top_failed_rule:unknown", "headline": "h3", "why_it_matters": "w3", "prompt": "p3"},
        ])

    client = FakeCreateClient(handler)
    notes: list[str] = []
    result = await narrate_insights(client, config, candidates, "developer", notes=notes)

    assert {n.candidate_id for n in result} == {"top_failed_rule:a", "top_failed_rule:b"}
    assert len(result) == 2
    assert any("top_failed_rule:unknown" in note for note in notes)


async def test_disabled_narration_makes_no_call(config):
    config = config.model_copy(update={"enable_insight_narration": False})
    candidates = [_candidate("top_failed_rule:a")]

    async def handler(**kwargs: Any) -> Any:
        raise AssertionError("must not be called when narration is disabled")

    client = FakeCreateClient(handler)
    result = await narrate_insights(client, config, candidates, "developer")

    assert result == []
    assert client.calls == []


async def test_exception_and_timeout_return_empty(config):
    candidates = [_candidate("top_failed_rule:a")]

    async def raising_handler(**kwargs: Any) -> Any:
        raise RuntimeError("boom")

    client = FakeCreateClient(raising_handler)
    result = await narrate_insights(client, config, candidates, "developer")
    assert result == []

    config_short_timeout = config.model_copy(update={"insight_narration_timeout_s": 1})

    async def slow_handler(**kwargs: Any) -> Any:
        await asyncio.sleep(2)
        return _tool_use_response([])

    slow_client = FakeCreateClient(slow_handler)
    result = await narrate_insights(slow_client, config_short_timeout, candidates, "developer")
    assert result == []


async def test_overlong_headline_is_rejected_not_truncated_silently(config):
    known_a = _candidate("top_failed_rule:a")
    known_b = _candidate("top_failed_rule:b")
    candidates = [known_a, known_b]
    overlong_headline = "h" * 81

    async def handler(**kwargs: Any) -> Any:
        return _tool_use_response([
            {"candidate_id": "top_failed_rule:a", "headline": overlong_headline, "why_it_matters": "w1", "prompt": "p1"},
            {"candidate_id": "top_failed_rule:b", "headline": "fine", "why_it_matters": "w2", "prompt": "p2"},
        ])

    client = FakeCreateClient(handler)
    notes: list[str] = []
    result = await narrate_insights(client, config, candidates, "developer", notes=notes)

    assert [n.candidate_id for n in result] == ["top_failed_rule:b"]
    assert any("top_failed_rule:a" in note for note in notes)


async def test_request_forces_the_tool_and_fences_candidates(config):
    candidate = _candidate("top_failed_rule:a")

    async def handler(**kwargs: Any) -> Any:
        return _tool_use_response([
            {"candidate_id": "top_failed_rule:a", "headline": "h", "why_it_matters": "w", "prompt": "p"},
        ])

    client = FakeCreateClient(handler)
    await narrate_insights(client, config, [candidate], "developer")

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "submit_insights"}
    assert call["tools"][0]["name"] == "submit_insights"
    assert "do not restate" in call["system"].lower()
    user_content = call["messages"][0]["content"]
    assert "top_failed_rule:a" in user_content
    assert ATWORKS_FENCE.open in user_content
    assert ATWORKS_FENCE.close in user_content
