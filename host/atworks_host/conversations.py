"""Saved conversations: the session store on SQLite, plus what each turn showed on screen.

One chat session IS one conversation. ``SessionStore`` (``sessions.py``, identical to the reference
host contract modulo line endings) already keeps the two things a turn needs to resume — the state
document (principal, provenance, queued app events) and the model transcript — and says a
deployment "subclasses it and puts the storage methods at the bottom over its own store". This
module is that subclass: the same six methods over three tables, plus the conversation metadata
(title, turn count, timestamps) the list needs and a per-turn DISPLAY record the portal replays.

Why a display record next to the transcript: the transcript is what the MODEL saw (tool_use /
tool_result blocks, notes), while the screen showed text and server-enriched cards. Rebuilding a
card from a ``tool_use`` input would mean re-running enrichment over state that has moved on, so
the host keeps what it actually streamed — prose, the final ``ui`` blocks, the chips — written by
the host from the event stream, never by a client.

Structure follows open-design's ``conversations``/``messages`` tables (Apache-2.0,
``apps/daemon/src/db.ts``); the code is this project's own.

Threading: ``stream_turn`` writes the record back from Starlette's threadpool while every other
read runs on the event loop's thread, so this store owns its OWN connection (never the backend
``Store``'s, which must stay on one thread) and serializes every call behind one lock.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from commerce_common.streaming import AgentEvent

from .retention import TimestampedSessionStore
from .sessions import SessionConflictError, StateT

#: The chips component: consumed into ``suggestions``, never a card.
CHIPS_COMPONENT = "suggestions"
#: Screen directives EXECUTE (switch view, draw boxes) rather than render; replaying one from
#: history would move the operator's screen, so they are never kept.
NOT_DISPLAYED = frozenset({"screen_navigate", "screen_highlight", "plan"})
TITLE_MAX = 40
FAILED_TURN_TEXT = "답변 중 오류가 발생해 이 턴이 끝까지 저장되지 않았습니다."

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    session_id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    title TEXT,
    turn_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    touched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_operator_updated
    ON conversations(operator_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS conversation_messages (
    session_id TEXT NOT NULL REFERENCES conversations(session_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    message_json TEXT NOT NULL,
    PRIMARY KEY (session_id, position)
);

CREATE TABLE IF NOT EXISTS conversation_turns (
    session_id TEXT NOT NULL REFERENCES conversations(session_id) ON DELETE CASCADE,
    turn INTEGER NOT NULL,
    user_text TEXT NOT NULL,
    display_json TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (session_id, turn)
);
"""


def title_from(text: str) -> str:
    """The first user message, whitespace-collapsed and cut at ``TITLE_MAX`` characters."""
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= TITLE_MAX else flat[: TITLE_MAX - 1].rstrip() + "…"


class TurnDisplay:
    """What one turn put on the screen, folded from the same events the portal consumed.

    Mirrors the portal's reducer (``web-shared/turn.ts``) for FINAL output only: prose deltas join
    into text segments (a paragraph break after a tool call), a final ``ui`` block replaces the
    card of the same ``stream_id`` (a retry) or, without one, the last card of its component, the
    chips become ``suggestions``, and an ``error`` event becomes an error segment. Partials,
    progress lines and tool traces are not history."""

    def __init__(self) -> None:
        self.segments: list[dict[str, Any]] = []
        self.suggestions: list[str] = []
        self._after_tool = False
        self._stream_index: dict[str, int] = {}

    def observe(self, event: AgentEvent) -> None:
        data = event.data
        if event.type == "text_delta":
            delta = str(data.get("text", ""))
            if not delta:
                return
            last = self.segments[-1] if self.segments else None
            if last is not None and last["type"] == "text":
                spaced = last["text"].endswith((" ", "\n")) or delta.startswith((" ", "\n"))
                gap = "\n\n" if self._after_tool and last["text"] and not spaced else ""
                last["text"] += gap + delta
            else:
                self.segments.append({"type": "text", "text": delta})
            self._after_tool = False
        elif event.type == "tool_call":
            self._after_tool = True
        elif event.type == "ui":
            component = str(data.get("component", ""))
            payload = data.get("payload") or {}
            if component == CHIPS_COMPONENT:
                self.suggestions = [str(s) for s in (payload.get("suggestions") or [])]
                return
            if not component or component in NOT_DISPLAYED:
                return
            segment = {"type": "ui", "component": component, "payload": payload}
            stream_id = data.get("stream_id")
            index = self._stream_index.get(str(stream_id)) if stream_id else None
            if index is None and not stream_id:
                index = next((i for i in range(len(self.segments) - 1, -1, -1)
                              if self.segments[i].get("component") == component), None)
            if index is None:
                self.segments.append(segment)
                index = len(self.segments) - 1
            else:
                self.segments[index] = segment
            if stream_id:
                self._stream_index[str(stream_id)] = index
        elif event.type == "error":
            self.segments.append({"type": "error", "text": str(data.get("message", ""))})

    def note_failure(self) -> None:
        """The turn raised before it finished; ``stream_turn`` sent the client its own error
        event, which never passes through here, so the record says so itself."""
        if not any(s["type"] == "error" for s in self.segments):
            self.segments.append({"type": "error", "text": FAILED_TURN_TEXT})

    def document(self, attachments: list[dict[str, str]] | None = None) -> dict[str, Any]:
        """이 턴의 표시 기록. `attachments`는 이벤트 스트림이 아니라 **요청**에서 온다 —
        전송 뒤 입력창의 칩은 사라지므로, 다시 연 대화에서 그 턴이 무엇을 달고 갔는지
        확인할 수 있는 곳은 이 기록뿐이다."""
        document: dict[str, Any] = {"segments": self.segments, "suggestions": self.suggestions}
        if attachments:
            document["attachments"] = attachments
        return document


class RecordingAgent:
    """Passes ``stream_turn`` through unchanged while ``display`` observes every event."""

    def __init__(self, agent: Any, display: TurnDisplay) -> None:
        self._agent = agent
        self._display = display

    async def stream_turn(self, *args: Any, **kwargs: Any) -> AsyncIterator[AgentEvent]:
        async for event in self._agent.stream_turn(*args, **kwargs):
            self._display.observe(event)
            yield event


class SqliteSessionStore(TimestampedSessionStore[StateT]):
    """Sessions that survive a restart and can be listed, reopened, renamed and deleted.

    The six storage methods keep the base class's exact contract (the state document under a
    compare-and-set version, the transcript written from ``start``), so ``session_dependency`` and
    ``stream_turn`` work unchanged. ``touch`` persists the last-access stamp, and ``sweep`` only
    ever removes a session nobody has chatted in: every page load starts a session, and those
    empty ones are what the daily idle sweep is for. A conversation with at least one turn is
    the operator's history and is kept until they delete it."""

    def __init__(self, path: str | Path, state_type: type[StateT],
                 clock: Callable[[], datetime] | None = None) -> None:
        super().__init__(state_type, clock=clock)
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def _now(self) -> str:
        return self._clock().isoformat()

    # -- The six storage methods ------------------------------------------------------------

    def read_state(self, session_id: str) -> tuple[int, dict[str, Any]] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT version, state_json FROM conversations WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        self.touch(session_id)
        return row["version"], json.loads(row["state_json"])

    def write_state(self, session_id: str, document: dict[str, Any], version: int) -> None:
        state_json = json.dumps(document, ensure_ascii=False)
        now = self._now()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT version FROM conversations WHERE session_id = ?", (session_id,)
            ).fetchone()
            if (row["version"] if row else 0) != version:
                raise SessionConflictError(session_id)
            if row is None:
                self._conn.execute(
                    "INSERT INTO conversations (session_id, operator_id, version, state_json,"
                    " created_at, updated_at, touched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (session_id, document["user_id"], version + 1, state_json, now, now, now),
                )
            else:
                self._conn.execute(
                    "UPDATE conversations SET version = ?, state_json = ?, touched_at = ?"
                    " WHERE session_id = ?",
                    (version + 1, state_json, now, session_id),
                )

    def read_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT message_json FROM conversation_messages WHERE session_id = ?"
                " ORDER BY position", (session_id,)
            ).fetchall()
        return [json.loads(row["message_json"]) for row in rows]

    def write_messages(self, session_id: str, messages: list[dict[str, Any]], start: int) -> None:
        encoded = [json.dumps(message, ensure_ascii=False) for message in messages]
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM conversation_messages WHERE session_id = ? AND position >= ?",
                (session_id, start),
            )
            self._conn.executemany(
                "INSERT INTO conversation_messages (session_id, position, message_json)"
                " VALUES (?, ?, ?)",
                [(session_id, start + i, text) for i, text in enumerate(encoded)],
            )

    def delete(self, session_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))
        self._touched.pop(session_id, None)

    def session_ids_for_user(self, user_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id FROM conversations WHERE operator_id = ?", (user_id,)
            ).fetchall()
        return [row["session_id"] for row in rows]

    # -- Last access and the idle sweep -------------------------------------------------------

    def touch(self, session_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE conversations SET touched_at = ? WHERE session_id = ?",
                (self._now(), session_id),
            )

    def sweep(self, idle_hours: int, now: datetime) -> int:
        """Delete sessions with NO turns that nobody has touched in ``idle_hours``."""
        cutoff = (now - timedelta(hours=idle_hours)).isoformat()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM conversations WHERE turn_count = 0 AND touched_at < ?", (cutoff,)
            )
        return cursor.rowcount

    # -- Conversation metadata and the display record ---------------------------------------

    def record_turn(self, session_id: str, user_text: str, display: dict[str, Any]) -> int:
        """Append one turn's display record; the first turn also names the conversation.
        Returns the turn number, or 0 when the session no longer exists (deleted mid-turn)."""
        now = self._now()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT turn_count, title FROM conversations WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return 0
            turn = row["turn_count"] + 1
            self._conn.execute(
                "INSERT INTO conversation_turns (session_id, turn, user_text, display_json, at)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, turn, user_text, json.dumps(display, ensure_ascii=False), now),
            )
            self._conn.execute(
                "UPDATE conversations SET turn_count = ?, updated_at = ?, title = ?"
                " WHERE session_id = ?",
                (turn, now, row["title"] or title_from(user_text), session_id),
            )
        return turn

    @staticmethod
    def _meta(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["session_id"], "title": row["title"] or "새 대화",
                "turn_count": row["turn_count"], "created_at": row["created_at"],
                "updated_at": row["updated_at"]}

    def conversation(self, session_id: str, operator_id: str) -> dict[str, Any] | None:
        """One conversation's metadata — only when ``operator_id`` owns it. A foreign id reads
        exactly like a missing one, so the answer never says that someone else's exists."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE session_id = ? AND operator_id = ?",
                (session_id, operator_id),
            ).fetchone()
        return self._meta(row) if row is not None else None

    def list_conversations(self, operator_id: str, query: str = "",
                           limit: int = 50) -> tuple[list[dict[str, Any]], int]:
        """``operator_id``'s conversations that have at least one turn, newest activity first,
        optionally filtered by a title substring; returns ``(page, total)``."""
        where = "operator_id = ? AND turn_count > 0"
        params: list[Any] = [operator_id]
        if query.strip():
            where += " AND title LIKE ? ESCAPE '\\'"
            escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM conversations WHERE {where}", params
            ).fetchone()[0]
            rows = self._conn.execute(
                f"SELECT * FROM conversations WHERE {where}"
                " ORDER BY updated_at DESC, session_id DESC LIMIT ?", [*params, limit]
            ).fetchall()
        return [self._meta(row) for row in rows], total

    def turns(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT turn, user_text, display_json, at FROM conversation_turns"
                " WHERE session_id = ? ORDER BY turn", (session_id,)
            ).fetchall()
        return [{"turn": row["turn"], "user_text": row["user_text"], "at": row["at"],
                 **json.loads(row["display_json"])} for row in rows]

    def rename(self, session_id: str, operator_id: str, title: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ?"
                " WHERE session_id = ? AND operator_id = ?",
                (title, self._now(), session_id, operator_id),
            )
        return cursor.rowcount == 1
