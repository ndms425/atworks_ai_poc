from datetime import UTC, datetime

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.gates import (
    APPROVAL_GATE,
    PROVENANCE_GATE,
    STAGING_FOLLOWTHROUGH_REMINDER,
    check_api_provenance,
    check_apply_job,
    check_discard_job,
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
