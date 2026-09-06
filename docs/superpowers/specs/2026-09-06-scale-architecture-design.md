# 점진 축적 스케일 아키텍처 — 인입 시 물질화, 페이지 계약, 보존·마스킹, 증명 하네스

**Date:** 2026-09-06 · **Status:** design for review · **Branch:** `feature/scale-architecture` (main과 분리,
완성 시 브랜치로 push) · **Target:** 대형 금융 차세대 SI · **Builds on:** 전 기능(매트릭스 job·리포트·
브리핑·규칙·포맷·parity·채팅↔화면·인사이트 패널)과 두 감사(MVP 데이터 경로 감사, refs 스케일 전략 감사).

## 1. 목적과 결정

수개월에 걸쳐 **매일 수천 건의 실행이 점진적으로 쌓이는** 현장에서, 지금까지 만든 채팅·도구·카드·패널이
**정확하고 빠르게** 동작하게 한다. 감사 결론: 모델(LLM) 경로는 이미 항목 수로 보호되지만, **백엔드 계약은
페이지네이션을 표현할 수 없고, 호스트는 전체 스캔·조용한 절단을 하며, 웹은 페이지가 없고, 보존 정책이
없다.** refs도 "데이터 소스 볼륨"은 풀지 않았다(페이지네이션·카운트·인덱스·보존 0건, 픽스처 10²). 이 층은
우리가 처음 정의한다.

핵심 전환: **질의 시점 스캔 → 인입 시점 물질화.** 인입 지점은 하나(`record_execution`, 회차당 정확히 1회)이
므로, 거기서 집계표·현재상태·워터마크·운영자 인덱스를 갱신하면 이후 모든 읽기가 작은 표를 읽는다. 우리
원칙 "숫자는 결정론 코드가 계산"이 그대로 인입 시점으로 이동한다 — 안전 모델은 바뀌지 않는다.

운영자 결정(2026-09-06, 대형 금융 차세대 SI 기준):

| 결정 | 값 | 근거 |
|---|---|---|
| 응답 본문 보존 | **90일**, 캡처 시 **마스킹 필수**, API 그룹별 **캡처 해제** 가능 | parity 재비교 창을 덮되 개인·금융정보를 무한 보유하지 않음. 본문은 유일하게 *삭제*하는 데이터 |
| 원본 실행(run) 보존 | **핫 180일** → **콜드 보관**(삭제 아님) **프로젝트 종료+1년**(설정값) | 질의는 6개월 안; 테스트 이력은 감리 증적 |
| 집계·산출물 | **영구**(일별 롤업·워터마크·현재상태·리포트·브리핑) | 작고, 그 자체가 증적 |
| 인사이트 서술 캐시 | **30일 회전** | 재생성 가능한 파생물 |
| Mock 저장소 | **SQLite** | 인덱스·GROUP BY·페이지·파티션을 증명; 자바 측 스키마 청사진 |
| 스케일 목표 | **5만 API · 평균 1만 run/일(피크 5만) · 핫 ~200만 run · 운영자 500(동시 100)** | 코어 교체 규모 + 컷오버 리허설 피크 |
| 페이지네이션 | **keyset 커서**(불투명) + `total`; offset 금지 | 깊은 페이지에서도 O(page) |
| 롤업 단위 | **일별 영구**; "오늘"은 핫 파티션 직접 | 시간별 롤업은 복잡도 대비 이득 작음 |
| 세션 | 유휴 **24h TTL**; 긴 대화는 refs `compact_history` 계약 | |
| 감사 로그 | 승인·발효·적용을 **append-only**로 | "AI가 무엇을 바꿨나 → 아무것도; 사람이 이 시각에 승인"을 증적으로 |

## 2. 안전 모델 (변하지 않음 + 두 조항 추가)

- 숫자는 결정론 코드가 계산한다 — 이제 **인입 시점에** 계산해 저장하고, 카드·패널·브리핑은 그 표를 읽는다.
- 모델은 창(window)과 페이지만 본다(기존 상한 전부 유지: fence 12k, 도구 limit ≤200, 카드 8/12, 화면 40).
- **응답 본문은 모델에 절대 전달되지 않는다.** `run_record`는 본문을 제외하고 `has_body`만 싣는다(오늘의
  fence 초과 버그를 여기서 봉인).
- **감사 로그**: 승인 마크가 찍히는 모든 호스트 라우트(`/changes|/rules|/format-batches|/profiles/*/apply|discard`)와
  발효(`apply_*`)가 `audit_log`에 `{at, operator, action, target_id, session_id}`를 append한다. 읽기 전용 API로 노출.
- 절단은 **조용히 일어나지 않는다**: 모든 목록 읽기는 `Page[T] {items, next_cursor, total}` 봉투로 돌아오고,
  집계는 롤업 합산이라 절단 자체가 없다. 표본에 의존하는 계산(2000건 표본)은 사라진다.

## 3. 데이터 모델 (자바 측 스키마 청사진 = Mock SQLite 스키마)

```
runs            (run_id PK, api_id, job_id, target_env, test_data_label, status, http_status, duration_ms,
                 executed_at, executed_by, failed_rules JSON, day)          -- day = 로컬 날짜 파티션 키
                 IDX (executed_at DESC, run_id), (api_id, executed_at), (status, executed_at), (executed_by, executed_at), (day)
bodies          (run_id PK → runs, body JSON(마스킹 후), captured_at)       -- 90일 뒤 삭제
current_state   (api_id, target_env, test_data_label) PK → latest run_id, status, executed_at, transitions_total
rollup_day      (day, api_id, target_env, test_data_label) PK → count, pass, fail, error, transitions,
                 p95_duration_ms, failed_rule_counts JSON
api_watermark   (api_id PK) → last_pass_at, first_non_pass_at, last_non_pass_at, latest_status, updated_at(api)
operator_api    (operator_id, api_id) PK → last_executed_at, run_count
jobs / rules / profiles / formats / format_batches  -- 기존 ledger + status 인덱스; jobs.active(remaining>0) 인덱스
audit_log       (seq PK, at, operator, action, target_kind, target_id, session_id)  -- append-only
retention_state (partition key → archived_at)  -- 콜드 이관 기록
```

- `failed_rule_counts`는 규칙명→건수 맵(그룹핑 `failed_rule` 롤업). `top_failed_rule` 후보의 "apis" 진짜 개수는
  롤업의 api 집합 카디널리티에서(표본 아님).
- `transitions`는 셀별 pass↔non-pass 전환 카운터를 **인입 시 증가**(flaky_v1 = 창 안 롤업 합 ≥ `flaky_min_transitions`).
- `api_watermark`로 회귀 의심(`last_pass_at < api.updated_at ≤ first_non_pass_at`)과 `failed_since`가 스캔 없이 풀린다.

## 4. 인입 시 물질화 (`record_execution` 확장)

회차 1회 호출 안에서, 새 run들에 대해 **하나의 트랜잭션**으로: `runs` insert(파티션 키 계산) → `bodies` insert
(마스킹 후; 그룹 캡처 해제면 생략) → `current_state` upsert(전환 감지 → `transitions_total`++) → `rollup_day`
upsert(count/pass/fail/error/transitions/p95 근사/failed_rule_counts) → `api_watermark` 갱신 → `operator_api`
upsert(`executed_by`) → job `run_ids`는 **카운터+최근 50개**로 대체(`run_count`, `recent_run_ids`).
멱등성: `record_execution`은 슬롯당 1회라는 기존 계약을 그대로 쓰고, 재시도 안전을 위해 `run_id` PK 충돌은 무시.
브리핑은 하루 1회 파일 존재 가드 대신 **`rollup_day` 읽기**로 계산하되 멱등 파일 가드는 유지.

## 5. 계약(ABC) 변경 — 페이지 봉투와 집계 읽기

```python
class Page(BaseModel, Generic[T]): items: list[T]; next_cursor: str | None; total: int
class RunsQuery(BaseModel): since: datetime | None; until: datetime | None; status: RunStatusFilter | None;
                            api_id: str | None; executed_by: str | None; cursor: str | None; limit: int = 50 (≤200)
class AggregateQuery(BaseModel): since; until; group_by: GroupBy; scope_api_ids: list[str] | None; limit: int = 50

search_apis(session, query, group, updated_after, cursor, limit) -> Page[ApiSpec]      # 인덱스 검색
list_runs(session, q: RunsQuery) -> Page[RunResult]                                      # keyset 페이지, 핫 파티션
count_runs(session, since, until, status, api_id) -> int
aggregate_runs(session, q: AggregateQuery) -> list[RunGroup]                             # rollup_day 합산
current_state(session, scope_api_ids | None) -> list[CellState]
watermarks(session, api_ids | None, first_non_pass_since | None) -> list[ApiWatermark]
operator_scope(session, operator_id, window_days) -> ScopeSummary {api_ids(≤N), total}
simulate_rule(session, draft, window_days) -> RuleImpact                                 # 창 안 파티션만
find_apis_with_param(session, param, cursor, limit) -> Page[ApiSpec]
recommend_rules_for_api(session, api_id, limit) -> list[RuleRecommendation]
all_jobs / list_rules / list_profiles(session, status | None, cursor, limit) -> Page[...]
active_jobs(session) -> list[JobSpec]                                                     # remaining_executions > 0
get_body(session, run_id) -> dict | None                                                 # parity 전용, 90일 창
audit(session, cursor, limit) -> Page[AuditEntry]
```
- 기존 `list_runs(limit)`·`search_apis(limit)` 호출자(도구 핸들러)는 봉투를 받아 `items`를 쓰고 `total`을 카드의
  모집단으로 쓴다 — `aggregate_runs` 카드의 잘못된 모집단(2000)이 사라진다.
- `get_context`는 `count_runs` 2회(창 30일) + `operator_scope`로 O(1)~O(범위).
- REST 어댑터의 의무를 ABC 독스트링에 적는다: 커서는 서버가 만들고 해석한다(클라이언트 불투명), `total`은
  필터 적용 후의 전체 수, 정렬은 `executed_at desc, run_id`.

## 6. 보존 계층

| 층 | 데이터 | 기간 | 동작 |
|---|---|---|---|
| 핫 | `runs` 파티션 | 180일 | 인덱스 조회 |
| 콜드 | `runs` 파티션(180일 초과) | 프로젝트 종료+1년(`retention_cold_until`) | 일 단위 **이관**(압축 보관 테이블/파일). 삭제 아님. 조회는 명시적 `archived=true` |
| 본문 | `bodies` | **90일** | 일 단위 삭제. parity 재비교는 창 안에서만 — 창 밖은 "본문 만료" 노트 |
| 영구 | 롤업·워터마크·현재상태·리포트·브리핑·감사 로그 | — | 산출물은 bounded JSON 봉투 |
| 파생 | `insights_out` 서술 캐시 | 30일 | 회전 |
| 세션 | `SessionStore` | 유휴 24h | TTL 스윕; 긴 대화 `compact_history` |

보존 작업은 스케줄러 tick의 꼬리에서 **하루 1회**(브리핑과 같은 자리), LLM 0회. 설정: `retention_hot_days=180`,
`retention_body_days=90`, `retention_cold_until`(날짜 또는 None=영구), `insights_cache_days=30`, `session_idle_hours=24`.

## 7. 마스킹 (캡처 시)

`MaskingPolicy {rules: [{name, pattern, replacement}], disabled_groups: [api_group]}` — 기본 규칙은 포맷
라이브러리의 정규식 자산을 재사용(계좌번호·주민번호·카드번호·전화·이메일). `stub_response`/실 캡처 결과는
`bodies`에 저장되기 **전에** `mask_body(body, policy)`(순수 함수, 문자열 리프에 정규식 치환)를 거친다. parity
`compare_bodies`는 마스킹된 본문끼리 비교한다(같은 규칙 적용 → 동등성 판정 불변). 그룹이 `disabled_groups`에
있으면 본문을 저장하지 않고 parity는 상태 비교로 폴백(리포트에 "본문 캡처 해제" 표시).

## 8. 모델 경로

- 상한 전부 유지. `run_record`에서 `response_body` 제거, `has_body: bool` 추가. `last_listed_run_ids`는
  `PROVENANCE_CAP`(200)으로 캡, 모집단은 `total`(정수)로 별도 보관.
- 도구 결과 봉투: 목록 도구는 `{items, total, next_cursor}`를 fence에 넣는다 — 모델이 "8 of 12,400"을 안다.
  `aggregate_runs`의 `truncated` 플래그는 사라지고 정확한 `population`이 들어간다.
- `compact_history`↔`turn_complete.results_cleared`↔`stored_messages=0` 3층 계약이 우리 호스트에서 동작함을
  테스트로 확인(상속 여부 검증).
- 임의 분석 질의("어떤 API가 추세적으로 나빠지나")는 롤업 읽기 도구로 대부분 답한다; 그 밖은 refs 분석
  델리게이트 패턴(SELECT-only·행/바이트 캡·시리즈는 카드로) — **후속(Phase 2)**로 기록.

## 9. 호스트 경로 재배선

| 경로 | 이후 |
|---|---|
| `get_context` | `count_runs`×2(30일) + `operator_scope` |
| `/runs/insights`, 브리핑, 인사이트 후보 | `aggregate_runs`(롤업) + `watermarks` + `current_state` + `active_jobs` — 표본 없음, 전 API 커버 |
| 리포트 | `runs.json`(회차별 증분 append) + `parity.json`(재계산 대상) + 본문은 `get_body` 참조; `index.html`은 요약만 임베드(run 표는 페이지 로드) |
| 스케줄러 | `active_jobs`만 순회; `execute_job_once`는 `asyncio.Semaphore(max_concurrency)`로 배치 병렬 + 배치 사이 `await`; 보존 작업 tick 꼬리 |
| `simulate_rule` | `window_days` 파티션만 + `count` |
| `select_where`(`failed_since`/`related_to`) | `watermarks(first_non_pass_since)` + 경로 prefix 인덱스 |
| 세션 | TTL 스윕; 저장은 dirty-flag(전체 deep-compare 제거) |

## 10. 웹

- 모든 목록: 커서 페이지(기본 50, 최대 100) + **"N개 중 M개 · 다음"** 컨트롤 + 가상화(긴 표). `screen_state.visible`은
  현재 페이지(≤40 유지). `navigate_screen`의 필터 어휘는 그대로(페이지 이동은 뷰 담당).
- run 레코드에 `api_method/api_path` 라벨 포함 → RunsView의 500-API 다운로드 제거.
- Home은 단일 `/home/summary`(카운트+인사이트+브리핑 헤더) 1회 호출; `useResource` 디바운스.

## 11. Mock → SQLite

`MockAtworks`는 §3 스키마의 SQLite(파일 또는 `:memory:`)를 열고 부팅 시 픽스처(apis/runs/operators)를 적재한다.
모든 ABC 읽기는 SQL(인덱스·GROUP BY·keyset). `execute_job_once`는 §4 트랜잭션. 데모 경험은 그대로(픽스처
동일). 이 스키마·쿼리가 자바(Oracle/Tibero/PostgreSQL) 어댑터의 청사진이다.

## 12. 스케일 하네스 (증명)

- `scripts/scale/generate.py`: 5만 API(그룹 분포), 운영자 500, 6개월치 200만 run(평균 1만/일, 리허설 피크 일
  5만, 실패율·전환·회귀 패턴 주입, executed_by 분포), 본문 일부 — SQLite 파일로 출력.
- `scripts/scale/bench.py` + `pytest -m scale`(기본 제외): SLO 단언 — `get_context` **<50ms**, `list_runs` 페이지
  **<200ms**, `/home/insights` 결정론 부분 **<500ms**, `aggregate_runs` **<300ms**, `simulate_rule(30일)` **<1s**,
  브리핑 **<5s**, `execute_job_once`(400건) 진행 중 **채팅 SSE 지연 <100ms**(이벤트 루프 비차단), 보존 작업
  하루치 **<30s**. 각 수치는 config에 상한으로 두고 CI(선택)에서 회귀 감지.

## 13. 범위 밖 (기록)

실제 자바 REST 어댑터 구현(계약·청사진까지만); RBAC/SSO(운영자 픽커 유지); 벡터/의미 검색; 시간별 롤업;
콜드 파티션의 대화형 질의(명시적 보관 조회만); 분석 델리게이트(Phase 2); 사용자 간 데이터 격리 보장(단일
테넌트); 리포트 `index.html`의 run 표 페이지 로드 UI 고도화.

## 14. 테스트/검수 포인트

- 계약: 모든 목록 읽기가 `Page`를 돌려주고 `total`이 필터 후 전체 수; 커서로 끝까지 순회하면 `total`개; 모델
  도구 limit ≤200 유지; `count_runs(api_id)`.
- 물질화: 인입 후 `rollup_day`/`current_state`/`api_watermark`/`operator_api`가 원본 재계산과 **일치**(속성
  기반 테스트: 무작위 run 시퀀스 → 두 경로 동일); 전환 카운터; 멱등(중복 record 무시).
- 보존: 181일 파티션이 콜드로 이관되고 핫에서 사라짐; 91일 본문 삭제; parity 창 밖 재비교는 "만료" 노트;
  롤업 영구; 세션 TTL; `insights_out` 30일 회전.
- 마스킹: 계좌·주민·카드 패턴이 저장 전에 치환; 그룹 해제 시 본문 미저장 + parity 상태 폴백; `compare_bodies`가
  마스킹 본문으로 동일 판정.
- 모델: `run_record`에 본문 없음(`has_body`); `list_runs` 50건이 fence 안; `last_listed_run_ids` ≤200; 봉투
  `total`이 카드 모집단; `compact_history` 동작.
- 호스트: `get_context` 스캔 없음(쿼리 카운트 단언); 브리핑이 하루 5만 run에서 정확; 인사이트가 5만 API
  전부의 회귀 의심을 봄; 스케줄러 tick이 비활성 job을 안 순회; 실행 중 SSE 비차단.
- 감사 로그: 승인 클릭마다 1행; 채팅 경로에선 0행.
- 웹: 빌드 clean; 페이지 이동으로 51번째 행 도달; "N개 중 M개" 정확; `screen_state` ≤40.
- **하네스**: 합성 200만 run에서 §12 SLO 전부 통과 — 이 spec의 완성 기준.

## 15. 단계 (plan의 파트)

**P0 계약**: `Page`/쿼리 모델·ABC 시그니처·독스트링(REST 의무)·도구 핸들러 봉투 적용·모집단 정정.
**P0' Mock SQLite + 물질화 + 하네스 골격**: 스키마, 적재, 인입 트랜잭션, 합성 생성기, bench 골격(SLO 상한 config).
**P1 호스트**: `get_context`·인사이트·브리핑·리포트·스케줄러·`simulate_rule`·`select_where` 재배선; 보존 작업;
마스킹; 감사 로그; `run_record`/`last_listed_run_ids`; `compact_history` 검증.
**P2 웹**: 페이지·N/M·가상화·라벨·`/home/summary`.
**P3 문서·SLO 통과·라이브 스모크**: CLAUDE.md 결정 기록, README, 하네스 green, 기존 라이브 스모크 3종 재통과
(parity·채팅↔화면·인사이트), 브랜치 push.
