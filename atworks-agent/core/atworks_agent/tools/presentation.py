"""내장 presentation 툴의 payload 스키마 — 모델이 보낼 수 있는 것. 값을 채우는 조인은 enrichment에."""
from __future__ import annotations

from typing import Literal

from commerce_common.presentation import PresentationPayload
from pydantic import BaseModel, Field

from ..types import ScreenFilter, ScreenTarget, ScreenTargetKind

DIGEST_TOOL = "present_run_digest"
GROUPS_TOOL = "present_run_groups"
QUERY_TABLE_TOOL = "present_query_table"
PREVIEW_TOOL = "present_job_preview"
RULE_PREVIEW_TOOL = "present_rule_preview"
FORMAT_BATCH_TOOL = "present_format_batch"
PARITY_SUMMARY_TOOL = "present_parity_summary"
PROFILE_PREVIEW_TOOL = "present_profile_preview"
QUESTION_TOOL = "present_question_form"
NAVIGATE_SCREEN_TOOL = "navigate_screen"
HIGHLIGHT_SCREEN_TOOL = "highlight_screen"


class DigestItem(BaseModel):
    kind: Literal["fail", "error", "pending_job", "note"]
    ref_id: str | None = Field(default=None, max_length=64)
    headline: str = Field(max_length=120)
    why_it_matters: str | None = Field(default=None, max_length=160)


class PresentRunDigestPayload(PresentationPayload):
    title: str | None = Field(default=None, max_length=80)
    items: list[DigestItem] = Field(min_length=1, max_length=8)


class PresentRunGroupsPayload(PresentationPayload):
    """모델은 제목과 보여줄 그룹 키만 고른다; 숫자는 aggregate_runs가 세션에 남긴 RunGroup에서 온다."""
    title: str | None = Field(default=None, max_length=80)
    group_keys: list[str] = Field(min_length=1, max_length=50)
    note: str | None = Field(default=None, max_length=200)


class PresentQueryTablePayload(PresentationPayload):
    """모델은 제목(과 선택적 한 줄)만 고른다; 열·행·숫자·창·소스는 전부 마지막 query_runs가 세션에
    남긴 QueryResult에서 온다. 결과가 없으면 enrichment가 거절한다."""
    title: str = Field(max_length=80)
    note: str | None = Field(default=None, max_length=200)


class PresentJobPreviewPayload(PresentationPayload):
    """모델은 job을 고른다; 카드의 모든 값은 스테이징 레코드에서 온다."""
    job_id: str
    headline: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=200)


class PresentRulePreviewPayload(PresentationPayload):
    """모델은 rule을 고른다; 카드의 모든 값은 스테이징 레코드+영향도에서 온다."""
    rule_id: str
    headline: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=200)


class PresentFormatBatchPayload(PresentationPayload):
    """모델은 batch를 고른다; 카드의 모든 값(entries·outcome·counts)은 스테이징 레코드에서 온다."""
    batch_id: str
    headline: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=200)


class PresentParitySummaryPayload(PresentationPayload):
    """모델은 job을 고른다; 클러스터·카운트는 저장된 parity 리포트에서 온다."""
    job_id: str
    title: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=200)


class PresentProfilePreviewPayload(PresentationPayload):
    """모델은 profile을 고른다; 카드의 모든 값은 스테이징 레코드에서 온다."""
    profile_id: str
    headline: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=200)


class NavigateScreenPayload(PresentationPayload):
    """DIRECTIVE: 카드가 아니라 화면을 바꾼다. focus/filter는 enrichment에서 근거·뷰 소유 필터로 검증된다."""
    view: Literal["home", "apis", "runs", "jobs", "rules"]
    focus: ScreenTarget | None = None
    filter: ScreenFilter | None = None


class HighlightTarget(BaseModel):
    kind: ScreenTargetKind
    ref_id: str = Field(max_length=64)
    note: str | None = Field(default=None, max_length=120)


class HighlightScreenPayload(PresentationPayload):
    """DIRECTIVE: 카드가 아니라 화면을 바꾼다. 근거 없는 target은 enrichment에서 조용히 걸러진다.
    max_length=50 is a loose safety ceiling only — the effective cap is the registry's
    ``maxItems: config.max_highlight_targets`` (tools/registry.py), so a config raising that
    above 8 doesn't make every full call fail this model's own validation."""
    targets: list[HighlightTarget] = Field(min_length=1, max_length=50)
    headline: str | None = Field(default=None, max_length=80)
