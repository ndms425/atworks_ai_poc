# 채팅 기능 개선 분석 — 새 대화 · 대화 저장 · 긴 대화 축약

작성일: 2026-11-02 · 대상: `feature/self-growth` 브랜치 · 참조: `refs/commerce-agents` (커밋 `fd4d592`), `refs/open-design` (커밋 `5a0c477`, Apache 2.0)

## 결론

두 참조를 합치면 세 기능의 설계 근거가 모두 있다. commerce-agents는 **모델 호출 루프와 축약**을, open-design은 **대화 저장과 새 대화·목록 UI**를 갖고 있다.

| 기능 | commerce-agents | open-design | 이 프로젝트 현재 |
|---|---|---|---|
| 새 대화 | 서버 `/reset`만, 버튼 없음 | **있음**. 새 대화 버튼 + 대화 생성 API | 없음 |
| 대화 저장·다시 열기 | 없음 (메모리 뼈대만) | **있음**. SQLite `conversations`/`messages` 테이블, 목록·조회·이름변경·삭제 API, 대화 목록 UI | 없음 |
| 긴 대화 축약 | 오래된 도구 결과 삭제 (10만 토큰 기준) | 자체 축약 없음. 외부 에이전트에 위임 + 매 턴 대화 재구성 시 메시지별 길이 제한 | commerce-agents 방식 그대로 |

**어느 참조에도 없는 것:** 이전 대화를 모델로 요약해 짧게 만드는 기능. 새로 구현해야 한다.

## 공통 사실

- 이 프로젝트는 참조 라이브러리를 그대로 설치해 쓴다. `requirements.txt`가 `-e refs/commerce-agents/commerce-common[examples]`를 설치한다.
- `host/atworks_host/sessions.py`는 참조의 `examples/demo_common/sessions.py`와 내용이 같다(줄바꿈만 다름).
- 모델은 Claude가 아니다. OpenRouter나 LiteLLM을 거쳐 Qwen을 쓴다(`ANTHROPIC_BASE_URL`).
  - 그래서 Anthropic 서버의 컨텍스트 편집·자동 축약 기능은 쓸 수 없다.
  - 캐시와 토큰 사용량 집계가 동작하는지는 프록시에 달려 있다.

## 1. 새 대화

**참조 소스**
- `SessionStore.reset(record)` (`examples/demo_common/sessions.py:119-121`): 세션을 끝내고 지운다.
- `POST /reset` (`examples/demo_common/merchant.py:289-295`): 세션을 지우고 새로 시작해 새 `session_id`를 돌려준다. 옵션으로 메모리도 지운다.
- 화면에서 이 API를 부르는 곳은 **없다**. `web-shared/api.ts`에 reset 메서드가 없고, 패널 헤더 버튼은 Activity, 전체화면, 닫기뿐이다.

**이 프로젝트**
- `host/atworks_host/app.py`에 `/reset` 라우트가 없다. `SessionStore.reset()` 메서드는 있지만 아무도 부르지 않는다.
- 새 세션은 운영자를 바꿀 때만 생긴다(`page.tsx`의 `useSession`). 그때도 화면의 이전 대화 목록은 지워지지 않는다.
- 채팅 항목은 React 상태(`web-shared/turn.ts`의 `useAgentTurn`)에만 있고 초기화 코드가 없다.

**해야 할 일**
1. 참조의 `/reset` 라우트를 `app.py`로 이식한다.
2. `api.resetSession()`을 추가하고 `useSession`이 새 ID를 받게 한다.
3. `useAgentTurn`의 대화 항목과 trace를 비운다.
4. 어시스턴트 패널 헤더에 "새 대화" 버튼을 단다.

## 2. 대화 저장·다시 열기

**참조 소스**
- `SessionStore`는 프로세스 메모리의 dict 두 개다.
  - 상태 문서: 버전 번호로 동시 수정 충돌을 막는다(충돌 시 HTTP 409).
  - 대화 기록: 턴마다 이어 붙인다.
- 주석에 "배포할 때 저장 메서드 6개를 각자 저장소로 바꿔 구현하라"고 되어 있다. **영구 저장 구현은 없다.**
- 세션 목록 API, 대화 조회 API, 제목, 시각, 만료 정책 모두 없다.
- 화면은 열 때마다 새 세션을 만들어서, 새로고침하면 대화가 사라진다.

**이 프로젝트**
- `TimestampedSessionStore` (`host/atworks_host/retention.py`)가 마지막 사용 시각을 기록한다. 하루 한 번 24시간 쓰지 않은 세션을 지운다.
- 결과적으로 다음과 같다.
  - 서버를 재시작하면 모든 대화가 사라진다.
  - 24시간 쓰지 않은 세션은 다음 정리 때 지워진다.
  - 새로고침하거나 운영자를 바꾸면 새 세션이 생겨, 이전 대화는 메모리에 남아도 다시 열 수 없다.
- 화면은 세션 ID를 메모리에만 둔다. localStorage에는 운영자 ID와 패널 폭만 있다.
- `host/atworks_host/store.py`의 SQLite에 대화 테이블이 없다.

**해야 할 일 (참조에 없음, 전부 신규)**
1. 저장 메서드 6개(`read_state`, `write_state`, `read_messages`, `write_messages`, `delete`, `session_ids_for_user`)를 기존 SQLite `Store` 위에 구현하는 영구 세션 저장소.
2. 대화 메타데이터: 제목, 생성·수정 시각.
3. 운영자 범위로 제한된 `GET /sessions`, `GET /sessions/{id}/messages`, 세션 이어가기.
4. 화면: 세션 ID 보관·선택, 저장된 Anthropic 형식 메시지를 화면 대화 항목으로 복원.
5. 저장한 대화에 24시간 유휴 삭제를 적용할지 정책 결정.

## 3. 긴 대화 축약

**참조 소스: 도구 결과 삭제 방식**
- `compact_history` (`commerce-common/commerce_common/turn.py:124-157`)
  - 직전 모델 호출의 입력 토큰이 `compact_history_above_tokens`(기본 100,000, 0이면 끔) 이상일 때만 동작한다.
  - 가장 오래된 도구 결과부터 "이전 턴의 결과 삭제됨, 필요하면 도구를 다시 호출" 문구로 바꾼다.
  - 대화 크기가 절반이 되면 멈춘다.
  - 사용자와 AI가 주고받은 말은 건드리지 않고, **요약은 만들지 않는다.**
- 매 턴 끝에 실행되고, 삭제가 일어나면 대화 기록 전체를 다시 저장한다.
- 프롬프트 캐시 지점: 시스템 프롬프트, 마지막 도구 정의, 가장 최근 저장 메시지(롤링).
- 참조 어디에도 없는 것: Anthropic API의 컨텍스트 편집, 서버 측 자동 축약, 이전 턴 요약, 턴 수 기준 자르기.

**이 프로젝트**
- 오케스트레이터(`atworks-agent/runtime/atworks_agent_runtime/orchestrator.py`)가 참조와 같은 루프를 쓴다.
- 매 호출마다 **저장된 대화 전체**를 보낸다.
- 턴 끝에 같은 `compact_history`를 부른다. 설정은 참조 기본값을 그대로 쓴다: `max_tokens=2048`, 도구 반복 8회, 롤링 캐시 켬, 축약 기준 100,000 토큰.

**한계와 해야 할 일**
- 지금 긴 대화를 막는 장치는 10만 토큰에서 도구 결과를 지우는 것 하나다. 그마저 프록시가 토큰 사용량을 돌려줘야 동작한다.
- 이전 대화 요약은 새로 만들어야 한다. 예: 별도 모델 호출로 오래된 N개 메시지를 요약 메시지 하나로 바꾼다.
- 구현할 때 지켜야 할 조건:
  - `tool_use`와 `tool_result` 쌍을 깨지 않는다.
  - 대화 기록 전체를 다시 저장한다(`stored_messages = 0`).
  - 롤링 캐시 지점이 계속 동작하게 한다.
- Qwen은 컨텍스트 창 크기가 Claude와 다르므로 기준 토큰 수를 모델에 맞게 다시 정해야 한다.

## 참고: 메모리는 대화 기록이 아니다

- 참조의 `commerce_common/memory.py`는 사용자에 대한 짧은 사실(선호, 제약, 맥락)을 저장한다. 키 64자, 값 200자 제한이다.
- 이 프로젝트의 `SqliteMemoryStore`는 같은 인터페이스로 팀이 확정한 용어(어휘)를 저장한다. 모델이 직접 쓰는 `save_memory`/`recall_memories` 도구는 막혀 있다.
- 둘 다 대화 저장 용도로는 쓸 수 없다.

## open-design 분석 (2026-11-02 추가)

open-design은 디자인 작업 공간이고, 채팅 뒤에서 Claude Code, Codex, OpenCode 같은 **외부 코딩 에이전트**를 돌린다. 그래서 대화 저장은 직접 하지만, 모델 맥락 관리는 대부분 그 에이전트에 맡긴다. 경로는 `refs/open-design/apps/` 기준이다.

### 대화 저장

- **테이블** (`daemon/src/db.ts:203-277`)
  - `conversations`: `id`, `project_id`, `title`, `session_mode`, `created_at`, `updated_at`. 프로젝트 삭제 시 함께 삭제된다.
  - `messages`: `conversation_id`, `role`, `content`, `position`(순서), `events_json`(도구 호출 등 이벤트), `attachments_json`, `started_at`/`ended_at` 등.
  - `agent_sessions`: 대화마다 외부 에이전트의 세션 ID, 모델, 마지막 메시지 ID, 마지막 입력 토큰 수를 저장한다.
- **API** (`daemon/src/routes/project/conversations.ts`)

| 메서드 | 경로 | 용도 |
|---|---|---|
| GET | `/api/projects/:id/conversations` | 프로젝트의 대화 목록 |
| POST | `/api/projects/:id/conversations` | 새 대화. 기존 대화를 특정 메시지 시점에서 복제(fork)하는 옵션 포함 |
| PATCH | `/api/projects/:id/conversations/:cid` | 이름 변경 등 |
| DELETE | `/api/projects/:id/conversations/:cid` | 삭제 |
| GET | `/api/projects/:id/conversations/:cid/messages` | 대화 내용 불러오기 |
| PUT | `/api/projects/:id/conversations/:cid/messages/:mid` | 메시지 저장·갱신 |

- 모든 라우트가 `authorizeProjectRequest`로 프로젝트 읽기·쓰기 권한을 확인한다. aTworks의 프로젝트 권한 판정과 같은 위치에 대응시킬 수 있다.
- 복제한 대화의 제목은 원래 제목에 번호를 붙여 자동으로 만든다(`conversation-fork-title.ts`).

### 새 대화와 대화 목록 UI

- `web/src/components/ChatPane.tsx:4302-4360`: 채팅 패널 안에 대화 목록 드롭다운이 있다.
  - 검색창, "새 세션" 버튼, 대화 행 목록(현재 대화 표시, 선택, 삭제)으로 구성된다.
- 클라이언트 API: `web/src/state/projects.ts`의 `listConversations`, `createConversation`, `patchConversation`, `deleteConversation`.

### 긴 대화 처리

- **자체 요약이나 축약은 없다.** `agent_sessions.last_input_tokens` 주석에 "관찰용일 뿐, 축약이나 세션 교체에 쓰지 않는다"고 명시돼 있다.
- **외부 에이전트 세션 이어가기** (`daemon/src/agent-session-resume.ts`)
  - 다음 턴에는 저장해 둔 에이전트 세션 ID로 이어서 실행한다. 그 에이전트가 자기 맥락을 스스로 관리하고 필요하면 스스로 축약한다. 화면은 그 축약 이벤트(`opencode_compaction`, `context_compaction`)를 "맥락 정리 중"으로만 표시한다.
  - 이어가기가 안전하지 않으면 새 세션을 열고 DB의 대화 전체로 맥락을 다시 채운다(reseed). 모델·작업 폴더가 바뀌었거나, 사이에 다른 에이전트가 답했거나, 메시지가 수정된 경우다.
- **대화 재구성 규칙** (`web/src/providers/daemon.ts:126-260`, `buildDaemonTranscript`)
  - 이전 메시지 하나는 12,000자까지만 보낸다. 넘으면 자르고 "전체 내용은 저장된 기록에 남아 있다"는 표시를 붙인다.
  - 이전 AI 답변의 결과물과 질문 폼은 요약 문구로 바꿔 넣는다.
  - 도구 결과는 다시 보내지 않는다. 메시지 본문만 보낸다.
  - 이전 턴 입력이 200,000 토큰을 넘었거나 8,000자 넘는 도구 결과가 있었으면, "이번 턴은 짧게, 이전 도구 출력은 요약하라"는 경고문을 맥락에 붙인다.
  - 다른 에이전트로 바꾼 경우, 마지막으로 다른 에이전트가 답한 이후의 기록만 보낸다.

## 권장 설계 (두 참조 결합)

| 기능 | 가져올 곳 | 이 프로젝트에 맞춘 방법 |
|---|---|---|
| 대화 저장 | open-design의 테이블 구조 | 기존 SQLite `Store`에 `conversations`, `messages` 테이블 추가. `project_id` 대신 `operator_id`(aTworks 이식 후에는 aTworks 사용자·프로젝트 ID)로 범위를 건다. 세션 저장 메서드 6개를 이 테이블 위에 구현해 `SessionStore`를 교체한다 |
| 목록·불러오기 API | open-design의 라우트 구성 | `GET/POST /conversations`, `PATCH/DELETE /conversations/{id}`, `GET /conversations/{id}/messages`. 운영자 본인 것만 보이게 한다 |
| 새 대화·목록 UI | open-design `ChatPane`의 드롭다운 | 어시스턴트 패널 헤더에 "새 대화" 버튼과 대화 목록 드롭다운 추가. 선택하면 저장된 메시지로 화면 대화를 복원 |
| 제목 | open-design은 명시값 또는 복제 번호 | 첫 질문 앞부분을 제목으로 쓰고 이름 변경 허용 |
| 긴 대화 1단계 | commerce-agents `compact_history` 유지 | 오래된 도구 결과 삭제. 기준 토큰 수를 Qwen 컨텍스트 창에 맞게 조정 |
| 긴 대화 2단계 | open-design 재구성 규칙 | 메시지별 길이 제한, 큰 도구 결과 재전송 금지, 토큰 경고문 |
| 긴 대화 3단계 | 신규 | 기준을 넘으면 오래된 턴을 별도 모델 호출로 요약해 요약 메시지 하나로 교체. 원문은 DB에 그대로 둔다 |

**주의:** open-design의 "외부 에이전트에 맥락 위임" 방식은 이 프로젝트에 맞지 않는다. 이 프로젝트는 자체 루프로 Messages API를 직접 호출하므로, 맥락은 직접 관리해야 한다.
