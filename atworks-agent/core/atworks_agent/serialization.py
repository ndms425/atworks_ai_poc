"""툴 결과 페이로드. 모델이 읽는 모양을 한 곳에서 고정한다."""
from __future__ import annotations

from typing import Any

from .rules import FormatDefinition, ValidationRule
from .types import (
    ApiSpec,
    AuditEntry,
    ComparisonProfile,
    FailedRank,
    FormatBatch,
    JobSpec,
    RuleRecommendation,
    RunResult,
)


def api_record(api: ApiSpec) -> dict[str, Any]:
    return api.model_dump(mode="json", exclude_none=True)


def run_record(run: RunResult) -> dict[str, Any]:
    # response_body must never reach a model-facing payload (a safety line, not just a size
    # cut) -- has_body tells the model whether one exists without ever carrying its content.
    record = run.model_dump(mode="json", exclude_none=True, exclude={"response_body"})
    record["has_body"] = run.response_body is not None
    return record


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


def rule_record(rule: ValidationRule) -> dict[str, Any]:
    record = rule.model_dump(mode="json", exclude_none=True)
    record["change_id"] = rule.rule_id   # web-shared의 change_update 훅과 호환 (Task 15)
    return record


def profile_record(profile: ComparisonProfile) -> dict[str, Any]:
    record = profile.model_dump(mode="json", exclude_none=True)
    record["change_id"] = profile.profile_id   # web-shared의 change_update 훅과 호환 (Task 15)
    return record


def format_record(defn: FormatDefinition) -> dict[str, Any]:
    return defn.model_dump(mode="json", exclude_none=True)


def rule_recommendation_record(rec: RuleRecommendation) -> dict[str, Any]:
    record = rec.model_dump(mode="json", exclude_none=True)
    record["rule"] = rule_record(rec.rule)
    return record


def audit_record(entry: AuditEntry) -> dict[str, Any]:
    """감사 로그 1행. append-only 원장의 읽기 모양 — 파생 필드도 별칭도 없다."""
    return entry.model_dump(mode="json")


def format_batch_record(batch: FormatBatch) -> dict[str, Any]:
    record = batch.model_dump(mode="json", exclude_none=True)
    record["change_id"] = batch.batch_id   # web-shared의 change_update 훅과 호환 (Task 15)
    # FormatBatch의 new_count/duplicate_count/invalid_count는 plain @property라 model_dump에
    # 실리지 않는다 — 여기서 얹어야 카드가 매번 entries를 다시 세지 않는다.
    record["new_count"] = batch.new_count
    record["duplicate_count"] = batch.duplicate_count
    record["invalid_count"] = batch.invalid_count
    return record
