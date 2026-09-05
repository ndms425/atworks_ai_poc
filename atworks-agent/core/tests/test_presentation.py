from datetime import UTC, datetime

from commerce_common.presentation import EnrichmentContext, run_presentation

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.enrichment import PRESENTATION_COMPONENTS
from atworks_agent.rules import RuleImpact, ValidationRule
from atworks_agent.types import (
    ApiSpec,
    AtworksSessionContext,
    AtworksSessionState,
    FailedRank,
    JobKind,
    JobSpec,
    RunGroup,
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
    state.last_listed_run_ids = ["run-17"]
    return state


async def test_run_digest_joins_run_and_population():
    state = _state_with_run()
    state.last_listed_filter = "fail"
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_run_digest"],
        {"title": "먼저 볼 실패", "items": [{"kind": "fail", "ref_id": "run-17", "headline": "amount 음수", "why_it_matters": "계약 금액 규칙 위반"}]},
        _ctx(state), "Shown.",
    )
    ui = outcome.events[0]
    assert ui.data["component"] == "run_digest"
    payload = ui.data["payload"]
    assert payload["population"] == 47 and payload["scorer"] == "risk_v1"
    assert payload["population_filter"] == "fail"
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


async def test_run_digest_drops_run_outside_window():
    state = _state_with_run()
    state.remember_run(RunResult(run_id="run-18", api_id="api-1", executed_at=datetime.now(UTC), target_env="dev",
                                 status=RunStatus.FAIL, http_status=200))
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_run_digest"],
        {"items": [{"kind": "fail", "ref_id": "run-17", "headline": "y"}, {"kind": "fail", "ref_id": "run-18", "headline": "z"}]},
        _ctx(state), "Shown.",
    )
    assert len(outcome.events[0].data["payload"]["items"]) == 1
    assert "run-18" in outcome.result_text


async def test_run_digest_uses_run_status_and_drops_pass():
    state = _state_with_run()
    state.remember_run(RunResult(run_id="run-20", api_id="api-1", executed_at=datetime.now(UTC), target_env="dev",
                                 status=RunStatus.ERROR, http_status=500))
    state.remember_run(RunResult(run_id="run-21", api_id="api-1", executed_at=datetime.now(UTC), target_env="dev",
                                 status=RunStatus.PASS, http_status=200))
    state.last_listed_run_ids = ["run-17", "run-20", "run-21"]
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_run_digest"],
        {"items": [
            {"kind": "fail", "ref_id": "run-20", "headline": "x"},
            {"kind": "fail", "ref_id": "run-21", "headline": "y"},
        ]},
        _ctx(state), "Shown.",
    )
    items = outcome.events[0].data["payload"]["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "error"
    assert "run-21" in outcome.result_text


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
    state.remember_job(JobSpec(job_id="job-0001", kind=JobKind.RUN_NOW, summary="s", api_ids=["api-1"], target_envs=["dev"],
                               confidence={"target_envs": 0.3}, assumptions=["target_env defaulted to dev"],
                               created_at=datetime.now(UTC), created_by="op"))
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_job_preview"], {"job_id": "job-0001", "headline": "h"}, _ctx(state), "Shown.")
    payload = outcome.events[0].data["payload"]
    assert payload["job"]["job_id"] == "job-0001" and payload["job"]["confidence"]["target_envs"] == 0.3
    assert payload["low_confidence"] == ["target_envs"]


async def test_job_preview_job_record_carries_change_id_for_the_card_buttons():
    # The card hands payload.job to web-shared's useChangeActions, which posts to
    # /changes/{change.change_id}/apply — without the alias the click went to /changes/undefined/apply.
    state = AtworksSessionState()
    state.remember_job(JobSpec(job_id="job-0001", kind=JobKind.RUN_NOW, summary="s", api_ids=["api-1"], target_envs=["dev"],
                               created_at=datetime.now(UTC), created_by="op"))
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_job_preview"], {"job_id": "job-0001"}, _ctx(state), "Shown.")
    job = outcome.events[0].data["payload"]["job"]
    assert job["change_id"] == "job-0001"
    assert job["runs_total"] == 1 and job["total_executions"] == 1   # same record shape as GET /jobs


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


async def test_job_preview_carries_the_matrix_block():
    from atworks_agent.types import JobSchedule, TestDataSet

    state = AtworksSessionState()
    state.remember_job(JobSpec(
        job_id="job-0002", kind=JobKind.SCHEDULED_RUN, summary="s", api_ids=["api-1", "api-2"],
        target_envs=["dev", "stg"],
        schedules=[JobSchedule(kind="daily", at="09:00", from_date="2026-09-05", count=3)],
        test_data=[TestDataSet(label="S1 정상", values={"amount": "1000"})],
        created_at=datetime.now(UTC), created_by="op"))
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_job_preview"],
                                     {"job_id": "job-0002"}, _ctx(state), "Shown.")
    assert outcome.events[0].data["payload"]["matrix"] == {
        "apis": 2, "envs": ["dev", "stg"], "data_sets": ["S1 정상"],
        "executions": 3, "runs_per_execution": 4, "runs_total": 12,
        "max_runs_per_execution": 4, "max_runs_total": 12,
    }


async def test_job_preview_matrix_ceiling_for_late_binding_is_the_deployment_cap():
    from atworks_agent.types import Binding, JobSchedule

    config = AtworksAgentConfig(model="m", max_matrix_size=400)
    state = AtworksSessionState()
    state.remember_job(JobSpec(
        job_id="job-0003", kind=JobKind.SCHEDULED_RUN, summary="s", api_ids=["api-1", "api-2"],
        target_envs=["dev", "stg"], binding=Binding.LATE, select_where={"query": "x"},
        schedules=[JobSchedule(kind="daily", at="09:00", from_date="2026-09-05", count=3)],
        created_at=datetime.now(UTC), created_by="op"))
    outcome = await run_presentation(
        PRESENTATION_COMPONENTS["present_job_preview"], {"job_id": "job-0003"},
        EnrichmentContext(backend=None, config=config, session=SESSION, state=state), "Shown.",
    )
    matrix = outcome.events[0].data["payload"]["matrix"]
    assert matrix["max_runs_per_execution"] == 400
    assert matrix["max_runs_total"] == 400 * 3


async def test_job_preview_matrix_ceiling_for_frozen_binding_equals_the_staged_figures():
    state = AtworksSessionState()
    state.remember_job(JobSpec(
        job_id="job-0004", kind=JobKind.RUN_NOW, summary="s", api_ids=["api-1"], target_envs=["dev"],
        created_at=datetime.now(UTC), created_by="op"))
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_job_preview"],
                                     {"job_id": "job-0004"}, _ctx(state), "Shown.")
    matrix = outcome.events[0].data["payload"]["matrix"]
    assert matrix["max_runs_per_execution"] == matrix["runs_per_execution"]
    assert matrix["max_runs_total"] == matrix["runs_total"]


def _rule(**overrides):
    fields = {
        "rule_id": "rule-0001", "api_id": "api-1", "param": "amount", "kind": "compare",
        "op": ">=", "value": "0", "message": "amount >= 0",
        "confidence": {"value": 0.3}, "assumptions": ["value defaulted to 0"],
        "created_at": datetime.now(UTC), "created_by": "op",
    }
    fields.update(overrides)
    return ValidationRule(**fields)


async def test_rule_preview_joins_staged_record_and_impact():
    state = AtworksSessionState()
    state.remember_api(ApiSpec(api_id="api-1", method="POST", path="/v1/refunds", name="환불", updated_at=datetime.now(UTC)))
    state.remember_rule(_rule())
    state.rule_impacts["rule-0001"] = RuleImpact(window_runs=10, known_inputs=8, would_fail=2, excluded_unknown=2)
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_rule_preview"], {"rule_id": "rule-0001", "headline": "h"}, _ctx(state), "Shown.")
    payload = outcome.events[0].data["payload"]
    assert payload["rule"]["rule_id"] == "rule-0001" and payload["rule"]["op"] == ">="
    assert payload["change_id"] == "rule-0001"
    assert payload["review_required"] is False
    assert payload["low_confidence"] == ["value"]
    assert payload["api"]["api_id"] == "api-1"
    assert payload["impact"] == {"window_runs": 10, "known_inputs": 8, "would_fail": 2, "excluded_unknown": 2}


async def test_rule_preview_omits_api_and_impact_when_unknown():
    state = AtworksSessionState()
    state.remember_rule(_rule())
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_rule_preview"], {"rule_id": "rule-0001"}, _ctx(state), "Shown.")
    payload = outcome.events[0].data["payload"]
    assert "api" not in payload and "impact" not in payload


async def test_rule_preview_format_hint_for_named_format():
    from atworks_agent.rules import NAMED_FORMATS

    state = AtworksSessionState()
    state.remember_rule(_rule(kind="format", op=None, value=None, format="email", message="email matches email"))
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_rule_preview"], {"rule_id": "rule-0001"}, _ctx(state), "Shown.")
    payload = outcome.events[0].data["payload"]
    assert payload["format_hint"] == {"label": "email", "example": "user@example.com", "pattern": NAMED_FORMATS["email"]}


async def test_rule_preview_format_hint_for_raw_pattern():
    state = AtworksSessionState()
    state.remember_rule(_rule(kind="format", op=None, value=None, pattern="^[A-Z]{3}$", message="code matches /^[A-Z]{3}$/"))
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_rule_preview"], {"rule_id": "rule-0001"}, _ctx(state), "Shown.")
    payload = outcome.events[0].data["payload"]
    assert payload["format_hint"] == {"label": "정규식", "example": None, "pattern": "^[A-Z]{3}$"}


async def test_rule_preview_format_hint_includes_examples_for_raw_pattern():
    state = AtworksSessionState()
    state.remember_rule(_rule(kind="format", op=None, value=None, pattern="^[A-Z]{3}$",
                              pass_examples=["ABC"], fail_examples=["ab1"],
                              message="code matches /^[A-Z]{3}$/"))
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_rule_preview"], {"rule_id": "rule-0001"}, _ctx(state), "Shown.")
    payload = outcome.events[0].data["payload"]
    assert payload["format_hint"] == {
        "label": "정규식", "example": None, "pattern": "^[A-Z]{3}$",
        "pass_examples": ["ABC"], "fail_examples": ["ab1"],
    }


async def test_rule_preview_refuses_unknown_rule():
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_rule_preview"], {"rule_id": "nope"}, _ctx(AtworksSessionState()), "Shown.")
    assert outcome.blocked == "provenance"


def _state_with_groups():
    state = AtworksSessionState()
    g1 = RunGroup(key="amount <= limit", label="amount <= limit", count=3, fail=3, error=0, passed=0, run_ids=["run-1"])
    g2 = RunGroup(key="(error) HTTP 503", label="(error) HTTP 503", count=1, fail=0, error=1, passed=0, run_ids=["run-5"])
    state.remember_groups("failed_rule", [g1, g2], None)
    state.last_population = 9
    state.last_listed_filter = "non_pass"
    return state


async def test_run_groups_joins_session_groups_and_carries_population():
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_run_groups"],
                                     {"title": "원인별", "group_keys": ["amount <= limit", "(error) HTTP 503"]},
                                     _ctx(_state_with_groups()), "Shown.")
    payload = outcome.events[0].data["payload"]
    assert payload["group_by"] == "failed_rule" and payload["population"] == 9 and payload["shown"] == 2
    assert payload["items"][0]["fail"] == 3 and payload["items"][1]["error"] == 1


async def test_run_groups_refuses_without_an_aggregate_call():
    outcome = await run_presentation(PRESENTATION_COMPONENTS["present_run_groups"], {"group_keys": ["x"]},
                                     _ctx(AtworksSessionState()), "Shown.")
    assert outcome.is_error and "aggregate_runs" in outcome.result_text


async def test_run_groups_unknown_keys_are_provenance_when_all_and_a_note_when_some():
    state = _state_with_groups()
    all_bad = await run_presentation(PRESENTATION_COMPONENTS["present_run_groups"], {"group_keys": ["nope"]}, _ctx(state), "Shown.")
    assert all_bad.blocked == "provenance"
    some = await run_presentation(PRESENTATION_COMPONENTS["present_run_groups"],
                                  {"group_keys": ["amount <= limit", "nope"]}, _ctx(state), "Shown.")
    assert not some.is_error and "Dropped nope" in some.result_text
    assert some.events[0].data["payload"]["shown"] == 1
