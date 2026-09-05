"""AtworksToolExecutor: 툴당 핸들러 하나, 공용 프레임(BaseToolExecutor) 위에서. 게이트는 핸들러
안에서 백엔드 호출 전에 돈다. stage_shows_preview면 stage_job이 preview 카드도 함께 낸다.
merchant_agent/executor.py 미러."""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from commerce_common.execution import BaseToolExecutor, Handler, clamp_limit, parse_argument
from commerce_common.memory import MemoryRuntime
from commerce_common.presentation import PresentationComponent, PresentationExtension
from commerce_common.skills import SkillRegistry
from commerce_common.streaming import AgentEvent, ToolOutcome

from .aggregation import GROUP_BY, aggregate
from .backend import AtworksBackend
from .config import AtworksAgentConfig
from .enrichment import PRESENTATION_COMPONENTS
from .fencing import ATWORKS_FENCE
from .gates import (
    GUARDRAIL_GATE,
    PROVENANCE_GATE,
    QUESTION_FORM_GATE,
    STAGED_AND_SHOWN_NOTE,
    STAGED_NOTE,
    applied_confirmation,
    apply_guardrail_message,
    check_api_provenance,
    check_apply_job,
    check_apply_rule,
    check_discard_job,
    check_discard_rule,
    check_rule_param_provenance,
    guardrail_block_message,
    take_discard_actor_kind,
    take_rule_discard_actor_kind,
)
from .jobs import (
    CONFIDENCE_KEYS,
    GuardrailViolation,
    JobDraft,
    JobNotApplicable,
    SelectWhere,
    check_job_guardrails,
)
from .memory import ATWORKS_MEMORY_EXTRACTION_PROMPT
from .rules import RuleDraft, RuleGuardrailViolation, ValidationRule, check_rule_guardrails
from .scoring import UnknownScorer, rank_runs
from .selection import resolve_select_where
from .serialization import api_record, job_record, rank_record, rule_record, run_record
from .tools.presentation import PREVIEW_TOOL, QUESTION_TOOL
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


def _rule_guardrail_message(violations: list[str]) -> str:
    """gates.guardrail_block_message names a job; a RuleGuardrailViolation raised from the
    backend (the per-api applied-rule cap, which check_rule_guardrails cannot see without
    the ledger) needs its own wording instead of misreporting a rule as a job."""
    return (
        "That rule exceeds this deployment's guardrails: " + "; ".join(violations)
        + ". Explain the block to the operator and propose a compliant alternative."
    )


def _applied_rule_confirmation(rule_id: str, operator: str) -> str:
    return (
        f"Applied {rule_id} as {operator}. Confirm to the operator that the rule is now "
        "effective for executions from this moment forward (effective_from); past runs are "
        "not re-evaluated."
    )


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
        # Per-turn guards: the executor is built fresh in every stream_turn call, so this
        # instance state never survives past the turn it was built for.
        self._asked_form = False
        self._previewed_jobs: set[str] = set()

    @property
    def memory_subject(self) -> str:
        return self._session.project_id

    def domain_error(self, error: Exception) -> ToolOutcome | None:
        if isinstance(error, RuleGuardrailViolation):
            return ToolOutcome.held(GUARDRAIL_GATE, _rule_guardrail_message(error.violations))
        if isinstance(error, GuardrailViolation):
            return ToolOutcome.held(GUARDRAIL_GATE, guardrail_block_message(error.violations))
        if isinstance(error, (JobNotApplicable, UnknownScorer)):
            return ToolOutcome.error(self._sanitize(str(error), 300))
        if isinstance(error, InvalidToolArgument):
            if error.kind == "integer":
                return ToolOutcome.error(f"{error.field} must be an integer; adjust and call again.")
            if error.kind == "status":
                return ToolOutcome.error(f"{error.field} must be one of pass, fail, error, non_pass; adjust and call again.")
            if error.kind == "enum":
                return ToolOutcome.error(f"{error.field} must be one of {', '.join(GROUP_BY)}; adjust and call again.")
            if error.kind == "provenance":
                return ToolOutcome.error(f"{error.field} must be an api_id that search_apis or get_api returned this session; call search_apis first.")
            if error.kind == "selection":
                return ToolOutcome.error("select_where matched no API; widen failed_since, pick another related_to, or name api_ids.")
            if error.kind == "object":
                if error.field == "test_data.values":
                    return ToolOutcome.error(
                        "test_data.values must be an object of parameter → value strings; adjust and call again."
                    )
                return ToolOutcome.error(f"{error.field} must be an object; adjust and call again.")
            return ToolOutcome.error(
                f"{error.field} must be an ISO 8601 datetime with offset, e.g. "
                "2026-08-27T00:00:00+09:00; adjust and call again."
            )
        return None

    async def _present(self, spec: PresentationComponent, tool_input: dict[str, Any]) -> ToolOutcome:
        # A job already shown this turn is not re-rendered: the card is already on
        # screen, and the round stays "clean" (no note appended) so the turn can still
        # close on it.
        if spec.name == PREVIEW_TOOL and str(tool_input.get("job_id", "")) in self._previewed_jobs:
            return ToolOutcome(self.displayed_text)
        outcome = await super()._present(spec, tool_input)
        if not outcome.refused:
            if spec.name == QUESTION_TOOL:
                self._asked_form = True
            if spec.name == PREVIEW_TOOL:
                self._previewed_jobs.add(str(tool_input.get("job_id", "")))
        return outcome

    async def dispatch(self, name: str, tool_input: dict[str, Any]) -> ToolOutcome:
        if name in ("stage_job", "apply_job", "stage_rule", "apply_rule") and self._asked_form and name not in self._absent:
            return ToolOutcome.held(
                QUESTION_FORM_GATE,
                "A question form is open this turn. End the turn with present_suggestions "
                "and wait for the operator's answers; stage or apply in the next turn.",
            )
        return await super().dispatch(name, tool_input)

    def handlers(self) -> dict[str, Handler]:
        return {
            "search_apis": self._search_apis,
            "get_api": self._get_api,
            "list_runs": self._list_runs,
            "get_run": self._get_run,
            "rank_failed_runs": self._rank_failed_runs,
            "aggregate_runs": self._aggregate_runs,
            "get_pending_jobs": self._get_pending_jobs,
            "stage_job": self._stage_job,
            "apply_job": self._apply_job,
            "discard_job": self._discard_job,
            "get_pending_rules": self._get_pending_rules,
            "stage_rule": self._stage_rule,
            "apply_rule": self._apply_rule,
            "discard_rule": self._discard_rule,
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

    async def _aggregate_runs(self, tool_input: dict[str, Any]) -> ToolOutcome:
        group_by = str(tool_input.get("group_by", ""))
        if group_by not in GROUP_BY:
            raise InvalidToolArgument("group_by", kind="enum")
        status = tool_input.get("status") or None
        if status is not None and status not in RUN_STATUS_FILTERS:
            raise InvalidToolArgument("status", kind="status")
        api_id = tool_input.get("api_id") or None
        if api_id is not None and str(api_id) not in self._state.seen_apis:
            raise InvalidToolArgument("api_id", kind="provenance")
        floor = datetime.now(UTC) - timedelta(days=self._config.max_aggregate_window_days)
        since = _iso(tool_input.get("since"), "since")
        since = floor if since is None or since < floor else since
        runs = await self._backend.list_runs(
            self._session, since=since, status=status, api_id=api_id, limit=self._config.max_aggregate_runs,
        )
        for run in runs:
            self._state.remember_run(run)
        # The api axis labels groups by method+path: fetch the specs the session has not seen yet
        # so the card can name them (each one is remembered, so it also passes provenance later).
        if group_by == "api":
            for missing in {r.api_id for r in runs} - set(self._state.seen_apis):
                api = await self._backend.get_api(self._session, missing)
                if api is not None:
                    self._state.remember_api(api)
        groups = aggregate(runs, self._state.seen_apis, group_by, flaky_min_transitions=self._config.flaky_min_transitions)
        population = len(runs) if api_id is not None else await self._backend.count_runs(self._session, since, status)
        self._state.last_population = population
        self._state.last_listed_run_ids = [r.run_id for r in runs]
        self._state.last_listed_filter = status or "all"
        self._state.remember_groups(group_by, groups, since)
        shown = groups[: self._config.max_group_items * 2]
        return self._fenced({
            "group_by": group_by, "since": since.isoformat(), "population": population,
            "groups": [g.model_dump(mode="json", exclude_none=True, exclude={"run_ids"}) | {"run_ids": g.run_ids[:3]} for g in shown],
            "more": max(0, len(groups) - len(shown)),
            "truncated": len(runs) >= self._config.max_aggregate_runs,
            "note": "Figures are host-computed; show them with present_run_groups (group keys above), never in prose.",
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
                self._previewed_jobs.add(job.job_id)
        return self._fenced({"staged": job_record(job), "note": note}, events)

    async def _stage_job(self, tool_input: dict[str, Any]) -> ToolOutcome:
        api_ids = list(dict.fromkeys(str(a) for a in (_coerce_list(tool_input.get("api_ids")) or [])))
        if held := check_api_provenance(self._state, api_ids):
            return held
        select_where = tool_input.get("select_where")
        selection_basis: str | None = None
        where_model: SelectWhere | None = None
        if select_where is not None:
            where_model = parse_argument(SelectWhere, select_where)
            select_where = where_model.model_dump(mode="json", exclude_none=True)
        if where_model is not None and (where_model.related_to or where_model.failed_since):
            # Server-resolved selections: the anchor must be a seen api (provenance), and the
            # resolved ids replace whatever the model listed — the backend is the source.
            if where_model.related_to and where_model.related_to not in self._state.seen_apis:
                return ToolOutcome.held(
                    PROVENANCE_GATE,
                    f"select_where.related_to {where_model.related_to!r} was not returned by search_apis/get_api this session; look it up first.",
                )
            resolution = await resolve_select_where(self._backend, self._session, where_model, self._config, now=datetime.now(UTC))
            if not resolution.api_ids:
                raise InvalidToolArgument("select_where", kind="selection")
            for api in resolution.apis.values():
                self._state.remember_api(api)
            api_ids = resolution.api_ids
            selection_basis = resolution.basis
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
        test_data: list[dict[str, Any]] = []
        for item in (_coerce_list(tool_input.get("test_data")) or []):
            if not isinstance(item, dict):
                continue
            values = item.get("values")
            if values is not None and not isinstance(values, dict):
                raise InvalidToolArgument("test_data.values", kind="object")
            test_data.append({
                "label": self._sanitize(item.get("label"), 40),
                "values": {
                    self._sanitize(key, 60): self._sanitize(value, 200)
                    for key, value in (values or {}).items()
                },
            })
        confidence_input = tool_input.get("confidence")
        if confidence_input is not None and not isinstance(confidence_input, dict):
            raise InvalidToolArgument("confidence", kind="object")
        draft = parse_argument(JobDraft, {
            "kind": tool_input.get("kind"), "summary": self._sanitize(tool_input.get("summary"), 200),
            "api_ids": api_ids,
            "target_envs": target_envs,
            "schedules": _coerce_list(tool_input.get("schedules")) or [],
            "test_data": test_data,
            "select_where": select_where,
            "selection_basis": selection_basis,
            "binding": tool_input.get("binding") or "FROZEN", "report": tool_input.get("report", True),
            "confidence": {
                k: float(v) for k, v in (confidence_input or {}).items()
                if k in CONFIDENCE_KEYS and isinstance(v, (int, float))
            },
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

    # -- 검증 규칙 ----------------------------------------------------------------------

    async def _remember_and_preview_rule(self, rule: ValidationRule) -> ToolOutcome:
        self._state.remember_rule(rule)
        events = [AgentEvent.change_update(rule_record(rule))]
        # Task 5 adds the rule_preview card (RULE_PREVIEW_TOOL / present_rule_preview do not
        # exist yet): only the change_update fires here, so the note always reads as staged
        # only, never "shown on its preview card".
        return self._fenced({"staged": rule_record(rule), "note": STAGED_NOTE}, events)

    async def _stage_rule(self, tool_input: dict[str, Any]) -> ToolOutcome:
        api_id = str(tool_input.get("api_id", ""))
        param = self._sanitize(tool_input.get("param"), 80)
        if held := check_rule_param_provenance(self._state, api_id, param):
            return held
        values = [self._sanitize(v, 120) for v in (_coerce_list(tool_input.get("values")) or [])]
        confidence_input = tool_input.get("confidence")
        if confidence_input is not None and not isinstance(confidence_input, dict):
            raise InvalidToolArgument("confidence", kind="object")
        raw_value = tool_input.get("value")
        raw_pattern = tool_input.get("pattern")
        draft = parse_argument(RuleDraft, {
            "api_id": api_id, "param": param, "kind": tool_input.get("kind"), "op": tool_input.get("op"),
            "value": self._sanitize(raw_value, 120) if raw_value is not None else None,
            "values": values,
            "format": tool_input.get("format") or None,
            "pattern": self._sanitize(raw_pattern, 200) if raw_pattern is not None else None,
            "summary": self._sanitize(tool_input.get("summary"), 200),
            "confidence": {
                k: float(v) for k, v in (confidence_input or {}).items() if isinstance(v, (int, float))
            },
            "assumptions": [self._sanitize(a, 160) for a in (_coerce_list(tool_input.get("assumptions")) or [])][:6],
        })
        # Mirrors _stage_job: the guardrail runs here, before the backend call, using the
        # session's own provenance-checked catalogue (check_rule_param_provenance above
        # already confirmed api_id is in state.seen_apis).
        api = self._state.seen_apis.get(api_id)
        if violations := check_rule_guardrails(draft, self._config, api):
            return ToolOutcome.held(GUARDRAIL_GATE, _rule_guardrail_message(violations))
        rule = await self._backend.stage_rule(self._session, draft, ActorKind.AGENT)
        self._state.rule_impacts[rule.rule_id] = await self._backend.simulate_rule(self._session, draft)
        return await self._remember_and_preview_rule(rule)

    async def _get_pending_rules(self, _: dict[str, Any]) -> ToolOutcome:
        pending = await self._backend.get_pending_rules(self._session)
        for rule in pending:
            self._state.remember_rule(rule)
        return self._fenced([rule_record(r) for r in pending] or {"note": "Nothing is waiting for approval."})

    async def _apply_rule(self, tool_input: dict[str, Any]) -> ToolOutcome:
        rule_id = str(tool_input.get("rule_id", ""))
        if held := check_apply_rule(self._state, self._config, rule_id):
            return held
        try:
            applied = await self._backend.apply_rule(self._session, rule_id)
        except RuleGuardrailViolation as violation:
            return ToolOutcome.held(GUARDRAIL_GATE, _rule_guardrail_message(violation.violations))
        self._state.remember_rule(applied)
        return ToolOutcome(_applied_rule_confirmation(rule_id, self._session.operator),
                           [AgentEvent.change_update(rule_record(applied))])

    async def _discard_rule(self, tool_input: dict[str, Any]) -> ToolOutcome:
        rule_id = str(tool_input.get("rule_id", ""))
        if held := check_discard_rule(self._state, rule_id):
            return held
        discarded = await self._backend.discard_rule(
            self._session, rule_id, take_rule_discard_actor_kind(self._state, rule_id)
        )
        self._state.remember_rule(discarded)
        return ToolOutcome(f"Discarded {rule_id}.", [AgentEvent.change_update(rule_record(discarded))])
