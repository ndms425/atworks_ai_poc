from datetime import datetime

from commerce_common.skills import Skill, SkillRegistry

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.prompt import build_dynamic_context, build_static_system
from atworks_agent.types import AttachedItem

SKILLS = SkillRegistry([Skill(name="failed-triage", description="실패 triage", body="...")])


def test_static_prompt_is_byte_stable_and_leads_with_safety():
    cfg = AtworksAgentConfig(model="m")
    a, b = build_static_system(cfg, SKILLS), build_static_system(cfg, SKILLS)
    assert a == b
    assert a.index("# Hard lines") < a.index("# How you work") < a.index("# Skills")
    assert "never decide pass or fail" in a
    assert "population" in a
    assert "failed-triage" in a


def test_static_prompt_drops_job_rules_when_switched_off():
    text = build_static_system(AtworksAgentConfig(model="m", enable_jobs=False), SKILLS)
    assert "stage_job" not in text and "apply_job" not in text and "does not run or schedule" in text


def test_dynamic_context_carries_attachments_and_clock():
    item = AttachedItem(order=1, kind="run", ref_id="run-17", label="POST /x", comment="왜 실패?")
    text = build_dynamic_context(atworks_context={"project": "MES"}, attached_items=[item],
                                 now=datetime(2026, 9, 3, 14, 27))
    assert text.startswith("# aTworks context")
    assert "<atworks_data>" in text and '"project": "MES"' in text
    assert "<attached-result-items>" in text and "run-17" in text
    assert "2026-09-03T14:00" in text  # 시 단위 시계 (캐시 안정)


def test_dynamic_context_without_attachments_has_no_block():
    assert "<attached-result-items>" not in build_dynamic_context(atworks_context=None, attached_items=[], now=None)


def test_static_prompt_states_the_matrix_contract():
    text = build_static_system(AtworksAgentConfig(model="m"), SKILLS)
    assert "several target environments, several schedules and several test-data sets" in text
    assert "approval covers the whole matrix" in text
    assert "never facts about the system" in text
    assert "Never default target_envs to anything but [dev]" in text


def test_static_prompt_forbids_computing_group_figures():
    text = build_static_system(AtworksAgentConfig(model="m"), SKILLS)
    assert "aggregate_runs" in text and text.index("aggregate_runs") < text.index("# How you work")


def test_static_prompt_carries_the_rule_hard_line_before_how_you_work():
    text = build_static_system(AtworksAgentConfig(model="m"), SKILLS)
    hard_line = ("You draft validation rules as structured objects; you never judge a run against a rule "
                 "and you never change a past result. A rule applies only after a person approves it on "
                 "the Rules page, and only to runs executed after that.")
    assert hard_line in text
    assert text.index(hard_line) < text.index("# How you work")


def test_static_prompt_states_the_rule_contract():
    text = build_static_system(AtworksAgentConfig(model="m"), SKILLS)
    assert "stage_rule" in text and "apply_rule" in text
    assert "param must come from get_api" in text
    assert "prefer a named format over a raw pattern" in text


def test_static_prompt_drops_rule_rules_when_switched_off():
    text = build_static_system(AtworksAgentConfig(model="m", enable_rules=False), SKILLS)
    assert "stage_rule" not in text and "apply_rule" not in text
    assert "You draft validation rules as structured objects" not in text


def test_static_prompt_carries_the_format_example_reuse_bulk_hard_line():
    text = build_static_system(AtworksAgentConfig(model="m"), SKILLS)
    hard_line = ("When you author a format pattern you MUST supply pass and fail examples; the system "
                 "verifies the pattern against them before approval. Reuse a saved format by name instead "
                 "of re-authoring it. Bulk-add formats with stage_format_batch; duplicates are skipped.")
    assert hard_line in text
    assert text.index(hard_line) < text.index("# How you work")


def test_static_prompt_drops_format_hard_line_when_rules_switched_off():
    text = build_static_system(AtworksAgentConfig(model="m", enable_rules=False), SKILLS)
    assert "Bulk-add formats with stage_format_batch" not in text
