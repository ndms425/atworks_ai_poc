"""라우터. demo_common/merchant.py의 build_merchant_router 미러. 승인은 HTTP 라우트만 찍는다.

Route parameters below are annotated with dependencies built at call time (``CurrentSession``,
``Record``), so this module evaluates its annotations eagerly (no ``from __future__ import
annotations``) — FastAPI resolves string annotations against a function's globals, and these
names are local to ``create_app``."""

import logging
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from atworks_agent import (
    AskOutcome,
    AttachedItem,
    AtworksSessionContext,
    AtworksSessionState,
    RunsQuery,
    RunStatusFilter,
    SavedQuestion,
    ScreenState,
    VocabularyEntry,
    classify_turn,
    policy_from_config,
)
from atworks_agent.serialization import (
    api_record,
    ask_record,
    audit_record,
    format_batch_record,
    format_record,
    job_record,
    profile_record,
    rule_record,
    run_record,
    saved_question_record,
    vocabulary_record,
)
from atworks_agent.vocabulary import match_terms
from atworks_agent_runtime import AtworksAgent

from .briefing import Briefings
from .evals_writer import default_cases_dir, write_case
from .insights import InsightPanels
from .mock_backend import MockAtworks
from .reports import Reports
from .retention import TimestampedSessionStore
from .scheduler import Scheduler
from .sessions import SessionRecord, SessionStore, session_dependency
from .streaming import append_user_turn, build_app, stream_turn

logger = logging.getLogger(__name__)

PROJECT_ID = "mes-demo"
DEFAULT_OPERATOR_ID = "minseong"

#: The staged/applied/discarded ledgers all share one status vocabulary (JobStatus / RuleStatus /
#: ProfileStatus are the same three members). Declared as a Literal so FastAPI validates it: a
#: `?status=nonsense` used to reach the backend as a plain string, match nothing, and come back as
#: an empty page with `total: 0` -- indistinguishable from "no rules in that state" (M18).
LedgerStatusFilter = Literal["staged", "applied", "discarded"]

#: `GET /vocabulary?status=` -- the same three-member vocabulary the sidecar stores. Declared as a
#: Literal so FastAPI 422s a typo instead of letting it reach the backend, match nothing and come
#: back as an empty page indistinguishable from "no terms in that state" (the M18 lesson).
VocabularyStatusFilter = Literal["pending", "confirmed", "rejected"]

#: `GET /saved-questions?status=` — the sidecar's two states plus an explicit `all`. The default
#: is `active` (Home's card asks for exactly that), and "everything, hidden included" has to be
#: SAID rather than being what an omitted parameter happens to mean.
SavedQuestionStatusFilter = Literal["active", "hidden", "all"]

T = TypeVar("T")

#: What a paged route says when the cursor it was handed cannot be decoded (or no longer points
#: anywhere). It is the CLIENT's input, so it is a 400, not a 500, and the message says what to do.
BAD_CURSOR = "stale or malformed cursor; start from the first page"


@dataclass(frozen=True)
class _Surface:
    """Everything that differs between the four host approval surfaces (job / rule / profile /
    format batch). They were four copies of the same forty lines, identical down to the comments,
    differing only in these names -- and the lines they shared are the approval contract itself
    (mark on, audit pair, try/finally, mark off). `host_action` is now the single copy; this is
    the table it reads."""
    kind: str           # audit `target_kind`
    arg: str            # the executor tool argument this id is passed as
    noun: str           # how the pending_app_events line names it
    seen: str           # AtworksSessionState: the provenance dict
    approved: str       # ...the host-approval mark set
    host_marks: str     # ...the host-action (discard) mark set
    remember: str       # ...the method that records a freshly looked-up object
    lookup: str         # AtworksBackend: the by-id read (never a list page -- see get_rule)


JOB_SURFACE = _Surface("job", "job_id", "job", "seen_jobs", "approved_job_ids",
                       "host_action_job_ids", "remember_job", "get_job")
RULE_SURFACE = _Surface("rule", "rule_id", "rule", "seen_rules", "approved_rule_ids",
                        "host_action_rule_ids", "remember_rule", "get_rule")
PROFILE_SURFACE = _Surface("profile", "profile_id", "comparison profile", "seen_profiles",
                           "approved_profile_ids", "host_action_profile_ids", "remember_profile",
                           "get_profile")
FORMAT_BATCH_SURFACE = _Surface("format_batch", "batch_id", "format batch", "seen_format_batches",
                                "approved_format_batch_ids", "host_action_format_batch_ids",
                                "remember_format_batch", "get_format_batch")


async def _paged(call: Coroutine[Any, Any, T]) -> T:
    """Await a backend read that takes a cursor, turning the one exception a bad cursor produces
    into a 400. `decode_cursor` raises `ValueError` for every malformed shape (bad base64, bad
    JSON, missing keys, bad datetime); before this, that propagated out of the route as a 500 on
    every one of the six paged endpoints (final review I5). Nothing else in these reads raises
    `ValueError`, so the catch stays exactly as narrow as the failure it names."""
    try:
        return await call
    except ValueError as error:
        raise HTTPException(status_code=400, detail=BAD_CURSOR) from error


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


class FeedbackRequest(BaseModel):
    """카드 푸터의 한 표(자가발전 §9). `vote`는 두 값뿐이라 오타는 422이고, `null`은 표를 지운다
    — 잘못 누른 사람이 되돌릴 자리가 있어야 한다."""
    turn_id: str = Field(min_length=1, max_length=64)
    vote: Literal["up", "down"] | None = None


def create_app(*, agent: AtworksAgent, backend: MockAtworks, scheduler: Scheduler, reports: Reports,
               briefings: Briefings, insights: InsightPanels | None = None,
               sessions: SessionStore[AtworksSessionState] | None = None,
               evals_cases_dir: Path | None = None,
               on_startup: Sequence[Callable[[], Awaitable[None]]] = ()) -> FastAPI:
    backend.reports = reports  # lets Mock's apply_profile re-diff the target job's stored report
    if reports.capture_disabled is None:
        # Lets a report tell "본문 캡처 해제" (this API's group opted out) apart from "본문 만료"
        # (the 90-day body window passed) instead of one note covering both.
        reports.capture_disabled = backend.capture_disabled
    # A local route below is itself named `insights` (the existing GET /runs/insights) — Python
    # functions have one flat namespace, so that `async def insights(...)` would silently
    # rebind this parameter for the rest of create_app. Alias it immediately so the panel
    # routes always see the InsightPanels instance, never the shadowing route function.
    insight_panels = insights
    # 👍가 쓰는 회귀 케이스의 자리(spec §9). 기본은 `ATWORKS_EVALS_DIR` 또는 `<repo>/evals/cases`;
    # 테스트는 tmp_path를 넘겨 리포지토리를 건드리지 않는다.
    evals_cases_dir = evals_cases_dir if evals_cases_dir is not None else default_cases_dir()
    app = build_app("atworks-ai host", on_startup=on_startup)
    # TimestampedSessionStore, not the bare SessionStore: `sessions.py` is identical to the
    # reference host contract modulo line endings, so the idle-TTL stamp and sweep (spec §6) live
    # in the subclass.
    # main.py passes the SAME instance it handed the retention job, so the sweep really reaches
    # the sessions this app serves.
    sessions = sessions if sessions is not None else TimestampedSessionStore(AtworksSessionState)
    CurrentSession = session_dependency(sessions, "/api/atworks/session")
    router = APIRouter(prefix="/api/atworks")
    Record = SessionRecord[AtworksSessionState]

    def context(record: Record) -> AtworksSessionContext:
        profile = backend.operator_profile(record.user_id)
        return AtworksSessionContext(session_id=record.session_id, project_id=PROJECT_ID, operator=record.user_id,
                                     role=profile.role if profile is not None else None, now=datetime.now().astimezone())

    async def audit_action(record: Record, action: str, target_kind: str, target_id: str,
                           outcome: str | None = None) -> None:
        """One append-only audit row (spec §2). Every host approval surface writes TWO: the
        bare action name BEFORE the executor call, and `<action>:<ok|blocked|error>` after —
        so a route that dies mid-flight still leaves the attempt on the record, and the pair
        says what actually became of it. The model never reaches this: it is written by the
        route around the approval mark, which only an authenticated host click can set."""
        await backend.append_audit(context(record), f"{action}:{outcome}" if outcome else action,
                                   target_kind, target_id)

    @router.get("/audit")
    async def audit(record: CurrentSession, cursor: str | None = None,
                    limit: int = Query(50, ge=1, le=200)) -> dict:
        """읽기 전용 감사 로그(최신순, keyset 커서). "AI가 무엇을 바꿨나 → 아무것도;
        사람이 이 시각에 승인했다"를 그대로 보여주는 증적."""
        page = await _paged(backend.audit(context(record), cursor=cursor, limit=limit))
        return {"items": [audit_record(e) for e in page.items],
                "next_cursor": page.next_cursor, "total": page.total}

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

    def _require_growth() -> None:
        if not agent.config.enable_growth:
            raise HTTPException(status_code=404, detail="growth view disabled")

    async def record_ask(record: Record, turn_id: str, failure: BaseException | None,
                         message: str, vocabulary_terms: Sequence[str] = ()) -> None:
        """The turn-end hook (self-growth spec §6): ONE ask_log row per chat turn, written from
        the per-turn counters the executor filled, classified by `asklog.classify_turn` — a pure
        function of what the turn actually called. The model never writes this row and never sees
        it; `note_unmet_ask` only leaves a triple in session scratch for the classifier to read.

        A turn that raised still leaves its row (`failure` is logged, not raised): the questions
        that break a turn are exactly the ones the Growth view must show. The row is written even
        when the model answered from prose alone — that is what `partial` means.

        `vocabulary_terms` are the confirmed terms this turn's context actually carried: they go
        ON the row because a 👎 arrives long after the turn ended (spec §9), and by then the
        session's scratch belongs to a later turn."""
        del failure   # already logged by stream_turn; the row itself records what the turn did
        state = record.state
        spec = (state.last_query_result.spec
                if state.last_query_result is not None
                and state.last_query_result.turn_id == turn_id else None)
        entry = classify_turn(
            question=message, tool_names=list(state.turn_tool_names), cards=state.turn_cards,
            unmet=state.turn_unmet, spec=spec, policy=policy_from_config(agent.config),
            session=context(record), turn_id=turn_id, now=datetime.now().astimezone(),
            vocabulary_terms=vocabulary_terms,
        )
        await backend.record_ask(context(record), entry)

    async def matched_vocabulary(record: Record, message: str) -> list[VocabularyEntry]:
        """이번 메시지에 나온 확정 어휘 (자가발전 §7). 실패해도 턴은 그대로 간다 — 어휘는 편의지
        권한이 아니라서, 읽지 못한 턴은 어휘 없이 답할 뿐 실패하지 않는다."""
        if not agent.config.enable_growth:
            return []
        try:
            confirmed = await backend.confirmed_vocabulary(context(record))
        except Exception:
            logger.exception("vocabulary lookup failed; the turn runs without it")
            return []
        return match_terms(message, confirmed, agent.config.vocabulary_max_inject)

    @router.post("/chat")
    async def chat(request: ChatRequest, record: CurrentSession) -> StreamingResponse:
        append_user_turn(record, request.message, "Portal events")
        turn_id = uuid4().hex
        message = request.message
        # 어휘 주입(자가발전 §7 단계 3): 이번 메시지에 실제로 나온 **확정된** 용어만, 최신순으로
        # `vocabulary_max_inject`개. pending은 여기 오지 않는다 -- 제안한 세션의 카드에서만
        # 보인다(spec §2 조항 4). 맞는 게 없으면 컨텍스트 블록에 키 자체가 생기지 않는다(바이트
        # 동일). 확정본은 프로세스 캐시라 대부분의 턴에서 SQL이 한 번도 돌지 않는다: 턴 끝의
        # `note_vocabulary_use`는 `uses + 1`이라 confirmed 집합을 바꾸지 않고 캐시를 버리지
        # 않는다(버리면 어휘가 실린 모든 턴이 캐시 미스가 되어 캐시가 하는 일이 없어진다).
        matched = await matched_vocabulary(record, message)

        async def on_turn_end(rec: Record, tid: str, failure: BaseException | None) -> None:
            if agent.config.enable_growth:
                await record_ask(rec, tid, failure, message,
                                 [entry.term for entry in matched])
                # 집계는 턴이 어떻게 끝났든(성공·예외·중단) 한 번. 어휘가 실제로 실린 턴만 센다.
                if matched:
                    await backend.note_vocabulary_use(
                        context(rec), [entry.term for entry in matched])

        return stream_turn(
            agent, sessions, record, context(record), env_hint=".env",
            attached_items=request.attached_items, screen_state=request.screen_state,
            turn_id=turn_id, vocabulary=matched, on_turn_end=on_turn_end,
        )

    # -- 자가발전: 질문 기록과 성장 요약 (spec §6/§10) ------------------------------------
    # Both are read-only and gated on `enable_growth` — off, they 404 like the insight panel,
    # so a deployment that does not want the Growth view exposes no part of it.
    @router.get("/ask-log")
    async def ask_log(record: CurrentSession, outcome: AskOutcome | None = None,
                      cursor: str | None = None, limit: int = Query(50, ge=1, le=200)) -> dict:
        """질문 기록(최신순, keyset 커서). 저장된 question은 이미 마스킹된 요약이다."""
        _require_growth()
        page = await _paged(
            backend.list_asks(context(record), outcome=outcome, cursor=cursor, limit=limit))
        return {"items": [ask_record(e) for e in page.items], "next_cursor": page.next_cursor,
                "total": page.total}

    @router.get("/growth/summary")
    async def growth_summary(record: CurrentSession, days: int = Query(7, ge=1, le=365)) -> dict:
        """Growth 뷰 '이번 주' 타일 — 전부 COUNT다."""
        _require_growth()
        since = datetime.now(UTC) - timedelta(days=days)
        summary = await backend.growth_summary(context(record), since=since)
        return {**summary.model_dump(mode="json"), "window_days": days}

    # -- 자가발전: 피드백 → eval 케이스 (spec §9) -------------------------------------------
    @router.post("/feedback")
    async def feedback(payload: FeedbackRequest, record: CurrentSession) -> dict:
        """카드 푸터의 👍/👎. 같은 턴을 다시 투표하면 마지막 표만 남고, 없는 턴은 404다.

        **감사 로그에 남기지 않는다.** 감사 2행은 "사람이 공유 상태를 바꿨다"의 증적이고, 표는
        그런 변경이 아니다 -- 승인 마크도, 원장도, 다른 사람에게 보이는 무엇도 움직이지 않는다.
        투표까지 감사에 실으면 그 로그는 "무엇이 승인됐나"를 더 이상 한눈에 보여주지 못한다.

        표가 하는 일은 둘뿐이다. 👎면 이 턴의 컨텍스트에 실렸던 어휘가 ``rejections + 1``을 받고
        (충분히 쌓이면 자동으로 pending으로 강등된다, §7 단계 4), 👍면 -- 그 턴이 실제로 답을
        했고(``answered``) 실행한 스펙이 남아 있을 때만 -- 회귀 eval 케이스 한 장이 된다(§9).
        답하지 못한 턴의 👍는 기록만 남는다: 고정할 행동이 없다."""
        _require_growth()
        entry = await backend.set_feedback(context(record), payload.turn_id, payload.vote)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown turn: {payload.turn_id!r}")
        case_path: str | None = None
        if payload.vote == "down" and entry.vocabulary_terms:
            await backend.note_vocabulary_use(
                context(record), list(entry.vocabulary_terms), rejected=True)
        if payload.vote == "up" and entry.outcome == "answered" and entry.spec is not None:
            try:
                case_path = str(write_case(entry, cases_dir=evals_cases_dir))
            except OSError:
                # 케이스를 못 쓴 것이 투표를 실패로 만들지는 않는다 -- 표는 이미 원장에 있다.
                logger.exception("could not write the eval case for turn %s", entry.turn_id)
        return {"ok": True, "entry": ask_record(entry), "case_path": case_path}

    # -- 자가발전: 조직 공용 어휘 (spec §7/§10) --------------------------------------------
    async def growth_action(record: Record, *, action: str, target_kind: str, target_id: str,
                            fn: Callable[[], Awaitable[T]]) -> T:
        """`host_action`의 가벼운 짝: 감사 2행(시도 + `<action>:<ok|error>`)과 try/except는 같고,
        **승인 마크는 없다**. 어휘와 저장 질문은 job 승인이 아니다 — 실행 계획을 집행하지도,
        원장의 상태를 모델이 쓸 수 있게 열어 주지도 않으므로, 승인 마크라는 일회용 권한을
        여기서 쓰면 그 단어의 뜻이 흐려진다. 그래도 사람이 눌렀다는 증적은 남는다(spec §10).

        `fn`은 백엔드 호출 하나다(실행기를 거치지 않는다): 어휘 라우트가 부르는 것은 모델에게
        열려 있지 않은 백엔드 메서드라, 도구 이름으로 우회할 표면 자체가 없다.

        **없는 대상의 404도 `fn` 안에서 난다**(`host_action`의 `blocked`와 같은 자리): 404를
        헬퍼 바깥에서 던지면 감사 로그에 `:ok`가 먼저 찍히고, 아무 일도 일어나지 않은 클릭이
        성공으로 남는다."""
        await audit_action(record, action, target_kind, target_id)
        try:
            result = await fn()
        except HTTPException:
            await audit_action(record, action, target_kind, target_id, "blocked")
            raise
        except Exception:
            await audit_action(record, action, target_kind, target_id, "error")
            raise
        await audit_action(record, action, target_kind, target_id, "ok")
        return result

    @router.get("/vocabulary")
    async def vocabulary(record: CurrentSession, status: VocabularyStatusFilter | None = None,
                         cursor: str | None = None, limit: int = Query(50, ge=1, le=200)) -> dict:
        """조직 공용 어휘(제안 최신순, keyset 커서). 다른 목록 라우트와 같은 봉투다."""
        _require_growth()
        page = await _paged(
            backend.list_vocabulary(context(record), status=status, cursor=cursor, limit=limit))
        return {"items": [vocabulary_record(e) for e in page.items],
                "next_cursor": page.next_cursor, "total": page.total}

    async def vocabulary_action(term: str, action: str, record: Record) -> dict:
        """[예]/[아니오]/삭제 — 사람의 클릭만 닿는 세 경로. 모델에게는 `propose_alias` 하나뿐이고
        그건 pending밖에 못 만든다: 확정은 오직 여기서 일어난다."""
        _require_growth()
        method = {"confirm": backend.confirm_alias, "reject": backend.reject_alias,
                  "delete": backend.delete_alias}[action]

        async def act() -> VocabularyEntry:
            entry = await method(context(record), term)
            if entry is None:
                # 404는 여기, `growth_action`의 try 안이다 -- 밖에서 던지면 감사 로그가 아무 일도
                # 없었던 클릭을 `:ok`로 기록한다.
                raise HTTPException(status_code=404, detail=f"unknown term: {term!r}")
            return entry

        entry = await growth_action(
            record, action=f"vocabulary_{action}", target_kind="vocabulary", target_id=term, fn=act,
        )
        record.pending_app_events.append(
            f"Operator {action}ed the term {entry.term!r} on the vocabulary list."
        )
        return {"ok": True, "entry": vocabulary_record(entry)}

    @router.post("/vocabulary/{term:path}/confirm")
    async def confirm_vocabulary(term: str, record: CurrentSession) -> dict:
        return await vocabulary_action(term, "confirm", record)

    @router.post("/vocabulary/{term:path}/reject")
    async def reject_vocabulary(term: str, record: CurrentSession) -> dict:
        return await vocabulary_action(term, "reject", record)

    @router.post("/vocabulary/{term:path}/delete")
    async def delete_vocabulary(term: str, record: CurrentSession) -> dict:
        return await vocabulary_action(term, "delete", record)

    # -- 자가발전: 저장 질문 (spec §8/§10) --------------------------------------------------
    # `/changes/` 도 `/rules/` 도 아니다 — 저장 질문은 job 승인이 아니고, 승인 마크와는 아무
    # 관계가 없다(어휘와 같은 자리, 같은 이유). 사람이 눌렀다는 증적만 감사 2행으로 남는다.
    @router.get("/saved-questions")
    async def saved_questions(record: CurrentSession, status: SavedQuestionStatusFilter = "active",
                              cursor: str | None = None,
                              limit: int = Query(50, ge=1, le=200)) -> dict:
        """승격된 저장 질문(많이 쓰는 순, keyset 커서). 기본은 active — Home 카드가 읽는 목록이고,
        숨긴 질문까지 보려면 `?status=all`을 **적어야** 한다."""
        _require_growth()
        page = await _paged(backend.list_saved_questions(
            context(record), status=None if status == "all" else status,
            cursor=cursor, limit=limit))
        return {"items": [saved_question_record(q) for q in page.items],
                "next_cursor": page.next_cursor, "total": page.total}

    @router.get("/saved-questions/{saved_id}/run")
    async def run_saved_question(saved_id: str, record: CurrentSession) -> dict:
        """저장 질문 1건을 지금 실행한다 — 승격 당시의 답이 아니라 지금의 창으로 다시 계산한 결과다.
        모델은 이 경로에 없다: 저장된 QuerySpec을 그대로 실행할 뿐이라 질문이 도중에 바뀌지 않는다.
        없는 id도, 숨긴 질문도 404 — 숨김이 링크 하나로 무력해지면 숨김이 아니다."""
        _require_growth()
        result = await backend.run_saved_question(context(record), saved_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"unknown saved question: {saved_id!r}")
        return result.model_dump(mode="json")

    async def saved_question_action(saved_id: str, action: str, record: Record) -> dict:
        """숨기기/복원 — 사람의 클릭만 닿는다. `growth_action`이라 감사 2행은 남고 승인 마크는
        움직이지 않는다(저장 질문은 실행 계획이 아니다)."""
        _require_growth()
        status = "hidden" if action == "hide" else "active"

        async def act() -> SavedQuestion:
            question = await backend.set_saved_question_status(context(record), saved_id, status)
            if question is None:
                # 404는 여기, `growth_action`의 try 안이다 -- 밖에서 던지면 감사 로그가 아무 일도
                # 없었던 클릭을 `:ok`로 기록한다(어휘 라우트와 같은 자리, 같은 이유).
                raise HTTPException(status_code=404, detail=f"unknown saved question: {saved_id!r}")
            return question

        question = await growth_action(
            record, action=f"saved_question_{action}", target_kind="saved_question",
            target_id=saved_id, fn=act,
        )
        record.pending_app_events.append(
            f"Operator {'hid' if action == 'hide' else 'restored'} the saved question "
            f"{question.title!r} on Home."
        )
        return {"ok": True, "saved_question": saved_question_record(question)}

    @router.post("/saved-questions/{saved_id}/hide")
    async def hide_saved_question(saved_id: str, record: CurrentSession) -> dict:
        return await saved_question_action(saved_id, "hide", record)

    @router.post("/saved-questions/{saved_id}/unhide")
    async def unhide_saved_question(saved_id: str, record: CurrentSession) -> dict:
        return await saved_question_action(saved_id, "unhide", record)

    # Every list route answers in ONE shape: the paged envelope {items, next_cursor, total}
    # (Task 10 — the legacy `apis` / `runs` + `population` keys are gone, and the 500-row default
    # with them). `total` is the count AFTER the filter and independent of `limit`, so the portal's
    # "N개 중 M개" header is honest at any size; `next_cursor` is an opaque server string the web
    # only ever echoes back. Default 50 / max 200 everywhere, matching RunsQuery.limit's own cap.
    @router.get("/apis")
    async def apis(record: CurrentSession, query: str = "", group: str | None = None,
                   cursor: str | None = None, limit: int = Query(50, ge=1, le=200)) -> dict:
        page = await _paged(
            backend.search_apis(context(record), query=query, group=group, cursor=cursor, limit=limit))
        return {"items": [api_record(a) for a in page.items], "next_cursor": page.next_cursor,
                "total": page.total}

    @router.get("/runs/insights")
    async def insights(record: CurrentSession) -> dict:
        s = context(record)
        cfg = agent.config
        since = datetime.now(UTC) - timedelta(days=cfg.max_aggregate_window_days)
        # Two SQL counts over the materialized tables — not a count over a ranked group list.
        # Counting `g.flaky` across `aggregate_runs(limit=500)` saturated: those groups are ranked
        # by failure VOLUME, and a cell that flips with one failure ranks last, so the tile read 0
        # on exactly the projects that needed it. `summarize_insights` has no limit at all.
        insights = await backend.summarize_insights(s, since=since)
        return {"flaky": insights.flaky, "regression_suspect": insights.regression_suspect,
                "window_days": cfg.max_aggregate_window_days}

    @router.get("/runs")
    async def runs(record: CurrentSession, status: RunStatusFilter | None = None,
                   since: str | None = None, cursor: str | None = None,
                   limit: int = Query(50, ge=1, le=200)) -> dict:
        s = context(record)
        since_dt = _aware(since)
        # RunsQuery.limit caps at 200 (the paged read contract, spec 2026-09-06); the route's
        # own bound matches so an over-limit request 422s here instead of a validation error
        # surfacing from inside list_runs.
        page = await _paged(
            backend.list_runs(s, RunsQuery(since=since_dt, status=status, cursor=cursor, limit=limit)))
        # `page.total` IS the old `population` (the count after this filter, limit-independent) —
        # the separate count_runs call that used to fill it was a second scan of the same predicate.
        return {"items": [run_record(r) for r in page.items], "next_cursor": page.next_cursor,
                "total": page.total}

    @router.get("/jobs")
    async def jobs(record: CurrentSession, cursor: str | None = None,
                   limit: int = Query(50, ge=1, le=200)) -> dict:
        page = await _paged(backend.all_jobs(context(record), cursor=cursor, limit=limit))
        return {"items": [job_record(j) for j in page.items], "next_cursor": page.next_cursor,
                "total": page.total}

    # 이 배포의 메모리에는 **개인 사실이 하나도 없다**: 저장되는 건 조직 공용 어휘뿐이고
    # (self-growth §7, `memory_extract_facts=False`) 그건 `/vocabulary`가 자기 봉투로 서빙한다.
    # web-shared의 useAgentTurn이 로드 시 무조건 이 경로를 찾으므로, 빈 상태를 돌려주는
    # 자리표시 라우트를 그대로 둔다 -- 어휘를 여기로 흘리면 "내 기억"이라는 화면에 남의 팀
    # 어휘가 뜬다.
    @router.get("/memory")
    async def memory(record: CurrentSession) -> dict:
        del record
        return {"facts": []}

    @router.delete("/memory")
    async def delete_memory(record: CurrentSession, ref: dict | None = None) -> dict:
        """**의도적으로 아무것도 하지 않는다** — `GET /memory`와 같은 이유의 자리표시다.
        web-shared의 "내 기억" 화면이 이 경로를 찾으므로 라우트는 있어야 하지만, 이 배포에서
        지울 개인 기억이 없다(`memory_extract_facts=False`).

        `memory_facts`에 있는 것은 **팀의 확정 어휘**뿐이고, 그것을 지우는 자리는 Growth 뷰의
        `DELETE`(`POST /vocabulary/{term}/delete`)다: 감사 2행을 남기고, 사이드카 행과 fact를
        함께 지우고, 어느 term이 지워졌는지 target_id에 적는다. 여기서 `memory.store.clear`를
        부르면 한 사람의 "내 기억 지우기" 한 번이 팀 전체의 어휘를 증적 없이 날린다 — 그래서
        이 라우트는 200 OK를 돌려주고 아무 테이블도 건드리지 않는다."""
        del record, ref
        return {"ok": True}

    async def host_action(record: Record, *, action: str, target_id: str, surface: _Surface) -> dict:
        """The ONE host approval surface, in one place (final review I8). The four routes below
        were four 40-line copies that differed only in the four names `_Surface` carries; a rule
        as load-bearing as "the approval mark comes off on every path" must not live in four
        places, because three of them will eventually be almost right.

        카드의 버튼 클릭 = 호스트 자신의 승인. 마크는 클릭 한 번에 소비되고 남지 않는다.
        provenance는 모델을 지키는 게이트지 버튼을 지키는 게 아니다: 이 세션이 아직 모르는
        대상이라도, 호스트가 그것을 실제로 소유(ledger)하고 있으면 클릭 전에 기억시킨다 --
        상태와 무관한 단건 조회로(`get_job`/`get_rule`/`get_profile`/`get_format_batch`), 목록
        페이지를 크게 떠서 그 안에서 찾는 식이 아니라.
        """
        state = record.state
        if target_id not in getattr(state, surface.seen):
            known = await getattr(backend, surface.lookup)(context(record), target_id)
            if known is not None:
                getattr(state, surface.remember)(known)
        approving = action.startswith("apply_")
        getattr(state, surface.approved if approving else surface.host_marks).add(target_id)
        await audit_action(record, action, surface.kind, target_id)
        executor = agent.executor_class(backend=backend, config=agent.config, skills=agent.skills,
                                        session=context(record), state=record.state, memory=agent.memory)
        # try/finally: the approval mark comes off on EVERY path, including an executor that
        # raises (CLAUDE.md: "마크는 클릭 직전에 붙고, 결과와 무관하게 직후에 떨어진다"). A mark left
        # behind would let the NEXT chat turn spend a host approval nobody clicked. The `except`
        # writes the `:error` row before re-raising, so an exploding executor still leaves the
        # audit pair rather than a bare attempt.
        try:
            execution = await executor.execute(action, {surface.arg: target_id})
        except Exception:
            await audit_action(record, action, surface.kind, target_id, "error")
            raise
        finally:
            getattr(state, surface.approved).discard(target_id)
            getattr(state, surface.host_marks).discard(target_id)
        if execution.is_error:
            await audit_action(record, action, surface.kind, target_id, "error")
            raise HTTPException(status_code=400, detail=execution.result_text)
        if execution.blocked is not None:
            await audit_action(record, action, surface.kind, target_id, "blocked")
            return {"ok": False, "change": None, "reason": execution.result_text}
        await audit_action(record, action, surface.kind, target_id, "ok")
        record.pending_app_events.append(
            f"Operator {'approved' if approving else 'dismissed'} {surface.noun} {target_id} from the card."
        )
        change = next((e.data.get("change") for e in execution.events if e.type == "change_update"), None)
        return {"ok": True, "change": change}

    async def job_action(job_id: str, action: str, record: Record) -> dict:
        return await host_action(record, action=action, target_id=job_id, surface=JOB_SURFACE)

    # web-shared의 useMerchantChat.actOnChange가 치는 경로 — 이름을 바꾸지 않는다.
    @router.post("/changes/{job_id:path}/apply")
    async def approve(job_id: str, record: CurrentSession) -> dict:
        return await job_action(job_id, "apply_job", record)

    @router.post("/changes/{job_id:path}/discard")
    async def discard(job_id: str, record: CurrentSession) -> dict:
        return await job_action(job_id, "discard_job", record)

    @router.get("/rules")
    async def rules(record: CurrentSession, status: LedgerStatusFilter | None = None,
                    cursor: str | None = None, limit: int = Query(50, ge=1, le=200)) -> dict:
        # `status` is a SERVER-side filter (the ABC's `list_rules(status=...)`), not a client-side
        # split of one page: the Rules page used to fetch page 1 and bucket it into
        # staged/applied/discarded, so "applied" showed whatever applied rules happened to be in
        # the newest 50 and its count was the page's, not the ledger's.
        page = await _paged(
            backend.list_rules(context(record), status=status, cursor=cursor, limit=limit))
        return {"items": [rule_record(r) for r in page.items], "next_cursor": page.next_cursor,
                "total": page.total}

    async def rule_action(rule_id: str, action: str, record: Record) -> dict:
        return await host_action(record, action=action, target_id=rule_id, surface=RULE_SURFACE)

    @router.post("/rules/{rule_id}/apply")
    async def approve_rule(rule_id: str, record: CurrentSession) -> dict:
        return await rule_action(rule_id, "apply_rule", record)

    @router.post("/rules/{rule_id}/discard")
    async def discard_rule(rule_id: str, record: CurrentSession) -> dict:
        return await rule_action(rule_id, "discard_rule", record)

    @router.get("/profiles")
    async def profiles(record: CurrentSession, job_id: str | None = None, cursor: str | None = None,
                       limit: int = Query(50, ge=1, le=200)) -> dict:
        page = await _paged(
            backend.list_profiles(context(record), job_id, cursor=cursor, limit=limit))
        return {"items": [profile_record(p) for p in page.items], "next_cursor": page.next_cursor,
                "total": page.total}

    async def profile_action(profile_id: str, action: str, record: Record) -> dict:
        return await host_action(record, action=action, target_id=profile_id, surface=PROFILE_SURFACE)

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
        return await host_action(record, action=action, target_id=batch_id, surface=FORMAT_BATCH_SURFACE)

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

    @router.get("/home/summary")
    async def home_summary(record: CurrentSession) -> dict:
        """Home's four tiles + briefing header in ONE call (Task 10). The portal used to fetch
        `/runs?status=fail`, `/runs?status=error`, `/jobs` and `/runs/insights` in parallel and read
        their *lists* for counts — two of them shipped whole run pages just to read `population`,
        and the pending count was `jobs.filter(status == "staged").length` over every job in the
        ledger. Here every number is a count query: `count_runs`×2 over `scope_window_days`
        (the same window `get_context` uses, so the tiles and the model's context block agree),
        `get_pending_jobs` for the queue depth, `summarize_insights` for the flaky tile, and only
        the briefing's HEADER — no run list of any kind crosses this route."""
        s = context(record)
        cfg = agent.config
        now = s.local_now() or datetime.now(UTC)
        counts = {
            "fail": await backend.count_runs(s, since=now - timedelta(days=cfg.scope_window_days), status="fail"),
            "error": await backend.count_runs(s, since=now - timedelta(days=cfg.scope_window_days), status="error"),
            "pending_jobs": len(await backend.get_pending_jobs(s)),
        }
        summary = await backend.summarize_insights(s, since=now - timedelta(days=cfg.max_aggregate_window_days))
        latest = briefings.latest()
        return {
            "counts": counts,
            "insights": {"flaky": summary.flaky, "regression_suspect": summary.regression_suspect,
                         "window_days": cfg.max_aggregate_window_days},
            # Header only: the briefing's own page (`GET /briefings/latest`) still serves the body.
            "briefing_header": None if latest is None else {"date": latest["date"], "generated_at": latest["generated_at"]},
        }

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
