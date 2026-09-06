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
서술 캐시 30일 회전, 세션 유휴 24시간 스윕. 롤업·워터마크·현재상태·리포트·브리핑·감사 로그는 영구다.
승인 클릭마다 감사 로그 2행(실행 전/후)이 남고 `GET /api/atworks/audit`로 읽는다 — 채팅 턴은 0행이다.

Mock 백엔드는 dict가 아니라 **SQLite**(`host/atworks_host/store.py`) 위에서 돈다. 그 DDL·인덱스·쿼리가
그대로 자바(Oracle/Tibero/PostgreSQL) 어댑터의 청사진이다.

```bash
ATWORKS_STORE_PATH=./atworks.sqlite python -m atworks_host.main   # 파일 저장소로 기동(기본 :memory:)
```

### 하네스 실행

```bash
# 1) 합성 데이터셋 생성 (전체 세트 ~6분, sqlite 약 1.8GB)
.venv/Scripts/python.exe scripts/scale/generate.py --out <dir> \
    --apis 50000 --days 180 --per-day 11000 --peak-day 120:50000 --operators 500
# 2) SLO 벤치
.venv/Scripts/python.exe scripts/scale/bench.py --db <dir> --json bench.json
# 3) 축소 세트로 도는 opt-in 테스트 (기본 suite에서는 제외돼 있다)
.venv/Scripts/python.exe -m pytest -m scale -q
```

SLO 상한은 전부 `AtworksAgentConfig`의 `slo_*` 필드다(코드가 곧 기준):

| 경로 | 상한 | 2,019,000 run 실측 |
|---|---|---|
| `get_context` | 50 ms | 21.4 ms |
| `list_runs` 페이지(50건) | 200 ms | 30.9 ms |
| `count_runs`(fail, 30일) | 50 ms | 2.7 ms |
| `aggregate_runs`(5개 축 중 최악) | 300 ms | **346.5 ms** (`group_by=http_status`) |
| `simulate_rule`(30일) | 1 s | 0.1 ms |
| `insights.build`(결정론 부분) | 500 ms | **728 ms** |
| 브리핑 생성 | 5 s | 157.9 ms |
| 400셀 실행 중 채팅 SSE 지연 | 100 ms | 61.8 ms |
| 보존 작업 1일치 | 30 s | **127.9 s**(단, 200만 run **전량**이 한 번에 만료되는 상한 시나리오) |

굵은 세 줄은 상한을 넘겨 **기록으로 남긴** 항목이다(상한을 낮추지 않았다). 보존 행은 200만 run
전량이 하루에 만료되는 상한 시나리오라 실제 하루치(≈1.1만 run)로 환산하면 1초 미만이다. 나머지 두
줄은 뿌리가 같다: 이 데이터 모양에서 `rollup_day`가 101만 행(run 202만 건 = **롤업 1행당 run 2건**)
이라 "작은 표 읽기"가 여전히 30일 창 17만 행 GROUP BY이고, `failed_rule`/`http_status` 축은 그 위에
행마다 JSON 맵을 `json_each`로 펼친다(어떤 인덱스도 덮을 수 없는 구간). 시간이 전부 SQL 안에 있고
파이썬 쪽은 10ms 미만이다. 축소·중간 세트(45만 run)에서는 두 줄 다 녹색이다. 다음 수는 두 맵 축을
JSON이 아니라 `(day, rule)`·`(day, http_status)` 자체 롤업 행으로 물질화하는 것 — 이번 브랜치 범위
밖이다. 자세한 진단은 `.superpowers/sdd/2026-09-06-scale-architecture/task-11-report.md`.

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
