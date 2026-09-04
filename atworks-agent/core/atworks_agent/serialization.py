"""툴 결과 페이로드. 모델이 읽는 모양을 한 곳에서 고정한다."""
from __future__ import annotations

from typing import Any

from .types import ApiSpec, FailedRank, JobSpec, RunResult


def api_record(api: ApiSpec) -> dict[str, Any]:
    return api.model_dump(mode="json", exclude_none=True)


def run_record(run: RunResult) -> dict[str, Any]:
    return run.model_dump(mode="json", exclude_none=True)


def rank_record(rank: FailedRank) -> dict[str, Any]:
    return rank.model_dump(mode="json", exclude_none=True)


def job_record(job: JobSpec) -> dict[str, Any]:
    record = job.model_dump(mode="json", exclude_none=True)
    record["change_id"] = job.job_id   # web-shared의 change_update 훅과 호환 (Task 15)
    # JobSpec의 matrix_size/total_executions/remaining_executions/runs_total은 plain @property라
    # model_dump에 실리지 않는다 — 여기서 얹어야 카드/뷰가 매번 재계산하지 않는다.
    record["matrix_size"] = job.matrix_size
    record["total_executions"] = job.total_executions
    record["remaining_executions"] = job.remaining_executions
    record["runs_total"] = job.runs_total
    return record
