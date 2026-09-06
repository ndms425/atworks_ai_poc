"""MockAtworks: 픽스처 위의 AtworksBackend. 판정은 결정론 스텁 — 실제 aTworks에선 DSL 엔진이
낸다. Java 통합 시 이 파일과 같은 인터페이스로 rest_backend.py를 쓴다."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from atworks_agent import (
    ActorKind,
    AggregateQuery,
    ApiSpec,
    ApiWatermark,
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
    JobDraft,
    JobLedger,
    JobSpec,
    OperatorProfile,
    Page,
    ProfileDraft,
    ProfileLedger,
    RuleDraft,
    RuleImpact,
    RuleLedger,
    RuleRecommendation,
    RunResult,
    RunsQuery,
    RunStatus,
    ScopeSummary,
    SelectWhere,
    TestDataSet,
    ValidationRule,
    decode_cursor,
    encode_cursor,
    enforce_execution_matrix,
    evaluate,
    resolve_select_where,
)
from atworks_agent.aggregation import aggregate
from atworks_agent.types import Binding

from .reports import Reports
from .store import Store


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
        self._run_seq = self.store.run_count()
        self.operators: dict[str, OperatorProfile] = {
            row["operator_id"]: OperatorProfile(**row)
            for row in json.loads((fixtures_dir / "operators.json").read_text(encoding="utf-8"))
        }

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

    async def search_apis(self, session, query="", group=None, updated_after=None, cursor=None, limit=20):
        return self.store.search_apis(query=query, group=group, updated_after=updated_after, cursor=cursor, limit=limit)

    async def get_api(self, session, api_id):
        return self.apis.get(api_id)

    async def list_runs(self, session, q: RunsQuery) -> Page[RunResult]:
        return self.store.list_runs(q)

    async def get_run(self, session, run_id):
        return self.store.get_run(run_id)

    async def count_runs(self, session, since=None, until=None, status=None, api_id=None):
        return self.store.count_runs(since=since, until=until, status=status, api_id=api_id)

    async def aggregate_runs(self, session, q: AggregateQuery):
        # Windowed/scoped SQL fetch (indexed on executed_at/api_id) narrows the row set; the
        # grouping/derived-field logic (run_ids, transitions, p95, regression_suspect, ...) is
        # reused byte-for-byte from aggregation.aggregate so the output is identical by
        # construction to the pre-SQL dict implementation.
        rows = self.store.fetch_runs(since=q.since, until=q.until, api_ids=q.scope_api_ids)
        groups = aggregate(rows, self.apis, q.group_by, flaky_min_transitions=self._config.flaky_min_transitions)
        return groups[: q.limit]

    async def current_state(self, session, scope_api_ids=None) -> list[CellState]:
        return self.store.current_state(scope_api_ids)

    async def watermarks(self, session, api_ids=None, first_non_pass_since=None) -> list[ApiWatermark]:
        return self.store.watermarks(api_ids, first_non_pass_since)

    async def operator_scope(self, session, operator_id: str, window_days: int) -> ScopeSummary:
        now = session.local_now() or datetime.now(UTC)
        scope = self.store.operator_scope_ids(operator_id, window_days, now)
        return ScopeSummary(api_ids=sorted(scope)[:100], total=len(scope))

    async def stage_job(self, session, draft: JobDraft, actor_kind: ActorKind) -> JobSpec:
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
        if self.reports is not None and self.reports.read_html(applied.job_id) is not None:
            self.reports.rediff(applied.job_id, applied.ignore_paths, applied.per_api_ignore)
        return applied

    async def discard_profile(self, session, profile_id, actor_kind):
        return self.profile_ledger.discard(profile_id, actor=session.operator, actor_kind=actor_kind)

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

    async def audit(self, session, cursor=None, limit=50) -> Page[AuditEntry]:
        # seq is zero-padded to a fixed width in the cursor id so the tie-break orders
        # numerically, not lexicographically (str(9) > str(10) would otherwise mis-page
        # same-timestamp rows) -- see Store.audit.
        return self.store.audit(cursor, limit)

    async def append_audit(self, session, action: str, target_kind: str, target_id: str) -> None:
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

    async def apply_format_batch(self, session, batch_id):
        return self.format_batch_ledger.apply(batch_id, actor=session.operator)

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
        bindings: list[TestDataSet | None] = list(job.test_data) or [None]
        for env in job.target_envs:
            for data in bindings:
                for api_id in api_ids:
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
                    run = RunResult(
                        run_id=f"run-{self._run_seq:04d}", api_id=api_id, executed_at=datetime.now(UTC),
                        target_env=env, test_data_label=data.label if data is not None else None,
                        status=status, failed_rules=rules, http_status=http,
                        duration_ms=100 + self._run_seq % 50,
                        response_body=stub_response(api, env, data, self._run_seq), job_id=job_id,
                        executed_by=job.applied_by)
                    self.store.upsert_run(run, self._config.briefing_tz)
                    produced.append(run)
        self.ledger.record_execution(job_id, [r.run_id for r in produced], schedule_index)
        return produced

    async def get_context(self, session):
        fails = self.store.count_runs(status="fail")
        errors = self.store.count_runs(status="error")
        now = session.local_now() or datetime.now(UTC)
        scope = self.store.operator_scope_ids(session.operator, self._config.scope_window_days, now)
        return {"project": session.project_id, "allowed_targets": list(self._config.allowed_target_envs),
                "recent_counts": {"fail": fails, "error": errors, "pending_jobs": len(self.ledger.pending())},
                "operator": session.operator, "operator_role": session.role,
                "scope_api_ids": sorted(scope)[:20], "scope_api_count": len(scope)}
