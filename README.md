# atworks-ai
aTworks 옆에 붙는 AI 채팅 서비스. commerce-agents의 commerce_common 위에 atworks_agent 역할 패키지를 얹는다.
설치: python -m venv .venv && source .venv/Scripts/activate && pip install -r requirements-dev.txt (macOS/Linux: source .venv/bin/activate)

## 환경 변수

`.env.example`을 `.env`로 복사하고 채운다 (`.env`는 `.gitignore`에 있다). 모델 엔드포인트를 폐쇄망
LiteLLM 프록시로 바꾸는 방법과 `ATWORKS_TRUST_OS_CA`(폐쇄망 프록시 CA를 OS 인증서 저장소에서 신뢰)는
`docs/sllm-seam.md` 참고.

```bash
cp .env.example .env   # 그리고 ANTHROPIC_AUTH_TOKEN 등을 채운다
```

## 실행

```bash
python scripts/run_demo.py             # host(:8010) + web(:3110) 둘 다, Ctrl-C로 둘 다 종료
python scripts/run_demo.py --host-only # host만
python scripts/run_demo.py --web-only  # web만
```

개별 실행:

```bash
python -m atworks_host.main                    # host — :8010
npm run dev --prefix web/atworks-web            # web — :3110
```

## 검증

```bash
ruff check . && pytest                 # 결정론 층: 게이트·guardrail·스코어러·스케줄러
python scripts/smoke_chat.py           # host가 떠 있는 상태에서 — 발화 4개의 카드·게이트를 확인
```

수동 시나리오 4개 (host + web을 띄운 채, `docs/sllm-seam.md`·`docs/safety.md` 참고):

1. "최근 실패한 api 중 risk 있는 것 가져와" → `run_digest` 카드에 "N건 중 먼저 볼 k건" 헤더가 뜬다.
2. Runs 뷰에서 실패 행을 "채팅에 첨부" → "이거 왜 실패했어" → 답변이 첨부한 그 run만 다룬다.
3. "지난 1주일 업데이트된 api 오늘부터 3일간 매일 9시에 실행해줘" → `question_form` 또는
   `job_preview`(대상 건에 ● 표시) → Jobs 뷰에서 승인 →
   `POST /api/atworks/scheduler/tick?now=<from_date>T09:00:00%2B09:00`
   (`+` must be URL-encoded as `%2B`) → 리포트 링크가 열린다.
4. "오늘 업데이트한 API를 개발서버와 이관서버에서 동일한 테스트 데이터로 수행하고 결과를 비교해줘"
   → `question_form` 또는 `job_preview` — 카드에 대상 계 2개(dev, stg), 테스트 데이터 세트,
   "총 실행 N건 × M회 = K건"이 보인다(승인 한 번이 이 전부를 덮는다) → Jobs 뷰에서 승인 →
   `POST /api/atworks/scheduler/tick` → 리포트 상단에 계 비교 표와 "차이 N건" 타일이 뜬다.

### 데모 픽스처와 30일 창 (주의)

픽스처 실행 이력(`host/atworks_host/fixtures/runs.json`)은 **2026-09-01 ~ 09-03**에 고정돼 있고,
Home 타일·`GET /runs/insights`·`get_context`의 요약은 모두 `scope_window_days`(30일) 창 안의 실행만
센다. 그래서 **2026-10-03이 지나면** 픽스처만으로 띄운 데모의 실패/에러·불안정 타일은 정상적으로 0으로
읽힌다 — 버그가 아니라 창 밖으로 나간 것이다. 숫자를 다시 채우려면 job을 하나 승인해 스케줄러로
실제 실행을 만들거나(`POST /scheduler/tick`), 픽스처의 날짜를 오늘 기준으로 옮기면 된다
(테스트는 `host/tests/test_app.py`의 `_redate_fixture_runs`가 같은 일을 한다).
회귀 의심(`regression_suspect`) 타일은 픽스처에서 **0이 정상**이다: 스펙 §3의 규칙은
`last_pass_at < api.updated_at <= first_non_pass_at`인데, 픽스처의 12개 API는 모두 마지막 pass가
자기 `updated_at`보다 뒤에 있다(예: api-004는 실패 뒤 다시 통과했다 — 회복한 API는 회귀가 아니다).

## Insights & briefing

새로 생긴 질문 축 네 가지 — 숫자는 항상 호스트 계산이고 모델은 축과 표시만 고른다.

1. **묶기** — "이번 주 실패 원인별로 묶어줘" → `aggregate_runs {group_by: failed_rule}` →
   `present_run_groups` (`run_groups` 카드): 그룹별 건수·실패/에러·첫 실패 시각·`flaky_v1`(불안정)·
   `regression_suspect`(회귀 의심)·p95.
2. **언제부터** — "환불 API 언제부터 깨졌어?" → `aggregate_runs {group_by: api, api_id}` — 첫 실패,
   직전 pass, 스펙 수정일을 한 행으로 보여준다.
3. **실패만 / 영향 범위 재실행** — "어제 실패한 것만 dev에서 다시 돌려"(`select_where.failed_since`)
   또는 "결제 승인 API 고쳤는데 뭘 다시 돌려야 해?"(`select_where.related_to`, 같은 group 또는 경로
   prefix) → `stage_job`이 API 목록을 서버에서 해석해 채우고, `JobSpec.selection_basis`(서버 문장)가
   미리보기 카드와 Jobs 행에 선택 근거로 뜬다.
4. **불안정** — Home의 `Needs attention`에 **불안정** 타일(`GET /api/atworks/runs/insights`,
   `flaky_v1` 개수). 클릭하면 "요즘 왔다갔다 하는 API 뭐야" 질의로 이어진다.

**일일 브리핑**은 스케줄러 tick 끝에서 LLM 없이 하루 1회(`briefing_at` 09:00 Asia/Seoul, 파일 존재로
멱등) 생성되며 `GET /api/atworks/briefings/latest`(세션, JSON)와
`GET /api/atworks/briefings/{date}`(세션 없음, HTML)로 조회한다. Home 상단 BriefingCard에 요약이
뜬다. 리포트의 실행 행에는 "채팅에서 보기" 링크(`?attach=run:{run_id}`)가 있어 포털이 열리면서 해당
run이 자동으로 첨부된다.

## Validation rules

채팅에서 값 검증 규칙 초안을 작성 — 수치 비교, 코드값 소속(membership), 필수값(required), 포맷(named
또는 raw regex) 네 종류 — 하고 Rules 뷰에서 승인하면 이후 실행되는 run에만 적용된다. 과거 run과
성공률, 리포트, 브리핑은 그대로 남는다(effective_from 이후만 평가).

## 포맷 라이브러리, 예시 검증 & 추천

raw regex 포맷 규칙은 `pass_examples`/`fail_examples`를 함께 내야 스테이징된다 — 패턴을 컴파일해 pass는
전부 fullmatch, fail은 전부 불일치해야 하고, 하나라도 어긋나면 그 자리에서 거부된다(내장 5개
email/date/iso8601/uuid/number는 예시가 필요 없다). 승인된 raw-pattern 규칙에 이름을 붙이면
(`save_format_as`) 포맷 라이브러리에 저장돼 이후 다른 규칙의 `format`에 그 이름을 그대로 쓸 수 있다
— 라이브러리 포맷은 어떤 run도 참조되기 전까진 판정하지 않으므로(inert), 여러 개를 한 번에 추가하는
`stage_format_batch`도 승인 한 번으로 끝난다(이름·패턴이 겹치면 자동으로 걸러진다). 추천은 두 방향 —
한 API에 적용한 포맷을 같은 이름의 param을 가진 다른 API로 확장할지 묻거나, 규칙이 없는 API에 대해
같은 이름의 param을 가진 다른 API들에 이미 적용된 규칙을 제안한다(대응하는 사례가 없으면 아무것도
지어내지 않는다) — 제안된 규칙도 각각 개별 스테이징·승인을 거친다. Rules 페이지의 Formats 섹션에서
`GET /formats`·`GET /format-batches`로 보고 `POST /format-batches/{id}/apply|discard`로 승인/폐기한다.

## 값 병행 비교 (Value Parity)

노후·신규 서버(예: `legacy`/`renewed`, 명명 타깃)에 같은 API·같은 테스트 데이터를 동시에 돌려
**응답 값**까지 대량으로 비교한다 — 상태(pass/fail)만 같아도 payload가 다르면 리포트의 parity
블록이 값 불일치로 잡아낸다. 매 호출마다 달라지는 `serverTime` 같은 필드는 노이즈일 뿐이라, 채팅에서
"serverTime 무시해"라고 하면 실제 차이 클러스터를 근거로 무시 스펙(비교 프로파일)을 초안하고, Rules
페이지의 Profiles 섹션에서 승인하면 **서버를 다시 부르지 않고** 저장된 응답 본문만 재비교해 노이즈가
걷힌 리포트를 즉시 보여준다. 판정은 항상 결정론 엔진(`compare_bodies`)이 하고, 모델은 무시 경로를
제안만 하며, 과거 run과 그 판정은 재비교로도 절대 바뀌지 않는다.

## 채팅 ↔ 화면 상호작용

지금까지는 화면 → 채팅 한 방향(항목을 첨부)만 있었다. 이제 반대 방향도 연다: 채팅이 포털을
움직인다. "runs 화면으로 가"라고 하면 뷰를 전환하고, "api-004 상세 보여줘"라고 하면 그 화면으로
가서 해당 행을 스크롤·포커스하고, "실패만 보여줘"라고 하면 그 뷰가 이미 가진 필터(runs의
status, apis의 검색어)를 건다. 답변이 화면의 특정 항목에 근거할 때는 모델이 스스로 판단해
①②③ 붉은 박스로 그 항목들을 짚어준다("지금 화면에서 먼저 봐야 할 건 뭐야?" 같은 명시적 질문이
아니어도). 포털은 매 채팅 요청마다 지금 보고 있는 화면(뷰·필터·보이는 항목 최대 40개)을
`screen_state`로 함께 보내 모델이 "지금 뭐가 보이는지" 알게 하고, 가리키는 대상은 이 세션에서
실제로 본 것이거나 지금 화면에 있는 것으로만 한정된다(지어낸 id로는 화면을 움직일 수 없다).
이 모든 지시는 **화면만 바꾸는 순수 UI 동작**이다 — 아무것도 실행·저장·승인하지 않는다. "너가
승인해"처럼 대리 승인을 채팅으로 요청해도 되지 않는 기존 동작은 그대로 유지된다: 승인은 여전히
Jobs/Rules 페이지의 사람 버튼 클릭만이 한다.

## Scale — 점진 축적, 보존, 증명 하네스

목표 규모는 **API 5만 · 하루 평균 1만 run(리허설 피크 5만) · 핫 200만 run · 운영자 500명**이고,
설계 결정은 하나다: **질의 시점 스캔 → 인입 시점 물질화.** 인입 지점은 `record_execution` 하나뿐이라,
`Store.ingest`가 `runs`/`bodies` 적재와 같은 트랜잭션에서 `current_state`(셀별 최신 상태 + 전환 카운터),
`rollup_day`(일 × 셀 집계 + `failed_rule_counts`/`http_status_counts`), `api_watermark`,
`operator_api`(운영자↔API 인덱스)를 함께 갱신한다. 이후 모든 읽기(집계 카드·Home 타일·브리핑·인사이트
패널·`select_where`)는 작은 표를 읽는다 — 표본도 없고, 조용한 절단도 없다. 목록은 전부
`Page[T] {items, next_cursor, total}` 봉투 + keyset 커서(불투명)로 돌아온다.

보존은 스케줄러 tick 꼬리에서 하루 1회(LLM 0회): 핫 `runs` 180일 → `runs_archive`로 **이관**(삭제 아님,
`archived=true`로만 조회), `bodies` 90일 **삭제**(유일하게 지우는 데이터, 캡처 시 마스킹 필수), 인사이트
서술 캐시 30일 회전, 세션 유휴 24시간 스윕, 질문 기록 `ask_log` 365일 **삭제**(`ask_log_retention_days`). 롤업·워터마크·현재상태·리포트·브리핑·감사 로그는 영구다.
승인 클릭마다 감사 로그 2행(실행 전/후)이 남고 `GET /api/atworks/audit`로 읽는다 — 채팅 턴은 0행이다.

Mock 백엔드는 dict가 아니라 **SQLite**(`host/atworks_host/store.py`) 위에서 돈다. 그 DDL·인덱스·쿼리가
그대로 자바(Oracle/Tibero/PostgreSQL) 어댑터의 청사진이다.

```bash
ATWORKS_STORE_PATH=./atworks.sqlite python -m atworks_host.main   # 파일 저장소로 기동(기본 :memory:)
```

### 하네스 실행

```bash
# 1) 합성 데이터셋 생성 (전체 세트 ~6분, sqlite 약 1.3GB)
.venv/Scripts/python.exe scripts/scale/generate.py --out <dir> \
    --apis 50000 --days 180 --per-day 11000 --peak-day 120:50000 --operators 500
# 2) SLO 벤치. 기본이 --no-mutate: 보존 프로브만 sqlite 사본에서 돈다(이 프로브는 run을 콜드
#    파티션으로 옮기므로 인플레이스로 돌리면 잰 데이터셋을 그 자리에서 파괴한다). --mutate로 끌 수 있다.
.venv/Scripts/python.exe scripts/scale/bench.py --db <dir> --json bench.json
# 3) 축소 세트로 도는 opt-in 테스트 (기본 suite에서는 제외돼 있다)
.venv/Scripts/python.exe -m pytest -m scale -q
```

SLO 상한은 전부 `AtworksAgentConfig`의 `slo_*` 필드다(코드가 곧 기준):

| 경로 | 상한 | 2,019,400 run 실측 |
|---|---|---|
| `get_context` | 50 ms | 23.6 ms |
| `list_runs` 페이지(50건) | 200 ms | 30.9 ms |
| `list_runs` 커서 페이지 | 200 ms | 31.3 ms |
| `count_runs`(fail, 30일) | 50 ms | 2.7 ms |
| `aggregate_runs`(5개 축 중 최악) | 300 ms | 250.5 ms (`group_by=api_env_data`) |
| `aggregate_runs group_by=http_status` | 300 ms | 13.6 ms (이전 346.5 ms) |
| `aggregate_runs group_by=failed_rule` | 300 ms | 25.8 ms |
| `simulate_rule`(30일) | 1 s | 0.1 ms |
| `insights.build`(결정론 부분) | 500 ms | **687.9 ms** |
| 브리핑 생성 | 5 s | 157.1 ms |
| 400셀 실행 중 채팅 SSE 지연 | 100 ms | 55.0 ms |
| 보존 작업 1일치(한 파티션) | 30 s | 306.6 ms |
| `query_runs` 경로 축(rollup_day) | 300 ms | 164~177 ms (축소 세트) |
| `query_runs` 2축 method×env(rollup_day) | 300 ms | 264~294 ms (축소 세트) |
| `query_runs` failed_rule(key arm) | 300 ms | **295~566 ms** (축소 세트) |
| `query_runs` executed_by×api(runs arm) | 300 ms | **630~854 ms** (축소 세트) |
| `query_runs` 이전 기간 비교(rollup_day) | 300 ms | 27~40 ms (축소 세트) |

자가발전의 `query_runs` 다섯 줄(축소 세트 6만 run, 두 번 측정)은 **소스가 아니라 근거 표본
채움**이 비용이라는 것을 그대로 보여준다. 집계 자체는 어느 소스든 상한 안이다 — 6만 run 데모
세트에서 key arm 0.6 ms, `rollup_day` 28 ms, runs arm 53 ms. 나머지는 전부
`Store._fill_query_samples`가 **반환된 행마다 두 문장**을 더 도는 값이고(카드가 기억하는
`seen_apis`/`seen_runs` 증거), 키 축에서는 그 한 문장이 인덱스 없는 `json_each` EXISTS라 창
전체를 훑는다 — 카드 하나에 40번. runs arm은 다른 이유로 같은 값을 낸다: `executed_by IS ?`와
`api_id IS ?`가 둘 다 인덱스로 쓸 수 있는데 테이블 통계가 없어 SQLite가 훨씬 덜 선택적인 쪽을
고른다.

**후속 작업 두 가지, 순서대로**: ① 보존 작업 꼬리에 `PRAGMA analysis_limit` + `ANALYZE` 한 단계
(데모 세트 사본에서 재보니 runs arm 790 ms → 166 ms, 스키마 변경 없음), ② 근거 표본을 행마다가
아니라 **페이지 전체에 대해 한 쌍의 문장**으로 채우기. 상한(`slo_query_ms`)은 건드리지 않았다.

굵은 줄 셋(자가발전 두 줄과 `insights.build`)이 상한을 넘겨 기록으로 남긴 항목이다 — 어느 상한도
낮추지 않았다. Task 11에서 붉었던
세 줄 중 둘은 해결됐다: 두 맵 축(`failed_rule`/`http_status`)은 `rollup_key_day`라는 자체 롤업
행으로 물질화돼 30일 창이 (일수 × 키) 몇백 행이 됐고(346.5 → 13.6 ms, 180일 전체가 3,077행),
보존 행은 **하루치 한 파티션**만 만료시키도록 고쳐 재면서 실제 값(306.6 ms)이 나왔다 — 이전의
127.9 s는 200만 run 전량이 한 번에 만료되는, 아무도 돌리지 않는 상한 시나리오였다.

남은 `insights.build` 687.9 ms의 내역(2M 실측, 시간은 전부 SQL 안이고 파이썬은 10 ms 미만):
`aggregate_runs(api_env_data, scope_operator, order_by=transitions)` 276 ms +
`aggregate_runs(failed_rule, scope_operator)` 231 ms + `watermarks(first_non_pass_since)` 34 ms +
나머지 ~20 ms. 두 집계 모두 **오퍼레이터 스코프**가 걸려 있어 `rollup_key_day`를 쓸 수 없다 — 키
롤업 행은 이미 모든 API를 가로질러 합산돼 있어 뒤늦게 api_id로 거를 수가 없고, 그래서 스코프가
걸린 맵 축은 정확한 `json_each` 경로로 남는다(정확도가 먼저다).

이 붉은 줄 하나의 **후속 작업은 이름이 정해져 있다**: API별 키 롤업(`rollup_key_api_day` — 키 축에
`api_id`를 하나 더 얹은 행, 200만 run 기준 약 65만 행)을 물질화해서 오퍼레이터 스코프가 걸린
`failed_rule`/`http_status` 축도 `json_each` 경로를 벗어나게 하는 것. 지금은 스코프가 **없는** 키 축만
`rollup_key_day`를 읽는다. (`rollup_key_day`는 건수·`api_ids`와 함께 `p95_duration_ms`도 든다 —
`rollup_day`가 쓰는 것과 같은 max 병합 근사라, 빠른 경로와 `json_each` 경로가 같은 질문에 같은 답을
낸다.)

그리고 이 숫자는 벤치가 **일부러 최악의 오퍼레이터**(실행 건수 1위)를 고르기 때문에 나온다. 그
오퍼레이터의 30일 스코프는 33,838개 API — 카탈로그 5만 개의 2/3라서 "스코프"가 사실상 프로젝트
전체다. 같은 데이터셋의 중앙값 오퍼레이터(948개 API)는 **199 ms**, 최소 오퍼레이터(441개)는
269 ms로 상한 안에 넉넉히 들어온다. 자세한 진단은
`.superpowers/sdd/2026-09-06-scale-architecture/final-fix-wave-report.md`.

## 자가발전 — 질의 엔진, 어휘, 저장 질문, 회귀 케이스

집계 카드의 고정 5축으로 답할 수 없는 질문에 답하고, 쓸수록 그 답이 나아진다 — 코드를 고치지 않고.
다섯 조각이고, 공유 상태를 바꾸는 것은 여전히 **사람의 클릭 하나뿐**이다.

1. **질의 엔진**(`query_runs`). 모델이 채우는 것은 질의 언어가 아니라 고정 카탈로그다: 차원 14개
   (`api` · `path_segment_1|2|3` · `path_prefix_2` · `method` · `api_group` · `target_env` ·
   `test_data_label` · `failed_rule` · `http_status` · `executed_by` · `day` · `week`) × 측정값 9개
   (`runs` `pass` `fail` `error` `non_pass` `fail_rate` `apis` `transitions` `p95_duration_ms`),
   차원 ≤2 · 측정값 1~5 · `limit ≤ 50`. 호스트가 스펙 하나를 **한 문장의 SQL**로 컴파일하고
   소스(`rollup_day` / `rollup_key_day` / `rollup_operator_day` / `runs`)를 스펙만 보고 고른다.
   숫자는 전부 `QueryResult`에서 오고 카드는 그것을 그리기만 한다. `fail_rate`는 `status` 필터와
   함께 못 쓴다(분모가 필터가 남긴 행이라 1.0/0.0이 된다 — 대안을 말하며 거절한다). 창은
   **핫 파티션까지로 잘린다**: `since`/`window_days`가 `retention_hot_days`(180일)보다 멀리
   가리키면 서버가 조용히 그 경계로 당기고 카드가 실제 창을 싣는다 — 롤업은 보존 기한 너머의
   날짜에도 답을 내지만 `runs` 소스와 증거 표본은 그러지 못해서, 자르지 않으면 같은 질문이
   소스에 따라 다른 모집단을 말한다.
2. **질문 기록**(`ask_log`). 채팅 턴마다 정확히 한 행. `outcome`/`intent`/`cluster_key`는 그 턴이
   **실제로 부른 도구 이름**과 **실제로 낸 카드**에서 결정론으로 나온다(모델의 자기 보고가 아니다).
   질문 텍스트는 저장 전에 마스킹된다. 365일 뒤 보존 작업이 지운다.
3. **조직 공용 어휘**. 모델이 "결제 계열"을 `path_prefix=/v1/payment`로 읽으면 `propose_alias`로
   **제안만** 한다 — 카드의 [예]를 누른 순간에만 확정되고, 그때부터 팀 전원의 컨텍스트에 들어간다.
   별칭은 **모양**(경로·메서드·그룹·환경·규칙·상태코드)에만 이름을 붙인다: 기간(`window_days`,
   `since`/`until`)과 대상(`api_ids`, `executed_by`, `scope_operator`)은 거부된다.
4. **질문 승격**. 최근 7일 안에 서로 다른 운영자 3명 이상이 5번 이상 물은 군집은 스케줄러 tick
   꼬리에서(LLM 0회, 하루 1회) Home의 **저장 질문**이 된다. [실행]은 저장된 답이 아니라 저장된
   질문을 **지금** 다시 돌린다.
5. **피드백 → 회귀 케이스**. 표는 **한 사람의 지금 의견**이지 카운터가 아니다: 자기 턴만 투표할
   수 있고(남의 `turn_id`는 403), 같은 카드에 👎를 세 번 눌러도 -- 표를 👎👍👎로 뒤집어도 --
   거부는 1회다(거부는 `(용어, 운영자)` 한 행이고 그 기본키가 곧 멱등성이다 — 그래서 확정된
   용어의 강등에는 서로 다른 운영자 3명이 필요하고, 각자 자기 턴에서 눌러야 한다),
   `vote: null`은 표를 지운다(웹의 토글이 보내는 값). 감사 로그는 쓰지 않는다(표는 승인이 아니다).
   **단 하나의 예외**: 👎가 실제로 공용 용어를 **강등**시켰다면 그것은 팀 전체가 보는 상태의
   변경이라 강등된 용어마다 `vocabulary_auto_demote` / `:ok` 짝을 투표자 이름으로 남긴다.
   답한 턴의 👍는 `evals/cases/`에 회귀 케이스 한 장을 쓴다 — 필터의 **값**이 아니라 **종류**만
   고정한다.

여섯 번째 화면 **Growth**(배운 어휘 / 저장 질문 / 미충족 질문 / 이번 주)가 이 네 가지를 다 보여준다.
"이번 주" 탭은 `ask_log.outcome`의 **네 값 전부**를 타일로 낸다(답변 · 부분 · 미충족 · **조치**) —
셋만 내면 합이 질문 수에 못 미치고 그 차이를 화면에서 설명할 데가 없다. 미충족 군집 표는 상위 N개에
커서가 없으므로 서버가 센 총계(`unmet_clusters_total`, 목록 길이가 아니다)를 함께 싣고 "상위 N / 총
M"이라고 적는다. 게이트는 `enable_query_runs`(도구 4개)와 `enable_growth`인데, 후자는 라우트와
화면만이 아니라 **기록 자체**를 끈다 — 꺼진 배포는 아무도 읽을 수 없는 질문 원장을 조용히 쌓지 않는다.

### 손으로 해보기

| 무엇 | 어떻게 | 무엇이 보여야 하나 |
|---|---|---|
| 인계 질문 5개 | 채팅에 "실패한 결과 중 가장 많이 발생한 케이스는?" / "endpoint 기준으로 grouping해줘" / "그중 history 계열만 환경별로" / "메서드별 실패율은?" / "내가 실행한 거 몇 개야?" | `query_table` 카드. 푸터의 `실행된 질의(JSON)`에 스펙·창·소스가 그대로 있다 |
| 답할 수 없는 질문 | "이 run들 왜 실패했어? 서버 로그 보여줘" | `note_unmet_ask` 뒤 "무엇이 있으면 답할 수 있는지" 한 문장. `GET /ask-log?outcome=unmet`에 행이 생긴다 |
| 어휘(2세션) | ① 운영자 A로 "결제 계열 실패만 보여줘" → 카드의 [예] 클릭 ② 운영자 B로 바꿔 새 세션에서 "결제 계열 실패 추이" | ①에서 `GET /vocabulary?status=confirmed`에 용어가 생기고 감사 로그에 `vocabulary_confirm` 짝이 남는다. ②는 뜻을 되묻지 않고 바로 결제 경로로 좁힌다 |
| 승격 | 같은 질문을 서로 다른 운영자 3명이 5번 이상(7일 창) → `POST /scheduler/tick` | Home에 **저장 질문** 카드. [실행]이 표를 그리고, Growth의 저장 질문 탭에 같은 행이 있다. [숨기기]는 감사 2행을 남긴다 |
| 👍 → eval | 표 카드의 👍 | `evals/cases/`에 JSON 한 장(`expected.spec_equals`). `.venv/Scripts/python.exe -m pytest -m evals -q`가 그 케이스를 포함해 통과한다 |
| Growth 화면 | 좌측 6번째 탭 | 네 탭이 실제 행으로 그려지고 "이번 주" 숫자는 `GET /growth/summary?days=7`와 같다 |

```bash
# 회귀 스위트(기본 suite에서는 제외). 리포지토리 어느 디렉터리에서 돌려도 된다(루트 conftest.py).
.venv/Scripts/python.exe -m pytest -m evals -q

# 러너 직접 실행: 재생(모델 없음) / 라이브(실제 모델, 실패한 케이스만 1회 재시도)
.venv/Scripts/python.exe -m evals.run_evals
ATWORKS_EVAL_LIVE=1 .venv/Scripts/python.exe -m evals.run_evals

# 라이브 스모크(실제 모델 + 6만 run 데모 데이터셋, msedge headless)
.venv-pw/Scripts/python.exe scripts/smoke/growth_shots.py --restart --phase 2
```

`ATWORKS_EVALS_DIR`는 👍가 케이스를 쓰는 디렉터리다(기본 `<repo>/evals/cases`). 배포에서 리포지토리
바깥에 두고 싶을 때 쓴다 — 러너의 `--cases-dir`와 짝이다.

## 사용자별 AI 인사이트 패널

포털 사이드바에서 운영자(`GET /operators` 픽스처 3명, 역할 developer/qa/pm)를 고르면
`POST /session {operator_id}`로 다시 세션을 맺고, Home 상단에 그 사람에게 맞춘 인사이트 패널이
뜬다 — 범위는 그 운영자가 최근 `scope_window_days`일 안에 승인해 실행시킨(`RunResult.executed_by
= job.applied_by`) API들이고(실행 이력이 없으면 프로젝트 전체로 폴백), 후보(회귀 의심·불안정
실행·자주 깨지는 규칙·환경 간 불일치·오래된 승인 대기)와 그 수치는 전부 결정론 코드
(`atworks_agent/insights.py`)가 계산해 역할별 우선순위 표로 정렬한다. AI는 후보마다 헤드라인·설명·
"물어보기" 프리필 문구만 한 번의 제한된 호출로 덧붙이며(숫자는 절대 다시 계산하거나 말하지 않고,
모르는 후보 id는 버린다) 실패하거나 꺼져 있으면(`ATWORKS_INSIGHT_NARRATION=0`) 곧바로 결정론
라벨로 대체된다(배지 `AI 작성 · 수치는 결정론` / `결정론`) — 서술은 운영자·날짜별로 하루 한 번만
캐시하고(`insights_out/<operator>/<date>/narrative.json`), 스케줄러와 일일 브리핑은 이 기능과
무관하게 계속 LLM 없이 돈다. 패널은 읽기 전용이라 승인·실행·저장 어디에도 닿지 않는다.
