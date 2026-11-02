"""A chat turn that outlives its HTTP connection, so a client can reconnect to it.

Before this, the turn WAS the response: when the browser dropped (a network blip, a reload) Starlette
cancelled the stream's generator and the agent stopped mid-turn. Now ``LiveTurns.start`` lets the turn
run in its own task, every SSE chunk going into a buffer; each connection only *subscribes* to that
buffer. A dropped subscriber costs nothing — the turn finishes, is saved and written back — and
``GET /chat/live`` replays the buffer from the first chunk and then follows it live, so a reconnecting
client rebuilds the whole reply. Follows open-design's reconnect behaviour (Apache-2.0,
``apps/web/src/components/chat/Reconnect.tsx``); the code is this host's own.

One running turn per session (a session's turns are sequential); a finished turn leaves the registry
the moment its stream ends, after which the saved conversation is the source of truth.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from starlette.responses import StreamingResponse

from .streaming import spawn_background

logger = logging.getLogger(__name__)

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def resume_event(user_text: str) -> str:
    """The first frame a reconnect gets: the question the running turn answers."""
    return f"event: turn_resume\ndata: {json.dumps({'user_text': user_text}, ensure_ascii=False)}\n\n"


class LiveTurn:
    def __init__(self, user_text: str) -> None:
        self.user_text = user_text
        self._chunks: list[str] = []
        self._done = False
        self._changed = asyncio.Condition()

    async def push(self, chunk: str) -> None:
        async with self._changed:
            self._chunks.append(chunk)
            self._changed.notify_all()

    async def finish(self) -> None:
        async with self._changed:
            self._done = True
            self._changed.notify_all()

    async def subscribe(self, prefix: str = "") -> AsyncIterator[str]:
        """Every chunk from the first, then live ones until the turn ends."""
        if prefix:
            yield prefix
        sent = 0
        while True:
            async with self._changed:
                while sent >= len(self._chunks) and not self._done:
                    await self._changed.wait()
                batch = self._chunks[sent:]
                sent = len(self._chunks)
                done = self._done
            for chunk in batch:
                yield chunk
            if done and sent >= len(self._chunks):
                return


class LiveTurns:
    def __init__(self) -> None:
        self._running: dict[str, LiveTurn] = {}

    def get(self, session_id: str) -> LiveTurn | None:
        return self._running.get(session_id)

    def start(self, session_id: str, user_text: str, response: StreamingResponse) -> StreamingResponse:
        """Run ``response``'s stream in the background and answer with a subscription to it. The
        original response's background task (the session write-back) runs once the stream ends."""
        turn = LiveTurn(user_text)
        self._running[session_id] = turn

        async def produce() -> None:
            try:
                async for chunk in response.body_iterator:
                    await turn.push(chunk if isinstance(chunk, str) else bytes(chunk).decode("utf-8"))
            except Exception:
                logger.exception("live turn stream failed")
            finally:
                # Save FIRST, then end the subscribers' streams: a client that sends its next
                # message the instant this reply ends must load the session as this turn left it.
                if response.background is not None:
                    try:
                        await response.background()
                    except Exception:
                        logger.exception("live turn write-back failed")
                if self._running.get(session_id) is turn:
                    del self._running[session_id]
                await turn.finish()

        async def first_subscriber() -> AsyncIterator[str]:
            # The turn starts when THIS body starts streaming, not when the route returns: the
            # request's session dependency writes the record back in between, and a turn already
            # running would race that write (two saves of one record -> 409).
            spawn_background(produce())
            async for chunk in turn.subscribe():
                yield chunk

        return StreamingResponse(first_subscriber(), media_type="text/event-stream", headers=SSE_HEADERS)
