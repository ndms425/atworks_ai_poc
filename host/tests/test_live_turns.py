"""A turn outlives its connection: a late subscriber replays every chunk, then follows live."""

import asyncio

from atworks_host.live_turns import LiveTurn, resume_event


async def test_a_late_subscriber_replays_from_the_first_chunk_then_follows_live():
    turn = LiveTurn("실패 원인 묶어줘")
    await turn.push("a")
    await turn.push("b")
    received: list[str] = []

    async def reconnect():
        async for chunk in turn.subscribe(resume_event(turn.user_text)):
            received.append(chunk)

    task = asyncio.create_task(reconnect())
    await asyncio.sleep(0)
    await turn.push("c")
    await turn.finish()
    await asyncio.wait_for(task, 1)
    assert received[0].startswith("event: turn_resume") and "실패 원인 묶어줘" in received[0]
    assert received[1:] == ["a", "b", "c"]


async def test_a_dropped_subscriber_does_not_stop_the_turn():
    turn = LiveTurn("q")
    stream = turn.subscribe()
    await turn.push("x")
    assert await stream.__anext__() == "x"
    await stream.aclose()                       # the browser went away
    await turn.push("y")
    await turn.finish()
    assert [c async for c in turn.subscribe()] == ["x", "y"]
