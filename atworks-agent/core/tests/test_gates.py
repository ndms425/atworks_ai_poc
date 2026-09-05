from datetime import UTC, datetime

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.gates import (
    APPROVAL_GATE,
    FIGURES_IN_PROSE_REMINDER,
    PROVENANCE_GATE,
    STAGING_FOLLOWTHROUGH_REMINDER,
    check_api_provenance,
    check_apply_job,
    check_apply_rule,
    check_discard_job,
    check_discard_rule,
    check_rule_param_provenance,
    prose_restates_figures,
    take_rule_discard_actor_kind,
    turn_attempted_staging,
)
from atworks_agent.rules import ValidationRule
from atworks_agent.types import ActorKind, ApiSpec, AtworksSessionState, JobKind, JobSpec

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


def _rule(rule_id="rule-0001", api_id="api-1"):
    return ValidationRule(rule_id=rule_id, api_id=api_id, param="amount", kind="compare", op=">=",
                          value="0", message="amount >= 0", created_at=datetime.now(UTC), created_by="op")


def test_check_rule_param_provenance_unknown_api():
    state = AtworksSessionState()
    held = check_rule_param_provenance(state, "api-1", "amount")
    assert held is not None and held.blocked == PROVENANCE_GATE


def test_check_rule_param_provenance_unknown_param():
    state = AtworksSessionState()
    state.remember_api(ApiSpec(api_id="api-1", method="GET", path="/x", name="x",
                               updated_at=datetime.now(UTC), params=["amount"]))
    held = check_rule_param_provenance(state, "api-1", "bogus")
    assert held is not None and held.blocked == PROVENANCE_GATE
    assert "bogus" in held.result_text


def test_check_rule_param_provenance_ok():
    state = AtworksSessionState()
    state.remember_api(ApiSpec(api_id="api-1", method="GET", path="/x", name="x",
                               updated_at=datetime.now(UTC), params=["amount"]))
    assert check_rule_param_provenance(state, "api-1", "amount") is None


def test_check_apply_rule_requires_seen_then_approval():
    state = AtworksSessionState()
    assert check_apply_rule(state, CFG, "rule-0001").blocked == PROVENANCE_GATE
    state.remember_rule(_rule())
    held = check_apply_rule(state, CFG, "rule-0001")
    assert held is not None and held.blocked == APPROVAL_GATE
    state.approved_rule_ids.add("rule-0001")
    assert check_apply_rule(state, CFG, "rule-0001") is None


def test_check_apply_rule_rechecks_guardrails_under_current_config():
    # Staged under a loose config (max_membership_values=50); apply-time config has since
    # tightened to 1 — check_apply_rule must hold it, mirroring check_apply_job.
    state = AtworksSessionState()
    membership_rule = ValidationRule(rule_id="rule-0002", api_id="api-1", param="status", kind="membership",
                                     op="in", values=["A", "B", "C"], message="status in [A, B, C]",
                                     created_at=datetime.now(UTC), created_by="op")
    state.remember_rule(membership_rule)
    state.approved_rule_ids.add("rule-0002")
    tight_cfg = AtworksAgentConfig(model="m", max_membership_values=1)
    held = check_apply_rule(state, tight_cfg, "rule-0002")
    assert held is not None and held.blocked == "guardrail"
    assert "3 values" in held.result_text and "limit is 1" in held.result_text


def test_check_discard_rule_and_actor_kind():
    state = AtworksSessionState()
    assert check_discard_rule(state, "rule-x").blocked == PROVENANCE_GATE
    state.remember_rule(_rule())
    assert check_discard_rule(state, "rule-0001") is None
    assert take_rule_discard_actor_kind(state, "rule-0001") is ActorKind.AGENT
    state.host_action_rule_ids.add("rule-0001")
    assert take_rule_discard_actor_kind(state, "rule-0001") is ActorKind.OPERATOR
    assert "rule-0001" not in state.host_action_rule_ids
