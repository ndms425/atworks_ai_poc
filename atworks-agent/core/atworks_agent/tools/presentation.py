"""내장 presentation 툴의 payload 스키마 — 모델이 보낼 수 있는 것. 값을 채우는 조인은 enrichment에."""
from __future__ import annotations

from typing import Literal

from commerce_common.presentation import PresentationPayload
from pydantic import BaseModel, Field

DIGEST_TOOL = "present_run_digest"
PREVIEW_TOOL = "present_job_preview"
QUESTION_TOOL = "present_question_form"


class DigestItem(BaseModel):
    kind: Literal["fail", "error", "pending_job", "note"]
    ref_id: str | None = Field(default=None, max_length=64)
    headline: str = Field(max_length=120)
    why_it_matters: str | None = Field(default=None, max_length=160)


class PresentRunDigestPayload(PresentationPayload):
    title: str | None = Field(default=None, max_length=80)
    items: list[DigestItem] = Field(min_length=1, max_length=8)


class PresentJobPreviewPayload(PresentationPayload):
    """모델은 job을 고른다; 카드의 모든 값은 스테이징 레코드에서 온다."""
    job_id: str
    headline: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=200)
