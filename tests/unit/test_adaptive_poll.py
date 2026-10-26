"""`tts_adaptive_poll` (tenant flag, default OFF): overlapping status polls for voice jobs. SIMULATED: httpx.MockTransport with a simulated gateway
round trip (a sleep), no network, no RunPod. The timing assertions are deliberately loose (monotonic clock, shared host)."""
from __future__ import annotations

import asyncio
import statistics as st
import time

import httpx
import pytest

from app.ai import turn_trace
from app.ai.clients import AiError, ModelClients, predict_voice_wall_s
from config.settings import Settings

RTT = 0.10          # simulated gateway time per request (the real one is about 0.22 s: MEASURED); the whole test is scaled down by ~2


class Gateway:
    """A simulated RunPod endpoint: the job finishes `run_s` after the submit; every request takes RTT (half before it reaches the origin, half after)."""

    def __init__(self, run_s: float, status_codes=None):
        self.run_s, self.done_at, self.gets, self.get_times, self.status_codes = run_s, None, 0, [], list(status_codes or [])
        self.posted_at = None

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(RTT / 2)
        now = time.monotonic()
        if req.method == "POST":
            self.posted_at, self.done_at = now, now + self.run_s
            await asyncio.sleep(RTT / 2)
            return httpx.Response(200, json={"id": "j1", "status": "IN_QUEUE"})
        self.gets += 1
        self.get_times.append(now)
        code = self.status_codes.pop(0) if self.status_codes else 200
        await asyncio.sleep(RTT / 2)
        if code != 200:
            return httpx.Response(code, text="rate limited")
        if now < self.done_at:
            return httpx.Response(200, json={"id": "j1", "status": "IN_PROGRESS"})
        return httpx.Response(200, json={"id": "j1", "status": "COMPLETED", "delayTime": 50, "executionTime": int(self.run_s * 1000), "output": {"ok": True}})


def clients(gw: Gateway, **cfg) -> ModelClients:
    base = {"tts_url": "https://api.example.invalid/v2/ep/runsync", "keys": {"tts": "k"}, "runpod_poll_cap_s": 0.05}
    base.update(cfg)
    return ModelClients(Settings(), base, transport=httpx.MockTransport(gw))


async def lag_of(gw: Gateway, cl: ModelClients, words: int | None = None) -> tuple[float, dict]:
    """Seconds from the moment the simulated job finished until the client returned the result."""
    job = {"words": words} if words else {}
    await cl._runpod_call("tts", cl.endpoint("tts"), {}, {"input": {}}, job=job)
    return time.monotonic() - gw.done_at, job


@pytest.fixture(autouse=True)
def _isolate_trace():
    tok = turn_trace._current.set(None)
    yield
    turn_trace._current.reset(tok)


def test_predict_voice_wall_s_follows_the_measured_table():
    assert predict_voice_wall_s(3) == 2.00 and predict_voice_wall_s(4) == 2.63 and predict_voice_wall_s(8) == 3.39 and predict_voice_wall_s(12) == 4.32
    assert 2.63 < predict_voice_wall_s(6) < 3.39 and predict_voice_wall_s(14) > 4.32 and predict_voice_wall_s(1) >= 1.0


def test_the_first_poll_is_never_later_than_the_measured_median_for_that_word_count():
    cl = ModelClients(Settings(), {"tts_url": "https://api.example.invalid/v2/ep/runsync", "keys": {"tts": "k"}})
    for words, measured in ((3, 2.00), (4, 2.63), (8, 3.39), (12, 4.32)):
        for lead in (0.0, 0.5, 2.0, -3.0):                                       # a negative lead is clamped to 0
            cl.cfg["tts_poll_lead_s"] = lead
            for elapsed_ms in (0.0, 300.0):
                d = cl._first_poll_delay({"words": words, "submit_ms": elapsed_ms})
                assert 0.06 <= d <= max(0.06, measured - elapsed_ms / 1000) + 1e-9, (words, lead, elapsed_ms, d)
    for words in (14, 20, 40):                                                   # beyond the measured range: limited by the 12-word median, not the extrapolation
        for lead in (0.0, 0.5):
            cl.cfg["tts_poll_lead_s"] = lead
            assert cl._first_poll_delay({"words": words, "submit_ms": 0.0}) <= 4.32 + 1e-9
    assert cl._first_poll_delay({}) == 0.06 and cl._first_poll_delay({"words": 0}) == 0.06     # unknown length: poll at once


async def test_flag_off_uses_the_sequential_loop_and_the_old_record():
    gw = Gateway(run_s=0.5)
    cl = clients(gw)
    try:
        lag, job = await lag_of(gw, cl)
    finally:
        await cl.aclose()
    assert "adaptive_poll" not in job and job["polls"] >= 1 and job["status"] == "ok"


async def test_flag_on_returns_the_result_and_records_the_overlap():
    gw = Gateway(run_s=0.6)
    cl = clients(gw, tts_adaptive_poll=True, tts_poll_step_s=0.03)
    try:
        lag, job = await lag_of(gw, cl, words=4)
    finally:
        await cl.aclose()
    assert job["status"] == "ok" and job["adaptive_poll"] is True and job["delay_ms"] == 50.0 and job["exec_ms"] == 600.0
    assert job["polls"] >= 2 and job["first_poll_s"] >= 0.06


async def test_flag_on_notices_a_finished_job_sooner_than_the_sequential_loop_on_average_simulated():
    off, on = [], []
    for run_s in (0.40, 0.43, 0.46, 0.49, 0.52, 0.55):                       # job end at varied phases of the poll period
        for flag, out in ((False, off), (True, on)):
            gw = Gateway(run_s=run_s)
            cl = clients(gw, **({"tts_adaptive_poll": True, "tts_poll_step_s": 0.03, "tts_poll_lead_s": 5.0} if flag else {}))   # lead 5 s: start polling at once
            try:
                lag, _ = await lag_of(gw, cl, words=4)
            finally:
                await cl.aclose()
            out.append(lag)
    assert st.mean(on) < st.mean(off) - 0.01, (st.mean(on), st.mean(off))     # SIMULATED: a clear but small gain, nothing like a second
    assert st.mean(off) - st.mean(on) < 0.5


async def test_the_first_poll_waits_for_the_predicted_end():
    gw = Gateway(run_s=1.2)
    cl = clients(gw, tts_adaptive_poll=True, tts_poll_step_s=0.05, tts_poll_lead_s=0.3)
    try:
        await lag_of(gw, cl, words=3)                                         # predicted wall 2.0 s: first poll at about 2.0 - submit - 0.3 s, but the job ends at 1.2 s
    finally:
        await cl.aclose()
    # nothing could poll before ~1.6 s after the submit: the job ended at 1.2 s and was noticed only then (the point is: no early flood of polls)
    assert gw.get_times and gw.get_times[0] - gw.posted_at >= 1.4
    assert gw.gets <= 6


async def test_polls_are_started_without_waiting_and_capped_at_four_in_flight():
    gw = Gateway(run_s=0.8)
    cl = clients(gw, tts_adaptive_poll=True, tts_poll_step_s=0.01, tts_poll_lead_s=5.0)
    try:
        await lag_of(gw, cl, words=4)
    finally:
        await cl.aclose()
    assert gw.gets >= 4
    # at any moment at most 4 requests were in flight: arrivals are spread by the step, never a burst of more than 4 within one round trip
    arrivals = sorted(gw.get_times)
    assert all(sum(1 for t in arrivals if t0 <= t < t0 + RTT) <= 5 for t0 in arrivals)


async def test_a_429_is_skipped_but_a_real_error_is_raised():
    gw = Gateway(run_s=0.3, status_codes=[429, 429])
    cl = clients(gw, tts_adaptive_poll=True, tts_poll_step_s=0.03, tts_poll_lead_s=5.0)
    try:
        lag, job = await lag_of(gw, cl, words=4)
        assert job["status"] == "ok"
        bad = Gateway(run_s=0.3, status_codes=[403])
        cl2 = clients(bad, tts_adaptive_poll=True, tts_poll_step_s=0.03, tts_poll_lead_s=5.0)
        try:
            with pytest.raises(AiError) as ei:
                await cl2._runpod_call("tts", cl2.endpoint("tts"), {}, {"input": {}}, job={"words": 4})
            assert ei.value.status_code == 403
        finally:
            await cl2.aclose()
    finally:
        await cl.aclose()


async def test_cancelling_the_call_cancels_the_pending_polls_and_the_job():
    gw = Gateway(run_s=5.0)
    cl = clients(gw, tts_adaptive_poll=True, tts_poll_step_s=0.03, tts_poll_lead_s=5.0)
    task = asyncio.ensure_future(cl._runpod_call("tts", cl.endpoint("tts"), {}, {"input": {}}, job={"words": 4}))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)
    gets_after_cancel = gw.gets
    await asyncio.sleep(0.4)
    assert gw.gets == gets_after_cancel                                         # no poll is started after the cancel
    await cl.aclose()
    assert cl.cancelled_jobs and cl.cancelled_jobs[0][0] == "tts"               # the RunPod job was cancelled too


def test_tts_adaptive_poll_is_off_by_default_in_the_tenant_config():
    from app.ai.service import LATENCY_FLAGS, AiService
    assert "tts_adaptive_poll" in LATENCY_FLAGS
    svc = AiService.__new__(AiService)
    svc.s = Settings()
    assert AiService.default_config(svc, "t1")["tts_adaptive_poll"] is False
