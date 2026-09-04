"""라우터. demo_common/merchant.py의 build_merchant_router 미러. 승인은 HTTP 라우트만 찍는다.

Route parameters below are annotated with dependencies built at call time (``CurrentSession``,
``Record``), so this module evaluates its annotations eagerly (no ``from __future__ import
annotations``) — FastAPI resolves string annotations against a function's globals, and these
names are local to ``create_app``."""

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from atworks_agent import (
    AttachedItem,
    AtworksSessionContext,
    AtworksSessionState,
    AtworksToolExecutor,
)
from atworks_agent.serialization import api_record, job_record, run_record
from atworks_agent_runtime import AtworksAgent

from .mock_backend import MockAtworks
from .reports import Reports
from .scheduler import Scheduler
from .sessions import SessionRecord, SessionStore, session_dependency
from .streaming import append_user_turn, build_app, stream_turn

PROJECT_ID = "mes-demo"
OPERATOR = "minseong"


def _aware(value: str | None) -> datetime | None:
    """Parse a query-string timestamp to a timezone-aware datetime, or raise a 400 —
    the fixtures' run timestamps carry an offset, and comparing them against a naive
    datetime raises TypeError instead of answering the request."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=f"{value!r} must be ISO 8601, e.g. 2026-09-01T00:00:00+09:00.") from error
    return parsed.astimezone() if parsed.tzinfo is None else parsed


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    attached_items: list[AttachedItem] = Field(default_factory=list, max_length=8)


def create_app(*, agent: AtworksAgent, backend: MockAtworks, scheduler: Scheduler, reports: Reports,
               on_startup: Sequence[Callable[[], Awaitable[None]]] = ()) -> FastAPI:
    app = build_app("atworks-ai host", on_startup=on_startup)
    sessions: SessionStore[AtworksSessionState] = SessionStore(AtworksSessionState)
    CurrentSession = session_dependency(sessions, "/api/atworks/session")
    router = APIRouter(prefix="/api/atworks")
    Record = SessionRecord[AtworksSessionState]

    def context(record: Record) -> AtworksSessionContext:
        return AtworksSessionContext(session_id=record.session_id, project_id=PROJECT_ID, operator=OPERATOR, now=datetime.now().astimezone())

    @router.post("/session")
    async def start_session() -> dict:
        record = sessions.start(PROJECT_ID)
        return {"session_id": record.session_id, "project_id": PROJECT_ID, "operator": OPERATOR}

    @router.post("/chat")
    async def chat(request: ChatRequest, record: CurrentSession) -> StreamingResponse:
        append_user_turn(record, request.message, "Portal events")
        return stream_turn(agent, sessions, record, context(record), env_hint=".env", attached_items=request.attached_items)

    @router.get("/apis")
    async def apis(record: CurrentSession, query: str = "", group: str | None = None) -> dict:
        rows = await backend.search_apis(context(record), query=query, group=group, limit=500)
        return {"apis": [api_record(a) for a in rows]}

    @router.get("/runs")
    async def runs(record: CurrentSession, status: str | None = None, since: str | None = None, limit: int = Query(50, le=500)) -> dict:
        s = context(record)
        since_dt = _aware(since)
        rows = await backend.list_runs(s, since=since_dt, status=status, limit=limit)
        return {"population": await backend.count_runs(s, since_dt, status), "runs": [run_record(r) for r in rows]}

    @router.get("/jobs")
    async def jobs(record: CurrentSession) -> dict:
        del record
        return {"jobs": [job_record(j) for j in (*backend.ledger.pending(), *backend.ledger.applied())]}

    async def job_action(job_id: str, action: str, record: Record) -> dict:
        # 카드의 버튼 클릭 = 호스트 자신의 승인. 마크는 클릭 한 번에 소비되고 남지 않는다.
        # provenance는 모델을 지키는 게이트지 버튼을 지키는 게 아니다: 이 세션이 아직 모르는
        # job이라도, 호스트가 그 job을 실제로 소유(ledger)하고 있으면 클릭 전에 기억시킨다.
        if job_id not in record.state.seen_jobs:
            known = backend.ledger.get(job_id)
            if known is not None:
                record.state.remember_job(known)
        if action == "apply_job":
            record.state.approved_job_ids.add(job_id)
        else:
            record.state.host_action_job_ids.add(job_id)
        executor = AtworksToolExecutor(backend=backend, config=agent.config, skills=agent.skills,
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

    @router.post("/scheduler/tick")
    async def tick(now: str | None = None) -> dict:
        at = _aware(now) or datetime.now().astimezone()
        return {"executed": await scheduler.tick(at)}

    @router.get("/reports/{job_id}", response_class=HTMLResponse)
    async def report(job_id: str, record: CurrentSession) -> str:
        del record
        html = reports.read_html(job_id)
        if html is None:
            raise HTTPException(status_code=404, detail="no report yet")
        return html

    @router.get("/health")
    async def health() -> dict:
        return {"ok": True}

    app.include_router(router)
    return app
