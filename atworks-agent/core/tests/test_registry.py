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
    assert {"kind", "summary", "api_ids", "target_envs", "schedules", "test_data",
            "binding", "confidence", "assumptions"} <= set(props)
    assert stage["input_schema"]["required"] == ["kind", "summary", "api_ids", "target_envs"]


def test_stage_job_matrix_caps_come_from_config():
    cfg = AtworksAgentConfig(model="m", max_target_envs_per_job=4, max_schedules_per_job=2,
                             max_test_data_sets=1)
    props = next(t for t in build_tools(cfg, []) if t["name"] == "stage_job")["input_schema"]["properties"]
    assert props["target_envs"]["type"] == "array"
    assert props["target_envs"]["minItems"] == 1 and props["target_envs"]["maxItems"] == 4
    assert props["schedules"]["type"] == "array" and props["schedules"]["maxItems"] == 2
    assert props["test_data"]["type"] == "array" and props["test_data"]["maxItems"] == 1


def test_stage_job_test_data_items_are_bounded_and_closed():
    stage = next(t for t in build_tools(AtworksAgentConfig(model="m"), []) if t["name"] == "stage_job")
    item = stage["input_schema"]["properties"]["test_data"]["items"]
    assert item["additionalProperties"] is False
    assert item["required"] == ["label", "values"]
    assert item["properties"]["label"]["maxLength"] == 40
    assert item["properties"]["values"]["additionalProperties"] == {"type": "string", "maxLength": 200}


def test_list_runs_filters_are_nested():
    list_runs = next(t for t in build_tools(AtworksAgentConfig(model="m"), []) if t["name"] == "list_runs")
    props = list_runs["input_schema"]["properties"]
    assert set(props) == {"status", "filters", "limit"}
    assert props["filters"]["properties"]["status"]["enum"] == ["pass", "fail", "error", "non_pass"]


def test_stage_job_select_where_schema_forbids_extra_properties():
    stage = next(t for t in build_tools(AtworksAgentConfig(model="m"), []) if t["name"] == "stage_job")
    select_where = stage["input_schema"]["properties"]["select_where"]
    assert select_where["additionalProperties"] is False
    assert set(select_where["properties"]) == {"query", "group", "updated_after"}
