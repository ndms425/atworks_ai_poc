"""AtworksToolExecutor: 툴당 핸들러 하나, 공용 프레임(BaseToolExecutor) 위에서. 게이트는 핸들러
안에서 백엔드 호출 전에 돈다. stage_shows_preview면 stage_job이 preview 카드도 함께 낸다.
merchant_agent/executor.py 미러."""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, get_args

from commerce_common.execution import BaseToolExecutor, Handler, clamp_limit, parse_argument
from commerce_common.memory import MemoryRuntime
from commerce_common.presentation import PresentationComponent, PresentationExtension
from commerce_common.skills import SkillRegistry
from commerce_common.streaming import AgentEvent, ToolOutcome
from commerce_common.types import PROVENANCE_CAP
from pydantic import ValidationError

from .aggregation import GROUP_BY
from .backend import AtworksBackend
from .catalog import title_for_spec
from .config import AtworksAgentConfig
from .enrichment import PRESENTATION_COMPONENTS
from .fencing import ATWORKS_FENCE
from .gates import (
    GUARDRAIL_GATE,
    PROVENANCE_GATE,
    QUESTION_FORM_GATE,
    STAGED_AND_SHOWN_NOTE,
    STAGED_AND_SHOWN_RULE_NOTE,
    STAGED_NOTE,
    STAGED_RULE_NOTE,
    applied_confirmation,
    apply_guardrail_message,
    check_api_provenance,
    check_apply_format_batch,
    check_apply_job,
    check_apply_profile,
    check_apply_rule,
    check_discard_format_batch,
    check_discard_job,
    check_discard_profile,
    check_discard_rule,
    check_rule_param_provenance,
    guardrail_block_message,
    take_discard_actor_kind,
    take_format_batch_discard_actor_kind,
    take_profile_discard_actor_kind,
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
from .profiles import ProfileDraft, ProfileGuardrailViolation, check_profile_guardrails
from .rules import (
    NAMED_FORMATS,
    FormatBatchDraft,
    FormatBatchGuardrailViolation,
    RuleDraft,
    RuleGuardrailViolation,
    ValidationRule,
    check_format_batch_guardrails,
    check_rule_guardrails,
)
from .scoring import UnknownScorer, rank_runs
from .selection import resolve_select_where
from .serialization import (
    api_record,
    format_batch_record,
    job_record,
    profile_record,
    rank_record,
    rule_recommendation_record,
    rule_record,
    run_record,
)
from .tools.presentation import PREVIEW_TOOL, QUESTION_TOOL, RULE_PREVIEW_TOOL
from .types import (
    ActorKind,
    AggregateQuery,
    AtworksSessionContext,
    AtworksSessionState,
    FormatBatch,
    JobSpec,
    QuerySpec,
    RunResult,
    RunsQuery,
    RunStatus,
    UnmetReason,
)


def build_memory(config: AtworksAgentConfig, store: Any, write_filter: Any = None) -> MemoryRuntime:
    return MemoryRuntime.build(config, store, fence=ATWORKS_FENCE,
                               extraction_prompt=ATWORKS_MEMORY_EXTRACTION_PROMPT, write_filter=write_filter)


RUN_STATUS_FILTERS = ("pass", "fail", "error", "non_pass")
# The four reasons note_unmet_ask accepts, read off the Literal so the tool schema, the executor's
# validation and the ask_log column can never drift apart (self-growth spec §5/§6).
UNMET_REASONS: tuple[str, ...] = get_args(UnmetReason)
# Groups asked of one aggregate_runs call. Comfortably above the card's own max_group_items*2, so
# the envelope's `more` is exact for any ordinary result, and each returned group costs at most
# one bounded run_ids lookup in the backend.
_AGGREGATE_GROUP_LIMIT = 50
# Candidate evidence ids gathered from those groups before the newest PROVENANCE_CAP are kept.
_AGGREGATE_RUN_ID_FETCH = 1000
# Evidence run ids per row in the query_runs envelope. The backend stores up to 5 per row (and
# the card shows them all); the fenced text carries fewer, because the model only ever needs one
# or two to cite -- the rest are for the card and for provenance, not for the prompt's bytes.
_QUERY_ROW_RUN_IDS = 3


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
    by field name instead of the generic unavailable ladder.

    ``detail`` carries a specific reason when there is one (the pydantic message behind a
    ``kind="schema"`` failure) so the model is told WHICH field of a nested object it got
    wrong, rather than only that the object was rejected."""

    def __init__(self, field: str, *, kind: str = "datetime", detail: str | None = None) -> None:
        super().__init__(field)
        self.field = field
        self.kind = kind
        self.detail = detail


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


def _format_batch_guardrail_message(violations: list[str]) -> str:
    return (
        "That format batch exceeds this deployment's guardrails: " + "; ".join(violations)
        + ". Explain the block to the operator and propose a smaller batch."
    )


def _applied_format_batch_confirmation(batch: FormatBatch, operator: str) -> str:
    return (
        f"Applied {batch.batch_id} as {operator}: {batch.new_count} new format(s) added to the "
        f"library, {batch.duplicate_count} duplicate(s) and {batch.invalid_count} invalid entry/entries "
        "skipped. A library format changes no run until an approved rule references it."
    )


def _profile_guardrail_message(violations: list[str]) -> str:
    """Mirrors _rule_guardrail_message: gates.apply_profile_guardrail_message is worded for the
    apply path (check_apply_profile), so stage_profile needs its own staging-time wording."""
    return (
        "That comparison profile exceeds this deployment's guardrails: " + "; ".join(violations)
        + ". Explain the block to the operator and propose fewer ignore paths."
    )


def _applied_profile_confirmation(profile_id: str, operator: str) -> str:
    return (
        f"Applied {profile_id} as {operator}. Confirm to the operator that the profile is now "
        "effective for comparisons from this moment forward (effective_from) and that its "
        "target job's parity report has been re-diffed with these ignore paths — no new runs "
        "were made, and past comparison results were not re-judged."
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
        self._previewed_rules: set[str] = set()

    @property
    def memory_subject(self) -> str:
        return self._session.project_id

    def domain_error(self, error: Exception) -> ToolOutcome | None:
        if isinstance(error, RuleGuardrailViolation):
            return ToolOutcome.held(GUARDRAIL_GATE, _rule_guardrail_message(error.violations))
        if isinstance(error, ProfileGuardrailViolation):
            return ToolOutcome.held(GUARDRAIL_GATE, _profile_guardrail_message(error.violations))
        if isinstance(error, FormatBatchGuardrailViolation):
            return ToolOutcome.held(GUARDRAIL_GATE, _format_batch_guardrail_message(error.violations))
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
            if error.kind == "schema":
                detail = f" ({self._sanitize(error.detail, 300)})" if error.detail else ""
                return ToolOutcome.error(
                    f"{error.field} is not a valid query spec{detail}; fix it against the catalogue "
                    "in the tool description and call again."
                )
            if error.kind == "scope":
                return ToolOutcome.error(
                    f"{error.field} may only be this session's own operator; drop it or use the "
                    "executed_by filter to name whose runs you mean."
                )
            if error.kind == "provenance":
                return ToolOutcome.error(f"{error.field} must be an api_id that search_apis or get_api returned this session; call search_apis first.")
            if error.kind == "selection":
                return ToolOutcome.error("select_where matched no API; widen failed_since, pick another related_to, or name api_ids.")
            if error.kind == "format_examples":
                return ToolOutcome.error(
                    f"a raw pattern format rule needs at least {self._config.min_format_examples} pass "
                    f"example(s) and {self._config.min_format_examples} fail example(s); add more and call again."
                )
            if error.kind == "unknown_format":
                return ToolOutcome.error(
                    f"{error.field} does not name a known format (built-in or saved); call list_formats "
                    "or use a raw pattern instead."
                )
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
        # A job (or rule) already shown this turn is not re-rendered: the card is already
        # on screen, and the round stays "clean" (no note appended) so the turn can still
        # close on it.
        if spec.name == PREVIEW_TOOL and str(tool_input.get("job_id", "")) in self._previewed_jobs:
            return ToolOutcome(self.displayed_text)
        if spec.name == RULE_PREVIEW_TOOL and str(tool_input.get("rule_id", "")) in self._previewed_rules:
            return ToolOutcome(self.displayed_text)
        outcome = await super()._present(spec, tool_input)
        if not outcome.refused:
            # ask_log's "카드가 나왔나" (asklog.decide_outcome). Counted here, at the one place a
            # component's `ui` event is born, so a refused or held presentation never counts as
            # an answer -- the operator saw nothing.
            if any(event.type == "ui" for event in outcome.events):
                self._state.turn_cards += 1
            if spec.name == QUESTION_TOOL:
                self._asked_form = True
            if spec.name == PREVIEW_TOOL:
                self._previewed_jobs.add(str(tool_input.get("job_id", "")))
            if spec.name == RULE_PREVIEW_TOOL:
                self._previewed_rules.add(str(tool_input.get("rule_id", "")))
        return outcome

    async def dispatch(self, name: str, tool_input: dict[str, Any]) -> ToolOutcome:
        # Every tool this turn calls, in call order, at the ONE place every path (execute, a
        # host prefetch, a parallel gather) funnels through. asklog.classify_turn reads nothing
        # else to decide the turn's outcome and intent, so a name recorded anywhere but here
        # would be a second, drifting source of truth.
        self._state.turn_tool_names.append(name)
        if name in (
            "stage_job", "apply_job", "stage_rule", "apply_rule", "stage_profile", "apply_profile",
        ) and self._asked_form and name not in self._absent:
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
            "query_runs": self._query_runs,
            "get_pending_jobs": self._get_pending_jobs,
            "stage_job": self._stage_job,
            "apply_job": self._apply_job,
            "discard_job": self._discard_job,
            "get_pending_rules": self._get_pending_rules,
            "stage_rule": self._stage_rule,
            "apply_rule": self._apply_rule,
            "discard_rule": self._discard_rule,
            "find_apis_with_param": self._find_apis_with_param,
            "recommend_rules_for_api": self._recommend_rules_for_api,
            "get_pending_format_batches": self._get_pending_format_batches,
            "stage_format_batch": self._stage_format_batch,
            "apply_format_batch": self._apply_format_batch,
            "discard_format_batch": self._discard_format_batch,
            "get_pending_profiles": self._get_pending_profiles,
            "stage_profile": self._stage_profile,
            "apply_profile": self._apply_profile,
            "discard_profile": self._discard_profile,
            "recommend_ignore_paths": self._recommend_ignore_paths,
            "note_unmet_ask": self._note_unmet_ask,
        }

    # -- 읽기 -------------------------------------------------------------------------

    async def _search_apis(self, tool_input: dict[str, Any]) -> ToolOutcome:
        page = await self._backend.search_apis(
            self._session, query=self._sanitize(tool_input.get("query"), 120),
            updated_after=_iso(tool_input.get("updated_after"), "updated_after"), group=tool_input.get("group") or None,
            limit=_limit(tool_input.get("limit"), 20, 200),
        )
        for api in page.items:
            self._state.remember_api(api)
        if not page.items:
            return self._fenced({"note": "No APIs matched."})
        return self._fenced({
            "items": [api_record(a) for a in page.items], "total": page.total, "next_cursor": page.next_cursor,
        })

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
        page = await self._backend.list_runs(self._session, RunsQuery(
            since=since, status=status, api_id=filters.get("api_id") or None,
            limit=_limit(tool_input.get("limit"), 50, 200),
        ))
        self._state.last_population = page.total
        for run in page.items:
            self._state.remember_run(run)
        # Provenance state is capped like every other seen-record map (R32-adjacent):
        # PROVENANCE_CAP bounds how many ids this window can name even though page.total
        # (the population a card cites) is the true, uncapped count.
        self._state.last_listed_run_ids = [r.run_id for r in page.items][:PROVENANCE_CAP]
        self._state.last_listed_filter = status or "all"
        return self._fenced({
            "items": [run_record(r) for r in page.items], "total": page.total, "next_cursor": page.next_cursor,
        })

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
        # Task 8: the backend groups over its rollups -- the whole window, every API, no run
        # sample. `limit` bounds how many groups come back (they are ranked fail+error first), so
        # `more` is exact up to that bound and understates beyond it.
        groups = await self._backend.aggregate_runs(self._session, AggregateQuery(
            since=since, group_by=group_by, status=status,
            scope_api_ids=[str(api_id)] if api_id is not None else None, limit=_AGGREGATE_GROUP_LIMIT,
        ))
        # The api axis labels groups by method+path: fetch the specs the session has not seen yet
        # so the card can name them (each one is remembered, so it also passes provenance later).
        if group_by == "api":
            for missing in {g.key for g in groups} - set(self._state.seen_apis):
                api = await self._backend.get_api(self._session, missing)
                if api is not None:
                    self._state.remember_api(api)
        # Provenance: the evidence run ids the groups themselves cite (<=50 newest each), newest
        # first across the whole result and capped like every other seen-record map. This is a
        # SAMPLE for grounding, never the population -- which stays the backend's own count.
        cited = list(dict.fromkeys(i for g in groups for i in g.run_ids))[:_AGGREGATE_RUN_ID_FETCH]
        runs = await self._backend.runs_by_ids(self._session, cited)
        runs.sort(key=lambda r: (r.executed_at, r.run_id), reverse=True)
        for run in runs:
            self._state.remember_run(run)
        population = await self._backend.count_runs(self._session, since=since, until=None, status=status, api_id=api_id)
        self._state.last_population = population
        self._state.last_listed_run_ids = [r.run_id for r in runs][:PROVENANCE_CAP]
        self._state.last_listed_filter = status or "all"
        self._state.remember_groups(group_by, groups, since)
        shown = groups[: self._config.max_group_items * 2]
        return self._fenced({
            "group_by": group_by, "since": since.isoformat(), "population": population,
            "groups": [
                g.model_dump(mode="json", exclude_none=True, exclude={"run_ids", "api_sample"})
                | {"run_ids": g.run_ids[:3]} for g in shown
            ],
            "more": max(0, len(groups) - len(shown)),
            "note": "Figures are host-computed; show them with present_run_groups (group keys above), never in prose.",
        })

    async def _query_runs(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """The self-growth structured query (spec §5). The whole tool input IS the QuerySpec:
        pydantic (extra="forbid", catalogue enums, limit<=50) is the only thing that decides what
        a valid question looks like, and the SQL behind it is the host's — no model string
        reaches a statement. Two checks live here rather than in the model because they are about
        this SESSION, not about the shape: an api_id filter must name an API the session has
        seen, and scope_operator may only be the session's own operator."""
        try:
            spec = QuerySpec.model_validate(tool_input)
        except ValidationError as invalid:
            first = invalid.errors()[0]
            location = ".".join(str(part) for part in first["loc"]) or "spec"
            raise InvalidToolArgument("spec", kind="schema", detail=f"{location}: {first['msg']}") from invalid
        unseen = [i for i in (spec.filters.api_ids or []) if i not in self._state.seen_apis]
        if unseen:
            raise InvalidToolArgument("filters.api_ids", kind="provenance")
        # "내가 실행한 것" is the operator asking about THEIR OWN scope. A spec naming another
        # operator there would use one person's session to profile another's work, so it is
        # refused outright rather than silently rewritten; `executed_by` is the honest filter for
        # "runs someone else made" and stays open.
        if spec.filters.scope_operator is not None and spec.filters.scope_operator != self._session.operator:
            raise InvalidToolArgument("filters.scope_operator", kind="scope")
        result = await self._backend.query_runs(self._session, spec)
        if result.turn_id is None and self._state.current_turn_id:
            result = result.model_copy(update={"turn_id": self._state.current_turn_id})
        # Provenance (spec §2 clause 2): the evidence ids the rows cite become session-seen, so a
        # card or a screen directive may name them under exactly the same rule as any other id.
        # These are SAMPLES -- the population stays the backend's own `population` count.
        missing = [i for i in dict.fromkeys(i for row in result.rows for i in row.api_ids)
                   if i not in self._state.seen_apis]
        if missing:
            for api in await self._backend.get_apis(self._session, missing):
                self._state.remember_api(api)
        cited = list(dict.fromkeys(i for row in result.rows for i in row.run_ids))
        runs = await self._backend.runs_by_ids(self._session, cited) if cited else []
        runs.sort(key=lambda r: (r.executed_at, r.run_id), reverse=True)
        for run in runs:
            self._state.remember_run(run)
        self._state.last_listed_run_ids = [r.run_id for r in runs][:PROVENANCE_CAP]
        self._state.last_listed_filter = spec.filters.status or "all"
        self._state.last_population = result.population
        self._state.last_query_result = result
        since, until = result.window
        return self._fenced({
            "spec_summary": title_for_spec(spec, default_window_days=self._config.max_aggregate_window_days),
            "source": result.source,
            "window": {"since": since.isoformat(), "until": until.isoformat()},
            "population": result.population,
            "total_groups": result.total_groups,
            "rows": [
                {"keys": row.keys, "measures": row.measures, "run_ids": row.run_ids[:_QUERY_ROW_RUN_IDS]}
                for row in result.rows[: spec.limit]
            ],
            "note": "Figures are host-computed; show them with present_query_table, never in prose.",
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
        note = STAGED_RULE_NOTE
        if self._config.stage_shows_preview:
            preview = await self._present(self.components[RULE_PREVIEW_TOOL], {"rule_id": rule.rule_id})
            if not preview.refused:
                events += preview.events
                note = STAGED_AND_SHOWN_RULE_NOTE
                self._previewed_rules.add(rule.rule_id)
        return self._fenced({"staged": rule_record(rule), "note": note}, events)

    async def _stage_rule(self, tool_input: dict[str, Any]) -> ToolOutcome:
        api_id = str(tool_input.get("api_id", ""))
        param = self._sanitize(tool_input.get("param"), 80)
        if held := check_rule_param_provenance(self._state, api_id, param):
            return held
        values = [self._sanitize(v, 120) for v in (_coerce_list(tool_input.get("values")) or [])]
        pass_examples = [self._sanitize(v, 120) for v in (_coerce_list(tool_input.get("pass_examples")) or [])]
        fail_examples = [self._sanitize(v, 120) for v in (_coerce_list(tool_input.get("fail_examples")) or [])]
        confidence_input = tool_input.get("confidence")
        if confidence_input is not None and not isinstance(confidence_input, dict):
            raise InvalidToolArgument("confidence", kind="object")
        raw_value = tool_input.get("value")
        raw_pattern = tool_input.get("pattern")
        raw_format = tool_input.get("format") or None
        raw_save_format_as = tool_input.get("save_format_as")
        format_name: str | None = None
        if tool_input.get("kind") == "format" and raw_format is not None and raw_format not in NAMED_FORMATS:
            # Not one of the five built-in enum values: it must name a LIBRARY entry (built-in
            # or operator-saved). Resolved here, at stage time, into a pattern rule -- the
            # source name is kept only for display (ValidationRule.format_name) so evaluate()
            # stays pattern-based and backend-independent (Task 3 ruling). A name the library
            # does not recognize is a named error, never a crash.
            defn = await self._backend.get_format(self._session, raw_format)
            if defn is None:
                raise InvalidToolArgument("format", kind="unknown_format")
            format_name = raw_format
            raw_format = None
            raw_pattern = defn.pattern
            pass_examples = list(defn.pass_examples)
            fail_examples = list(defn.fail_examples)
            # Referencing an existing library format must not also carry a "save this under a
            # new name" instruction -- the pattern already exists, so re-saving is meaningless
            # and confusing (Task 3 fix round 1 ruling). Clear it before the draft is built.
            raw_save_format_as = None
        elif (
            # RuleDraft's own validator only guarantees >=1 pass/fail example for a raw-pattern
            # format rule; this deployment's floor (config.min_format_examples) can be higher, so
            # it is enforced here as a named error before the draft is even built. Skipped for a
            # library-resolved format above: its examples already passed this check when the
            # format was first authored as a raw-pattern rule.
            tool_input.get("kind") == "format" and raw_format is None and raw_pattern is not None
            and (len(pass_examples) < self._config.min_format_examples
                 or len(fail_examples) < self._config.min_format_examples)
        ):
            raise InvalidToolArgument("pass_examples", kind="format_examples")
        draft = parse_argument(RuleDraft, {
            "api_id": api_id, "param": param, "kind": tool_input.get("kind"), "op": tool_input.get("op"),
            "value": self._sanitize(raw_value, 120) if raw_value is not None else None,
            "values": values,
            "format": raw_format,
            "pattern": self._sanitize(raw_pattern, 200) if raw_pattern is not None else None,
            "pass_examples": pass_examples,
            "fail_examples": fail_examples,
            "save_format_as": self._sanitize(raw_save_format_as, 60) if raw_save_format_as is not None else None,
            "format_name": format_name,
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
        self._state.rule_impacts[rule.rule_id] = await self._backend.simulate_rule(
            self._session, draft, window_days=self._config.max_aggregate_window_days
        )
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

    # -- 추천 (읽기 전용, 판정 없음) -----------------------------------------------------

    async def _find_apis_with_param(self, tool_input: dict[str, Any]) -> ToolOutcome:
        param = self._sanitize(tool_input.get("param"), 80)
        page = await self._backend.find_apis_with_param(self._session, param)
        for api in page.items:
            self._state.remember_api(api)
        if not page.items:
            return self._fenced({"note": "No APIs declare that parameter without an already-applied format rule for it."})
        return self._fenced({
            "items": [api_record(a) for a in page.items], "total": page.total, "next_cursor": page.next_cursor,
        })

    async def _recommend_rules_for_api(self, tool_input: dict[str, Any]) -> ToolOutcome:
        api_id = str(tool_input.get("api_id", ""))
        api = await self._backend.get_api(self._session, api_id)
        if api is None:
            return ToolOutcome.error("No API with that id.")
        self._state.remember_api(api)
        # max_group_items: the existing cap on how many grouped items a card shows at once
        # (present_run_groups' schema maxItems) -- reused here as the recommendation cap
        # rather than inventing a new config field for a second read-only list.
        recommendations = await self._backend.recommend_rules_for_api(
            self._session, api_id, limit=self._config.max_group_items
        )
        return self._fenced(
            [rule_recommendation_record(r) for r in recommendations] if recommendations
            else {"note": "No rule-less parameter of this API has a peer with an applied rule to suggest."}
        )

    # -- 포맷 배치 (bulk seed, deduped, one host approval) ------------------------------
    # Task 6 owns the tool schema and the format_batch preview card (present_format_batch /
    # enrich_format_batch); these handlers only stage/apply/discard through the backend and
    # remember the batch in session state so a later turn (or Task 6's card) can read it.

    async def _stage_format_batch(self, tool_input: dict[str, Any]) -> ToolOutcome:
        formats: list[dict[str, Any]] = []
        for item in (_coerce_list(tool_input.get("formats")) or []):
            if not isinstance(item, dict):
                continue
            formats.append({
                "name": self._sanitize(item.get("name"), 60),
                "pattern": self._sanitize(item.get("pattern"), 200),
                "pass_examples": [self._sanitize(v, 120) for v in (_coerce_list(item.get("pass_examples")) or [])],
                "fail_examples": [self._sanitize(v, 120) for v in (_coerce_list(item.get("fail_examples")) or [])],
            })
        raw_summary = tool_input.get("summary")
        draft = parse_argument(FormatBatchDraft, {
            "formats": formats,
            "summary": self._sanitize(raw_summary, 200) if raw_summary is not None else None,
        })
        # Mirrors _stage_job/_stage_rule: the guardrail runs here, before the backend call, so
        # a permissive backend is never handed a batch this deployment's cap rejects.
        if violations := check_format_batch_guardrails(draft, self._config):
            return ToolOutcome.held(GUARDRAIL_GATE, _format_batch_guardrail_message(violations))
        batch = await self._backend.stage_format_batch(self._session, draft, ActorKind.AGENT)
        self._state.remember_format_batch(batch)
        return self._fenced({"staged": format_batch_record(batch), "note": (
            "Staged only — outcomes (new/duplicate/invalid) are computed above. Apply it only "
            "after the operator approves it on the Formats page; a library format changes no "
            "run until an approved rule references it."
        )})

    async def _get_pending_format_batches(self, _: dict[str, Any]) -> ToolOutcome:
        pending = await self._backend.get_pending_format_batches(self._session)
        for batch in pending:
            self._state.remember_format_batch(batch)
        return self._fenced([format_batch_record(b) for b in pending] or {"note": "Nothing is waiting for approval."})

    async def _apply_format_batch(self, tool_input: dict[str, Any]) -> ToolOutcome:
        batch_id = str(tool_input.get("batch_id", ""))
        if held := check_apply_format_batch(self._state, self._config, batch_id):
            return held
        applied = await self._backend.apply_format_batch(self._session, batch_id)
        self._state.remember_format_batch(applied)
        return ToolOutcome(_applied_format_batch_confirmation(applied, self._session.operator),
                           [AgentEvent.change_update(format_batch_record(applied))])

    async def _discard_format_batch(self, tool_input: dict[str, Any]) -> ToolOutcome:
        batch_id = str(tool_input.get("batch_id", ""))
        if held := check_discard_format_batch(self._state, batch_id):
            return held
        discarded = await self._backend.discard_format_batch(
            self._session, batch_id, take_format_batch_discard_actor_kind(self._state, batch_id)
        )
        self._state.remember_format_batch(discarded)
        return ToolOutcome(f"Discarded {batch_id}.", [AgentEvent.change_update(format_batch_record(discarded))])

    # -- 값 비교 프로파일 (propose → approve → apply; effective_from 이후에도 과거 판정은 안 건드림) --
    # No preview card in this task: Task 8/9 owns present_profile_preview and the profile
    # provenance = the parity job seen this session (staged/applied/discarded or listed by
    # get_pending_jobs all call remember_job), mirroring check_api_provenance's own-catalogue
    # discipline for stage_job.

    async def _stage_profile(self, tool_input: dict[str, Any]) -> ToolOutcome:
        job_id = str(tool_input.get("job_id", ""))
        if job_id not in self._state.seen_jobs:
            return ToolOutcome.held(
                PROVENANCE_GATE,
                f"job_id {job_id} was not staged, applied, or listed in this session. Call "
                "get_pending_jobs (or stage/apply it) first, then propose ignore paths for it.",
            )
        ignore_paths = [self._sanitize(p, 200) for p in (_coerce_list(tool_input.get("ignore_paths")) or [])]
        per_api_ignore_input = tool_input.get("per_api_ignore")
        if per_api_ignore_input is not None and not isinstance(per_api_ignore_input, dict):
            raise InvalidToolArgument("per_api_ignore", kind="object")
        per_api_ignore: dict[str, list[str]] = {
            self._sanitize(api_id, 80): [self._sanitize(p, 200) for p in (_coerce_list(paths) or [])]
            for api_id, paths in (per_api_ignore_input or {}).items()
        }
        draft = parse_argument(ProfileDraft, {
            "job_id": job_id,
            "ignore_paths": ignore_paths,
            "per_api_ignore": per_api_ignore,
            "summary": self._sanitize(tool_input.get("summary"), 200),
        })
        # Mirrors _stage_rule/_stage_format_batch: the guardrail runs here, before the backend
        # call, so a permissive backend is never handed a profile this deployment's cap rejects.
        if violations := check_profile_guardrails(draft, self._config):
            return ToolOutcome.held(GUARDRAIL_GATE, _profile_guardrail_message(violations))
        profile = await self._backend.stage_profile(self._session, draft, ActorKind.AGENT)
        self._state.remember_profile(profile)
        return self._fenced({"staged": profile_record(profile), "note": (
            "Staged only — approve it on the Jobs/Profiles page; applying re-diffs the stored "
            "bodies with these ignore paths, no re-run."
        )})

    async def _get_pending_profiles(self, _: dict[str, Any]) -> ToolOutcome:
        pending = await self._backend.get_pending_profiles(self._session)
        for profile in pending:
            self._state.remember_profile(profile)
        return self._fenced([profile_record(p) for p in pending] or {"note": "Nothing is waiting for approval."})

    async def _apply_profile(self, tool_input: dict[str, Any]) -> ToolOutcome:
        profile_id = str(tool_input.get("profile_id", ""))
        if held := check_apply_profile(self._state, self._config, profile_id):
            return held
        try:
            applied = await self._backend.apply_profile(self._session, profile_id)
        except ProfileGuardrailViolation as violation:
            return ToolOutcome.held(GUARDRAIL_GATE, _profile_guardrail_message(violation.violations))
        self._state.remember_profile(applied)
        return ToolOutcome(_applied_profile_confirmation(profile_id, self._session.operator),
                           [AgentEvent.change_update(profile_record(applied))])

    async def _discard_profile(self, tool_input: dict[str, Any]) -> ToolOutcome:
        profile_id = str(tool_input.get("profile_id", ""))
        if held := check_discard_profile(self._state, profile_id):
            return held
        discarded = await self._backend.discard_profile(
            self._session, profile_id, take_profile_discard_actor_kind(self._state, profile_id)
        )
        self._state.remember_profile(discarded)
        return ToolOutcome(f"Discarded {profile_id}.", [AgentEvent.change_update(profile_record(discarded))])

    async def _note_unmet_ask(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """"카탈로그로는 못 답한다"를 모델이 신고하는 유일한 경로(자가발전 spec §5). 백엔드를
        부르지 않고 카드도 내지 않는다 -- 이 턴의 스크래치에 삼중항을 남기면, 턴이 끝날 때
        호스트 훅이 ``asklog.classify_turn``으로 ``unmet`` 행 하나를 쓴다. 그 행이 §8의 승격기와
        Growth 뷰의 "미충족 질문"이 읽는 유일한 재료다.

        모델이 스스로 outcome을 정하는 것처럼 보이지만 아니다: 이 도구는 신고일 뿐이고, 행의
        outcome·intent·cluster_key는 결정론 규칙이 낸다(스테이징 도구가 같이 돌았다면 그 턴은
        ``unmet``이 아니라 ``action``이 된다)."""
        reason = str(tool_input.get("reason", ""))
        if reason not in UNMET_REASONS:
            return ToolOutcome.error(
                f"reason must be one of {', '.join(UNMET_REASONS)}; adjust and call again."
            )
        summary = self._sanitize(tool_input.get("summary"), 200)
        if not summary:
            return ToolOutcome.error("summary must say, in one line, what was asked; add it and call again.")
        wanted = self._sanitize(tool_input.get("wanted"), 200)
        self._state.turn_unmet = (reason, summary, wanted)
        return ToolOutcome(
            "기록했습니다 — 이 질문은 미충족으로 남고, 무엇이 있으면 답할 수 있는지 한 문장으로 설명하세요."
        )

    async def _recommend_ignore_paths(self, tool_input: dict[str, Any]) -> ToolOutcome:
        """Read-only: ranks the parity report's noise clusters biggest-first so the model can
        propose which paths to ignore. Deterministic — every cluster and count here is what
        the report already computed (cluster_diffs), never a fabricated path."""
        job_id = str(tool_input.get("job_id", ""))
        parity = await self._backend.get_parity_report(self._session, job_id)
        if parity is None:
            return self._fenced({"note": "No parity report for that job yet — run the parity comparison first."})
        clusters = sorted(parity.get("clusters", []), key=lambda c: -c.get("count", 0))
        return self._fenced({
            "job_id": job_id,
            "clusters": clusters,
            "note": (
                "Ignoring a cluster's paths would clear its count rows; propose the biggest "
                "cluster(s) as a profile via stage_profile."
            ),
        })
