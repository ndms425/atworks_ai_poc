from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from commerce_common.skills import Skill, SkillRegistry

from atworks_agent.backend import AtworksBackend
from atworks_agent.config import AtworksAgentConfig
from atworks_agent.jobs import JobDraft, JobLedger
from atworks_agent.rules import FormatBatchLedger, FormatLibrary, RuleImpact, RuleLedger
from atworks_agent.types import (
    ActorKind,
    ApiSpec,
    AtworksSessionContext,
    AtworksSessionState,
    RuleRecommendation,
    RunResult,
    RunStatus,
)

T0 = datetime(2026, 9, 1, 9, tzinfo=UTC)


class InMemoryBackend(AtworksBackend):
    def __init__(self, config: AtworksAgentConfig):
        self.apis = {
            "api-1": ApiSpec(api_id="api-1", method="POST", path="/v1/contracts", name="계약 생성", group="contract", updated_at=T0, has_rules=True, params=["contractNo", "amount"]),
            "api-2": ApiSpec(api_id="api-2", method="GET", path="/v1/contracts/{id}", name="계약 조회", group="contract", updated_at=T0 - timedelta(days=20), has_rules=False),
        }
        self.ledger = JobLedger(config, self.apis)
        self.rule_ledger = RuleLedger(config, self.apis)
        self.format_library = FormatLibrary(max_size=config.max_format_library)
        self.format_batch_ledger = FormatBatchLedger(config, self.format_library)
        self.runs = [
            RunResult(run_id="run-1", api_id="api-1", executed_at=T0, target_env="dev", status=RunStatus.FAIL, failed_rules=["amount >= 0"], http_status=200),
            RunResult(run_id="run-2", api_id="api-2", executed_at=T0 + timedelta(minutes=5), target_env="dev", status=RunStatus.ERROR, http_status=503),
            RunResult(run_id="run-3", api_id="api-1", executed_at=T0 + timedelta(minutes=9), target_env="dev", status=RunStatus.PASS, http_status=200),
        ]
        self.executed: list[str] = []

    async def search_apis(self, session, query="", updated_after=None, group=None, limit=20):
        rows = [a for a in self.apis.values() if (query.lower() in (a.path + a.name).lower()) and (group is None or a.group == group)
                and (updated_after is None or a.updated_at >= updated_after)]
        return rows[:limit]

    async def get_api(self, session, api_id):
        return self.apis.get(api_id)

    async def list_runs(self, session, since=None, status=None, api_id=None, limit=50):
        rows = [r for r in self.runs if (since is None or r.executed_at >= since)
                and (status is None or (status == "non_pass" and r.status.value != "pass") or r.status.value == status)
                and (api_id is None or r.api_id == api_id)]
        return sorted(rows, key=lambda r: r.executed_at, reverse=True)[:limit]

    async def get_run(self, session, run_id):
        return next((r for r in self.runs if r.run_id == run_id), None)

    async def count_runs(self, session, since, status):
        return len(await self.list_runs(session, since=since, status=status, limit=10_000))

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

    async def all_jobs(self, session):
        return [*self.ledger.pending(), *self.ledger.applied()]

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

    async def list_rules(self, session, api_id=None):
        return self.rule_ledger.list(api_id=api_id)

    async def simulate_rule(self, session, draft):
        return RuleImpact()

    async def find_apis_with_param(self, session, param):
        applied_format_apis = {r.api_id for r in self.rule_ledger.applied() if r.kind == "format" and r.param == param}
        return [a for a in self.apis.values() if param in a.params and a.api_id not in applied_format_apis]

    async def recommend_rules_for_api(self, session, api_id):
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
        return recommendations

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

    async def apply_format_batch(self, session, batch_id):
        return self.format_batch_ledger.apply(batch_id, actor=session.operator)

    async def discard_format_batch(self, session, batch_id, actor_kind):
        return self.format_batch_ledger.discard(batch_id, actor=session.operator, actor_kind=actor_kind)

    async def execute_job_once(self, session, job_id, schedule_index=None):
        self.executed.append(job_id)
        return []

    async def get_context(self, session):
        return {"project": "MES", "allowed_targets": ["dev", "stg"]}


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
