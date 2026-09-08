"""툴 결과 페이로드. 모델이 읽는 모양을 한 곳에서 고정한다."""
from __future__ import annotations

from typing import Any

from .rules import FormatDefinition, ValidationRule
from .types import (
    ApiSpec,
    AskEntry,
    AuditEntry,
    ComparisonProfile,
    FailedRank,
    FormatBatch,
    JobSpec,
    RuleRecommendation,
    RunResult,
    SavedQuestion,
    VocabularyEntry,
)
from .vocabulary import fragment_summary_ko


def api_record(api: ApiSpec) -> dict[str, Any]:
    return api.model_dump(mode="json", exclude_none=True)


def run_record(run: RunResult) -> dict[str, Any]:
    # response_body must never reach a model-facing payload (a safety line, not just a size
    # cut) -- has_body tells the model whether one exists without ever carrying its content.
    # A run read back from the store carries no body at all (no read path joins `bodies`), so the
    # stored `has_body` flag answers for it; a freshly EXECUTED run still holds its body in
    # memory and answers from that. The `exclude` stays regardless: it is the last line, not the
    # only one.
    record = run.model_dump(mode="json", exclude_none=True, exclude={"response_body"})
    record["has_body"] = run.response_body is not None or bool(run.has_body)
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


def ask_record(entry: AskEntry) -> dict[str, Any]:
    """ask_log 1행. ``question``은 저장 시점에 이미 마스킹된 요약이라 여기서 더 손대지 않는다 --
    한 번 더 마스킹하면 규칙이 바뀐 뒤 같은 행이 날마다 다르게 읽힌다.

    ``session_id``만 **빠진다.** ``/ask-log``는 세션 범위가 아니라 팀 전체를 보여 주는 읽기라
    (Growth 뷰), 그 응답에 세션 id가 실리면 한 사람의 브라우저 탭에서 다른 사람의 살아 있는
    세션 식별자를 읽을 수 있다 -- 그 자체로 요청에 붙일 수 있는 값이다(``X-Session-Id``). 저장
    자체는 그대로다(``AskEntry.session_id``는 원장에 남아 조사에 쓰인다); 나가지 않을 뿐이다.
    그 밖에는 파생 필드도 별칭도 없다."""
    return entry.model_dump(mode="json", exclude={"session_id"})


def vocabulary_record(entry: VocabularyEntry) -> dict[str, Any]:
    """어휘 1행. 사이드카의 필드 그대로 + 사람이 읽는 요약 한 줄. 요약은 카탈로그 라벨로 만들고
    (``fragment_summary_ko``), 그래서 포털이 필터 이름을 저 나름으로 번역하지 않는다 -- 카드 푸터와
    Growth 뷰와 컨텍스트 블록이 같은 문장을 읽는다."""
    record = entry.model_dump(mode="json", exclude_none=True)
    record["fragment_summary"] = fragment_summary_ko(entry.fragment)
    return record


def saved_question_record(question: SavedQuestion) -> dict[str, Any]:
    """저장 질문 1행 (자가발전 §8). 파생 필드도 별칭도 없다 -- ``title``은 이미 카탈로그 라벨로
    만들어진 결정론 문장이고, ``source_users``/``source_asks``는 이 질문을 화면에 올린 증거
    그대로다. 카드는 여기 없는 숫자를 지어내지 않는다."""
    return question.model_dump(mode="json", exclude_none=True)


def format_batch_record(batch: FormatBatch) -> dict[str, Any]:
    record = batch.model_dump(mode="json", exclude_none=True)
    record["change_id"] = batch.batch_id   # web-shared의 change_update 훅과 호환 (Task 15)
    # FormatBatch의 new_count/duplicate_count/invalid_count는 plain @property라 model_dump에
    # 실리지 않는다 — 여기서 얹어야 카드가 매번 entries를 다시 세지 않는다.
    record["new_count"] = batch.new_count
    record["duplicate_count"] = batch.duplicate_count
    record["invalid_count"] = batch.invalid_count
    return record
