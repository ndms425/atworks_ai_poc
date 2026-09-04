from pathlib import Path

from commerce_common.skills import SkillRegistry

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


def test_four_skills_load_with_index():
    reg = SkillRegistry.from_dir(SKILLS_DIR)
    assert reg.names == ["api-lookup", "failed-triage", "job-approval", "schedule-run"]
    assert "population" in reg.get_instructions("failed-triage")
    assert "present_question_form" in reg.get_instructions("schedule-run")


def test_schedule_run_skill_covers_envs_schedules_and_test_data():
    body = SkillRegistry.from_dir(SKILLS_DIR).get_instructions("schedule-run")
    assert "present_question_form" in body
    assert "양쪽" in body and "[dev, stg]" in body
    assert "test data" in body.lower() and "임의로" in body
    assert "one column per environment" in body
    approval = SkillRegistry.from_dir(SKILLS_DIR).get_instructions("job-approval")
    assert "every environment, schedule and data set" in approval
