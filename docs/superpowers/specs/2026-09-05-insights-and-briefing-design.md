# Insights & Briefing — 실패 묶기, 영향 범위 실행, 불안정 타일, 첨부 확장, 일일 브리핑

**Date:** 2026-09-05 · **Status:** approved by operator (chat, 2026-09-05) · **Builds on:**
`2026-09-03-atworks-ai-chat-mvp.md` (Parts A–D), `2026-09-04-multi-dimension-jobs-design.md`

## 1. 목적과 원칙

채팅이 답할 수 있는 질문의 폭을 넓힌다. 다섯 항목을 한 설계서에 담고, 구현은 §3→§7 순서로
항목마다 커밋한다. 모두 다음 세 원칙 위에서만 움직인다.

1. **모델은 축과 표시를 고르고, 숫자는 서버가 낸다.** 집계·첫 실패 시점·불안정 여부는
   이름 붙은 결정론 규칙(`flaky_v1`)의 출력이다. 카드의 모든 수치는 세션 기록 조인이다.
2. **쓰기 경로는 그대로.** 새로 생기는 것은 `stage_job`의 선택 조건뿐이며, 해석은 서버가 하고
   결과는 기존 guardrail(`check_job_guardrails`)과 승인 표면(Jobs 페이지)을 그대로 탄다.
3. **스케줄러는 LLM 0회를 유지한다.** 브리핑은 리포트와 같은 "data.json + 템플릿" 경로다.

집계 위치는 **호스트(파이썬)**다. `AtworksBackend`에는 메서드를 추가하지 않는다 — 호스트가
`list_runs`로 기간 내 기록을 상한까지 받아 계산한다(운영자 결정, Q1). 대량 데이터 환경에서
Java가 직접 집계해야 하면 그때 `aggregate_runs` ABC 메서드를 추가하되, 툴·카드·계산 규칙은
그대로 두고 데이터 공급자만 바꾼다.

## 2. 공통 기반 (§3·§5·§7이 공유)

### 2.1 집계 모듈 `atworks_agent/aggregation.py`

순수 함수. 입력은 `Sequence[RunResult]`와 `Mapping[str, ApiSpec]`, 축, 옵션. 출력은
`RunGroup` 목록(pydantic).

```python
GroupBy = Literal["api", "failed_rule", "http_status", "env", "api_env_data"]

class RunGroup(BaseModel):
    key: str                      # 축 값. api → api_id, failed_rule → 규칙 문자열, http_status → "500",
                                  # env → "dev", api_env_data → "api-003|dev|음수금액"
    label: str                    # 사람용 라벨. api → "POST /v1/payments/refund", 나머지는 key
    count: int
    fail: int
    error: int
    passed: int
    run_ids: list[str]            # 그룹에 속한 run_id (최신순, 최대 50)
    first_non_pass_at: datetime | None     # 그룹 안 가장 이른 non_pass
    last_pass_before: datetime | None      # first_non_pass_at 이전의 마지막 pass
    latest_status: RunStatus | None        # 가장 최근 run의 상태
    transitions: int              # 시간순 pass↔non_pass 전환 횟수
    flaky: bool                   # flaky_v1: transitions >= config.flaky_min_transitions (기본 2)
    p95_duration_ms: int | None   # duration_ms가 있는 run 기준 p95 (nearest-rank)
    regression_suspect: bool      # 축이 api일 때만: last_pass_before < api.updated_at <= first_non_pass_at
    api_updated_at: datetime | None        # 축이 api일 때만
```

규칙:

- `failed_rule` 축은 `failed_rules`의 각 원소를 키로 한다(한 run이 두 규칙이면 두 그룹에 든다).
  규칙이 없는 non_pass(error)는 키 `"(error) HTTP <http_status>"`로 묶인다. pass는 이 축에서 제외한다.
- `http_status` 축은 `http_status`가 None이면 `"(none)"`.
- 정렬: `fail + error` 내림차순 → `count` 내림차순 → `key` 오름차순. 결정론이다.
- `transitions`는 `executed_at` 오름차순으로 훑으며 `status == pass` 값이 바뀔 때마다 1.
- `p95`는 nearest-rank: 정렬된 값의 `ceil(0.95 * n) - 1` 인덱스.
- 빈 입력이면 빈 목록. 예외를 내지 않는다.

`summarize_insights(runs, apis, config) -> Insights` 도 여기 둔다:
`{flaky: int, regression_suspect: int}` — `api_env_data` 축의 flaky 그룹 수와 `api` 축의
regression_suspect 그룹 수. §5와 §7이 쓴다.

### 2.2 설정 (`AtworksAgentConfig`)

| 필드 | 기본 | 뜻 |
|---|---|---|
| `max_aggregate_runs` | 2000 | 집계에 넣는 run 상한. 백엔드 `list_runs(limit=…)`에 그대로 전달 |
| `max_aggregate_window_days` | 30 | `since`가 없거나 더 오래면 `now - 30d`로 자른다 |
| `flaky_min_transitions` | 2 | flaky_v1 임계 |
| `max_group_items` | 12 | 카드에 올릴 그룹 수 상한(스키마 `maxItems`) |
| `impact_path_segments` | 2 | §4 `related_to`의 경로 prefix 세그먼트 수 |
| `briefing_enabled` | True | §7 |
| `briefing_at` | `"09:00"` | §7 생성 시각 (HH:MM) |
| `briefing_tz` | `"Asia/Seoul"` | §7 |
| `aggregate_intent_terms` | ("묶어","원인별","언제부터","왔다갔다","불안정","flaky","패턴","cluster","since when") | §2.4 grounding 어휘 |

모든 상한은 설정 함수이며 툴 스키마(`maxItems`, `maximum`)에도 같은 값이 들어간다(기존 규칙).

### 2.3 툴 `aggregate_runs` (읽기)

```json
{"name": "aggregate_runs",
 "description": "Group this project's run results by one axis and return per-group counts, first-failure time, flakiness (flaky_v1) and p95 duration — all computed by the host, never by you. Use it for 'group failures by cause', 'since when is X broken', 'which APIs flap'. Call it before present_run_groups. / 실행 기록을 한 축으로 묶어 집계한다. 숫자는 서버 계산.",
 "input_schema": {"type":"object","properties":{
    "group_by":{"type":"string","enum":["api","failed_rule","http_status","env","api_env_data"]},
    "since":{"type":"string","description":"ISO datetime; default now-30d (config cap)"},
    "status":{"type":"string","enum":["pass","fail","error","non_pass"]},
    "api_id":{"type":"string","description":"Narrow to one API this session saw (search_apis/get_api)."}},
  "required":["group_by"],"additionalProperties":false}}
```

핸들러(executor):

1. `since`를 `max_aggregate_window_days`로 자른다. `api_id`가 있으면 `seen_apis`에 있어야 한다
   (없으면 `InvalidToolArgument` → 모델에 "search_apis 먼저").
2. `backend.list_runs(session, since, status, api_id, limit=max_aggregate_runs)`.
   run은 `remember_run`, api는 필요 시 `get_api`로 채워 `remember_api`.
3. `aggregate(...)` → 그룹을 `state.seen_groups: dict[str, RunGroup]`(키 `f"{group_by}:{key}"`)에 기록,
   `state.last_population = len(runs)`, `state.last_listed_filter`,
   `state.last_group_by = group_by`, `state.last_aggregate_since = since`.
4. 모델에는 상위 `max_group_items * 2`개 그룹을 펜스 안 텍스트로 돌려준다(키, 라벨, count/fail/error,
   first_non_pass_at, flaky, regression_suspect). 나머지는 "… and N more groups".

툴 목록 순서: `rank_failed_runs` 바로 뒤. 프롬프트 바이트는 여전히 설정의 함수다.

### 2.4 grounding 규칙 `aggregate`

`GroundingRule("aggregate", "aggregate_runs", _aggregate, prefetch_intro=...)`. 매칭은 기존
`_runs`와 같은 방식(한글 부분 문자열 + 요청 어미): `aggregate_intent_terms` 중 하나 **그리고**
`runs_intent_cues`(가져와·보여·알려·…) 중 하나. `runs` 규칙보다 **앞에** 둔다 — "실패 원인별로
묶어줘"는 runs 어휘도 맞으므로 먼저 검사해야 한다. prefetch는 하지 않는다(축을 모델이 골라야
하므로 `tool_choice` 강제만).

### 2.5 카드 `present_run_groups` → 컴포넌트 `run_groups`

모델 입력:

```json
{"title": "string ≤80", "group_keys": ["string", … ≤ max_group_items], "note": "string ≤200 (optional)"}
```

서버 조인(`enrich_run_groups`): 각 키를 `seen_groups[f"{last_group_by}:{key}"]`에서 찾는다.
하나도 못 찾으면 `PresentationRefused(gate=PROVENANCE_GATE)`, 일부만 없으면 그 키만 빼고 노트에
"Dropped …". `last_population`이 None이면 `ValueError("aggregate_runs first")`. payload:

```json
{"title","note","group_by","population","since","shown",
 "items":[{"key","label","count","fail","error","passed","first_non_pass_at","last_pass_before",
           "latest_status","transitions","flaky","p95_duration_ms","regression_suspect","api_updated_at",
           "run_ids"}]}
```

웹 `RunGroupsCard`: 헤더 "기간 내 N건 · 축 · 그룹 M개", 표(라벨 / 건수 / 실패·에러 / 첫 실패 /
표시: `flaky`→"불안정" 칩, `regression_suspect`→"회귀 의심" 칩 / p95). 행의 run_ids 첫 항목은
"채팅에 첨부" 버튼(§6). `close_on_presentation`에 `present_run_groups` 포함.

### 2.6 게이트·리마인더

- 산문 수치 재진술 검사(`prose_restates_figures`)는 `run_groups` payload의 count/fail/error/p95도
  본다.
- 프롬프트 Hard line 추가 1줄: "Group counts, first-failure times and flakiness come from
  aggregate_runs; never compute or restate them yourself."

## 3. 항목 1 — 실패 묶기 + 첫 실패 시점

§2만으로 동작한다. 여기서는 절차와 픽스처를 정한다.

- **묶기**: "이번 주 실패 원인별로 묶어줘" → `aggregate_runs {group_by: failed_rule, status: non_pass,
  since: 7d}` → `present_run_groups`. error는 `"(error) HTTP 500"`류 키로 함께 나온다.
- **첫 실패 시점**: "환불 API 언제부터 깨졌어?" → (모르는 API면 `search_apis`) →
  `aggregate_runs {group_by: api, api_id}` → 카드 한 행: 첫 non_pass, 직전 pass, 스펙 수정일,
  회귀 의심 여부. 모델은 그 행을 사람 말로 한 문장 설명하되 시각은 카드가 보여준다.
- failed-triage 스킬에 위 두 절차를 추가한다. 스킬 인덱스 한 줄 갱신(프롬프트 바이트 변경은
  스킬 파일이 바뀔 때만).
- 픽스처 `runs.json`에 결정론 검증용 데이터를 추가한다: api-004에 pass→fail→pass→fail(불안정),
  api-012에 `updated_at`이 마지막 pass와 첫 fail 사이인 회귀 케이스. 기존 30건과 기존 테스트가
  기대하는 집계(fail 7, error 2)는 유지되도록 새 run은 **pass 또는 기존 카운트에 이미 포함된
  api의 non_pass 재배치**로만 구성한다. 바뀌면 Home 기대값 테스트도 함께 갱신한다.

## 4. 항목 2 — 실패만 재실행 / 영향 범위 실행

### 4.1 `SelectWhere` 확장

```python
class SelectWhere(BaseModel):
    query: str = ""                       # 기존
    group: str | None = None              # 기존
    updated_after: datetime | None = None # 기존
    failed_since: datetime | None = None  # NEW: 이 시각 이후 non_pass run이 하나라도 있는 API
    related_to: str | None = None         # NEW: 이 api_id와 같은 group 또는 같은 경로 prefix인 API
```

해석(`resolve_select_where`, 백엔드 안 — Mock은 `search_apis` + `list_runs`로, REST도 같은
조합): 조건은 AND. `related_to`는 (a) `group`이 같거나 (b) 경로의 앞 `impact_path_segments`
세그먼트가 같은 API(자기 자신 포함). `failed_since`는 `list_runs(since=failed_since,
status="non_pass")`의 `api_id` 집합. 둘 다 결정론이며 재해석(LATE)도 같은 함수를 쓴다.

### 4.2 stage_job 스키마

`select_where.properties`에 `failed_since`(ISO), `related_to`(`_SESSION_API_ID`) 추가.
`related_to`는 세션에서 본 api_id여야 한다(출처 게이트, 기존 api_ids와 같은 규칙). 해석 결과
api_ids는 서버가 채우므로 출처 조건은 구성상 만족되고, 이후 `check_job_guardrails`
(`max_apis_per_job`, 매트릭스 상한)는 변경 없이 적용된다. 해석 결과가 0개면 스테이징 거부
(`InvalidToolArgument`: "no API matches; widen the window or name APIs").

### 4.3 선택 근거

`JobSpec.selection_basis: str | None` (서버 생성 문장, ≤160자)을 추가한다. 예:
"api-003과 같은 group(payments) 또는 /v1/payments/* — 3개", "2026-08-29 이후 실패한 API — 4개".
`job_record`에 실리고, job 미리보기 카드와 Jobs 행에 "선택 근거" 줄로 표시된다. 모델 문장이
아니다.

### 4.4 절차

schedule-run 스킬에 두 절차 추가: "실패만 다시 돌려" → `failed_since` (기간이 없으면 24h),
"X 고쳤는데 뭘 다시 돌려야 해?" → `related_to: X`, 그리고 어느 쪽이든 기존 슬롯(계·스케줄·데이터)
규칙은 그대로. grounding 강제는 없다(쓰기 툴).

## 5. 항목 3 — 불안정 탐지 타일

- 라우트 `GET /api/atworks/runs/insights` (세션 헤더 필요, 다른 목록 라우트와 동일) →
  `{"flaky": int, "regression_suspect": int, "window_days": int}`. `summarize_insights` 재사용,
  기간은 `max_aggregate_window_days`. LLM 없음.
- Home `Needs attention`에 타일 **불안정**(`flaky`) 추가. 클릭 → "요즘 왔다갔다 하는 API 뭐야".
  `regression_suspect`는 타일로 두지 않고(4개 타일 유지) 브리핑(§7)에만 싣는다.
- `lib/api.ts`에 `fetchInsights()`; HomeView는 `loadCounts`에 병렬로 붙인다.

## 6. 항목 4 — 화면 첨부 확장

### 6.1 kind별 힌트

`render_attached_items_hint`가 kind에 따라 다른 줄과 스코프 문장을 낸다.

| kind | 줄 | 스코프 문장 |
|---|---|---|
| run | (기존) field / actual / expected | (기존) 판정 재해석 금지 |
| api | method, path, group, has_rules, params(≤8) | "Answer about this API's spec, params and its runs only. Staging a job for it is allowed if asked." |
| job | summary, status, target_envs, executions/total, runs_total | "Answer about this job only. Approval happens on the Jobs page; you cannot approve it." |

포털이 보내는 `AttachedItem`은 kind와 ref_id, label만 필수이고 서버가 세션/백엔드에서 나머지를
채운다(`get_api`/`ledger.get`). 세션에 없으면 백엔드에서 읽어 `remember_*` 한다 — 첨부는 운영자의
명시적 행동이므로 출처로 인정한다(기존 run 첨부와 같은 규칙).

### 6.2 포털

- APIs 행: "채팅에 첨부" 버튼(`kind: "api"`). Jobs 행: "채팅에 첨부"(`kind: "job"`). 기존
  `onAttach` 경로 재사용, `MAX_ITEMS=8` 유지.
- `RunGroupsCard` 행: 첫 run_id를 `kind: "run"`으로 첨부.
- 리포트 페이지: 실행 목록의 run_id 셀에 "채팅에서 보기" 링크 →
  `{portal_origin}/?attach=run:{run_id}`. 포털 `page.tsx`는 마운트 시 `attach` 쿼리를 읽어
  `pendingAttachments`에 넣고(라벨은 `fetchRuns`로 보강, 실패 시 run_id 그대로) URL에서 제거한다.
  `portal_origin`은 호스트 설정 `ATWORKS_PORTAL_ORIGIN`(기본 `http://localhost:3110`)이며 템플릿에
  escape 되어 들어간다.

## 7. 항목 5 — 일일 브리핑

### 7.1 데이터 (`host/atworks_host/briefing.py`)

`build_briefing(now, runs_24h, apis, jobs, config) -> dict`:

```json
{"date": "2026-09-05", "generated_at": "...", "window": {"from": "...", "to": "..."},
 "counts": {"total": n, "pass": n, "fail": n, "error": n},
 "top_groups": [ {RunGroup 축 failed_rule 상위 3} ],
 "insights": {"flaky": n, "regression_suspect": n},
 "jobs": {"executed": [{"job_id","summary","runs"}], "pending": [{"job_id","summary","created_at"}],
          "stale_pending": n},
 "note": "statuses are aTworks rule verdicts; no model output on this page"}
```

창은 `[전날 briefing_at, 오늘 briefing_at)` (`briefing_tz`). `stale_pending`은 스테이징 후 24시간
넘은 job 수. 모든 값은 서버 계산이고 산문은 없다(범위 밖, §9).

### 7.2 생성과 저장

`Briefings` 클래스(리포트와 같은 모양): `root/briefings/{date}/data.json` + 렌더된
`index.html`(템플릿 `briefing_template.html`, 리포트와 같은 `esc()`·`<` 규칙).
`Scheduler.tick(now)` 끝에서 `briefings.maybe_generate(now)`: `now`가 오늘 `briefing_at`을
지났고 오늘 파일이 없으면 생성. 파일 존재로 멱등. 생성 실패는 로그만, tick은 계속(기존 M12 원칙).
스케줄러가 모델 클라이언트를 import하지 않는다는 기존 테스트가 브리핑 모듈도 덮도록 확장한다.

### 7.3 라우트와 화면

- `GET /api/atworks/briefings/latest` → 최신 `data.json`(없으면 404). 세션 헤더 필요.
- `GET /api/atworks/briefings/{date}` → 렌더된 HTML. 리포트처럼 세션 없음, `SAFE_ID` 규칙
  (`YYYY-MM-DD`만 허용).
- Home 상단 `BriefingCard`: 날짜, 4개 숫자(전체/실패/에러/불안정), 상위 그룹 3개 라벨, 승인 대기·
  오래된 대기 수, "브리핑 열기" 링크. 숫자 클릭은 채팅 질의 프리필. 브리핑이 없으면 카드 대신
  한 줄 "오늘 브리핑은 09:00에 생성됩니다".

## 8. 테스트와 검수

| 영역 | 테스트 |
|---|---|
| 집계 | 고정 픽스처로 그룹 수·정렬·first_non_pass·last_pass_before·transitions·flaky·p95·regression_suspect 값 고정. 빈 입력. 두 규칙 run이 두 그룹에 드는 것 |
| 툴 | `aggregate_runs` 기간 클램프, 모르는 api_id 거부, `seen_groups`·population 기록, 툴 텍스트에 상위 그룹만 |
| 카드 | population 없음 → 거부, 모르는 키 → 출처 게이트, 일부 키 드롭 노트, payload 수치가 세션 값과 일치, close_on_presentation |
| grounding | 어휘+어미 매칭, runs 규칙보다 우선, 영문 |
| 스키마 바이트 | 툴 목록/프롬프트가 설정의 함수임(기존 스냅샷 테스트 갱신) |
| SelectWhere | `related_to` group/prefix 해석, `failed_since`, AND 결합, 0개 거부, 세션에 없는 related_to 출처 거부, guardrail 상한 적용, `selection_basis` 문장, LATE 재해석 동일 |
| 라우트 | `/runs/insights` 값·세션 필요, `/briefings/latest` 404→200, `/briefings/{date}` SAFE_ID, HTML escape |
| 스케줄러 | 브리핑 하루 1회 멱등, `briefing_at` 이전엔 미생성, 생성 실패가 job 실행을 막지 않음, LLM 클라이언트 미import |
| 첨부 | kind별 힌트 줄·스코프 문장, 세션에 없는 api/job을 백엔드에서 채움, 경계 태그 제거 유지 |
| 웹 | `next build` 클린; RunGroupsCard/BriefingCard 렌더 스냅샷은 두지 않음(기존 관례) |
| 라이브 | smoke_chat에 "이번 주 실패 원인별로 묶어줘" 추가; 브라우저: 묶기·언제부터·실패만 재실행·영향 범위·불안정 타일·API/Job 첨부·리포트→첨부·브리핑 카드 캡처 |

커밋 단위: §2+§3 → §4 → §5 → §6 → §7. 각 단위는 `pytest -q`·ruff 통과, §5 이후는 `next build`도.

## 9. 범위 밖 (후속 후보)

- 브리핑 산문 한 단락(LLM) — 켜면 스케줄러 LLM-free 불변식이 깨지므로 별도 경로로 설계.
- 메신저(웹훅) 발송.
- "최근 함께 실패한 API" 상관 기반 영향 범위.
- 응답 본문 diff(백엔드 응답 저장 필요), 스펙 버전 diff, 응답시간 SLA 질의(p95는 카드에 나오지만
  SLA 임계 판정은 아직 없음).
- `aggregate_runs`의 백엔드 네이티브 구현(ABC 메서드) — 데이터 규모가 커질 때.
