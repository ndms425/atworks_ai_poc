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
        """실행 이력. status는 pass/fail/error/non_pass. 판정값은 aTworks DSL이 낸 그대로다.
        non_pass는 fail과 error를 함께 묶는다(트리아지 모집단)."""

    @abstractmethod
    async def get_run(self, session: AtworksSessionContext, run_id: str) -> RunResult | None: ...

    @abstractmethod
    async def count_runs(self, session: AtworksSessionContext, since: datetime | None, status: str | None) -> int:
        """triage 카드의 모수(population). list_runs의 limit과 무관하게 전체 건수.
        status는 pass/fail/error/non_pass; non_pass는 fail과 error를 함께 묶는다."""

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

    @abstractmethod
    async def get_job(self, session: AtworksSessionContext, job_id: str) -> JobSpec | None:
        """id로 job 1건 조회. 상태(staged/applied/discarded) 무관 — 감사 이력 포함."""

    @abstractmethod
    async def applied_jobs(self, session: AtworksSessionContext) -> list[JobSpec]:
        """승인(applied) 상태인 job 전체. 스케줄러가 매 tick마다 순회하는 대상."""

    @abstractmethod
    async def all_jobs(self, session: AtworksSessionContext) -> list[JobSpec]:
        """pending + applied job (Jobs 페이지가 보여주는 전체). discarded는 빠진다."""

    @abstractmethod
    async def runs_by_ids(self, session: AtworksSessionContext, run_ids: list[str]) -> list[RunResult]:
        """id 목록으로 실행 이력 조회(순서 무관, 없는 id는 건너뛴다). 리포트가 job.run_ids로 실행
        레코드를 모을 때 쓴다."""

    @abstractmethod
    async def record_execution(
        self, session: AtworksSessionContext, job_id: str, run_ids: list[str]
    ) -> JobSpec:
        """job의 실행 1회를 기록한다: run_ids가 이번 실행이 낸 결과(비어 있을 수 있다 — 실행이
        실패했거나 LATE 재평가가 상한을 넘겨 건너뛴 경우), runs_remaining을 정확히 1 줄인다."""

    @abstractmethod
    async def add_guardrail_note(self, session: AtworksSessionContext, job_id: str, note: str) -> JobSpec:
        """job의 guardrail_notes에 note 한 줄을 남긴다. 스케줄러가 실행/리포트 실패를 기록할 때 쓴다."""

    # -- 실행 (스케줄러가 부른다, LLM 경로 아님) ------------------------------------------
    @abstractmethod
    async def execute_job_once(self, session: AtworksSessionContext, job_id: str) -> list[RunResult]:
        """job의 api_ids(또는 LATE면 select_where 재평가)를 target_env에 1회 실행하고 결과를 돌려준다.

        계약: 이 호출 하나가 정확히 한 번의 실행이다. 구현은 결과와 무관하게(빈 리스트를 내더라도)
        ``record_execution``을 정확히 한 번 호출해 job의 남은 실행 횟수를 소비해야 한다 — 그러지
        않으면 스케줄러의 ``due_at(index)``가 앞으로 나아가지 않고 같은 job이 매 tick마다 실제
        target_env를 향해 다시 실행된다(R29가 막으려던 바로 그 루프). 예외로 남는 경우는 딱 하나,
        ``runs_remaining``이 이미 0이어서 애초에 소비할 슬롯이 없을 때뿐이다 — 그때는 아무 것도
        기록하지 않고 빈 리스트를 돌려준다. apply 이후 guardrail이 다시 걸린 실행 시도는 빈 결과와
        함께 ``add_guardrail_note``로 이유를 남기고, 그 시도 역시 슬롯을 소비한다."""

    # -- 선택 --------------------------------------------------------------------------
    async def get_context(self, session: AtworksSessionContext) -> dict[str, Any] | None:
        """요청별 컨텍스트(프로젝트명, 허용 계, 최근 실행 요약 카운트). 동적 프롬프트 블록에 들어간다."""
        return None
