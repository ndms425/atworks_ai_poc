"""명명된 결정론 스코어러. LLM은 이름만 고르고 가중치·피처는 여기서 고정된다. 같은 입력 → 같은
순서. 출력은 '먼저 볼 순서'이지 판정이 아니다: pass는 절대 순위에 오르지 않고, fail/error는
전량이 모수(population)로 카드에 표시된다."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from .types import ApiSpec, FailedRank, RunResult, RunStatus

Scorer = Callable[[RunResult, Sequence[RunResult], Mapping[str, ApiSpec]], tuple[float, list[str]]]


class UnknownScorer(ValueError):
    pass


def _risk_v1(run: RunResult, all_runs: Sequence[RunResult], apis: Mapping[str, ApiSpec]) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    same_api = [r for r in all_runs if r.api_id == run.api_id and r.status is not RunStatus.PASS]
    if len(same_api) >= 2:
        score += 3.0 * (len(same_api) - 1)
        reasons.append(f"{len(same_api)} consecutive non-pass runs on this API")
    if run.status is RunStatus.ERROR:
        score += 4.0
        reasons.append("error (no verdict from rules — transport or 5xx)")
    if run.http_status is not None and run.http_status >= 500:
        score += 2.0
        reasons.append(f"HTTP {run.http_status}")
    if run.failed_rules:
        score += 1.0 * len(run.failed_rules)
        reasons.append(f"{len(run.failed_rules)} rule(s) failed: {', '.join(run.failed_rules[:3])}")
    api = apis.get(run.api_id)
    if api is not None and not api.has_rules:
        score += 0.5
        reasons.append("API has no value rules registered — failure is transport-level only")
    return score, reasons


SCORERS: dict[str, Scorer] = {"risk_v1": _risk_v1}


def rank_runs(
    scorer_name: str, runs: Sequence[RunResult], apis: Mapping[str, ApiSpec], limit: int
) -> list[FailedRank]:
    scorer = SCORERS.get(scorer_name)
    if scorer is None:
        raise UnknownScorer(f"no scorer named {scorer_name!r}; available: {', '.join(sorted(SCORERS))}")
    candidates = [r for r in runs if r.status is not RunStatus.PASS]
    # API당 최신 1건만 순위에 올린다 — 같은 API가 카드를 도배하지 않게.
    latest: dict[str, RunResult] = {}
    for r in sorted(candidates, key=lambda x: x.executed_at):
        latest[r.api_id] = r
    scored = []
    for r in latest.values():
        score, reasons = scorer(r, candidates, apis)
        scored.append(FailedRank(run_id=r.run_id, api_id=r.api_id, scorer=scorer_name, score=score, reasons=reasons))
    scored.sort(key=lambda x: (-x.score, x.api_id, x.run_id))
    return scored[:limit]
