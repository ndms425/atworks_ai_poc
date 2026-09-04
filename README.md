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
python scripts/smoke_chat.py           # host가 떠 있는 상태에서 — 발화 3개의 카드·게이트를 확인
```

수동 시나리오 3개 (host + web을 띄운 채, `docs/sllm-seam.md`·`docs/safety.md` 참고):

1. "최근 실패한 api 중 risk 있는 것 가져와" → `run_digest` 카드에 "N건 중 먼저 볼 k건" 헤더가 뜬다.
2. Runs 뷰에서 실패 행을 "채팅에 첨부" → "이거 왜 실패했어" → 답변이 첨부한 그 run만 다룬다.
3. "지난 1주일 업데이트된 api 오늘부터 3일간 매일 9시에 실행해줘" → `question_form` 또는
   `job_preview`(대상 건에 ● 표시) → Jobs 뷰에서 승인 →
   `POST /api/atworks/scheduler/tick?now=<from_date>T09:00:00%2B09:00`
   (`+` must be URL-encoded as `%2B`) → 리포트 링크가 열린다.
