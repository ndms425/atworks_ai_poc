import json
from datetime import UTC, datetime, timedelta

from commerce_common.types import PROVENANCE_CAP

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.executor import AtworksToolExecutor
from atworks_agent.rules import RuleDraft
from atworks_agent.types import ApiSpec, RunResult, RunStatus

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


async def test_list_runs_envelope_carries_total_and_next_cursor(backend, config, skills, session, state):
    out = await _exec(backend, config, skills, session, state).execute("list_runs", {})
    payload = _payload(out)
    assert payload["total"] == 3 and len(payload["items"]) == 3 and payload["next_cursor"] is None


async def test_list_runs_with_bodies_stays_under_the_fence_cap_and_parses_as_json(backend, config, skills, session, state):
    # 50 runs each carrying a response body large enough that including the bodies would
    # blow well past the fence's 12,000-char truncation cap (config.max_fenced_chars) and
    # truncate mid-JSON; run_record drops response_body so the envelope stays small and the
    # fenced text always parses.
    backend.runs = [
        RunResult(run_id=f"run-{i:04d}", api_id="api-1", executed_at=T0 + timedelta(minutes=i),
                  target_env="dev", status=RunStatus.PASS, http_status=200,
                  response_body={"echo": {"note": "x" * 400}})
        for i in range(50)
    ]
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("list_runs", {"limit": 50})
    assert not out.refused
    raw = out.result_text.split("<atworks_data>", 1)[1].split("</atworks_data>", 1)[0]
    assert len(raw) < 12_000
    payload = json.loads(raw)
    assert payload["total"] == 50 and len(payload["items"]) == 50
    assert all("response_body" not in item and item["has_body"] is True for item in payload["items"])


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


async def test_aggregate_runs_population_counts_the_true_total_and_no_longer_samples_runs(backend, skills, session, state):
    small_config = AtworksAgentConfig(model="m", max_aggregate_runs=2)
    ex = AtworksToolExecutor(backend=backend, config=small_config, skills=skills, session=session, state=state)
    out = await ex.execute("aggregate_runs", {"group_by": "api"})
    assert not out.is_error
    # population is the backend's true count_runs; "truncated" is gone from the envelope (Task 3).
    assert state.last_population == 3
    assert "truncated" not in out.result_text
    # Task 8: the grouping is done by the backend over its rollups, so max_aggregate_runs no
    # longer truncates ANYTHING here -- the provenance window is the evidence ids the groups
    # themselves cite (<=MAX_RUN_IDS each), newest first, not a run sample the executor paged.
    assert state.last_listed_run_ids == ["run-3", "run-2", "run-1"]


async def test_aggregate_runs_population_for_api_id_uses_count_runs_not_the_truncated_window(
    backend, skills, session, state
):
    # Regression: population used to be len(runs) whenever api_id was given, which silently
    # undercounted once max_aggregate_runs truncated the collected window. It must equal the
    # backend's own count_runs(api_id=...) instead.
    small_config = AtworksAgentConfig(model="m", max_aggregate_runs=1)
    ex = AtworksToolExecutor(backend=backend, config=small_config, skills=skills, session=session, state=state)
    await ex.execute("get_api", {"api_id": "api-1"})
    out = await ex.execute("aggregate_runs", {"group_by": "api", "api_id": "api-1"})
    assert not out.is_error
    assert state.last_population == 2       # run-1 and run-3 both belong to api-1
    # api_id scopes the backend's aggregate query, so the cited evidence is that API's runs only
    assert state.last_listed_run_ids == ["run-3", "run-1"]


async def test_aggregate_runs_caps_last_listed_run_ids_at_the_provenance_cap(backend, skills, session, state):
    # 10 groups x MAX_RUN_IDS(50) cited ids = 500 candidates, well past PROVENANCE_CAP (200):
    # last_listed_run_ids must still be trimmed to PROVENANCE_CAP the way every other
    # seen-record map already is (remember()), and keep the newest ids.
    backend.apis = {
        f"api-{a}": ApiSpec(api_id=f"api-{a}", method="GET", path=f"/v1/x{a}", name=f"x{a}", updated_at=T0)
        for a in range(10)
    }
    backend.runs = [
        RunResult(run_id=f"run-{i:04d}", api_id=f"api-{i % 10}", executed_at=T0 + timedelta(minutes=i),
                  target_env="dev", status=RunStatus.PASS, http_status=200)
        for i in range(300)
    ]
    big_config = AtworksAgentConfig(model="m", max_aggregate_runs=300)
    ex = AtworksToolExecutor(backend=backend, config=big_config, skills=skills, session=session, state=state)
    out = await ex.execute("aggregate_runs", {"group_by": "api"})
    assert not out.is_error
    assert state.last_population == 300
    assert len(state.last_listed_run_ids) == PROVENANCE_CAP
    assert state.last_listed_run_ids[0] == "run-0299"


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
    assert rule.review_required is True   # fresh raw pattern, not yet vetted into the library


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


async def test_stage_rule_named_builtin_format_stages_unchanged(backend, config, skills, session, state):
    # Task 3: built-in enum names (email/date/iso8601/uuid/number) must keep working exactly
    # as before -- no library lookup, no format_name, format kept as the enum value.
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_rule", {"api_id": "api-1", "param": "amount", "kind": "format",
                                          "format": "email", "summary": "s"})
    assert not out.refused
    rule = state.seen_rules["rule-0001"]
    assert rule.format == "email"
    assert rule.pattern is None
    assert rule.format_name is None


async def test_stage_rule_resolves_a_saved_library_format_by_name(backend, config, skills, session, state):
    # Task 3 ruling: a `format` naming a LIBRARY entry (here, a saved one) is resolved at
    # stage time into pattern/examples; the source name is kept only for display.
    from atworks_agent.rules import FormatDefinition

    added, reason = await backend.save_format(session, FormatDefinition(
        name="phone-digits", pattern=r"^\d{3}-\d{4}$",
        pass_examples=["123-4567"], fail_examples=["abc"], created_by=session.operator,
    ))
    assert added is True and reason is None

    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_rule", {"api_id": "api-1", "param": "amount", "kind": "format",
                                          "format": "phone-digits", "summary": "s"})
    assert not out.refused
    rule = state.seen_rules["rule-0001"]
    assert rule.format is None
    assert rule.pattern == r"^\d{3}-\d{4}$"
    assert rule.format_name == "phone-digits"
    # Fix round 2 ruling: resolving a trusted, already-verified library format must NOT flag
    # for review -- only a fresh, unvetted raw pattern does.
    assert rule.review_required is False

    from atworks_agent.rules import evaluate
    assert evaluate(rule, "123-4567") is True
    assert evaluate(rule, "abc") is False


async def test_stage_rule_resolving_a_saved_format_clears_save_format_as(backend, config, skills, session, state):
    # Fix round 1: referencing an existing library format must not also carry a "save this
    # under a new name" instruction -- the pattern already exists, so re-saving it is
    # meaningless and confusing. Resolution must clear save_format_as, even when the caller
    # supplied one alongside `format`.
    from atworks_agent.rules import FormatDefinition

    added, reason = await backend.save_format(session, FormatDefinition(
        name="phone-digits", pattern=r"^\d{3}-\d{4}$",
        pass_examples=["123-4567"], fail_examples=["abc"], created_by=session.operator,
    ))
    assert added is True and reason is None

    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_rule", {
        "api_id": "api-1", "param": "amount", "kind": "format",
        "format": "phone-digits", "save_format_as": "other-name", "summary": "s",
    })
    assert not out.refused
    rule = state.seen_rules["rule-0001"]
    assert rule.format_name == "phone-digits"
    assert rule.pattern == r"^\d{3}-\d{4}$"
    assert rule.save_format_as is None


async def test_stage_rule_raw_pattern_with_misclassifying_example_is_refused(backend, config, skills, session, state):
    # Safety spine at the tool boundary: RuleDraft._kind_fields runs verify_examples on a raw
    # pattern before a rule can ever be staged. "12a" does not fullmatch ^\d+$, so it is a bad
    # pass example -- stage_rule must refuse it, not silently stage a self-contradicting rule.
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_rule", {
        "api_id": "api-1", "param": "amount", "kind": "format", "pattern": r"^\d+$",
        "pass_examples": ["12a"], "fail_examples": ["abc"], "summary": "s",
    })
    assert out.is_error
    assert "rule-0001" not in state.seen_rules


async def test_stage_rule_unknown_format_name_is_a_named_error(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_rule", {"api_id": "api-1", "param": "amount", "kind": "format",
                                          "format": "no-such-format", "summary": "s"})
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


# -- Task 7: recommendation read tools -------------------------------------------------


async def test_find_apis_with_param_excludes_apis_with_an_applied_format_rule(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("find_apis_with_param", {"param": "contractNo"})
    payload = _payload(out)
    assert [a["api_id"] for a in payload["items"]] == ["api-1"]
    assert payload["total"] == 1
    assert "api-1" in state.seen_apis
    rule = backend.rule_ledger.stage(
        RuleDraft(api_id="api-1", param="contractNo", kind="format", format="uuid"), actor="op")
    backend.rule_ledger.apply(rule.rule_id, actor="op")
    out = await ex.execute("find_apis_with_param", {"param": "contractNo"})
    assert _payload(out) == {"note": "No APIs declare that parameter without an already-applied format rule for it."}


async def test_find_apis_with_param_no_match(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("find_apis_with_param", {"param": "nonexistentParam"})
    assert _payload(out) == {"note": "No APIs declare that parameter without an already-applied format rule for it."}


async def test_recommend_rules_for_api_suggests_a_peers_applied_rule_on_the_same_named_param(
    backend, config, skills, session, state
):
    backend.apis["api-3"] = ApiSpec(api_id="api-3", method="GET", path="/v1/payments/{id}", name="결제 상세",
                                    group="payment", updated_at=T0, has_rules=False, params=["amount"])
    rule = backend.rule_ledger.stage(
        RuleDraft(api_id="api-1", param="amount", kind="compare", op=">=", value="0"), actor="op")
    applied = backend.rule_ledger.apply(rule.rule_id, actor="op")
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("recommend_rules_for_api", {"api_id": "api-3"})
    recs = _payload(out)
    assert len(recs) == 1
    assert recs[0]["param"] == "amount"
    assert recs[0]["from_api_id"] == "api-1"
    assert recs[0]["rule"]["rule_id"] == applied.rule_id
    assert "api-3" in state.seen_apis


async def test_recommend_rules_for_api_suggests_nothing_when_no_peer_has_a_rule(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("recommend_rules_for_api", {"api_id": "api-1"})
    assert not out.is_error
    assert _payload(out) == {"note": "No rule-less parameter of this API has a peer with an applied rule to suggest."}
    assert "api-1" in state.seen_apis


async def test_recommend_rules_for_api_errors_on_an_unknown_api_id(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("recommend_rules_for_api", {"api_id": "no-such-api"})
    assert out.is_error and "No API with that id." in out.result_text
    assert "no-such-api" not in state.seen_apis


async def test_recommend_rules_for_api_skips_params_the_target_already_has_a_rule_for(
    backend, config, skills, session, state
):
    backend.apis["api-3"] = ApiSpec(api_id="api-3", method="GET", path="/v1/payments/{id}", name="결제 상세",
                                    group="payment", updated_at=T0, has_rules=False, params=["amount"])
    peer_rule = backend.rule_ledger.stage(
        RuleDraft(api_id="api-3", param="amount", kind="compare", op=">=", value="0"), actor="op")
    backend.rule_ledger.apply(peer_rule.rule_id, actor="op")
    own_rule = backend.rule_ledger.stage(
        RuleDraft(api_id="api-1", param="amount", kind="required"), actor="op")
    backend.rule_ledger.apply(own_rule.rule_id, actor="op")
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("recommend_rules_for_api", {"api_id": "api-1"})
    assert _payload(out) == {"note": "No rule-less parameter of this API has a peer with an applied rule to suggest."}


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


# -- Task 4: format-batch lifecycle ---------------------------------------------------


async def test_stage_format_batch_computes_outcomes(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("stage_format_batch", {"formats": [
        {"name": "email", "pattern": r"^y$"},
        {"name": "mail-like", "pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"},
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$",
         "pass_examples": ["123-4567"], "fail_examples": ["abc"]},
    ], "summary": "bulk seed"})
    assert not out.refused
    batch_id = next(iter(state.seen_format_batches))
    batch = state.seen_format_batches[batch_id]
    assert [e.outcome for e in batch.entries] == ["duplicate", "duplicate", "new"]
    assert "Staged only" in out.result_text
    payload = _payload(out)
    assert payload["staged"]["new_count"] == 1 and payload["staged"]["duplicate_count"] == 2


async def test_stage_format_batch_guardrail_over_batch_size(backend, config, skills, session, state):
    tight = config.model_copy(update={"max_format_batch": 1})
    ex = _exec(backend, tight, skills, session, state)
    out = await ex.execute("stage_format_batch", {"formats": [
        {"name": "a", "pattern": r"^a$"}, {"name": "b", "pattern": r"^b$"},
    ]})
    assert out.blocked == "guardrail"
    assert "batch" not in state.seen_format_batches


async def test_get_pending_format_batches(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("stage_format_batch", {"formats": [
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$", "pass_examples": ["123-4567"], "fail_examples": ["abc"]},
    ]})
    out = await ex.execute("get_pending_format_batches", {})
    assert not out.refused
    pending = _payload(out)
    assert len(pending) == 1 and pending[0]["batch_id"] == "format-batch-0001"


async def test_apply_format_batch_requires_host_mark_and_adds_only_new(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("stage_format_batch", {"formats": [
        {"name": "email", "pattern": r"^y$"},
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$", "pass_examples": ["123-4567"], "fail_examples": ["abc"]},
    ]})
    held = await ex.execute("apply_format_batch", {"batch_id": "format-batch-0001"})
    assert held.blocked == "approval"
    state.approved_format_batch_ids.add("format-batch-0001")
    out = await ex.execute("apply_format_batch", {"batch_id": "format-batch-0001"})
    assert not out.refused
    assert state.seen_format_batches["format-batch-0001"].status.value == "applied"
    names = {f.name for f in await backend.list_formats(session)}
    assert "phone-digits" in names and "email" in names
    assert out.events[0].type == "change_update"
    assert "1 new format(s)" in out.result_text


async def test_apply_format_batch_unknown_id_is_provenance_blocked(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("apply_format_batch", {"batch_id": "format-batch-9999"})
    assert out.blocked == "provenance"


async def test_discard_format_batch(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("stage_format_batch", {"formats": [
        {"name": "phone-digits", "pattern": r"^\d{3}-\d{4}$", "pass_examples": ["123-4567"], "fail_examples": ["abc"]},
    ]})
    out = await ex.execute("discard_format_batch", {"batch_id": "format-batch-0001"})
    assert not out.refused
    assert state.seen_format_batches["format-batch-0001"].status.value == "discarded"


async def test_discard_format_batch_unknown_id_is_provenance_blocked(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("discard_format_batch", {"batch_id": "format-batch-9999"})
    assert out.blocked == "provenance"


async def test_apply_format_batch_with_zero_new_entries_is_still_applied(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("stage_format_batch", {"formats": [{"name": "email", "pattern": r"^y$"}]})
    state.approved_format_batch_ids.add("format-batch-0001")
    out = await ex.execute("apply_format_batch", {"batch_id": "format-batch-0001"})
    assert not out.refused
    assert state.seen_format_batches["format-batch-0001"].status.value == "applied"
    assert "0 new format(s)" in out.result_text


# -- Task 7: comparison-profile handlers + ignore-path recommendation -----------------


async def _staged_job(ex):
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"], "target_envs": ["dev"]})
    assert not out.refused
    return "job-0001"


async def test_stage_profile_holds_unknown_job_then_stages(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    held = await ex.execute("stage_profile", {"job_id": "job-0001", "ignore_paths": ["$.serverTime"], "summary": "noisy field"})
    assert held.blocked == "provenance"

    job_id = await _staged_job(ex)
    out = await ex.execute("stage_profile", {"job_id": job_id, "ignore_paths": ["$.serverTime"], "summary": "noisy field"})
    assert not out.refused
    profile_id = next(iter(state.seen_profiles))
    assert profile_id == "profile-0001"
    assert state.seen_profiles[profile_id].job_id == job_id
    assert state.seen_profiles[profile_id].ignore_paths == ["$.serverTime"]
    assert "Staged only" in out.result_text
    payload = _payload(out)
    assert payload["staged"]["profile_id"] == "profile-0001"


async def test_stage_profile_per_api_ignore_is_kept_and_remembered(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    job_id = await _staged_job(ex)
    out = await ex.execute("stage_profile", {
        "job_id": job_id, "per_api_ignore": {"api-1": ["$.a.b"]}, "summary": "per-api noise",
    })
    assert not out.refused
    profile = state.seen_profiles["profile-0001"]
    assert profile.per_api_ignore == {"api-1": ["$.a.b"]}


async def test_stage_profile_invalid_ignore_path_is_a_named_error(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    job_id = await _staged_job(ex)
    out = await ex.execute("stage_profile", {"job_id": job_id, "ignore_paths": ["serverTime"], "summary": "s"})
    assert out.is_error
    assert "unavailable" not in out.result_text
    assert "profile-0001" not in state.seen_profiles


async def test_stage_profile_guardrail_over_ignore_path_cap(backend, config, skills, session, state):
    tight = config.model_copy(update={"max_ignore_paths": 1})
    ex = _exec(backend, tight, skills, session, state)
    job_id = await _staged_job(ex)
    out = await ex.execute("stage_profile", {
        "job_id": job_id, "ignore_paths": ["$.a", "$.b"], "summary": "s",
    })
    assert out.blocked == "guardrail"
    assert "profile-0001" not in state.seen_profiles


async def test_apply_profile_requires_host_mark(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    job_id = await _staged_job(ex)
    await ex.execute("stage_profile", {"job_id": job_id, "ignore_paths": ["$.serverTime"], "summary": "s"})
    held = await ex.execute("apply_profile", {"profile_id": "profile-0001"})
    assert held.blocked == "approval"

    state.approved_profile_ids.add("profile-0001")
    out = await ex.execute("apply_profile", {"profile_id": "profile-0001"})
    assert not out.refused
    assert state.seen_profiles["profile-0001"].status.value == "applied"
    assert out.events[0].type == "change_update"


async def test_apply_profile_unknown_id_is_provenance_blocked(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("apply_profile", {"profile_id": "profile-9999"})
    assert out.blocked == "provenance"


async def test_discard_profile(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    job_id = await _staged_job(ex)
    await ex.execute("stage_profile", {"job_id": job_id, "ignore_paths": ["$.serverTime"], "summary": "s"})
    out = await ex.execute("discard_profile", {"profile_id": "profile-0001"})
    assert not out.refused
    assert state.seen_profiles["profile-0001"].status.value == "discarded"


async def test_discard_profile_unknown_id_is_provenance_blocked(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("discard_profile", {"profile_id": "profile-9999"})
    assert out.blocked == "provenance"


async def test_get_pending_profiles(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    job_id = await _staged_job(ex)
    await ex.execute("stage_profile", {"job_id": job_id, "ignore_paths": ["$.serverTime"], "summary": "s"})
    out = await ex.execute("get_pending_profiles", {})
    assert not out.refused
    pending = _payload(out)
    assert len(pending) == 1 and pending[0]["profile_id"] == "profile-0001"


async def test_recommend_ignore_paths_ranks_clusters_biggest_first(backend, config, skills, session, state):
    backend.parity_reports["job-0001"] = {
        "targets": ["dev", "stg"],
        "clusters": [
            {"paths": ["$.a"], "count": 2, "row_keys": ["k1", "k2"]},
            {"paths": ["$.b"], "count": 7, "row_keys": ["k3"]},
        ],
    }
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("recommend_ignore_paths", {"job_id": "job-0001"})
    assert not out.refused
    payload = _payload(out)
    assert [c["count"] for c in payload["clusters"]] == [7, 2]
    assert payload["clusters"][0]["paths"] == ["$.b"]


async def test_recommend_ignore_paths_no_report_yet(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("recommend_ignore_paths", {"job_id": "job-9999"})
    assert not out.refused
    assert "No parity report" in _payload(out)["note"]


async def test_open_question_form_blocks_staging_profile_this_turn(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    job_id = await _staged_job(ex)
    form = await ex.execute("present_question_form", {
        "id": "ignore-pick", "title": "무시할 경로",
        "questions": [{"id": "path", "label": "경로", "type": "radio", "why": "경로 확인 필요",
                       "default": "$.serverTime", "options": ["$.serverTime", "$.requestId"]}],
    })
    assert not form.refused
    out = await ex.execute("stage_profile", {"job_id": job_id, "ignore_paths": ["$.serverTime"], "summary": "s"})
    assert out.blocked == "question_form"


# -- query_runs (self-growth spec §5) ----------------------------------------------------

async def test_query_runs_envelope_repeats_the_backend_result(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("query_runs", {
        "filters": {"window_days": 180}, "dimensions": ["api"],
        "measures": ["runs", "non_pass"], "order_by": "runs",
    })
    assert not out.refused
    payload = _payload(out)
    result = state.last_query_result
    # Every figure in the envelope is the backend's, verbatim -- the executor computes none.
    assert payload["population"] == result.population == 3
    assert payload["total_groups"] == result.total_groups == 2
    assert payload["source"] == result.source
    assert [row["keys"] for row in payload["rows"]] == [row.keys for row in result.rows]
    assert [row["measures"] for row in payload["rows"]] == [row.measures for row in result.rows]
    assert payload["rows"][0]["keys"] == {"api": "api-1"}
    assert payload["rows"][0]["measures"] == {"runs": 2, "non_pass": 1}
    assert "present_query_table" in payload["note"]


async def test_query_runs_spec_summary_names_the_resolved_default_window(backend, config, skills, session, state):
    # No window in the spec: the title must say the window the host actually applied, never
    # "기본 기간" (ledger ruling, Task 1 review).
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("query_runs", {"dimensions": ["api"], "measures": ["runs"]})
    assert not out.refused
    summary = _payload(out)["spec_summary"]
    assert f"{config.max_aggregate_window_days}일" in summary and "기본 기간" not in summary


async def test_query_runs_rejects_a_spec_outside_the_catalogue(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("query_runs", {"dimensions": ["nonsense_axis"], "measures": ["runs"]})
    assert out.refused and "not a valid query spec" in out.result_text
    assert state.last_query_result is None


async def test_query_runs_rejects_an_unknown_filter_key(backend, config, skills, session, state):
    # extra="forbid" is the point of QueryFilters: an invented filter is a schema error, never
    # a silently ignored key.
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("query_runs", {"filters": {"invented": "x"}, "measures": ["runs"]})
    assert out.refused and "not a valid query spec" in out.result_text


async def test_query_runs_rejects_an_unseen_api_id(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("query_runs", {"filters": {"api_ids": ["api-1"]}, "measures": ["runs"]})
    assert out.refused and "search_apis" in out.result_text and state.last_query_result is None


async def test_query_runs_accepts_a_seen_api_id(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("get_api", {"api_id": "api-1"})
    out = await ex.execute("query_runs", {
        "filters": {"api_ids": ["api-1"], "window_days": 180}, "dimensions": ["api"], "measures": ["runs"],
    })
    assert not out.refused and _payload(out)["population"] == 2


async def test_query_runs_refuses_another_operators_scope(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("query_runs", {"filters": {"scope_operator": "someone-else"}, "measures": ["runs"]})
    assert out.refused and "own operator" in out.result_text and state.last_query_result is None


async def test_query_runs_allows_the_sessions_own_scope(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("query_runs", {
        "filters": {"scope_operator": session.operator}, "measures": ["runs"],
    })
    assert not out.refused and state.last_query_result is not None


async def test_query_runs_remembers_its_evidence_samples(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("query_runs", {
        "filters": {"window_days": 180}, "dimensions": ["api"], "measures": ["runs"],
    })
    assert not out.refused
    # api ids and run ids the rows cite are session-seen afterwards, so a card or a screen
    # directive may name them (spec §2 clause 2).
    assert {"api-1", "api-2"} <= set(state.seen_apis)
    assert {"run-1", "run-2", "run-3"} <= set(state.seen_runs)
    assert state.last_population == 3
    assert state.last_listed_run_ids[0] == "run-3"   # newest first


async def test_query_runs_caps_the_remembered_run_ids(backend, config, skills, session, state):
    backend.apis = {
        f"api-{i:03d}": ApiSpec(api_id=f"api-{i:03d}", method="GET", path=f"/v1/x{i}", name=f"n{i}",
                                group="g", updated_at=T0)
        for i in range(60)
    }
    backend.runs = [
        RunResult(run_id=f"run-{i:03d}-{j}", api_id=f"api-{i:03d}", executed_at=T0 + timedelta(minutes=i * 10 + j),
                  target_env="dev", status=RunStatus.FAIL, http_status=500)
        for i in range(60) for j in range(5)
    ]
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("query_runs", {
        "filters": {"window_days": 180}, "dimensions": ["api"], "measures": ["runs"], "limit": 50,
    })
    assert not out.refused
    # 50 rows x 5 sampled run ids = 250 cited; the session keeps the newest PROVENANCE_CAP.
    assert len(state.last_listed_run_ids) == PROVENANCE_CAP
    assert state.last_population == 300 and state.last_query_result.total_groups == 60


async def test_query_runs_envelope_trims_row_samples_to_three(backend, config, skills, session, state):
    backend.runs = [
        RunResult(run_id=f"run-{j}", api_id="api-1", executed_at=T0 + timedelta(minutes=j),
                  target_env="dev", status=RunStatus.FAIL, http_status=500)
        for j in range(8)
    ]
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("query_runs", {
        "filters": {"window_days": 180}, "dimensions": ["api"], "measures": ["runs"],
    })
    assert len(_payload(out)["rows"][0]["run_ids"]) == 3
    assert len(state.last_query_result.rows[0].run_ids) == 5


async def test_query_runs_is_absent_when_the_feature_is_off(backend, skills, session, state):
    off = AtworksAgentConfig(model="m", enable_query_runs=False)
    out = await _exec(backend, off, skills, session, state).execute("query_runs", {"measures": ["runs"]})
    assert state.last_query_result is None and "query_runs" in out.result_text


# -- note_unmet_ask + the per-turn counters ask_log classifies from (Task 4, spec §5/§6) ------

async def test_note_unmet_ask_records_the_triple_and_draws_no_card(backend, config, skills, session, state):
    out = await _exec(backend, config, skills, session, state).execute("note_unmet_ask", {
        "reason": "no_dimension", "summary": "요청 헤더별로 실패를 묶어달라",
        "wanted": "요청 헤더 차원",
    })
    assert not out.refused and out.events == []
    assert state.turn_unmet == ("no_dimension", "요청 헤더별로 실패를 묶어달라", "요청 헤더 차원")
    assert "기록했습니다" in out.result_text


async def test_note_unmet_ask_rejects_a_reason_outside_the_enum(backend, config, skills, session, state):
    out = await _exec(backend, config, skills, session, state).execute("note_unmet_ask", {
        "reason": "because_i_said_so", "summary": "…",
    })
    assert out.is_error and state.turn_unmet is None


async def test_note_unmet_ask_needs_a_summary(backend, config, skills, session, state):
    out = await _exec(backend, config, skills, session, state).execute("note_unmet_ask", {
        "reason": "refused", "summary": "",
    })
    assert out.is_error and state.turn_unmet is None


async def test_note_unmet_ask_truncates_summary_and_wanted_at_200(backend, config, skills, session, state):
    await _exec(backend, config, skills, session, state).execute("note_unmet_ask", {
        "reason": "no_evidence", "summary": "가" * 500, "wanted": "나" * 500,
    })
    reason, summary, wanted = state.turn_unmet
    assert reason == "no_evidence" and len(summary) <= 200 and len(wanted) <= 200


async def test_note_unmet_ask_is_absent_when_the_feature_is_off(backend, skills, session, state):
    off = AtworksAgentConfig(model="m", enable_query_runs=False)
    out = await _exec(backend, off, skills, session, state).execute("note_unmet_ask", {
        "reason": "refused", "summary": "…",
    })
    assert state.turn_unmet is None and "note_unmet_ask" in out.result_text


async def test_every_dispatch_appends_its_tool_name_in_call_order(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": "contracts"})
    await ex.execute("list_runs", {})
    await ex.execute("nonexistent_tool", {})
    # Even the unknown name is recorded: tool_calls counts what the turn ATTEMPTED, and a turn
    # that spent its rounds on tools that do not exist is exactly what `partial` has to catch.
    assert state.turn_tool_names == ["search_apis", "list_runs", "nonexistent_tool"]


async def test_a_rendered_card_increments_turn_cards(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("list_runs", {})
    out = await ex.execute("present_run_digest", {
        "title": "실패", "items": [{"kind": "fail", "ref_id": "run-1", "headline": "amount 음수"}],
    })
    assert not out.refused and state.turn_cards == 1


async def test_a_refused_card_does_not_increment_turn_cards(backend, config, skills, session, state):
    # An ungrounded run id is refused by the presentation gate: the operator saw nothing, so the
    # turn must not read as `answered`.
    out = await _exec(backend, config, skills, session, state).execute("present_run_digest", {
        "title": "실패", "items": [{"kind": "fail", "ref_id": "run-never-seen", "headline": "x"}],
    })
    assert out.refused and state.turn_cards == 0
