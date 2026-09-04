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
    out = await _exec(backend, config, skills, session, state).execute("list_runs", {"status": "fail"})
    assert not out.refused and state.last_population == 1 and "run-1" in state.seen_runs


async def test_rank_needs_runs_first_then_ranks(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    out = await ex.execute("rank_failed_runs", {"scorer": "risk_v1"})
    assert out.is_error and "list_runs" in out.result_text
    await ex.execute("list_runs", {})
    out = await ex.execute("rank_failed_runs", {"scorer": "risk_v1", "limit": 5})
    ranks = _payload(out)["ranked"]
    assert [r["run_id"] for r in ranks] == ["run-2", "run-1"] and state.seen_ranks


async def test_stage_job_holds_unknown_api_then_stages_with_preview(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    draft = {"kind": "run_now", "summary": "run contracts", "api_ids": ["api-1"], "target_env": "dev",
             "confidence": {"target_env": 0.3}, "assumptions": ["target_env defaulted to dev"]}
    held = await ex.execute("stage_job", draft)
    assert held.blocked == "provenance"
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", draft)
    assert not out.refused
    kinds = [(e.type, e.data.get("component")) for e in out.events]
    assert kinds == [("change_update", None), ("ui", "job_preview")]
    job_id = next(iter(state.seen_jobs))
    assert out.events[1].data["payload"]["low_confidence"] == ["target_env"]
    assert "Staged, and shown" in out.result_text and job_id == "job-0001"


async def test_stage_job_guardrail_prod(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    out = await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"], "target_env": "prod"})
    assert out.blocked == "guardrail" and "prod" in out.result_text


async def test_apply_requires_host_mark(backend, config, skills, session, state):
    ex = _exec(backend, config, skills, session, state)
    await ex.execute("search_apis", {"query": ""})
    await ex.execute("stage_job", {"kind": "run_now", "summary": "s", "api_ids": ["api-1"], "target_env": "dev"})
    held = await ex.execute("apply_job", {"job_id": "job-0001"})
    assert held.blocked == "approval"
    state.approved_job_ids.add("job-0001")
    out = await ex.execute("apply_job", {"job_id": "job-0001"})
    assert not out.refused and state.seen_jobs["job-0001"].status.value == "applied"
    assert out.events[0].type == "change_update"


async def test_unknown_tool_is_refused(backend, config, skills, session, state):
    out = await _exec(backend, config, skills, session, state).execute("drop_database", {})
    assert out.is_error
