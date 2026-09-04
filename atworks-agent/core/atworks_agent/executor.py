"""AtworksToolExecutor: 툴당 핸들러 하나, 공용 프레임(BaseToolExecutor) 위에서. 게이트는 핸들러
안에서 백엔드 호출 전에 돈다. stage_shows_preview면 stage_job이 preview 카드도 함께 낸다.
merchant_agent/executor.py 미러."""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from commerce_common.execution import BaseToolExecutor, Handler, clamp_limit, parse_argument
from commerce_common.memory import MemoryRuntime
from commerce_common.presentation import PresentationExtension
from commerce_common.skills import SkillRegistry
from commerce_common.streaming import AgentEvent, ToolOutcome

from .backend import AtworksBackend
from .config import AtworksAgentConfig
from .enrichment import PRESENTATION_COMPONENTS
from .fencing import ATWORKS_FENCE
from .gates import (
    GUARDRAIL_GATE,
    STAGED_AND_SHOWN_NOTE,
    STAGED_NOTE,
    applied_confirmation,
    apply_guardrail_message,
    check_api_provenance,
    check_apply_job,
    check_discard_job,
    guardrail_block_message,
    take_discard_actor_kind,
)
from .jobs import GuardrailViolation, JobDraft, JobNotApplicable, SelectWhere, check_job_guardrails
from .memory import ATWORKS_MEMORY_EXTRACTION_PROMPT
from .scoring import UnknownScorer, rank_runs
from .serialization import api_record, job_record, rank_record, run_record
from .tools.presentation import PREVIEW_TOOL
from .types import (
    ActorKind,
    AtworksSessionContext,
    AtworksSessionState,
    JobSpec,
    RunResult,
    RunStatus,
)


def build_memory(config: AtworksAgentConfig, store: Any, write_filter: Any = None) -> MemoryRuntime:
    return MemoryRuntime.build(config, store, fence=ATWORKS_FENCE,
                               extraction_prompt=ATWORKS_MEMORY_EXTRACTION_PROMPT, write_filter=write_filter)


RUN_STATUS_FILTERS = ("pass", "fail", "error", "non_pass")


def _matches_filter(run: RunResult, filt: str) -> bool:
    """Whether ``run`` belongs to the population the last ``list_runs`` (or a fresh/attached
    window) is counting — the same rule ``list_runs``' backend filter applies, so ``get_run``
    never grows a filtered population with a run the filter would have excluded (R32/I1)."""
    return (
        filt in ("all", "attached")
        or (filt == "non_pass" and run.status is not RunStatus.PASS)
        or run.status.value == filt
    )


class InvalidToolArgument(ValueError):
    """A tool argument failed a manual (non-pydantic) check; ``domain_error`` reports it
    by field name instead of the generic unavailable ladder."""

    def __init__(self, field: str, *, kind: str = "datetime") -> None:
        super().__init__(field)
        self.field = field
        self.kind = kind


def _iso(value: Any, field: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidToolArgument(field) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _limit(raw: Any, default: int, ceiling: int) -> int:
    try:
        return clamp_limit(raw, default, ceiling)
    except (TypeError, ValueError) as error:
        raise InvalidToolArgument("limit", kind="integer") from error


def _coerce_list(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, list) else None
    return None


class AtworksToolExecutor(BaseToolExecutor):
    fence = ATWORKS_FENCE
    components = PRESENTATION_COMPONENTS
    displayed_text = "Shown to the operator."
    unavailable_text = "{name} is temporarily unavailable. Work with what you already have or let the operator know."
    absent_text = "{name} is not something this deployment does; say so plainly and do not suggest it."

    def __init__(
        self, *, backend: AtworksBackend, config: AtworksAgentConfig, skills: SkillRegistry,
        session: AtworksSessionContext, state: AtworksSessionState, memory: MemoryRuntime | None = None,
        extensions: Sequence[PresentationExtension] = (), progress: Callable[[AgentEvent], None] | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        super().__init__(backend=backend, config=config, skills=skills, session=session, state=state,
                         memory=memory or build_memory(config, None), extensions=extensions, delegates=(),
                         progress=progress, usage=usage)

    @property
    def memory_subject(self) -> str:
        return self._session.project_id

    def domain_error(self, error: Exception) -> ToolOutcome | None:
        if isinstance(error, GuardrailViolation):
            return ToolOutcome.held(GUARDRAIL_GATE, guardrail_block_message(error.violations))
        if isinstance(error, (JobNotApplicable, UnknownScorer)):
            return ToolOutcome.error(self._sanitize(str(error), 300))
        if isinstance(error, InvalidToolArgument):
            if error.kind == "integer":
                return ToolOutcome.error(f"{error.field} must be an integer; adjust and call again.")
            if error.kind == "status":
                return ToolOutcome.error(f"{error.field} must be one of pass, fail, error, non_pass; adjust and call again.")
            return ToolOutcome.error(
                f"{error.field} must be an ISO 8601 datetime with offset, e.g. "
                "2026-08-27T00:00:00+09:00; adjust and call again."
            )
        return None

    def handlers(self) -> dict[str, Handler]:
        return {
            "search_apis": self._search_apis,
            "get_api": self._get_api,
            "list_runs": self._list_runs,
            "get_run": self._get_run,
            "rank_failed_runs": self._rank_failed_runs,
            "get_pending_jobs": self._get_pending_jobs,
            "stage_job": self._stage_job,
            "apply_job": self._apply_job,
            "discard_job": self._discard_job,
        }

    # -- 읽기 -------------------------------------------------------------------------

    async def _search_apis(self, tool_input: dict[str, Any]) -> ToolOutcome:
        apis = await self._backend.search_apis(
            self._session, query=self._sanitize(tool_input.get("query"), 120),
            updated_after=_iso(tool_input.get("updated_after"), "updated_after"), group=tool_input.get("group") or None,
            limit=_limit(tool_input.get("limit"), 20, 200),
        )
        for api in apis:
            self._state.remember_api(api)
        return self._fenced({"count": len(apis), "apis": [api_record(a) for a in apis]} if apis else {"note": "No APIs matched."})

    async def _get_api(self, tool_input: dict[str, Any]) -> ToolOutcome:
        api = await self._backend.get_api(self._session, str(tool_input.get("api_id", "")))
        if api is None:
            return ToolOutcome.error("No API with that id.")
        self._state.remember_api(api)
        return self._fenced(api_record(api))

    async def _list_runs(self, tool_input: dict[str, Any]) -> ToolOutcome:
        filters = tool_input.get("filters") or {}
        since = _iso(filters.get("since"), "since")
        status = filters.get("status") or None
        if status is not None and status not in RUN_STATUS_FILTERS:
            raise InvalidToolArgument("filters.status", kind="status")
        runs = await self._backend.list_runs(
            self._session, since=since, status=status, api_id=filters.get("api_id") or None,
            limit=_limit(tool_input.get("limit"), 50, 200),
        )
        population = await self._backend.count_runs(self._session, since, status)
        self._state.last_population = population
        for run in runs:
            self._state.remember_run(run)
        self._state.last_listed_run_ids = [r.run_id for r in runs]
        self._state.last_listed_filter = status or "all"
        return self._fenced({"population": population, "shown": len(runs), "runs": [run_record(r) for r in runs]})

    async def _get_run(self, tool_input: dict[str, Any]) -> ToolOutcome:
        run = await self._backend.get_run(self._session, str(tool_input.get("run_id", "")))
        if run is None:
            return ToolOutcome.error("No run with that id.")
        self._state.remember_run(run)
        # A run fetched directly (an attached item, a follow-up on one id) has no
        # list_runs population behind it yet; seed one so present_run_digest can still
        # say "N of M" instead of refusing. A population already drawn from a real
        # filter (last_listed_filter != "attached") is left alone except to grow by one
        # when this run was not already in that window — and only when the run actually
        # belongs to that filter's population (R32/I1): a pass run fetched while the last
        # list_runs was "non_pass" must not inflate a non-pass count.
        if not _matches_filter(run, self._state.last_listed_filter):
            return self._fenced(run_record(run))
        window = self._state.last_listed_run_ids
        appended = run.run_id not in window
        if appended:
            window.append(run.run_id)
        if self._state.last_population is None:
            self._state.last_population = len(window)
            self._state.last_listed_filter = "attached"
        elif self._state.last_listed_filter == "attached":
            self._state.last_population = len(window)
        elif appended:
            self._state.last_population += 1
        return self._fenced(run_record(run))

    async def _rank_failed_runs(self, tool_input: dict[str, Any]) -> ToolOutcome:
        if not self._state.last_listed_run_ids or self._state.last_population is None:
            return ToolOutcome.error("Nothing to rank yet — call list_runs first; it records the runs and their population.")
        scorer = str(tool_input.get("scorer") or self._config.default_scorer)
        limit = _limit(tool_input.get("limit"), self._config.max_rank_items, self._config.max_rank_items)
        window = [self._state.seen_runs[i] for i in self._state.last_listed_run_ids if i in self._state.seen_runs]
        ranked = rank_runs(scorer, window, self._state.seen_apis, limit)
        for rank in ranked:
            self._state.remember_rank(rank)
        return self._fenced({
            "scorer": scorer, "population": self._state.last_population, "ranked": [rank_record(r) for r in ranked],
            "note": "A reading order over this session's non-pass runs, not a verdict. Present with present_run_digest.",
        })

    # -- 실행 계획 ----------------------------------------------------------------------

    async def _remember_and_preview(self, job: JobSpec) -> ToolOutcome:
        self._state.remember_job(job)
        events = [AgentEvent.change_update(job_record(job))]
        note = STAGED_NOTE
        if self._config.stage_shows_preview:
            preview = await self._present(self.components[PREVIEW_TOOL], {"job_id": job.job_id})
            if not preview.refused:
                events += preview.events
                note = STAGED_AND_SHOWN_NOTE
        return self._fenced({"staged": job_record(job), "note": note}, events)

    async def _stage_job(self, tool_input: dict[str, Any]) -> ToolOutcome:
        api_ids = list(dict.fromkeys(str(a) for a in (_coerce_list(tool_input.get("api_ids")) or [])))
        if held := check_api_provenance(self._state, api_ids):
            return held
        select_where = tool_input.get("select_where")
        if select_where is not None:
            select_where = parse_argument(SelectWhere, select_where).model_dump(mode="json", exclude_none=True)
        # Envs are sanitized to the per-item cap and de-duplicated the way api_ids are: a
        # repeated env is a harmless restatement, and an over-long one is trimmed here so the
        # guardrail message names a readable value instead of a wall of text. The trimmed
        # value still has to be on the allow-list, so nothing slips through by being long.
        target_envs = list(dict.fromkeys(
            self._sanitize(e, 32) for e in (_coerce_list(tool_input.get("target_envs")) or [])
        ))
        # Test-data labels, keys and values are model-authored strings that end up on a card
        # and in a report: sanitize each through the fence before pydantic validates them.
        # A label that had to be truncated or scrubbed carries the fence's "...[truncated]" /
        # "[removed]" marker, whose brackets TestDataSet.label's pattern rejects — an
        # over-long or hostile label surfaces as a named invalid-arguments error rather than
        # being quietly mangled into something the operator then approves.
        test_data = [
            {
                "label": self._sanitize(item.get("label"), 40),
                "values": {
                    self._sanitize(key, 60): self._sanitize(value, 200)
                    for key, value in (item.get("values") or {}).items()
                },
            }
            for item in (_coerce_list(tool_input.get("test_data")) or [])
            if isinstance(item, dict)
        ]
        draft = parse_argument(JobDraft, {
            "kind": tool_input.get("kind"), "summary": self._sanitize(tool_input.get("summary"), 200),
            "api_ids": api_ids,
            "target_envs": target_envs,
            "schedules": _coerce_list(tool_input.get("schedules")) or [],
            "test_data": test_data,
            "select_where": select_where,
            "binding": tool_input.get("binding") or "FROZEN", "report": tool_input.get("report", True),
            "confidence": tool_input.get("confidence") or {},
            "assumptions": [self._sanitize(a, 160) for a in (_coerce_list(tool_input.get("assumptions")) or [])][:6],
        })
        # The guardrail runs here, before the backend call: a permissive backend must never be
        # handed a job this deployment's caps reject (R18). state.seen_apis is the catalogue
        # rule 7 needs — every api_id above already passed provenance, so every one is in it.
        if violations := check_job_guardrails(draft, self._config, self._state.seen_apis):
            return ToolOutcome.held(GUARDRAIL_GATE, guardrail_block_message(violations))
        job = await self._backend.stage_job(self._session, draft, ActorKind.AGENT)
        return await self._remember_and_preview(job)

    async def _get_pending_jobs(self, _: dict[str, Any]) -> ToolOutcome:
        pending = await self._backend.get_pending_jobs(self._session)
        for job in pending:
            self._state.remember_job(job)
        return self._fenced([job_record(j) for j in pending] or {"note": "Nothing is waiting for approval."})

    async def _apply_job(self, tool_input: dict[str, Any]) -> ToolOutcome:
        job_id = str(tool_input.get("job_id", ""))
        if held := check_apply_job(self._state, self._config, job_id):
            return held
        try:
            applied = await self._backend.apply_job(self._session, job_id)
        except GuardrailViolation as violation:
            return ToolOutcome.held(GUARDRAIL_GATE, apply_guardrail_message(violation.violations))
        self._state.remember_job(applied)
        return ToolOutcome(applied_confirmation(job_id, applied.kind.value, self._session.operator),
                           [AgentEvent.change_update(job_record(applied))])

    async def _discard_job(self, tool_input: dict[str, Any]) -> ToolOutcome:
        job_id = str(tool_input.get("job_id", ""))
        if held := check_discard_job(self._state, job_id):
            return held
        discarded = await self._backend.discard_job(self._session, job_id, take_discard_actor_kind(self._state, job_id))
        self._state.remember_job(discarded)
        return ToolOutcome(f"Discarded {job_id}.", [AgentEvent.change_update(job_record(discarded))])
