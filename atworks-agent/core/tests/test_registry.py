import json

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.tools.registry import build_tools

EXPECTED = ["load_skill", "search_apis", "get_api", "list_runs", "get_run", "rank_failed_runs",
            "get_pending_jobs", "stage_job", "apply_job", "discard_job",
            "present_run_digest", "present_job_preview", "present_question_form", "present_suggestions"]


def test_fixed_order_and_status_field():
    tools = build_tools(AtworksAgentConfig(model="m"), ["failed-triage"])
    assert [t["name"] for t in tools] == EXPECTED
    assert "status" in next(t for t in tools if t["name"] == "search_apis")["input_schema"]["properties"]
    assert "status" not in next(t for t in tools if t["name"] == "present_run_digest")["input_schema"]["properties"]


def test_jobs_switch_removes_tools():
    names = [t["name"] for t in build_tools(AtworksAgentConfig(model="m", enable_jobs=False), [])]
    assert not {"stage_job", "apply_job", "discard_job", "get_pending_jobs", "present_job_preview"} & set(names)


def test_same_config_same_bytes():
    a = json.dumps(build_tools(AtworksAgentConfig(model="m"), ["a", "b"]), sort_keys=True)
    b = json.dumps(build_tools(AtworksAgentConfig(model="m"), ["b", "a"]), sort_keys=True)
    assert a == b


def test_stage_job_schema_has_confidence_and_assumptions():
    stage = next(t for t in build_tools(AtworksAgentConfig(model="m"), []) if t["name"] == "stage_job")
    props = stage["input_schema"]["properties"]
    assert {"kind", "summary", "api_ids", "target_env", "schedule", "binding", "confidence", "assumptions"} <= set(props)
    assert stage["input_schema"]["required"] == ["kind", "summary", "api_ids", "target_env"]
