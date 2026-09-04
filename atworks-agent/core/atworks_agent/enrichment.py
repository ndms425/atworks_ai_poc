"""presentation payload에 세션 레코드를 조인한다. 모델은 id와 문구만 고르고 값은 여기서 채운다.
- run_digest: run·api·rank 레코드 조인, population(모수) 필수 — "N건 중 먼저 볼 k건"
- job_preview: 스테이징 레코드 그대로 + confidence<0.5 슬롯 목록
- question_form: confidence<0.5 문항에 highlight
merchant_agent/enrichment.py 미러."""
from __future__ import annotations

from typing import Any

from commerce_common.presentation import (
    CHIPS_COMPONENT,
    CHIPS_TOOL,
    EnrichmentContext,
    PresentationComponent,
    PresentationRefused,
    PresentSuggestionsPayload,
)

from .gates import PROVENANCE_GATE
from .question_form import QuestionFormPayload
from .tools.presentation import (
    DIGEST_TOOL,
    PREVIEW_TOOL,
    QUESTION_TOOL,
    PresentJobPreviewPayload,
    PresentRunDigestPayload,
)

LOW_CONFIDENCE = 0.5


def _record(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


async def enrich_run_digest(payload: PresentRunDigestPayload, context: EnrichmentContext) -> dict[str, Any]:
    state = context.state
    if state.last_population is None:
        raise ValueError(
            "The digest needs the population it was drawn from — call list_runs (it records the "
            "count) before presenting a run digest."
        )
    items: list[dict[str, Any]] = []
    dropped: list[str] = []
    scorer: str | None = None
    for item in payload.items:
        entry = item.model_dump(exclude_none=True)
        if item.kind == "note":
            items.append(entry)
            continue
        if item.kind == "pending_job":
            job = state.seen_jobs.get(item.ref_id or "")
            if job is None:
                dropped.append(item.ref_id or "(no id)")
                continue
            entry["job"] = _record(job)
            items.append(entry)
            continue
        run = state.seen_runs.get(item.ref_id or "")
        if run is None:
            dropped.append(item.ref_id or "(no id)")
            continue
        entry["run"] = _record(run)
        api = state.seen_apis.get(run.api_id)
        if api is not None:
            entry["api"] = _record(api)
        rank = state.seen_ranks.get(run.run_id)
        if rank is not None:
            entry["rank"] = _record(rank)
            scorer = scorer or rank.scorer
        items.append(entry)
    if dropped:
        context.notes.append(
            f"Dropped {', '.join(dropped)}: not returned by list_runs/get_run/rank_failed_runs this session."
        )
    if not items:
        raise ValueError("Nothing on the digest could be joined to this session's records; fetch runs first.")
    return {
        "title": payload.title,
        "population": state.last_population,
        "shown": sum(1 for i in items if i["kind"] in ("fail", "error")),
        "scorer": scorer,
        "items": items,
    }


async def enrich_job_preview(payload: PresentJobPreviewPayload, context: EnrichmentContext) -> dict[str, Any]:
    job = context.state.seen_jobs.get(payload.job_id)
    if job is None:
        raise PresentationRefused(
            "That job_id was not staged or listed in this session. Stage the job (or call get_pending_jobs) "
            "first and use its id.",
            gate=PROVENANCE_GATE,
        )
    enriched = payload.model_dump(exclude_none=True)
    enriched["job"] = _record(job)
    enriched["change_id"] = job.job_id   # web-shared useMerchantChat이 이 키로 카드를 찾는다
    enriched["low_confidence"] = sorted(k for k, v in job.confidence.items() if v < LOW_CONFIDENCE)
    enriched["apis"] = [_record(a) for a in (context.state.seen_apis.get(i) for i in job.api_ids) if a is not None]
    return enriched


async def enrich_question_form(payload: QuestionFormPayload, context: EnrichmentContext) -> dict[str, Any]:
    del context
    enriched = payload.model_dump(exclude_none=True)
    for q in enriched["questions"]:
        conf = q.get("confidence")
        q["highlight"] = conf is not None and conf < LOW_CONFIDENCE
    return enriched


PRESENTATION_COMPONENTS: dict[str, PresentationComponent] = {
    spec.name: spec
    for spec in (
        PresentationComponent(name=DIGEST_TOOL, component="run_digest", payload_model=PresentRunDigestPayload, enrich=enrich_run_digest),
        PresentationComponent(name=PREVIEW_TOOL, component="job_preview", payload_model=PresentJobPreviewPayload, enrich=enrich_job_preview),
        PresentationComponent(name=QUESTION_TOOL, component="question_form", payload_model=QuestionFormPayload, enrich=enrich_question_form),
        PresentationComponent(name=CHIPS_TOOL, component=CHIPS_COMPONENT, payload_model=PresentSuggestionsPayload),
    )
}
