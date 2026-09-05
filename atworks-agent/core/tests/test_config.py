import pytest
from commerce_common.types import PROVENANCE_CAP

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


def test_max_apis_per_job_default_is_below_the_provenance_cap():
    default = AtworksAgentConfig(model="m").max_apis_per_job
    assert default == 100
    assert default < PROVENANCE_CAP


def test_matrix_caps_are_config_fields_with_defaults():
    cfg = AtworksAgentConfig(model="m")
    assert (cfg.max_target_envs_per_job, cfg.max_schedules_per_job,
            cfg.max_test_data_sets, cfg.max_matrix_size) == (2, 3, 5, 400)


def test_allowed_target_envs_default_includes_dev_stg_and_parity_targets():
    cfg = AtworksAgentConfig(model="m")
    assert set(cfg.allowed_target_envs) == {"dev", "stg", "legacy", "renewed"}
    assert cfg.target_endpoints == {}


def test_format_rule_caps_are_config_fields_with_defaults():
    cfg = AtworksAgentConfig(model="m")
    assert (cfg.min_format_examples, cfg.max_format_examples,
            cfg.max_format_library, cfg.max_format_batch) == (1, 8, 200, 30)
