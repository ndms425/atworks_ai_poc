"""MockAtworks: 픽스처 위의 AtworksBackend. 판정은 결정론 스텁 — 실제 aTworks에선 DSL 엔진이
낸다. Java 통합 시 이 파일과 같은 인터페이스로 rest_backend.py를 쓴다."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from commerce_common.memory import (
    MemoryWriteRejected,
    check_memory_store,
    validate_fact,
    write_filter_for,
)
from commerce_common.turn import session_tag

from atworks_agent import (
    ActorKind,
    AggregateQuery,
    AliasProposal,
    ApiSpec,
    ApiWatermark,
    AskEntry,
    AtworksAgentConfig,
    AtworksBackend,
    AuditEntry,
    CellState,
    ComparisonProfile,
    FormatBatch,
    FormatBatchDraft,
    FormatBatchLedger,
    FormatDefinition,
    FormatLibrary,
    GrowthSummary,
    Insights,
    JobDraft,
    JobLedger,
    JobSpec,
    OperatorProfile,
    Page,
    ProfileDraft,
    ProfileLedger,
    QueryResult,
    QuerySpec,
    RuleDraft,
    RuleImpact,
    RuleLedger,
    RuleRecommendation,
    RunResult,
    RunsQuery,
    RunStatus,
    SavedQuestion,
    ScopeSummary,
    SelectWhere,
    TestDataSet,
    ValidationRule,
    VocabularyEntry,
    body_capture_enabled,
    decode_cursor,
    encode_cursor,
    enforce_execution_matrix,
    evaluate,
    mask_body_paths,
    policy_from_config,
    resolve_select_where,
)
from atworks_agent.fencing import ATWORKS_FENCE
from atworks_agent.types import Binding, QueryFilters
from atworks_agent.vocabulary import (
    entry_to_fact,
    normalize_term,
    rejected_fragment_fields,
)

from .memory_store import SqliteMemoryStore
from .reports import Reports
from .store import Store

logger = logging.getLogger(__name__)


def _page(rows: list, cursor: str | None, limit: int, *, key) -> Page:
    """Generic keyset page over an already status/filter-narrowed list: sorts newest-first by
    ``key`` (must return a ``(datetime, str)`` tuple matching the cursor shape), slices after the
    decoded cursor position, and reports ``total`` as the filtered count before that slice."""
    ordered = sorted(rows, key=key, reverse=True)
    total = len(ordered)
    if cursor:
        after = decode_cursor(cursor)
        ordered = [row for row in ordered if key(row) < after]
    items = ordered[:limit]
    next_cursor = encode_cursor(*key(items[-1])) if len(ordered) > limit else None
    return Page(items=items, next_cursor=next_cursor, total=total)


def stub_verdict(api: ApiSpec, env: str, data: TestDataSet | None) -> tuple[RunStatus, list[str], int]:
    """결정론 스텁 "DSL". 실제 aTworks에선 규칙 엔진이 내는 판정을 대신한다. 규칙은 위에서 아래로,
    처음 맞는 하나가 판정이다:

    1. 바인딩된 키 중 이름에 ``amount``/``Amount``가 들어가고 값이 음수로 파싱되면 그 규칙 FAIL(200).
    2. 그 외, 경로에 ``refund``가 있고 바인딩이 없으면 ``refundAmount`` 규칙 FAIL(200) — 기존
       픽스처와 테스트가 계속 의미를 갖도록 남긴 오늘의 규칙.
    3. 그 외, ``api-007``을 ``stg``에서 부르면 ERROR(503) — dev/stg 비교가 차이를 보이도록. dev에선
       통과한다.
    4. 그 외 PASS(200).
    """
    if data is not None:
        for key, value in data.values.items():
            if "amount" in key or "Amount" in key:
                try:
                    number = float(value)
                except ValueError:
                    continue
                if number < 0:
                    return RunStatus.FAIL, [f"{key} >= 0"], 200
    if "refund" in api.path and data is None:
        return RunStatus.FAIL, ["refundAmount >= 0"], 200
    if api.api_id == "api-007" and env == "stg":
        return RunStatus.ERROR, [], 503
    return RunStatus.PASS, [], 200


def stub_response(api: ApiSpec, env: str, data: TestDataSet | None, seq: int) -> dict:
    """Deterministic per-target response body for parity demos. A volatile field (serverTime)
    differs every call (noise); a `_renewed`-only value difference on some APIs is a REAL diff."""
    body: dict[str, Any] = {
        "path": api.path, "serverTime": f"2026-09-05T00:00:{seq % 60:02d}",  # volatile → noise
        "echo": {k: v for k, v in (data.values.items() if data else [])},
    }
    # a real, deterministic value difference on the renewed server for one payment API:
    if api.api_id == "api-004":
        body["limit"] = 1000 if env != "renewed" else 900   # renewed changed the value → real diff
    # Task 6 (scale spec §7): a PII-shaped sample so masking-at-capture has something real to
    # catch. Neither field depends on env/seq, so no existing parity/stub_response test that
    # asserts "everything but serverTime matches" breaks -- api-001 carries no field-by-field
    # diff_paths assertion anywhere in the suite (unlike api-002/api-004/api-008).
    if api.api_id == "api-001":
        body["contact"] = "hong@example.com"
        body["ssn"] = "900101-1234567"
    return body


class _RunsView:
    """Dict-compat facade over ``Store``'s ``runs`` table. ``MockAtworks.runs`` is a *property*
    returning this, not a plain dict -- scale spec 2026-09-06 Task 4 removes the in-memory run
    dict entirely, runs live only in SQL. This view exists purely so the pre-existing test suite's
    ``backend.runs[...]`` / ``.values()`` / ``.items()`` / ``.keys()`` / ``len(...)`` / membership
    checks, and wholesale ``backend.runs = {...}`` reassignment (see the property's setter below)
    keep working unmodified: every access here goes straight to the Store, nothing is cached."""

    def __init__(self, store: Store, briefing_tz: str) -> None:
        self._store = store
        self._briefing_tz = briefing_tz

    def __getitem__(self, run_id: str) -> RunResult:
        run = self._store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def __setitem__(self, run_id: str, run: RunResult) -> None:
        self._store.upsert_run(run, self._briefing_tz)

    def __contains__(self, run_id: object) -> bool:
        return isinstance(run_id, str) and self._store.get_run(run_id) is not None

    def __iter__(self):
        return iter(self._store.run_ids())

    def __len__(self) -> int:
        return self._store.run_count()

    def get(self, run_id: str, default: RunResult | None = None) -> RunResult | None:
        run = self._store.get_run(run_id)
        return run if run is not None else default

    def keys(self) -> list[str]:
        return self._store.run_ids()

    def values(self) -> list[RunResult]:
        return self._store.all_runs()

    def items(self) -> list[tuple[str, RunResult]]:
        return [(r.run_id, r) for r in self._store.all_runs()]


class _ApisDict(dict):
    """A real ``dict`` (O(1) reads -- JobLedger/RuleLedger hold this exact object as their
    in-memory api index, scale spec 2026-09-06 Task 4's controller ruling: ``self.apis`` stays a
    plain dict, unlike ``self.runs``) that also mirrors every point write into the Store's
    ``apis`` table, so SQL-backed ``search_apis`` stays in sync with in-place mutation (tests
    redate fixture apis via ``backend.apis[api_id] = api.model_copy(...)`` and then exercise
    ``search_apis`` indirectly through it)."""

    def __init__(self, store: Store, initial: dict[str, ApiSpec] | None = None) -> None:
        super().__init__(initial or {})
        self._store = store

    def __setitem__(self, key: str, value: ApiSpec) -> None:
        super().__setitem__(key, value)
        self._store.load_apis([value])


class MockAtworks(AtworksBackend):
    def __init__(self, config: AtworksAgentConfig, fixtures_dir: Path, store: Store | None = None):
        self._config = config
        self.store = store if store is not None else Store(":memory:")
        self.store.load_fixtures(fixtures_dir, briefing_tz=config.briefing_tz)
        self.apis = {
            row["api_id"]: ApiSpec(**row) for row in json.loads((fixtures_dir / "apis.json").read_text(encoding="utf-8"))
        }
        self.ledger = JobLedger(config, self.apis)
        self.rule_ledger = RuleLedger(config, self.apis)
        self.profile_ledger = ProfileLedger(config)
        self.reports: Reports | None = None
        self.format_library = FormatLibrary(max_size=config.max_format_library)
        self.format_batch_ledger = FormatBatchLedger(config, self.format_library)
        self._runs_view = _RunsView(self.store, config.briefing_tz)
        # NOT run_count() (M17): that counts only the HOT partition, so once retention
        # moved a day into `runs_archive` the counter jumped backwards and the next
        # execution re-issued ids that already exist there -- which `INSERT OR IGNORE`
        # then silently dropped as duplicates.
        self._run_seq = self.store.max_run_seq()
        self.operators: dict[str, OperatorProfile] = {
            row["operator_id"]: OperatorProfile(**row)
            for row in json.loads((fixtures_dir / "operators.json").read_text(encoding="utf-8"))
        }
        # The org vocabulary's durable half (self-growth §7): the commerce_common MemoryStore
        # contract over this same SQLite file. `check_memory_store` runs here rather than at first
        # use, so a store missing part of the contract fails at construction.
        self.memory_store = check_memory_store(SqliteMemoryStore(self.store))
        self._memory_filter = write_filter_for(config.memory_blocked_patterns)
        # Process cache for `confirmed_vocabulary` -- read once per turn by /chat, invalidated by
        # EVERY write below (propose/confirm/reject/delete/use). None = not loaded.
        self._confirmed_cache: list[VocabularyEntry] | None = None

    @property
    def apis(self) -> _ApisDict:
        return self._apis

    @apis.setter
    def apis(self, value: dict[str, ApiSpec]) -> None:
        self._apis = _ApisDict(self.store, value)
        self.store.replace_all_apis(value.values())

    @property
    def runs(self) -> _RunsView:
        return self._runs_view

    @runs.setter
    def runs(self, value: dict[str, RunResult]) -> None:
        # Wholesale replacement (a handful of tests build a purpose-built run set from scratch,
        # discarding the fixtures entirely) -- deletes every existing run/body row then reinserts.
        self.store.replace_all_runs(value, self._config.briefing_tz)

    def operator_profile(self, operator_id: str) -> OperatorProfile | None:
        return self.operators.get(operator_id)

    async def list_operators(self, session) -> list[OperatorProfile]:
        return list(self.operators.values())

    async def search_apis(self, session, query="", group=None, updated_after=None, cursor=None, limit=20,
                          path_prefix=None):
        return self.store.search_apis(query=query, group=group, updated_after=updated_after, cursor=cursor,
                                      limit=limit, path_prefix=path_prefix)

    async def get_api(self, session, api_id):
        return self.apis.get(api_id)

    async def get_apis(self, session, api_ids):
        del session
        return self.store.apis_by_ids(api_ids)

    async def list_runs(self, session, q: RunsQuery) -> Page[RunResult]:
        return self.store.list_runs(q)

    async def get_run(self, session, run_id):
        return self.store.get_run(run_id)

    async def count_runs(self, session, since=None, until=None, status=None, api_id=None):
        return self.store.count_runs(since=since, until=until, status=status, api_id=api_id)

    async def count_runs_by_job(self, session, since=None, until=None) -> dict[str, int]:
        return self.store.count_runs_by_job(since=since, until=until)

    async def aggregate_runs(self, session, q: AggregateQuery):
        # Task 8: rollup-fed. The group set and every count come from `rollup_day` (+ an exact
        # edge query over `runs` for a partial first/last day), the derived fields from
        # `api_watermark` / `current_state` -- so every API in the window is represented and the
        # cost follows the window's DAYS, not its runs. `Store.aggregate_rollups`' docstring
        # carries the documented semantic deltas vs the old whole-window run scan.
        return self.store.aggregate_rollups(
            q, flaky_min=self._config.flaky_min_transitions, apis=self.apis,
            briefing_tz=self._config.briefing_tz,
        )

    async def query_runs(self, session, spec: QuerySpec) -> QueryResult:
        # The whole method is one Store call: compilation, source selection, the window rule,
        # the ranked page, the totals and the evidence samples all live in `Store.query` /
        # `query_sql` (self-growth spec §4), so this backend adds nothing but the two pieces of
        # deployment context the compiler cannot know -- "now" (the session's clock) and the
        # config's timezone plus the default window a spec that names none falls back to. Same
        # `max_aggregate_window_days` the executor hands `title_for_spec`, so the card's title
        # and the rows below it always describe the same window.
        now = session.local_now() if session is not None else None
        return self.store.query(
            spec, now=now or datetime.now(UTC), tz=self._config.briefing_tz,
            default_window_days=self._config.max_aggregate_window_days,
            hot_days=self._config.retention_hot_days,
        )

    async def summarize_insights(self, session, since, until=None, scope_operator=None) -> Insights:
        # Two SQL counts, no group list: the flaky/regression tiles cannot saturate at a limit
        # any more (see Store.summarize_insights for why the old top-500 count read 0).
        return self.store.summarize_insights(
            since, until, flaky_min=self._config.flaky_min_transitions,
            scope_operator=scope_operator, briefing_tz=self._config.briefing_tz,
        )

    def _scope_since(self, session) -> datetime:
        """The operator-scope window's lower bound for the two reads that have no window of their
        own (``current_state`` / ``watermarks``): ``scope_window_days``, the same bound
        ``operator_scope`` uses -- so "the operator's APIs" means one thing everywhere."""
        now = session.local_now() if session is not None else None
        return (now or datetime.now(UTC)) - timedelta(days=self._config.scope_window_days)

    async def current_state(self, session, scope_api_ids=None, scope_operator=None) -> list[CellState]:
        return self.store.current_state(
            scope_api_ids, scope_operator=scope_operator,
            scope_since=self._scope_since(session) if scope_operator else None,
        )

    async def watermarks(self, session, api_ids=None, first_non_pass_since=None,
                         last_non_pass_since=None, scope_operator=None) -> list[ApiWatermark]:
        return self.store.watermarks(
            api_ids, first_non_pass_since, last_non_pass_since, scope_operator=scope_operator,
            scope_since=self._scope_since(session) if scope_operator else None,
        )

    async def operator_scope(self, session, operator_id: str, window_days: int) -> ScopeSummary:
        now = session.local_now() or datetime.now(UTC)
        sample, total = self.store.operator_scope_summary(operator_id, window_days, now, limit=100)
        return ScopeSummary(api_ids=sample, total=total)

    async def stage_job(self, session, draft: JobDraft, actor_kind: ActorKind) -> JobSpec:
        return self.ledger.stage(draft, actor=session.operator, actor_kind=actor_kind)

    async def get_pending_jobs(self, session):
        return self.ledger.pending()

    async def apply_job(self, session, job_id):
        applied = self.ledger.apply(job_id, actor=session.operator)
        self._audit(session, "applied", "job", job_id)
        return applied

    async def discard_job(self, session, job_id, actor_kind):
        return self.ledger.discard(job_id, actor=session.operator, actor_kind=actor_kind)

    async def get_job(self, session, job_id):
        return self.ledger.get(job_id)

    async def applied_jobs(self, session):
        return self.ledger.applied()

    async def all_jobs(self, session, status=None, cursor=None, limit=50) -> Page[JobSpec]:
        rows = [*self.ledger.pending(), *self.ledger.applied()]
        if status is not None:
            rows = [j for j in rows if j.status.value == status]
        return _page(rows, cursor, limit, key=lambda j: (j.created_at, j.job_id))

    async def active_jobs(self, session) -> list[JobSpec]:
        return [j for j in self.ledger.applied() if j.remaining_executions > 0]

    async def runs_by_ids(self, session, run_ids):
        return self.store.runs_by_ids(run_ids)

    async def record_execution(self, session, job_id, run_ids, schedule_index):
        return self.ledger.record_execution(job_id, run_ids, schedule_index)

    async def add_guardrail_note(self, session, job_id, note):
        return self.ledger.add_guardrail_note(job_id, note)

    async def stage_rule(self, session, draft: RuleDraft, actor_kind: ActorKind) -> ValidationRule:
        return self.rule_ledger.stage(draft, actor=session.operator, actor_kind=actor_kind)

    async def get_pending_rules(self, session):
        return self.rule_ledger.pending()

    async def apply_rule(self, session, rule_id):
        """규칙을 발효시킨다. Task 5 ruling: save_format_as를 가진 raw-pattern 규칙(패턴이 있는
        규칙)이 승인되면 그 즉시 포맷 라이브러리에도 승격한다 -- apply_rule은 host 승인 마크 뒤에서만
        불리므로 이 승격도 승인된 것이다. library.add가 (False, reason)을 돌려주면(이름/패턴 충돌,
        라이브러리 만원) 규칙 적용 자체는 실패시키지 않고 guardrail_notes에 한 줄 남긴다 -- 라이브러리
        추가는 inert하므로 판정에는 아무 영향이 없다."""
        applied = self.rule_ledger.apply(rule_id, actor=session.operator)
        self._audit(session, "applied", "rule", rule_id)
        if applied.save_format_as and applied.pattern:
            added, skip_reason = self.format_library.add(FormatDefinition(
                name=applied.save_format_as, pattern=applied.pattern,
                pass_examples=list(applied.pass_examples), fail_examples=list(applied.fail_examples),
                created_at=datetime.now(UTC), created_by=session.operator,
            ))
            if not added:
                applied = self.rule_ledger.add_guardrail_note(
                    rule_id, f"format {applied.save_format_as!r} was not saved: {skip_reason}")
        return applied

    async def discard_rule(self, session, rule_id, actor_kind):
        return self.rule_ledger.discard(rule_id, actor=session.operator, actor_kind=actor_kind)

    async def get_rule(self, session, rule_id: str) -> ValidationRule | None:
        del session
        return self.rule_ledger.get(rule_id)

    async def list_rules(self, session, api_id=None, status=None, cursor=None, limit=50) -> Page[ValidationRule]:
        rows = self.rule_ledger.list(api_id=api_id)
        if status is not None:
            rows = [r for r in rows if r.status.value == status]
        return _page(rows, cursor, limit, key=lambda r: (r.created_at, r.rule_id))

    async def simulate_rule(self, session, draft: RuleDraft, window_days: int) -> RuleImpact:
        """읽기 전용: draft를 아직 저장하지 않은 채 evaluate로만 돌려본다. window_days 안의 실행만
        본다. 각 과거 run의 입력값은 그 run을 낳은 job의 test_data 세트(test_data_label로 찾는다)에서
        draft.param 바인딩을 복원한다 — job_id나 test_data_label이 없거나, 그 세트에 param이 없으면
        복원 불가로 excluded_unknown에 들어간다. ledger에도 runs에도 아무것도 쓰지 않는다."""
        candidate = ValidationRule(
            rule_id="__simulated__", api_id=draft.api_id, param=draft.param, kind=draft.kind, op=draft.op,
            value=draft.value, values=list(draft.values), format=draft.format, pattern=draft.pattern,
            message=draft.message(), created_at=datetime.now(UTC), created_by=session.operator,
            created_by_kind=draft.created_by_kind)
        now = session.local_now() or datetime.now(UTC)
        floor = now - timedelta(days=window_days)
        window_runs = 0
        known_inputs = 0
        would_fail = 0
        for run in self.store.fetch_runs(api_id=draft.api_id, since=floor):
            window_runs += 1
            if run.job_id is None or run.test_data_label is None:
                continue
            job = self.ledger.get(run.job_id)
            if job is None:
                continue
            data_set = next((d for d in job.test_data if d.label == run.test_data_label), None)
            if data_set is None or draft.param not in data_set.values:
                continue
            known_inputs += 1
            if not evaluate(candidate, data_set.values[draft.param]):
                would_fail += 1
        return RuleImpact(window_runs=window_runs, known_inputs=known_inputs, would_fail=would_fail,
                          excluded_unknown=window_runs - known_inputs)

    async def stage_profile(self, session, draft: ProfileDraft, actor_kind: ActorKind) -> ComparisonProfile:
        return self.profile_ledger.stage(draft, actor=session.operator, actor_kind=actor_kind)

    async def get_pending_profiles(self, session):
        return self.profile_ledger.pending()

    async def apply_profile(self, session, profile_id):
        """프로파일을 발효시킨다: effective_from을 찍은 뒤(과거 비교는 절대 다시 판정하지 않는다), 리포트
        저장소가 붙어 있고(self.reports) 대상 job에 이미 리포트가 존재하면 그 parity 블록을 저장된
        응답 바디로부터 이 프로파일의 ignore path로 재-diff한다 — 새 run은 하나도 만들지 않는다(self.runs
        는 건드리지 않는다). 아직 리포트가 없는 job이면 재-diff는 조용히 건너뛴다: 프로파일 발효 자체는
        실패시키지 않는다."""
        applied = self.profile_ledger.apply(profile_id, actor=session.operator)
        self._audit(session, "applied", "profile", profile_id)
        if self.reports is not None and self.reports.read_html(applied.job_id) is not None:
            async def body_loader(run_id: str) -> dict | None:
                return await self.get_body(session, run_id)

            async def masked_paths_loader(run_id: str) -> list[str]:
                return await self.get_body_masked_paths(session, run_id)

            await self.reports.rediff(applied.job_id, applied.ignore_paths, applied.per_api_ignore,
                                      body_loader=body_loader,
                                      masked_paths_loader=masked_paths_loader)
        return applied

    async def discard_profile(self, session, profile_id, actor_kind):
        return self.profile_ledger.discard(profile_id, actor=session.operator, actor_kind=actor_kind)

    async def get_profile(self, session, profile_id: str) -> ComparisonProfile | None:
        del session
        return self.profile_ledger.get(profile_id)

    async def list_profiles(self, session, job_id=None, status=None, cursor=None, limit=50) -> Page[ComparisonProfile]:
        rows = self.profile_ledger.list(job_id=job_id)
        if status is not None:
            rows = [p for p in rows if p.status.value == status]
        return _page(rows, cursor, limit, key=lambda p: (p.created_at, p.profile_id))

    async def get_parity_report(self, session, job_id: str) -> dict | None:
        return self.reports.parity(job_id) if self.reports is not None else None

    async def find_apis_with_param(self, session, param: str, cursor=None, limit=20) -> Page[ApiSpec]:
        applied_format_apis = {
            r.api_id for r in self.rule_ledger.applied() if r.kind == "format" and r.param == param
        }
        rows = [a for a in self.apis.values() if param in a.params and a.api_id not in applied_format_apis]
        return _page(rows, cursor, limit, key=lambda a: (a.updated_at, a.api_id))

    async def recommend_rules_for_api(self, session, api_id: str, limit=20) -> list[RuleRecommendation]:
        api = self.apis.get(api_id)
        if api is None:
            return []
        applied = self.rule_ledger.applied()
        already_ruled_params = {r.param for r in applied if r.api_id == api_id}
        recommendations: list[RuleRecommendation] = []
        for param in api.params:
            if param in already_ruled_params:
                continue
            for peer_rule in applied:
                if peer_rule.api_id != api_id and peer_rule.param == param:
                    recommendations.append(
                        RuleRecommendation(param=param, from_api_id=peer_rule.api_id, rule=peer_rule)
                    )
        return recommendations[:limit]

    async def get_body(self, session, run_id: str) -> dict | None:
        return self.store.get_body(run_id)

    async def get_body_masked_paths(self, session, run_id: str) -> list[str]:
        return self.store.get_body_masked_paths(run_id)

    async def audit(self, session, cursor=None, limit=50) -> Page[AuditEntry]:
        # seq is zero-padded to a fixed width in the cursor id so the tie-break orders
        # numerically, not lexicographically (str(9) > str(10) would otherwise mis-page
        # same-timestamp rows) -- see Store.audit.
        return self.store.audit(cursor, limit)

    async def append_audit(self, session, action: str, target_kind: str, target_id: str) -> None:
        self._audit(session, action, target_kind, target_id)

    # -- ask_log (자가발전 spec §6) --------------------------------------------------------

    async def record_ask(self, session, entry: AskEntry) -> AskEntry:
        # Idempotent on turn_id inside the Store (INSERT OR IGNORE): the host's turn-end hook is
        # best-effort and may fire twice for one turn, and two rows would skew every ratio the
        # Growth view publishes. Nothing is classified here — the entry arrives already decided.
        return self.store.insert_ask(entry)

    async def list_asks(self, session, outcome=None, cursor=None, limit=50) -> Page[AskEntry]:
        return self.store.list_asks(outcome, cursor, limit)

    async def get_ask(self, session, turn_id: str) -> AskEntry | None:
        # UNIQUE 인덱스 한 번. 투표 라우트가 덮어쓰기 전에 소유자와 직전 표를 읽는 자리다.
        del session
        return self.store.ask_by_turn(turn_id)

    async def set_feedback(self, session, turn_id: str, vote) -> AskEntry | None:
        # 덮어쓰기 UPDATE 하나 + 갱신된 행 읽기 하나. 감사 로그는 건드리지 않는다 -- 표는 공유
        # 상태의 변경이 아니다(§9). 없는 turn_id면 None이고, 404는 라우트가 낸다.
        del session
        if not self.store.set_feedback(turn_id, vote):
            return None
        return self.store.ask_by_turn(turn_id)

    async def growth_summary(self, session, since: datetime) -> GrowthSummary:
        # COUNTs and one GROUP BY, never a row list this method then counts.
        counts = self.store.ask_counts(since)
        return GrowthSummary(
            asks_total=counts["total"], answered=counts["answered"], partial=counts["partial"],
            unmet=counts["unmet"], action=counts["action"], up=counts["up"], down=counts["down"],
            # Both are COUNTs over their own table -- the vocabulary sidecar (§7) and the
            # promoted saved questions (§8) -- never a number inferred from the ask log.
            new_terms=self.store.count_vocabulary_since(since),
            new_saved=self.store.count_saved_questions_since(since),
            unmet_clusters=self.store.unmet_clusters(since),
            unmet_clusters_total=self.store.count_unmet_clusters(since),
            # 승격 문턱은 config의 값이고, 화면은 그것을 읽어 빈 상태 문구를 쓴다 -- 포털에
            # "3명·5회"를 상수로 박으면 config를 바꾼 배포에서 조용히 거짓말이 된다(spec §8).
            thresholds={
                "min_users": self._config.promote_min_users,
                "min_asks": self._config.promote_min_asks,
                "window_days": self._config.promote_window_days,
            },
        )

    # -- 어휘 (자가발전 spec §7) -------------------------------------------------------------

    def _vocabulary_fact_subject(self, session) -> str:
        """저장 주체는 **프로젝트**다 — 사람이 아니다. 한 사람이 확인한 용어를 팀이 쓰는 것이
        이 기능의 전부라, 세션의 operator는 저자로만 기록되고 키에는 들어가지 않는다."""
        return session.project_id if session is not None else "project"

    def _validated_fact(self, session, entry: VocabularyEntry):
        """저장 직전의 단 하나의 문: 펜스 + ``MemoryWriteFilter``. term이나 fragment 값에 개인정보
        모양(9자리+ 숫자·IBAN·이메일)이 있으면 ``MemoryWriteRejected``가 올라오고, 호출자는 그것을
        ``None``으로 바꿔 돌려준다 — 아무것도 저장되지 않는다."""
        fact = entry_to_fact(
            entry,
            source_session_id=session_tag(session.session_id) if session is not None else None,
        )
        return validate_fact(
            fact.key, fact.value, fact.category.value, fence=ATWORKS_FENCE,
            write_filter=self._memory_filter, source_session_id=fact.source_session_id,
        )

    async def propose_alias(self, session, term: str, fragment: QueryFilters,
                            note: str | None = None) -> AliasProposal:
        del note   # 카드에도 컨텍스트에도 실리지 않는다 -- 모델이 남기는 메모일 뿐이라 저장하지 않는다
        normalized = normalize_term(term)
        if not normalized or rejected_fragment_fields(fragment):
            # 모양 검사는 실행기에도 있지만(툴 입력) 저장하는 쪽에도 있다: "오퍼레이터 id가 팀
            # 전체의 컨텍스트에 굳는 일은 없다"는 규칙은 오늘의 배선이 아니라 계약이어야 한다.
            return AliasProposal(outcome="refused")
        now = (session.local_now() if session is not None else None) or datetime.now(UTC)
        existing = self.store.get_vocabulary(normalized)
        cooled = (existing is not None and existing.status == "rejected"
                  and (existing.cooldown_until is None or existing.cooldown_until <= now))
        if existing is not None and not cooled:
            # 이미 있는 행은 덮지 않는다 -- 확정된 뜻이 이번 턴의 조각으로 조용히 바뀌면 안 되고,
            # 냉각 중인 거부를 다음 날 다시 물어도 안 된다. 저장된 행을 그대로 돌려준다.
            return AliasProposal(outcome="existing", entry=existing)
        candidate = VocabularyEntry(
            term=normalized, fragment=fragment, status="pending",
            proposed_by=session.operator if session is not None else "", proposed_at=now,
        )
        try:
            self._validated_fact(session, candidate)
        except MemoryWriteRejected:
            # 사이드카에도 아무것도 쓰지 않는다: 필터가 막은 값은 어느 테이블에도 남지 않는다.
            # fact 자체는 여기서 쓰지 않는다(확정이 쓴다) -- 이 호출은 그 문을 통과하는지만 본다.
            return AliasProposal(outcome="refused")
        if cooled:
            # 쿨다운이 끝났다: 같은 행을 fresh pending으로 되살린다(카운터와 이력은 남는다).
            stored = self.store.repropose_vocabulary(candidate)
            self._confirmed_cache = None
            return AliasProposal(outcome="proposed", entry=stored or candidate)
        stored, inserted = self.store.propose_vocabulary(candidate)
        self._confirmed_cache = None
        return AliasProposal(outcome="proposed" if inserted else "existing", entry=stored)

    async def list_vocabulary(self, session, status=None, cursor=None, limit=50) -> Page[VocabularyEntry]:
        del session
        return self.store.list_vocabulary(status, cursor, limit)

    async def confirm_alias(self, session, term: str) -> VocabularyEntry | None:
        now = (session.local_now() if session is not None else None) or datetime.now(UTC)
        entry = self.store.set_vocabulary_status(
            normalize_term(term), "confirmed",
            by=session.operator if session is not None else "", now=now,
            cooldown_days=self._config.vocabulary_cooldown_days,
        )
        self._confirmed_cache = None
        if entry is not None:
            # fact는 **여기서** 쓴다(ABC 의무 4): 사람의 클릭이 그 뜻을 팀의 것으로 만든 순간이다.
            # 그래서 `memory_facts`가 곧 confirmed 집합이고, 승인되지 않은 뜻이 기억 저장소에
            # 앉아 있는 상태가 없다. 필터는 제안 때 이미 통과했으므로 여기서 막힐 수 없다.
            await self.memory_store.upsert_facts(
                self._vocabulary_fact_subject(session), [self._validated_fact(session, entry)])
        return entry

    async def reject_alias(self, session, term: str) -> VocabularyEntry | None:
        now = (session.local_now() if session is not None else None) or datetime.now(UTC)
        normalized = normalize_term(term)
        entry = self.store.set_vocabulary_status(
            normalized, "rejected",
            by=session.operator if session is not None else "", now=now,
            cooldown_days=self._config.vocabulary_cooldown_days,
        )
        self._confirmed_cache = None
        if entry is not None:
            await self._forget_facts(session, [normalized])
        return entry

    async def _forget_facts(self, session, terms: list[str]) -> None:
        """확정이 풀린 term의 fact를 지운다(거부·삭제·자동 강등). 사이드카 행은 남을 수 있지만
        (쿨다운도 이력도 거기 있다) fact는 남지 않는다 -- `memory_facts`는 confirmed 집합이다."""
        subject = self._vocabulary_fact_subject(session)
        for term in terms:
            await self.memory_store.delete_fact(subject, term.replace(" ", "_"))

    async def delete_alias(self, session, term: str) -> VocabularyEntry | None:
        normalized = normalize_term(term)
        entry = self.store.delete_vocabulary(normalized)
        if entry is not None:
            # 사이드카와 fact를 함께 지운다 -- 한쪽만 지우면 용어를 반쯤 기억한 저장소가 남는다.
            await self._forget_facts(session, [normalized])
        self._confirmed_cache = None
        return entry

    async def confirmed_vocabulary(self, session) -> list[VocabularyEntry]:
        del session
        if self._confirmed_cache is None:
            self._confirmed_cache = self.store.confirmed_vocabulary()
        return list(self._confirmed_cache)

    async def note_vocabulary_use(self, session, terms: list[str], rejected: bool = False) -> list[str]:
        normalized = [normalize_term(t) for t in terms if normalize_term(t)]
        if not normalized:
            return []
        if rejected:
            demoted = self.store.note_vocabulary_rejection(
                normalized, auto_demote_rejections=self._config.vocabulary_auto_demote_rejections)
            if demoted:
                # 강등된 term만 confirmed 집합에서 빠진다. 강등이 없었던 호출은 집합을 바꾸지
                # 않으므로 캐시도 그대로 둔다.
                await self._forget_facts(session, demoted)
                self._confirmed_cache = None
            return list(demoted)
        else:
            # `uses + 1`은 순수 집계다 -- confirmed 집합이 그대로이므로 캐시를 버릴 이유가 없다.
            # (매 턴 버리면 캐시가 하는 일이 없어지고, 어휘가 실린 턴마다 SQL이 한 번 더 돈다.)
            self.store.bump_vocabulary_uses(normalized)
            return []

    # -- 저장 질문 (자가발전 spec §8) --------------------------------------------------------

    async def list_saved_questions(self, session, status=None, cursor=None, limit=50) -> Page[SavedQuestion]:
        del session
        return self.store.list_saved_questions(status, cursor, limit)

    async def run_saved_question(self, session, saved_id: str) -> QueryResult | None:
        saved = self.store.get_saved_question(saved_id)
        if saved is None or saved.status != "active":
            # 숨긴 질문은 링크로도 돌지 않는다 -- 그러지 않으면 '숨김'이 아무 뜻도 없다.
            return None
        # 저장 질문은 스냅샷이 아니라 질문이다: 승격 당시의 행이 아니라 지금의 창 규칙으로 다시
        # 계산한다. 실행기는 `query_runs`가 쓰는 바로 그것이라 카드와 저장 질문이 같은 숫자를 본다.
        now = session.local_now() if session is not None else None
        result = self.store.query(
            saved.spec, now=now or datetime.now(UTC), tz=self._config.briefing_tz,
            default_window_days=self._config.max_aggregate_window_days,
            hot_days=self._config.retention_hot_days,
        )
        self.store.bump_saved_use(saved_id, now or datetime.now(UTC))
        return result

    async def set_saved_question_status(self, session, saved_id: str, status) -> SavedQuestion | None:
        del session
        return self.store.set_saved_status(saved_id, status)

    def _audit(self, session, action: str, target_kind: str, target_id: str) -> None:
        """Append-only audit row (spec §2). Every `apply_*` below writes one the moment the
        ledger state actually changes -- so "사람이 이 시각에 승인했다" is evidence held by the
        backend itself, not by whichever route happened to call it. A held apply (no host
        approval mark) never reaches these methods, so it writes nothing."""
        now = session.local_now() or datetime.now(UTC)
        self.store.append_audit(now, session.operator, action, target_kind, target_id, session.session_id)

    async def get_format(self, session, name: str) -> FormatDefinition | None:
        return self.format_library.get(name)

    async def list_formats(self, session) -> list[FormatDefinition]:
        return self.format_library.list()

    async def save_format(self, session, defn: FormatDefinition) -> tuple[bool, str | None]:
        return self.format_library.add(defn)

    async def stage_format_batch(self, session, draft: FormatBatchDraft, actor_kind: ActorKind) -> FormatBatch:
        return self.format_batch_ledger.stage(draft, actor=session.operator, actor_kind=actor_kind)

    async def get_pending_format_batches(self, session):
        return self.format_batch_ledger.pending()

    async def get_format_batch(self, session, batch_id: str) -> FormatBatch | None:
        del session
        return self.format_batch_ledger.get(batch_id)

    async def apply_format_batch(self, session, batch_id):
        applied = self.format_batch_ledger.apply(batch_id, actor=session.operator)
        self._audit(session, "applied", "format_batch", batch_id)
        return applied

    async def discard_format_batch(self, session, batch_id, actor_kind):
        return self.format_batch_ledger.discard(batch_id, actor=session.operator, actor_kind=actor_kind)

    async def execute_job_once(self, session, job_id, schedule_index=None) -> list[RunResult]:
        job = self.ledger.get(job_id)
        if job is None:
            return []
        if job.remaining_executions <= 0:
            return []
        api_ids = job.api_ids
        if job.binding is Binding.LATE and job.select_where:
            # LATE re-resolves once per execution, not once per environment: the selection is
            # a property of the job, and re-running it per env would let two envs disagree
            # about what the job even is. Stage-time and LATE re-resolution share the same
            # resolve_select_where — the backend passed here is this MockAtworks itself, which
            # implements search_apis/get_api/list_runs.
            where = SelectWhere.model_validate(job.select_where)
            resolution = await resolve_select_where(self, session, where, self._config, now=datetime.now(UTC))
            api_ids = resolution.api_ids
        # The size caps are re-derived here (not just relied on from staging): a LATE
        # selection may have grown since then, and a FROZEN job may run under a config that
        # has since tightened (M11).
        violations = enforce_execution_matrix(self._config, job, api_ids)
        if violations:
            for message in violations:
                self.ledger.add_guardrail_note(job_id, message)
            # the slot is spent even though nothing ran, so a scheduled job does not retry
            # the same over-limit matrix forever
            self.ledger.record_execution(job_id, [], schedule_index)
            return []
        produced: list[RunResult] = []
        # Task 6 (scale spec §7): one policy per call. `body_capture_enabled` decides, per API
        # group, whether a body is even built; `mask_body` scrubs whatever body IS built on its
        # way into `bodies` at the `store.ingest` boundary below -- masking never touches the
        # in-memory `produced` list itself, only what SQLite ends up storing.
        policy = policy_from_config(self._config)
        bindings: list[TestDataSet | None] = list(job.test_data) or [None]
        # The matrix is flattened first so the yield points below are evenly spaced over the
        # whole envs × data × apis product, not per environment. Mock execution is CPU-only, so
        # a batch is just "max_concurrency cells then hand the loop back" -- the shape a REST
        # adapter fills with a real semaphore, and what keeps an SSE turn from starving while a
        # 400-cell job runs.
        cells = [(env, data, api_id) for env in job.target_envs for data in bindings for api_id in api_ids]
        for start in range(0, len(cells), self._config.max_concurrency):
            for env, data, api_id in cells[start:start + self._config.max_concurrency]:
                api = self.apis.get(api_id)
                if api is None:
                    continue
                self._run_seq += 1
                status, rules, http = stub_verdict(api, env, data)
                # additive: an applied rule effective as of now can only add failures on
                # top of the legacy stub's verdict, never remove one (fixtures have no
                # applied rules, so no fixture verdict changes).
                now = datetime.now(UTC)
                extra_failed: list[str] = []
                for r in self.rule_ledger.applied():
                    if r.api_id != api_id or r.effective_from is None or r.effective_from > now:
                        continue
                    bound = data.values.get(r.param) if data is not None else None
                    if not evaluate(r, bound):
                        extra_failed.append(r.message)
                if extra_failed:
                    status = RunStatus.FAIL if status is RunStatus.PASS else status
                    rules = [*rules, *extra_failed]
                # A group opted out of body capture (`masking_disabled_groups`) never gets a
                # body built at all -- response_body stays None from construction, so no
                # `bodies` row is ever written and `has_body` is False on the model-facing
                # record (serialization.py).
                body = (
                    stub_response(api, env, data, self._run_seq)
                    if body_capture_enabled(api, policy) else None
                )
                run = RunResult(
                    run_id=f"run-{self._run_seq:04d}", api_id=api_id, executed_at=datetime.now(UTC),
                    target_env=env, test_data_label=data.label if data is not None else None,
                    status=status, failed_rules=rules, http_status=http,
                    duration_ms=100 + self._run_seq % 50,
                    response_body=body, job_id=job_id,
                    executed_by=job.applied_by)
                produced.append(run)
            await asyncio.sleep(0)
        # The WRITE is chunked (Task 9, spec §9) so a 400-cell job can no longer park the event
        # loop inside one unbroken ingest -- that single call was the whole SSE-latency breach the
        # Task 7 bench measured. The chunk is `ingest_chunk_size`, NOT `max_concurrency`: the
        # generation loop above batches how many cells are computed before yielding, this one
        # batches how many runs share a transaction, and tying them together made a 400-cell job
        # 100 commits (Task 9 review). Each slice is its own transaction (runs + bodies + the four
        # materialized tables move together, spec §4), and `ingest` is idempotent per run_id.
        # `mask` rewrites each body on its way into `bodies` -- store-boundary masking only,
        # per Task 5's `ingest(mask=...)` hook.
        chunk = self._config.ingest_chunk_size
        slices = [produced[start:start + chunk] for start in range(0, len(produced), chunk)]
        committed: list[RunResult] = []
        for batch in slices:
            try:
                self.store.ingest(batch, self._config.briefing_tz,
                                  mask=lambda b: mask_body_paths(b, policy))
            except Exception as error:
                # A mid-chunk failure is OWNED here, not re-raised: the scheduler's exception
                # path would call `record_execution` a SECOND time and spend two slots for one
                # occurrence. The committed chunks are real runs sitting in the store, so the
                # slot is recorded with exactly those run_ids -- `run_count` then matches what
                # was actually written -- the reason is left as a guardrail note, and the
                # committed runs are returned so the report is built over what exists.
                logger.exception("job %s: ingest failed after %d/%d chunks",
                                 job_id, len(committed) // chunk, len(slices))
                self.ledger.record_execution(job_id, [r.run_id for r in committed], schedule_index)
                self.ledger.add_guardrail_note(
                    job_id,
                    f"ingest failed after {len(committed) // chunk}/{len(slices)} chunks: "
                    f"{type(error).__name__}",
                )
                return committed
            committed.extend(batch)
            await asyncio.sleep(0)
        # `record_execution` happens exactly ONCE per call whatever the outcome -- on the happy
        # path here, on the failure path above, and with an empty list on the guardrail path
        # earlier. The slot contract is unchanged.
        self.ledger.record_execution(job_id, [r.run_id for r in produced], schedule_index)
        return produced

    def capture_disabled(self, api_id: str) -> bool:
        """Is this API's group opted out of response-body capture (`masking_disabled_groups`)?
        The report asks so a missing body can say WHICH of the two reasons it is -- capture off
        vs. aged past `retention_body_days` -- instead of one vague note covering both."""
        api = self.apis.get(api_id)
        return api is not None and not body_capture_enabled(api, policy_from_config(self._config))

    async def get_context(self, session):
        # Windowed counts (spec §9: count_runs ×2 over 30 days + operator_scope). Without the
        # window these two were an index scan of every fail and every error ever recorded -- the
        # same count bounded to the window is O(window), which is what keeps this read inside its
        # 50ms budget at 2M runs. The window is the same one the operator scope uses.
        now = session.local_now() or datetime.now(UTC)
        since = now - timedelta(days=self._config.scope_window_days)
        fails = self.store.count_runs(since=since, status="fail")
        errors = self.store.count_runs(since=since, status="error")
        scope, scope_total = self.store.operator_scope_summary(
            session.operator, self._config.scope_window_days, now, limit=20)
        return {"project": session.project_id, "allowed_targets": list(self._config.allowed_target_envs),
                "recent_counts": {"fail": fails, "error": errors, "pending_jobs": len(self.ledger.pending())},
                "operator": session.operator, "operator_role": session.role,
                "scope_api_ids": scope, "scope_api_count": scope_total}
