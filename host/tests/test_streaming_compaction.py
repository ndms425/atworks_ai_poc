"""Task 3: streaming.py's ``stream_turn`` must reset ``record.stored_messages`` to 0
whenever a turn's compaction rewrote earlier messages (``results_cleared`` truthy on
``turn_complete``) -- the compacted transcript is shorter/different in place, not merely
longer, so ``SessionStore.save``'s own ``grew`` check (``stored_messages < len(messages)``)
would miss it and the store would keep serving stale (uncleared) tool results. A full
stream through a real ``AtworksAgent`` would need scripting the model client just to reach
this one branch, so the handler is exercised directly against a stub agent instead
(mirrors streaming.py:134)."""
from collections.abc import AsyncIterator

from commerce_common.streaming import AgentEvent

from atworks_agent.types import AtworksSessionContext, AtworksSessionState
from atworks_host.sessions import SessionStore
from atworks_host.streaming import stream_turn


class _StubAgent:
    """A TurnAgent that replays a scripted event list instead of calling a model."""

    def __init__(self, events: list[AgentEvent]) -> None:
        self._events = events

    async def stream_turn(
        self, messages, session, state, *, attached_items=(), screen_state=None
    ) -> AsyncIterator[AgentEvent]:
        del messages, session, state, attached_items, screen_state
        for event in self._events:
            yield event


async def _drain(response) -> None:
    async for _ in response.body_iterator:
        pass


def _record_and_session(store: SessionStore) -> tuple:
    record = store.start("minseong")
    # Simulate a transcript the store already has, the way a real record would after an
    # earlier turn: only a compacted turn should force this back to 0.
    record.stored_messages = 5
    session = AtworksSessionContext(session_id=record.session_id, project_id="mes", operator="minseong")
    return record, session


async def test_stream_turn_resets_stored_messages_when_a_turn_compacted_history():
    store = SessionStore(AtworksSessionState)
    record, session = _record_and_session(store)
    agent = _StubAgent([AgentEvent.turn_complete("end_turn", {}, 10, 2)])

    response = stream_turn(agent, store, record, session, env_hint=".env")
    await _drain(response)

    assert record.stored_messages == 0


async def test_stream_turn_leaves_stored_messages_alone_when_nothing_was_cleared():
    store = SessionStore(AtworksSessionState)
    record, session = _record_and_session(store)
    agent = _StubAgent([AgentEvent.turn_complete("end_turn", {}, 10, 0)])

    response = stream_turn(agent, store, record, session, env_hint=".env")
    await _drain(response)

    assert record.stored_messages == 5
