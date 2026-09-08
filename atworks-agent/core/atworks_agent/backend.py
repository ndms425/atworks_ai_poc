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
    AggregateQuery,
    AliasProposal,
    ApiSpec,
    ApiWatermark,
    AskEntry,
    AskOutcome,
    AtworksSessionContext,
    AuditEntry,
    CellState,
    ComparisonProfile,
    FormatBatch,
    GrowthSummary,
    Insights,
    JobSpec,
    OperatorProfile,
    Page,
    QueryFilters,
    QueryResult,
    QuerySpec,
    RuleRecommendation,
    RunGroup,
    RunResult,
    RunsQuery,
    SavedQuestion,
    SavedQuestionStatus,
    ScopeSummary,
    VocabularyEntry,
    VocabularyStatus,
)


class AtworksBackend(ABC):
    # -- 읽기 ------------------------------------------------------------------------
    @abstractmethod
    async def search_apis(
        self, session: AtworksSessionContext, query: str = "", group: str | None = None,
        updated_after: datetime | None = None, cursor: str | None = None, limit: int = 20,
        path_prefix: str | None = None,
    ) -> Page[ApiSpec]:
        """텍스트·갱신일·그룹으로 API 스펙 검색. updated_after는 '지난 1주일 업데이트' 류의 리졸버.

        ``path_prefix``는 경로 prefix 전용 술어다(``related_to`` 영향도 스캔): ``query``와 달리
        **앞에 고정**되어 ``<prefix>`` 자신과 ``<prefix>/...``만 맞는다 — 자유 텍스트 검색을
        넓히지 않는다(REST 구현은 경로 인덱스로 처리한다).

        REST 구현 의무: ``cursor``는 서버가 만든 불투명 문자열이다(다른 프로세스가 만든 값을 절대
        직접 해석하지 않는다) — ``updated_at`` DESC, ``api_id`` DESC 순서에서 이 커서 이후의 다음
        페이지를 낸다. ``Page.total``은 이 필터를 적용한 전체 건수, limit과 무관하다."""

    @abstractmethod
    async def get_api(self, session: AtworksSessionContext, api_id: str) -> ApiSpec | None: ...

    @abstractmethod
    async def get_apis(self, session: AtworksSessionContext, api_ids: list[str]) -> list[ApiSpec]:
        """id 목록으로 스펙을 **한 번에** 읽는다(없는 id는 빠진다, 순서 보장 없음). 이미 id를 손에
        든 선택 경로(``resolve_select_where``의 ``failed_since`` 가지)가 ``get_api``를 id마다 한 번씩
        부르던 N+1을 없앤다 — 한 job의 상한만큼, 즉 최대 ``max_apis_per_job + 1`` 회였다.
        REST 구현 의무: 한 번의 조회로 답하고, IN 목록은 서버가 알아서 나눠 던진다."""

    @abstractmethod
    async def list_runs(self, session: AtworksSessionContext, q: RunsQuery) -> Page[RunResult]:
        """실행 이력. q.status는 pass/fail/error/non_pass. 판정값은 aTworks DSL이 낸 그대로다.
        non_pass는 fail과 error를 함께 묶는다(트리아지 모집단).

        REST 구현 의무: 결과는 ``executed_at`` DESC, ``run_id`` DESC 순서다(동률을 run_id로 깨
        페이지 경계가 안정적이다). ``q.cursor``는 서버가 만든 불투명 문자열 — 이 순서에서 그 지점
        이후의 다음 페이지를 낸다. ``Page.total``은 이 필터를 적용한 전체 건수, ``q.limit``과
        무관하다. 모든 ``executed_at``은 timezone-aware여야 한다(naive 값을 aware ``q.since``와
        비교하면 하위에서 ``TypeError``가 난다).

        ``q.archived``가 True면 **콜드 파티션**(``retention_hot_days``를 넘겨 이관된 실행)을 읽는다 —
        같은 술어, 같은 keyset 순서, 같은 봉투로 다른 파티션을 볼 뿐이다. 기본값 False가 핫
        파티션이고, 콜드는 명시적으로 요청해야만 보인다(spec §6)."""

    @abstractmethod
    async def get_run(self, session: AtworksSessionContext, run_id: str) -> RunResult | None: ...

    @abstractmethod
    async def count_runs(
        self, session: AtworksSessionContext, since: datetime | None = None, until: datetime | None = None,
        status: str | None = None, api_id: str | None = None,
    ) -> int:
        """triage 카드의 모수(population). list_runs의 limit과 무관하게 전체 건수.
        status는 pass/fail/error/non_pass; non_pass는 fail과 error를 함께 묶는다. 이 필터를 적용한
        전체 건수이며 정렬은 하지 않는다(population 집계에는 순서가 필요 없다)."""

    @abstractmethod
    async def count_runs_by_job(
        self, session: AtworksSessionContext, since: datetime | None = None, until: datetime | None = None,
    ) -> dict[str, int]:
        """창 안에서 실행된 job별 run 건수(``{job_id: n}``). 일일 브리핑의 "어제 어떤 job이 몇 건
        돌았나"를 run 목록을 만들지 않고 답하기 위한 읽기다.

        REST 구현 의무: ``executed_at`` 범위로 필터한 뒤 ``job_id``로 GROUP BY 한 정확한 건수이며,
        job_id가 없는 run은 빠진다. 표본이 아니다 — 절단하지 않는다."""

    @abstractmethod
    async def aggregate_runs(self, session: AtworksSessionContext, q: AggregateQuery) -> list[RunGroup]:
        """q.group_by로 실행 이력을 묶는다(원인별/계별/API별). 숫자는 전부 host가 계산하고, 모델은
        어떤 그룹을 보여줄지만 고른다. q.scope_api_ids가 주어지면 그 API로만 스코프를 좁힌다.
        q.status가 주어지면 각 그룹은 그 판정에 해당하는 건수만 보고한다(non_pass = fail+error).
        q.scope_operator가 주어지면 그 오퍼레이터가 창 안에서 실행한 API로만 좁힌다(아래).
        q.include_run_ids=False면 run_ids를 채우지 않는다(카운터만 쓰는 호출자용).
        q.order_by는 **어느 limit개를 남길지**만 정한다(개수/라벨/파생 필드는 둘 다 같다):
        ``"failures"``(기본) = ``(fail+error) DESC, count DESC, key ASC``,
        ``"transitions"`` = ``transitions DESC, (fail+error) DESC, key ASC``.

        REST 구현 의무(scale spec §9): 이 읽기는 **표본이 아니다** — 창 안의 모든 API가 그룹으로
        나와야 하고(롤업 합산), 정렬은 위 두 순서 중 ``q.order_by``가 고른 것, 잘림은 오직
        ``q.limit``이다. 정렬·LIMIT는 **집계 질의 안에서** 처리한다(GROUP BY … ORDER BY … LIMIT):
        전체 그룹을 애플리케이션으로 가져와 정렬하면 셀 축에서 프로젝트의 셀 수만큼 행이 넘어온다.
        ``"transitions"`` 순서가 없으면 "실패는 드물지만 자주 뒤집히는 셀"은 카운트만 되고
        (``summarize_insights``) 이름은 끝내 나오지 않는다 — flaky 후보가 요구하는 순서다.
        ``run_ids``는 그룹당 최신 50건 이하의 증거 표본일 뿐 모집단이 아니며,
        ``api_count``는 한 키가 걸친 API의 **참** 개수(len(run_ids)가 아니다)다.
        ``q.scope_operator``는 **서버 측 조인/필터**로 구현한다 — 오퍼레이터의 API id 목록을 받아
        ``scope_api_ids``에 싣는 방식은 금지다(그 목록엔 상한이 있고, 상한은 곧 커버리지 구멍이다).
        스코프 = "이 오퍼레이터가 창 안에서 실행한 API"이고, 창의 하한은 이 질의의 ``since``다."""

    @abstractmethod
    async def query_runs(self, session: AtworksSessionContext, spec: QuerySpec) -> QueryResult:
        """자가발전 질의(self-growth spec §3-5) 1회. ``spec``은 모델이 채운 **구조화** 질의이고,
        SQL은 서버가 컴파일한다 -- 모델이 쓴 SQL 문자열은 이 경로 어디에도 없다. 읽기 전용이다:
        무엇도 스테이징하지 않고, 무엇도 판정하지 않으며, 숫자는 전부 서버가 계산한다.

        REST 구현 의무(자가발전 spec §4):
        * **자기 물질화 데이터 위에서 컴파일한다.** 소스 선택은 spec §4의 표 그대로다 -- 차원이
          없거나 ``api``/``path_*``/``method``/``api_group``/``target_env``/``test_data_label``/
          ``day``/``week``면 일별 롤업 ⋈ API 카탈로그, ``failed_rule``/``http_status`` 단독이고 API
          범위 필터가 없으면 키 축 롤업, ``executed_by`` 단독(또는 ``day``/``week``와만 조합)이면
          오퍼레이터×일 롤업, 그 밖의 ``executed_by`` 조합과 ``executed_by`` **필터**는 run
          테이블이다. ``QueryResult.source``는 실제로 읽은 소스의 이름이어야 한다 -- 카드와
          벤치가 그 문자열을 읽는다.
        * **창**: ``filters.window_days``가 있으면 ``[now - window_days, now]``, ``since``/
          ``until``이 있으면 그대로, 둘 다 없으면 서버의 기본 창(``max_aggregate_window_days``).
          ``day``/``week``와 창 경계는 서버의 ``briefing_tz`` 기준 로컬 날짜다.
        * **정렬과 절단은 집계 질의 안에서** (``ORDER BY … LIMIT``). 애플리케이션이 전체 그룹을
          받아 정렬하면 셀 축에서 프로젝트 크기만큼 행이 넘어온다 -- scale 브랜치의 상시 규칙.
        * ``total_groups``는 **LIMIT 전** 그룹 수, ``population``은 필터를 적용한 run COUNT.
          둘 다 필터 적용 후의 값이고 ``limit``과 무관하다 -- 표본이 아니다.
        * ``rows[*].api_ids``(≤20) / ``run_ids``(≤5, 최신순)는 **증거 표본**이다. 호출자가 그
          id를 인용할 수 있게 서버가 채우며(그래서 실행기가 세션 ``seen_*``에 기억한다),
          모집단이 아니다. ``include_samples=False``면 비운다.
        * 어떤 측정값을 이 소스가 아예 못 내면 그 자리는 **NULL**로 둔다 -- 0으로 채우면 아무도
          계산하지 않은 수를 발표하는 것이다. ``compare_previous_window``도 같다: 바로 앞
          같은 길이의 창을 같은 스펙으로 돌려 그룹 키 기준으로 이어 붙이고, 못 낸 측정값은
          ``_prev``/``_delta`` 둘 다 NULL이다.
        * **응답 바디는 절대 싣지 않는다.** 이 읽기는 집계와 id뿐이다(scale spec의 바디 규율)."""

    @abstractmethod
    async def summarize_insights(
        self, session: AtworksSessionContext, since: datetime, until: datetime | None = None,
        scope_operator: str | None = None,
    ) -> Insights:
        """창 안의 flaky/regression 의심 **개수** 둘. Home 타일(``/runs/insights``)과 일일 브리핑이
        읽는다.

        REST 구현 의무: 그룹 목록을 받아 세는 게 아니라 **집계 질의 2개**로 답한다 — (1) flaky =
        창 안 셀(api×env×test_data) 중 전이 합계가 ``flaky_min_transitions`` 이상인 셀의 개수,
        (2) regression = ``last_pass_at < api.updated_at <= first_non_pass_at``이면서
        ``first_non_pass_at >= since``인 API의 개수. 상위 N개 그룹을 세는 구현은 **틀린다**:
        실패 건수로 정렬한 상위 N에 "실패 1건짜리 불안정 셀"은 영영 들어오지 못한다.
        ``scope_operator``는 ``aggregate_runs``와 같은 서버 측 스코프 술어다."""

    @abstractmethod
    async def current_state(
        self, session: AtworksSessionContext, scope_api_ids: list[str] | None = None,
        scope_operator: str | None = None,
    ) -> list[CellState]:
        """api_id × target_env × test_data_label 셀마다 최신 상태 1행(물질화 뷰). transitions_total은
        그 셀의 시간순 상태 시퀀스에서 계산한다.

        REST 구현 의무: ``scope_operator``는 "그 오퍼레이터가 최근 ``scope_window_days`` 안에 실행한
        API"라는 **서버 측** 술어다(오퍼레이터×API 색인 조인). 이 읽기엔 질의 창이 없으므로 하한은
        서버가 가진 ``scope_window_days``이고, 그래야 ``operator_scope``의 스코프 정의와 한 벌로
        유지된다. 오퍼레이터의 id 목록을 받아 ``scope_api_ids``로 거르는 구현은 금지다."""

    @abstractmethod
    async def watermarks(
        self, session: AtworksSessionContext, api_ids: list[str] | None = None,
        first_non_pass_since: datetime | None = None, last_non_pass_since: datetime | None = None,
        scope_operator: str | None = None,
    ) -> list[ApiWatermark]:
        """API별 pass/non-pass 워터마크. flaky/regression 판정을 전체 히스토리 재스캔 없이 낸다.
        ``first_non_pass_since``는 "첫 실패가 이 시각 이후"(새로 깨진 API), ``last_non_pass_since``는
        "이 시각 이후 실패가 하나라도 있음"(``select_where.failed_since``가 뜻하는 바로 그 집합)이다.
        ``api_updated_at``은 카탈로그의 ``ApiSpec.updated_at``을 그대로 실어 회귀 규칙
        (``last_pass_at < api.updated_at <= first_non_pass_at``)이 조인 없이 풀리게 한다.

        REST 구현 의무: ``scope_operator``는 ``current_state``와 같은 서버 측 스코프 술어다 — "그
        오퍼레이터가 최근 ``scope_window_days`` 안에 실행한 API"를 조인/필터로 풀고, 오퍼레이터의
        api_id 목록을 받아 ``api_ids``에 싣는 식으로는 절대 구현하지 않는다."""

    @abstractmethod
    async def operator_scope(
        self, session: AtworksSessionContext, operator_id: str, window_days: int
    ) -> ScopeSummary:
        """operator_id가 최근 window_days일 동안 건드린 API 모집단 요약. api_ids는 표본(<=100),
        total이 실제 개수."""

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
        """승인(applied) 상태인 job 전체 — 소진된 것까지 포함한다. 스케줄러가 매 tick마다 순회하는
        대상은 이제 ``active_jobs``(남은 슬롯이 있는 것만)다."""

    @abstractmethod
    async def all_jobs(
        self, session: AtworksSessionContext, status: str | None = None,
        cursor: str | None = None, limit: int = 50,
    ) -> Page[JobSpec]:
        """pending + applied job (Jobs 페이지가 보여주는 전체). discarded는 빠진다. status가 주어지면
        그 상태(staged/applied)로 더 좁힌다. REST 구현 의무: ``created_at`` DESC, ``job_id`` DESC
        순서, ``cursor``는 서버가 만든 불투명 문자열, ``Page.total``은 이 필터를 적용한 전체 건수."""

    @abstractmethod
    async def active_jobs(self, session: AtworksSessionContext) -> list[JobSpec]:
        """applied 상태이고 remaining_executions > 0인 job만 — 스케줄러가 아직 소비할 슬롯이 남은
        job을 빠르게 추리는 데 쓴다(다 소진된 applied job까지 매 tick 다시 스캔하지 않도록)."""

    @abstractmethod
    async def runs_by_ids(self, session: AtworksSessionContext, run_ids: list[str]) -> list[RunResult]:
        """id 목록으로 실행 이력 조회(순서 무관, 없는 id는 건너뛴다). ``job.recent_run_ids``(최근 50개)
        처럼 **이미 손에 든 id 목록**을 레코드로 바꿀 때 쓴다 — 리포트는 더 이상 이 경로가 아니라
        ``list_runs(RunsQuery(job_id=...))`` 페이징으로 job의 전체 실행을 모은다(scale spec §4:
        ``job.run_ids``는 더 이상 자라지 않는다)."""

    @abstractmethod
    async def record_execution(
        self, session: AtworksSessionContext, job_id: str, run_ids: list[str], schedule_index: int | None
    ) -> JobSpec:
        """job의 실행 1회를 기록한다: run_ids가 이번 실행이 낸 결과(비어 있을 수 있다 — 실행이
        실패했거나 LATE 재평가가 상한을 넘겨 건너뛴 경우, 인입이 중간에 실패했다면 **커밋된 청크의
        run_id만** 실린다), ``executions``를 정확히 1 늘린다.
        ``schedule_index``는 이번 실행이 소비한 스케줄의 인덱스다(run_now면 ``None``) — 여러
        스케줄이 각자의 ``done``을 따로 세기 때문에 이 값 없이는 어느 회차가 소비됐는지 알 수 없다.
        REST 어댑터 의무(물질화, spec §4): 한 실행이 낸 run들은 인입 시 ``current_state``/``rollup_day``/
        ``api_watermark``/``operator_api``로 접혀 들어가며 전환(transitions) 카운터는 인입 순서를 기준으로
        증가한다 — run은 **실행 순서대로** 인입되어야 하고, 이미 물질화된 시점보다 오래된 run을 뒤늦게
        인입하면 전환 수가 왜곡된다(재시도가 안전한 것은 run_id가 **실행에서 유도**되어 중복 무시가
        먹힐 때뿐이다 — ``execute_job_once`` 참고). ``job.run_ids``는 더 자라지 않는다;
        ``run_count``/``recent_run_ids``(최근 50)가 그 자리를 대신하고, 리포트는
        ``list_runs(RunsQuery(job_id=...))``로 실행 이력을 읽는다."""

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
    async def get_rule(self, session: AtworksSessionContext, rule_id: str) -> ValidationRule | None:
        """rule_id 하나를 그대로 읽는다 — 없으면 None. 호스트의 승인/폐기 라우트가 아직 이 세션이
        모르는 rule을 클릭 직전에 기억시킬 때 쓰는 **단건 조회**다. 목록 페이지를 크게 떠서 그
        안에서 찾는 방식(list_rules(limit=1000))은 그 한도를 넘긴 오래된 rule을 못 찾는 조용한
        구멍이었다. REST 구현 의무: 인덱스된 단건 조회여야 하고, 목록의 정렬/커서와 무관하다."""

    @abstractmethod
    async def list_rules(
        self, session: AtworksSessionContext, api_id: str | None = None, status: str | None = None,
        cursor: str | None = None, limit: int = 50,
    ) -> Page[ValidationRule]:
        """api_id가 주어지면 그 API의 규칙만, status가 주어지면 그 상태로 더 좁힌다(둘 다 없으면
        전체, 상태 무관). REST 구현 의무: ``created_at`` DESC, ``rule_id`` DESC 순서, ``cursor``는
        서버가 만든 불투명 문자열, ``Page.total``은 이 필터를 적용한 전체 건수."""

    @abstractmethod
    async def simulate_rule(self, session: AtworksSessionContext, draft: RuleDraft, window_days: int) -> RuleImpact:
        """읽기 전용: 아무 것도 쓰지 않는다 — 규칙 레저에도 실행 이력에도 흔적을 남기지 않는다.
        window_days 안의 실행만 본다(더 오래된 run은 애초에 셈에서 빠진다). 최근 실행 중
        draft.param에 대한 입력값을 복원할 수 있는 것만 세어(``known_inputs``) 그 중 드래프트가
        실패시켰을 것을 ``would_fail``로 센다; 복원 불가능한 나머지는 ``excluded_unknown``이다."""

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
    async def get_profile(
        self, session: AtworksSessionContext, profile_id: str
    ) -> ComparisonProfile | None:
        """profile_id 하나를 그대로 읽는다 — 없으면 None. get_rule과 같은 이유의 단건 조회다."""

    @abstractmethod
    async def list_profiles(
        self, session: AtworksSessionContext, job_id: str | None = None, status: str | None = None,
        cursor: str | None = None, limit: int = 50,
    ) -> Page[ComparisonProfile]:
        """job_id가 주어지면 그 job의 프로파일만, status가 주어지면 그 상태로 더 좁힌다(둘 다 없으면
        전체, 상태 무관). REST 구현 의무: ``created_at`` DESC, ``profile_id`` DESC 순서, ``cursor``는
        서버가 만든 불투명 문자열, ``Page.total``은 이 필터를 적용한 전체 건수."""

    @abstractmethod
    async def get_parity_report(self, session: AtworksSessionContext, job_id: str) -> dict | None:
        """저장된 parity 블록(targets/rows/clusters/counts)을 job_id로 읽는다 — 판정 없는 읽기, 새 run 없음."""

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
    async def get_format_batch(self, session: AtworksSessionContext, batch_id: str) -> FormatBatch | None:
        """batch_id 하나를 그대로 읽는다 — 없으면 None. ``get_rule``/``get_profile``과 같은 이유의
        단건 조회다. 호스트의 승인 라우트가 **대기 목록만** 훑어 찾는 방식은 이미 적용/폐기된
        배치를 못 찾았고, 그러면 클릭이 provenance 게이트에서 막혔다 — 상태와 무관한 단건 조회여야
        한다. REST 구현 의무: 인덱스된 단건 조회이며 목록의 정렬/커서와 무관하다."""

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
    async def find_apis_with_param(
        self, session: AtworksSessionContext, param: str, cursor: str | None = None, limit: int = 20,
    ) -> Page[ApiSpec]:
        """param을 선언한 API 중, 그 API에 이미 적용된(``applied``) format 규칙이 그 param에 대해
        없는 것만 돌려준다 — 한 API에 포맷을 적용한 뒤 '다른 API에도 이 이름의 param이 있는데
        확장할까요'를 결정론으로 묻기 위한 카탈로그 스캔. 아무것도 쓰지 않는다. REST 구현 의무:
        ``updated_at`` DESC, ``api_id`` DESC 순서, ``cursor``는 서버가 만든 불투명 문자열,
        ``Page.total``은 이 필터를 적용한 전체 건수."""

    @abstractmethod
    async def recommend_rules_for_api(
        self, session: AtworksSessionContext, api_id: str, limit: int = 20,
    ) -> list[RuleRecommendation]:
        """대상 API의 param마다: 이미 적용된 규칙이 있으면 건너뛰고, 없으면 같은 이름의 param을 가진
        *다른* API에 이미 적용된 규칙들을 제안(``RuleRecommendation``)으로 돌려준다. 대응하는 peer가
        전혀 없는 param은 아무것도 제안하지 않는다 — param 이름만으로 제약을 지어내지 않는다(no
        fabrication). 각 제안은 operator가 개별적으로 ``stage_rule``에 실어 스테이징하고 승인한다.
        읽기 전용: 아무것도 쓰지 않는다."""

    @abstractmethod
    async def get_body(self, session: AtworksSessionContext, run_id: str) -> dict | None:
        """run 1건의 저장된 응답 바디만 읽는다(run_result 전체가 아니라) — parity 재-diff처럼 바디만
        필요한 경로가 나머지 필드를 실어 나르지 않게 한다. 없는 run이면 None."""

    @abstractmethod
    async def get_body_masked_paths(self, session: AtworksSessionContext, run_id: str) -> list[str]:
        """이 run의 저장된 바디에서 **캡처 시점 마스킹이 실제로 바꾼** JSON 경로들(``$.a.b[2]``
        모양, parity가 쓰는 그 표기). 마스킹된 게 없거나 바디 자체가 없으면 빈 리스트다.

        REST 구현 의무: 이 값은 **캡처 시점에 기록**해 두었다가 그대로 돌려줘야 한다 — 저장된
        바디를 나중에 다시 훑어 ``***``를 찾아내는 식은 안 된다(원래 값이 진짜 ``***``였을
        수도 있고, 무엇보다 마스킹 규칙은 그 뒤에 바뀔 수 있다). 마스킹은 단방향이라 마스킹된
        리프에서만 다른 두 바디는 둘 다 ``***``로 읽혀 **같아 보인다**; parity는 이 목록을 받아
        그 경로들을 판정에서 빼고 행의 근거(basis)를 ``masked``로 적는다. 이 읽기가 없으면
        리포트는 보지도 못한 값에 대해 "동등"이라고 말하게 된다."""

    # -- 감사 로그 (append-only, seq가 전역 순서) -----------------------------------------
    @abstractmethod
    async def audit(
        self, session: AtworksSessionContext, cursor: str | None = None, limit: int = 50,
    ) -> Page[AuditEntry]:
        """감사 로그를 최신순으로 읽는다. REST 구현 의무: ``at`` DESC, ``seq`` DESC 순서, ``cursor``는
        서버가 만든 불투명 문자열, ``Page.total``은 전체 건수."""

    @abstractmethod
    async def append_audit(
        self, session: AtworksSessionContext, action: str, target_kind: str, target_id: str
    ) -> None:
        """감사 로그에 한 행을 남긴다(append-only) — seq는 구현이 전역 순서로 부여한다."""

    # -- 질문 기록 ask_log (자가발전 spec §6: 한 턴 = 한 행) --------------------------------
    @abstractmethod
    async def record_ask(self, session: AtworksSessionContext, entry: AskEntry) -> AskEntry:
        """턴 하나의 기록을 남긴다. 호스트의 턴 종료 훅만 부른다 — 모델이 닿는 도구는 없다.

        REST 구현 의무(자가발전 spec §6):
        * **``turn_id``에 대해 멱등**이어야 한다(UNIQUE + 무시하는 삽입). 훅은 best-effort라 한
          턴에 두 번 불릴 수 있고, 그때 행이 둘이 되면 Growth 뷰가 세는 모든 비율이 틀어진다.
          이미 있는 행이 이기고 그대로 돌아온다 — 재시도가 과거를 바꾸지 못한다.
        * ``entry.question``은 **마스킹된 요약**(≤300자)이다. 원문 메시지를 저장해서는 안 된다:
          이 테이블은 대화 보관소가 아니라 "무엇을 물었나"의 집계 재료이고, 여기 담긴 예시는
          Growth 뷰가 그대로 화면에 보여준다.
        * ``outcome``/``intent``/``cluster_key``를 다시 계산하지 않는다. 그 셋은 호스트의
          결정론 분류기(``asklog.classify_turn``)가 이미 정했다 — 백엔드가 제 나름으로 다시
          매기면 같은 턴이 저장소마다 다르게 분류된다."""

    @abstractmethod
    async def list_asks(
        self, session: AtworksSessionContext, outcome: AskOutcome | None = None,
        cursor: str | None = None, limit: int = 50,
    ) -> Page[AskEntry]:
        """질문 기록을 최신순으로 읽는다. REST 구현 의무: ``at`` DESC, ``seq`` DESC 순서,
        ``cursor``는 서버가 만든 불투명 문자열, ``Page.total``은 ``outcome`` 필터를 적용한 뒤의
        건수이고 ``limit``과 무관하다."""

    @abstractmethod
    async def growth_summary(
        self, session: AtworksSessionContext, since: datetime
    ) -> GrowthSummary:
        """Growth 뷰 '이번 주' 타일. REST 구현 의무: 전부 **COUNT 질의**로 답한다 — 행 목록을
        받아 애플리케이션에서 세면 안 된다(그 목록엔 상한이 있고, 상한은 곧 틀린 비율이다).
        ``unmet_clusters``의 예시는 저장된 마스킹 요약 그대로다."""

    # -- 어휘 (자가발전 spec §7: commerce_common.memory 위의 조직 공용 어휘) ------------------
    # 이 6개 메서드가 공유하는 세 가지 의무 (REST 구현이 반드시 지킨다):
    #   1) **어휘는 프로젝트 공유다.** subject는 사람이 아니라 프로젝트다 — 한 사람이 확인한 용어를
    #      팀 전체가 쓰는 것이 이 기능의 전부이고, 그래서 저장 키에 사용자 id가 없다.
    #   2) **쓰기 필터를 통과한 값만 저장된다.** term과 fragment는 저장 직전에
    #      ``commerce_common.memory.validate_fact``(펜스 + ``MemoryWriteFilter``)를 지난다.
    #      9자리+ 숫자·IBAN·이메일 모양이면 ``MemoryWriteRejected``이고, 그때 이 계약은 예외를
    #      밖으로 내보내지 않고 거절을 돌려준다 — 거절 사유는 도구 결과의 한 문장이다.
    #   3) **모델의 컨텍스트에는 confirmed만 들어간다.** pending은 제안한 세션의 카드에서만 쓰이고
    #      (spec §2 조항 4), rejected는 쿨다운이 끝나기 전까지 다시 제안되지 않는다.
    #   4) **``memory_facts``는 확정된 term만 담는다.** fact는 ``confirm_alias``가 쓰고,
    #      ``reject_alias``/``delete_alias``와 자동 강등이 지운다 — 그래서 ``get_facts`` /
    #      ``search_facts``가 돌려주는 집합이 곧 confirmed 집합이고, 사람이 승인하지 않은 뜻이
    #      기억 저장소에 앉아 있는 상태가 존재하지 않는다. pending은 사이드카에만 있다.
    @abstractmethod
    async def propose_alias(
        self, session: AtworksSessionContext, term: str, fragment: QueryFilters,
        note: str | None = None,
    ) -> AliasProposal:
        """모델이 사용자 용어를 필터 조각으로 해석했을 때 남기는 제안 1건(``status=pending``).

        결과는 :class:`AliasProposal`이고 셋 중 하나다. ``proposed``는 **이 호출이 새 행을 썼을
        때만** — 카드가 확인을 묻는 것도 그때뿐이다. 이미 그 term의 행이 있으면(confirmed든,
        확인 대기 중인 pending이든, 냉각 중인 rejected든) ``existing``이고 **아무것도 쓰이지
        않는다**: 저장된 행을 그대로 담아 돌려주므로 도구 결과가 팀이 실제로 가진 것을 말한다.
        쓰기 필터가 막았거나(개인정보 모양) 조각이 모양이 아닌 대상·기간을 담고 있으면
        ``refused``이고, 그때도 사이드카 행도 fact도 남지 않는다.

        REST 구현 의무: term은 정규형으로 저장한다(``vocabulary.normalize_term``); 이미 있는
        term은 **덮어쓰지 않는다**(확정된 항목이 새 제안으로 pending이 되면 안 되고, 거부가
        남긴 쿨다운도 지워지면 안 된다); 쿨다운이 **지난** rejected 행은 예외로, 새 제안이 그
        행을 fresh pending으로 되살린다(카운터와 이력은 남는다) — 쿨다운에 끝이 있다는 것이
        그 필드의 뜻이다. ``fragment``에 ``since``/``until``/``window_days``/``api_ids``/
        ``executed_by``/``scope_operator``가 있으면 저장하지 않고 거절한다: 확정된 별칭은 팀
        전체의 컨텍스트에 실리므로 거기 기간이나 남의 오퍼레이터 id가 굳으면 안 된다."""

    @abstractmethod
    async def list_vocabulary(
        self, session: AtworksSessionContext, status: VocabularyStatus | None = None,
        cursor: str | None = None, limit: int = 50,
    ) -> Page[VocabularyEntry]:
        """어휘 목록(제안 최신순). REST 구현 의무: ``proposed_at`` DESC, ``term`` DESC 순서,
        ``cursor``는 서버가 만든 불투명 문자열, ``Page.total``은 ``status`` 필터 적용 후의 건수이고
        ``limit``과 무관하다."""

    @abstractmethod
    async def confirm_alias(
        self, session: AtworksSessionContext, term: str
    ) -> VocabularyEntry | None:
        """사람이 [예]를 눌렀다. ``confirmations + 1``, 누른 사람과 시각을 찍고 상태를 confirmed로.
        다른 오퍼레이터가 같은 term을 나중에 확인하면 카운터가 또 오른다 — 그 숫자가 §7의 자동
        강등 규칙에서 거부 수와 비교된다. 없는 term이면 ``None``(라우트는 404).

        **``memory_facts``의 fact를 쓰는 것은 여기다**(의무 4): 사람의 클릭이 그 뜻을 팀의 것으로
        만든 순간이지, 모델이 제안한 순간이 아니다."""

    @abstractmethod
    async def reject_alias(
        self, session: AtworksSessionContext, term: str
    ) -> VocabularyEntry | None:
        """사람이 [아니오]를 눌렀다. ``rejections + 1``, 상태는 rejected, ``cooldown_until``에
        ``now + vocabulary_cooldown_days``를 찍는다. **행은 남긴다**: 쿨다운이 곧 그 거부의
        기억이고, 행을 지우면 다음 턴에 같은 제안이 다시 올라온다. ``memory_facts``의 fact는
        **지운다**(의무 4) — 확정이 아닌 뜻은 기억 저장소에 남지 않는다."""

    @abstractmethod
    async def delete_alias(
        self, session: AtworksSessionContext, term: str
    ) -> VocabularyEntry | None:
        """Growth 뷰의 삭제. 사이드카 행과 ``memory_facts``의 fact를 **둘 다** 지운다 — 한쪽만
        지우면 저장소가 용어를 반쯤 기억한 상태로 남는다."""

    @abstractmethod
    async def confirmed_vocabulary(self, session: AtworksSessionContext) -> list[VocabularyEntry]:
        """컨텍스트 주입의 재료: 확정된 항목 전부(최신 확인순). 페이지가 아니다 — 여기에 상한을
        두면 오래된 절반이 조용히 매칭되지 않는다. 구현은 프로세스 캐시를 둘 수 있지만 **모든
        쓰기(제안·확인·거부·삭제·강등)에서 무효화**해야 한다."""

    @abstractmethod
    async def note_vocabulary_use(
        self, session: AtworksSessionContext, terms: list[str], rejected: bool = False
    ) -> None:
        """이번 턴 컨텍스트가 실제로 실은 term들의 집계. 기본은 ``uses + 1``이고,
        ``rejected=True``(그 턴에 👎, §9)면 ``rejections + 1``에 자동 강등 규칙까지 — 거부가
        ``vocabulary_auto_demote_rejections`` 이상이고 확인 수보다 많으면 상태가 pending으로
        내려가 아무의 컨텍스트에도 들어가지 않는다(그때 그 term의 fact도 지운다 — 의무 4).
        순수 집계라 어떤 경우에도 턴을 실패시키지 않는다.

        ``uses + 1``은 confirmed 집합을 바꾸지 않으므로 프로세스 캐시를 무효화하지 **않는다**;
        강등이 실제로 일어난 호출만 무효화한다."""

    # -- 저장 질문 (자가발전 spec §8: 되풀이되는 질문의 승격) ---------------------------------
    # 이 3개 메서드가 공유하는 의무 (REST 구현이 반드시 지킨다):
    #   1) **승격은 호스트의 일이고 모델의 일이 아니다.** 문턱(``promote_window_days`` /
    #      ``promote_min_users`` / ``promote_min_asks``)은 호스트 config의 값이고, 승격기는 하루
    #      1회 LLM 없이 돈다. 백엔드는 그 결과를 읽고 쓸 뿐 스스로 승격 규칙을 갖지 않는다.
    #   2) **id는 군집의 함수다** (``sq-`` + sha1(cluster_key) 앞 12자리): 같은 질문 군집은 어느
    #      저장소에서 승격되든 같은 id를 갖고, ``cluster_key``는 UNIQUE라 재승격이 없다.
    #   3) **저장 질문은 스냅샷이 아니라 질문이다.** 실행은 언제나 지금의 창 규칙으로 다시
    #      계산한다 -- 승격 당시의 행을 되돌려 주면 그건 저장된 답이지 저장된 질문이 아니다.
    @abstractmethod
    async def list_saved_questions(
        self, session: AtworksSessionContext, status: SavedQuestionStatus | None = None,
        cursor: str | None = None, limit: int = 50,
    ) -> Page[SavedQuestion]:
        """저장 질문 목록. REST 구현 의무: 정렬은 ``uses`` DESC, ``created_at`` DESC, ``id`` DESC
        (Home 카드가 '많이 쓰는 순 ≤5'라 정렬이 곧 제품 요구사항이다), ``cursor``는 서버가 만든
        불투명 문자열, ``Page.total``은 ``status`` 필터 적용 후의 건수이고 ``limit``과 무관하다."""

    @abstractmethod
    async def run_saved_question(
        self, session: AtworksSessionContext, saved_id: str
    ) -> QueryResult | None:
        """저장 질문 1건을 **지금** 실행한다 -- ``query_runs``와 같은 실행기, 같은 창 규칙, 같은
        소스 선택. 승격 당시의 결과를 캐시해 두지 않는다.

        ``None``은 두 경우 -- 그런 id가 없거나, 숨겨진(hidden) 질문이거나. 라우트는 둘 다 404로
        답한다: 사람이 숨긴 질문이 링크로는 계속 돌아간다면 '숨김'이 아무 뜻도 없게 된다.

        REST 구현 의무: 실행에 성공하면 ``uses + 1``과 ``last_used_at``을 남긴다(그 카운터가
        목록의 정렬 기준이다). 이 경로에는 모델이 없다 -- 저장된 ``QuerySpec``을 그대로 실행할
        뿐이라, 저장 질문 하나가 갑자기 다른 질문이 되는 일이 없다."""

    @abstractmethod
    async def set_saved_question_status(
        self, session: AtworksSessionContext, saved_id: str, status: SavedQuestionStatus
    ) -> SavedQuestion | None:
        """숨기기/복원 -- 사람의 클릭만 닿는 경로다(모델에게 열린 도구가 없다). 행은 남는다:
        ``cluster_key``가 UNIQUE라, 지웠다면 같은 군집이 내일 새 카드로 되살아난다. 없는 id면
        ``None``(라우트는 404)."""

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
        ``add_guardrail_note``로 이유를 남기고, 그 시도 역시 슬롯을 소비한다.

        REST 구현 의무(이벤트 루프, spec §9): 400셀짜리 매트릭스 하나가 채팅 SSE 턴을 굶기면 안 된다
        — 실행 자체는 ``max_concurrency`` 크기의 배치로, **인입(쓰기)은** ``ingest_chunk_size`` 크기의
        배치로 나누고 배치 사이에 제어를 돌려줘야 한다(끊기지 않는 400건 트랜잭션 하나가 정확히 이
        SLO를 깨뜨렸다). 두 손잡이는 서로 다른 것을 잰다: 앞은 동시에 때리는 셀 수, 뒤는 한 트랜잭션에
        커밋하는 run 수다. 배치마다 트랜잭션 하나이고, 인입은 ``run_id`` 기준 멱등이라 중간에 끊겨도
        커밋된 배치는 정확하며 재인입해도 어떤 집계도 이중 계산되지 않는다.

        REST 구현 의무(중간 실패): 인입이 k번째 청크에서 실패하면 **예외를 밖으로 던지지 말 것**.
        스케줄러의 예외 경로가 ``record_execution``을 한 번 더 불러 한 회차에 슬롯 두 개가 소비된다.
        대신 실패를 여기서 소유한다 — 커밋된 청크의 run_id들로 ``record_execution``을 **정확히 한 번**
        부르고(그래야 ``run_count``가 실제로 쓰인 run 수와 맞는다), ``add_guardrail_note``로 이유를
        남기고, 커밋된 run들을 돌려준다.

        REST 구현 의무(멱등 재인입): 위의 "재인입해도 안전하다"는 run_id가 **실행에서 유도**될 때만
        참이다 — 같은 (job_id, schedule_index, 셀) 조합은 재시도해도 같은 run_id를 내야 하고, 그래야
        ``INSERT OR IGNORE``가 중복을 지운다. 호출마다 새 id를 발급하는 구현에서는 재시도가 중복 run을
        만든다. MockAtworks는 프로세스 카운터(``run-NNNN``)를 쓰므로 이 보증이 없다 — 데모 백엔드라
        허용하지만, REST 어댑터는 반드시 실행 유도 id를 써야 한다."""

    # -- 선택 --------------------------------------------------------------------------
    @abstractmethod
    async def list_operators(self, session: AtworksSessionContext | None) -> list[OperatorProfile]:
        """읽기: 한 배포가 자신의 인증 체계를 매핑하는 오퍼레이터 레지스트리 전체."""

    async def get_context(self, session: AtworksSessionContext) -> dict[str, Any] | None:
        """요청별 컨텍스트(프로젝트명, 허용 계, 최근 실행 요약 카운트). 동적 프롬프트 블록에 들어간다."""
        return None
