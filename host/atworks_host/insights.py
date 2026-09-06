"""per-operator 인사이트 패널 조립. 매 호출 scope/candidate는 실시간 재계산되고, 서술(narration)
만 하루 1회 캐시된다(`insights_out/<operator_id>/<YYYY-MM-DD>/narrative.json`). LLM은 `narrator`
콜백 안에서만 호출된다 — 이 파일 자체에는 LLM 호출이 없다. `reports.py`/`briefing.py`와 같은
경로-안전 규칙(SAFE_ID·SAFE_DATE + is_relative_to 탈출 검사)을 쓴다."""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from atworks_agent import (
    AtworksAgentConfig,
    AtworksBackend,
    AtworksSessionContext,
    InsightCandidate,
    InsightItem,
    InsightNarrative,
    InsightPanel,
)
from atworks_agent.insights import candidate_insights, operator_scope

logger = logging.getLogger(__name__)

# 운영자 id 모양만 통과시킨다: OperatorProfile.operator_id의 패턴과 같다. 세그먼트 구분자
# (``/``, ``\``)도, ``..``도 이 안엔 들어갈 수 없다 — 캐시 경로에 그대로 잇는 유일한 방어선이다.
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SAFE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

Narrator = Callable[..., Awaitable[list[InsightNarrative]]]


class InsightPanels:
    def __init__(self, out_dir: Path, config: AtworksAgentConfig, narrator: Narrator):
        self.out_dir = out_dir
        self.config = config
        self.narrator = narrator
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def _folder(self, operator_id: str, date: str) -> Path:
        if not SAFE_ID.fullmatch(operator_id):
            raise ValueError(f"unsafe operator id: {operator_id!r}")
        if not SAFE_DATE.fullmatch(date):
            raise ValueError(f"unsafe insight date: {date!r}")
        folder = (self.out_dir / operator_id / date).resolve()
        if not folder.is_relative_to(self.out_dir.resolve()):
            raise ValueError("insight cache path escapes the directory")
        return folder

    def _date_for(self, now: datetime) -> str:
        return now.astimezone(ZoneInfo(self.config.briefing_tz)).date().isoformat()

    async def _narrate(self, candidates: list[InsightCandidate], role: str) -> list[InsightNarrative]:
        # narrate_insights already fails open to [], but this callable is a seam a caller
        # could swap for something that doesn't — never let a narrator exception surface
        # here and break the panel.
        notes: list[str] = []
        try:
            result = await self.narrator(candidates, role, notes=notes)
        except Exception:
            return []
        finally:
            for note in notes:
                logger.warning("insight narrator dropped an item: %s", note)
        return list(result) if result else []

    async def build(
        self, backend: AtworksBackend, session: AtworksSessionContext, now: datetime, *, refresh: bool = False
    ) -> InsightPanel:
        cfg = self.config
        since = now - timedelta(days=max(cfg.scope_window_days, cfg.max_aggregate_window_days))
        runs = await backend.list_runs(session, since=since, status=None, limit=cfg.max_aggregate_runs)
        apis = {a.api_id: a for a in await backend.search_apis(session, query="", limit=1000)}
        jobs = await backend.all_jobs(session)
        profile = next((p for p in await backend.list_operators(session) if p.operator_id == session.operator), None)
        name = profile.name if profile is not None else session.operator
        role = session.role or "developer"

        scope = operator_scope(runs, session.operator, cfg.scope_window_days, now)
        scope_fallback = not scope
        cands = candidate_insights(runs, apis, jobs, (scope or None), role, cfg, now)

        date = self._date_for(now)
        folder = self._folder(session.operator, date)
        cache_path = folder / "narrative.json"

        cache_hit = False
        generated_at_raw: str | None = None
        narratives: list[InsightNarrative] = []
        if cache_path.exists() and not refresh:
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                generated_at_raw = cached["generated_at"]
                datetime.fromisoformat(generated_at_raw)
                narratives = [InsightNarrative.model_validate(n) for n in cached.get("narratives", [])]
                cache_hit = True
            except Exception as exc:
                # A corrupt/partial narrative.json (bad JSON, a stale schema, a garbled date)
                # must never 500 the panel for the rest of the day — log it and fall through to
                # treating this as a cache miss, which overwrites the file below.
                logger.warning("insight narrative cache read failed for %s/%s: %s", session.operator, date, exc)

        if not cache_hit:
            narratives = await self._narrate(cands, role) if cfg.enable_insight_narration else []
            generated_at_raw = now.isoformat()
            # Don't write the cache when narration is disabled outright, so the demo switch
            # (ATWORKS_INSIGHT_NARRATION=0) isn't sticky for the rest of the day. But DO keep
            # caching an empty result when the narrator ran and failed (returned []) — without
            # this, every Home load re-attempts narration and eats the full timeout while the
            # model endpoint is down.
            if cfg.enable_insight_narration:
                folder.mkdir(parents=True, exist_ok=True)
                tmp_path = cache_path.with_name(cache_path.name + ".tmp")
                tmp_path.write_text(
                    json.dumps(
                        {
                            "generated_at": generated_at_raw,
                            "generated_by": "agent" if narratives else "deterministic",
                            "narratives": [n.model_dump(mode="json") for n in narratives],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                os.replace(tmp_path, cache_path)

        narrative_by_id = {n.candidate_id: n for n in narratives}
        items = [InsightItem(candidate=c, narrative=narrative_by_id.get(c.candidate_id)) for c in cands]
        attached = any(item.narrative is not None for item in items)

        generated_at = datetime.fromisoformat(generated_at_raw) if generated_at_raw else now

        return InsightPanel(
            operator_id=session.operator,
            name=name,
            role=role,
            scope_api_ids=sorted(scope)[:20],
            scope_size=len(scope),
            scope_fallback=scope_fallback,
            window_days=cfg.scope_window_days,
            generated_at=generated_at,
            generated_by="agent" if attached else "deterministic",
            items=items,
        )
