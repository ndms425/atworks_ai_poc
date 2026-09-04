# 모델 교체 지점 (OpenRouter → 폐쇄망 sLLM)

런타임은 `anthropic.AsyncAnthropic` 인터페이스만 쓴다 (`AtworksAgent(client=...)`). 개발은 OpenRouter의
Anthropic 호환 엔드포인트(`ANTHROPIC_BASE_URL=https://openrouter.ai/api`)에 Qwen 슬러그를, 폐쇄망은
vLLM/TGI 앞에 LiteLLM 프록시(`/v1/messages`)를 두고 같은 두 환경변수만 바꾼다. 코드 변경 0.

Anthropic 전용 요청 필드는 보내지 않는다: `send_thinking_fields=False`(config), presentation 컴포넌트에
`enrich_partial`이 없어 `eager_input_streaming` 플래그도 붙지 않는다. `cache_control` 마커는 그대로 보낸다 —
OpenRouter·LiteLLM은 무시하거나 통과시키고, Anthropic 모델로 돌아가면 다시 캐시가 된다.
프록시가 `cache_control`을 거부하면 `commerce_common.prompt_assembly.with_tool_cache_control`·
`build_system_blocks`를 호출하는 자리(orchestrator)에서 마커를 떼는 config 스위치를 하나 추가한다.

TLS: 폐쇄망의 프록시가 제시하는 CA는 OS 인증서 저장소에는 있지만 Python의 certifi 번들에는 없다.
`truststore`가 `anthropic` 클라이언트가 여는 SSL 컨텍스트를 OS 저장소를 신뢰하도록 패치한다
(`host/atworks_host/main.py`의 `build()` 맨 앞, `ATWORKS_TRUST_OS_CA` 환경변수로 켜고 끈다; 기본값 1) —
리포에 cert 파일을 두지 않는다.

확인 순서 (모델을 바꿀 때마다):
1. `pytest` — 결정론 층(게이트·guardrail·스코어러·스케줄러)은 모델과 무관하게 통과해야 한다.
2. `scripts/smoke_chat.py` — 발화 3종에 카드가 나오는가. `[gate]` 줄이 찍히면 모델이 provenance를
   어긴 것이다: 정상(게이트가 잡음). 카드가 안 나오면 툴콜링 품질 문제.
3. `refs/commerce-agents/docs/safety.md` "Still asked of the model" 항목을 sLLM에서 다시 본다.
   깨지는 항목은 코드로 옮긴다 — 이 프로젝트의 grounding 어휘·question_form 강제가 그 예다.

sLLM에서 먼저 깨지는 순서(예상): 자유 서술에 수치 재진술 → present_suggestions 누락 →
stage_job의 confidence/assumptions 누락 → api_ids 환각(게이트가 잡음).
대응: 앞 셋은 프롬프트 반복이 아니라 검증기 추가(누락 시 host reminder 1회, follow-through 패턴 재사용).
