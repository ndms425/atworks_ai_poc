"""질문 승격(자가발전 spec §8). 스케줄러 tick 꼬리에서 **하루 1회**, 보존 작업 바로 뒤 —
브리핑·보존과 같은 자리, 같은 성질이다: 결정론, 멱등, LLM 0회, 실패해도 실행을 막지 않는다.

하는 일 한 줄: 최근 ``promote_window_days``의 ``ask_log``에서 답이 나온 행을 ``cluster_key``로
묶어, 서로 다른 사람이 ``promote_min_users`` 이상, 건수가 ``promote_min_asks`` 이상인 군집을
``saved_questions`` 한 행으로 만든다. 그게 전부다 —

* **모델은 이 경로에 없다.** 제목은 ``catalog.title_for_spec``이 카탈로그 라벨로 만든 문장이고,
  스펙은 그 군집이 실제로 실행했던 ``QuerySpec``이며, 숫자(몇 명이 몇 번)는 SQL이 센 값이다.
* **재승격이 없다.** ``cluster_key``가 UNIQUE고 삽입은 ``INSERT OR IGNORE``라, 같은 질문이 다음
  주에도 문턱을 넘어도 카드는 하나이고 사람이 숨긴 질문이 내일 새 행으로 되살아나지 않는다.
* **👎가 많던 군집은 승격하지 않는다.** 투표가 있는 행 중 절반 넘게 👎였다면 그건 "자주 묻는
  질문"이 아니라 "자주 빗나가는 답"이라, Home 카드에 고정할 이유가 없다.
* **id는 군집의 함수다**(``sq-`` + sha1(cluster_key) 12자리). 저장소를 다시 만들어도, 다른
  배포에서 같은 군집이 승격돼도 같은 id가 나온다 -- 링크와 감사 행이 그 위에서 안정적이다.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from atworks_agent import AtworksAgentConfig, QuerySpec, SavedQuestion
from atworks_agent.catalog import title_for_spec

from .store import Store

logger = logging.getLogger(__name__)

#: 하루 1회 가드가 쓰는 ``retention_state`` 행의 접두사 (spec §8/§11: ``promote:<YYYY-MM-DD>``).
DAILY_GUARD = "promote"

#: 이 비율을 **넘는** 👎(투표한 행 기준)면 승격하지 않는다. 정확히 절반은 통과한다 -- 반반인
#: 군집을 나쁜 평가로 단정하지 않는다.
MAX_DOWN_RATIO = 0.5


def saved_question_id(cluster_key: str) -> str:
    """군집 키의 결정론 해시. 같은 질문은 언제 어디서 승격되든 같은 id를 얻는다."""
    return "sq-" + hashlib.sha1(cluster_key.encode("utf-8")).hexdigest()[:12]


class Promoter:
    """``run(now)``은 무조건 한 번 돌고(테스트·수동 실행), ``maybe_run(now)``은 스케줄러가 부르는
    쪽으로 ``retention_state``의 ``promote:<YYYY-MM-DD>`` 행에 하루 1회로 묶여 있다 -- 보존
    작업의 ``daily:`` 가드와 같은 모양, 같은 이유(tick은 60초마다 온다)."""

    def __init__(self, store: Store, config: AtworksAgentConfig, *,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.config = config
        self._clock = clock or (lambda: datetime.now().astimezone())

    def local_date(self, now: datetime) -> date:
        return now.astimezone(ZoneInfo(self.config.briefing_tz)).date()

    async def run(self, now: datetime | None = None) -> dict[str, int]:
        """후보를 훑어 승격한다. 돌려주는 숫자는 전부 이번 실행에서 실제로 일어난 일이다:
        ``candidates``(문턱을 넘은 군집 수), ``promoted``(새로 만든 행), ``skipped_downvoted``
        (👎 비율로 제외), ``invalid``(스펙이 지금 카탈로그에서 더 이상 유효하지 않아 건너뛴 군집 --
        카탈로그가 바뀐 뒤의 오래된 행이 여기 들어온다).

        ``async``인 이유는 보존 작업과 같다: 채팅 SSE를 서빙하는 바로 그 이벤트 루프에서 도니까
        후보 사이마다 제어를 돌려준다."""
        moment = now or self._clock()
        since = moment - timedelta(days=self.config.promote_window_days)
        candidates = self.store.promote_candidates(
            since, self.config.promote_min_users, self.config.promote_min_asks)
        counts = {"candidates": len(candidates), "promoted": 0, "skipped_downvoted": 0, "invalid": 0}
        for candidate in candidates:
            await asyncio.sleep(0)
            if candidate["down_ratio"] > MAX_DOWN_RATIO:
                counts["skipped_downvoted"] += 1
                continue
            question = self._build(candidate, moment)
            if question is None:
                counts["invalid"] += 1
                continue
            if self.store.insert_saved_question(question):
                counts["promoted"] += 1
        return counts

    async def maybe_run(self, now: datetime | None = None) -> dict[str, int] | None:
        """하루 1회(``briefing_tz`` 기준), ``promote:<YYYY-MM-DD>`` 행이 가드다. 이미 돈 날이면
        None -- 매 tick마다 같은 주를 다시 접지 않는다."""
        moment = now or self._clock()
        key = f"{DAILY_GUARD}:{self.local_date(moment).isoformat()}"
        if self.store.get_retention_state(key) is not None:
            return None
        counts = await self.run(moment)
        self.store.set_retention_state(key, moment)
        return counts

    def _build(self, candidate: dict[str, Any], now: datetime) -> SavedQuestion | None:
        """군집 하나 -> 저장 질문 하나. 스펙이 지금의 카탈로그에서 유효하지 않으면(차원이 사라졌다든가)
        None을 돌려준다 -- 하루치 승격 전체를 예외로 날리는 대신 그 군집만 조용히 건너뛴다."""
        try:
            spec = QuerySpec.model_validate_json(candidate["spec_json"])
        except ValueError:
            logger.warning("saved-question candidate %r has a spec the current catalogue rejects",
                           candidate["cluster_key"])
            return None
        return SavedQuestion(
            id=saved_question_id(candidate["cluster_key"]),
            cluster_key=candidate["cluster_key"],
            spec=spec,
            # 제목의 기간은 호스트가 실제로 적용하는 기본 창이다 -- 카드가 "기본 기간"이라고 쓰지
            # 않도록 `Store.query`/`query_runs`와 같은 값을 넘긴다.
            title=title_for_spec(spec, default_window_days=self.config.max_aggregate_window_days),
            created_at=now,
            status="active",
            source_users=candidate["users"],
            source_asks=candidate["asks"],
        )
