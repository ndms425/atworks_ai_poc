"""승인(applied)된 job을 예정 시각에 실행한다. LLM 호출 없음. tick(now)는 멱등적이다: 같은 회차는
두 번 돌지 않는다(회차 = 지금까지 실행된 횟수)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from atworks_agent import AtworksBackend, AtworksSessionContext, JobKind, JobSpec, JobStatus

from .reports import Reports

logger = logging.getLogger(__name__)


def due_at(job: JobSpec, index: int) -> datetime | None:
    """index번째(0부터) 회차의 예정 시각. run_now는 시각이 없다(승인 즉시, tick이 따로 본다)."""
    if job.kind is JobKind.RUN_NOW:
        return None
    s = job.schedule
    if s is None or index >= s.count:
        return None
    hh, mm = (int(x) for x in s.at.split(":"))
    first = datetime.fromisoformat(s.from_date).replace(hour=hh, minute=mm, tzinfo=ZoneInfo(s.tz))
    return first + timedelta(days=index) if s.kind == "daily" else (first if index == 0 else None)


class Scheduler:
    def __init__(self, backend: AtworksBackend, reports: Reports, session: AtworksSessionContext | None):
        self.backend = backend
        self.reports = reports
        self.session = session or AtworksSessionContext(session_id="scheduler", project_id="default", operator="scheduler")
        self._lock = asyncio.Lock()

    async def tick(self, now: datetime) -> list[str]:
        async with self._lock:
            executed: list[str] = []
            for job in list(await self.backend.applied_jobs(self.session)):
                if job.status is not JobStatus.APPLIED or (job.runs_remaining or 0) <= 0:
                    continue
                index = (job.schedule.count if job.schedule else 1) - (job.runs_remaining or 0)
                if job.kind is JobKind.RUN_NOW:
                    due = index == 0
                else:
                    when = due_at(job, index)
                    due = when is not None and when <= now
                if not due:
                    continue
                try:
                    produced = await self.backend.execute_job_once(self.session, job.job_id)
                except Exception as error:
                    # Execution failure: record as such and move on.
                    logger.exception("job %s failed during scheduled execution", job.job_id)
                    await self.backend.add_guardrail_note(self.session, job.job_id, f"execution failed: {type(error).__name__}")
                    await self.backend.record_execution(self.session, job.job_id, [])
                    continue
                if not produced:
                    continue
                if job.report:
                    try:
                        current = await self.backend.get_job(self.session, job.job_id)
                        every = await self.backend.runs_by_ids(self.session, current.run_ids)
                        self.reports.write(current, every)
                    except Exception as error:
                        # Report failure: log and note it, but do NOT record a second execution.
                        # The execution already happened.
                        logger.exception("job %s report failed", job.job_id)
                        await self.backend.add_guardrail_note(self.session, job.job_id, f"report failed: {type(error).__name__}")
                executed.append(job.job_id)
            return executed
