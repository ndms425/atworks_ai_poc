"""조직 공용 어휘 (self-growth, spec 2026-09-07 §7). 이 모듈은 저장하지도, 판정하지도 않는다 --
용어를 정규화하고, 모델이 낸 조각이 **모양**(경로·메서드·그룹·계·규칙·상태코드)인지 검사하고,
확정된 항목을 이번 턴 메시지에 맞춰 고르는 순수 함수들만 있다. 저장은 host의
``SqliteMemoryStore``(commerce_common.memory 계약)와 ``vocabulary`` 사이드카가 한다.

한 줄 규칙: **별칭은 모양에 이름을 붙이는 것이지 기간이나 id 목록에 붙이는 게 아니다.** "결제 계열"이
``path_prefix=/v1/payment``이면 그건 다음 달에도 참이지만, "지난주"를 ``since/until``로 굳히면 다음
주에 거짓이 된다 -- 그래서 시간 창과 id 목록은 ``fragment_from_tool``이 거부한다."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime

from commerce_common.types import MemoryCategory, MemoryFact

from .catalog import FILTER_LABELS_KO
from .types import QueryFilters, VocabularyEntry

#: term의 길이 상한 -- ``VocabularyEntry.term``(max_length=40)과 툴 스키마가 같은 값을 쓴다.
MAX_TERM_CHARS = 40
#: fragment JSON의 길이 상한. ``MemoryFact.value``가 200자에서 잘리므로(자르면 JSON이 깨진다)
#: 자르기 전에 거부한다.
MAX_FRAGMENT_JSON_CHARS = 200
#: 한 턴에 주입할 어휘 수의 기본 상한(config ``vocabulary_max_inject``가 실제 값).
DEFAULT_INJECT_CAP = 8

#: 별칭이 이름 붙일 수 없는 필터. 시간 창(``since``/``until``/``window_days``)은 오늘만 참인 값이고,
#: id 목록(``api_ids``)과 ``scope_operator``는 모양이 아니라 대상이다 -- 조직 전체 컨텍스트에 실리면
#: 다른 오퍼레이터의 질문을 남의 id로 좁힌다.
REJECTED_FRAGMENT_FIELDS: tuple[str, ...] = (
    "since", "until", "window_days", "api_ids", "scope_operator",
)


def normalize_text(text: str) -> str:
    """용어와 메시지가 공유하는 접기: NFKC → 소문자 → 밑줄을 공백으로 → 공백 묶음 1칸 → strip.
    길이 상한은 **여기 없다** -- :func:`normalize_term`만 자른다. 매칭의 건초더미(메시지)를
    40자로 자르면 긴 질문의 뒷부분에 있는 용어가 조용히 안 맞는다."""
    folded = unicodedata.normalize("NFKC", text or "").lower().replace("_", " ")
    return " ".join(folded.split())


def normalize_term(term: str) -> str:
    """용어의 정규형: :func:`normalize_text` + ≤40자.

    밑줄을 공백으로 접는 이유는 ``MemoryFact``와의 왕복 때문이다: ``validate_fact``가 fact key의
    공백을 밑줄로 바꾸므로, 정규형에 밑줄이 남아 있으면 term → key → term이 두 값으로 갈린다.
    정규화 뒤 밑줄이 없으면 ``key.replace("_", " ")``가 정확한 역함수다(``fact_to_entry``)."""
    return normalize_text(term)[:MAX_TERM_CHARS]


def fragment_from_tool(tool_fragment: object) -> QueryFilters:
    """모델이 ``propose_alias(fragment=...)``로 낸 딕트를 ``QueryFilters`` 조각으로 검증한다.
    ``ValueError``로 거절하는 세 경우: 빈 조각, 시간 창·id 목록(:data:`REJECTED_FRAGMENT_FIELDS`),
    직렬화가 200자를 넘는 조각. pydantic(``extra="forbid"``)이 나머지를 본다."""
    if not isinstance(tool_fragment, dict):
        raise ValueError("fragment must be an object of QueryFilters fields")
    given = {name: value for name, value in tool_fragment.items() if value is not None}
    if not given:
        raise ValueError(
            "fragment는 비어 있을 수 없습니다 -- 이 용어가 뜻하는 필터를 하나 이상 적으세요 "
            "(path_prefix, path_contains, method, api_group, target_env, test_data_label, "
            "failed_rule, http_status, status)."
        )
    offending = [name for name in REJECTED_FRAGMENT_FIELDS if name in given]
    if offending:
        raise ValueError(
            f"별칭은 모양에만 붙입니다 -- {', '.join(offending)}는 기간이거나 id 목록이라 "
            "다음 달에는 다른 것을 뜻합니다. 경로·메서드·그룹·계·규칙·상태코드로 적으세요."
        )
    fragment = QueryFilters.model_validate(given)
    if len(fragment_json(fragment)) > MAX_FRAGMENT_JSON_CHARS:
        raise ValueError(
            f"fragment가 너무 큽니다(JSON {MAX_FRAGMENT_JSON_CHARS}자 이하) -- 값 목록을 줄이세요."
        )
    return fragment


def fragment_json(fragment: QueryFilters) -> str:
    """저장되는 fragment의 정규 직렬화. ``MemoryFact.value``와 ``vocabulary.fragment_json``이
    같은 문자열을 쓴다 -- 키 순서까지 같아야 두 자리의 값이 글자 단위로 비교된다."""
    return json.dumps(
        fragment.model_dump(mode="json", exclude_none=True), ensure_ascii=False, sort_keys=True
    )


def fragment_summary_ko(fragment: QueryFilters) -> str:
    """사람이 읽는 한 줄("경로 접두사 /v1/payment"). 카드 푸터와 컨텍스트 블록의 ``means``가
    이 문자열을 쓴다 -- 라벨은 카탈로그(:data:`FILTER_LABELS_KO`)의 것이고, 모델 문장이 아니다."""
    parts: list[str] = []
    for name, value in fragment.model_dump(mode="json", exclude_none=True).items():
        label = FILTER_LABELS_KO.get(name, name)
        rendered = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        parts.append(f"{label} {rendered}")
    return " · ".join(parts) if parts else "조건 없음"


def match_terms(
    message: str, entries: Sequence[VocabularyEntry], cap: int = DEFAULT_INJECT_CAP
) -> list[VocabularyEntry]:
    """이번 턴 메시지에 실제로 나온 항목만, 긴 용어부터 ``cap``개. 정규형끼리의 부분일치다 --
    "결제 계열"과 "결제"가 둘 다 확정돼 있으면 긴 쪽이 먼저 온다(더 구체적인 뜻이 이긴다).

    호출자는 **확정된**(confirmed) 항목만 넘긴다: pending은 제안한 세션의 카드에서만 쓰인다
    (spec §2 조항 4). 이 함수는 상태를 보지 않는다 -- 넘어온 것을 그대로 맞춘다."""
    haystack = normalize_text(message)
    if not haystack:
        return []
    seen: set[str] = set()
    matched: list[VocabularyEntry] = []
    for entry in sorted(entries, key=lambda e: (-len(e.term), e.term)):
        term = normalize_term(entry.term)
        if term and term in haystack and term not in seen:
            seen.add(term)
            matched.append(entry)
    return matched[:cap]


def entry_to_fact(entry: VocabularyEntry, *, source_session_id: str | None = None) -> MemoryFact:
    """어휘 항목 → ``MemoryFact``. key는 정규형 term(공백은 ``validate_fact``가 밑줄로 바꾼다),
    value는 fragment JSON, category는 ``context``다. 쓰기 경로는 이 fact를 그대로 저장하지 않고
    ``validate_fact``에 다시 넣는다 -- 쓰기 필터는 저장 직전에 한 번만 서는 문이다."""
    return MemoryFact(
        key=normalize_term(entry.term).replace(" ", "_"),
        value=fragment_json(entry.fragment),
        category=MemoryCategory.CONTEXT,
        updated_at=entry.confirmed_at or entry.proposed_at,
        source_session_id=source_session_id,
    )


def fact_to_entry(fact: MemoryFact, *, proposed_by: str = "", status: str = "pending") -> VocabularyEntry:
    """``MemoryFact`` → 어휘 항목(:func:`entry_to_fact`의 역함수). 사이드카가 없는 저장소에서
    facts만으로 항목을 복원할 때 쓴다 -- 상태·집계 필드는 fact에 없으므로 호출자가 준다."""
    return VocabularyEntry(
        term=normalize_term(fact.key.replace("_", " ")),
        fragment=QueryFilters.model_validate(json.loads(fact.value)),
        status=status,  # type: ignore[arg-type]
        proposed_by=proposed_by,
        proposed_at=fact.updated_at or datetime.now(UTC),
    )
