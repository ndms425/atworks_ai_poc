import pytest

from atworks_agent.question_form import FormQuestion, QuestionFormPayload, format_form_answers


def _q(qid="target_env", **over):
    base = dict(id=qid, label="어느 계에 실행할까요?", type="radio", why="발화에 대상 계가 없었습니다",
                default="dev", options=[{"label": "개발계", "value": "dev"}, {"label": "이관계", "value": "stg"}])
    base.update(over)
    return FormQuestion(**base)


def test_form_caps_at_five_questions():
    with pytest.raises(ValueError):
        QuestionFormPayload(id="job-slots", title="확인", questions=[_q(f"q{i}") for i in range(6)])


def test_form_requires_why_on_every_question():
    with pytest.raises(ValueError):
        FormQuestion(id="x", label="l", type="text", why="")


def test_format_form_answers_matches_open_design_shape():
    form = QuestionFormPayload(id="job-slots", title="확인", questions=[_q()])
    text = format_form_answers(form.id, form.questions, {"target_env": "stg"})
    assert text.splitlines()[0] == "[form answers — job-slots]"
    assert "- 어느 계에 실행할까요?: 이관계 [value: stg]" in text


def test_skipped_answer_rendered():
    form = QuestionFormPayload(id="f", title="t", questions=[_q()])
    assert "(skipped)" in format_form_answers(form.id, form.questions, {})
