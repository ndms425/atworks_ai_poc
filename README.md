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
