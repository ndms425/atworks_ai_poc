# 사용자별 AI 인사이트 패널 — 역할 × "내가 실행한 API" 범위

**Date:** 2026-09-06 · **Status:** design for review · **Builds on:** `aggregation.py`(RunGroup, flaky_v1,
regression_suspect, summarize_insights), the LLM-free daily briefing (`host/atworks_host/briefing.py`), the
session seam (`sessions.start`, `AtworksSessionContext.operator`), and the presentation/enrichment discipline
("the model selects and annotates; every fact is joined server-side").

## 1. 목적과 결정

Home에 **이 사용자에게 맞춘 AI 인사이트 패널**을 얹는다. "맞춤"의 두 축은 운영자 결정(2026-09-06):

- **(가) 역할** — `developer` / `qa` / `pm`. 무엇을 먼저 보여줄지의 **우선순위**와 서술의 **관점**을 바꾼다.
- **(나) 내 범위 = 내가 실행한 API들** — 프로필에 적어두는 정적 목록이 아니라 **실행 이력에서 파생**한다:
  최근 `scope_window_days`(기본 30) 안에 내가 실행시킨 run들의 API 집합. 내가 하는 일을 따라 움직인다.
- "내가 실행한"의 기준 = **그 job을 승인한 사람**(`JobSpec.applied_by`). 이 시스템의 실행은 전부 승인된 job을
  통해 일어나고, 초안은 모델이 만들었어도 실행을 일으킨 것은 승인 클릭이다. 실행 시점에 각 run에
  `executed_by = job.applied_by`를 찍는다.
- **AI 실행 시점: 지연 생성 + 하루 캐시.** 운영자별 첫 Home 방문(또는 "새로 분석" 클릭)에 서술을 생성해 그날
  캐시한다. 스케줄러 tick에서는 부르지 않는다 — 스케줄러 LLM 0회는 안전선이다.
- **실제 인증은 범위 밖.** MVP는 운영자 프로필 픽스처 + 포털의 운영자 선택으로 주체를 흉내 낸다. 실배포는
  사내 인증 → `operator_id` 매핑으로 갈아탄다(참조 `demo_common/sessions.py`가 정의한 seam 그대로).

참조 검토 결과(`refs/`): 부품은 다 있고 완성품은 없다. merchant 포털 Home의 "From the assistant" 패널은
**하드코딩 규칙 3개**가 만들고 각 항목이 채팅 프리필 `prompt`를 단다(`mock_merchant.py:223-301`); AI 분석은
`merchant_agent/analysis.py`의 "Compute; do not eyeball" 델리게이트가 유일한 안전 설계; open-design Live
Artifact가 `provenance.generatedBy: agent|refresh_runner` 어휘를 준다. 다중 사용자 개인화·모델 구성 레이아웃·
AI 주기 요약은 어느 참조에도 없다 — 이 스펙은 그 셋 중 첫째를 최소로 연다.

## 2. 안전 모델 (변하지 않음)

- **숫자는 결정론이 만든다.** 후보 인사이트와 그 수치(건수, 첫 실패 시각, 관련 id)는 `insights.py`의 순수
  함수가 `aggregation.py` 위에서 계산한다. 모델은 계산하지도, 수치를 다시 말하지도 않는다 — 카드의 수치는
  후보 레코드에서 서버가 렌더한다.
- **모델은 서술과 설명만 쓴다**: 후보마다 `headline / why_it_matters / prompt`. 후보 목록에 없는
  `candidate_id`는 버린다(provenance 게이트). 없는 인사이트를 지어낼 수 없다.
- **패널은 읽기 전용.** 승인·실행·저장 어디에도 닿지 않는다(승인 마크·ledger 불변을 테스트로 고정).
- **LLM이 죽어도 패널은 뜬다.** 서술 실패/비활성 시 후보의 결정론 라벨로 대체하고 `generated_by:
  "deterministic"`을 표시한다. AI는 향상이지 의존이 아니다.
- **기존 LLM-free 브리핑·리포트는 손대지 않는다.** AI가 쓴 화면은 별도 캐시(`insights_out/`)에 `generated_by:
  "agent"`로 구분 저장한다(Live Artifact 어휘).
- 서술 텍스트는 길이 상한 + 새니타이즈. 캐시는 운영자별 격리, 경로는 `SAFE_ID`.

## 3. 주체 — 운영자 프로필과 세션 바인딩

```
OperatorProfile { operator_id: str, name: str, role: "developer" | "qa" | "pm" }
```

- 픽스처 `host/atworks_host/fixtures/operators.json`에 데모 운영자 3명(역할 하나씩). `GET /operators`로 목록.
- `POST /session { operator_id? }` — 주어지면 그 프로필로, 없으면 기본 운영자로. `sessions.start(operator_id)`
  (지금은 project id를 넣는다 — `user_id` 자리에 운영자 id가 들어가는 것이 원래 seam의 의도).
  `context(record)`는 `operator = record.user_id`, `role = OPERATORS[operator].role`로
  `AtworksSessionContext`를 만든다(`role: OperatorRole | None` 필드 추가). 이후 요청은 세션 헤더만 나른다.
- `RunResult.executed_by: str | None = None`. Mock `execute_job_once`가 `job.applied_by`로 찍는다. 픽스처
  `runs.json`에도 데모 운영자를 나눠 적어 첫 화면부터 범위가 살아 있게 한다. 과거·미상 run은 `None` → 범위 밖.

## 4. 결정론 후보 생성 (`atworks_agent/insights.py`, 순수 함수)

- `operator_scope(runs, operator_id, window_days, now) -> set[str]` — `executed_by == operator_id`이고
  `executed_at >= now - window`인 run들의 `api_id` 집합.
- `InsightCandidate { candidate_id, kind, label, figures: dict[str, str|int], api_ids, ref_ids, priority }`
  — `candidate_id`는 결정론(`f"{kind}:{key}"`), `label`은 결정론 한국어 라벨(폴백 표시용).
- 종류(`InsightKind`)와 원천 — 전부 범위 안 API로 한정:

| kind | 뜻 | 계산 |
|---|---|---|
| `regression_suspect` | API 변경이 마지막 성공~첫 실패 사이 → 회귀 의심 | `aggregate(group_by="api")`의 `regression_suspect` |
| `flaky_cell` | pass↔fail 전환이 잦은 api×env×data | `aggregate(group_by="api_env_data")`의 `flaky` |
| `top_failed_rule` | 가장 많이 깨지는 규칙 | `aggregate(group_by="failed_rule")` 상위 |
| `env_divergence` | 같은 API가 환경/타깃별로 최신 상태가 갈림 | run들의 env별 최신 status 비교(결정론 프록시) |
| `stale_pending` | 승인 대기가 오래된 job | `status==STAGED`이고 `created_at`이 `stale_pending_hours` 이전 |

- **역할 우선순위 표**(결정론): developer → regression_suspect, top_failed_rule, flaky_cell, env_divergence,
  stale_pending / qa → flaky_cell, env_divergence, top_failed_rule, regression_suspect, stale_pending / pm →
  stale_pending, top_failed_rule, regression_suspect, flaky_cell, env_divergence. 정렬 = (역할 순위, -심각도
  건수, key). 상한 `max_insight_candidates`(기본 5).
- `candidate_insights(runs, apis, jobs, scope, role, config, now) -> list[InsightCandidate]`.
- 범위가 비면(새 사용자) 호출자가 `scope=None`으로 **전체** 기준을 요청할 수 있고, 결과 패널은
  `scope_fallback=True`를 표시한다.

## 5. AI 서술 (`atworks_agent_runtime/insight_narrator.py`, 한 번의 제한된 호출)

- `narrate_insights(client, config, candidates, role) -> list[InsightNarrative]`. 비스트리밍
  `messages.create` 1회, 도구 하나(`submit_insights`, `tool_choice` 강제), 후보는 **울타리 친 데이터**로 시스템
  프롬프트에 넣는다. 지시: "계산하지 마라, 수치를 다시 쓰지 마라, 후보마다 headline/why_it_matters/prompt만,
  `role` 관점으로(개발자: 원인 가설 방향 / QA: 재현·환경 / PM: 영향·일정)".
- `InsightNarrative { candidate_id, headline ≤80, why_it_matters ≤160, prompt ≤120 }` — pydantic 상한 +
  fence 새니타이즈. 후보에 없는 `candidate_id`는 버리고 note. 예외·타임아웃(`insight_narration_timeout_s`)이면
  빈 목록 → 호출자가 결정론 폴백.
- `enable_insight_narration=False`면 호출 자체를 건너뛴다(폴백 경로의 결정론 테스트 스위치).

## 6. 호스트 — 패널 조립과 캐시 (`host/atworks_host/insights.py`)

- `InsightPanels(out_dir, config, narrator)`:
  - `build(backend, session, now, *, refresh=False) -> InsightPanel` — 매 호출 **범위와 후보를 실시간 재계산**;
    서술은 `insights_out/<operator_id>/<YYYY-MM-DD>/narrative.json`에 그날 캐시가 있고 `refresh=False`면 재사용,
    없으면(또는 `refresh=True`) 서술 호출 후 저장(`generated_at`, `generated_by: "agent"`). 서술이 비면
    `generated_by: "deterministic"`. 캐시된 서술이 오늘 후보에 없는 id를 가리키면 그 항목은 라벨 폴백.
  - 경로: `SAFE_ID`(운영자 id)·`SAFE_DATE`, `is_relative_to` 탈출 검사 — `reports.py`와 같은 규칙.
- `InsightPanel { operator_id, name, role, scope_api_ids, scope_fallback, window_days, generated_at,
  generated_by, items: [{candidate, narrative | None}] }`.
- 라우트: `GET /home/insights`(세션) → 패널 JSON; `POST /home/insights/refresh`(세션) → 재서술 후 패널.
  `GET /operators`(세션 불필요) → 프로필 목록. 기존 `/briefings/*`는 변경 없음.
- `get_context`에 `operator_role`과 `scope_api_ids`(상한 20)를 추가 → 동적 컨텍스트 블록에 "operator ·
  role · scope: n APIs you ran" 한 줄. 채팅이 "내 범위" 관점으로 답할 수 있게. 도구의 기본 필터는 바꾸지 않는다.

## 7. 웹

- **운영자 선택**: 셸 헤더(운영자 이름/역할 자리)에 픽커. 선택은 `localStorage`에 기억되고 `POST /session
  {operator_id}`로 새 세션을 만든다(데모용 로그인 흉내).
- **Home 상단 "AI 인사이트" 패널**: 제목 `AI 인사이트 — {name} · {role} · 내 범위 API n개`(또는 `범위: 전체`
  배지). 카드 3~5개: `headline`(없으면 `label`), `why_it_matters`, 수치 pill들(`figures`), "물어보기" 칩(`prompt`
  프리필 → 채팅), 관련 id들. 배지 `AI 작성 · 수치는 결정론` 또는 `결정론` + 생성 시각. "새로 분석" 버튼 →
  `POST /home/insights/refresh`.
- 기존 브리핑 카드·Needs attention 타일은 그대로 아래에 둔다.

## 8. 설정

`enable_insight_panel: bool = True`, `enable_insight_narration: bool = True`, `scope_window_days: int = 30`,
`max_insight_candidates: int = 5`, `stale_pending_hours: int = 24`(브리핑의 stale 기준과 같은 값을 쓰거나 이미
있으면 재사용), `insight_narration_timeout_s: int = 20`, `insight_narration_model: str | None = None`(None이면
`config.model`; 더 싼 모델을 지정할 자리).

## 9. 범위 밖 (기록)

- 실제 인증/SSO — 운영자 픽커로 대체; seam은 유지.
- 메모리 기반 학습(다 안) — `enable_memory=False` 유지, 폐쇄망 정책 검토 후 후속.
- 모델이 위젯을 골라 레이아웃을 짜는 대시보드(C안) — 참조 사례 0, 후속.
- 스케줄러 내 LLM(B안, 밤마다 AI 브리프) — 2단계 후보. 이 스펙은 요청 경로에서만 LLM을 부른다.
- 도구 기본 범위 필터("내가 돌린 것만 보여줘"를 기본으로) — `executed_by`가 생겨 자리는 있음, 후속.
- 사용자 간 데이터 격리 보장 — 단일 테넌트 전제(범위는 편의이지 권한이 아니다).
- 수치 서술 검증(모델이 숫자를 썼는지 자동 검출) — 프롬프트 지시 + 카드가 서버 수치만 렌더하는 것으로 충분.

## 10. 테스트/검수 포인트

- `operator_scope`: 본인·기간 내 run만; `executed_by None`/타인/기간 밖 제외; 빈 결과.
- `candidate_insights`: 각 kind가 픽스처에서 나오는지; 범위 밖 API 제외; 역할별 정렬이 표와 일치; 상한.
- Mock `execute_job_once`가 `executed_by = applied_by`를 찍는다; 픽스처 run에 운영자가 있다.
- 세션: `POST /session {operator_id}` → 컨텍스트에 operator·role; 미지정 시 기본; 미지의 id → 400.
- 서술기: 페이크 클라이언트로 — 정상 응답 파싱, 미지의 candidate_id 드롭+note, 길이 초과 거부, 예외/타임아웃 →
  빈 목록; `enable_insight_narration=False` → 호출 0회.
- 패널: 캐시 히트(같은 날 두 번째 호출은 서술 호출 0회), `refresh=True`는 재호출, 날짜/운영자별 격리, 폴백
  `generated_by="deterministic"`, `scope_fallback` 플래그, 경로 탈출 거부.
- 안전: 패널 build/refresh 전후 승인 마크·ledger·run 수 불변.
- 컨텍스트: 동적 블록에 role/scope 한 줄, 없을 때 바이트 불변.
- 웹: 빌드 clean; 운영자 전환 시 패널 재조회; 라이브 스모크 — 개발자/QA 두 운영자로 로그인해 패널 순서가
  다르고 수치가 같으며, "물어보기"가 채팅을 프리필하고, `enable_insight_narration=False` 호스트에서 결정론
  폴백이 뜬다.
