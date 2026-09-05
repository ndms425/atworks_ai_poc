"""AtworksBackend: aTworks Java BE와의 유일한 접점. 읽기는 자유, stage_job은 제안만 기록,
apply_job만 실제 상태를 바꾼다(승인된 job의 실행 예약/즉시 실행). 모든 메서드는 서버 측
credential로 aTworks를 호출하고, 모델은 결과만 본다. MerchantBackend 미러."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from .jobs import JobDraft
from .profiles import ProfileDraft
from .rules import FormatBatchDraft, FormatDefinition, RuleDraft, RuleImpact, ValidationRule
from .types import (
    ActorKind,
    ApiSpec,
    AtworksSessionContext,
    ComparisonProfile,
    FormatBatch,
    JobSpec,
    RuleRecommendation,
    RunResult,
)


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
        non_pass는 fail과 error를 함께 묶는다(트리아지 모집단).

        A REST adapter must honor two obligations the Mock backend already does: results are
        ordered newest ``executed_at`` first (callers truncate to ``limit`` on that assumption),
        and every ``executed_at`` is timezone-aware (a naive value compared against an aware
        ``since``/``updated_after``/``failed_since`` raises ``TypeError`` downstream)."""

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
        self, session: AtworksSessionContext, job_id: str, run_ids: list[str], schedule_index: int | None
    ) -> JobSpec:
        """job의 실행 1회를 기록한다: run_ids가 이번 실행이 낸 결과(비어 있을 수 있다 — 실행이
        실패했거나 LATE 재평가가 상한을 넘겨 건너뛴 경우), ``executions``를 정확히 1 늘린다.
        ``schedule_index``는 이번 실행이 소비한 스케줄의 인덱스다(run_now면 ``None``) — 여러
        스케줄이 각자의 ``done``을 따로 세기 때문에 이 값 없이는 어느 회차가 소비됐는지 알 수 없다."""

    @abstractmethod
    async def add_guardrail_note(self, session: AtworksSessionContext, job_id: str, note: str) -> JobSpec:
        """job의 guardrail_notes에 note 한 줄을 남긴다. 스케줄러가 실행/리포트 실패를 기록할 때 쓴다."""

    # -- 검증 규칙 (propose → preview → approve → apply; effective_from 이후만 적용) --------
    @abstractmethod
    async def stage_rule(
        self, session: AtworksSessionContext, draft: RuleDraft, actor_kind: ActorKind
    ) -> ValidationRule: ...

    @abstractmethod
    async def get_pending_rules(self, session: AtworksSessionContext) -> list[ValidationRule]: ...

    @abstractmethod
    async def apply_rule(self, session: AtworksSessionContext, rule_id: str) -> ValidationRule:
        """승인된 규칙을 발효시킨다. REST 구현의 의무: 이 호출 시점의 timestamp를
        ``effective_from``에 찍어야 한다 — 그 이전에 실행된 run은 절대 건드리지 않고, 그 이후의
        실행만 이 규칙을 평가받는다(과거는 안 건드린다는 불변식은 여기서 시작한다).

        이 호출은 host 승인 마크를 통과한 뒤에만 실행되므로(``require_host_approval``), 규칙이
        ``save_format_as``와 ``pattern``을 함께 가진 raw-pattern 포맷 규칙이면 그 승인은 포맷
        라이브러리 승격도 함께 승인한 것이다 — 구현은 발효 직후 그 이름으로 포맷 라이브러리에
        더해야 한다(이름/패턴 충돌이나 라이브러리 만원이면 조용히 건너뛰고 규칙에 guardrail note를
        남긴다; 승격 실패는 규칙 적용 자체를 실패시키지 않는다). 라이브러리 추가는 inert하므로 어떤
        run 판정도 바꾸지 않는다."""

    @abstractmethod
    async def discard_rule(
        self, session: AtworksSessionContext, rule_id: str, actor_kind: ActorKind
    ) -> ValidationRule: ...

    @abstractmethod
    async def list_rules(
        self, session: AtworksSessionContext, api_id: str | None = None
    ) -> list[ValidationRule]:
        """api_id가 주어지면 그 API의 규칙만, 아니면 전체(상태 무관)."""

    @abstractmethod
    async def simulate_rule(self, session: AtworksSessionContext, draft: RuleDraft) -> RuleImpact:
        """읽기 전용: 아무 것도 쓰지 않는다 — 규칙 레저에도 실행 이력에도 흔적을 남기지 않는다.
        최근 실행 중 draft.param에 대한 입력값을 복원할 수 있는 것만 세어(``known_inputs``) 그 중
        드래프트가 실패시켰을 것을 ``would_fail``로 센다; 복원 불가능한 나머지는
        ``excluded_unknown``이다."""

    # -- 값 비교 프로파일 (propose → approve → apply; effective_from 이후에도 과거 판정은 안 건드림) --
    @abstractmethod
    async def stage_profile(
        self, session: AtworksSessionContext, draft: ProfileDraft, actor_kind: ActorKind
    ) -> ComparisonProfile: ...

    @abstractmethod
    async def get_pending_profiles(self, session: AtworksSessionContext) -> list[ComparisonProfile]: ...

    @abstractmethod
    async def apply_profile(self, session: AtworksSessionContext, profile_id: str) -> ComparisonProfile:
        """승인된 프로파일을 발효시킨다. 구현 의무 둘: (1) 이 호출 시점의 timestamp를
        ``effective_from``에 찍는다 — 과거 비교 결과는 절대 다시 판정하거나 고치지 않는다(불변식은
        여기서 시작한다). (2) 대상 job의 저장된 응답 바디로부터 parity 리포트를 이 프로파일의
        ignore path로 **재-diff**한다 — 새 run은 하나도 만들지 않는다. REST 구현은 자기 쪽 리포트
        저장소를 동등하게 갱신해야 한다."""

    @abstractmethod
    async def discard_profile(
        self, session: AtworksSessionContext, profile_id: str, actor_kind: ActorKind
    ) -> ComparisonProfile: ...

    @abstractmethod
    async def list_profiles(
        self, session: AtworksSessionContext, job_id: str | None = None
    ) -> list[ComparisonProfile]:
        """job_id가 주어지면 그 job의 프로파일만, 아니면 전체(상태 무관)."""

    # -- 포맷 라이브러리 (내장 5개 + 저장된 항목; format_library.add와 동일한 dedup 규약) ----
    @abstractmethod
    async def get_format(self, session: AtworksSessionContext, name: str) -> FormatDefinition | None:
        """이름 하나로 라이브러리 항목 조회(내장 또는 저장). 없으면 None — 크래시하지 않는다."""

    @abstractmethod
    async def list_formats(self, session: AtworksSessionContext) -> list[FormatDefinition]:
        """라이브러리 전체(내장 5개 + 저장된 항목)."""

    @abstractmethod
    async def save_format(self, session: AtworksSessionContext, defn: FormatDefinition) -> tuple[bool, str | None]:
        """``FormatLibrary.add``와 같은 계약: (added, skip_reason). 이름 또는 동일 패턴으로 dedup한다."""

    # -- 포맷 배치 (propose → approve → apply; 라이브러리 포맷은 inert이라 승인 1회로 충분) ------
    @abstractmethod
    async def stage_format_batch(
        self, session: AtworksSessionContext, draft: FormatBatchDraft, actor_kind: ActorKind
    ) -> FormatBatch:
        """각 항목의 outcome(new/duplicate/invalid)을 계산해 FormatBatch로 저장한다. 아직 라이브러리에
        아무것도 더하지 않는다 — apply만 outcome이 new인 항목을 더한다."""

    @abstractmethod
    async def get_pending_format_batches(self, session: AtworksSessionContext) -> list[FormatBatch]: ...

    @abstractmethod
    async def apply_format_batch(self, session: AtworksSessionContext, batch_id: str) -> FormatBatch:
        """승인된 배치를 적용한다: outcome이 new인 항목만 라이브러리에 더하고 duplicate/invalid는
        건드리지 않는다. new가 0건이어도 적용은 되고(라이브러리는 안 바뀐다) — 그 결과를 보고한다."""

    @abstractmethod
    async def discard_format_batch(
        self, session: AtworksSessionContext, batch_id: str, actor_kind: ActorKind
    ) -> FormatBatch: ...

    # -- 추천 (읽기 전용, 판정 없음) -----------------------------------------------------
    @abstractmethod
    async def find_apis_with_param(self, session: AtworksSessionContext, param: str) -> list[ApiSpec]:
        """param을 선언한 API 중, 그 API에 이미 적용된(``applied``) format 규칙이 그 param에 대해
        없는 것만 돌려준다 — 한 API에 포맷을 적용한 뒤 '다른 API에도 이 이름의 param이 있는데
        확장할까요'를 결정론으로 묻기 위한 카탈로그 스캔. 아무것도 쓰지 않는다."""

    @abstractmethod
    async def recommend_rules_for_api(
        self, session: AtworksSessionContext, api_id: str
    ) -> list[RuleRecommendation]:
        """대상 API의 param마다: 이미 적용된 규칙이 있으면 건너뛰고, 없으면 같은 이름의 param을 가진
        *다른* API에 이미 적용된 규칙들을 제안(``RuleRecommendation``)으로 돌려준다. 대응하는 peer가
        전혀 없는 param은 아무것도 제안하지 않는다 — param 이름만으로 제약을 지어내지 않는다(no
        fabrication). 각 제안은 operator가 개별적으로 ``stage_rule``에 실어 스테이징하고 승인한다.
        읽기 전용: 아무것도 쓰지 않는다."""

    # -- 실행 (스케줄러가 부른다, LLM 경로 아님) ------------------------------------------
    @abstractmethod
    async def execute_job_once(
        self, session: AtworksSessionContext, job_id: str, schedule_index: int | None
    ) -> list[RunResult]:
        """job의 매트릭스를 1회 실행하고 결과를 돌려준다: ``target_envs`` × (``test_data`` 또는
        바인딩 없음) × ``api_ids``(LATE면 ``select_where`` 재평가)의 조합마다 run 1건. 각 run은
        자기 계를 ``target_env``에, 자기 데이터 세트를 ``test_data_label``에 달고 나오므로 한 번의
        실행이 낸 두 run이 같은 식별자로 겹치지 않는다.

        구현은 API 목록(LATE/FROZEN 해석 후)이 확정된 뒤 ``enforce_execution_matrix``를 실행 1회당
        한 번 호출해 크기 상한을 다시 매겨야 한다 — LATE 선택은 stage 이후 늘어날 수 있어 그때의
        확인만으로는 부족하다. 위반이 있으면 각각을 ``add_guardrail_note``로 남기고, run은 하나도
        내지 않은 채 슬롯만 소비한다(``record_execution``에 빈 ``run_ids``와 이 실행의
        ``schedule_index``를 실어서).

        계약: 이 호출 하나가 정확히 한 번의 실행이다. 구현은 결과와 무관하게(빈 리스트를 내더라도)
        ``record_execution``을 정확히 한 번, 받은 ``schedule_index``와 함께 호출해 job의 남은 실행
        횟수를 소비해야 한다 — 그러지 않으면 스케줄러의 ``due_at``이 앞으로 나아가지 않고 같은 job이
        매 tick마다 실제 target을 향해 다시 실행된다(R29가 막으려던 바로 그 루프). 예외로 남는 경우는
        딱 하나, ``remaining_executions``가 이미 0이어서 애초에 소비할 슬롯이 없을 때뿐이다 — 그때는
        아무 것도 기록하지 않고 빈 리스트를 돌려준다. LATE 재평가와 상한 초과 스킵은 계마다가 아니라
        실행 1회당 한 번 적용된다. apply 이후 guardrail이 다시 걸린 실행 시도는 빈 결과와 함께
        ``add_guardrail_note``로 이유를 남기고, 그 시도 역시 슬롯을 소비한다."""

    # -- 선택 --------------------------------------------------------------------------
    async def get_context(self, session: AtworksSessionContext) -> dict[str, Any] | None:
        """요청별 컨텍스트(프로젝트명, 허용 계, 최근 실행 요약 카운트). 동적 프롬프트 블록에 들어간다."""
        return None
