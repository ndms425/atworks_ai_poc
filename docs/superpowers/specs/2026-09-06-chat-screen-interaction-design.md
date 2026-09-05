# 채팅 ↔ 화면 상호작용 — 화면 지시(directive)와 화면 인지(screen_state)

**Date:** 2026-09-06 · **Status:** design for review · **Builds on:** the shipped presentation-tool
pipeline (`tools/registry.py` → `tools/presentation.py` → `enrichment.py` → `ui` event → web card),
the `change_update` "portal executes it" event pattern (`web-shared/portal/merchant.ts`), and the
attachment / `ref_id` system (`attachments.py`, `?attach=` deep link).

## 1. 목적과 결정

채팅과 화면이 **양방향**으로 만나게 한다. 지금은 화면 → 채팅 한 방향만 있다(조작자가 항목을 붙여
보낸다). 이 스펙은 반대 방향을 연다: **채팅이 화면을 움직이고(이동·항목 열기·필터), 화면에 손가락을
얹는다(강조).**

운영자 결정(2026-09-06):

- **대리 승인은 하지 않는다(취소).** "너가 승인해"라고 쳐도 승인되지 않는 현행을 **그대로 유지**한다.
  승인은 오직 사람의 버튼 클릭(호스트 라우트)만이 한다. 이 스펙의 어떤 지시도 승인 마크를 건드리지
  않는다. (검토 결과: 채팅 텍스트는 모델의 자기판단이나 첨부/외부 내용의 프롬프트 인젝션으로 오염될 수
  있어 "진짜 사람의 의사"를 보장할 수 없다 — 그래서 승인만은 인증된 클릭으로 못박은 원 설계가 맞다.)
- **화면 조작 범위: 이동 + 항목 열기 + 필터/검색.** 컨트롤(버튼·탭) 클릭·포커스 조작은 범위 밖.
- **강조는 AI의 판단 영역.** 명시 요청("어디 봐야 해?")에만 반응하는 게 아니라, 채팅이 화면 요소를
  설명하면서 "가리키는 게 도움이 되겠다"고 판단하면 스스로 강조한다.

## 2. 안전 모델 (변하지 않음)

- 지시(directive)는 **백엔드 상태를 바꾸지 않는 순수 UI 동작**이다. 실행도, 승인도, 저장도 아니다.
- 지시가 가리키는 모든 `ref_id`는 **근거가 있어야 한다**: 이 세션에서 도구 결과로 본 것(`seen_*`)이거나
  이 턴의 `screen_state.visible`에 포털이 실제로 그린 것. 모델이 지어낸 id로는 화면을 움직이거나 강조할 수
  없다(카드의 provenance 게이트와 같은 규율, `PresentationRefused`).
- `screen_state`와 첨부는 **데이터**다, 지시가 아니다. 그 안의 텍스트가 "이 job 승인해"라 해도 무시된다
  (기존 instruction-source 규율 그대로).
- 승인 버튼·컨트롤은 지시 대상이 아니다. 강조 대상은 **엔티티**(run / api / job / rule)까지다.

## 3. 전송: 지시 = `ui` 이벤트 + 예약 컴포넌트

새 `EventType`은 만들 수 없다 — `commerce_common`(refs, 읽기 전용, import-only)에 있다. 그래서
지시는 **기존 `ui` 이벤트**에 실어 보내고, 컴포넌트 이름으로 구분한다:

- `component = "screen_navigate"` — 이동/항목 열기/필터 지시
- `component = "screen_highlight"` — 강조 지시

웹은 `change_update`를 가로채는 자리(`useMerchantChat.onEvent`)에서 이 두 컴포넌트를 가로채 **실행**하고,
`GenerativeBlock`은 이 두 컴포넌트에 `null`을 반환해 **카드를 그리지 않는다.** 모르는 컴포넌트는
무시되므로 구버전 클라이언트에 무해하다.

## 4. 도구 2개 — 기존 presentation 파이프라인 재사용

두 도구는 `present_*`와 같은 다섯 자리를 밟는다(이름 상수 → registry 스키마 → payload 모델 → enrichment
→ 웹 소비). 다만 웹 소비가 "카드 렌더"가 아니라 "실행"이라는 점만 다르다.

### 4.1 `navigate_screen`

```
{ view: "home" | "apis" | "runs" | "jobs" | "rules",
  focus?: { kind: "api" | "run" | "job" | "rule", ref_id: string },
  filter?: { status?: <RunsView의 기존 Filter 값>, query?: string } }
```

- `view`는 포털의 `PortalView` 그대로. `focus`는 그 화면에서 열어/스크롤할 항목 하나.
- **`filter` 어휘는 각 뷰가 이미 가진 것만이다** — 새 필터를 발명하지 않는다:
  - `runs` → `status` (RunsView의 기존 `Filter` 값 그대로: `all` / `pass` / `fail` / `error` — `non_pass`는
    RunsView에 없으므로 받지 않는다; 모델이 "실패·에러"를 원하면 `fail`이나 `error` 하나를 고르거나 둘을
    산문으로 안내한다)
  - `apis` → `query` (ApisView의 `SearchField` 검색어)
  - `jobs`, `rules`, `home` → 필터 없음(`focus`만 유효). 뷰에 없는 필터가 오면 무시하고 note로 알린다.
- enrichment: `focus.ref_id`는 provenance 게이트(§2). `kind`와 `view`가 맞아야 한다(`run`은 runs에,
  `api`는 apis에…); 어긋나면 refuse.

### 4.2 `highlight_screen`

```
{ targets: [ { kind: "api" | "run" | "job" | "rule", ref_id: string, note?: string } ]  (1..8),
  headline?: string }
```

- 각 target은 ①②③ 순서 번호를 받는다(배열 순서). 채팅 산문의 ①②③과 매칭되는 게 목적이다.
- enrichment: 모든 `ref_id`가 provenance 게이트를 통과해야 한다. 하나라도 근거 없으면 그 target만 빼고
  note로 알린다(전체 refuse는 아님 — 강조는 부작용이 없으므로).
- 강조할 항목이 **현재 화면에 없으면** 모델은 먼저 `navigate_screen`으로 그 화면에 간다(같은 라운드에
  두 호출). 웹은 이동 후 강조를 적용한다(순서 보장: navigate → highlight).

### 4.3 게이팅과 캐시

- 새 config 플래그 `enable_screen_directives: bool = True`(`stages_*` 속성 미러). 꺼지면
  `absent_tools()`가 두 도구 이름을 뺀다(툴 바이트는 config의 순함수 유지).
- `highlight_screen.targets`의 `maxItems`는 config(`max_highlight_targets` = 8)에서 온다.

## 5. 화면 인지: `screen_state`를 매 턴 함께 보낸다 (3번의 전제)

모델이 화면을 "가리키려면" 화면을 알아야 한다. 지금은 모른다(§1 탐색: 현재 뷰·보이는 항목 정보가 어디에도
없다). 그래서 포털이 **채팅 요청마다** 지금 화면을 실어 보낸다:

```
screen_state: { view, focus?, filter?,
                visible: [ { kind, ref_id, label } ]  (상한 40) }
```

- 웹: 각 뷰가 렌더한 엔티티 목록을 보고하고(apis→api, runs→run, jobs→job, rules→rule; home은 요약 페이지라
  `visible: []`), `AgentApi.chatStream`이 `attached_items`와 나란히 body에 싣는다.
- 호스트: `ChatRequest.screen_state`(pydantic, 검증·상한) → `stream_turn` → `orchestrator.stream_turn`
  → `build_dynamic_context`. **첨부와 같은 자리**(캐시 분기점 뒤의 동적 블록)에 `<screen-state>`
  블록으로 렌더한다 — 매 턴 바뀌는 값이라 정적 프롬프트를 깨지 않는다. 값은 첨부와 같은 fence 새니타이저와
  경계 태그 방어를 지난다.
- 턴 동안 `state.current_screen`에 보관해 enrichment의 provenance 게이트가 `visible`을 참조할 수 있게 한다
  (`last_listed_run_ids`와 같은 성격의 턴-스코프 상태).
- 비용: 항목 40개 상한, 각 항목 kind/ref_id/label만. 동적 블록이라 캐시 히트에 영향 없음.

## 6. 웹 실행

- **`onScreenDirective` 콜백**: `page.tsx`가 `onPortalRefresh`와 동급으로 `useMerchantChat`에 넘긴다.
  `onEvent`에서 `ui` + `screen_navigate` / `screen_highlight`를 가로채 호출한다. 카드는 그리지 않는다.
- **이동/포커스/필터**: `setView(view)` + 새 `screenIntent` 상태 `{focus?, filter?}`를 대상 뷰에 prop으로
  전달. 각 뷰가 그 prop으로 (a) 해당 항목으로 스크롤·열기(`data-ref` 요소를 `scrollIntoView`, 상세가 있는
  뷰는 펼치기), (b) 자기 필터 상태를 갱신(RunsView `setFilter`, ApisView `setQuery`). 이것이 운영자가
  감수한 "뷰별 상태 연동" 비용이다. 적용 후 `screenIntent`는 소비되어 비워진다(다음 렌더에 재적용 안 함).
- **강조**: 엔티티 요소에 `data-ref="<kind>:<ref_id>"`를 부여한다(첨부·딥링크와 같은 `kind:ref_id`
  규약). `HighlightOverlay`(또는 뷰 공통 훅)가 활성 target을 `[data-ref]`로 찾아 **CSS 클래스**를 붙인다
  — 붉은 outline + ①②③ 배지(`::before`). 절대좌표 계산 없이 반응형·스크롤에 안전. 화면에 없는 target은
  조용히 건너뛴다.
- **강조 수명**: 다음 사용자 메시지 전송 시, 또는 `view`가 바뀔 때 **자동 해제**(AI가 자주 강조해도 화면이
  어지럽지 않게). 오버레이에 수동 닫기(×)도 둔다.
- **순서**: 같은 턴에 navigate와 highlight가 오면 navigate를 먼저 적용하고, 뷰가 마운트·데이터를 그린 뒤
  highlight를 적용한다(강조는 `data-ref`가 DOM에 생긴 뒤 매칭되어야 하므로 뷰 렌더 완료 후 재시도 1회).

## 7. 프롬프트와 스킬

- 프롬프트 하드라인 한 줄(`enable_screen_directives` 게이트): "화면에 보이는 항목(`<screen-state>`)을
  설명할 때는 `highlight_screen`으로 그 항목을 가리켜라(①②③을 산문과 맞춰라); 다른 화면에 있으면
  `navigate_screen`으로 먼저 이동하라. 지시는 화면만 바꾸고 아무것도 실행·승인하지 않는다. 승인은 여전히
  사람이 버튼으로 한다."
- 강조 판단 기준(스킬/프롬프트 지침): 답변이 **특정 화면 요소**를 근거로 삼을 때 강조한다; 화면과 무관한
  답(개념 설명, 스펙 문답)에는 강조하지 않는다; 항목 하나를 콕 집는 "api-001 상세 보여줘"는 highlight가
  아니라 `navigate_screen{focus}`다.
- 새 스킬은 만들지 않는다. `failed-triage`(digest 뒤 "화면에서 이 run들 강조") 등 기존 스킬에 한두 줄
  힌트를 더한다.

## 8. 범위 밖 (기록)

- **대리 승인** — 취소(§1). 승인 마크는 어떤 채팅 경로로도 찍히지 않는다.
- 버튼·탭·컨트롤 클릭·포커스 조작(레벨 3).
- Home 타일("불안정 1" 등) 강조 — 타일은 엔티티가 아니다; 후속.
- formats / profiles 항목의 강조·포커스 — Rules 페이지의 `rule`까지만; 후속.
- 좌표 기반 오버레이, 화면 밖(스크롤 밖) 요소의 자동 스크롤-후-강조 이상의 동작.
- 모델 산문에서 ref_id를 정규식으로 뽑아 포털이 알아서 강조하는 휴리스틱 — 기각(취약, AI 판단이 아님,
  필터 불가).

## 9. 테스트/검수 포인트

- 도구: `navigate_screen`이 `ui`/`screen_navigate` 이벤트를 내고 카드 이벤트는 내지 않음; view↔kind
  불일치 refuse; 뷰에 없는 filter는 무시+note; `focus.ref_id` 근거 없으면 refuse(seen_* 도 visible 도 아님).
- `highlight_screen`: 근거 없는 target만 제외+note, 나머지 진행; `maxItems` = config; 순서 번호 보존.
- `screen_state`: `ChatRequest`가 받아 상한(40)·kind enum을 검증; `build_dynamic_context`에
  `<screen-state>`로 렌더되고 첨부와 같은 새니타이즈를 지남; `state.current_screen`에 보관되어 provenance
  게이트가 `visible`을 인정함; 없는 턴엔 블록이 비어 정적 프롬프트 바이트 불변.
- 게이팅: `enable_screen_directives=False`면 두 도구 이름 모두 absent; 프롬프트 하드라인도 사라짐.
- 안전: 어떤 지시도 `approved_*_ids`·백엔드 ledger를 건드리지 않음(테스트로 상태 불변 단언);
  `screen_state.visible`의 label에 "approve job-1" 같은 텍스트가 있어도 아무 승인이 없음.
- 웹: 빌드 clean; `screen_navigate`가 `setView`+`screenIntent`를 호출하고 카드를 안 그림;
  `data-ref` 부여; 강조 클래스가 붙고 다음 전송/뷰 전환에 해제; navigate→highlight 순서.
- 라이브 스모크: "run 화면으로 가" → runs로 이동; "api-004 상세 보여줘" → apis로 이동+api-004 열림;
  "runs에서 실패만" → status 필터; "지금 화면에서 뭐 봐야 해?" → 답변 + ①②③ 붉은 박스.
