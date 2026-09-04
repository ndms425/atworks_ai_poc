from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from commerce_common.skills import Skill, SkillRegistry

from atworks_agent.backend import AtworksBackend
from atworks_agent.config import AtworksAgentConfig
from atworks_agent.jobs import JobDraft, JobLedger
from atworks_agent.types import (
    ActorKind,
    ApiSpec,
    AtworksSessionContext,
    AtworksSessionState,
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
