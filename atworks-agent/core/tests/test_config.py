import pytest

from atworks_agent.config import AtworksAgentConfig


def test_unknown_field_is_rejected():
    with pytest.raises(ValueError):
        AtworksAgentConfig(model="claude-sonnet-4-5", not_a_field=1)


def test_thinking_fields_off_by_default_for_non_anthropic_models():
    assert AtworksAgentConfig(model="qwen/x").thinking_request_fields() == {}
    assert AtworksAgentConfig(model="claude-sonnet-4-5", send_thinking_fields=True, thinking_effort="low")\
        .thinking_request_fields()["thinking"] == {"type": "adaptive"}


def test_absent_tools_follow_switches():
    cfg = AtworksAgentConfig(model="m", enable_jobs=False)
    assert {"stage_job", "apply_job", "discard_job", "get_pending_jobs"} <= cfg.absent_tools()
    assert AtworksAgentConfig(model="m").absent_tools() == frozenset()
