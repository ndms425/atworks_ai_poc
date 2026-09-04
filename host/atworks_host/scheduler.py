"""승인(applied)된 job을 예정 시각에 실행한다. LLM 호출 없음 — 여기서 모델을 부르는 순간
"실행은 사람이 승인한 계획대로만"이라는 선이 무너진다. tick(now)는 멱등적이다: 한 스케줄의 같은
회차는 두 번 돌지 않는다(회차 = 그 스케줄의 done). 한 job에 여러 스케줄이 있으면 각자의 시계로
따로 판단하고, 한 tick에 둘이 동시에 due면 실행도 두 번이다."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from atworks_agent import AtworksBackend, AtworksSessionContext, JobKind, JobSchedule, JobStatus

from .briefing import Briefings
from .reports import Reports

logger = logging.getLogger(__name__)


def due_at(schedule: JobSchedule, index: int) -> datetime | None:
    """이 스케줄의 index번째(0부터) 회차 예정 시각. count를 넘어서면 None."""
    if index >= schedule.count:
        return None
    hh, mm = (int(x) for x in schedule.at.split(":"))
    first = datetime.fromisoformat(schedule.from_date).replace(hour=hh, minute=mm, tzinfo=ZoneInfo(schedule.tz))
    return first + timedelta(days=index) if schedule.kind == "daily" else (first if index == 0 else None)


class Scheduler:
    def __init__(self, backend: AtworksBackend, reports: Reports, session: AtworksSessionContext | None,
                 briefings: Briefings | None = None):
        self.backend = backend
        self.reports = reports
        self.session = session or AtworksSessionContext(session_id="scheduler", project_id="default", operator="scheduler")
        self.briefings = briefings
        self._lock = asyncio.Lock()

    async def tick(self, now: datetime) -> list[str]:
        async with self._lock:
            executed: list[str] = []
            for job in list(await self.backend.applied_jobs(self.session)):
                if job.status is not JobStatus.APPLIED or job.remaining_executions <= 0:
                    continue
                try:
                    if job.kind is JobKind.RUN_NOW:
                        # No schedule to advance: run_now is due once, the moment it is applied.
                        slots: list[int | None] = [None] if job.executions == 0 else []
                    else:
                        slots = []
                        for index, schedule in enumerate(job.schedules):
                            if schedule.done >= schedule.count:
                                continue
                            when = due_at(schedule, schedule.done)
                            if when is not None and when <= now:
                                slots.append(index)
                except Exception as error:
                    # A bad schedule (e.g. an unresolvable tz) must not stall every other job:
                    # spend the slot and move on, the same shape as an execution failure (M12).
                    logger.exception("job %s failed while computing due slots", job.job_id)
                    await self.backend.add_guardrail_note(
                        self.session, job.job_id, f"scheduling failed: {type(error).__name__}"
                    )
                    await self.backend.record_execution(self.session, job.job_id, [], None)
                    continue
                for schedule_index in slots:
                    if await self._execute_one(job.job_id, schedule_index, job.report):
                        executed.append(job.job_id)
            if self.briefings is not None:
                try:
                    await self.briefings.maybe_generate(self.backend, self.session, now)
                except Exception:
                    # The briefing is a convenience artifact; a failure must not stall job execution.
                    logger.exception("daily briefing failed")
            return executed

    async def _execute_one(self, job_id: str, schedule_index: int | None, report: bool) -> bool:
        """One occurrence of one schedule. Returns whether it produced runs. The backend
        consumes the slot itself; the only slot this method consumes is the one an exception
        would otherwise leave unspent (R29: an unspent slot re-runs against the real target
        on the very next tick)."""
        try:
            produced = await self.backend.execute_job_once(self.session, job_id, schedule_index)
        except Exception as error:
            # Execution failure: record as such and move on.
            logger.exception("job %s failed during scheduled execution", job_id)
            await self.backend.add_guardrail_note(self.session, job_id, f"execution failed: {type(error).__name__}")
            await self.backend.record_execution(self.session, job_id, [], schedule_index)
            return False
        if not produced:
            return False
        if report:
            try:
                current = await self.backend.get_job(self.session, job_id)
                every = await self.backend.runs_by_ids(self.session, current.run_ids)
                self.reports.write(current, every)
            except Exception as error:
                # Report failure: log and note it, but do NOT record a second execution.
                # The execution already happened.
                logger.exception("job %s report failed", job_id)
                await self.backend.add_guardrail_note(self.session, job_id, f"report failed: {type(error).__name__}")
        return True
