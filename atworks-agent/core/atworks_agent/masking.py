"""캡처 시점 마스킹 (spec §7). `Store.ingest(..., mask=...)`가 `bodies` insert 직전에 부르는
순수 함수들 -- PII는 `bodies`(따라서 리포트의 data.json, parity diff)에 절대 닿지 않는다.
기본 규칙은 rule-authoring의 포맷 라이브러리와 같은 정규식 자산(이메일)을 재사용한다."""
from __future__ import annotations

import re
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from .rules import NAMED_FORMATS
from .types import ApiSpec, MaskingPolicy, MaskingRule

if TYPE_CHECKING:
    from .config import AtworksAgentConfig

DEFAULT_MASKING_RULES: list[MaskingRule] = [
    MaskingRule(name="krn-resident-id", pattern=r"\b\d{6}-?[1-4]\d{6}\b"),
    MaskingRule(name="card-number", pattern=r"\b(?:\d[ -]?){15,16}\b"),
    MaskingRule(name="account-number", pattern=r"\b\d{3}-?\d{2,6}-?\d{4,8}\b"),
    MaskingRule(name="phone", pattern=r"\b01[016789]-?\d{3,4}-?\d{4}\b"),
    MaskingRule(name="email", pattern=NAMED_FORMATS["email"]),
]


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def mask_body(body: Any, policy: MaskingPolicy) -> Any:
    """Recursive, pure. String leaves get every rule's `re.sub(pattern, replacement)` applied in
    order; dict keys and non-string leaves (int/bool/float/None) pass through untouched. No rules
    (e.g. `masking_enabled=False` baked into the policy by `policy_from_config`) is a no-op."""
    if isinstance(body, dict):
        return {key: mask_body(value, policy) for key, value in body.items()}
    if isinstance(body, list):
        return [mask_body(item, policy) for item in body]
    if isinstance(body, str):
        masked = body
        for rule in policy.rules:
            masked = _compiled(rule.pattern).sub(rule.replacement, masked)
        return masked
    return body


def body_capture_enabled(api: ApiSpec, policy: MaskingPolicy) -> bool:
    """False iff the API's group is in the policy's `disabled_groups` -- the per-group capture
    opt-out. An API with no group (`api.group is None`) is never opted out by a group name."""
    return api.group not in policy.disabled_groups


def policy_from_config(config: AtworksAgentConfig) -> MaskingPolicy:
    """`masking_enabled=False` empties `rules` (masking is off, full stop) but `disabled_groups`
    is still honoured -- capture opt-out is a separate switch from the masking rules themselves."""
    rules = list(DEFAULT_MASKING_RULES) if config.masking_enabled else []
    return MaskingPolicy(rules=rules, disabled_groups=list(config.masking_disabled_groups))
