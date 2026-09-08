"""어휘의 순수 함수들 (self-growth spec §7): 정규화, 조각 검증, 요약, 매칭, MemoryFact 왕복.

여기 있는 것은 전부 저장도 판정도 하지 않는다 — 저장 규율(쓰기 필터·쿨다운·확정)은 host 쪽
테스트(host/tests/test_vocabulary.py)가 본다.
"""
from datetime import UTC, datetime

import pytest

from atworks_agent.types import QueryFilters, VocabularyEntry
from atworks_agent.vocabulary import (
    MAX_TERM_CHARS,
    entry_to_fact,
    existing_alias_note_ko,
    fact_to_entry,
    fragment_from_tool,
    fragment_json,
    fragment_summary_ko,
    match_terms,
    normalize_term,
    rejected_fragment_fields,
)

T0 = datetime(2026, 9, 8, 9, tzinfo=UTC)


def _entry(term: str, **filters) -> VocabularyEntry:
    return VocabularyEntry(term=normalize_term(term), fragment=QueryFilters(**filters),
                           status="confirmed", proposed_by="minseong", proposed_at=T0)


# -- normalize_term ------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "expected"), [
    ("  결제   계열 ", "결제 계열"),
    ("Payment APIs", "payment apis"),
    ("ＰＡＹ", "pay"),                      # NFKC: 전각 → 반각
    ("payment_apis", "payment apis"),       # 밑줄은 공백이다 (MemoryFact key와의 왕복)
    ("결제\t계열\n", "결제 계열"),
    ("", ""),
])
def test_normalize_term(raw, expected):
    assert normalize_term(raw) == expected


def test_normalize_term_is_idempotent_and_capped():
    long = "가" * 80
    once = normalize_term(long)
    assert len(once) == MAX_TERM_CHARS and normalize_term(once) == once


# -- fragment_from_tool --------------------------------------------------------------------

def test_fragment_from_tool_keeps_a_shape():
    fragment = fragment_from_tool({"path_prefix": "/v1/payment", "method": ["GET"]})
    assert fragment.path_prefix == "/v1/payment" and fragment.method == ["GET"]
    # 조각이지 스펙이 아니다: 적지 않은 필드는 None으로 남고 직렬화에도 안 실린다.
    assert fragment_json(fragment) == '{"method": ["GET"], "path_prefix": "/v1/payment"}'


def test_fragment_from_tool_refuses_an_empty_fragment():
    with pytest.raises(ValueError, match="비어 있을 수 없습니다"):
        fragment_from_tool({})
    with pytest.raises(ValueError, match="비어 있을 수 없습니다"):
        fragment_from_tool({"path_prefix": None})


@pytest.mark.parametrize("rejected", [
    {"window_days": 30},
    {"since": "2026-09-01T00:00:00Z"},
    {"until": "2026-09-08T00:00:00Z"},
    {"api_ids": ["api-001"]},
    {"executed_by": ["jihoon"]},
    {"scope_operator": "minseong"},
])
def test_fragment_from_tool_refuses_windows_and_id_lists(rejected):
    # 별칭은 모양에 이름을 붙이는 것이다: "지난주"를 since/until로 굳히면 다음 주에 거짓이 되고,
    # 대상(api_ids·executed_by·scope_operator)은 남의 질문을 남의 id로 좁힌다 -- 확정된 별칭은
    # 팀 전체의 컨텍스트에 실리므로 거기 오퍼레이터 id가 굳으면 안 된다. 섞여 있어도 거절된다.
    with pytest.raises(ValueError, match="모양에만"):
        fragment_from_tool({"path_prefix": "/v1/payment", **rejected})


def test_fragment_from_tool_refuses_an_unknown_field():
    with pytest.raises(Exception):  # noqa: B017 -- pydantic extra="forbid"
        fragment_from_tool({"endpoint": "/v1/payment"})


def test_fragment_from_tool_refuses_a_fragment_too_long_to_store():
    # MemoryFact.value는 200자에서 잘리고, 잘린 JSON은 다시 읽히지 않는다 -- 자르기 전에 거절한다.
    with pytest.raises(ValueError, match="너무 큽니다"):
        fragment_from_tool({"failed_rule": [f"rule-{i:03d}-with-a-long-name" for i in range(10)]})


# -- fragment_summary_ko -------------------------------------------------------------------

def test_fragment_summary_uses_catalogue_labels():
    assert fragment_summary_ko(QueryFilters(path_prefix="/v1/payment")) == "경로 접두사 /v1/payment"
    summary = fragment_summary_ko(QueryFilters(method=["GET", "POST"], api_group=["payment"]))
    assert summary == "메서드 GET, POST · API 그룹 payment"


# -- match_terms ---------------------------------------------------------------------------

def test_match_terms_matches_normalized_substrings_longest_first():
    entries = [_entry("결제", path_prefix="/v1"), _entry("결제 계열", path_prefix="/v1/payment")]
    matched = match_terms("결제 계열 실패 보여줘", entries)
    assert [e.term for e in matched] == ["결제 계열", "결제"]


def test_match_terms_ignores_terms_the_message_does_not_carry():
    entries = [_entry("계약 계열", path_prefix="/v1/contract")]
    assert match_terms("결제 실패 보여줘", entries) == []
    assert match_terms("", entries) == []


def test_match_terms_is_case_and_spacing_insensitive_and_capped():
    entries = [_entry(f"term{i}", path_prefix=f"/v{i}") for i in range(12)]
    message = " ".join(f"TERM{i}" for i in range(12))
    assert len(match_terms(message, entries)) == 8
    assert len(match_terms(message, entries, cap=3)) == 3


# -- MemoryFact 왕복 -----------------------------------------------------------------------

def test_entry_to_fact_and_back():
    entry = _entry("결제 계열", path_prefix="/v1/payment")
    fact = entry_to_fact(entry, source_session_id="sess")
    assert fact.key == "결제_계열" and fact.category.value == "context"
    assert fact.value == fragment_json(entry.fragment) and fact.source_session_id == "sess"
    back = fact_to_entry(fact, proposed_by="minseong")
    assert back.term == entry.term and back.fragment == entry.fragment


# -- 저장하는 쪽의 같은 문 / 재제안 문장 ------------------------------------------------------

@pytest.mark.parametrize("filters", [
    {"window_days": 30},
    {"api_ids": ["api-001"]},
    {"executed_by": ["jihoon"]},
    {"scope_operator": "minseong"},
])
def test_rejected_fragment_fields_names_every_offender(filters):
    # 백엔드가 저장 직전에 쓰는 같은 규칙(툴 입력이 아니라 이미 만들어진 QueryFilters 위에서).
    fragment = QueryFilters(path_prefix="/v1/payment", **filters)
    assert rejected_fragment_fields(fragment) == list(filters)
    assert rejected_fragment_fields(QueryFilters(path_prefix="/v1/payment")) == []


def test_existing_alias_note_names_the_status_and_what_to_do():
    confirmed = _entry("결제 계열", path_prefix="/v1/payment")
    assert "confirmed" in existing_alias_note_ko(confirmed)
    assert "그대로 쓰세요" in existing_alias_note_ko(confirmed)
    pending = confirmed.model_copy(update={"status": "pending"})
    assert "확인 대기" in existing_alias_note_ko(pending)
    rejected = confirmed.model_copy(update={
        "status": "rejected", "cooldown_until": datetime(2026, 10, 8, tzinfo=UTC)})
    assert "2026-10-08" in existing_alias_note_ko(rejected) and "냉각" in existing_alias_note_ko(rejected)
    # 세 문장 모두 "다시 제안하지 마세요"로 끝난다 -- 재제안은 아무것도 쓰지 않는다.
    for entry in (confirmed, pending, rejected):
        assert existing_alias_note_ko(entry).endswith("다시 제안하지 마세요.")
