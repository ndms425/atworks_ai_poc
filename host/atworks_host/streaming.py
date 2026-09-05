# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""Process-level plumbing the host shares: the app with its host and CORS middleware,
background tasks, the loopback guard, and the SSE response one chat turn streams."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol

import anthropic
from commerce_common.streaming import AgentEvent, to_sse
from commerce_common.turn import session_tag
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .sessions import SessionConflictError, SessionRecord, SessionStore

logger = logging.getLogger(__name__)


# The event loop holds only weak references to tasks, so fire-and-forget work would
# otherwise be dropped mid-flight; kept alive here until it completes.
_background_tasks: set[asyncio.Task[Any]] = set()


def spawn_background(coro: Coroutine[Any, Any, object]) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _lifespan(on_startup: Sequence[Callable[[], Awaitable[None]]]):
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            logger.info(
                "No API key in the environment or .env files; the Anthropic SDK falls back "
                "to its own credential chain. If chat returns auth errors, set "
                "ANTHROPIC_API_KEY in a .env file (repo root)."
            )
        for step in on_startup:
            await step()
        yield

    return lifespan


def build_app(title: str, on_startup: Sequence[Callable[[], Awaitable[None]]] = ()) -> FastAPI:
    """A FastAPI app that answers only to loopback host names (plus ``DEMO_ALLOWED_HOSTS``,
    for a deployment that puts its own authentication in front) and to any localhost
    origin. Rejecting other Host headers stops DNS-rebinding, which CORS does not. Logs go
    to stderr at ``DEMO_LOG_LEVEL``: ``INFO`` is a line per model call, ``DEBUG`` adds the bodies."""
    logging.basicConfig(
        level=os.environ.get("DEMO_LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    # The model-call line carries what httpx's line for the same request would.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    extra_hosts = [
        host.strip().rsplit(":", 1)[0] if ":" in host.strip() else host.strip()
        for host in os.environ.get("DEMO_ALLOWED_HOSTS", "").split(",")
    ]
    app = FastAPI(title=title, version="0.1.0", lifespan=_lifespan(on_startup))
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", *(host for host in extra_hosts if host)],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


class TurnAgent(Protocol):
    def stream_turn(
        self,
        messages: list[dict[str, Any]],
        session: Any,
        state: Any,
        *,
        attached_items: Any = (),
        screen_state: Any = None,
    ) -> AsyncIterator[AgentEvent]: ...


def append_user_turn(record: SessionRecord[Any], message: str, events_label: str) -> None:
    """Add the user's message to the transcript, preceded by a note listing what happened
    outside the conversation since the last reply, when anything did."""
    if not record.pending_app_events:
        record.messages.append({"role": "user", "content": message})
        return
    note = f"[{events_label} since your last reply: " + " ".join(record.pending_app_events) + "]"
    record.pending_app_events.clear()
    record.messages.append(
        {
            "role": "user",
            "content": [{"type": "text", "text": note}, {"type": "text", "text": message}],
        }
    )


def stream_turn(
    agent: TurnAgent,
    sessions: SessionStore[Any],
    record: SessionRecord[Any],
    session: Any,
    *,
    env_hint: str,
    attached_items: Any = (),
    screen_state: Any = None,
) -> StreamingResponse:
    """Stream one turn as SSE; the record is written back once the stream has ended (the
    request dependency wrote back before it began). Credential failures become a readable
    error event naming ``env_hint`` (the repo-root ``.env`` path); anything else is logged
    and reported generically."""

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in agent.stream_turn(
                record.messages, session, record.state, attached_items=attached_items, screen_state=screen_state
            ):
                if event.type == "turn_complete" and event.data.get("results_cleared"):
                    record.stored_messages = 0  # earlier messages changed: rewrite the transcript
                yield to_sse(event)
        except anthropic.AuthenticationError:
            logger.exception("chat turn failed: API authentication")
            yield to_sse(
                AgentEvent.error(
                    f"Anthropic API authentication failed (401). Set ANTHROPIC_AUTH_TOKEN (or "
                    f"ANTHROPIC_API_KEY) in {env_hint} and unset any stale key exported by your "
                    "shell, then restart."
                )
            )
        except Exception as error:  # the client gets a safe event, the log gets the rest
            logger.exception("chat turn failed")
            described = str(error).lower()
            if any(word in described for word in ("authentication", "credential", "api_key")):
                yield to_sse(
                    AgentEvent.error(
                        "No Anthropic API credentials are configured, so chat can't run. Set "
                        f"ANTHROPIC_AUTH_TOKEN (or ANTHROPIC_API_KEY) in {env_hint} and restart; "
                        "everything except chat works without one."
                    )
                )
            else:
                yield to_sse(
                    AgentEvent.error("Something went wrong on our side. Please try again.")
                )

    def write_back() -> None:
        try:
            sessions.save(record)
        except SessionConflictError:
            # A button's request wrote the session while the turn streamed. The turn is the
            # larger write, so it goes in over that version; the note the button queued is lost.
            record.version = (sessions.read_state(record.session_id) or (0, {}))[0]
            logger.warning(
                "session %s: a write raced the turn; the turn wins", session_tag(record.session_id)
            )
            sessions.save(record)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(write_back),
    )
