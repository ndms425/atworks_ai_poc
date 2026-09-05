"""내장 presentation 툴의 payload 스키마 — 모델이 보낼 수 있는 것. 값을 채우는 조인은 enrichment에."""
from __future__ import annotations

from typing import Literal

from commerce_common.presentation import PresentationPayload
from pydantic import BaseModel, Field

DIGEST_TOOL = "present_run_digest"
GROUPS_TOOL = "present_run_groups"
PREVIEW_TOOL = "present_job_preview"
RULE_PREVIEW_TOOL = "present_rule_preview"
FORMAT_BATCH_TOOL = "present_format_batch"
QUESTION_TOOL = "present_question_form"


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
