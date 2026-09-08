# 자가발전 질의 엔진 (Self-growth) 설계 — 1·2단계

날짜 2026-09-07 · 브랜치 `feature/self-growth` (분기점 `feature/scale-architecture` 3a02f61) · 상태: 승인된 설계

## 1. 목적과 결정

100명의 오퍼레이터가 무엇을 물을지는 예측할 수 없다. 질문의 **표현**은 무한하지만 **구조**는 유한하다 — 어떤
것을(필터) 무엇으로 묶어(차원) 무엇을 세거나 비교해(측정) 어느 기간에(창) 어떤 순서로 몇 개(정렬·상한). 그래서 축을
하나씩 코드로 추가하는 대신 **이 대수 전체를 하나의 도구로 열고**, 못 푼 질문은 사라지지 않고 기록되어 다음 부품이
되게 한다. 이번 라운드는 두 단계다.

| 단계 | 능력 | 자동화 정도 |
|---|---|---|
| 1 | **즉석 조합**: `QuerySpec` → 호스트가 물질화 테이블 위 SQL로 계산 → `query_table` 카드 | 완전 자동 (승인 없음) |
| 1 | **질문 기록** `ask_log`: 모든 질문의 의도·시도한 스펙·결과 등급·미충족 사유 | 자동 |
| 1 | **"없다"로 끝내지 않기**: 스킬 규칙 + `note_unmet_ask` 도구 | — |
| 2 | **어휘 학습**: 용어 → 스펙 조각 별칭, 질문자 확인 클릭 뒤 조직 공유 | 자동 제안 → 사람 확인 |
| 2 | **질문 승격**: 반복 질문 → 저장 질문(Home 카드) | 자동 (임계치 규칙) |
| 2 | **피드백 → eval**: 👍 받은 답 → 회귀 케이스; 러너 | 자동 |
| 2 | **성장 대시보드**: 배운 어휘·저장 질문·미충족 군집 | 사후 검토 |

**3단계(절차 학습 → 스킬 초안 → 승인)와 L2 분석 샌드박스는 범위 밖**(§13). 참조 구현 재사용 지도는 §16.

변하지 않는 것: 모델은 숫자와 판정을 만들지 않는다. 쓰기(작업·규칙·프로파일)는 기존 승인 경로 하나뿐이다. 어휘 확정,
저장 질문 숨김 같은 "공유 상태 변경"은 호스트 클릭이며 감사 로그 2행을 남긴다. 본문은 어디에도 가지 않는다.

## 2. 안전 모델 (기존 + 네 조항)

1. `query_runs`는 읽기 전용이다. 스펙은 pydantic으로 검증되고(`extra=forbid`, enum 차원·측정값, `limit ≤ 50`), SQL은
   호스트가 컴파일한다. 모델이 쓴 SQL 문자열은 어디에도 없다.
2. 결과 행의 `api_ids`·`run_ids` 표본은 호스트가 채우고 세션 `seen_*`에 기억되어 출처 규율(카드·화면 지시의 id는 세션이
   본 것)이 그대로 적용된다. 모집단은 `total_groups`·`population`(필터 적용 COUNT).
3. `ask_log`의 질문 텍스트는 마스킹 정책(`masking.py`)을 거친 ≤300자 요약이다. 모델은 다른 사람의 질문 원문을 보지
   않는다 — 대시보드와 승격기만 읽고, 모델의 컨텍스트에는 **확정된 어휘**만 들어간다.
4. 어휘는 `MemoryWriteFilter`(9자리+ 숫자·IBAN·이메일 차단)를 통과한 값만 저장된다. pending 상태의 별칭은 제안한 세션
   안에서만 쓰이고, confirmed가 되어야 다른 오퍼레이터의 컨텍스트에 들어간다.

## 3. QuerySpec — 카탈로그와 의미

```python
class QueryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: RunStatusFilter | None = None            # all|pass|fail|error|non_pass
    since: datetime | None = None
    until: datetime | None = None
    window_days: int | None = Field(default=None, ge=1, le=180)   # since/until 대신
    api_ids: list[str] | None = Field(default=None, max_length=100)
    path_contains: list[str] | None = Field(default=None, max_length=5)   # 소문자 부분일치, OR
    path_prefix: str | None = Field(default=None, max_length=120)
    method: list[HttpMethod] | None = None
    api_group: list[str] | None = Field(default=None, max_length=10)
    target_env: list[str] | None = None
    test_data_label: list[str] | None = None
    executed_by: list[str] | None = Field(default=None, max_length=10)     # 오퍼레이터 id
    scope_operator: str | None = None                # "내가 실행한 API" 범위 (operator_api JOIN)
    failed_rule: list[str] | None = Field(default=None, max_length=10)
    http_status: list[int] | None = Field(default=None, max_length=10)

Dimension = Literal[
    "api", "path_segment_1", "path_segment_2", "path_segment_3", "path_prefix_2", "method", "api_group",
    "target_env", "test_data_label", "failed_rule", "http_status", "executed_by", "day", "week",
]
Measure = Literal["runs", "pass", "fail", "error", "non_pass", "fail_rate", "apis", "transitions", "p95_duration_ms"]

class QuerySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filters: QueryFilters = Field(default_factory=QueryFilters)
    dimensions: list[Dimension] = Field(min_length=0, max_length=2)   # 0 = 전체 합계 1행
    measures: list[Measure] = Field(min_length=1, max_length=5)
    order_by: Measure | Literal["key"] = "non_pass"
    descending: bool = True
    limit: int = Field(default=20, ge=1, le=50)
    compare_previous_window: bool = False
    include_samples: bool = True
```

의미 규칙(전부 호스트):

- `window_days`가 있으면 `since = now - window_days`, `until = now`. 둘 다 없으면 `max_aggregate_window_days`.
- **창의 하한은 hot 파티션이다**(최종 수정 파도에서 확정): `since`는 `now - retention_hot_days`(180)로 클램프한다.
  `window_days`는 이미 `le=180`이라 명시적 `since`만 이 선을 넘을 수 있고, 넘으면 같은 질문이 소스에 따라 다른 답을
  낸다 — 롤업은 영구라 답하고, `runs` arm과 증거 표본은 보존 작업이 `runs_archive`로 옮긴 행을 못 본다.
  `QueryResult.window`는 **클램프된** 쌍을 보고한다(답은 실제로 덮은 창을 말한다). 더 옛날은 `RunsQuery(archived=True)`.
  `compare_previous_window`의 이전 창은 **클램프된** 창을 기준으로 뒤로 민다 — 비교가 클램프를 넓히지는 못한다.
- `path_segment_n` = 경로를 `/`로 나눈 n번째 조각(1-base, 선행 `/v1` 같은 버전 조각도 1). `path_prefix_2` = 앞 두 조각.
  `{id}`·숫자만인 조각은 그대로(라벨링 안 함). `n`은 1·2·3 — **세 번째 조각이 T10에서 추가됐다**: 실제 카탈로그는
  `/v1/product/history/001796`처럼 버전 → 도메인 → 액션 순이라 "엔드포인트 계열"이 세 번째 조각에 있다.
- `fail_rate = (fail+error)/runs`, 소수 4자리. `apis` = COUNT(DISTINCT api_id). `transitions`·`p95_duration_ms`는 롤업의
  정의(전환 누계, 최대값 병합 근사)를 그대로 따른다. `fail_rate`는 `status` 필터(`all`/없음 외)와 **함께 쓸 수 없다**
  (구현 시 확정, `QuerySpec` 검증기가 거부): 분모가 필터가 남긴 행이라 `status=non_pass`에서는 모든 행이 1.0,
  `status=pass`에서는 0.0 — 아무도 묻지 않은 분모다. 거부 문구가 대안을 말한다("실패율은 status 필터 없이;
  실패 건수는 non_pass").
- `day`/`week`는 `briefing_tz` 기준 로컬 날짜·ISO 주. `compare_previous_window=True`면 동일 스펙을 바로 앞 같은 길이의
  창에 실행해 각 측정값에 `_prev`·`_delta` 컬럼을 붙인다(그룹 키 기준 외부 조인, 없는 쪽은 0).
- 정렬은 요청 측정값 내림차순 기본, `key`는 그룹 키 오름차순. 동률은 키 오름차순.
- `executed_by`와 `scope_operator`는 다르다: 전자는 "그 사람들이 돌린 run"(run 필터/차원), 후자는 "그 사람이 만진 API
  집합"(API 범위).

## 4. 실행 — SQL 컴파일

`Store.query(spec, *, now, flaky_min) -> QueryResult`가 한 곳에서 컴파일한다. 소스 선택 규칙:

| 차원 조합 | 소스 | 비고 |
|---|---|---|
| 없음 / `api` / `path_*` / `method` / `api_group` / `target_env` / `test_data_label` / `day` / `week` | `rollup_day ⋈ apis` | 창은 `day` 컬럼(인덱스). 경로 조각은 `apis.path_segment_1/2`(인입 시 물질화, 인덱스) |
| `failed_rule` / `http_status` 단독, API 범위 필터 없음 | `rollup_key_day` | T8/최종 수정웨이브의 빠른 길 |
| `failed_rule` / `http_status` + 다른 차원 또는 API 범위 필터 | `rollup_day` + `json_each` | 정확 경로, 창 안 |
| `executed_by` 단독 또는 `day`/`week`와 조합 | **`rollup_operator_day`** (신규) | 인입 시 물질화: `(day, operator_id) → runs/pass/fail/error` |
| `executed_by` × 다른 차원 | `runs` 창 안 GROUP BY (인덱스 `(executed_by, executed_at)`) | 유일한 run 테이블 경로. 창 상한 180일, SLO 행으로 측정 |
| 필터 `executed_by`(차원 아님) | 위 소스에 `api_id IN (SELECT … FROM runs WHERE executed_by IN (…) AND executed_at ≥ …)`가 아니라 **run 필터가 필요한 경우 `runs` 소스로 강제** | 롤업은 실행자를 모른다 — 문서화 |

- **정제(T2 수정 1라운드):** 필터 `executed_by`가 **`executed_by` 차원 위에**(단독 또는 `day`/`week` 조합) 올라올 때는 `runs`로 강제하지 않는다 — `rollup_operator_day`의 키 컬럼이 곧 실행자이므로 `operator_id IN (…)`로 정확히 표현된다. 그 밖의 모든 `executed_by` 필터(다른 차원 아래)는 표대로 `runs`.

- `measures`가 `apis`를 포함하면 `COUNT(DISTINCT api_id)`; 키 축 소스에서는 `api_ids` 집합 크기.
- `limit`·`order_by`는 SQL `ORDER BY … LIMIT`. 파이썬은 `limit`행만 본다(스케일 브랜치의 규칙).
- 표본 채움(`include_samples`): 반환된 그룹마다 `api_ids ≤ 20`, `run_ids ≤ 5`(최신) — 인덱스 쿼리 그룹당 ≤2회.
- `population` = 필터 적용 후 run COUNT(롤업 합계), `total_groups` = LIMIT 전 그룹 수.
- 골든 테스트: fixture와 합성 데이터에서 모든 소스 경로가 **파이썬 오라클**(`fetch_runs` 후 순수 파이썬 집계)과
  runs/pass/fail/error/non_pass/fail_rate/apis에서 일치. `transitions`·`p95`는 정의된 근사 규칙으로 대조.

## 5. 도구 · 카드 · 스킬 규칙

- 도구 `query_runs(spec: QuerySpec)` — 스키마는 카탈로그 enum을 그대로 실어 **config의 순함수**(캐시 안정). 설명에 카탈로그
  각 항목의 한 줄 의미와 예시 스펙 2개.
- 실행기 `_query_runs`: 스펙 검증 → `backend.query_runs(session, spec)` → 표본 id를 `seen_apis`/`seen_runs`에 기억
  (`runs_by_ids`/`get_apis` 배치, `PROVENANCE_CAP`) → `last_query_result`에 저장 → `present_query_table`.
- 카드 `query_table` (presentation tool `present_query_table {title, note}`): 열 = 차원 키 + 측정값(+ `_prev/_delta`),
  행 ≤ limit, 푸터에 **실행 스펙 요약**(사람이 읽는 문장 + JSON 접기), `population`, `total_groups`, 표본 링크(`attach=run:`),
  👍/👎(§9). 웹 `GenerativeBlock`에 컴포넌트 추가. 카드 숫자는 전부 `QueryResult`에서.
  `result_ref` 인자는 **없다**(구현 시 확정): 세션에는 `last_query_result` 슬롯이 하나뿐이고 카드는 그것을 읽는다 —
  `present_run_groups`와 같은 모양이다. 모델이 참조 id를 지어내 넣을 자리를 아예 만들지 않는 쪽이 안전하다.
- 도구 `note_unmet_ask(reason: no_dimension|no_evidence|out_of_scope|refused, summary ≤200, wanted ≤200)` — 모델이
  카탈로그로 답할 수 없다고 판단하면 **설명 전에** 부른다. 호스트는 `ask_log`에 unmet으로 기록하고 도구 결과로
  "기록했습니다"만 돌려준다. 카드 없음.
- 도구 `propose_alias(term ≤40, fragment: QueryFilters 부분, note ≤120)` — §7.
- 스킬 규칙(`failed-triage`·`api-lookup`에 힌트, 새 스킬 없음): (1) 요청한 묶기·필터가 `aggregate_runs` 축 밖이면
  `query_runs`. (2) 카탈로그에도 없으면 `note_unmet_ask` 후 "무엇이 있으면 답할 수 있는지"를 한 문장으로. "지원하지
  않습니다"로 끝나는 답은 규칙 위반. (3) 사용자 용어를 필터 값으로 해석했으면 `propose_alias`.
- `aggregate_runs`·`run_groups`는 그대로 둔다(기존 스킬·테스트·카드).

## 6. 질문 기록 — `ask_log`

```
ask_log(seq PK, at, session_id, operator, role, question TEXT(마스킹, ≤300), intent, spec_json, outcome,
        unmet_reason, wanted TEXT, tool_calls INT, cards INT, feedback (NULL|up|down), cluster_key, turn_id)
IDX (at), (cluster_key, at), (outcome, at)
```

- 기록 시점: 턴 종료 시 오케스트레이터 훅(`turn_id` = 스트림 id). 한 턴 = 한 행.
- `outcome` 판정(결정론): `query_runs`/`aggregate_runs`/`rank_failed_runs`/`search_apis` 등 데이터 도구 뒤 카드가 나오면
  `answered`; `note_unmet_ask`가 불렸으면 `unmet`(+사유); 데이터 도구 0회·카드 0이면 `partial`; 승인·스테이징 턴은
  `action`(성장 대상 아님).
- `intent`: 사용한 도구로 결정론 매핑(lookup/status/aggregate/trend/compare/cause/action/meta); unmet이면 도구 인자.
- `cluster_key`: answered → 값을 뺀 정규화 스펙 `dims=a,b|measures=…|filters=kind1,kind2|compare`; unmet → `reason|` +
  질문 토큰 정규화(불용어 제거, 정렬, ≤6 토큰).
- 질문 텍스트: 사용자 메시지에 `mask_body`(문자열)를 적용하고 300자에서 자른다.
- 보존: `ask_log_retention_days=365`(config) — 보존 작업에 단계 추가.
- 라우트: `GET /ask-log?outcome&cursor&limit`(페이지 봉투, 세션), `GET /growth/summary`.

## 7. 어휘 학습 — `commerce_common.memory` 재사용, 조직 수준

- 저장소: `host/atworks_host/memory_store.py` `SqliteMemoryStore(MemoryStore)` — 테이블
  `memory_facts(subject_id, key, value, category, updated_at, source_session_id, PK(subject_id,key))` + `purge_generation`.
  `check_memory_store`로 프로토콜 완전성 검사. `subject_id = session.project_id`(조직 공유).
- 사이드카 `vocabulary(term PK, fragment_json, status pending|confirmed|rejected, proposed_by, proposed_at,
  confirmed_by, confirmed_at, confirmations INT, uses INT, rejections INT, cooldown_until)`.
- `MemoryFact`: `key = 정규화된 term`, `value = fragment JSON(≤200자)`, `category = "context"`. `validate_fact` +
  `MemoryWriteFilter`(기본 패턴)가 모든 쓰기 앞에 선다. term/fragment에 PII 모양이 있으면 `MemoryWriteRejected` → 제안 무시.
- 흐름:
  1. 모델이 `propose_alias(term, fragment)` → 호스트: `status=pending`, 이 세션의 `pending_aliases`에 기억. 도구 결과에
     "이 턴의 카드에서 확인을 요청하세요"는 필요 없다 — 카드 푸터에 호스트가 자동으로 "‘결제 계열’을 `/v1/payment` 경로로
     해석했습니다 — 맞나요? [예] [아니오]"를 붙인다(`query_table.pending_alias`).
  2. `POST /vocabulary/{term}/confirm|reject`(세션, 사람 클릭) → confirmed(`confirmations+1`, 감사 2행) / rejected
     (`cooldown_until = now + 30d`). 다른 오퍼레이터가 같은 term을 나중에 확인하면 `confirmations+1`.
  3. 컨텍스트 주입: `build_dynamic_context` payload `vocabulary` — 이 턴 메시지에 실제로 **나타난** confirmed 용어 ≤8.
     매칭은 `match_terms`(용어 문자열을 긴 것부터 메시지에서 찾는다), `match_facts`+`select_tier_one_facts`가 아니다
     (구현 시 확정): 어휘는 자유 문장 사실이 아니라 이름이 있는 항목이고, 긴 것 우선이라야 '결제 계열'이 '결제'에
     먹히지 않는다. 모델은 되묻지 않고 fragment를 쓴다. `uses+1`.
  4. 👎(§9)를 받은 턴이 어휘를 썼으면 `rejections+1`; `rejections ≥ 3 AND rejections > confirmations`면 자동 pending으로 강등.
- `enable_memory=True`로 켜되 참조 구현의 **턴 후 자유 사실 추출(`extract_and_store`)은 켜지 않는다**
  (`memory_extract_facts=False` 신설). 저장되는 것은 어휘만.
- `save_memory`/`recall_memories` 도구는 노출하지 않는다(`absent_tools`에 유지). 어휘는 컨텍스트 블록으로만 들어간다.

## 8. 질문 승격 — 저장 질문

- `Promoter.maybe_run(now)`: 스케줄러 tick 꼬리(보존 뒤), 하루 1회 가드(`retention_state` `promote:<date>`), LLM 없음.
- 규칙: 최근 `promote_window_days=7`의 `answered` 행을 `cluster_key`로 묶어 **서로 다른 operator ≥ `promote_min_users`(3)
  AND 건수 ≥ `promote_min_asks`(5)** 이면 `saved_questions` 생성(cluster_key UNIQUE, 재승격 없음). 👎 비율 > 50%인 군집은 제외.
- `saved_questions(id PK, cluster_key UNIQUE, spec_json(대표 스펙 = 군집 최신 행), title, created_at, status active|hidden,
  uses, last_used_at, source_users INT, source_asks INT)`. `title`은 카탈로그 라벨로 결정론 생성
  (예: "실패·에러 · 경로 2조각별 · 30일 · 상위 20").
- 라우트: `GET /saved-questions`(페이지 봉투), `GET /saved-questions/{id}/run`(→ `QueryResult` 실시간),
  `POST /saved-questions/{id}/hide|unhide`(사람 클릭, 감사 2행). Home "저장 질문" 카드(≤5, `uses` 순).

## 9. 피드백 → eval

- 카드 푸터 👍/👎 → `POST /feedback {turn_id, vote}`(세션) → `ask_log.feedback`. 같은 턴 재투표는 덮어쓴다.
- 👍 & `outcome=answered` & `spec_json` 있음 → `evals/cases/<yyyymmdd>-<seq>.json` 생성(commerce-evals 케이스 스키마):
  ```json
  {"id":"…","priority":"P2","tags":["query_runs","path_segment_2"],"skip":false,
   "state":{"operator":"op-0000","role":"developer"},
   "turns":["실패를 endpoint 기준으로 묶어줘…(마스킹 후)"],
   "expected":{"calls_tool":"query_runs","spec_equals":{"dimensions":["path_segment_2"],"measures":["non_pass","apis"],"filters_kinds":["status","window_days"]},
               "ui_components":["query_table"],"never_calls":["stage_job","apply_job"],"max_tool_calls":4},
   "notes":"auto-generated from 👍 on turn …"}
  ```
- 러너 `evals/run_evals.py` + `pytest -m evals`(기본 제외):
  - **재생 모드(기본, 모델 없음)**: 케이스의 스펙이 현재 카탈로그에서 유효(pydantic)하고 현재 Store에서 실행되며
    결과 형태(컬럼 집합, 행 ≤ limit, population ≥ 0)가 맞는지. 카탈로그를 깨는 변경을 잡는다.
  - **라이브 모드(`ATWORKS_EVAL_LIVE=1`)**: 실제 모델로 턴을 재생해 `calls_tool`/`spec_equals`(차원·측정값·필터 종류)/
    `ui_components`/`never_calls`를 코드 그레이더로 채점. 비결정성은 1회 재시도.
- 👎는 케이스를 만들지 않고 `ask_log`에 남는다(대시보드 "낮은 평가" 목록).

## 10. 성장 대시보드 — 포털 뷰 `growth`

- 6번째 내비 "Growth". 탭: **배운 어휘**(confirmed/pending/rejected, 확인·거부·삭제 버튼), **저장 질문**(숨김/복원, 실행),
  **미충족 질문**(군집별 사유·건수·마지막 시각·예시 1건(마스킹된 요약)), **이번 주**(질문 수, answered/partial/unmet 비율,
  새 어휘, 새 저장 질문, 👍👎).
- 라우트: `GET /growth/summary`(위 숫자들, COUNT만), `GET /vocabulary?status&cursor&limit`, `POST /vocabulary/{term}/confirm|reject|delete`,
  `GET /saved-questions…`, `GET /ask-log…`. 모두 세션. 쓰기 라우트는 `host_action` 헬퍼 패턴(감사 2행)을 쓴다 — 단
  승인 마크는 관여하지 않는다(어휘·저장질문은 job 승인이 아니다).
- `screen_state`: growth 뷰의 항목(term·saved question id)도 `data-ref="vocab:…"`/`saved:…`를 단다. 화면 지시 대상
  kind는 확장하지 않는다(이번 라운드).

## 11. 데이터 모델 (Store DDL 추가)

```
apis                  + path_segment_1 TEXT, path_segment_2 TEXT, path_prefix_2 TEXT   IDX (path_segment_1), (path_segment_2), (path_prefix_2), (method)
rollup_operator_day   (day, operator_id) PK → count, pass, fail, error                   IDX (operator_id, day)
ask_log               §6
memory_facts          (subject_id, key) PK → value, category, updated_at, source_session_id ; memory_meta(purge_generation)
vocabulary            §7
saved_questions       §8
retention_state       + 'promote:<date>', 'ask_log:<date>'
```

- `apis.path_segment_*`는 `load_apis`/`replace_all_apis` 시 계산. 기존 파일은 `_migrate_columns` + 백필.
- `rollup_operator_day`는 `rollup_delta`가 `executed_by`별 델타를 내고 `ingest`가 접는다(멱등 규칙 동일, 재빌드 포함).
- 보존: `ask_log` 365일 삭제 단계 추가. 나머지 신규 테이블은 영구(작다).

## 12. 성능·SLO

- 새 SLO `slo_query_ms=300`: 벤치 행 `query_runs` × 4 (path_segment_2 / method×env / failed_rule 키축 / executed_by×api
  run 경로), `test_scale.py` `SLO_ASSERT`에 추가. `compare_previous_window`는 2배 비용 허용(같은 300ms 한도 안이어야 함;
  아니면 600ms 별도 행으로 문서화).
- `ask_log` 쓰기는 턴 종료 훅 1 INSERT. 승격기는 하루 1회 `GROUP BY cluster_key` 1문장.
- 어휘 주입은 confirmed 항목 전체(≤ 수백)를 메모리에 캐시하고 키워드 매칭 — 턴당 <1ms.

## 13. 범위 밖 (기록)

- 3단계: 스킬 초안 자동 생성·승인·런타임 로드(`SkillRegistry` 재생성 훅 포함).
- L2 분석 샌드박스(`DelegateExtension` 기반 제한 계산).
- "왜 실패했나" 근거(오류 메시지·응답 요약·스펙 diff) — aTworks 데이터 협의 필요.
- 화면 지시의 growth 뷰 대상 kind 확장, 저장 질문의 개인 소유/공유 구분(전부 공유), 다국어 토큰화.
- 참조 구현의 자유 사실 추출(`extract_and_store`) — 켜지 않음.

## 14. 테스트·검수 포인트

- 컴파일 골든: 모든 소스 경로 × 차원 조합 표본 × 필터 조합에서 파이썬 오라클과 일치(fixture + 6만 run 합성 세트의 축소판).
- 속성: `rollup_operator_day` 합계 = run 재계산; 멱등.
- 도구·실행기: 잘못된 스펙 → `InvalidToolArgument`; 표본 id가 `seen_*`에 들어감; 카드 숫자 = 결과.
- ask_log: answered/partial/unmet/action 4가지 판정 테스트; 마스킹된 질문 저장; cluster_key 결정론; 보존 삭제.
- 어휘: PII 모양 제안 거부; pending은 제안 세션에만; confirm 후 다른 세션 컨텍스트에 등장; reject 냉각; 👎 강등.
- 승격: 임계치 경계(2명/5회 ×, 3명/4회 ×, 3명/5회 ○); 👎 50% 초과 제외; 재승격 없음; 제목 결정론.
- 피드백/eval: 👍 → 케이스 파일 생성 형식; 재생 모드 통과; 카탈로그에서 차원을 빼면 재생 실패.
- 라우트: 봉투 형태, 400/422, 감사 2행(confirm/reject/hide).
- 웹: `npm run build`; Growth 뷰 4탭; 카드 푸터 👍👎·별칭 확인 버튼.
- 라이브 스모크(6만 run 데이터셋, 실제 모델): 담당자 질문 5개(endpoint 묶기, 내가 실행한 수, history+규칙있음, 내 API 이슈,
  1~5위) 모두 카드; "결제 계열" 2세션 학습; 3명×5회 승격; 👍 → eval 파일; 안전(마크·감사) 불변.

## 15. 단계 (plan의 파트)

- **Part A — 부품**: 타입·카탈로그·config(T1) → Store 컴파일러 + `apis.path_segment_*` + `rollup_operator_day`(T2) →
  ABC/Mock/더블 + 도구 + 실행기 + 카드 + 웹 컴포넌트(T3)
- **Part B — 기록**: `ask_log` + `note_unmet_ask` + 턴 훅 + 라우트(T4) → 스킬 규칙 + 카탈로그 프롬프트 블록 + 라이브
  스모크 1차(T5)
- **Part C — 학습**: `SqliteMemoryStore` + `vocabulary` + `propose_alias` + confirm/reject + 주입(T6) → 승격기 + 저장
  질문 + Home 카드(T7) → 피드백 + eval 케이스 + 러너(T8)
- **Part D — 표면·증명**: Growth 뷰 + 라우트(T9) → 벤치/SLO·보존·문서·라이브 스모크 2차·푸시(T10)

## 16. 참조 구현 재사용 지도 (refs 감사 2026-09-07)

| 층 | refs 심볼 | 이번 라운드에서 |
|---|---|---|
| 스펙 → 호스트 계산 패턴 | `merchant_agent/tools/registry.py:149` `query_metrics`, `analysis.py:44` 분석 브리프 | 패턴만. `QuerySpec` 타입·컴파일러는 신규 |
| 어휘 저장·필터·주입 | `commerce_common/memory.py` `MemoryStore`(78) `MemoryWriteFilter`(150) `validate_fact`(183) `match_facts`(216) `select_tier_one_facts`(229) `render_memory_block`(256) `MemoryRuntime`(544) | **그대로 import**. 저장소 구현만 SQLite로. 추출(`extract_and_store`)은 사용 안 함 |
| 주입 seam | `commerce_common/prompt_assembly.py:33` `build_system_blocks`; 우리 `prompt.py build_dynamic_context` payload | payload 키 `vocabulary`, `catalog_hint` 추가 |
| 출처 규율 | `commerce_common/grounding.py`, `state.seen_*` | 그대로. `query_table` 표본 id가 `seen_*`로 |
| eval 케이스 스키마 | `plugins/commerce-builder/skills/commerce-evals/SKILL.md:17` | 스키마 채택. 러너·그레이더는 신규(refs는 하네스를 싣지 않음) |
| 스킬 온디맨드 로드 | `commerce_common/skills.py` `SkillRegistry` | 이번엔 힌트 수정만. 런타임 재로드는 3단계 |
| 질문 기록·군집·승격 | (없음, 확인) | 전부 신규 |
