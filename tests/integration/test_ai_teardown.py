"""Hangup mid-inference must end the media socket handler within seconds and leave no task behind.
Regression for the teardown hangs of test_hangup_cancels_ai_and_closes_media_no_stale_audio and
test_hangup_during_inference_cancels_it_and_no_audio_arrives_afterwards (root cause: a swallowed CancelledError, see
tests/unit/test_pipeline_cancellation.py). Model services are MOCKS; the media client is SYNTHETIC."""
import asyncio
import json
import logging
import time

import websockets

from tests.conftest import auth
from tests.helpers import pcm_tone
from tests.integration.test_ai import collect, connect, esl, hello, media, speak  # noqa: F401  (`media` is a fixture)

WORKER = "serve_loop.<locals>.worker"
HANDLERS = ("handle_media", "handle_plivo_media")


def _chain(coro) -> list[str]:
    """Qualnames of a task's whole await chain (the socket handler is awaited by uvicorn's task, it is not a task root)."""
    out = []
    while coro is not None:
        out.append(getattr(coro, "__qualname__", ""))
        coro = getattr(coro, "cr_await", None) or getattr(coro, "ag_await", None)
    return out


def _live(names: tuple[str, ...]) -> list[str]:
    found = []
    for t in asyncio.all_tasks():
        if not t.done():
            found += [q for q in _chain(t.get_coro()) if q in names]
    return found


def _leaked() -> list[str]:
    return _live((WORKER,) + HANDLERS)


async def _until_none(names: tuple[str, ...], timeout: float) -> list[str]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not _live(names):
            return []
        await asyncio.sleep(0.05)
    return _live(names)


async def _until_no_leak(timeout: float) -> list[str]:
    return await _until_none((WORKER,) + HANDLERS, timeout)


async def _closed_within(ws, seconds: float) -> float:
    t0 = time.monotonic()
    try:
        while True:
            await asyncio.wait_for(ws.recv(), seconds)
    except websockets.ConnectionClosed:
        return time.monotonic() - t0
    except asyncio.TimeoutError:
        raise AssertionError(f"media socket still open {seconds}s after hangup") from None


async def test_hangup_mid_stt_closes_the_socket_within_seconds_and_leaves_no_tasks(media):
    e = media
    e.models.stt_delay = 30.0                      # STT would run far past the test: only cancellation can end the turn
    ws = await connect(e)
    await hello(ws)
    await speak(ws, silence=800)
    await asyncio.sleep(0.4)                       # inference in flight
    await e.ctrl.on_esl_event(esl("CHANNEL_HANGUP", e.fs_uuid, **{"Hangup-Cause": "NORMAL_CLEARING"}))
    assert await _closed_within(ws, 4.0) < 4.0
    assert await _until_no_leak(3.0) == []         # worker AND handler are gone
    assert e.sid not in e.ai.runtime
    assert (await e.c.get(f"/v1/sessions/{e.sid}", headers=auth())).json()["status"] == "ended"


async def test_a_worker_that_ignores_cancellation_cannot_keep_the_handler_alive(media, caplog):
    """Defence in depth: even if some future code swallows cancellation again, the bounded wait in serve_loop ends the handler."""
    e = media
    stop = asyncio.Event()

    async def stubborn(pcm, rate):                 # the OLD bug, forever: refuses every cancellation
        while not stop.is_set():
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                continue
        return
        yield                                      # pragma: no cover  (makes this an async generator)

    ws = await connect(e)
    await hello(ws)
    e.ai.runtime[e.sid].conversation.stream_turn = stubborn
    try:
        await speak(ws, silence=800)
        await asyncio.sleep(0.4)
        with caplog.at_level(logging.ERROR, logger="mme_telephony"):
            await e.ctrl.on_esl_event(esl("CHANNEL_HANGUP", e.fs_uuid, **{"Hangup-Cause": "NORMAL_CLEARING"}))
            await _closed_within(ws, 6.0)
            # bounded wait = 2 s + 1 s: the handler must be gone shortly after that although the worker never honours cancellation
            assert await _until_none(HANDLERS, 6.0) == [], "socket handler still alive: the bounded wait did not release it"
            assert WORKER in _leaked()             # the stubborn worker is (deliberately) still there: that is the case under test
            assert "did not stop after cancellation" in caplog.text
    finally:
        stop.set()                                 # always release the deliberately stubborn worker, even if an assert failed
        await asyncio.sleep(0.3)                   # let it leave the stub (a cancel delivered inside the stub would be swallowed again)
        for t in asyncio.all_tasks():              # the abandoned worker is now parked on its queue: this test cleans it up itself
            if not t.done() and WORKER in _chain(t.get_coro()):
                t.cancel()
    assert await _until_no_leak(3.0) == []


async def test_barge_in_stops_audio_and_the_next_turn_still_works(media):
    e = media
    e.models.tts_ms = 3000
    ws = await connect(e)
    await hello(ws)
    await speak(ws)
    await collect(ws, "playback_start")
    for _ in range(15):                            # caller talks over the assistant
        await ws.send(pcm_tone(20))
        await asyncio.sleep(0.01)
    kinds = []
    for _ in range(100):
        m = await asyncio.wait_for(ws.recv(), 5)
        if isinstance(m, str):
            kinds.append(json.loads(m)["type"])
            if "interrupt" in kinds:
                break
    assert "interrupt" in kinds and "playback_end" not in kinds
    assert _leaked() != []                         # worker and handler are still alive after a barge-in (it is not a teardown)
    e.models.tts_ms = 300
    await speak(ws)
    msgs, audio = await collect(ws, "playback_end")
    assert audio > 0                               # the conversation recovered and answered the next utterance
    await ws.close()
