import json
from datetime import UTC, datetime

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.executor import AtworksToolExecutor
from atworks_agent.types import ApiSpec

from .conftest import T0


def _exec(backend, config, skills, session, state):
    return AtworksToolExecutor(backend=backend, config=config, skills=skills, session=session, state=state)


def _payload(outcome):
    body = outcome.result_text.split("<atworks_data>", 1)[1].split("</atworks_data>", 1)[0]
    return json.loads(body)


async def test_search_apis_records_provenance(backend, config, skills, session, state):
    out = await _exec(backend, config, skills, session, state).execute("search_apis", {"query": "contracts"})
    assert not out.refused and set(state.seen_apis) == {"api-1", "api-2"}


async def test_list_runs_records_population_and_runs(backend, config, skills, session, state):
    out = await _exec(backend, config, skills, session, state).execute("list_runs", {"filters": {"status": "fail"}})
    assert not out.refused and state.last_population == 1 and "run-1" in state.seen_runs


async def test_list_runs_non_pass_population(backend, config, skills, session, state):
    out = await _exec(backend, config, skills, session, state).execute("list_runs", {"filters": {"status": "non_pass"}})
    assert not out.refused and state.last_population == 2
    assert {"run-1", "run-2"} <= set(state.seen_runs)


async def test_list_runs_status_narration_is_stripped_not_filter(backend, config, skills, session, state):
    out = await _exec(backend, config, skills, session, state).execute(
        "list_runs", {"status": "looking up runs", "filters": {"status": "fail"}}
    )
    assert not out.refused and state.last_population == 1


async def test_list_runs_rejects_unknown_status_filter(backend, config, skills, session, state):
    out = await _exec(backend, config, skills, session, state).execute(
        "list_runs", {"filters": {"status": "<b>everything</b>"}}
    )
    assert out.is_error and "filters.status" in out.result_text and "unavailable" not in out.result_text
    assert state.last_listed_filter == "all" and state.last_population is None


async def test_rank_needs_runs_first_then_ranks(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("rank_failed_runs", {"scorer": "risk_v1"})
    assert out.is_error and "list_runs" in out.result_text
    await ex.execute("list_runs", {})
    out = await ex.execute("rank_failed_runs", {"scorer": "risk_v1", "limit": 5})
    ranks = _payload(out)["ranked"]
    assert [r["run_id"] for r in ranks] == ["run-2", "run-1"] and state.seen_ranks


async def test_rank_uses_last_listed_window(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("list_runs", {})
    await ex.execute("list_runs", {"filters": {"status": "error"}})
    out = await ex.execute("rank_failed_runs", {"scorer": "risk_v1"})
    ranks = _payload(out)["ranked"]
    assert [r["run_id"] for r in ranks] == ["run-2"] and state.last_population == 1


async def test_stage_job_holds_unknown_api_then_stages_with_preview(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    draft = {"kind": "run_now", "summary": "run contracts", "api_ids": ["api-1"], "target_envs": ["dev"],
             "confidence": {"target_envs": 0.3}, "assumptions": ["target_env defaulted to dev"]}
    held = await ex.execute("stage_job", draft)
    assert held.blocked == "provenance"
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", draft)
    assert not out.refused
    kinds = [(e.type, e.data.get("component")) for e in out.events]
    assert kinds == [("change_update", None), ("ui", "job_preview")]
    job_id = next(iter(state.seen_jobs))
    assert out.events[1].data["payload"]["low_confidence"] == ["target_envs"]
    assert "Staged, and shown" in out.result_text and job_id == "job-0001"


async def test_stage_job_truncates_an_oversized_target_env_then_the_guardrail_blocks_it(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"],
                                          "target_envs": ["x" * 200]})
    assert out.blocked == "guardrail"
    assert "not allowed targets" in out.result_text
    assert "x" * 33 not in out.result_text   # sanitized to 32 chars before the message is built


async def test_stage_job_guardrail_prod(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"], "target_envs": ["prod"]})
    assert out.blocked == "guardrail" and "prod" in out.result_text


async def test_apply_requires_host_mark(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"], "target_envs": ["dev"]})
    held = await ex.execute("apply_job", {"job_id": "job-0001"})
    assert held.blocked == "approval"
    state.approved_job_ids.add("job-0001")
    out = await ex.execute("apply_job", {"job_id": "job-0001"})
    assert not out.refused and state.seen_jobs["job-0001"].status.value == "applied"
    assert out.events[0].type == "change_update"


async def test_unknown_tool_is_refused(backend, config, skills, session, state):
    out = await _exec(backend, config, skills, session, state).execute("drop_database", {})
    assert out.is_error


async def test_search_apis_bad_updated_after_names_field(backend, config, skills, session, state):
    out = await _exec(backend, config, skills, session, state).execute(
        "search_apis", {"query": "", "updated_after": "not-a-date"}
    )
    assert out.is_error
    assert "updated_after" in out.result_text
    assert "unavailable" not in out.result_text


async def test_list_runs_naive_since_does_not_outage(backend, config, skills, session, state):
    out = await _exec(backend, config, skills, session, state).execute(
        "list_runs", {"filters": {"since": "2026-09-01"}}
    )
    assert not out.refused


async def test_list_runs_records_last_listed_filter(backend, config, skills, session, state):
    out = await _exec(backend, config, skills, session, state).execute(
        "list_runs", {"filters": {"status": "non_pass"}}
    )
    assert not out.refused
    assert state.last_listed_filter == "non_pass"


async def test_list_runs_bad_limit_names_field(backend, config, skills, session, state):
    out = await _exec(backend, config, skills, session, state).execute(
        "list_runs", {"filters": {"since": "2026-09-01"}, "limit": "abc"}
    )
    assert out.is_error
    assert "limit" in out.result_text
    assert "unavailable" not in out.result_text


async def test_stage_job_guardrail_checked_before_backend_call(backend, config, skills, session, state):
    class PermissiveBackend(backend.__class__):
        async def stage_job(self, session, draft, actor_kind):
            from datetime import UTC, datetime

            from atworks_agent.types import ActorKind as AK
            from atworks_agent.types import JobSpec
            return JobSpec(job_id="job-bypass", kind=draft.kind, summary=draft.summary,
                           api_ids=draft.api_ids, target_envs=draft.target_envs,
                           created_at=datetime.now(UTC), created_by=session.operator,
                           created_by_kind=AK.AGENT)

    permissive = PermissiveBackend(config)
    ex = _exec(permissive, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"], "target_envs": ["prod"]})
    assert out.blocked == "guardrail"


async def test_stage_job_dedupes_api_ids(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1", "api-1"], "target_envs": ["dev"]})
    assert not out.refused
    job_id = next(iter(state.seen_jobs))
    assert state.seen_jobs[job_id].api_ids == ["api-1"]


async def test_stage_job_rejects_select_where_with_unknown_field(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"], "target_envs": ["dev"],
                                          "binding": "LATE", "select_where": {"query": "x", "evil": 1}})
    assert out.is_error
    assert "unavailable" not in out.result_text


async def test_stage_job_related_to_resolves_the_selection_server_side(backend, config, skills, session, state):
    backend.apis["api-3"] = ApiSpec(api_id="api-3", method="GET", path="/v1/contracts/{id}/history", name="이력", group="contract", updated_at=T0, has_rules=False)
    ex = AtworksToolExecutor(backend=backend, config=config, skills=skills, session=session, state=state)
    await ex.execute("get_api", {"api_id": "api-1"})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "관련 재실행", "api_ids": ["api-1"],
                                         "target_envs": ["dev"], "select_where": {"related_to": "api-1"}})
    assert not out.is_error, out.result_text
    job = next(iter(state.seen_jobs.values()))
    assert set(job.api_ids) == {"api-1", "api-2", "api-3"}
    assert job.selection_basis and "api-1" in job.selection_basis
    assert {"api-2", "api-3"} <= set(state.seen_apis)             # resolved specs are remembered


async def test_stage_job_related_to_must_be_a_seen_api(backend, config, skills, session, state):
    ex = AtworksToolExecutor(backend=backend, config=config, skills=skills, session=session, state=state)
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "x", "api_ids": ["api-1"],
                                         "target_envs": ["dev"], "select_where": {"related_to": "api-1"}})
    assert out.blocked == "provenance"


async def test_stage_job_failed_since_with_no_match_is_a_named_argument_error(backend, config, skills, session, state):
    ex = AtworksToolExecutor(backend=backend, config=config, skills=skills, session=session, state=state)
    await ex.execute("get_api", {"api_id": "api-1"})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "x", "api_ids": ["api-1"], "target_envs": ["dev"],
                                         "select_where": {"failed_since": "2030-01-01T00:00:00+00:00"}})
    assert out.is_error and "select_where" in out.result_text and "no API" in out.result_text


async def test_get_run_on_fresh_state_seeds_attached_population(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("get_run", {"run_id": "run-1"})
    assert not out.refused
    assert state.last_population == 1
    assert state.last_listed_run_ids == ["run-1"]
    assert state.last_listed_filter == "attached"

    digest = await ex.execute("present_run_digest", {"items": [{"kind": "fail", "ref_id": "run-1", "headline": "h"}]})
    assert not digest.refused
    payload = digest.events[0].data["payload"]
    assert payload["population"] == 1
    assert payload["population_filter"] == "attached"


async def test_get_run_of_a_pass_run_does_not_grow_a_non_pass_population(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("list_runs", {"filters": {"status": "non_pass"}})
    assert state.last_population == 2

    out = await ex.execute("get_run", {"run_id": "run-3"})  # run-3 is a PASS run
    assert not out.refused
    assert state.last_population == 2
    assert "run-3" not in state.last_listed_run_ids
    assert "run-3" in state.seen_runs


async def test_stage_job_accepts_select_where_group_only(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"], "target_envs": ["dev"],
                                          "binding": "LATE", "select_where": {"group": "contract"}})
    assert not out.refused
    job_id = next(iter(state.seen_jobs))
    assert state.seen_jobs[job_id].select_where == {"query": "", "group": "contract"}


async def test_stage_job_sanitizes_and_dedupes_target_envs(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"],
                                          "target_envs": ["dev", "dev", "stg"]})
    assert not out.refused
    job_id = next(iter(state.seen_jobs))
    assert state.seen_jobs[job_id].target_envs == ["dev", "stg"]


async def test_stage_job_binds_test_data_and_previews_the_matrix(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {
        "kind": "scheduled_run", "summary": "dev/stg 비교", "api_ids": ["api-1"],
        "target_envs": ["dev", "stg"],
        "schedules": [{"kind": "daily", "at": "09:00", "from_date": "2026-09-05", "count": 3}],
        "test_data": [{"label": "S1 정상", "values": {"amount": "1000"}},
                      {"label": "S2 음수 금액", "values": {"amount": "-1"}}],
        "confidence": {"target_envs": 0.3, "test_data": 0.4},
    })
    assert not out.refused
    payload = out.events[1].data["payload"]
    assert payload["matrix"] == {
        "apis": 1, "envs": ["dev", "stg"], "data_sets": ["S1 정상", "S2 음수 금액"],
        "executions": 3, "runs_per_execution": 4, "runs_total": 12,
        "max_runs_per_execution": 4, "max_runs_total": 12,
    }
    assert payload["low_confidence"] == ["target_envs", "test_data"]


async def test_stage_job_rejects_a_test_data_key_no_selected_api_declares(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"],
                                          "target_envs": ["dev"],
                                          "test_data": [{"label": "S1", "values": {"nope": "1"}}]})
    assert out.blocked == "guardrail"
    assert "test data set(s) S1" in out.result_text


async def test_stage_job_matrix_guardrail_checked_before_backend_call(backend, config, skills, session, state):
    class PermissiveBackend(backend.__class__):
        async def stage_job(self, session, draft, actor_kind):
            from datetime import UTC, datetime

            from atworks_agent.types import ActorKind as AK
            from atworks_agent.types import JobSpec
            return JobSpec(job_id="job-bypass", kind=draft.kind, summary=draft.summary,
                           api_ids=draft.api_ids, target_envs=draft.target_envs,
                           created_at=datetime.now(UTC), created_by=session.operator,
                           created_by_kind=AK.AGENT)

    tight = config.model_copy(update={"max_matrix_size": 1})
    ex = _exec(PermissiveBackend(tight), tight, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s",
                                          "api_ids": ["api-1", "api-2"], "target_envs": ["dev", "stg"]})
    assert out.blocked == "guardrail" and "4 runs per execution" in out.result_text
    assert "job-bypass" not in state.seen_jobs


async def test_stage_job_drops_unknown_confidence_keys(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"],
                                          "target_envs": ["dev"],
                                          "confidence": {"target_envs": 0.3, "<script>evil": 0.1}})
    assert not out.refused
    job_id = next(iter(state.seen_jobs))
    assert state.seen_jobs[job_id].confidence == {"target_envs": 0.3}


async def test_stage_job_names_a_non_dict_test_data_values_shape(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"],
                                          "target_envs": ["dev"],
                                          "test_data": [{"label": "S1", "values": "amount=1000"}]})
    assert out.is_error
    assert "test_data.values" in out.result_text
    assert "unavailable" not in out.result_text


async def test_stage_job_names_a_non_dict_confidence_shape(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"],
                                          "target_envs": ["dev"], "confidence": "high"})
    assert out.is_error
    assert "confidence" in out.result_text
    assert "unavailable" not in out.result_text


async def test_open_question_form_blocks_staging_this_turn(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    form = await ex.execute("present_question_form", {
        "id": "target-env", "title": "대상 환경",
        "questions": [{"id": "target_envs", "label": "대상 환경", "type": "radio", "why": "target_envs가 없음",
                       "default": "dev", "options": ["dev", "stg"]}],
    })
    assert not form.refused
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"], "target_envs": ["dev"]})
    assert out.blocked == "question_form"

    fresh = _exec(backend, config, skills, session, state)
    await fresh.execute("search_apis", {"query": ""})
    fresh_out = await fresh.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"], "target_envs": ["dev"]})
    assert not fresh_out.refused


async def test_preview_job_once_per_turn_then_a_different_job_still_renders(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    staged = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"], "target_envs": ["dev"]})
    assert not staged.refused
    kinds = [(e.type, e.data.get("component")) for e in staged.events]
    assert kinds == [("change_update", None), ("ui", "job_preview")]

    repeat = await ex.execute("present_job_preview", {"job_id": "job-0001"})
    assert not repeat.refused
    assert repeat.events == []
    assert repeat.result_text == ex.displayed_text

    other = await ex.execute("stage_job", {"kind": "run_now", "summary": "s2", "api_ids": ["api-1"], "target_envs": ["dev"]})
    assert not other.refused
    other_kinds = [(e.type, e.data.get("component")) for e in other.events]
    assert other_kinds == [("change_update", None), ("ui", "job_preview")]


async def test_aggregate_runs_records_groups_population_and_axis(backend, config, skills, session, state):
    ex = AtworksToolExecutor(backend=backend, config=config, skills=skills, session=session, state=state)
    out = await ex.execute("aggregate_runs", {"group_by": "api"})
    assert not out.is_error
    assert state.last_population == 3 and state.last_group_by == "api"
    assert set(state.seen_groups) == {"api:api-1", "api:api-2"}
    assert state.seen_groups["api:api-1"].fail == 1
    assert "api-1" in out.result_text and "population" in out.result_text


async def test_aggregate_runs_clamps_since_to_the_config_window(backend, config, skills, session, state):
    ex = AtworksToolExecutor(backend=backend, config=config, skills=skills, session=session, state=state)
    out = await ex.execute("aggregate_runs", {"group_by": "env", "since": "2000-01-01T00:00:00+00:00"})
    assert not out.is_error
    assert state.last_aggregate_since is not None
    assert (datetime.now(UTC) - state.last_aggregate_since).days in (config.max_aggregate_window_days - 1, config.max_aggregate_window_days)


async def test_aggregate_runs_rejects_an_api_id_not_seen_this_session(backend, config, skills, session, state):
    ex = AtworksToolExecutor(backend=backend, config=config, skills=skills, session=session, state=state)
    out = await ex.execute("aggregate_runs", {"group_by": "api", "api_id": "api-1"})
    assert out.is_error and "search_apis" in out.result_text
    await ex.execute("get_api", {"api_id": "api-1"})
    out = await ex.execute("aggregate_runs", {"group_by": "api", "api_id": "api-1"})
    assert not out.is_error and set(state.seen_groups) == {"api:api-1"}


async def test_aggregate_runs_rejects_an_unknown_axis(backend, config, skills, session, state):
    ex = AtworksToolExecutor(backend=backend, config=config, skills=skills, session=session, state=state)
    out = await ex.execute("aggregate_runs", {"group_by": "moon"})
    assert out.is_error and "group_by" in out.result_text


async def test_aggregate_runs_keeps_the_listed_window_coherent(backend, config, skills, session, state):
    ex = AtworksToolExecutor(backend=backend, config=config, skills=skills, session=session, state=state)
    await ex.execute("list_runs", {"filters": {"status": "non_pass"}})
    out = await ex.execute("aggregate_runs", {"group_by": "api"})
    assert not out.is_error
    assert state.last_listed_run_ids == ["run-3", "run-2", "run-1"]
    assert state.last_population == 3


async def test_aggregate_runs_population_counts_the_true_total_and_flags_truncation(backend, skills, session, state):
    small_config = AtworksAgentConfig(model="m", max_aggregate_runs=2)
    ex = AtworksToolExecutor(backend=backend, config=small_config, skills=skills, session=session, state=state)
    out = await ex.execute("aggregate_runs", {"group_by": "api"})
    assert not out.is_error
    assert state.last_population == 3
    assert '"truncated": true' in out.result_text
    assert len(state.last_listed_run_ids) == 2


async def test_stage_rule_holds_unknown_api_then_stages(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    draft = {"api_id": "api-1", "param": "amount", "kind": "compare", "op": ">=", "value": "0",
             "summary": "amount must be non-negative"}
    held = await ex.execute("stage_rule", draft)
    assert held.blocked == "provenance"
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_rule", draft)
    assert not out.refused
    kinds = [(e.type, e.data.get("component")) for e in out.events]
    assert kinds == [("change_update", None), ("ui", "rule_preview")]
    rule_id = next(iter(state.seen_rules))
    assert rule_id == "rule-0001"
    assert "Staged, and shown" in out.result_text
    assert state.rule_impacts[rule_id] is not None
    assert out.events[1].data["payload"]["rule"]["rule_id"] == "rule-0001"
    assert out.events[1].data["payload"]["api"]["api_id"] == "api-1"
    assert "impact" in out.events[1].data["payload"]


async def test_stage_rule_raw_pattern_carries_examples_and_save_format_as(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_rule", {
        "api_id": "api-1", "param": "contractNo", "kind": "format", "pattern": "^C-\\d{4}$",
        "pass_examples": ["C-1234"], "fail_examples": ["C-12"],
        "save_format_as": "contract-no", "summary": "contract number format",
    })
    assert not out.refused
    rule = state.seen_rules["rule-0001"]
    assert rule.pass_examples == ["C-1234"]
    assert rule.fail_examples == ["C-12"]
    assert rule.save_format_as == "contract-no"


async def test_stage_rule_below_min_format_examples_is_a_named_error(backend, skills, session, state):
    tight = AtworksAgentConfig(model="m", min_format_examples=2)
    ex = _exec(backend.__class__(tight), tight, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_rule", {
        "api_id": "api-1", "param": "contractNo", "kind": "format", "pattern": "^C-\\d{4}$",
        "pass_examples": ["C-1234"], "fail_examples": ["C-12"], "summary": "s",
    })
    assert out.is_error
    assert "unavailable" not in out.result_text
    assert "rule-0001" not in state.seen_rules


async def test_preview_rule_once_per_turn_then_a_different_rule_still_renders(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    staged = await ex.execute("stage_rule", {"api_id": "api-1", "param": "amount", "kind": "compare",
                                             "op": ">=", "value": "0", "summary": "first"})
    assert not staged.refused
    kinds = [(e.type, e.data.get("component")) for e in staged.events]
    assert kinds == [("change_update", None), ("ui", "rule_preview")]

    repeat = await ex.execute("present_rule_preview", {"rule_id": "rule-0001"})
    assert not repeat.refused
    assert repeat.events == []
    assert repeat.result_text == ex.displayed_text

    other = await ex.execute("stage_rule", {"api_id": "api-1", "param": "amount", "kind": "required", "summary": "second"})
    assert not other.refused
    other_kinds = [(e.type, e.data.get("component")) for e in other.events]
    assert other_kinds == [("change_update", None), ("ui", "rule_preview")]


async def test_stage_rule_holds_unknown_param(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    held = await ex.execute("stage_rule", {"api_id": "api-1", "param": "nope", "kind": "required", "summary": "s"})
    assert held.blocked == "provenance"


async def test_stage_rule_membership_needs_values_is_a_named_error(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_rule", {"api_id": "api-1", "param": "amount", "kind": "membership",
                                          "op": "in", "values": [], "summary": "s"})
    assert out.is_error
    assert "unavailable" not in out.result_text


async def test_stage_rule_guardrail_membership_values_limit(backend, config, skills, session, state):
    tight = config.model_copy(update={"max_membership_values": 2})
    ex = _exec(backend, tight, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_rule", {"api_id": "api-1", "param": "amount", "kind": "membership",
                                          "op": "in", "values": ["a", "b", "c"], "summary": "s"})
    assert out.blocked == "guardrail"
    assert "3 values" in out.result_text


async def test_stage_rule_guardrail_when_api_already_at_the_applied_limit(backend, config, skills, session, state):
    tight = config.model_copy(update={"max_rules_per_api": 1})
    ex = _exec(backend.__class__(tight), tight, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    await ex.execute("stage_rule", {"api_id": "api-1", "param": "amount", "kind": "compare",
                                    "op": ">=", "value": "0", "summary": "first"})
    state.approved_rule_ids.add("rule-0001")
    await ex.execute("apply_rule", {"rule_id": "rule-0001"})
    out = await ex.execute("stage_rule", {"api_id": "api-1", "param": "amount", "kind": "required",
                                          "summary": "second"})
    assert out.blocked == "guardrail"
    assert "rule-0002" not in state.seen_rules


async def test_apply_rule_requires_host_mark(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    await ex.execute("stage_rule", {"api_id": "api-1", "param": "amount", "kind": "compare",
                                    "op": ">=", "value": "0", "summary": "s"})
    held = await ex.execute("apply_rule", {"rule_id": "rule-0001"})
    assert held.blocked == "approval"
    state.approved_rule_ids.add("rule-0001")
    out = await ex.execute("apply_rule", {"rule_id": "rule-0001"})
    assert not out.refused and state.seen_rules["rule-0001"].status.value == "applied"
    assert out.events[0].type == "change_update"


async def test_apply_rule_guardrail_when_limit_exceeded_between_stage_and_apply(backend, config, skills, session, state):
    tight = config.model_copy(update={"max_rules_per_api": 1})
    ex = _exec(backend.__class__(tight), tight, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    await ex.execute("stage_rule", {"api_id": "api-1", "param": "amount", "kind": "compare",
                                    "op": ">=", "value": "0", "summary": "first"})
    await ex.execute("stage_rule", {"api_id": "api-1", "param": "amount", "kind": "required", "summary": "second"})
    state.approved_rule_ids.update({"rule-0001", "rule-0002"})
    first = await ex.execute("apply_rule", {"rule_id": "rule-0001"})
    assert not first.refused
    second = await ex.execute("apply_rule", {"rule_id": "rule-0002"})
    assert second.blocked == "guardrail"


async def test_discard_rule(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    await ex.execute("stage_rule", {"api_id": "api-1", "param": "amount", "kind": "compare",
                                    "op": ">=", "value": "0", "summary": "s"})
    out = await ex.execute("discard_rule", {"rule_id": "rule-0001"})
    assert not out.refused
    assert state.seen_rules["rule-0001"].status.value == "discarded"


async def test_discard_rule_unknown_id_is_provenance_blocked(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("discard_rule", {"rule_id": "rule-9999"})
    assert out.blocked == "provenance"


async def test_get_pending_rules(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    await ex.execute("stage_rule", {"api_id": "api-1", "param": "amount", "kind": "compare",
                                    "op": ">=", "value": "0", "summary": "s"})
    out = await ex.execute("get_pending_rules", {})
    assert not out.refused
    pending = _payload(out)
    assert len(pending) == 1 and pending[0]["rule_id"] == "rule-0001"


async def test_open_question_form_blocks_staging_rule_this_turn(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    form = await ex.execute("present_question_form", {
        "id": "param-pick", "title": "파라미터",
        "questions": [{"id": "param", "label": "파라미터", "type": "radio", "why": "param이 없음",
                       "default": "amount", "options": ["amount", "contractNo"]}],
    })
    assert not form.refused
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_rule", {"api_id": "api-1", "param": "amount", "kind": "compare",
                                          "op": ">=", "value": "0", "summary": "s"})
    assert out.blocked == "question_form"


async def test_absent_tools_are_reported_as_absent_even_when_question_form_is_open(backend, config, skills, session, state):
    no_jobs_config = config.model_copy(update={"enable_jobs": False})
    ex = _exec(backend, no_jobs_config, skills, session, state)
    form = await ex.execute("present_question_form", {
        "id": "target-env", "title": "대상 환경",
        "questions": [{"id": "target_envs", "label": "대상 환경", "type": "radio", "why": "target_envs가 없음",
                       "default": "dev", "options": ["dev", "stg"]}],
    })
    assert not form.refused
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"], "target_envs": ["dev"]})
    assert out.is_error
    assert "not something this deployment does" in out.result_text
    assert out.blocked is None
