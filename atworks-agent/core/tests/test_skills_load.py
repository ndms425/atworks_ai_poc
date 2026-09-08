from pathlib import Path

from commerce_common.skills import SkillRegistry

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


def test_six_skills_load_with_index():
    reg = SkillRegistry.from_dir(SKILLS_DIR)
    assert reg.names == ["api-lookup", "failed-triage", "job-approval", "parity-compare", "rule-authoring", "schedule-run"]
    assert "population" in reg.get_instructions("failed-triage")
    assert "present_question_form" in reg.get_instructions("schedule-run")


def test_parity_compare_skill_loads_and_appears_in_index():
    reg = SkillRegistry.from_dir(SKILLS_DIR)
    assert "parity-compare" in reg.names
    assert "parity-compare" in reg.index_block()
    body = reg.get_instructions("parity-compare")
    assert "stage_job" in body and "stage_profile" in body and "present_profile_preview" in body
    assert "recommend_ignore_paths" in body and "present_parity_summary" in body
    assert "apply_profile" in body and "re-diff" in body


def test_rule_authoring_skill_covers_classification_and_staging():
    body = SkillRegistry.from_dir(SKILLS_DIR).get_instructions("rule-authoring")
    assert "get_api" in body and "stage_rule" in body
    assert "compare" in body and "membership" in body and "required" in body and "format" in body
    assert "Rules page" in body


def test_rule_authoring_skill_covers_examples_bulk_add_and_reuse():
    body = SkillRegistry.from_dir(SKILLS_DIR).get_instructions("rule-authoring")
    assert "pass_examples" in body and "fail_examples" in body
    assert "stage_format_batch" in body and "present_format_batch" in body
    assert "duplicate" in body
    assert "Formats page" in body
    assert "saved format" in body or "library" in body


def test_triage_and_lookup_skills_carry_the_beyond_the_fixed_axes_rules():
    # spec §5 세 규칙: 축 밖이면 query_runs / 카탈로그에도 없으면 note_unmet_ask 먼저 /
    # 사용자 용어를 필터 값으로 읽었으면 한 절로 말한다.
    reg = SkillRegistry.from_dir(SKILLS_DIR)
    for name in ("failed-triage", "api-lookup"):
        body = reg.get_instructions(name)
        assert "## Beyond the fixed axes" in body, name
        assert "query_runs" in body and "present_query_table" in body, name
        assert "note_unmet_ask(reason, summary, wanted)" in body, name
        assert "지원하지 않습니다" in body and "rule violation" in body, name
        assert "path_prefix" in body, name


def test_schedule_run_skill_covers_envs_schedules_and_test_data():
    body = SkillRegistry.from_dir(SKILLS_DIR).get_instructions("schedule-run")
    assert "present_question_form" in body
    assert "양쪽" in body and "[dev, stg]" in body
    assert "test data" in body.lower() and "임의로" in body
    assert "one column per environment" in body
    approval = SkillRegistry.from_dir(SKILLS_DIR).get_instructions("job-approval")
    assert "every environment, schedule and data set" in approval
