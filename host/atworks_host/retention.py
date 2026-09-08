"""보존 계층(scale spec 2026-09-06 §6). 스케줄러 tick 꼬리에서 **하루 1회**, LLM 0회 — 브리핑과
같은 자리, 같은 성질(결정론, 멱등, 실패해도 실행을 막지 않는다).

계층별 동작:

======  =========================  ==========================  ==================================
층      데이터                     기간                        동작
======  =========================  ==========================  ==================================
핫      ``runs``                   ``retention_hot_days``      인덱스 조회
콜드    ``runs_archive``           ``retention_cold_until``    핫에서 **이관**(삭제 아님).
                                                               조회는 ``RunsQuery(archived=True)``
본문    ``bodies``                 ``retention_body_days``     **삭제** — 유일하게 지우는 데이터
영구    롤업·워터마크·현재상태     —                           손대지 않는다
파생    ``insights_out/<op>/<날짜>`` ``insights_cache_days``    회전
세션    ``SessionStore``           ``session_idle_hours``      TTL 스윕
질문    ``ask_log``                ``ask_log_retention_days``  **삭제**(자가발전 spec §6)
통계    ``sqlite_stat1``           —                           ``ANALYZE`` — 계층이 아니라 플래너
                                                               유지보수(``analyze:<날짜>``)
======  =========================  ==========================  ==================================

콜드 삭제는 ``retention_cold_until``이 **설정되어 있고 지났을 때만** 일어난다 — 기본값 None은
"영구 보관"이고, 이 코드베이스에서 run 레코드를 지우는 경로는 그 한 줄이 유일하다.

세션 스윕만 이 모듈에 ``SessionStore`` 서브클래스로 들어온다: ``sessions.py``는 레퍼런스와
**줄바꿈만 다른 동일 파일**(레퍼런스는 CRLF, 이 저장소는 `.gitattributes`가 강제하는 LF)로
유지되어야 하므로(호스트 계약), 마지막 접근 시각 스탬프와 스윕은 여기
``TimestampedSessionStore``가 얹는다. "바이트 동일"이라고 적으면 EOL 정규화 때문에 영원히
거짓인 주장이 된다 — 지켜야 하는 불변식은 **내용이 한 글자도 다르지 않다**는 쪽이다.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from atworks_agent import AtworksAgentConfig

from .sessions import SessionStore, StateT
from .store import Store

logger = logging.getLogger(__name__)

#: The `retention_state.partition_key` prefixes, one per step, plus the once-a-day guard.
STEPS = ("runs_archived", "bodies_deleted", "archive_purged", "insight_cache_rotated",
         "sessions_swept", "asks_deleted", "analyze")
DAILY_GUARD = "daily"
#: Rows per body-DELETE statement. Small enough that no single statement holds the loop for
#: seconds at 450k rows, large enough that the loop is a few dozen statements, not thousands.
BODY_DELETE_SLICE = 5_000


class TimestampedSessionStore(SessionStore[StateT]):
    """``SessionStore`` plus a last-touched stamp and an idle sweep (spec §6 "세션 유휴 24h TTL").

    The base class is identical to the reference host contract MODULO LINE ENDINGS (the
    reference is CRLF, this repository is LF by `.gitattributes`) and must stay that way, so
    the TTL lives here: every state read and every state write stamps the session, and ``sweep``
    deletes the ones nobody has touched in ``idle_hours``. A session with no stamp at all is
    never swept — the sweep only ever acts on evidence it has, so it cannot delete a live
    session it simply never saw."""

    def __init__(self, state_type: type[StateT], clock: Callable[[], datetime] | None = None) -> None:
        super().__init__(state_type)
        self._touched: dict[str, datetime] = {}
        self._clock = clock or (lambda: datetime.now().astimezone())

    def touch(self, session_id: str) -> None:
        self._touched[session_id] = self._clock()

    def read_state(self, session_id: str) -> tuple[int, dict[str, Any]] | None:
        stored = super().read_state(session_id)
        if stored is not None:
            self.touch(session_id)
        return stored

    def write_state(self, session_id: str, document: dict[str, Any], version: int) -> None:
        super().write_state(session_id, document, version)
        self.touch(session_id)

    def delete(self, session_id: str) -> None:
        super().delete(session_id)
        self._touched.pop(session_id, None)

    def sweep(self, idle_hours: int, now: datetime) -> int:
        """Delete every session idle for longer than ``idle_hours``; returns how many went."""
        cutoff = now - timedelta(hours=idle_hours)
        stale = [
            session_id for session_id, touched in self._touched.items()
            if touched < cutoff
        ]
        for session_id in stale:
            self.delete(session_id)
        return len(stale)


class Retention:
    """The daily retention job. ``run(now)`` does the work unconditionally (tests and the bench
    call it directly); ``maybe_run(now)`` is what the scheduler calls at the tick tail and is
    guarded to once per LOCAL day by a ``retention_state`` row."""

    def __init__(self, store: Store, config: AtworksAgentConfig,
                 insights_dir: Path | None = None,
                 sessions: TimestampedSessionStore | None = None) -> None:
        self.store = store
        self.config = config
        self.insights_dir = insights_dir
        self.sessions = sessions

    def local_date(self, now: datetime) -> date:
        return now.astimezone(ZoneInfo(self.config.briefing_tz)).date()

    async def run(self, now: datetime) -> dict[str, int]:
        """One pass over every tier. Returns ``{step: rows affected}`` and writes one
        ``retention_state`` row per step (`<step>:<YYYY-MM-DD>`), so what ran on which day is
        itself evidence — the same reason the audit log exists.

        ``async`` because it runs at the scheduler's tick tail, inside the same event loop that
        serves chat SSE: the two big steps are broken into bounded statements with a yield
        between them (spec §6 "일 단위 이관"), so a day's housekeeping can never park the loop the
        way one whole-dataset statement did. It stays on the loop's own thread deliberately —
        the SQLite connection is shared with every read and every ingest, and handing a statement
        on it to ``asyncio.to_thread`` would put two threads on one connection."""
        day = self.local_date(now).isoformat()
        counts: dict[str, int] = {}

        # 핫 -> 콜드. Moves, never deletes; rollups/watermarks/current_state are permanent and
        # are deliberately not touched, so every aggregate read answers identically afterwards.
        counts["runs_archived"] = await self._archive_runs(now)

        # 본문 삭제 -- the one tier that really goes away (spec §6).
        counts["bodies_deleted"] = await self._delete_bodies(now)

        # 콜드 삭제: ONLY when a cutoff date is configured AND has passed. None = 영구 보관.
        cold_until = self.config.retention_cold_until
        counts["archive_purged"] = (
            self.store.purge_runs_archive()
            if cold_until is not None and self.local_date(now) > cold_until else 0
        )

        counts["insight_cache_rotated"] = self._rotate_insight_cache(now)
        counts["sessions_swept"] = (
            self.sessions.sweep(self.config.session_idle_hours, now) if self.sessions is not None else 0
        )

        # ask_log 회전 (자가발전 spec §6): the question text is the operator's own words, masked at
        # write, and it is evidence for the promoter and the unmet clusters -- but not forever.
        # One bounded DELETE, no day loop: a year of asks is thousands of rows, not millions.
        counts["asks_deleted"] = self.store.delete_asks_before(
            now - timedelta(days=self.config.ask_log_retention_days))

        # ANALYZE. Not a tier -- housekeeping for the PLANNER, and this is the one place that
        # already runs once a local day, LLM-free, on a store whose shape has just changed
        # (a day partition moved out, a day's bodies deleted). Stale `sqlite_stat1` is what made
        # the query engine's bench row read 790 ms instead of 166 ms. Counted as 1/0 rather than
        # a row count: ANALYZE affects no rows, and reporting 0 would read as "did not run".
        self.store.analyze()
        counts["analyzed"] = 1

        for step in STEPS:
            self.store.set_retention_state(f"{step}:{day}", now)
        return counts

    async def maybe_run(self, now: datetime) -> dict[str, int] | None:
        """Once per local day (``briefing_tz``), guarded by the ``daily:<YYYY-MM-DD>`` row —
        the same shape as the briefing's file-existence guard, and idempotent for the same
        reason: a tick every 60s must not re-archive all day. Returns None when already done."""
        key = f"{DAILY_GUARD}:{self.local_date(now).isoformat()}"
        if self.store.get_retention_state(key) is not None:
            return None
        counts = await self.run(now)
        self.store.set_retention_state(key, now)
        return counts

    async def _archive_runs(self, now: datetime) -> int:
        """One ``day`` partition per transaction, oldest first, with the loop handed back between
        days. Each partition is independently correct and the whole step is idempotent, so a
        crash (or a cancelled tick) mid-way leaves committed days moved and the rest for
        tomorrow — the same "committed slices are correct" property the chunked ingest has."""
        cutoff = now - timedelta(days=self.config.retention_hot_days)
        moved = 0
        for day in self.store.run_days_before(cutoff):
            moved += self.store.archive_runs_before(cutoff, now, day)
            await asyncio.sleep(0)
        return moved

    async def _delete_bodies(self, now: datetime) -> int:
        """Bounded slices, a yield between each. No day partition here — ``bodies`` has no ``day``
        column; ``captured_at`` plus a row cap is the same bound by a different key."""
        cutoff = now - timedelta(days=self.config.retention_body_days)
        deleted = 0
        while True:
            slice_deleted = self.store.delete_bodies_before(cutoff, BODY_DELETE_SLICE)
            deleted += slice_deleted
            await asyncio.sleep(0)
            if slice_deleted < BODY_DELETE_SLICE:
                return deleted

    def _rotate_insight_cache(self, now: datetime) -> int:
        """``insights_out/<operator>/<YYYY-MM-DD>/`` older than ``insights_cache_days`` — a
        regenerable derivative, so rotation loses nothing. A folder whose name is not a date is
        left alone: this deletes only what it can positively identify as an expired cache day."""
        if self.insights_dir is None or not self.insights_dir.exists():
            return 0
        cutoff = self.local_date(now) - timedelta(days=self.config.insights_cache_days)
        removed = 0
        for operator_dir in self.insights_dir.iterdir():
            if not operator_dir.is_dir():
                continue
            for day_dir in operator_dir.iterdir():
                if not day_dir.is_dir():
                    continue
                try:
                    folder_date = date.fromisoformat(day_dir.name)
                except ValueError:
                    continue
                if folder_date < cutoff:
                    shutil.rmtree(day_dir, ignore_errors=True)
                    removed += 1
        return removed
