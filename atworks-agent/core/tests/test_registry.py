import json
from typing import get_args

from atworks_agent.catalog import catalog_hint
from atworks_agent.config import AtworksAgentConfig
from atworks_agent.tools.registry import build_tools
from atworks_agent.types import Dimension, Measure, QueryFilters, QuerySpec

EXPECTED = ["load_skill", "search_apis", "get_api", "list_runs", "get_run", "rank_failed_runs", "aggregate_runs",
            "query_runs",
            "get_pending_jobs", "stage_job", "apply_job", "discard_job",
            "stage_rule", "apply_rule", "discard_rule", "get_pending_rules",
            "find_apis_with_param", "recommend_rules_for_api",
            "stage_format_batch", "apply_format_batch", "discard_format_batch", "get_pending_format_batches",
            "stage_profile", "apply_profile", "discard_profile", "get_pending_profiles", "recommend_ignore_paths",
            "present_run_digest", "present_run_groups", "present_query_table", "present_job_preview", "present_rule_preview",
            "present_format_batch", "present_parity_summary", "present_profile_preview",
            "present_question_form", "navigate_screen", "highlight_screen", "present_suggestions"]


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


def test_parity_tools_present_when_enabled():
    names = [t["name"] for t in build_tools(AtworksAgentConfig(model="m", enable_parity=True), [])]
    assert {"stage_profile", "apply_profile", "discard_profile", "get_pending_profiles",
            "recommend_ignore_paths", "present_parity_summary", "present_profile_preview"} <= set(names)


def test_parity_switch_removes_all_seven_parity_tools():
    names = [t["name"] for t in build_tools(AtworksAgentConfig(model="m", enable_parity=False), [])]
    assert not {"stage_profile", "apply_profile", "discard_profile", "get_pending_profiles",
                "recommend_ignore_paths", "present_parity_summary", "present_profile_preview"} & set(names)


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


def test_registry_has_navigate_screen_with_view_enum_and_filter_shape():
    tools = {t["name"]: t for t in build_tools(AtworksAgentConfig(model="m"), [], ())}
    s = tools["navigate_screen"]["input_schema"]["properties"]
    assert s["view"]["enum"] == ["home", "apis", "runs", "jobs", "rules"]
    assert s["filter"]["properties"]["status"]["enum"] == ["all", "pass", "fail", "error"]


def test_navigate_screen_absent_when_screen_directives_disabled():
    names = [t["name"] for t in build_tools(AtworksAgentConfig(model="m", enable_screen_directives=False), [])]
    assert "navigate_screen" not in names


def test_registry_highlight_max_items_tracks_config():
    tools = {t["name"]: t for t in build_tools(AtworksAgentConfig(model="m", max_highlight_targets=3), [], ())}
    assert tools["highlight_screen"]["input_schema"]["properties"]["targets"]["maxItems"] == 3


def test_highlight_screen_absent_when_screen_directives_disabled():
    names = [t["name"] for t in build_tools(AtworksAgentConfig(model="m", enable_screen_directives=False), [])]
    assert "highlight_screen" not in names


def test_screen_directive_tools_pair_toggles_together():
    off_names = [t["name"] for t in build_tools(AtworksAgentConfig(model="m", enable_screen_directives=False), [])]
    assert "navigate_screen" not in off_names and "highlight_screen" not in off_names
    on_names = [t["name"] for t in build_tools(AtworksAgentConfig(model="m", enable_screen_directives=True), [])]
    assert "navigate_screen" in on_names and "highlight_screen" in on_names


# -- query_runs (self-growth spec §5) ----------------------------------------------------

def _tool(name, config=None):
    return next(t for t in build_tools(config or AtworksAgentConfig(model="m"), []) if t["name"] == name)


def test_query_runs_schema_carries_the_catalogue_enums():
    schema = _tool("query_runs")["input_schema"]
    assert schema["additionalProperties"] is False and schema["required"] == ["measures"]
    assert schema["properties"]["dimensions"]["items"]["enum"] == list(get_args(Dimension))
    assert schema["properties"]["measures"]["items"]["enum"] == list(get_args(Measure))
    assert schema["properties"]["order_by"]["enum"] == [*get_args(Measure), "key"]
    assert schema["properties"]["filters"]["additionalProperties"] is False


def test_query_runs_schema_mirrors_the_pydantic_models_field_for_field():
    # The schema is hand-written (flat, like every other tool here) rather than generated, so
    # this is the guard that it cannot drift away from what pydantic will actually accept.
    schema = _tool("query_runs")["input_schema"]
    properties = dict(schema["properties"])
    properties.pop("status", None)   # with_status' narration line, not a QuerySpec field
    assert set(properties) == set(QuerySpec.model_fields)
    assert set(schema["properties"]["filters"]["properties"]) == set(QueryFilters.model_fields)


def test_query_runs_description_carries_the_catalogue_and_two_examples():
    description = _tool("query_runs")["description"]
    assert catalog_hint() in description
    specs = [json.loads(line) for line in description.split("Examples:\n", 1)[1].strip().split("\n")]
    assert len(specs) == 2
    # Both examples must themselves be valid specs -- an example the model copies verbatim and
    # gets rejected for would teach it the wrong shape.
    for example in specs:
        example["filters"].pop("scope_operator", None)
        QuerySpec.model_validate(example)
    assert specs[0]["dimensions"] == ["path_segment_2"] and specs[1]["dimensions"] == ["day"]


def test_query_table_tool_takes_only_a_title_and_a_note():
    schema = _tool("present_query_table")["input_schema"]
    assert set(schema["properties"]) == {"title", "note"} and schema["required"] == ["title"]


def test_query_runs_switch_removes_both_tools():
    names = [t["name"] for t in build_tools(AtworksAgentConfig(model="m", enable_query_runs=False), [])]
    assert not {"query_runs", "present_query_table"} & set(names)
    assert "aggregate_runs" in names and "present_run_groups" in names


def test_query_tools_are_byte_stable_across_builds():
    # The whole tool list must stay a pure function of config (cache-stable prefix); the two
    # query tools carry the catalogue hint and two JSON examples, the most likely place for a
    # dict-order or float-repr wobble to creep in.
    a = json.dumps(build_tools(AtworksAgentConfig(model="m"), ["a"]), sort_keys=True, ensure_ascii=False)
    b = json.dumps(build_tools(AtworksAgentConfig(model="m"), ["a"]), sort_keys=True, ensure_ascii=False)
    assert a == b
    assert json.dumps(_tool("query_runs"), sort_keys=True) == json.dumps(_tool("query_runs"), sort_keys=True)
