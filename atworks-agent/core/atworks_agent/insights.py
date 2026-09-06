"""per-operator 인사이트 패널의 결정론적 심장부. 여기서 나온 숫자가 패널이 보여줄 전부이고, 이후
어떤 서술(narration)도 이 값을 바꾸지 않는다. LLM은 없다.

Task 8(scale spec §9)부터 이 모듈은 **질의 구동**이다: 입력이 run 목록이 아니라 이미 집계된
``InsightInputs``(rollup 기반 ``aggregate_runs`` 결과 + ``current_state`` + ``watermarks`` +
job)다 — 2000건 표본을 페이징해 만든 그룹이 아니라 창 안 **모든 API**가 들어 있는 그룹이라,
"가장 최근 2000건 밖의 회귀"가 더는 보이지 않는 일이 없다. 옛 시그니처는
``candidate_insights_from_runs``로 남아 있고(테스트·더블), 그 안에서 같은 ``InsightInputs``를
만들어 같은 함수를 부른다 — 판정 로직은 한 벌뿐이다."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from .aggregation import _keys, aggregate
from .config import AtworksAgentConfig
from .types import (
    ApiSpec,
    CellState,
    InsightCandidate,
    InsightKind,
    JobSpec,
    JobStatus,
    OperatorRole,
    RunGroup,
    RunResult,
    RunStatus,
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


@dataclass
class InsightInputs:
    """후보 5종이 필요로 하는 **이미 집계된** 입력. 호스트(``InsightPanels.build``)가 백엔드
    질의로 채우고, 테스트/더블은 ``candidate_insights_from_runs``가 run 목록에서 채운다.

    * ``groups_by_api`` — ``aggregate_runs(group_by="api")``: regression_suspect
    * ``cells`` — ``aggregate_runs(group_by="api_env_data")``: flaky_cell
    * ``groups_by_rule`` — ``aggregate_runs(group_by="failed_rule")``: top_failed_rule
    * ``current_state`` — 셀별 최신 상태: env_divergence (run 목록 없이 계마다 최신 판정 비교)
    * ``jobs`` — staged job: stale_pending
    * ``scope`` — 후보를 이 api 집합으로 한정(오라클 경로 전용). 호스트 경로는 백엔드가 이미
      서버 측 스코프 술어로 좁혀 오므로 ``None``을 넘긴다 — 여기서 다시 id 목록으로 거르는 순간
      그 목록의 상한이 곧 커버리지 구멍이 된다.

    ``watermarks``/``apis``는 Task 8 fix round 1에서 **제거**했다: 어떤 후보도 읽지 않는데
    호스트가 채우느라 회귀 의심 API마다 ``get_api``를 한 번씩 더 부르고 있었다.
    """
    groups_by_api: Sequence[RunGroup] = ()
    cells: Sequence[RunGroup] = ()
    groups_by_rule: Sequence[RunGroup] = ()
    current_state: Sequence[CellState] = ()
    jobs: Sequence[JobSpec] = ()
    scope: set[str] | None = None


def operator_scope(runs: Sequence[RunResult], operator_id: str, window_days: int, now: datetime) -> set[str]:
    """이 오퍼레이터가 최근 window_days일 안에 직접 실행한(executed_by 일치) run들의 api_id 집합.
    빈 결과는 빈 set — 프로젝트 전체로 폴백할지는 호출자가 결정한다. (호스트 경로는 이 함수 대신
    백엔드의 물질화된 ``operator_scope``를 쓴다; 여기 남은 건 순수 오라클/테스트용이다.)"""
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


def _in_scope(api_id: str, scope: set[str] | None) -> bool:
    return scope is None or api_id in scope


def candidate_insights(
    inputs: InsightInputs, role: OperatorRole, config: AtworksAgentConfig, now: datetime,
) -> list[InsightCandidate]:
    """집계된 입력에서 5종 후보를 만들어 role의 우선순위표대로 정렬하고 max_insight_candidates로
    자른다. 숫자는 전부 호스트가 이미 센 값이다 — 여기서 새로 만드는 값은 env_divergence 문자열과
    stale_pending 나이뿐이다. candidate_id 형식은 서술 캐시의 조인 키라 절대 바뀌지 않는다."""
    scope = inputs.scope
    candidates: list[InsightCandidate] = []

    # -- regression_suspect ------------------------------------------------------------
    for g in inputs.groups_by_api:
        if not g.regression_suspect or not _in_scope(g.key, scope):
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
    for g in inputs.cells:
        cell_api, cell_env, cell_data = g.key.split("|", 2)
        if not g.flaky or not _in_scope(cell_api, scope):
            continue
        # The raw key's trailing segment is "-" when the run carried no test_data_label — drop
        # it from the LABEL only (candidate_id keeps the raw key: it's the narrative cache join).
        label_parts = [cell_api, cell_env] + ([cell_data] if cell_data and cell_data != "-" else [])
        candidates.append(InsightCandidate(
            candidate_id=f"flaky_cell:{g.key}",
            kind="flaky_cell",
            label=f"{KIND_LABEL['flaky_cell']} · {' · '.join(label_parts)}",
            figures={"transitions": g.transitions, "runs": g.count},
            api_ids=_capped([cell_api]),
            ref_ids=_capped(g.run_ids[:5]),
        ))

    # -- top_failed_rule -------------------------------------------------------------
    for g in sorted(inputs.groups_by_rule, key=lambda g: (-g.fail, g.key))[:3]:
        # figures reports the TRUE distinct-API count -- RunGroup.api_count, counted by the host
        # over the whole window (COUNT(DISTINCT api_id) on the rollup), never len(run_ids), which
        # is capped at aggregation.MAX_RUN_IDS. api_ids is a bounded (<=20) sample of it.
        sample = {i for i in (g.api_sample or ()) if _in_scope(i, scope)}
        candidates.append(InsightCandidate(
            candidate_id=f"top_failed_rule:{g.key}",
            kind="top_failed_rule",
            label=f"{KIND_LABEL['top_failed_rule']} · {g.key}",
            figures={"fail": g.fail, "error": g.error, "apis": g.api_count if g.api_count is not None else len(sample)},
            api_ids=_capped_set(sample),
            ref_ids=_capped(g.run_ids[:5]),
        ))

    # -- env_divergence ------------------------------------------------------------------
    # Per api, the LATEST cell per target_env (current_state IS the latest run per cell), then
    # compare those latest statuses across envs. No run list is read.
    cells_by_api: dict[str, list[CellState]] = defaultdict(list)
    for cell in inputs.current_state:
        if _in_scope(cell.api_id, scope):
            cells_by_api[cell.api_id].append(cell)
    for api_id in sorted(cells_by_api):
        latest_per_env: dict[str, CellState] = {}
        for cell in cells_by_api[api_id]:
            current = latest_per_env.get(cell.target_env)
            if current is None or (cell.executed_at, cell.run_id) > (current.executed_at, current.run_id):
                latest_per_env[cell.target_env] = cell
        if len(latest_per_env) < 2 or len({c.status for c in latest_per_env.values()}) < 2:
            continue
        envs_str = ",".join(f"{env}={latest_per_env[env].status.value}" for env in sorted(latest_per_env))
        candidates.append(InsightCandidate(
            candidate_id=f"env_divergence:{api_id}",
            kind="env_divergence",
            label=f"{KIND_LABEL['env_divergence']} · {api_id}",
            figures={"envs": envs_str},
            api_ids=_capped([api_id]),
            ref_ids=_capped_set(c.run_id for c in latest_per_env.values()),
        ))

    # -- stale_pending ---------------------------------------------------------------
    for job in inputs.jobs:
        if job.status is not JobStatus.STAGED:
            continue
        if scope is not None and not any(a in scope for a in job.api_ids):
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


def candidate_insights_from_runs(
    runs: Sequence[RunResult],
    apis: Mapping[str, ApiSpec],
    jobs: Sequence[JobSpec],
    scope: set[str] | None,
    role: OperatorRole,
    config: AtworksAgentConfig,
    now: datetime,
) -> list[InsightCandidate]:
    """옛 시그니처 그대로의 얇은 래퍼: run 목록에서 ``InsightInputs``를 만들어 위 함수에 넘긴다.
    호스트 경로는 이제 백엔드 질의로 같은 입력을 만들고(표본 없음), 이 래퍼는 테스트와 run 목록만
    가진 호출자를 위해 남는다 — 판정 로직은 한 벌뿐이다."""
    if scope is not None:
        runs = [r for r in runs if r.api_id in scope]
        apis = {api_id: api for api_id, api in apis.items() if api_id in scope}
        jobs = [j for j in jobs if any(a in scope for a in j.api_ids)]
    flaky_min = config.flaky_min_transitions
    by_rule = aggregate(runs, apis, "failed_rule", flaky_min_transitions=flaky_min)
    for group in by_rule:
        # aggregate() buckets a run under g.key when g.key is one of _keys(run, "failed_rule");
        # recount over the SOURCE runs with that same predicate so api_count is the true distinct
        # count (g.run_ids is capped at MAX_RUN_IDS and would undercount).
        sample = {r.api_id for r in runs if group.key in _keys(r, "failed_rule")}
        group.api_count = len(sample)
        group.api_sample = sorted(sample)[:_MAX_IDS]
    cells: list[CellState] = []
    latest: dict[tuple[str, str, str | None], RunResult] = {}
    transitions: dict[tuple[str, str, str | None], int] = {}
    previous: dict[tuple[str, str, str | None], bool] = {}
    for run in sorted(runs, key=lambda r: (r.executed_at, r.run_id)):
        key = (run.api_id, run.target_env, run.test_data_label)
        is_pass = run.status is RunStatus.PASS
        if key in previous and previous[key] != is_pass:
            transitions[key] = transitions.get(key, 0) + 1
        previous[key] = is_pass
        latest[key] = run
    for key, run in latest.items():
        cells.append(CellState(
            api_id=key[0], target_env=key[1], test_data_label=key[2], run_id=run.run_id,
            status=run.status, executed_at=run.executed_at, transitions_total=transitions.get(key, 0),
        ))
    inputs = InsightInputs(
        groups_by_api=aggregate(runs, apis, "api", flaky_min_transitions=flaky_min),
        cells=aggregate(runs, apis, "api_env_data", flaky_min_transitions=flaky_min),
        groups_by_rule=by_rule,
        current_state=cells,
        jobs=jobs,
        scope=scope,
    )
    return candidate_insights(inputs, role, config, now)
