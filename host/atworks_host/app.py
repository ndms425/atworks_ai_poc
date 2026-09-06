"""라우터. demo_common/merchant.py의 build_merchant_router 미러. 승인은 HTTP 라우트만 찍는다.

Route parameters below are annotated with dependencies built at call time (``CurrentSession``,
``Record``), so this module evaluates its annotations eagerly (no ``from __future__ import
annotations``) — FastAPI resolves string annotations against a function's globals, and these
names are local to ``create_app``."""

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from atworks_agent import (
    AttachedItem,
    AtworksSessionContext,
    AtworksSessionState,
    ScreenState,
)
from atworks_agent.aggregation import summarize_insights
from atworks_agent.serialization import (
    api_record,
    format_batch_record,
    format_record,
    job_record,
    profile_record,
    rule_record,
    run_record,
)
from atworks_agent_runtime import AtworksAgent

from .briefing import Briefings
from .insights import InsightPanels
from .mock_backend import MockAtworks
from .reports import Reports
from .scheduler import Scheduler
from .sessions import SessionRecord, SessionStore, session_dependency
from .streaming import append_user_turn, build_app, stream_turn

PROJECT_ID = "mes-demo"
DEFAULT_OPERATOR_ID = "minseong"


def _aware(value: str | None) -> datetime | None:
    """Parse a query-string timestamp to a timezone-aware datetime, or raise a 400 —
    the fixtures' run timestamps carry an offset, and comparing them against a naive
    datetime raises TypeError instead of answering the request."""
    if not value:
        return None
    if " " in value:
        # A literal '+' in a query string decodes to a space (application/x-www-form-
        # urlencoded convention); restore it so an offset like +09:00 round-trips even
        # when the caller did not percent-encode it as %2B.
        value = value.replace(" ", "+")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=f"{value!r} must be ISO 8601, e.g. 2026-09-01T00:00:00+09:00.") from error
    return parsed.astimezone() if parsed.tzinfo is None else parsed


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    attached_items: list[AttachedItem] = Field(default_factory=list, max_length=8)
    screen_state: ScreenState | None = None


class SessionStart(BaseModel):
    operator_id: str | None = None


def create_app(*, agent: AtworksAgent, backend: MockAtworks, scheduler: Scheduler, reports: Reports,
               briefings: Briefings, insights: InsightPanels | None = None,
               on_startup: Sequence[Callable[[], Awaitable[None]]] = ()) -> FastAPI:
    backend.reports = reports  # lets Mock's apply_profile re-diff the target job's stored report
    # A local route below is itself named `insights` (the existing GET /runs/insights) — Python
    # functions have one flat namespace, so that `async def insights(...)` would silently
    # rebind this parameter for the rest of create_app. Alias it immediately so the panel
    # routes always see the InsightPanels instance, never the shadowing route function.
    insight_panels = insights
    app = build_app("atworks-ai host", on_startup=on_startup)
    sessions: SessionStore[AtworksSessionState] = SessionStore(AtworksSessionState)
    CurrentSession = session_dependency(sessions, "/api/atworks/session")
    router = APIRouter(prefix="/api/atworks")
    Record = SessionRecord[AtworksSessionState]

    def context(record: Record) -> AtworksSessionContext:
        profile = backend.operator_profile(record.user_id)
        return AtworksSessionContext(session_id=record.session_id, project_id=PROJECT_ID, operator=record.user_id,
                                     role=profile.role if profile is not None else None, now=datetime.now().astimezone())

    @router.post("/session")
    async def start_session(payload: SessionStart | None = None) -> dict:
        operator_id = (payload.operator_id if payload is not None else None) or DEFAULT_OPERATOR_ID
        profile = backend.operator_profile(operator_id)
        if profile is None:
            raise HTTPException(status_code=400, detail=f"unknown operator: {operator_id!r}")
        record = sessions.start(profile.operator_id)
        return {"session_id": record.session_id, "project_id": PROJECT_ID, "operator": profile.operator_id,
                "operator_name": profile.name, "role": profile.role}

    @router.get("/operators")
    async def operators() -> dict:
        return {"operators": [p.model_dump(mode="json") for p in await backend.list_operators(None)]}

    @router.post("/chat")
    async def chat(request: ChatRequest, record: CurrentSession) -> StreamingResponse:
        append_user_turn(record, request.message, "Portal events")
        return stream_turn(
            agent, sessions, record, context(record), env_hint=".env",
            attached_items=request.attached_items, screen_state=request.screen_state,
        )

    @router.get("/apis")
    async def apis(record: CurrentSession, query: str = "", group: str | None = None) -> dict:
        rows = await backend.search_apis(context(record), query=query, group=group, limit=500)
        return {"apis": [api_record(a) for a in rows]}

    @router.get("/runs/insights")
    async def insights(record: CurrentSession) -> dict:
        s = context(record)
        cfg = agent.config
        since = datetime.now(UTC) - timedelta(days=cfg.max_aggregate_window_days)
        rows = await backend.list_runs(s, since=since, status=None, limit=cfg.max_aggregate_runs)
        apis = {a.api_id: a for a in await backend.search_apis(s, query="", limit=1000)}
        found = summarize_insights(rows, apis, cfg)
        return {"flaky": found.flaky, "regression_suspect": found.regression_suspect, "window_days": cfg.max_aggregate_window_days}

    @router.get("/runs")
    async def runs(record: CurrentSession, status: str | None = None, since: str | None = None, limit: int = Query(50, le=500)) -> dict:
        s = context(record)
        since_dt = _aware(since)
        rows = await backend.list_runs(s, since=since_dt, status=status, limit=limit)
        return {"population": await backend.count_runs(s, since_dt, status), "runs": [run_record(r) for r in rows]}

    @router.get("/jobs")
    async def jobs(record: CurrentSession) -> dict:
        return {"jobs": [job_record(j) for j in await backend.all_jobs(context(record))]}

    # 이 배포는 메모리가 꺼져 있다(enable_memory=False). web-shared의 useAgentTurn이
    # 로드 시 무조건 이 경로를 찾으므로, 빈 상태를 돌려주는 자리표시 라우트를 둔다.
    @router.get("/memory")
    async def memory(record: CurrentSession) -> dict:
        del record
        return {"facts": []}

    @router.delete("/memory")
    async def delete_memory(record: CurrentSession, ref: dict | None = None) -> dict:
        del record, ref
        return {"ok": True}

    async def job_action(job_id: str, action: str, record: Record) -> dict:
        # 카드의 버튼 클릭 = 호스트 자신의 승인. 마크는 클릭 한 번에 소비되고 남지 않는다.
        # provenance는 모델을 지키는 게이트지 버튼을 지키는 게 아니다: 이 세션이 아직 모르는
        # job이라도, 호스트가 그 job을 실제로 소유(ledger)하고 있으면 클릭 전에 기억시킨다.
        if job_id not in record.state.seen_jobs:
            known = await backend.get_job(context(record), job_id)
            if known is not None:
                record.state.remember_job(known)
        if action == "apply_job":
            record.state.approved_job_ids.add(job_id)
        else:
            record.state.host_action_job_ids.add(job_id)
        executor = agent.executor_class(backend=backend, config=agent.config, skills=agent.skills,
                                        session=context(record), state=record.state, memory=agent.memory)
        execution = await executor.execute(action, {"job_id": job_id})
        record.state.approved_job_ids.discard(job_id)
        record.state.host_action_job_ids.discard(job_id)
        if execution.is_error:
            raise HTTPException(status_code=400, detail=execution.result_text)
        if execution.blocked is not None:
            return {"ok": False, "change": None, "reason": execution.result_text}
        record.pending_app_events.append(f"Operator {'approved' if action == 'apply_job' else 'dismissed'} job {job_id} from the card.")
        change = next((e.data.get("change") for e in execution.events if e.type == "change_update"), None)
        return {"ok": True, "change": change}

    # web-shared의 useMerchantChat.actOnChange가 치는 경로 — 이름을 바꾸지 않는다.
    @router.post("/changes/{job_id:path}/apply")
    async def approve(job_id: str, record: CurrentSession) -> dict:
        return await job_action(job_id, "apply_job", record)

    @router.post("/changes/{job_id:path}/discard")
    async def discard(job_id: str, record: CurrentSession) -> dict:
        return await job_action(job_id, "discard_job", record)

    @router.get("/rules")
    async def rules(record: CurrentSession) -> dict:
        return {"rules": [rule_record(r) for r in await backend.list_rules(context(record))]}

    async def rule_action(rule_id: str, action: str, record: Record) -> dict:
        # rule_action은 job_action의 미러: 카드의 버튼 클릭이 호스트 자신의 승인이다. 마크는
        # 클릭 한 번에 소비되고 남지 않는다. provenance는 모델을 지키는 게이트지 버튼을 지키는
        # 게 아니다: 이 세션이 아직 모르는 rule이라도, 호스트가 그 rule을 실제로 소유(ledger)
        # 하고 있으면 클릭 전에 기억시킨다.
        if rule_id not in record.state.seen_rules:
            known = next((r for r in await backend.list_rules(context(record)) if r.rule_id == rule_id), None)
            if known is not None:
                record.state.remember_rule(known)
        if action == "apply_rule":
            record.state.approved_rule_ids.add(rule_id)
        else:
            record.state.host_action_rule_ids.add(rule_id)
        executor = agent.executor_class(backend=backend, config=agent.config, skills=agent.skills,
                                        session=context(record), state=record.state, memory=agent.memory)
        execution = await executor.execute(action, {"rule_id": rule_id})
        record.state.approved_rule_ids.discard(rule_id)
        record.state.host_action_rule_ids.discard(rule_id)
        if execution.is_error:
            raise HTTPException(status_code=400, detail=execution.result_text)
        if execution.blocked is not None:
            return {"ok": False, "change": None, "reason": execution.result_text}
        record.pending_app_events.append(
            f"Operator {'approved' if action == 'apply_rule' else 'dismissed'} rule {rule_id} from the card."
        )
        change = next((e.data.get("change") for e in execution.events if e.type == "change_update"), None)
        return {"ok": True, "change": change}

    @router.post("/rules/{rule_id}/apply")
    async def approve_rule(rule_id: str, record: CurrentSession) -> dict:
        return await rule_action(rule_id, "apply_rule", record)

    @router.post("/rules/{rule_id}/discard")
    async def discard_rule(rule_id: str, record: CurrentSession) -> dict:
        return await rule_action(rule_id, "discard_rule", record)

    @router.get("/profiles")
    async def profiles(record: CurrentSession, job_id: str | None = None) -> dict:
        return {"profiles": [profile_record(p) for p in await backend.list_profiles(context(record), job_id)]}

    async def profile_action(profile_id: str, action: str, record: Record) -> dict:
        # profile_action은 rule_action의 미러: 카드의 버튼 클릭이 호스트 자신의 승인이다. 마크는
        # 클릭 한 번에 소비되고 남지 않는다. provenance는 모델을 지키는 게이트지 버튼을 지키는
        # 게 아니다: 이 세션이 아직 모르는 profile이라도, 호스트가 그 profile을 실제로 소유(ledger)
        # 하고 있으면 클릭 전에 기억시킨다.
        if profile_id not in record.state.seen_profiles:
            known = next((p for p in await backend.list_profiles(context(record)) if p.profile_id == profile_id), None)
            if known is not None:
                record.state.remember_profile(known)
        if action == "apply_profile":
            record.state.approved_profile_ids.add(profile_id)
        else:
            record.state.host_action_profile_ids.add(profile_id)
        executor = agent.executor_class(backend=backend, config=agent.config, skills=agent.skills,
                                        session=context(record), state=record.state, memory=agent.memory)
        execution = await executor.execute(action, {"profile_id": profile_id})
        record.state.approved_profile_ids.discard(profile_id)
        record.state.host_action_profile_ids.discard(profile_id)
        if execution.is_error:
            raise HTTPException(status_code=400, detail=execution.result_text)
        if execution.blocked is not None:
            return {"ok": False, "change": None, "reason": execution.result_text}
        record.pending_app_events.append(
            f"Operator {'approved' if action == 'apply_profile' else 'dismissed'} comparison profile {profile_id} from the card."
        )
        change = next((e.data.get("change") for e in execution.events if e.type == "change_update"), None)
        return {"ok": True, "change": change}

    @router.post("/profiles/{profile_id}/apply")
    async def approve_profile(profile_id: str, record: CurrentSession) -> dict:
        return await profile_action(profile_id, "apply_profile", record)

    @router.post("/profiles/{profile_id}/discard")
    async def discard_profile(profile_id: str, record: CurrentSession) -> dict:
        return await profile_action(profile_id, "discard_profile", record)

    @router.get("/formats")
    async def formats(record: CurrentSession) -> dict:
        return {"formats": [format_record(f) for f in await backend.list_formats(context(record))]}

    @router.get("/format-batches")
    async def format_batches(record: CurrentSession) -> dict:
        return {"format_batches": [format_batch_record(b) for b in await backend.get_pending_format_batches(context(record))]}

    async def format_batch_action(batch_id: str, action: str, record: Record) -> dict:
        # format_batch_action은 rule_action의 미러: 카드의 버튼 클릭이 호스트 자신의 승인이다. 마크는
        # 클릭 한 번에 소비되고 남지 않는다. provenance는 모델을 지키는 게이트지 버튼을 지키는
        # 게 아니다: 이 세션이 아직 모르는 batch라도, 호스트가 그 batch를 실제로 소유(ledger)
        # 하고 있으면 클릭 전에 기억시킨다.
        if batch_id not in record.state.seen_format_batches:
            known = next((b for b in await backend.get_pending_format_batches(context(record)) if b.batch_id == batch_id), None)
            if known is not None:
                record.state.remember_format_batch(known)
        if action == "apply_format_batch":
            record.state.approved_format_batch_ids.add(batch_id)
        else:
            record.state.host_action_format_batch_ids.add(batch_id)
        executor = agent.executor_class(backend=backend, config=agent.config, skills=agent.skills,
                                        session=context(record), state=record.state, memory=agent.memory)
        execution = await executor.execute(action, {"batch_id": batch_id})
        record.state.approved_format_batch_ids.discard(batch_id)
        record.state.host_action_format_batch_ids.discard(batch_id)
        if execution.is_error:
            raise HTTPException(status_code=400, detail=execution.result_text)
        if execution.blocked is not None:
            return {"ok": False, "change": None, "reason": execution.result_text}
        record.pending_app_events.append(
            f"Operator {'approved' if action == 'apply_format_batch' else 'dismissed'} format batch {batch_id} from the card."
        )
        change = next((e.data.get("change") for e in execution.events if e.type == "change_update"), None)
        return {"ok": True, "change": change}

    @router.post("/format-batches/{batch_id}/apply")
    async def approve_format_batch(batch_id: str, record: CurrentSession) -> dict:
        return await format_batch_action(batch_id, "apply_format_batch", record)

    @router.post("/format-batches/{batch_id}/discard")
    async def discard_format_batch(batch_id: str, record: CurrentSession) -> dict:
        return await format_batch_action(batch_id, "discard_format_batch", record)

    @router.post("/scheduler/tick")
    async def tick(now: str | None = None) -> dict:
        at = _aware(now) or datetime.now().astimezone()
        return {"executed": await scheduler.tick(at)}

    @router.get("/reports/{job_id}", response_class=HTMLResponse)
    async def report(job_id: str) -> str:
        try:
            html = reports.read_html(job_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="no report yet") from error
        if html is None:
            raise HTTPException(status_code=404, detail="no report yet")
        return html

    @router.get("/briefings/latest")
    async def briefing_latest(record: CurrentSession) -> dict:
        del record
        data = briefings.latest()
        if data is None:
            raise HTTPException(status_code=404, detail="no briefing yet")
        return data

    @router.get("/briefings/{date}", response_class=HTMLResponse)
    async def briefing_page(date: str) -> str:
        try:
            html = briefings.read_html(date)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="no briefing") from error
        if html is None:
            raise HTTPException(status_code=404, detail="no briefing")
        return html

    @router.get("/home/insights")
    async def home_insights(record: CurrentSession) -> dict:
        if insight_panels is None or not agent.config.enable_insight_panel:
            raise HTTPException(status_code=404, detail="insight panel disabled")
        s = context(record)
        panel = await insight_panels.build(backend, s, s.now or datetime.now().astimezone())
        return panel.model_dump(mode="json")

    @router.post("/home/insights/refresh")
    async def home_insights_refresh(record: CurrentSession) -> dict:
        if insight_panels is None or not agent.config.enable_insight_panel:
            raise HTTPException(status_code=404, detail="insight panel disabled")
        s = context(record)
        panel = await insight_panels.build(backend, s, s.now or datetime.now().astimezone(), refresh=True)
        return panel.model_dump(mode="json")

    @router.get("/health")
    async def health() -> dict:
        return {"ok": True}

    app.include_router(router)
    return app
