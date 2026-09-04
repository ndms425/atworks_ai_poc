"""grounding 규칙(우선순위 순): 묶기/원인별/불안정 질문은 aggregate_runs에서 시작하고, 실패/에러/최근 질문은 list_runs에서
시작하고, 이 세션에서 job을 본 적이 없는데 승인/적용을 말하면 get_pending_jobs에서 시작한다.
merchant_agent/grounding.py 미러. 한국어는 조사가 붙어 단어경계가 없으므로 한글 needle은 substring으로 본다."""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from commerce_common.grounding import GroundingRule, matches_any

from .config import AtworksAgentConfig
from .types import AtworksSessionState

_HANGUL = re.compile(r"[ㄱ-ㆎ가-힣]")


def matches_any_ko(text: str, needles: Sequence[str]) -> bool:
    """한글이 든 needle은 substring, 그 외는 commerce_common의 whole-word 매칭."""
    lowered = text.lower()
    ko = [n for n in needles if _HANGUL.search(n)]
    en = [n for n in needles if not _HANGUL.search(n)]
    if any(n.strip() and n.strip().lower() in lowered for n in ko):
        return True
    return matches_any(text, en) if en else False


def matches_terms_and_cues_ko(text: str, terms: Sequence[str], cues: Sequence[str]) -> bool:
    if not text or not terms or not cues:
        return False
    return matches_any_ko(text, cues) and matches_any_ko(text, terms)


def job_requested(config: AtworksAgentConfig, text: str) -> bool:
    return (
        config.stages_jobs
        and config.staging_followthrough_gate
        and matches_terms_and_cues_ko(text, config.job_intent_terms, config.job_intent_cues)
    )


def _aggregate(config: AtworksAgentConfig, text: str, _: AtworksSessionState) -> dict[str, Any] | None:
    # Terms only: "묶어/원인별/언제부터/왔다갔다/불안정" are specific enough that a request cue adds
    # nothing, and the runs rule (which needs a cue) must not steal these.
    fires = config.aggregate_grounding_gate and matches_any_ko(text, config.aggregate_intent_terms)
    return {} if fires else None


def _runs(config: AtworksAgentConfig, text: str, _: AtworksSessionState) -> dict[str, Any] | None:
    fires = config.runs_grounding_gate and matches_terms_and_cues_ko(
        text, config.runs_intent_terms, config.runs_intent_cues
    )
    return {} if fires else None


def _queue(config: AtworksAgentConfig, text: str, state: AtworksSessionState) -> dict[str, Any] | None:
    lowered = text.lower()
    fires = (
        config.queue_grounding_gate
        and not state.seen_jobs
        and config.stages_jobs
        and any(phrase in lowered for phrase in config.apply_intent_phrases)
    )
    return {} if fires else None


GROUNDING_RULES: tuple[GroundingRule, ...] = (
    GroundingRule(
        "aggregate",
        "aggregate_runs",
        _aggregate,
        prefetch_intro=lambda _: "Aggregated run groups for this turn, fetched by the host (the same data an aggregate_runs call returns):",
    ),
    GroundingRule(
        "runs",
        "list_runs",
        _runs,
        prefetch_intro=lambda _: "Recent run results for this turn, fetched by the host (the same data a list_runs call returns):",
    ),
    GroundingRule(
        "queue",
        "get_pending_jobs",
        _queue,
        prefetch_intro=lambda _: "Pending job queue for this turn, fetched by the host (the same data a get_pending_jobs call returns):",
    ),
)
