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

def unanchored(pattern: str) -> str:
    """A format-library pattern validates a WHOLE value (`^...$`); a masking rule must find the
    same shape EMBEDDED in prose ("문의: hong@example.com 으로"). Reuse the asset, drop the anchors."""
    return pattern.removeprefix("^").removesuffix("$")


DEFAULT_MASKING_RULES: list[MaskingRule] = [
    MaskingRule(name="krn-resident-id", pattern=r"\b\d{6}-?[1-4]\d{6}\b"),
    # 15-16 digits in 4-4-4-3/4 groups, separators optional but the GROUPING is not: the earlier
    # `\b(?:\d[ -]?){15,16}\b` had no grouping at all, so any 15-16 character run of digits and
    # single separators matched -- and a uuid's `...-446655440000` tail or a long order id read as
    # a card number and was replaced by `***`. Over-masking is not the safe direction here: two
    # parity bodies whose only difference sat inside an over-masked field both read `***` and the
    # report called them `equal`.
    MaskingRule(name="card-number", pattern=r"\b(?:\d{4}[ -]?){3}\d{3,4}\b"),
    # Separators REQUIRED (`-?` -> `-`): without them this pattern is "any 9-17 digit run", which
    # is what ate `/v1/payments/1234567890` and `ORD-2026-000123456`. A Korean account number is
    # always written in groups; a bare digit run is an id, not an account.
    MaskingRule(name="account-number", pattern=r"\b\d{3}-\d{2,6}-\d{4,8}\b"),
    MaskingRule(name="phone", pattern=r"\b01[016789]-?\d{3,4}-?\d{4}\b"),
    MaskingRule(name="email", pattern=unanchored(NAMED_FORMATS["email"])),
]


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _mask(node: Any, policy: MaskingPolicy, path: str, rewritten: list[str]) -> Any:
    """The recursion both public entry points share. ``path`` is the JSON path of ``node`` in the
    exact shape ``parity.compare_bodies`` speaks (``$.a.b[2]``), and every leaf this actually
    changes appends its own path to ``rewritten``."""
    if isinstance(node, dict):
        return {key: _mask(value, policy, f"{path}.{key}", rewritten) for key, value in node.items()}
    if isinstance(node, list):
        return [_mask(item, policy, f"{path}[{i}]", rewritten) for i, item in enumerate(node)]
    if isinstance(node, str):
        masked = node
        for rule in policy.rules:
            masked = _compiled(rule.pattern).sub(rule.replacement, masked)
        if masked != node:
            rewritten.append(path)
        return masked
    return node


def mask_body(body: Any, policy: MaskingPolicy) -> Any:
    """Recursive, pure. String leaves get every rule's `re.sub(pattern, replacement)` applied in
    order; dict keys and non-string leaves (int/bool/float/None) pass through untouched. No rules
    (e.g. `masking_enabled=False` baked into the policy by `policy_from_config`) is a no-op."""
    return _mask(body, policy, "$", [])


def mask_body_paths(body: Any, policy: MaskingPolicy) -> tuple[Any, list[str]]:
    """``mask_body``'s result **and** the JSON paths it rewrote, in document order.

    Masking is one-way and lossy by design, so two bodies whose only difference sat inside a
    masked leaf both read ``***`` and compare equal (see ``test_masking``'s "by design" case).
    That is fine as long as nobody reports it AS equality -- which the parity block used to do,
    verbatim, with ``basis: "body"``. Recording the paths at capture (``bodies.masked_paths``) is
    what lets the report say "이 경로는 판정하지 않았다" instead of claiming a comparison it could
    not make: a verdict must never rest on a value the comparer was not allowed to see."""
    rewritten: list[str] = []
    masked = _mask(body, policy, "$", rewritten)
    return masked, rewritten


def body_capture_enabled(api: ApiSpec, policy: MaskingPolicy) -> bool:
    """False iff the API's group is in the policy's `disabled_groups` -- the per-group capture
    opt-out. An API with no group (`api.group is None`) is never opted out by a group name."""
    return api.group not in policy.disabled_groups


def policy_from_config(config: AtworksAgentConfig) -> MaskingPolicy:
    """`masking_enabled=False` empties `rules` (masking is off, full stop) but `disabled_groups`
    is still honoured -- capture opt-out is a separate switch from the masking rules themselves."""
    rules = list(DEFAULT_MASKING_RULES) if config.masking_enabled else []
    return MaskingPolicy(rules=rules, disabled_groups=list(config.masking_disabled_groups))
