from datetime import UTC, datetime

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.gates import (
    APPROVAL_GATE,
    FIGURES_IN_PROSE_REMINDER,
    PROVENANCE_GATE,
    STAGING_FOLLOWTHROUGH_REMINDER,
    check_api_provenance,
    check_apply_job,
    check_discard_job,
    prose_restates_figures,
    turn_attempted_staging,
)
from atworks_agent.types import ApiSpec, AtworksSessionState, JobKind, JobSpec

CFG = AtworksAgentConfig(model="m")


def _job(job_id="job-0001", envs=("dev",)):
    return JobSpec(job_id=job_id, kind=JobKind.RUN_NOW, summary="s", api_ids=["api-1"],
                   target_envs=list(envs), created_at=datetime.now(UTC), created_by="op")


def test_unknown_api_id_is_held_by_provenance():
    state = AtworksSessionState()
    held = check_api_provenance(state, ["api-1", "api-2"])
    assert held is not None and held.blocked == PROVENANCE_GATE
    assert "api-1, api-2" in held.result_text


def test_seen_api_passes():
    state = AtworksSessionState()
    state.remember_api(ApiSpec(api_id="api-1", method="GET", path="/x", name="x", updated_at=datetime.now(UTC)))
    assert check_api_provenance(state, ["api-1"]) is None


def test_provenance_message_stays_bounded_for_an_oversized_api_id_list():
    state = AtworksSessionState()
    unknown = [f"api-{i}" for i in range(200)]
    held = check_api_provenance(state, unknown)
    assert held is not None and held.blocked == PROVENANCE_GATE
    assert len(held.result_text) < 400
    assert "… and 195 more" in held.result_text


def test_apply_requires_seen_then_approval():
    state = AtworksSessionState()
    assert check_apply_job(state, CFG, "job-0001").blocked == PROVENANCE_GATE
    state.remember_job(_job())
    held = check_apply_job(state, CFG, "job-0001")
    assert held is not None and held.blocked == APPROVAL_GATE and CFG.approval_surface in held.result_text
    state.approved_job_ids.add("job-0001")
    assert check_apply_job(state, CFG, "job-0001") is None


def test_apply_rechecks_guardrails_under_current_config():
    # A prod anywhere in the list — not only a sole env — must still be held at apply.
    state = AtworksSessionState()
    state.remember_job(_job(envs=["dev", "prod"]))
    state.approved_job_ids.add("job-0001")
    held = check_apply_job(state, CFG, "job-0001")
    assert held is not None and held.blocked == "guardrail" and "prod" in held.result_text


def test_discard_and_followthrough_helpers():
    state = AtworksSessionState()
    assert check_discard_job(state, "job-x").blocked == PROVENANCE_GATE
    assert turn_attempted_staging(["search_apis", "mcp__atworks__stage_job"])
    assert not turn_attempted_staging(["search_apis"])


def test_staging_followthrough_reminder_names_the_list_valued_slots():
    assert "target_envs" in STAGING_FOLLOWTHROUGH_REMINDER
    assert "target_env," not in STAGING_FOLLOWTHROUGH_REMINDER
    assert "target_env " not in STAGING_FOLLOWTHROUGH_REMINDER


def test_figures_in_prose_reminder_does_not_ask_to_restate_reasoning():
    assert "Do not mention this check" in FIGURES_IN_PROSE_REMINDER
    assert "present_suggestions" in FIGURES_IN_PROSE_REMINDER


def test_prose_restates_figures_positives():
    assert prose_restates_figures("| 기간 | 실패율 |\n|---|---|\n| 이번 주 | 100% |")
    assert prose_restates_figures("이번 주 성공률은 100% 입니다.")
    assert prose_restates_figures("실패 3건이 있었습니다.")
    assert prose_restates_figures("fail rate 50% this week.")


def test_prose_restates_figures_negatives():
    assert not prose_restates_figures("run-0012 상세를 보시려면 클릭하세요.")
    assert not prose_restates_figures("2026-09-03 09:00 실행 예정입니다.")
    assert not prose_restates_figures("HTTP 503 error가 발생했습니다.")
    assert not prose_restates_figures("api-003 스펙을 확인하세요.")
