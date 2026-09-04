from pathlib import Path

from commerce_common.skills import SkillRegistry

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


def test_four_skills_load_with_index():
    reg = SkillRegistry.from_dir(SKILLS_DIR)
    assert reg.names == ["api-lookup", "failed-triage", "job-approval", "schedule-run"]
    assert "population" in reg.get_instructions("failed-triage")
    assert "present_question_form" in reg.get_instructions("schedule-run")
