# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""턴 분류 -- 자가발전 spec §6. 한 턴이 끝나면 호스트가 여기 있는 ``classify_turn``으로
``AskEntry`` 한 행을 만든다.

이 모듈에는 모델이 없다. outcome/intent/cluster_key는 **그 턴이 실제로 부른 도구 이름**과
**카드가 나왔는가**만 보고 결정론으로 정해진다 -- 모델이 "잘 답했다"고 스스로 채점하는 경로는
어디에도 없다(§6). 질문 텍스트는 저장 전에 ``mask_body``를 통과하고 300자에서 잘린다: ask_log는
원문 메시지 보관소가 아니라 **무엇을 물었나**의 요약이다.

순함수다 -- I/O도, 백엔드도, 시계도 없다(``now``는 인자로 받는다).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from . import catalog
from .masking import MaskingPolicy, mask_body
from .types import AskEntry, AskOutcome, QuerySpec, UnmetReason

#: 질문에 "답"할 수 있는 도구 -- 이들 중 하나가 돌고 카드가 하나라도 나와야 ``answered``다.
#: 프레젠테이션 도구도, load_skill도, 화면 지시도 여기 없다: 그것들은 데이터를 읽지 않는다.
DATA_TOOLS: frozenset[str] = frozenset({
    "query_runs",
    "aggregate_runs",
    "rank_failed_runs",
    "list_runs",
    "search_apis",
    "get_api",
    "get_run",
    "find_apis_with_param",
    "recommend_rules_for_api",
    "recommend_ignore_paths",
})

#: 원장을 건드리는 도구의 접두어. 이런 턴은 성장 대상이 아니라 **행동**이다(§6).
ACTION_PREFIXES: tuple[str, ...] = ("stage_", "apply_", "discard_")

#: 화면 지시 두 개 -- 이것만 부른 턴은 데이터 질문이 아니다.
SCREEN_TOOLS: frozenset[str] = frozenset({"navigate_screen", "highlight_screen"})

_STATUS_TOOLS: frozenset[str] = frozenset({"list_runs", "get_run", "rank_failed_runs"})
_LOOKUP_TOOLS: frozenset[str] = frozenset({
    "search_apis", "get_api", "find_apis_with_param", "recommend_rules_for_api",
    "recommend_ignore_paths",
})
_AGGREGATE_TOOLS: frozenset[str] = frozenset({"query_runs", "aggregate_runs"})

#: 추세 차원 -- 스펙이 이 중 하나로 묶으면 aggregate가 아니라 trend다.
_TREND_DIMENSIONS: frozenset[str] = frozenset({"day", "week"})

#: cluster_key 토큰화가 버리는 말들. 군집 키는 "무엇을 물었나"를 담아야 하므로 어느 질문에나
#: 붙는 말은 빠진다. 짧게 유지한다 -- 불용어 목록이 길어질수록 서로 다른 질문이 한 군집으로
#: 뭉개진다.
_STOPWORDS: frozenset[str] = frozenset({
    # 한국어
    "그리고", "그럼", "그거", "이거", "저거", "요즘", "지금", "정도", "관련", "대해", "대한",
    "해줘", "해주세요", "알려줘", "알려주세요", "보여줘", "보여주세요", "주세요", "부탁",
    "어떤", "무슨", "얼마나", "뭐야", "인가요", "인가", "있나", "있어", "있는", "없어", "좀",
    # English
    "the", "and", "for", "you", "can", "with", "what", "which", "this", "that", "please",
    "show", "give", "tell", "are", "was", "were", "how", "why", "from", "into", "about",
    "does", "did", "has", "have", "all", "any", "not", "but", "get", "let", "our", "its",
})

_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z가-힣]+")

#: cluster_key에 실리는 토큰 수 상한(§6: 정렬 후 ≤6).
MAX_CLUSTER_TOKENS = 6

#: 저장되는 질문 요약의 길이 상한(§6, AskEntry.question의 max_length와 같은 수).
MAX_QUESTION_CHARS = 300


def mask_question(question: str, policy: MaskingPolicy) -> str:
    """사용자 메시지 → ask_log에 저장할 요약. ``mask_body``는 문자열 리프에도 그대로 도는
    순함수라 본문 마스킹과 **같은 규칙 같은 코드**를 쓴다(주민번호·카드·계좌·전화·이메일).
    자르는 것은 마스킹 뒤다 -- 먼저 자르면 잘린 자리에서 규칙이 못 맞아 원문 조각이 남는다."""
    masked = mask_body(str(question), policy)
    return str(masked)[:MAX_QUESTION_CHARS]


def _has_action(tool_names: list[str]) -> bool:
    return any(name.startswith(ACTION_PREFIXES) for name in tool_names)


def decide_outcome(
    *, tool_names: list[str], cards: int, unmet: tuple[UnmetReason, str, str] | None
) -> AskOutcome:
    """§6의 판정 규칙, 그 순서 그대로.

    ``action``이 맨 앞이다: 승인·스테이징 턴은 질문이 아니므로 성장 지표(answered/partial/unmet
    비율)에 섞이면 안 된다. 그 다음이 ``unmet``(모델이 스스로 "카탈로그로 못 답한다"고 신고한
    턴), 그 다음이 ``answered``(데이터 도구 ≥1 **그리고** 카드 ≥1 -- 둘 중 하나만으로는 답이
    아니다: 도구만 돌고 카드가 없으면 산문으로 숫자를 말했다는 뜻이고, 카드만 있고 데이터
    도구가 없으면 새로 읽은 것이 없다는 뜻이다). 나머지는 전부 ``partial``이다."""
    if _has_action(tool_names):
        return "action"
    if unmet is not None:
        return "unmet"
    if cards >= 1 and any(name in DATA_TOOLS for name in tool_names):
        return "answered"
    return "partial"


def decide_intent(
    *,
    tool_names: list[str],
    unmet: tuple[UnmetReason, str, str] | None,
    spec: QuerySpec | None,
) -> str:
    """§6의 intent 매핑 -- 첫 일치가 이긴다.

    ``aggregate``만 스펙을 더 본다: 차원에 ``day``/``week``가 있으면 ``trend``,
    ``compare_previous_window``면 ``compare``. 둘 다면 ``compare``가 이긴다 -- 창 대 창 비교는
    사용자가 명시적으로 요청한 모양이고, 추세는 그 안에 딸려 오는 성질이기 때문이다."""
    if _has_action(tool_names):
        return "action"
    if tool_names and all(name in SCREEN_TOOLS for name in tool_names):
        return "meta"
    if any(name in _AGGREGATE_TOOLS for name in tool_names):
        if spec is not None and spec.compare_previous_window:
            return "compare"
        if spec is not None and _TREND_DIMENSIONS.intersection(spec.dimensions):
            return "trend"
        return "aggregate"
    if any(name in _STATUS_TOOLS for name in tool_names):
        return "status"
    if any(name in _LOOKUP_TOOLS for name in tool_names):
        return "lookup"
    if unmet is not None:
        return f"unmet:{unmet[0]}"
    return "meta"


def normalized_tokens(question: str, limit: int = MAX_CLUSTER_TOKENS) -> list[str]:
    """마스킹된 질문 → 군집 토큰. 소문자화, 문장부호 제거, 1글자 이하와 불용어 제거, 중복 제거,
    **정렬**, 앞에서 ``limit``개. 정렬하기 때문에 어순이 다른 같은 질문이 같은 키를 갖는다."""
    seen: list[str] = []
    for raw in _TOKEN_SPLIT.split(question.lower()):
        token = raw.strip()
        if len(token) <= 1 or token in _STOPWORDS or token in seen:
            continue
        seen.append(token)
    return sorted(seen)[:limit]


def cluster_key_for(
    *,
    outcome: AskOutcome,
    intent: str,
    question: str,
    spec: QuerySpec | None,
    unmet: tuple[UnmetReason, str, str] | None,
) -> str:
    """"같은 질문"의 유일한 판정 기준(§6). **unmet이 먼저다**: 답하지 못한 턴은 사유 + 질문
    토큰으로 묶는다 — 그 턴이 스펙 하나를 돌려 보고 나서 "이건 못 한다"고 말했더라도(첫 시도가
    빗나가고 `note_unmet_ask`로 끝나는 흔한 모양) 그 스펙은 **답이 아니라 실패한 시도**라,
    같은 스펙을 실제로 답한 군집과 한 열쇠로 묶으면 승격기가 답한 질문 5건 안에 못 답한 질문을
    섞어 세고 미충족 군집은 사라진다. 그다음이 스펙: 값을 뺀 정규화 스펙 키
    (``catalog.cluster_key_for_spec`` -- 필터 **값**은 절대 들어가지 않는다). 그 밖은
    outcome:intent다. 어느 갈래에서도 사용자 값(운영자 id, API 경로 값, 마스킹된 개인정보)이
    키에 실리지 않는다."""
    # `outcome == "unmet"`이 아니면서 삼중항이 있는 경우는 `action` 턴 하나뿐이고(action이
    # unmet보다 앞선다), 그때 스펙이 있으면 그건 실제로 돌아간 질의다 -- 스펙 키를 쓴다.
    if unmet is not None and (outcome == "unmet" or spec is None):
        return f"unmet:{unmet[0]}|" + ",".join(normalized_tokens(question))
    if spec is not None:
        return catalog.cluster_key_for_spec(spec)
    return f"{outcome}:{intent}"


def classify_turn(
    *,
    question: str,
    tool_names: list[str],
    cards: int,
    unmet: tuple[UnmetReason, str, str] | None,
    spec: QuerySpec | None,
    policy: MaskingPolicy,
    session: Any,
    turn_id: str,
    now: datetime,
    vocabulary_terms: Sequence[str] = (),
) -> AskEntry:
    """한 턴 → ``ask_log`` 한 행. 호출자는 그 턴의 도구 이름 목록, 카드 수, ``note_unmet_ask``가
    남긴 삼중항, (있다면) 이 턴이 실행한 ``QuerySpec``만 넘긴다. 숫자도 판정도 전부 여기서
    결정론으로 나온다.

    ``vocabulary_terms``는 호스트가 이 턴의 컨텍스트에 실제로 실은 확정 용어들이다(§7 단계 3).
    행에 같이 남는 이유는 하나뿐이다: 👎(§9)는 턴이 끝나고 한참 뒤에 오고, 그때 "이 답에 어떤
    용어가 관여했나"를 답할 수 있는 곳이 이 행 말고 없다."""
    masked = mask_question(question, policy)
    outcome = decide_outcome(tool_names=tool_names, cards=cards, unmet=unmet)
    intent = decide_intent(tool_names=tool_names, unmet=unmet, spec=spec)
    cluster_key = cluster_key_for(
        outcome=outcome, intent=intent, question=masked, spec=spec, unmet=unmet
    )
    return AskEntry(
        at=now,
        session_id=getattr(session, "session_id", ""),
        operator=getattr(session, "operator", ""),
        role=getattr(session, "role", None),
        question=masked,
        intent=intent,
        spec=spec,
        outcome=outcome,
        unmet_reason=unmet[0] if unmet is not None else None,
        wanted=(unmet[2] or None) if unmet is not None else None,
        tool_calls=len(tool_names),
        cards=cards,
        vocabulary_terms=list(vocabulary_terms),
        cluster_key=cluster_key,
        turn_id=turn_id,
    )
