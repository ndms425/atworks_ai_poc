"""일일 브리핑 = 리포트와 같은 "data.json + 템플릿" 경로. 스케줄러 tick이 briefing_at을 지나면 하루
한 번 만든다(파일 존재로 멱등). LLM 0회 — 모든 숫자는 aggregation.py의 결정론 출력이다."""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from atworks_agent import (
    ApiSpec,
    AtworksAgentConfig,
    AtworksBackend,
    AtworksSessionContext,
    JobSpec,
    JobStatus,
    RunResult,
    collect_runs,
)
from atworks_agent.aggregation import aggregate, summarize_insights

logger = logging.getLogger(__name__)
TEMPLATE = Path(__file__).with_name("briefing_template.html")
SAFE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STALE_AFTER = timedelta(hours=24)


def build_briefing(now: datetime, runs: list[RunResult], apis: dict[str, ApiSpec], jobs: list[JobSpec],
                   config: AtworksAgentConfig, *, window: tuple[datetime, datetime], portal_origin: str) -> dict:
    start, end = window
    in_window = [r for r in runs if start <= r.executed_at < end]
    counts = {"total": len(in_window), "pass": 0, "fail": 0, "error": 0}
    for r in in_window:
        counts[r.status.value] += 1
    groups = aggregate([r for r in in_window if r.status.value != "pass"], apis, "failed_rule",
                       flaky_min_transitions=config.flaky_min_transitions)
    insights = summarize_insights(runs, apis, config)
    executed_job_ids = {r.job_id for r in in_window if r.job_id}
    executed = [{"job_id": j.job_id, "summary": j.summary, "runs": sum(1 for r in in_window if r.job_id == j.job_id)}
                for j in jobs if j.job_id in executed_job_ids]
    pending = [j for j in jobs if j.status is JobStatus.STAGED]
    return {
        "date": end.date().isoformat(),
        "generated_at": now.isoformat(),
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "counts": counts,
        "top_groups": [g.model_dump(mode="json", exclude_none=True) for g in groups[:3]],
        "insights": insights.model_dump(),
        "jobs": {
            "executed": executed,
            "pending": [{"job_id": j.job_id, "summary": j.summary, "created_at": j.created_at.isoformat()} for j in pending],
            "stale_pending": sum(1 for j in pending if now - j.created_at >= STALE_AFTER),
        },
        "portal_origin": portal_origin,
        "note": "statuses are aTworks rule verdicts; no model output on this page",
    }


class Briefings:
    def __init__(self, out_dir: Path, config: AtworksAgentConfig, *, portal_origin: str = "http://localhost:3110"):
        self.out_dir = out_dir
        self.config = config
        self.portal_origin = portal_origin
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self.config.briefing_tz)

    def _folder(self, date: str) -> Path:
        if not SAFE_DATE.fullmatch(date):
            raise ValueError(f"unsafe briefing date: {date!r}")
        folder = (self.out_dir / date).resolve()
        if not folder.is_relative_to(self.out_dir.resolve()):
            raise ValueError("briefing date escapes the directory")
        return folder

    def window_end(self, now: datetime) -> datetime:
        """Today's briefing_at in briefing_tz — the window's exclusive end and the briefing's date."""
        hh, mm = (int(x) for x in self.config.briefing_at.split(":"))
        local = now.astimezone(self._tz())
        return local.replace(hour=hh, minute=mm, second=0, microsecond=0)

    def date_for(self, now: datetime) -> str:
        return self.window_end(now).date().isoformat()

    def is_due(self, now: datetime) -> bool:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        if not self.config.briefing_enabled:
            return False
        end = self.window_end(now)
        return now.astimezone(self._tz()) >= end and not (self._folder(end.date().isoformat()) / "data.json").exists()

    async def generate(self, backend: AtworksBackend, session: AtworksSessionContext, now: datetime) -> Path:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        end = self.window_end(now)
        start = end - timedelta(days=1)
        since = now - timedelta(days=self.config.max_aggregate_window_days)
        runs = await collect_runs(backend, session, since=since, cap=self.config.max_aggregate_runs)
        apis = {a.api_id: a for a in (await backend.search_apis(session, query="", limit=1000)).items}
        jobs = (await backend.all_jobs(session, limit=1000)).items
        data = build_briefing(now, runs, apis, jobs, self.config, window=(start, end), portal_origin=self.portal_origin)
        folder = self._folder(data["date"])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        embedded = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")   # same rule as reports.py
        (folder / "index.html").write_text(TEMPLATE.read_text(encoding="utf-8").replace("__BRIEFING_DATA__", embedded), encoding="utf-8")
        return folder / "index.html"

    async def maybe_generate(self, backend: AtworksBackend, session: AtworksSessionContext, now: datetime) -> str | None:
        if not self.is_due(now):
            return None
        await self.generate(backend, session, now)
        return self.date_for(now)

    def read_html(self, date: str) -> str | None:
        path = self._folder(date) / "index.html"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def latest(self) -> dict | None:
        dates = sorted(p.name for p in self.out_dir.iterdir() if p.is_dir() and SAFE_DATE.fullmatch(p.name) and (p / "data.json").exists())
        if not dates:
            return None
        return json.loads((self.out_dir / dates[-1] / "data.json").read_text(encoding="utf-8"))
