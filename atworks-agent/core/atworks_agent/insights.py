"""per-operator 인사이트 패널의 결정론적 심장부. 여기서 나온 숫자가 패널이 보여줄 전부이고, 이후
어떤 서술(narration)도 이 값을 바꾸지 않는다. LLM은 없다: scope는 실행 이력에서, candidate는
aggregation.py의 기존 집계 함수(중복 구현 금지)와 job ledger에서 순수 함수로 뽑는다."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from .aggregation import aggregate
from .config import AtworksAgentConfig
from .types import (
    ApiSpec,
    InsightCandidate,
    InsightKind,
    JobSpec,
    JobStatus,
    OperatorRole,
    RunResult,
)

ROLE_PRIORITY: dict[OperatorRole, list[InsightKind]] = {
    "developer": ["regression_suspect", "top_failed_rule", "flaky_cell", "env_divergence", "stale_pending"],
    "qa": ["flaky_cell", "env_divergence", "top_failed_rule", "regression_suspect", "stale_pending"],
    "pm": ["stale_pending", "top_failed_rule", "regression_suspect", "flaky_cell", "env_divergence"],
}

KIND_LABEL: dict[InsightKind, str] = {
    "regression_suspect": "회귀 의심",
    "flaky_cell": "불안정 실행",
    "top_failed_rule": "자주 깨지는 규칙",
    "env_divergence": "환경 간 결과 불일치",
    "stale_pending": "오래된 승인 대기",
}


def operator_scope(runs: Sequence[RunResult], operator_id: str, window_days: int, now: datetime) -> set[str]:
    """이 오퍼레이터가 최근 window_days일 안에 직접 실행한(executed_by 일치) run들의 api_id 집합.
    빈 결과는 빈 set — 프로젝트 전체로 폴백할지는 호출자가 결정한다."""
    cutoff = now - timedelta(days=window_days)
    return {r.api_id for r in runs if r.executed_by == operator_id and r.executed_at >= cutoff}


def _severity(figures: dict[str, int | str]) -> int:
    return int(figures.get("fail") or figures.get("transitions") or figures.get("age_hours") or 0)


_MAX_IDS = 20  # InsightCandidate.api_ids / ref_ids schema cap (types.py) — never exceed it


def _capped_set(ids) -> list[str]:
    """Bound an id collection to the schema's max_length so construction can never raise, no matter
    how many APIs/runs land in one bucket. Sorted first (ids may come from a set, whose iteration
    order isn't deterministic) so the truncated sample stays stable across calls."""
    return sorted(set(ids))[:_MAX_IDS]


def _capped(ids: list[str]) -> list[str]:
    """Bound an already-ordered id list (e.g. newest-first run ids) to the schema's max_length
    without disturbing that order."""
    return ids[:_MAX_IDS]


def candidate_insights(
    runs: Sequence[RunResult],
    apis: Mapping[str, ApiSpec],
    jobs: Sequence[JobSpec],
    scope: set[str] | None,
    role: OperatorRole,
    config: AtworksAgentConfig,
    now: datetime,
) -> list[InsightCandidate]:
    """scope(None이면 프로젝트 전체)로 걸러진 입력에서 5종 후보를 만들어 role의 우선순위표대로
    정렬하고 max_insight_candidates로 자른다. 숫자는 전부 aggregation.aggregate와 job ledger에서
    나온다 — 여기서 새로 만드는 값은 env_divergence 문자열과 stale_pending 나이뿐이다."""
    if scope is not None:
        runs = [r for r in runs if r.api_id in scope]
        apis = {api_id: api for api_id, api in apis.items() if api_id in scope}
        jobs = [j for j in jobs if any(a in scope for a in j.api_ids)]

    run_lookup = {r.run_id: r.api_id for r in runs}
    candidates: list[InsightCandidate] = []

    # -- regression_suspect ------------------------------------------------------------
    for g in aggregate(runs, apis, "api", flaky_min_transitions=config.flaky_min_transitions):
        if not g.regression_suspect:
            continue
        if g.first_non_pass_at is None or g.api_updated_at is None:
            continue  # regression_suspect implies both are set; skip defensively if not
        candidates.append(InsightCandidate(
            candidate_id=f"regression_suspect:{g.key}",
            kind="regression_suspect",
            label=f"{KIND_LABEL['regression_suspect']} · {g.key}",
            figures={
                "fail": g.fail,
                "first_failure_at": g.first_non_pass_at.isoformat(),
                "api_updated_at": g.api_updated_at.isoformat(),
            },
            api_ids=_capped([g.key]),
            ref_ids=_capped(g.run_ids[:5]),
        ))

    # -- flaky_cell ----------------------------------------------------------------------
    for g in aggregate(runs, apis, "api_env_data", flaky_min_transitions=config.flaky_min_transitions):
        if not g.flaky:
            continue
        cell_api = g.key.split("|", 1)[0]
        candidates.append(InsightCandidate(
            candidate_id=f"flaky_cell:{g.key}",
            kind="flaky_cell",
            label=f"{KIND_LABEL['flaky_cell']} · {g.key}",
            figures={"transitions": g.transitions, "runs": g.count},
            api_ids=_capped([cell_api]),
            ref_ids=_capped(g.run_ids[:5]),
        ))

    # -- top_failed_rule -------------------------------------------------------------
    by_rule = aggregate(runs, apis, "failed_rule", flaky_min_transitions=config.flaky_min_transitions)
    for g in sorted(by_rule, key=lambda g: (-g.fail, g.key))[:3]:
        rule_api_id_set = {run_lookup[rid] for rid in g.run_ids if rid in run_lookup}
        candidates.append(InsightCandidate(
            candidate_id=f"top_failed_rule:{g.key}",
            kind="top_failed_rule",
            label=f"{KIND_LABEL['top_failed_rule']} · {g.key}",
            # figures reports the TRUE api count; api_ids is a bounded (<=20) sample of it.
            figures={"fail": g.fail, "apis": len(rule_api_id_set)},
            api_ids=_capped_set(rule_api_id_set),
            ref_ids=_capped(g.run_ids[:5]),
        ))

    # -- env_divergence ------------------------------------------------------------------
    # No aggregation helper covers this axis: per api, the LATEST run per target_env, then
    # compare those latest statuses across envs.
    runs_by_api: dict[str, list[RunResult]] = defaultdict(list)
    for r in runs:
        runs_by_api[r.api_id].append(r)
    for api_id in sorted(runs_by_api):
        runs_by_env: dict[str, list[RunResult]] = defaultdict(list)
        for r in runs_by_api[api_id]:
            runs_by_env[r.target_env].append(r)
        if len(runs_by_env) < 2:
            continue
        latest_per_env = {
            env: max(env_runs, key=lambda r: (r.executed_at, r.run_id))
            for env, env_runs in runs_by_env.items()
        }
        if len({r.status for r in latest_per_env.values()}) < 2:
            continue
        envs_str = ",".join(f"{env}={latest_per_env[env].status.value}" for env in sorted(latest_per_env))
        candidates.append(InsightCandidate(
            candidate_id=f"env_divergence:{api_id}",
            kind="env_divergence",
            label=f"{KIND_LABEL['env_divergence']} · {api_id}",
            figures={"envs": envs_str},
            api_ids=_capped([api_id]),
            ref_ids=_capped_set(r.run_id for r in latest_per_env.values()),
        ))

    # -- stale_pending ---------------------------------------------------------------
    for job in jobs:
        if job.status is not JobStatus.STAGED:
            continue
        age = now - job.created_at
        if age < timedelta(hours=config.stale_pending_hours):
            continue
        job_api_id_set = set(job.api_ids)
        candidates.append(InsightCandidate(
            candidate_id=f"stale_pending:{job.job_id}",
            kind="stale_pending",
            label=f"{KIND_LABEL['stale_pending']} · {job.job_id}",
            # figures reports the TRUE api count; api_ids is a bounded (<=20) sample of it.
            figures={"age_hours": int(age.total_seconds() // 3600), "apis": len(job_api_id_set)},
            api_ids=_capped_set(job_api_id_set),
            ref_ids=_capped([job.job_id]),
        ))

    priority_order = ROLE_PRIORITY[role]
    for c in candidates:
        c.priority = priority_order.index(c.kind)
    candidates.sort(key=lambda c: (c.priority, -_severity(c.figures), c.candidate_id))
    return candidates[: config.max_insight_candidates]
