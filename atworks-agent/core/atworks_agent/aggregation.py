"""실행 기록 집계 — aggregate_runs 툴, /runs/insights, 일일 브리핑이 같은 함수를 쓴다. 순수 함수,
이름 붙은 결정론 규칙(flaky_v1). 숫자는 전부 여기서 나오고 모델은 어떤 그룹을 보여줄지만 고른다."""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import get_args

from .config import AtworksAgentConfig
from .types import ApiSpec, GroupBy, Insights, RunGroup, RunResult, RunStatus

# GroupBy (types.py) is the source of truth; this tuple is derived so the five values are
# never hand-duplicated between the Literal and this module.
GROUP_BY: tuple[str, ...] = get_args(GroupBy)
MAX_RUN_IDS = 50


def _keys(run: RunResult, group_by: str) -> list[str]:
    if group_by == "api":
        return [run.api_id]
    if group_by == "env":
        return [run.target_env]
    if group_by == "http_status":
        return [str(run.http_status) if run.http_status is not None else "(none)"]
    if group_by == "api_env_data":
        return [f"{run.api_id}|{run.target_env}|{run.test_data_label or '-'}"]
    if group_by == "failed_rule":
        if run.status is RunStatus.PASS:
            return []
        if run.failed_rules:
            return list(dict.fromkeys(run.failed_rules))
        return [f"(error) HTTP {run.http_status if run.http_status is not None else '(none)'}"]
    raise ValueError(f"group_by must be one of {', '.join(GROUP_BY)}; got {group_by!r}")


def _label(key: str, group_by: str, apis: Mapping[str, ApiSpec]) -> str:
    if group_by == "api":
        api = apis.get(key)
        return f"{api.method} {api.path}" if api is not None else key
    return key


def _p95(values: Sequence[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _build(key: str, runs: list[RunResult], group_by: str, apis: Mapping[str, ApiSpec], flaky_min: int) -> RunGroup:
    chronological = sorted(runs, key=lambda r: (r.executed_at, r.run_id))
    fail = sum(1 for r in runs if r.status is RunStatus.FAIL)
    error = sum(1 for r in runs if r.status is RunStatus.ERROR)
    first_non_pass = next((r for r in chronological if r.status is not RunStatus.PASS), None)
    last_pass_before = None
    if first_non_pass is not None:
        earlier_passes = [r for r in chronological if r.status is RunStatus.PASS and r.executed_at < first_non_pass.executed_at]
        last_pass_before = earlier_passes[-1].executed_at if earlier_passes else None
    transitions = 0
    previous: bool | None = None
    for r in chronological:
        is_pass = r.status is RunStatus.PASS
        if previous is not None and is_pass != previous:
            transitions += 1
        previous = is_pass
    api = apis.get(key) if group_by == "api" else None
    regression = (
        api is not None and first_non_pass is not None and last_pass_before is not None
        and last_pass_before < api.updated_at <= first_non_pass.executed_at
    )
    return RunGroup(
        key=key, label=_label(key, group_by, apis), count=len(runs), fail=fail, error=error,
        passed=len(runs) - fail - error,
        run_ids=[r.run_id for r in reversed(chronological)][:MAX_RUN_IDS],
        first_non_pass_at=first_non_pass.executed_at if first_non_pass else None,
        last_pass_before=last_pass_before,
        latest_status=chronological[-1].status if chronological else None,
        transitions=transitions, flaky=transitions >= flaky_min,
        p95_duration_ms=_p95([r.duration_ms for r in runs if r.duration_ms is not None]),
        regression_suspect=regression, api_updated_at=api.updated_at if api is not None else None,
    )


def aggregate(
    runs: Sequence[RunResult], apis: Mapping[str, ApiSpec], group_by: str, *, flaky_min_transitions: int
) -> list[RunGroup]:
    if group_by not in GROUP_BY:
        raise ValueError(f"group_by must be one of {', '.join(GROUP_BY)}; got {group_by!r}")
    buckets: dict[str, list[RunResult]] = defaultdict(list)
    for run in runs:
        for key in _keys(run, group_by):
            buckets[key].append(run)
    groups = [_build(key, rows, group_by, apis, flaky_min_transitions) for key, rows in buckets.items()]
    groups.sort(key=lambda g: (-(g.fail + g.error), -g.count, g.key))
    return groups


def summarize_insights(runs: Sequence[RunResult], apis: Mapping[str, ApiSpec], config: AtworksAgentConfig) -> Insights:
    cells = aggregate(runs, apis, "api_env_data", flaky_min_transitions=config.flaky_min_transitions)
    by_api = aggregate(runs, apis, "api", flaky_min_transitions=config.flaky_min_transitions)
    return Insights(
        flaky=sum(1 for g in cells if g.flaky),
        regression_suspect=sum(1 for g in by_api if g.regression_suspect),
    )
