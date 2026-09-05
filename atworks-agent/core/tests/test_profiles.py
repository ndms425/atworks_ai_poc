import json

import pytest

from atworks_agent.config import AtworksAgentConfig
from atworks_agent.profiles import (
    ProfileDraft,
    ProfileGuardrailViolation,
    ProfileLedger,
    check_profile_guardrails,
    is_valid_ignore_path,
)
from atworks_agent.types import (  # noqa: F401 -- interface check: importable from types
    ActorKind,
    AtworksSessionState,
    ComparisonProfile,
    ProfileStatus,
)

CFG = AtworksAgentConfig(model="m")


def _draft(**over):
    base = dict(job_id="job-0001", ignore_paths=["$.serverTime"], summary="ignore server-generated fields")
    base.update(over)
    return ProfileDraft(**base)


# -- path validation ------------------------------------------------------------------


def test_is_valid_ignore_path_accepts_dotted_and_bracket_forms():
    assert is_valid_ignore_path("$.serverTime")
    assert is_valid_ignore_path("$.a.b")
    assert is_valid_ignore_path("$.arr[0]")
    assert is_valid_ignore_path("$.arr[0].id")


def test_is_valid_ignore_path_rejects_empty_and_unrooted():
    assert not is_valid_ignore_path("")
    assert not is_valid_ignore_path("serverTime")
    assert not is_valid_ignore_path("$")
    assert not is_valid_ignore_path(".a.b")


def test_profile_draft_rejects_invalid_ignore_path():
    with pytest.raises(ValueError, match="serverTime"):
        ProfileDraft(job_id="job-0001", ignore_paths=["serverTime"], summary="s")


def test_profile_draft_rejects_invalid_per_api_ignore_path():
    with pytest.raises(ValueError):
        ProfileDraft(job_id="job-0001", per_api_ignore={"api-1": ["$"]}, summary="s")


def test_profile_draft_extra_field_forbidden():
    with pytest.raises(ValueError):
        ProfileDraft(job_id="job-0001", ignore_paths=["$.a"], summary="s", bogus="x")


# -- guardrails -------------------------------------------------------------------------


def test_check_profile_guardrails_within_cap():
    assert check_profile_guardrails(_draft(), CFG) == []


def test_check_profile_guardrails_over_cap_counts_ignore_paths_and_per_api_ignore():
    cfg = AtworksAgentConfig(model="m", max_ignore_paths=2)
    draft = _draft(ignore_paths=["$.a", "$.b"], per_api_ignore={"api-1": ["$.c"]})
    v = check_profile_guardrails(draft, cfg)
    assert any("3" in m and "limit is 2" in m for m in v)


def test_stage_raises_profile_guardrail_violation_over_cap():
    cfg = AtworksAgentConfig(model="m", max_ignore_paths=1)
    ledger = ProfileLedger(cfg)
    with pytest.raises(ProfileGuardrailViolation):
        ledger.stage(_draft(ignore_paths=["$.a", "$.b"]), actor="op")


# -- ledger lifecycle ---------------------------------------------------------------------


def test_stage_apply_stamps_effective_from_and_status():
    ledger = ProfileLedger(CFG)
    profile = ledger.stage(_draft(), actor="op")
    assert profile.profile_id == "profile-0001" and profile.status is ProfileStatus.STAGED
    assert profile.effective_from is None
    applied = ledger.apply(profile.profile_id, actor="op")
    assert applied.status is ProfileStatus.APPLIED
    assert applied.effective_from is not None
    assert applied.applied_at is not None and applied.applied_by == "op"


def test_second_stage_is_independent():
    ledger = ProfileLedger(CFG)
    first = ledger.stage(_draft(job_id="job-0001"), actor="op")
    second = ledger.stage(_draft(job_id="job-0002"), actor="op")
    assert first.profile_id != second.profile_id
    ledger.apply(first.profile_id, actor="op")
    assert ledger.get(second.profile_id).status is ProfileStatus.STAGED


def test_apply_refuses_non_staged_or_unknown_id():
    ledger = ProfileLedger(CFG)
    with pytest.raises(ProfileGuardrailViolation):
        ledger.apply("profile-9999", actor="op")
    profile = ledger.stage(_draft(), actor="op")
    ledger.apply(profile.profile_id, actor="op")
    with pytest.raises(ProfileGuardrailViolation):
        ledger.apply(profile.profile_id, actor="op")


def test_discard_records_actor_kind():
    ledger = ProfileLedger(CFG)
    profile = ledger.stage(_draft(), actor="op")
    discarded = ledger.discard(profile.profile_id, actor="assistant", actor_kind=ActorKind.AGENT)
    assert discarded.status is ProfileStatus.DISCARDED
    assert discarded.discarded_by_kind is ActorKind.AGENT
    assert discarded.discarded_by == "assistant"


def test_pending_applied_and_list_filter_by_job():
    ledger = ProfileLedger(CFG)
    p1 = ledger.stage(_draft(job_id="job-0001"), actor="op")
    p2 = ledger.stage(_draft(job_id="job-0002"), actor="op")
    ledger.apply(p1.profile_id, actor="op")
    assert [p.profile_id for p in ledger.pending()] == [p2.profile_id]
    assert [p.profile_id for p in ledger.applied()] == [p1.profile_id]
    assert [p.profile_id for p in ledger.list(job_id="job-0001")] == [p1.profile_id]
    assert {p.profile_id for p in ledger.list()} == {p1.profile_id, p2.profile_id}


def test_apply_rechecks_guardrails_under_current_config():
    ledger = ProfileLedger(CFG)
    staged = ledger.stage(_draft(ignore_paths=["$.a", "$.b"]), actor="op")
    ledger._config = AtworksAgentConfig(model="m", max_ignore_paths=1)
    with pytest.raises(ProfileGuardrailViolation):
        ledger.apply(staged.profile_id, actor="op")


# -- session round-trip: the load-bearing regression guard -----------------------------


def test_session_state_round_trip_keeps_seen_profiles_typed():
    ledger = ProfileLedger(CFG)
    profile = ledger.stage(_draft(), actor="op")
    state = AtworksSessionState()
    state.remember_profile(profile)

    dumped = state.model_dump_json()
    restored = AtworksSessionState.model_validate(json.loads(dumped))

    restored_profile = restored.seen_profiles[profile.profile_id]
    assert isinstance(restored_profile, ComparisonProfile)
    assert restored_profile.job_id == "job-0001"
