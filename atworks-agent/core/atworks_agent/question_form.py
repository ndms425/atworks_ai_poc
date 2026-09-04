"""AI→사용자 구조화 질문. open-design `<question-form>` 계약 이식:
- 문항 ≤ 5, 전부 default 프리필(그대로 제출해도 되는 폼), `default`는 `options`보다 앞,
- 폼 다음엔 턴 종료(프롬프트 규칙), 답은 `[form answers — <id>]` 사용자 메시지로 돌아온다.
차이: 텍스트 블록이 아니라 presentation 툴 payload이며, 문항마다 `why`(근거)가 필수다."""
from __future__ import annotations

from typing import Any, Literal

from commerce_common.presentation import PresentationPayload
from pydantic import BaseModel, Field, field_validator

FORM_ANSWERS_PREFIX = "[form answers — "
MAX_QUESTIONS = 5

QuestionType = Literal["radio", "checkbox", "select", "text", "date", "time", "number", "switch"]


class FormOption(BaseModel):
    label: str = Field(max_length=80)
    value: str = Field(max_length=64)


class FormQuestion(BaseModel):
    id: str = Field(max_length=40, pattern=r"^[a-z0-9_.-]+$")
    label: str = Field(max_length=120)
    type: QuestionType
    why: str = Field(min_length=1, max_length=200)          # 판단 근거 — 승인 화면에 노출
    default: str | list[str] | None = None
    options: list[FormOption] | None = None
    placeholder: str | None = Field(default=None, max_length=80)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("options", mode="before")
    @classmethod
    def _coerce_options(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [{"label": o, "value": o} if isinstance(o, str) else o for o in v]
        return v


class QuestionFormPayload(PresentationPayload):
    id: str = Field(max_length=40, pattern=r"^[a-z0-9_.-]+$")
    title: str = Field(max_length=80)
    description: str | None = Field(default=None, max_length=200)
    questions: list[FormQuestion] = Field(min_length=1, max_length=MAX_QUESTIONS)


QUESTION_FORM_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "maxLength": 40, "pattern": "^[a-z0-9_.-]+$"},
        "title": {"type": "string", "maxLength": 80},
        "description": {"type": "string", "maxLength": 200},
        "questions": {
            "type": "array", "minItems": 1, "maxItems": MAX_QUESTIONS,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "maxLength": 40, "pattern": "^[a-z0-9_.-]+$"},
                    "label": {"type": "string", "maxLength": 120},
                    "type": {"type": "string", "enum": ["radio", "checkbox", "select", "text", "date", "time", "number", "switch"]},
                    "why": {"type": "string", "maxLength": 200,
                            "description": "Why this question is being asked — the fact that was missing or ambiguous. Shown beside the control."},
                    "default": {"description": "Recommended answer, prefilled. Put this key BEFORE options.",
                                "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                    "options": {"type": "array", "maxItems": 8,
                                "items": {"anyOf": [{"type": "string"}, {"type": "object", "properties": {
                                    "label": {"type": "string"}, "value": {"type": "string"}},
                                    "required": ["label", "value"], "additionalProperties": False}]}},
                    "placeholder": {"type": "string", "maxLength": 80},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1,
                                   "description": "How sure you are of the default; below 0.5 the card highlights the question."},
                },
                "required": ["id", "label", "type", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["id", "title", "questions"],
    "additionalProperties": False,
}


def _display(q: FormQuestion, value: str) -> str:
    match = next((o for o in (q.options or []) if o.value == value or o.label == value), None)
    if match is None:
        return value
    return match.label if match.label == match.value else f"{match.label} [value: {match.value}]"


def format_form_answers(form_id: str, questions: list[FormQuestion], answers: dict[str, str | list[str]]) -> str:
    """open-design formatFormAnswers 미러. 이 문자열이 다음 user 메시지가 된다."""
    lines = [f"{FORM_ANSWERS_PREFIX}{form_id}]"]
    for q in questions:
        v = answers.get(q.id)
        if isinstance(v, list):
            display = ", ".join(_display(q, x) for x in v) if v else "(skipped)"
        elif isinstance(v, str) and v.strip():
            display = _display(q, v.strip())
        else:
            display = "(skipped)"
        lines.append(f"- {q.label}: {display}")
    return "\n".join(lines)
