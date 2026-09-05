"""값 동등성 비교의 ignore-spec 초안·원장. rules.py를 그대로 미러한다: stage가 제안하고,
apply가 effective_from을 찍는 유일한 상태 변화이며, 과거 비교 결과는 절대 다시 판정하지 않는다."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .types import ActorKind, ComparisonProfile, ProfileStatus

if TYPE_CHECKING:
    from .config import AtworksAgentConfig

# $ 뒤에 .key(dotted) 또는 [index](bracket) 세그먼트가 하나 이상 이어지는 형태만 허용한다.
# 예: $.serverTime, $.a.b, $.arr[0], $.arr[0].id -- 빈 문자열이나 $로 시작하지 않는 값은 거부한다.
_PATH_SEGMENT = re.compile(r"^\$(\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])+$")


def is_valid_ignore_path(path: str) -> bool:
    return bool(path) and bool(_PATH_SEGMENT.match(path))


def _validate_paths(paths: list[str]) -> None:
    for path in paths:
        if not is_valid_ignore_path(path):
            raise ValueError(
                f"ignore path {path!r} must be a non-empty, $-rooted dotted/bracket path "
                "(e.g. '$.serverTime', '$.items[0].id')"
            )


class ProfileDraft(BaseModel):
    """stage_profile 입력이 검증된 뒤의 모양. 백엔드는 이걸 받아 ComparisonProfile을 만든다."""
    model_config = ConfigDict(extra="forbid")
    job_id: str
    ignore_paths: list[str] = Field(default_factory=list)
    per_api_ignore: dict[str, list[str]] = Field(default_factory=dict)
    summary: str = Field(max_length=200)

    @model_validator(mode="after")
    def _paths_are_well_formed(self) -> ProfileDraft:
        _validate_paths(self.ignore_paths)
        for paths in self.per_api_ignore.values():
            _validate_paths(paths)
        return self


class ProfileGuardrailViolation(ValueError):
    def __init__(self, violations: list[str]):
        super().__init__("; ".join(violations))
        self.violations = violations


def _total_paths(draft: ProfileDraft) -> int:
    return len(draft.ignore_paths) + sum(len(v) for v in draft.per_api_ignore.values())


def check_profile_guardrails(draft: ProfileDraft, config: AtworksAgentConfig) -> list[str]:
    violations: list[str] = []
    total = _total_paths(draft)
    if total > config.max_ignore_paths:
        violations.append(f"profile has {total} ignore paths; the limit is {config.max_ignore_paths}")
    return violations


class ProfileLedger:
    def __init__(self, config: AtworksAgentConfig):
        self._config = config
        self._profiles: dict[str, ComparisonProfile] = {}
        self._sequence = 0

    def stage(self, draft: ProfileDraft, *, actor: str, actor_kind: ActorKind = ActorKind.OPERATOR) -> ComparisonProfile:
        if v := check_profile_guardrails(draft, self._config):
            raise ProfileGuardrailViolation(v)
        self._sequence += 1
        profile = ComparisonProfile(
            profile_id=f"profile-{self._sequence:04d}", job_id=draft.job_id,
            ignore_paths=list(draft.ignore_paths),
            per_api_ignore={k: list(v) for k, v in draft.per_api_ignore.items()},
            summary=draft.summary, created_at=datetime.now(UTC), created_by=actor, created_by_kind=actor_kind)
        self._profiles[profile.profile_id] = profile
        return profile

    def get(self, profile_id): return self._profiles.get(profile_id)
    def pending(self): return [p for p in self._profiles.values() if p.status is ProfileStatus.STAGED]
    def applied(self): return [p for p in self._profiles.values() if p.status is ProfileStatus.APPLIED]
    def list(self, job_id: str | None = None):
        return [p for p in self._profiles.values() if job_id is None or p.job_id == job_id]

    def apply(self, profile_id: str, *, actor: str) -> ComparisonProfile:
        profile = self._require_staged(profile_id, "apply")
        draft = ProfileDraft(job_id=profile.job_id, ignore_paths=list(profile.ignore_paths),
                             per_api_ignore={k: list(v) for k, v in profile.per_api_ignore.items()},
                             summary=profile.summary)
        if violations := check_profile_guardrails(draft, self._config):
            raise ProfileGuardrailViolation(violations)
        updated = profile.model_copy(update={"status": ProfileStatus.APPLIED, "applied_at": datetime.now(UTC),
                                             "applied_by": actor, "effective_from": datetime.now(UTC)})
        self._profiles[profile_id] = updated
        return updated

    def discard(self, profile_id: str, *, actor: str, actor_kind: ActorKind = ActorKind.OPERATOR) -> ComparisonProfile:
        profile = self._require_staged(profile_id, "discard")
        updated = profile.model_copy(update={"status": ProfileStatus.DISCARDED, "discarded_at": datetime.now(UTC),
                                             "discarded_by": actor, "discarded_by_kind": actor_kind})
        self._profiles[profile_id] = updated
        return updated

    def _require_staged(self, profile_id: str, action: str) -> ComparisonProfile:
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise ProfileGuardrailViolation([f"no profile {profile_id} to {action}"])
        if profile.status is not ProfileStatus.STAGED:
            raise ProfileGuardrailViolation([f"profile {profile_id} is {profile.status.value}, cannot {action}"])
        return profile
