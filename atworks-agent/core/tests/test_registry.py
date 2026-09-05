import json

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.tools.registry import build_tools

EXPECTED = ["load_skill", "search_apis", "get_api", "list_runs", "get_run", "rank_failed_runs", "aggregate_runs",
            "get_pending_jobs", "stage_job", "apply_job", "discard_job",
            "stage_rule", "apply_rule", "discard_rule", "get_pending_rules",
            "find_apis_with_param", "recommend_rules_for_api",
            "stage_format_batch", "apply_format_batch", "discard_format_batch", "get_pending_format_batches",
            "present_run_digest", "present_run_groups", "present_job_preview", "present_rule_preview",
            "present_format_batch", "present_question_form", "present_suggestions"]


def test_fixed_order_and_status_field():
    tools = build_tools(AtworksAgentConfig(model="m"), ["failed-triage"])
    assert [t["name"] for t in tools] == EXPECTED
    assert "status" in next(t for t in tools if t["name"] == "search_apis")["input_schema"]["properties"]
    assert "status" not in next(t for t in tools if t["name"] == "present_run_digest")["input_schema"]["properties"]


def test_jobs_switch_removes_tools():
    names = [t["name"] for t in build_tools(AtworksAgentConfig(model="m", enable_jobs=False), [])]
    assert not {"stage_job", "apply_job", "discard_job", "get_pending_jobs", "present_job_preview"} & set(names)


def test_rules_switch_removes_tools():
    names = [t["name"] for t in build_tools(AtworksAgentConfig(model="m", enable_rules=False), [])]
    assert not {"stage_rule", "apply_rule", "discard_rule", "get_pending_rules", "present_rule_preview"} & set(names)


def test_rules_switch_removes_format_batch_tools_too():
    names = [t["name"] for t in build_tools(AtworksAgentConfig(model="m", enable_rules=False), [])]
    assert not {"stage_format_batch", "apply_format_batch", "discard_format_batch",
                "get_pending_format_batches", "present_format_batch"} & set(names)


def test_rules_switch_removes_recommendation_tools_too():
    names = [t["name"] for t in build_tools(AtworksAgentConfig(model="m", enable_rules=False), [])]
    assert not {"find_apis_with_param", "recommend_rules_for_api"} & set(names)


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


def test_stage_rule_schema_has_enums_from_config_and_is_closed():
    cfg = AtworksAgentConfig(model="m")
    stage = next(t for t in build_tools(cfg, []) if t["name"] == "stage_rule")
    props = stage["input_schema"]["properties"]
    assert {"api_id", "param", "kind", "op", "value", "values", "format", "pattern",
            "pass_examples", "fail_examples", "save_format_as",
            "summary", "confidence", "assumptions"} <= set(props)
    assert props["kind"]["enum"] == list(cfg.allowed_rule_kinds)
    assert props["op"]["enum"] == list(cfg.allowed_compare_ops) + ["in", "not_in"]
    assert props["format"]["type"] == "string" and "enum" not in props["format"]
    assert props["values"]["maxItems"] == cfg.max_membership_values
    assert stage["input_schema"]["additionalProperties"] is False
    assert stage["input_schema"]["required"] == ["api_id", "param", "kind", "summary"]


def test_stage_rule_membership_cap_comes_from_config():
    cfg = AtworksAgentConfig(model="m", max_membership_values=3)
    props = next(t for t in build_tools(cfg, []) if t["name"] == "stage_rule")["input_schema"]["properties"]
    assert props["values"]["maxItems"] == 3


def test_stage_rule_format_examples_cap_comes_from_config():
    cfg = AtworksAgentConfig(model="m", max_format_examples=4)
    props = next(t for t in build_tools(cfg, []) if t["name"] == "stage_rule")["input_schema"]["properties"]
    assert props["pass_examples"]["maxItems"] == 4
    assert props["fail_examples"]["maxItems"] == 4


def test_list_runs_filters_are_nested():
    list_runs = next(t for t in build_tools(AtworksAgentConfig(model="m"), []) if t["name"] == "list_runs")
    props = list_runs["input_schema"]["properties"]
    assert set(props) == {"status", "filters", "limit"}
    assert props["filters"]["properties"]["status"]["enum"] == ["pass", "fail", "error", "non_pass"]


def test_stage_job_select_where_schema_forbids_extra_properties():
    stage = next(t for t in build_tools(AtworksAgentConfig(model="m"), []) if t["name"] == "stage_job")
    select_where = stage["input_schema"]["properties"]["select_where"]
    assert select_where["additionalProperties"] is False
    assert set(select_where["properties"]) == {"query", "group", "updated_after", "failed_since", "related_to"}


def test_aggregate_runs_schema_lists_the_axes():
    tool = next(t for t in build_tools(AtworksAgentConfig(model="m"), []) if t["name"] == "aggregate_runs")
    props = tool["input_schema"]["properties"]
    assert props["group_by"]["enum"] == ["api", "failed_rule", "http_status", "env", "api_env_data"]
    assert tool["input_schema"]["required"] == ["group_by"]


def test_stage_format_batch_schema_is_bounded_and_closed():
    cfg = AtworksAgentConfig(model="m", max_format_batch=7, max_format_examples=3)
    stage = next(t for t in build_tools(cfg, []) if t["name"] == "stage_format_batch")
    props = stage["input_schema"]["properties"]
    assert stage["input_schema"]["additionalProperties"] is False
    assert stage["input_schema"]["required"] == ["formats", "summary"]
    assert props["formats"]["type"] == "array" and props["formats"]["maxItems"] == 7
    item = props["formats"]["items"]
    assert item["additionalProperties"] is False
    assert item["required"] == ["name", "pattern"]
    assert set(item["properties"]) == {"name", "pattern", "pass_examples", "fail_examples"}
    assert item["properties"]["pass_examples"]["maxItems"] == 3
    assert item["properties"]["fail_examples"]["maxItems"] == 3


def test_apply_discard_get_format_batch_tools_shapes():
    tools = build_tools(AtworksAgentConfig(model="m"), [])
    apply_tool = next(t for t in tools if t["name"] == "apply_format_batch")
    discard_tool = next(t for t in tools if t["name"] == "discard_format_batch")
    get_tool = next(t for t in tools if t["name"] == "get_pending_format_batches")
    assert apply_tool["input_schema"]["required"] == ["batch_id"]
    assert discard_tool["input_schema"]["required"] == ["batch_id"]
    assert set(get_tool["input_schema"]["properties"]) == {"status"}


def test_find_apis_with_param_and_recommend_rules_for_api_schemas():
    tools = build_tools(AtworksAgentConfig(model="m"), [])
    find_tool = next(t for t in tools if t["name"] == "find_apis_with_param")
    recommend_tool = next(t for t in tools if t["name"] == "recommend_rules_for_api")
    assert find_tool["input_schema"]["required"] == ["param"]
    assert find_tool["input_schema"]["additionalProperties"] is False
    assert recommend_tool["input_schema"]["required"] == ["api_id"]
    assert recommend_tool["input_schema"]["additionalProperties"] is False


def test_present_format_batch_schema():
    tool = next(t for t in build_tools(AtworksAgentConfig(model="m"), []) if t["name"] == "present_format_batch")
    props = tool["input_schema"]["properties"]
    assert set(props) == {"batch_id", "headline", "note"}
    assert tool["input_schema"]["required"] == ["batch_id"]
