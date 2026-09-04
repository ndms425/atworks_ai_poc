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
    RunResult,
    RunStatus,
)
from atworks_agent.types import Binding


# 결정론 스텁 "DSL": 경로에 refund가 있으면 금액 규칙 실패, api-007은 503 에러, 나머지 pass.
def stub_verdict(api: ApiSpec, sequence: int) -> tuple[RunStatus, list[str], int]:
    if "refund" in api.path:
        return RunStatus.FAIL, ["refundAmount >= 0"], 200
    if api.api_id == "api-007":
        return RunStatus.ERROR, [], 503
    return RunStatus.PASS, [], 200


class MockAtworks(AtworksBackend):
    def __init__(self, config: AtworksAgentConfig, fixtures_dir: Path):
        self._config = config
        self.ledger = JobLedger(config)
        self.apis: dict[str, ApiSpec] = {
            row["api_id"]: ApiSpec(**row) for row in json.loads((fixtures_dir / "apis.json").read_text(encoding="utf-8"))
        }
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

    async def execute_job_once(self, session, job_id) -> list[RunResult]:
        job = self.ledger.get(job_id)
        if job is None:
            return []
        if (job.runs_remaining or 0) <= 0:
            return []
        api_ids = job.api_ids
        if job.binding is Binding.LATE and job.select_where:
            w = job.select_where
            api_ids = [a.api_id for a in await self.search_apis(
                session, query=w.get("query", ""), group=w.get("group"),
                updated_after=datetime.fromisoformat(w["updated_after"]) if w.get("updated_after") else None,
                limit=self._config.max_apis_per_job + 1)]
            if len(api_ids) > self._config.max_apis_per_job:
                self.ledger.add_guardrail_note(
                    job_id,
                    f"execution skipped: LATE selection resolved to {len(api_ids)} APIs, "
                    f"above the limit of {self._config.max_apis_per_job}",
                )
                # the slot is spent even though nothing ran, so a scheduled job does not
                # retry the same over-limit selection forever
                self.ledger.record_execution(job_id, [])
                return []
        produced: list[RunResult] = []
        for api_id in api_ids:
            api = self.apis.get(api_id)
            if api is None:
                continue
            self._run_seq += 1
            status, rules, http = stub_verdict(api, self._run_seq)
            run = RunResult(run_id=f"run-{self._run_seq:04d}", api_id=api_id, executed_at=datetime.now(UTC),
                            target_env=job.target_env, status=status, failed_rules=rules, http_status=http,
                            duration_ms=100 + self._run_seq % 50, job_id=job_id)
            self.runs[run.run_id] = run
            produced.append(run)
        self.ledger.record_execution(job_id, [r.run_id for r in produced])
        return produced

    async def get_context(self, session):
        fails = len(self._filter_runs(None, "fail", None))
        errors = len(self._filter_runs(None, "error", None))
        return {"project": session.project_id, "allowed_targets": ["dev", "stg"],
                "recent_counts": {"fail": fails, "error": errors, "pending_jobs": len(self.ledger.pending())}}
