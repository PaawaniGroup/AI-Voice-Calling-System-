"""The media watchdog (`AiService._media_deadline`) runs `_finish` INSIDE its own task. `_finish` used to cancel `rt.watchdog` (itself),
so the next real await raised CancelledError, which `_media_deadline` swallowed: the AI session record was marked failed but the rest
of `_finish` never ran (the call's session stayed `active` after a media reconnect timeout and the `ai.session.failed` event/webhook
was never emitted). Provider and models are MOCKS; the media client is SYNTHETIC."""
import asyncio
import time

from tests.conftest import auth
from tests.integration.test_ai import connect, hello, media  # noqa: F401  (`media` is a fixture)

WATCHDOG = "AiService._media_deadline"


def _chain(coro) -> list[str]:
    out = []
    while coro is not None:
        out.append(getattr(coro, "__qualname__", ""))
        coro = getattr(coro, "cr_await", None) or getattr(coro, "ag_await", None)
    return out


def _watchdogs() -> list[str]:
    return [q for t in asyncio.all_tasks() if not t.done() for q in _chain(t.get_coro()) if q == WATCHDOG]


async def _wait(predicate, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = await predicate()
        if v:
            return v
        await asyncio.sleep(0.05)
    return await predicate()


def _count_emits(e):
    """Count of `ai.session.failed` on the activity feed and through the webhook emitter (if one is configured)."""
    emitted = []
    if e.ai.emitter is not None:
        orig = e.ai.emitter.emit_generic

        async def counting(tenant_id, etype, key, payload, *a, **k):
            emitted.append(etype)
            return await orig(tenant_id, etype, key, payload, *a, **k)
        e.ai.emitter.emit_generic = counting

    def feed():
        return [i["kind"] for i in e.app.state.activity.recent("t1", 500)].count("ai.session.failed")
    return feed, emitted


async def _final_state_ok(e, reason):
    ai = await e.db["ai_sessions"].find_one({"session_id": e.sid})
    return ai if ai and ai["state"] == "failed" and ai["error"] == reason else None


async def test_connect_watchdog_fails_the_session_emits_once_and_leaves_no_task(media):
    e = media
    await e.c.post(f"/v1/ai/sessions/{e.sid}/stop", headers=auth())
    feed, emitted = _count_emits(e)
    e.ai.connect_s = 0.3
    assert (await e.c.post(f"/v1/ai/sessions/{e.sid}/start", json={}, headers=auth())).status_code == 202
    ai = await _wait(lambda: _final_state_ok(e, "media_connect_timeout"))
    assert ai and ai["stop_reason"] == "media_connect_timeout"
    await asyncio.sleep(0.5)                                   # anything skipped/duplicated by the watchdog would show up by now
    assert (await e.db["sessions"].find_one({"_id": e.sid}))["status"] == "answered"   # the call continues without AI
    assert feed() == 1 and emitted.count("ai.session.failed") <= 1
    assert e.sid not in e.ai.runtime
    assert _watchdogs() == []


async def test_reconnect_watchdog_returns_the_session_to_answered_and_emits_once(media):
    """The case the old code got wrong: the session was `active`, the media socket dropped and never came back."""
    e = media
    e.ai.reconnect_s = 0.3
    feed, emitted = _count_emits(e)
    ws = await connect(e)
    await hello(ws)
    assert (await e.db["sessions"].find_one({"_id": e.sid}))["status"] == "active"
    await ws.close()
    ai = await _wait(lambda: _final_state_ok(e, "media_reconnect_timeout"))
    assert ai and ai["stop_reason"] == "media_reconnect_timeout" and ai["media"]["connected"] is False
    await asyncio.sleep(0.5)
    sess = await e.db["sessions"].find_one({"_id": e.sid})
    assert sess["status"] == "answered", f"session left in {sess['status']!r} after the AI failed"   # was stuck `active`
    assert feed() == 1 and emitted.count("ai.session.failed") <= 1                                   # emitted once, not zero, not twice
    assert e.sid not in e.ai.runtime
    assert _watchdogs() == []


async def test_reconnecting_in_time_cancels_the_watchdog_and_keeps_the_session(media):
    e = media
    e.ai.reconnect_s = 5.0
    ws = await connect(e)
    await hello(ws)
    await ws.close()
    await _wait(lambda: _state_is(e, "media_disconnected"))
    assert _watchdogs() == ["AiService._media_deadline"]       # armed while the media is away
    ws = await connect(e)
    await hello(ws)
    assert (await e.db["ai_sessions"].find_one({"session_id": e.sid}))["state"] == "active"
    await asyncio.sleep(0.2)
    assert _watchdogs() == []                                  # cancelled by the reconnect: nothing left to fire later
    await ws.close()


async def _state_is(e, state):
    ai = await e.db["ai_sessions"].find_one({"session_id": e.sid})
    return ai if ai and ai["state"] == state else None
