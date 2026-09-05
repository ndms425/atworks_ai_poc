"""MockAtworks: 픽스처 위의 AtworksBackend. 판정은 결정론 스텁 — 실제 aTworks에선 DSL 엔진이
낸다. Java 통합 시 이 파일과 같은 인터페이스로 rest_backend.py를 쓴다."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from atworks_agent import (
    ActorKind,
    ApiSpec,
    AtworksAgentConfig,
    AtworksBackend,
    JobDraft,
    JobLedger,
    JobSpec,
    RuleDraft,
    RuleImpact,
    RuleLedger,
    RunResult,
    RunStatus,
    SelectWhere,
    TestDataSet,
    ValidationRule,
    enforce_execution_matrix,
    evaluate,
    resolve_select_where,
)
from atworks_agent.types import Binding


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


class MockAtworks(AtworksBackend):
    def __init__(self, config: AtworksAgentConfig, fixtures_dir: Path):
        self._config = config
        self.apis: dict[str, ApiSpec] = {
            row["api_id"]: ApiSpec(**row) for row in json.loads((fixtures_dir / "apis.json").read_text(encoding="utf-8"))
        }
        self.ledger = JobLedger(config, self.apis)
        self.rule_ledger = RuleLedger(config, self.apis)
        self.runs: dict[str, RunResult] = {
            row["run_id"]: RunResult(**row) for row in json.loads((fixtures_dir / "runs.json").read_text(encoding="utf-8"))
        }
        self._run_seq = len(self.runs)

    async def search_apis(self, session, query="", updated_after=None, group=None, limit=20):
        q = (query or "").lower()
        rows = [a for a in self.apis.values()
                if (not q or q in f"{a.method} {a.path} {a.name}".lower())
                and (group is None or a.group == group)
                and (updated_after is None or a.updated_at >= updated_after)]
        rows.sort(key=lambda a: a.updated_at, reverse=True)
        return rows[:limit]

    async def get_api(self, session, api_id):
        return self.apis.get(api_id)

    async def list_runs(self, session, since=None, status=None, api_id=None, limit=50):
        rows = self._filter_runs(since, status, api_id)
        return rows[:limit]

    async def get_run(self, session, run_id):
        return self.runs.get(run_id)

    async def count_runs(self, session, since, status):
        return len(self._filter_runs(since, status, None))

    def _filter_runs(self, since, status, api_id) -> list[RunResult]:
        rows = [r for r in self.runs.values()
                if (since is None or r.executed_at >= since)
                and (status is None or (r.status.value != "pass" if status == "non_pass" else r.status.value == status))
                and (api_id is None or r.api_id == api_id)]
        return sorted(rows, key=lambda r: r.executed_at, reverse=True)

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

    async def all_jobs(self, session):
        return [*self.ledger.pending(), *self.ledger.applied()]

    async def runs_by_ids(self, session, run_ids):
        return [self.runs[i] for i in run_ids if i in self.runs]

    async def record_execution(self, session, job_id, run_ids, schedule_index):
        return self.ledger.record_execution(job_id, run_ids, schedule_index)

    async def add_guardrail_note(self, session, job_id, note):
        return self.ledger.add_guardrail_note(job_id, note)

    async def stage_rule(self, session, draft: RuleDraft, actor_kind: ActorKind) -> ValidationRule:
        return self.rule_ledger.stage(draft, actor=session.operator, actor_kind=actor_kind)

    async def get_pending_rules(self, session):
        return self.rule_ledger.pending()

    async def apply_rule(self, session, rule_id):
        return self.rule_ledger.apply(rule_id, actor=session.operator)

    async def discard_rule(self, session, rule_id, actor_kind):
        return self.rule_ledger.discard(rule_id, actor=session.operator, actor_kind=actor_kind)

    async def list_rules(self, session, api_id=None):
        return self.rule_ledger.list(api_id=api_id)

    async def simulate_rule(self, session, draft: RuleDraft) -> RuleImpact:
        """읽기 전용: draft를 아직 저장하지 않은 채 evaluate로만 돌려본다. 각 과거 run의 입력값은
        그 run을 낳은 job의 test_data 세트(test_data_label로 찾는다)에서 draft.param 바인딩을
        복원한다 — job_id나 test_data_label이 없거나, 그 세트에 param이 없으면 복원 불가로
        excluded_unknown에 들어간다. ledger에도 runs에도 아무것도 쓰지 않는다."""
        candidate = ValidationRule(
            rule_id="__simulated__", api_id=draft.api_id, param=draft.param, kind=draft.kind, op=draft.op,
            value=draft.value, values=list(draft.values), format=draft.format, pattern=draft.pattern,
            message=draft.message(), created_at=datetime.now(UTC), created_by=session.operator,
            created_by_kind=draft.created_by_kind)
        window_runs = 0
        known_inputs = 0
        would_fail = 0
        for run in self.runs.values():
            if run.api_id != draft.api_id:
                continue
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
                        duration_ms=100 + self._run_seq % 50, job_id=job_id)
                    self.runs[run.run_id] = run
                    produced.append(run)
        self.ledger.record_execution(job_id, [r.run_id for r in produced], schedule_index)
        return produced

    async def get_context(self, session):
        fails = len(self._filter_runs(None, "fail", None))
        errors = len(self._filter_runs(None, "error", None))
        return {"project": session.project_id, "allowed_targets": ["dev", "stg"],
                "recent_counts": {"fail": fails, "error": errors, "pending_jobs": len(self.ledger.pending())}}
