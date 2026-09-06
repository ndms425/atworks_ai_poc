"""승인(applied)된 job을 예정 시각에 실행한다. LLM 호출 없음 — 여기서 모델을 부르는 순간
"실행은 사람이 승인한 계획대로만"이라는 선이 무너진다. tick(now)는 멱등적이다: 한 스케줄의 같은
회차는 두 번 돌지 않는다(회차 = 그 스케줄의 done). 한 job에 여러 스케줄이 있으면 각자의 시계로
따로 판단하고, 한 tick에 둘이 동시에 due면 실행도 두 번이다."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from atworks_agent import (
    AtworksBackend,
    AtworksSessionContext,
    JobKind,
    JobSchedule,
    JobStatus,
    ProfileStatus,
)

from .briefing import Briefings
from .reports import Reports
from .retention import Retention

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
                 briefings: Briefings | None = None, retention: Retention | None = None):
        self.backend = backend
        self.reports = reports
        self.session = session or AtworksSessionContext(session_id="scheduler", project_id="default", operator="scheduler")
        self.briefings = briefings
        self.retention = retention
        self._lock = asyncio.Lock()

    async def tick(self, now: datetime) -> list[str]:
        async with self._lock:
            executed: list[str] = []
            # scale spec §9: `active_jobs` (applied AND remaining_executions > 0), not every applied
            # job ever. An exhausted job is not fetched, not inspected and not touched -- at 500
            # operators' worth of finished schedules that is the difference between a tick that
            # costs O(due) and one that costs O(history). The guard below stays as a belt-and-
            # braces check on a backend whose `active_jobs` is looser than the ABC promises.
            for job in list(await self.backend.active_jobs(self.session)):
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
            if self.retention is not None:
                try:
                    # Once per local day, guarded inside maybe_run (spec §6). Same seat as the
                    # briefing and the same rule: housekeeping never stalls or fails a tick.
                    # `await`: the job steps day by day and yields between partitions, so the
                    # chat SSE turn sharing this loop is never parked behind it.
                    await self.retention.maybe_run(now)
                except Exception:
                    logger.exception("retention job failed")
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
                # scale spec §9: the report is INCREMENTAL now. This occurrence's runs are
                # appended to runs.jsonl and the report re-renders from that file -- the
                # scheduler no longer pages the job's whole run history back out of the store on
                # every occurrence (a 400-cell job × 14 schedule slots was 5,600 records
                # round-tripped through the process to rewrite one summary).
                # The runs are handed over WITHOUT bodies; parity fetches the two bodies per
                # compared cell it actually needs through this loader, inside the 90-day window.
                # Oldest-first, run_id breaking ties: the report resolves a same-timestamp cell
                # tie by taking the last one it sees.
                fresh = sorted(produced, key=lambda r: (r.executed_at, r.run_id))

                async def body_loader(run_id: str) -> dict | None:
                    return await self.backend.get_body(self.session, run_id)

                async def masked_paths_loader(run_id: str) -> list[str]:
                    # Which leaves capture-time masking replaced in that body. Without it the
                    # parity block would call two `***`-vs-`***` bodies equal on `basis: "body"`
                    # -- a value-equality claim over a value it never saw.
                    return await self.backend.get_body_masked_paths(self.session, run_id)

                # A re-write (a later scheduled occurrence, or a profile approved before the
                # job's first run) must still honor every APPLIED comparison profile — the
                # profile is durable once approved, and can't be re-staged. Listing profiles
                # is a best-effort lookup: a failure here must not block the report write
                # itself (the execution already happened), so it falls back to no ignore
                # paths rather than losing the report entirely — but it is still logged and
                # noted, never swallowed silently.
                ignore_paths: list[str] = []
                per_api_ignore: dict[str, list[str]] = {}
                try:
                    profiles = (await self.backend.list_profiles(self.session, job_id)).items
                    for profile in profiles:
                        if profile.status is not ProfileStatus.APPLIED:
                            continue
                        ignore_paths.extend(profile.ignore_paths)
                        for api_id, paths in profile.per_api_ignore.items():
                            per_api_ignore.setdefault(api_id, []).extend(paths)
                except Exception as profile_error:
                    logger.exception("job %s failed to list comparison profiles", job_id)
                    await self.backend.add_guardrail_note(
                        self.session, job_id, f"profile lookup failed: {type(profile_error).__name__}"
                    )
                await self.reports.write(current, fresh, body_loader=body_loader,
                                         masked_paths_loader=masked_paths_loader,
                                         ignore_paths=ignore_paths, per_api_ignore=per_api_ignore or None)
            except Exception as error:
                # Report failure: log and note it, but do NOT record a second execution.
                # The execution already happened.
                logger.exception("job %s report failed", job_id)
                await self.backend.add_guardrail_note(self.session, job_id, f"report failed: {type(error).__name__}")
        return True
