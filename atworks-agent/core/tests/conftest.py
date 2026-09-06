from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from commerce_common.skills import Skill, SkillRegistry

from atworks_agent.aggregation import aggregate, summarize_insights
from atworks_agent.backend import AtworksBackend
from atworks_agent.config import AtworksAgentConfig
from atworks_agent.cursor import decode_cursor, encode_cursor
from atworks_agent.jobs import JobDraft, JobLedger
from atworks_agent.profiles import ProfileLedger
from atworks_agent.rules import FormatBatchLedger, FormatLibrary, RuleImpact, RuleLedger
from atworks_agent.types import (
    ActorKind,
    ApiSpec,
    ApiWatermark,
    AtworksSessionContext,
    AtworksSessionState,
    AuditEntry,
    Page,
    RuleRecommendation,
    RunResult,
    RunStatus,
    ScopeSummary,
)

T0 = datetime(2026, 9, 1, 9, tzinfo=UTC)


class InMemoryBackend(AtworksBackend):
    def __init__(self, config: AtworksAgentConfig):
        self._config = config
        self.apis = {
            "api-1": ApiSpec(api_id="api-1", method="POST", path="/v1/contracts", name="계약 생성", group="contract", updated_at=T0, has_rules=True, params=["contractNo", "amount"]),
            "api-2": ApiSpec(api_id="api-2", method="GET", path="/v1/contracts/{id}", name="계약 조회", group="contract", updated_at=T0 - timedelta(days=20), has_rules=False),
        }
        self.ledger = JobLedger(config, self.apis)
        self.rule_ledger = RuleLedger(config, self.apis)
        self.profile_ledger = ProfileLedger(config)
        self.format_library = FormatLibrary(max_size=config.max_format_library)
        self.format_batch_ledger = FormatBatchLedger(config, self.format_library)
        self.parity_reports: dict[str, dict] = {}
        self.runs = [
            RunResult(run_id="run-1", api_id="api-1", executed_at=T0, target_env="dev", status=RunStatus.FAIL, failed_rules=["amount >= 0"], http_status=200),
            RunResult(run_id="run-2", api_id="api-2", executed_at=T0 + timedelta(minutes=5), target_env="dev", status=RunStatus.ERROR, http_status=503),
            RunResult(run_id="run-3", api_id="api-1", executed_at=T0 + timedelta(minutes=9), target_env="dev", status=RunStatus.PASS, http_status=200),
        ]
        self.executed: list[str] = []

    async def search_apis(self, session, query="", group=None, updated_after=None, cursor=None, limit=20,
                          path_prefix=None):
        rows = [a for a in self.apis.values() if (query.lower() in (a.path + a.name).lower()) and (group is None or a.group == group)
                and (updated_after is None or a.updated_at >= updated_after)
                and (path_prefix is None or a.path == path_prefix or a.path.startswith(path_prefix + "/"))]
        # Real keyset paging (mirrors mock_backend.py's ``_page``): selection.scan_apis pages the
        # catalogue by cursor, so a double that always returns next_cursor=None would make every
        # scan look complete.
        ordered = sorted(rows, key=lambda a: (a.updated_at, a.api_id), reverse=True)
        remaining = ordered
        if cursor:
            after = decode_cursor(cursor)
            remaining = [a for a in ordered if (a.updated_at, a.api_id) < after]
        items = remaining[:limit]
        next_cursor = encode_cursor(items[-1].updated_at, items[-1].api_id) if len(remaining) > limit else None
        return Page(items=items, next_cursor=next_cursor, total=len(ordered))

    async def get_api(self, session, api_id):
        return self.apis.get(api_id)

    async def get_apis(self, session, api_ids):
        return [self.apis[i] for i in api_ids if i in self.apis]

    async def list_runs(self, session, q):
        rows = [r for r in self.runs if (q.since is None or r.executed_at >= q.since)
                and (q.until is None or r.executed_at < q.until)
                and (q.status is None or (q.status == "non_pass" and r.status.value != "pass") or r.status.value == q.status)
                and (q.api_id is None or r.api_id == q.api_id)
                and (q.executed_by is None or r.executed_by == q.executed_by)
                and (q.job_id is None or r.job_id == q.job_id)]
        # Real keyset pagination (mirrors mock_backend.py's ``_page``): a caller collecting
        # more than one page (aggregate_runs' collect_runs) needs a real next_cursor to
        # actually advance, not the always-None stub this used to return.
        ordered = sorted(rows, key=lambda r: (r.executed_at, r.run_id), reverse=True)
        total = len(ordered)
        remaining = ordered
        if q.cursor:
            after = decode_cursor(q.cursor)
            remaining = [r for r in ordered if (r.executed_at, r.run_id) < after]
        items = remaining[: q.limit]
        next_cursor = encode_cursor(items[-1].executed_at, items[-1].run_id) if len(remaining) > q.limit else None
        return Page(items=items, next_cursor=next_cursor, total=total)

    async def get_run(self, session, run_id):
        return next((r for r in self.runs if r.run_id == run_id), None)

    async def count_runs(self, session, since=None, until=None, status=None, api_id=None):
        return sum(
            1 for r in self.runs
            if (since is None or r.executed_at >= since)
            and (until is None or r.executed_at < until)
            and (status is None or (status == "non_pass" and r.status.value != "pass") or r.status.value == status)
            and (api_id is None or r.api_id == api_id)
        )

    def _scope_since(self):
        return datetime.now(UTC) - timedelta(days=self._config.scope_window_days)

    def _operator_scope_runs(self, scope_operator, since):
        """The double's stand-in for the backend-side operator scope: the APIs this operator
        executed at or after ``since`` (the Mock resolves the same set from ``operator_api``)."""
        if scope_operator is None:
            return None
        return {r.api_id for r in self.runs
                if r.executed_by == scope_operator and (since is None or r.executed_at >= since)}

    async def aggregate_runs(self, session, q):
        # Task 8: the executor now asks the BACKEND to group (the Mock does it over rollups).
        # This double has no rollups, so it groups its own run list with the same pure function
        # the oracle uses -- same output shape, same ordering contract.
        scope = self._operator_scope_runs(q.scope_operator, q.since)
        rows = [r for r in self.runs
                if (q.since is None or r.executed_at >= q.since)
                and (q.until is None or r.executed_at < q.until)
                and (q.scope_api_ids is None or r.api_id in q.scope_api_ids)
                and (scope is None or r.api_id in scope)
                and (q.status is None or (q.status == "non_pass" and r.status.value != "pass")
                     or r.status.value == q.status)]
        groups = aggregate(rows, self.apis, q.group_by,
                           flaky_min_transitions=self._config.flaky_min_transitions)
        if q.order_by == "transitions":
            # `aggregate` returns the "failures" order; re-rank for the other one (Task 8 fix
            # round 2). The cut is the ONLY thing order_by moves, so it happens before [:limit].
            groups.sort(key=lambda g: (-g.transitions, -(g.fail + g.error), g.key))
        groups = groups[: q.limit]
        if not q.include_run_ids:
            for g in groups:
                g.run_ids = []
        return groups

    async def summarize_insights(self, session, since, until=None, scope_operator=None):
        scope = self._operator_scope_runs(scope_operator, since)
        rows = [r for r in self.runs
                if r.executed_at >= since and (until is None or r.executed_at < until)
                and (scope is None or r.api_id in scope)]
        return summarize_insights(rows, self.apis, self._config)

    async def count_runs_by_job(self, session, since=None, until=None):
        counts: dict[str, int] = {}
        for r in self.runs:
            if r.job_id is None:
                continue
            if (since is None or r.executed_at >= since) and (until is None or r.executed_at < until):
                counts[r.job_id] = counts.get(r.job_id, 0) + 1
        return counts

    async def current_state(self, session, scope_api_ids=None, scope_operator=None):
        return []

    async def watermarks(self, session, api_ids=None, first_non_pass_since=None, last_non_pass_since=None,
                         scope_operator=None):
        # Task 8: selection.resolve_select_where(failed_since) and the insight panel read this,
        # so the double computes real watermarks over its run list (the Mock materializes them).
        marks: list[ApiWatermark] = []
        scope = self._operator_scope_runs(scope_operator, self._scope_since())
        for api_id in sorted({r.api_id for r in self.runs}):
            if api_ids is not None and api_id not in api_ids:
                continue
            if scope is not None and api_id not in scope:
                continue
            rows = sorted((r for r in self.runs if r.api_id == api_id), key=lambda r: (r.executed_at, r.run_id))
            passes = [r.executed_at for r in rows if r.status.value == "pass"]
            non_pass = [r.executed_at for r in rows if r.status.value != "pass"]
            first_np = non_pass[0] if non_pass else None
            last_np = non_pass[-1] if non_pass else None
            if first_non_pass_since is not None and (first_np is None or first_np < first_non_pass_since):
                continue
            if last_non_pass_since is not None and (last_np is None or last_np < last_non_pass_since):
                continue
            api = self.apis.get(api_id)
            marks.append(ApiWatermark(
                api_id=api_id, last_pass_at=passes[-1] if passes else None, first_non_pass_at=first_np,
                last_non_pass_at=last_np, latest_status=rows[-1].status,
                api_updated_at=api.updated_at if api is not None else None,
            ))
        return marks

    async def operator_scope(self, session, operator_id, window_days):
        return ScopeSummary()

    async def get_body(self, session, run_id):
        run = next((r for r in self.runs if r.run_id == run_id), None)
        return run.response_body if run is not None else None

    async def get_body_masked_paths(self, session, run_id):
        # This double never masks anything on the way in, so nothing was rewritten.
        del session, run_id
        return []

    async def audit(self, session, cursor=None, limit=50):
        return Page[AuditEntry]()

    async def append_audit(self, session, action, target_kind, target_id):
        return None

    async def stage_job(self, session, draft: JobDraft, actor_kind: ActorKind):
        return self.ledger.stage(draft, actor=session.operator, actor_kind=actor_kind)

    async def get_pending_jobs(self, session):
        return self.ledger.pending()

    async def apply_job(self, session, job_id):
        return self.ledger.apply(job_id, actor=session.operator)

    async def discard_job(self, session, job_id, actor_kind):
        return self.ledger.discard(job_id, actor=session.operator, actor_kind=actor_kind)

    async def get_job(self, session, job_id):
        return self.ledger.get(job_id)

    async def applied_jobs(self, session):
        return self.ledger.applied()

    async def all_jobs(self, session, status=None, cursor=None, limit=50):
        rows = [*self.ledger.pending(), *self.ledger.applied()]
        if status is not None:
            rows = [j for j in rows if j.status.value == status]
        return Page(items=rows[:limit], total=len(rows))

    async def active_jobs(self, session):
        return [j for j in self.ledger.applied() if j.remaining_executions > 0]

    async def runs_by_ids(self, session, run_ids):
        return [r for r in self.runs if r.run_id in run_ids]

    async def record_execution(self, session, job_id, run_ids, schedule_index):
        return self.ledger.record_execution(job_id, run_ids, schedule_index)

    async def add_guardrail_note(self, session, job_id, note):
        return self.ledger.add_guardrail_note(job_id, note)

    async def stage_rule(self, session, draft, actor_kind):
        return self.rule_ledger.stage(draft, actor=session.operator, actor_kind=actor_kind)

    async def get_pending_rules(self, session):
        return self.rule_ledger.pending()

    async def apply_rule(self, session, rule_id):
        return self.rule_ledger.apply(rule_id, actor=session.operator)

    async def discard_rule(self, session, rule_id, actor_kind):
        return self.rule_ledger.discard(rule_id, actor=session.operator, actor_kind=actor_kind)

    async def get_rule(self, session, rule_id):
        return self.rule_ledger.get(rule_id)

    async def list_rules(self, session, api_id=None, status=None, cursor=None, limit=50):
        rows = self.rule_ledger.list(api_id=api_id)
        if status is not None:
            rows = [r for r in rows if r.status.value == status]
        return Page(items=rows[:limit], total=len(rows))

    async def simulate_rule(self, session, draft, window_days):
        return RuleImpact()

    async def stage_profile(self, session, draft, actor_kind):
        return self.profile_ledger.stage(draft, actor=session.operator, actor_kind=actor_kind)

    async def get_pending_profiles(self, session):
        return self.profile_ledger.pending()

    async def apply_profile(self, session, profile_id):
        return self.profile_ledger.apply(profile_id, actor=session.operator)

    async def discard_profile(self, session, profile_id, actor_kind):
        return self.profile_ledger.discard(profile_id, actor=session.operator, actor_kind=actor_kind)

    async def get_profile(self, session, profile_id):
        return self.profile_ledger.get(profile_id)

    async def list_profiles(self, session, job_id=None, status=None, cursor=None, limit=50):
        rows = self.profile_ledger.list(job_id=job_id)
        if status is not None:
            rows = [p for p in rows if p.status.value == status]
        return Page(items=rows[:limit], total=len(rows))

    async def get_parity_report(self, session, job_id):
        return self.parity_reports.get(job_id)

    async def find_apis_with_param(self, session, param, cursor=None, limit=20):
        applied_format_apis = {r.api_id for r in self.rule_ledger.applied() if r.kind == "format" and r.param == param}
        rows = [a for a in self.apis.values() if param in a.params and a.api_id not in applied_format_apis]
        return Page(items=rows[:limit], total=len(rows))

    async def recommend_rules_for_api(self, session, api_id, limit=20):
        api = self.apis.get(api_id)
        if api is None:
            return []
        applied = self.rule_ledger.applied()
        already_ruled_params = {r.param for r in applied if r.api_id == api_id}
        recommendations = []
        for param in api.params:
            if param in already_ruled_params:
                continue
            for peer_rule in applied:
                if peer_rule.api_id != api_id and peer_rule.param == param:
                    recommendations.append(RuleRecommendation(param=param, from_api_id=peer_rule.api_id, rule=peer_rule))
        return recommendations[:limit]

    async def get_format(self, session, name):
        return self.format_library.get(name)

    async def list_formats(self, session):
        return self.format_library.list()

    async def save_format(self, session, defn):
        return self.format_library.add(defn)

    async def stage_format_batch(self, session, draft, actor_kind):
        return self.format_batch_ledger.stage(draft, actor=session.operator, actor_kind=actor_kind)

    async def get_pending_format_batches(self, session):
        return self.format_batch_ledger.pending()

    async def get_format_batch(self, session, batch_id):
        return self.format_batch_ledger.get(batch_id)

    async def apply_format_batch(self, session, batch_id):
        return self.format_batch_ledger.apply(batch_id, actor=session.operator)

    async def discard_format_batch(self, session, batch_id, actor_kind):
        return self.format_batch_ledger.discard(batch_id, actor=session.operator, actor_kind=actor_kind)

    async def execute_job_once(self, session, job_id, schedule_index=None):
        self.executed.append(job_id)
        return []

    async def get_context(self, session):
        return {"project": "MES", "allowed_targets": ["dev", "stg"]}

    async def list_operators(self, session):
        return []


@pytest.fixture
def config():
    return AtworksAgentConfig(model="m")


@pytest.fixture
def backend(config):
    return InMemoryBackend(config)


@pytest.fixture
def skills():
    return SkillRegistry([Skill(name="failed-triage", description="실패 triage", body="Body.")])


@pytest.fixture
def session():
    return AtworksSessionContext(session_id="s-1", project_id="mes", operator="minseong")


@pytest.fixture
def state():
    return AtworksSessionState()
