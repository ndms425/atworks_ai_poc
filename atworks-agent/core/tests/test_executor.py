import json

from atworks_agent.executor import AtworksToolExecutor


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
