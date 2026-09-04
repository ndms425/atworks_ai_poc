"""AtworksAgentConfig. merchant_agent/config.py 섹션 순서를 따른다: 정체성 → 모델 → 스위치 →
guardrail → 승인 → grounding 어휘. (prompt) 표시 필드는 정적 프롬프트/툴 바이트에 들어간다."""
from __future__ import annotations

from typing import Literal

from commerce_common.config import BaseAgentConfig, ThinkingEffort
from pydantic import Field

JobReviewPolicy = Literal["always", "trusted-source", "auto-apply"]  # open-design automations 계약


class AtworksAgentConfig(BaseAgentConfig):
    brand_name: str = "aTworks"
    assistant_name: str = "the aTworks assistant"
    brand_voice: str = "plain and specific, results first; Korean when the user writes Korean"
    model: str = "qwen/qwen3-235b-a22b-2507"   # OpenRouter 슬러그. tool use 지원 모델이어야 한다 (Task 16)
    thinking_effort: ThinkingEffort | None = None
    # Anthropic 전용 요청 필드(`thinking`, `output_config`)를 보낼지. Qwen/OpenRouter/LiteLLM에서는 False.
    send_thinking_fields: bool = False
    enable_memory: bool = False          # 폐쇄망 SI: 메모리 추출 비활성 (Part A A4)

    # -- 스위치 (prompt) --------------------------------------------------------------
    enable_jobs: bool = True             # stage_job/apply_job/discard_job/get_pending_jobs
    enable_scheduling: bool = True       # JobSchedule 허용 여부

    # -- guardrail (stage·apply 2회 검사) ---------------------------------------------
    max_apis_per_job: int = Field(default=100, ge=1)
    allowed_target_envs: tuple[str, ...] = ("dev", "stg")
    max_target_envs_per_job: int = Field(default=2, ge=1)      # 한 job이 겨냥할 수 있는 계 수
    max_schedules_per_job: int = Field(default=3, ge=1)        # 스케줄 항목 수 (회차 수가 아니다)
    max_test_data_sets: int = Field(default=5, ge=1)           # 테스트 데이터 세트 수
    max_matrix_size: int = Field(default=400, ge=1)            # apis × envs × data 곱의 상한
    max_schedule_count: int = Field(default=14, ge=1)          # 전체 스케줄의 회차 합
    max_concurrency: int = Field(default=4, ge=1, le=32)

    # -- 승인 --------------------------------------------------------------------------
    require_host_approval: bool = True
    approval_surface: str = "the Jobs page approve button"
    stage_shows_preview: bool = True
    job_review_policy: JobReviewPolicy = "always"

    # -- 스코어러 ----------------------------------------------------------------------
    default_scorer: str = "risk_v1"
    max_rank_items: int = Field(default=8, ge=1, le=20)

    # -- grounding 어휘 (한/영). 한글 항목은 substring, 영문은 whole-word로 매칭된다 --------
    runs_grounding_gate: bool = True
    runs_intent_terms: tuple[str, ...] = (
        "실패", "에러", "오류", "risk", "리스크", "최근", "failed", "fail", "error", "recent",
    )
    runs_intent_cues: tuple[str, ...] = (
        "가져", "보여", "알려", "뭐", "어떤", "which", "show", "list", "what", "?",
    )
    queue_grounding_gate: bool = True
    staging_followthrough_gate: bool = True
    job_intent_terms: tuple[str, ...] = (
        "실행", "돌려", "돌리", "수행", "스케줄", "매일", "run", "execute", "schedule", "daily",
    )
    job_intent_cues: tuple[str, ...] = ("해줘", "해 줘", "줘", "please", "now", "every")
    apply_intent_phrases: tuple[str, ...] = ("승인", "적용", "approve", "apply", "go ahead")

    @property
    def stages_jobs(self) -> bool:
        return self.enable_jobs

    def thinking_request_fields(self) -> dict:
        """BaseAgentConfig는 항상 `thinking` 필드를 보낸다. 비-Anthropic 모델은 그 필드를 거부할 수
        있으므로 스위치가 꺼져 있으면 아무것도 보내지 않는다."""
        return super().thinking_request_fields() if self.send_thinking_fields else {}

    def absent_tools(self) -> frozenset[str]:
        names: set[str] = set()
        if not self.enable_jobs:
            names |= {"stage_job", "apply_job", "discard_job", "get_pending_jobs", "present_job_preview"}
        return frozenset(names)
