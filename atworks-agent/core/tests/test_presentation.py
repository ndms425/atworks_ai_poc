from datetime import UTC, datetime

from commerce_common.presentation import EnrichmentContext, run_presentation

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.enrichment import PRESENTATION_COMPONENTS
from atworks_agent.types import (
    ApiSpec,
    AtworksSessionContext,
    AtworksSessionState,
    FailedRank,
    JobKind,
    JobSpec,
    RunResult,
    RunStatus,
)

CFG = AtworksAgentConfig(model="m")
SESSION = AtworksSessionContext(session_id="s", project_id="p", operator="op")


def _ctx(state):
    return EnrichmentContext(backend=None, config=CFG, session=SESSION, state=state)


def _state_with_run():
    state = AtworksSessionState()
    state.remember_api(ApiSpec(api_id="api-1", method="POST", path="/v1/contracts", name="계약 생성", updated_at=datetime.now(UTC)))
    state.remember_run(RunResult(run_id="run-17", api_id="api-1", executed_at=datetime.now(UTC), target_env="dev",
                                 status=RunStatus.FAIL, failed_rules=["amount >= 0"], http_status=200))
    state.remember_rank(FailedRank(run_id="run-17", api_id="api-1", scorer="risk_v1", score=4.0, reasons=["1 rule failed"]))
    state.last_population = 47
    return state


async def test_run_digest_joins_run_and_population():
    state = _state_with_run()
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_run_digest"],
        {"title": "먼저 볼 실패", "items": [{"kind": "fail", "ref_id": "run-17", "headline": "amount 음수", "why_it_matters": "계약 금액 규칙 위반"}]},
        _ctx(state), "Shown.",
    )
    ui = outcome.events[0]
    assert ui.data["component"] == "run_digest"
    payload = ui.data["payload"]
    assert payload["population"] == 47 and payload["scorer"] == "risk_v1"
    item = payload["items"][0]
    assert item["run"]["status"] == "fail" and item["api"]["path"] == "/v1/contracts"
    assert item["rank"]["score"] == 4.0


async def test_run_digest_drops_unknown_ref_and_reports():
    state = _state_with_run()
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_run_digest"],
        {"items": [{"kind": "fail", "ref_id": "run-999", "headline": "x"}, {"kind": "fail", "ref_id": "run-17", "headline": "y"}]},
        _ctx(state), "Shown.",
    )
    assert len(outcome.events[0].data["payload"]["items"]) == 1
    assert "run-999" in outcome.result_text


async def test_run_digest_refused_without_population():
    state = _state_with_run()
    state.last_population = None
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_run_digest"],
        {"items": [{"kind": "fail", "ref_id": "run-17", "headline": "y"}]}, _ctx(state), "Shown.",
    )
    assert outcome.is_error and "list_runs" in outcome.result_text


async def test_job_preview_joins_staged_record():
    state = AtworksSessionState()
    state.remember_job(JobSpec(job_id="job-0001", kind=JobKind.RUN_NOW, summary="s", api_ids=["api-1"], target_env="dev",
                               confidence={"target_env": 0.3}, assumptions=["target_env defaulted to dev"],
                               created_at=datetime.now(UTC), created_by="op"))
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_job_preview"], {"job_id": "job-0001", "headline": "h"}, _ctx(state), "Shown.")
    payload = outcome.events[0].data["payload"]
    assert payload["job"]["job_id"] == "job-0001" and payload["job"]["confidence"]["target_env"] == 0.3
    assert payload["low_confidence"] == ["target_env"]


async def test_job_preview_refuses_unknown_job():
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_job_preview"], {"job_id": "nope"}, _ctx(AtworksSessionState()), "Shown.")
    assert outcome.blocked == "provenance"


async def test_question_form_marks_low_confidence():
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_question_form"],
        {"id": "job-slots", "title": "확인", "questions": [
            {"id": "target_env", "label": "어느 계?", "type": "radio", "why": "발화에 없음", "default": "dev", "options": ["dev", "stg"], "confidence": 0.3}]},
        _ctx(AtworksSessionState()), "Shown.",
    )
    payload = outcome.events[0].data["payload"]
    assert payload["questions"][0]["highlight"] is True
