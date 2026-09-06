"""presentation payload에 세션 레코드를 조인한다. 모델은 id와 문구만 고르고 값은 여기서 채운다.
- run_digest: run·api·rank 레코드 조인, population(모수) 필수 — "N건 중 먼저 볼 k건"
- job_preview: 스테이징 레코드 그대로 + confidence<0.5 슬롯 목록 + 서버가 계산한 matrix(계·데이터·총 실행)
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
from pydantic import BaseModel

from .gates import PROVENANCE_GATE
from .question_form import QuestionFormPayload
from .rules import FORMAT_EXAMPLES, NAMED_FORMATS
from .screen import screen_ref_grounded
from .serialization import format_batch_record, job_record, profile_record, rule_record, run_record
from .tools.presentation import (
    DIGEST_TOOL,
    FORMAT_BATCH_TOOL,
    GROUPS_TOOL,
    HIGHLIGHT_SCREEN_TOOL,
    NAVIGATE_SCREEN_TOOL,
    PARITY_SUMMARY_TOOL,
    PREVIEW_TOOL,
    PROFILE_PREVIEW_TOOL,
    QUESTION_TOOL,
    RULE_PREVIEW_TOOL,
    HighlightScreenPayload,
    NavigateScreenPayload,
    PresentFormatBatchPayload,
    PresentJobPreviewPayload,
    PresentParitySummaryPayload,
    PresentProfilePreviewPayload,
    PresentRulePreviewPayload,
    PresentRunDigestPayload,
    PresentRunGroupsPayload,
)
from .types import Binding, RunStatus

LOW_CONFIDENCE = 0.5

# Task 3/4/5 shared: which screen kind a view focuses, and which ScreenFilter keys a view owns.
VIEW_KIND = {"apis": "api", "runs": "run", "jobs": "job", "rules": "rule"}
VIEW_FILTERS = {"runs": {"status"}, "apis": {"query"}}


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
    dropped_outside_window: list[str] = []
    dropped_pass: list[str] = []
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
        if run.run_id not in state.last_listed_run_ids:
            dropped_outside_window.append(run.run_id)
            continue
        if run.status is RunStatus.PASS:
            dropped_pass.append(run.run_id)
            continue
        # The run record is the deterministic verdict; it overrides whatever kind the
        # model picked so a mislabeled item can never present a pass run as a failure.
        entry["kind"] = run.status.value
        entry["run"] = run_record(run)
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
    if dropped_outside_window:
        context.notes.append(
            f"Dropped {', '.join(dropped_outside_window)}: not in the window list_runs last counted — "
            "list again before presenting them."
        )
    if dropped_pass:
        context.notes.append(
            f"Dropped {', '.join(dropped_pass)}: status is pass; the digest lists non-pass runs only."
        )
    if not items:
        raise ValueError("Nothing on the digest could be joined to this session's records; fetch runs first.")
    return {
        "title": payload.title,
        "population": state.last_population,
        "population_filter": state.last_listed_filter,
        "shown": sum(1 for i in items if i["kind"] in ("fail", "error")),
        "scorer": scorer,
        "items": items,
    }


async def enrich_run_groups(payload: PresentRunGroupsPayload, context: EnrichmentContext) -> dict[str, Any]:
    state = context.state
    if state.last_group_by is None or state.last_population is None:
        raise ValueError("The groups card needs an aggregate_runs call first — it records the groups and their population.")
    items: list[dict[str, Any]] = []
    dropped: list[str] = []
    for key in payload.group_keys[: context.config.max_group_items]:
        group = state.seen_groups.get(f"{state.last_group_by}:{key}")
        if group is None:
            dropped.append(key)
            continue
        # api_sample is the host's internal id sample behind RunGroup.api_count -- the card
        # shows the COUNT, never the raw ids (Task 8).
        items.append(group.model_dump(mode="json", exclude_none=True, exclude={"api_sample"}))
    if not items:
        raise PresentationRefused(
            "None of those group keys came from aggregate_runs this session; use the keys it returned.",
            gate=PROVENANCE_GATE,
        )
    if dropped:
        context.notes.append(f"Dropped {', '.join(dropped)}: not a group aggregate_runs returned this session.")
    return {
        "title": payload.title, "note": payload.note, "group_by": state.last_group_by,
        "population": state.last_population, "population_filter": state.last_listed_filter,
        "since": state.last_aggregate_since.isoformat() if state.last_aggregate_since else None,
        "shown": len(items), "items": items,
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
    # job_record, not a bare model_dump: the card hands this object to web-shared's
    # useChangeActions, which posts to /changes/{change_id}/apply, and it needs the derived
    # matrix figures — the same shape GET /jobs serves.
    enriched["job"] = job_record(job)
    enriched["change_id"] = job.job_id   # web-shared useMerchantChat이 이 키로 카드를 찾는다
    enriched["low_confidence"] = sorted(k for k, v in job.confidence.items() if v < LOW_CONFIDENCE)
    enriched["apis"] = [_record(a) for a in (context.state.seen_apis.get(i) for i in job.api_ids) if a is not None]
    # Server-computed so the one approval click is informed consent over the whole matrix:
    # every env, every data set, and the totals the operator is actually authorizing.
    # LATE re-evaluates select_where at every execution (mock_backend.py), so the staged
    # api_ids count is only a lower bound — the true per-execution ceiling this deployment
    # can ever run is config.max_matrix_size, never the figure resolved at staging (C1).
    if job.binding is Binding.LATE:
        max_runs_per_execution = context.config.max_matrix_size
        max_runs_total = context.config.max_matrix_size * job.total_executions
    else:
        max_runs_per_execution = job.matrix_size
        max_runs_total = job.runs_total
    enriched["matrix"] = {
        "apis": len(job.api_ids),
        "envs": list(job.target_envs),
        "data_sets": [d.label for d in job.test_data],
        "executions": job.total_executions,
        "runs_per_execution": job.matrix_size,
        "runs_total": job.runs_total,
        "max_runs_per_execution": max_runs_per_execution,
        "max_runs_total": max_runs_total,
    }
    return enriched


async def enrich_rule_preview(payload: PresentRulePreviewPayload, context: EnrichmentContext) -> dict[str, Any]:
    rule = context.state.seen_rules.get(payload.rule_id)
    if rule is None:
        raise PresentationRefused(
            "That rule_id was not staged or listed in this session. Stage the rule (or call "
            "get_pending_rules) first and use its id.",
            gate=PROVENANCE_GATE,
        )
    enriched = payload.model_dump(exclude_none=True)
    enriched["rule"] = rule_record(rule)
    enriched["change_id"] = rule.rule_id   # web-shared의 change_update 훅과 호환 (Task 15)
    enriched["review_required"] = rule.review_required
    enriched["low_confidence"] = sorted(k for k, v in rule.confidence.items() if v < LOW_CONFIDENCE)
    if rule.kind == "format":
        hint: dict[str, Any] | None = None
        if rule.format:
            hint = {"label": rule.format, "example": FORMAT_EXAMPLES.get(rule.format), "pattern": NAMED_FORMATS.get(rule.format)}
        elif rule.pattern:
            hint = {"label": "정규식", "example": None, "pattern": rule.pattern}
        if hint is not None:
            if rule.pass_examples:
                hint["pass_examples"] = list(rule.pass_examples)
            if rule.fail_examples:
                hint["fail_examples"] = list(rule.fail_examples)
            enriched["format_hint"] = hint
    api = context.state.seen_apis.get(rule.api_id)
    if api is not None:
        enriched["api"] = _record(api)
    impact = context.state.rule_impacts.get(payload.rule_id)
    if impact is not None:
        enriched["impact"] = impact.model_dump(mode="json") if isinstance(impact, BaseModel) else dict(impact)
    return enriched


async def enrich_format_batch(payload: PresentFormatBatchPayload, context: EnrichmentContext) -> dict[str, Any]:
    batch = context.state.seen_format_batches.get(payload.batch_id)
    if batch is None:
        raise PresentationRefused(
            "That batch_id was not staged or listed in this session. Stage the batch (or call "
            "get_pending_format_batches) first and use its id.",
            gate=PROVENANCE_GATE,
        )
    enriched = payload.model_dump(exclude_none=True)
    enriched["batch"] = format_batch_record(batch)
    enriched["change_id"] = batch.batch_id   # web-shared의 change_update 훅과 호환 (Task 15)
    enriched["entries"] = [e.model_dump(mode="json", exclude_none=True) for e in batch.entries]
    enriched["new_count"] = batch.new_count
    enriched["duplicate_count"] = batch.duplicate_count
    enriched["invalid_count"] = batch.invalid_count
    return enriched


async def enrich_parity_summary(payload: PresentParitySummaryPayload, context: EnrichmentContext) -> dict[str, Any]:
    parity = await context.backend.get_parity_report(context.session, payload.job_id)
    if parity is None:
        raise PresentationRefused(
            "No parity report exists for that job yet — run the parity comparison first.",
            gate=PROVENANCE_GATE,
        )
    enriched = payload.model_dump(exclude_none=True)
    # Deterministic — every field here is what the parity report already computed; the model
    # only picked job_id/title/note.
    enriched["parity"] = parity
    enriched["value_diff_count"] = parity.get("value_diff_count")
    enriched["status_diff_count"] = parity.get("status_diff_count")
    enriched["clusters"] = parity.get("clusters", [])
    return enriched


async def enrich_profile_preview(payload: PresentProfilePreviewPayload, context: EnrichmentContext) -> dict[str, Any]:
    profile = context.state.seen_profiles.get(payload.profile_id)
    if profile is None:
        raise PresentationRefused(
            "That profile_id was not staged or listed in this session. Stage it (or call "
            "get_pending_profiles) first.",
            gate=PROVENANCE_GATE,
        )
    enriched = payload.model_dump(exclude_none=True)
    enriched["profile"] = profile_record(profile)
    enriched["change_id"] = profile.profile_id   # web-shared의 change_update 훅과 호환 (Task 15)
    return enriched


async def enrich_navigate_screen(payload: NavigateScreenPayload, context: EnrichmentContext) -> dict[str, Any]:
    enriched: dict[str, Any] = {"view": payload.view}
    notes: list[str] = []
    if payload.focus is not None:
        want = VIEW_KIND.get(payload.view)
        if want is None or payload.focus.kind != want:
            raise PresentationRefused(
                f"{payload.view} shows {want or 'no'} items; a {payload.focus.kind} cannot be focused there.",
                gate=PROVENANCE_GATE,
            )
        if not screen_ref_grounded(context.state, payload.focus.kind, payload.focus.ref_id):
            raise PresentationRefused(
                "That ref_id was not seen this session and is not on the current screen. Look it up "
                "(get_api/get_run/…) first.",
                gate=PROVENANCE_GATE,
            )
        enriched["focus"] = {"kind": payload.focus.kind, "ref_id": payload.focus.ref_id}
    if payload.filter is not None:
        allowed = VIEW_FILTERS.get(payload.view, set())
        given = payload.filter.model_dump(exclude_none=True)
        kept = {k: v for k, v in given.items() if k in allowed}
        dropped = sorted(set(given) - allowed)
        if kept:
            enriched["filter"] = kept
        if dropped:
            notes.append(f"{payload.view} has no {', '.join(dropped)} filter; ignored.")
    if notes:
        # Also into context.notes: run_presentation builds the tool result text from
        # context.notes, not the payload — without this the model never learns a filter was
        # dropped and can go on to claim it applied.
        msg = " ".join(notes)
        enriched["note"] = msg
        context.notes.append(msg)
    return enriched


async def enrich_highlight_screen(payload: HighlightScreenPayload, context: EnrichmentContext) -> dict[str, Any]:
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    # number = the target's original 1-based position in payload.targets, not a position over the
    # kept list — a dropped target must not shift the badge numbers of the targets after it, or
    # the model's ①②③ prose stops matching what's actually on screen.
    for number, t in enumerate(payload.targets, start=1):
        if screen_ref_grounded(context.state, t.kind, t.ref_id):
            kept.append({"kind": t.kind, "ref_id": t.ref_id, "note": t.note, "number": number})
        else:
            dropped.append(f"{t.kind}:{t.ref_id}")
    if not kept:
        raise PresentationRefused("None of those ids were seen this session or are on the current screen.", gate=PROVENANCE_GATE)
    enriched: dict[str, Any] = {"targets": kept}
    if payload.headline:
        enriched["headline"] = payload.headline
    if dropped:
        # Also into context.notes (see enrich_navigate_screen): otherwise run_presentation's tool
        # result text stays "Shown to the operator." and the model believes every target landed.
        note = "Not highlighted (ungrounded): " + ", ".join(dropped)
        enriched["note"] = note
        context.notes.append(note)
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
        PresentationComponent(name=GROUPS_TOOL, component="run_groups", payload_model=PresentRunGroupsPayload, enrich=enrich_run_groups),
        PresentationComponent(name=PREVIEW_TOOL, component="job_preview", payload_model=PresentJobPreviewPayload, enrich=enrich_job_preview),
        PresentationComponent(name=RULE_PREVIEW_TOOL, component="rule_preview", payload_model=PresentRulePreviewPayload, enrich=enrich_rule_preview),
        PresentationComponent(name=FORMAT_BATCH_TOOL, component="format_batch", payload_model=PresentFormatBatchPayload, enrich=enrich_format_batch),
        PresentationComponent(name=PARITY_SUMMARY_TOOL, component="parity_summary", payload_model=PresentParitySummaryPayload, enrich=enrich_parity_summary),
        PresentationComponent(name=PROFILE_PREVIEW_TOOL, component="profile_preview", payload_model=PresentProfilePreviewPayload, enrich=enrich_profile_preview),
        PresentationComponent(name=QUESTION_TOOL, component="question_form", payload_model=QuestionFormPayload, enrich=enrich_question_form),
        PresentationComponent(name=NAVIGATE_SCREEN_TOOL, component="screen_navigate", payload_model=NavigateScreenPayload, enrich=enrich_navigate_screen),
        PresentationComponent(name=HIGHLIGHT_SCREEN_TOOL, component="screen_highlight", payload_model=HighlightScreenPayload, enrich=enrich_highlight_screen),
        PresentationComponent(name=CHIPS_TOOL, component=CHIPS_COMPONENT, payload_model=PresentSuggestionsPayload),
    )
}
