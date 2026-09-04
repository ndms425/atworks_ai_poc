"""AtworksBackend: aTworks Java BE와의 유일한 접점. 읽기는 자유, stage_job은 제안만 기록,
apply_job만 실제 상태를 바꾼다(승인된 job의 실행 예약/즉시 실행). 모든 메서드는 서버 측
credential로 aTworks를 호출하고, 모델은 결과만 본다. MerchantBackend 미러."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from .jobs import JobDraft
from .types import ActorKind, ApiSpec, AtworksSessionContext, JobSpec, RunResult


class AtworksBackend(ABC):
    # -- 읽기 ------------------------------------------------------------------------
    @abstractmethod
    async def search_apis(
        self, session: AtworksSessionContext, query: str = "",
        updated_after: datetime | None = None, group: str | None = None, limit: int = 20,
    ) -> list[ApiSpec]:
        """텍스트·갱신일·그룹으로 API 스펙 검색. updated_after는 '지난 1주일 업데이트' 류의 리졸버."""

    @abstractmethod
    async def get_api(self, session: AtworksSessionContext, api_id: str) -> ApiSpec | None: ...

    @abstractmethod
    async def list_runs(
        self, session: AtworksSessionContext, since: datetime | None = None,
        status: str | None = None, api_id: str | None = None, limit: int = 50,
    ) -> list[RunResult]:
        """실행 이력. status는 pass/fail/error. 판정값은 aTworks DSL이 낸 그대로다."""

    @abstractmethod
    async def get_run(self, session: AtworksSessionContext, run_id: str) -> RunResult | None: ...

    @abstractmethod
    async def count_runs(self, session: AtworksSessionContext, since: datetime | None, status: str | None) -> int:
        """triage 카드의 모수(population). list_runs의 limit과 무관하게 전체 건수."""

    # -- 실행 계획 (propose → preview → approve → apply) ----------------------------
    @abstractmethod
    async def stage_job(self, session: AtworksSessionContext, draft: JobDraft, actor_kind: ActorKind) -> JobSpec: ...

    @abstractmethod
    async def get_pending_jobs(self, session: AtworksSessionContext) -> list[JobSpec]: ...

    @abstractmethod
    async def apply_job(self, session: AtworksSessionContext, job_id: str) -> JobSpec:
        """승인된 job을 실행 큐에 넣는다(run_now면 즉시 실행). 이미 승인 마크를 통과한 뒤에만 호출된다."""

    @abstractmethod
    async def discard_job(self, session: AtworksSessionContext, job_id: str, actor_kind: ActorKind) -> JobSpec: ...

    # -- 실행 (스케줄러가 부른다, LLM 경로 아님) ------------------------------------------
    @abstractmethod
    async def execute_job_once(self, session: AtworksSessionContext, job_id: str) -> list[RunResult]:
        """job의 api_ids(또는 LATE면 select_where 재평가)를 target_env에 1회 실행하고 결과를 돌려준다."""

    # -- 선택 --------------------------------------------------------------------------
    async def get_context(self, session: AtworksSessionContext) -> dict[str, Any] | None:
        """요청별 컨텍스트(프로젝트명, 허용 계, 최근 실행 요약 카운트). 동적 프롬프트 블록에 들어간다."""
        return None
