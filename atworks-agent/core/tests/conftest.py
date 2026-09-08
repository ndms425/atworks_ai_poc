from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import product

import pytest
from commerce_common.memory import MemoryWriteRejected, validate_fact, write_filter_for
from commerce_common.skills import Skill, SkillRegistry

from atworks_agent.aggregation import aggregate, summarize_insights
from atworks_agent.backend import AtworksBackend
from atworks_agent.config import AtworksAgentConfig
from atworks_agent.cursor import decode_cursor, encode_cursor
from atworks_agent.fencing import ATWORKS_FENCE
from atworks_agent.jobs import JobDraft, JobLedger
from atworks_agent.profiles import ProfileLedger
from atworks_agent.rules import FormatBatchLedger, FormatLibrary, RuleImpact, RuleLedger
from atworks_agent.types import (
    ActorKind,
    AliasProposal,
    ApiSpec,
    ApiWatermark,
    AskEntry,
    AtworksSessionContext,
    AtworksSessionState,
    AuditEntry,
    GrowthSummary,
    Page,
    QueryResult,
    QueryRow,
    RuleRecommendation,
    RunResult,
    RunStatus,
    SavedQuestion,
    ScopeSummary,
    VocabularyEntry,
)
from atworks_agent.vocabulary import (
    entry_to_fact,
    normalize_term,
    rejected_fragment_fields,
)

T0 = datetime(2026, 9, 1, 9, tzinfo=UTC)


def _double_api_matches(api, filters) -> bool:
    """The API-catalogue half of QueryFilters, for the double's own run list."""
    if api is None:
        return not (filters.path_contains or filters.path_prefix or filters.method or filters.api_group)
    path = api.path.lower()
    return (
        (filters.path_contains is None or any(t.lower() in path for t in filters.path_contains))
        and (filters.path_prefix is None or api.path.startswith(filters.path_prefix))
        and (filters.method is None or api.method in filters.method)
        and (filters.api_group is None or api.group in filters.api_group)
    )


def _double_keys(run, api, dimension: str) -> list:
    """Every group key ONE run contributes on one dimension -- a list, not a value: a run that
    failed three rules belongs to three ``failed_rule`` groups (the fan-out the key-axis rollup
    materializes), and a run with no failed rule, no status or no operator belongs to none."""
    segments = api.path.strip("/").split("/") if api is not None else []
    if dimension == "api":
        return [run.api_id]
    if dimension == "path_segment_1":
        return [segments[0]] if segments else [None]
    if dimension == "path_segment_2":
        return [segments[1]] if len(segments) >= 2 else [None]
    if dimension == "path_segment_3":
        return [segments[2]] if len(segments) >= 3 else [None]
    if dimension == "path_prefix_2":
        return ["/".join(segments[:2])] if segments else [None]
    if dimension == "method":
        return [api.method if api is not None else None]
    if dimension == "api_group":
        return [api.group if api is not None else None]
    if dimension == "target_env":
        return [run.target_env]
    if dimension == "test_data_label":
        return [run.test_data_label]
    if dimension == "failed_rule":
        return list(run.failed_rules)
    if dimension == "http_status":
        return [str(run.http_status)] if run.http_status is not None else []
    if dimension == "executed_by":
        return [run.executed_by] if run.executed_by else []
    if dimension == "day":
        return [run.executed_at.date().isoformat()]
    if dimension == "week":
        year, week, _ = run.executed_at.isocalendar()
        return [f"{year}-W{week:02d}"]
    # NOT `[None]`. A dimension this double does not know about would then answer "one group,
    # key None" for every run -- a plausible-looking table over a key the double never computed,
    # and a test that reads it as the truth. `path_segment_3` reached that fallback for a whole
    # task before anyone noticed. A new `Dimension` must be taught here or fail loudly.
    raise AssertionError(
        f"the double has no key rule for dimension {dimension!r} -- add one rather than letting "
        f"it group everything under None")


def _double_measures(names, group) -> dict:
    """The seven measures a plain run list can answer; the other two stay None (the ABC's rule:
    a measure this source cannot produce is NULL, never 0)."""
    total = len(group)
    counts = {s: sum(1 for r in group if r.status.value == s) for s in ("pass", "fail", "error")}
    values = {
        "runs": total, "pass": counts["pass"], "fail": counts["fail"], "error": counts["error"],
        "non_pass": counts["fail"] + counts["error"],
        "fail_rate": round((counts["fail"] + counts["error"]) / total, 4) if total else 0.0,
        "apis": len({r.api_id for r in group}),
        "transitions": None, "p95_duration_ms": None,
    }
    return {name: values[name] for name in names}


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
        self.asks: list[AskEntry] = []
        self.vocabulary: dict[str, VocabularyEntry] = {}
        self.saved_questions: dict[str, SavedQuestion] = {}

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

    async def query_runs(self, session, spec):
        """A naive in-Python stand-in for ``Store.query`` -- enough for the executor and card
        tests (grouping, totals, evidence samples), never an oracle. Measures this double cannot
        derive from a plain run list (``transitions``, ``p95_duration_ms``) come back None, which
        is exactly what the ABC says a source that cannot measure something must do. Days/weeks
        are UTC here, not ``briefing_tz``: the double has no rollups to be consistent with.
        ``compare_previous_window`` is not implemented -- a test that needs `_prev`/`_delta` sets
        ``state.last_query_result`` itself."""
        now = (session.local_now() if session is not None else None) or datetime.now(UTC)
        f = spec.filters
        if f.window_days is not None:
            since, until = now - timedelta(days=f.window_days), now
        else:
            since = f.since or now - timedelta(days=self._config.max_aggregate_window_days)
            until = f.until or now
        scope = self._operator_scope_runs(f.scope_operator, since)
        rows = [r for r in self.runs
                if since <= r.executed_at <= until
                and (f.status in (None, "all")
                     or (f.status == "non_pass" and r.status.value != "pass")
                     or r.status.value == f.status)
                and (f.api_ids is None or r.api_id in f.api_ids)
                and (scope is None or r.api_id in scope)
                and (f.target_env is None or r.target_env in f.target_env)
                and (f.test_data_label is None or r.test_data_label in f.test_data_label)
                and (f.executed_by is None or r.executed_by in f.executed_by)
                and (f.http_status is None or r.http_status in f.http_status)
                and (f.failed_rule is None or any(x in f.failed_rule for x in r.failed_rules))
                and _double_api_matches(self.apis.get(r.api_id), f)]
        buckets: dict[tuple, list] = {}
        for run in rows:
            api = self.apis.get(run.api_id)
            per_dimension = [_double_keys(run, api, d) for d in spec.dimensions]
            for key in product(*per_dimension) if spec.dimensions else [()]:
                buckets.setdefault(key, []).append(run)
        result_rows = [
            QueryRow(
                keys=dict(zip(spec.dimensions, key, strict=True)),
                measures=_double_measures(spec.measures, group),
                api_ids=(list(dict.fromkeys(r.api_id for r in group))[:20] if spec.include_samples else []),
                run_ids=([r.run_id for r in sorted(group, key=lambda r: (r.executed_at, r.run_id), reverse=True)][:5]
                         if spec.include_samples else []),
            )
            for key, group in buckets.items()
        ]
        # Ties break on the group key ascending, so the cut is deterministic: sort by key first,
        # then stable-sort by the ranked measure (the same two-level order the SQL states).
        result_rows.sort(key=lambda r: tuple("" if v is None else str(v) for v in r.keys.values()),
                         reverse=spec.order_by == "key" and spec.descending)
        if spec.order_by != "key":
            result_rows.sort(key=lambda r: r.measures.get(spec.order_by) or 0, reverse=spec.descending)
        return QueryResult(
            spec=spec, rows=result_rows[: spec.limit], total_groups=len(result_rows),
            population=len(rows), window=(since, until), source="runs",
        )

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

    # -- ask_log (self-growth spec §6). An in-memory list, idempotent on turn_id like the Store.
    async def record_ask(self, session, entry):
        if any(e.turn_id == entry.turn_id for e in self.asks):
            return next(e for e in self.asks if e.turn_id == entry.turn_id)
        stored = entry.model_copy(update={"seq": len(self.asks) + 1})
        self.asks.append(stored)
        return stored

    async def list_asks(self, session, outcome=None, cursor=None, limit=50):
        items = [e for e in self.asks if outcome is None or e.outcome == outcome]
        items = sorted(items, key=lambda e: (e.at, e.seq or 0), reverse=True)
        return Page[AskEntry](items=items[:limit], next_cursor=None, total=len(items))

    async def get_ask(self, session, turn_id):
        del session
        return next((e for e in self.asks if e.turn_id == turn_id), None)

    async def set_feedback(self, session, turn_id, vote):
        # 덮어쓴다(같은 턴 재투표), 없으면 None -- Store와 같은 계약.
        del session
        for index, entry in enumerate(self.asks):
            if entry.turn_id == turn_id:
                self.asks[index] = entry.model_copy(update={"feedback": vote})
                return self.asks[index]
        return None

    async def growth_summary(self, session, since):
        window = [e for e in self.asks if e.at >= since]
        return GrowthSummary(
            asks_total=len(window),
            answered=sum(1 for e in window if e.outcome == "answered"),
            partial=sum(1 for e in window if e.outcome == "partial"),
            unmet=sum(1 for e in window if e.outcome == "unmet"),
            action=sum(1 for e in window if e.outcome == "action"),
            up=sum(1 for e in window if e.feedback == "up"),
            down=sum(1 for e in window if e.feedback == "down"),
            # 군집 총계는 목록의 길이가 아니라 서로 다른 cluster_key의 수다 -- 이 더블도 그
            # 계약을 지킨다(목록 길이로 답하면 화면의 "상위 N / 총 M"이 늘 N == M이 된다).
            unmet_clusters_total=len({e.cluster_key for e in window if e.outcome == "unmet"}),
        )

    # -- 어휘 (self-growth §7). In-memory dicts: the same three gates as the Mock (normalize,
    # write filter, cooldown) but no SQL -- these doubles exist so a core/runtime test can drive
    # propose/confirm/inject without a host store.
    async def propose_alias(self, session, term, fragment, note=None):
        del note
        normalized = normalize_term(term)
        if not normalized or rejected_fragment_fields(fragment):
            return AliasProposal(outcome="refused")
        now = (session.local_now() if session is not None else None) or datetime.now(UTC)
        existing = self.vocabulary.get(normalized)
        cooled = (existing is not None and existing.status == "rejected"
                  and (existing.cooldown_until is None or existing.cooldown_until <= now))
        if existing is not None and not cooled:
            # 이미 있는 행은 덮지 않고 그대로 돌려준다 -- 재제안은 새 제안이 아니다.
            return AliasProposal(outcome="existing", entry=existing)
        entry = VocabularyEntry(
            term=normalized, fragment=fragment, status="pending",
            proposed_by=session.operator if session is not None else "", proposed_at=now,
            confirmations=existing.confirmations if existing else 0,
            uses=existing.uses if existing else 0,
            rejections=existing.rejections if existing else 0,
        )
        fact = entry_to_fact(entry)
        try:
            validate_fact(fact.key, fact.value, fact.category.value, fence=ATWORKS_FENCE,
                          write_filter=write_filter_for(()))
        except MemoryWriteRejected:
            return AliasProposal(outcome="refused")
        self.vocabulary[normalized] = entry
        return AliasProposal(outcome="proposed", entry=entry)

    async def list_vocabulary(self, session, status=None, cursor=None, limit=50):
        items = [e for e in self.vocabulary.values() if status is None or e.status == status]
        items = sorted(items, key=lambda e: (e.proposed_at, e.term), reverse=True)
        return Page[VocabularyEntry](items=items[:limit], next_cursor=None, total=len(items))

    def _set_vocabulary(self, session, term, status, **fields):
        entry = self.vocabulary.get(normalize_term(term))
        if entry is None:
            return None
        updated = entry.model_copy(update={"status": status, **fields})
        self.vocabulary[updated.term] = updated
        return updated

    async def confirm_alias(self, session, term):
        now = (session.local_now() if session is not None else None) or datetime.now(UTC)
        entry = self.vocabulary.get(normalize_term(term))
        if entry is None:
            return None
        return self._set_vocabulary(
            session, term, "confirmed", confirmations=entry.confirmations + 1,
            confirmed_by=session.operator if session is not None else "", confirmed_at=now,
            cooldown_until=None,
        )

    async def reject_alias(self, session, term):
        now = (session.local_now() if session is not None else None) or datetime.now(UTC)
        entry = self.vocabulary.get(normalize_term(term))
        if entry is None:
            return None
        return self._set_vocabulary(
            session, term, "rejected", rejections=entry.rejections + 1,
            cooldown_until=now + timedelta(days=30),
        )

    async def delete_alias(self, session, term):
        return self.vocabulary.pop(normalize_term(term), None)

    async def confirmed_vocabulary(self, session):
        return [e for e in self.vocabulary.values() if e.status == "confirmed"]

    async def note_vocabulary_use(self, session, terms, rejected=False):
        demoted: list[str] = []
        for term in terms:
            entry = self.vocabulary.get(normalize_term(term))
            if entry is None:
                continue
            if rejected:
                entry = entry.model_copy(update={"rejections": entry.rejections + 1})
                if entry.status == "confirmed" and entry.rejections >= 3 and entry.rejections > entry.confirmations:
                    entry = entry.model_copy(update={"status": "pending"})
                    demoted.append(entry.term)
            else:
                entry = entry.model_copy(update={"uses": entry.uses + 1})
            self.vocabulary[entry.term] = entry
        # 실제로 강등된 term만 (ABC 계약): 라우트가 이것만 감사 2행으로 남긴다.
        return demoted

    # -- 저장 질문 (self-growth §8). An in-memory dict: the promoter itself is a host job, so what
    # these doubles owe the ABC is the READ side -- list in `uses` order, run the stored spec
    # through this double's own `query_runs`, hide/unhide.
    async def list_saved_questions(self, session, status=None, cursor=None, limit=50):
        del session, cursor
        items = [q for q in self.saved_questions.values() if status is None or q.status == status]
        items = sorted(items, key=lambda q: (q.uses, q.created_at, q.id), reverse=True)
        return Page[SavedQuestion](items=items[:limit], next_cursor=None, total=len(items))

    async def run_saved_question(self, session, saved_id):
        saved = self.saved_questions.get(saved_id)
        if saved is None or saved.status != "active":
            return None
        result = await self.query_runs(session, saved.spec)
        now = (session.local_now() if session is not None else None) or datetime.now(UTC)
        self.saved_questions[saved_id] = saved.model_copy(
            update={"uses": saved.uses + 1, "last_used_at": now})
        return result

    async def set_saved_question_status(self, session, saved_id, status):
        del session
        saved = self.saved_questions.get(saved_id)
        if saved is None:
            return None
        self.saved_questions[saved_id] = saved.model_copy(update={"status": status})
        return self.saved_questions[saved_id]

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
