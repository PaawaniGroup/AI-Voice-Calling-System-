"""Keep-alive ping of the voice endpoint during a call. SIMULATED: virtual clock and a MOCK ping; no RunPod job is created and nothing is spent.
(Whether a ping really keeps a worker's pickup fast is NOT shown here: that needs one capped paid measurement.)"""
import asyncio
import json

import httpx
import pytest

from app.ai import turn_trace
from app.ai.clients import ModelClients
from app.ai.keepalive import KeepAlive
from tests.conftest import make_settings
from tests.fakeclock import FakeClock


class Ping:
    def __init__(self, clock, results=None, hang=False):
        self.clock, self.results, self.hang = clock, list(results or []), hang
        self.times, self.cancelled, self.trace_seen = [], 0, []

    async def __call__(self):
        self.times.append(self.clock.now())
        self.trace_seen.append(turn_trace.current())
        try:
            if self.hang:
                await self.clock.sleep(1000)
            return self.results.pop(0) if self.results else True
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


async def run_for(clock, ka, seconds, stop_at=None):
    async def scenario():
        ka.start()
        await clock.sleep(seconds)
        if stop_at is not None:
            await ka.stop("test")
    await clock.run(scenario())


async def test_pings_at_the_interval_and_stops_with_the_call_and_no_ping_follows():
    clock = FakeClock()
    p = Ping(clock)
    ka = KeepAlive(p, interval_s=5.0, max_s=300.0, clock=clock)
    seen = {}

    async def scenario():
        ka.start()
        await clock.sleep(21.0)
        seen["n"] = ka.pings
        await ka.stop("session_ended")                                              # the call ended
        await clock.sleep(60.0)
        seen["after"] = ka.pings

    await clock.run(scenario())
    assert seen["n"] == 4 and seen["after"] == 4 and not ka.running and ka.stop_reason == "session_ended"
    assert [round(b - a, 1) for a, b in zip(p.times, p.times[1:])] == [5.0, 5.0, 5.0]


async def test_a_ping_is_skipped_while_a_real_voice_job_ran_within_the_interval():
    clock = FakeClock()
    p = Ping(clock)
    last = {"t": None}
    ka = KeepAlive(p, interval_s=5.0, max_s=300.0, busy_since=lambda: last["t"], clock=clock)

    async def scenario():
        ka.start()
        await clock.sleep(4.0)
        last["t"] = clock.now()                                                     # a real clause was synthesized just now
        await clock.sleep(2.0)                                                      # the ping due at 5 s is skipped (the job is 1 s old)
        await clock.sleep(5.0)                                                      # the one due at 10 s runs (the job is 7 s old)
        await ka.stop()

    await clock.run(scenario())
    assert ka.skipped == 1 and ka.pings == 1


async def test_it_stops_by_itself_after_max_s_so_a_forgotten_call_cannot_ping_forever():
    clock = FakeClock()
    p = Ping(clock)
    ka = KeepAlive(p, interval_s=5.0, max_s=30.0, clock=clock)
    await run_for(clock, ka, 600.0)
    assert ka.stop_reason == "max_s" and not ka.running and ka.pings <= 6
    n = ka.pings
    await clock.run(asyncio.sleep(0))
    assert ka.pings == n


async def test_three_failures_in_a_row_stop_it_and_a_success_resets_the_count():
    clock = FakeClock()
    ka = KeepAlive(Ping(clock, results=[False, False, True, False, False, False]), interval_s=5.0, max_s=900.0, clock=clock)
    await run_for(clock, ka, 200.0)
    assert ka.stop_reason == "failures" and ka.pings == 6 and ka.failures == 5


async def test_a_ping_that_raises_counts_as_a_failure_and_never_breaks_the_call():
    clock = FakeClock()

    async def boom():
        raise RuntimeError("worker down")

    ka = KeepAlive(boom, interval_s=5.0, max_s=900.0, clock=clock)
    await run_for(clock, ka, 100.0)
    assert ka.stop_reason == "failures" and ka.failures == 3


async def test_a_hung_ping_is_cut_at_its_timeout_and_cancelled():
    clock = FakeClock()
    p = Ping(clock, hang=True)
    ka = KeepAlive(p, interval_s=5.0, max_s=900.0, ping_timeout_s=10.0, clock=clock)
    await run_for(clock, ka, 100.0)
    assert p.cancelled >= 3 and ka.stop_reason == "failures"                         # each hung ping was cancelled (the client then cancels the RunPod job)


async def test_stopping_during_a_ping_cancels_it():
    clock = FakeClock()
    p = Ping(clock, hang=True)
    ka = KeepAlive(p, interval_s=5.0, max_s=900.0, ping_timeout_s=60.0, clock=clock)

    async def scenario():
        ka.start()
        await clock.sleep(7.0)                                                      # a ping is in flight since 5 s
        await ka.stop("session_ended")

    await clock.run(scenario())
    assert p.cancelled == 1 and not ka.running


async def test_a_ping_never_writes_into_the_trace_of_the_turn_that_was_current_at_start():
    clock = FakeClock()
    p = Ping(clock)
    tok = turn_trace.start(turn_trace.TurnTrace())
    try:
        ka = KeepAlive(p, interval_s=5.0, max_s=60.0, clock=clock)
        await run_for(clock, ka, 12.0, stop_at=True)
    finally:
        turn_trace._current.reset(tok)
    assert p.times and all(t is None for t in p.trace_seen)


async def test_stop_before_start_and_twice_is_harmless():
    clock = FakeClock()
    ka = KeepAlive(Ping(clock), clock=clock)
    await ka.stop()
    await ka.stop()
    assert ka.report() == {"pings": 0, "skipped": 0, "failures": 0, "running": False, "stop_reason": None}


# ------------------------------------------------------------------ the client side: one no-op job, no synthesis
def _clients(handler, **cfg):
    s = make_settings(ai_allowed_hosts="api.runpod.ai")
    full = {"tts_url": "https://api.runpod.ai/v2/abc123/runsync", "keys": {"tts": "k"}, "runpod_mode": "run", **cfg}
    return ModelClients(s, full, httpx.MockTransport(handler))


async def test_ping_tts_submits_the_workers_no_op_job_and_is_true_only_when_it_answers_ok():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path.endswith("/run"):
            return httpx.Response(200, json={"id": "p1", "status": "IN_QUEUE"})
        return httpx.Response(200, json={"id": "p1", "status": "COMPLETED", "output": {"ok": True}})

    c = _clients(handler)
    assert await c.ping_tts() is True
    await c.aclose()
    assert seen[0] == ("POST", "/v2/abc123/run", {"input": {"warmup": True}})        # no text, no reference audio, nothing to synthesize
    assert c.last_tts_at is None                                                     # a ping is not a real voice job


async def test_ping_tts_is_false_on_a_worker_error_a_wrong_answer_or_a_plain_endpoint_and_never_raises():
    def failing(req):
        return httpx.Response(200, json={"id": "p", "status": "FAILED", "error": "boom"}) if not req.url.path.endswith("/run") else httpx.Response(200, json={"id": "p", "status": "IN_QUEUE"})

    c = _clients(failing)
    assert await c.ping_tts() is False
    await c.aclose()
    c = _clients(lambda r: httpx.Response(500))
    assert await c.ping_tts() is False
    await c.aclose()
    c = _clients(lambda r: httpx.Response(200), tts_url="http://tts.test:9000")       # not a RunPod /runsync URL: nothing to ping
    assert await c.ping_tts() is False
    await c.aclose()
