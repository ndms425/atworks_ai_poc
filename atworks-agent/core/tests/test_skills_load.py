from pathlib import Path

from commerce_common.skills import SkillRegistry

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


def test_five_skills_load_with_index():
    reg = SkillRegistry.from_dir(SKILLS_DIR)
    assert reg.names == ["api-lookup", "failed-triage", "job-approval", "rule-authoring", "schedule-run"]
    assert "population" in reg.get_instructions("failed-triage")
    assert "present_question_form" in reg.get_instructions("schedule-run")


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


def test_schedule_run_skill_covers_envs_schedules_and_test_data():
    body = SkillRegistry.from_dir(SKILLS_DIR).get_instructions("schedule-run")
    assert "present_question_form" in body
    assert "양쪽" in body and "[dev, stg]" in body
    assert "test data" in body.lower() and "임의로" in body
    assert "one column per environment" in body
    approval = SkillRegistry.from_dir(SKILLS_DIR).get_instructions("job-approval")
    assert "every environment, schedule and data set" in approval
