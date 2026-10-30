"""`stt_adaptive_poll` (tenant flag, default OFF): overlapping status polls for the STT job, the same mechanism as `tts_adaptive_poll` with a fixed first poll
(`stt_poll_first_s`, default 0.25 s) and a 0.12 s step (`stt_poll_step_s`). SIMULATED: httpx.MockTransport with a simulated gateway round trip, no network, no RunPod.
DRAFT: written, compiled, NOT RUN."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.ai import turn_trace
from app.ai.clients import ModelClients
from config.settings import Settings

RTT = 0.10


class Gateway:
    def __init__(self, run_s: float):
        self.run_s, self.done_at, self.gets = run_s, None, 0

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(RTT / 2)
        now = time.monotonic()
        if req.method == "POST":
            self.done_at = now + self.run_s
            await asyncio.sleep(RTT / 2)
            return httpx.Response(200, json={"id": "j1", "status": "IN_QUEUE"})
        self.gets += 1
        await asyncio.sleep(RTT / 2)
        if now < self.done_at:
            return httpx.Response(200, json={"id": "j1", "status": "IN_PROGRESS"})
        return httpx.Response(200, json={"id": "j1", "status": "COMPLETED", "delayTime": 40, "executionTime": int(self.run_s * 1000), "output": {"text": "x"}})


def clients(gw: Gateway, **cfg) -> ModelClients:
    base = {"stt_url": "https://api.example.invalid/v2/ep/runsync", "tts_url": "https://api.example.invalid/v2/ep2/runsync", "keys": {"stt": "k", "tts": "k"},
            "runpod_poll_cap_s": 0.05}
    base.update(cfg)
    return ModelClients(Settings(_env_file=None), base, transport=httpx.MockTransport(gw))


@pytest.fixture(autouse=True)
def _isolate_trace():
    tok = turn_trace._current.set(None)
    yield
    turn_trace._current.reset(tok)


async def _stt_job(cl: ModelClients) -> dict:
    job: dict = {}
    await cl._runpod_call("stt", cl.endpoint("stt"), {}, {"input": {}}, job=job)
    return job


async def test_flag_off_keeps_the_sequential_loop_for_stt():
    cl = clients(Gateway(0.4))
    try:
        job = await _stt_job(cl)
    finally:
        await cl.aclose()
    assert job["status"] == "ok" and "adaptive_poll" not in job and job["polls"] >= 1


async def test_flag_on_uses_overlapping_polls_with_the_fixed_first_poll_for_stt():
    cl = clients(Gateway(0.4), stt_adaptive_poll=True, stt_poll_first_s=0.1, stt_poll_step_s=0.03)
    try:
        job = await _stt_job(cl)
    finally:
        await cl.aclose()
    assert job["status"] == "ok" and job["adaptive_poll"] is True and job["first_poll_s"] == 0.1 and job["polls"] >= 2 and job["exec_ms"] == 400.0


async def test_the_default_first_poll_is_a_quarter_second():
    gw = Gateway(0.5)
    cl = clients(gw, stt_adaptive_poll=True, stt_poll_step_s=0.03)
    try:
        job = await _stt_job(cl)
    finally:
        await cl.aclose()
    assert job["first_poll_s"] == 0.25 and job["adaptive_poll"] is True


async def test_each_flag_only_affects_its_own_service():
    cl = clients(Gateway(0.3), tts_adaptive_poll=True, tts_poll_step_s=0.03)           # the voice flag does not switch the STT job
    try:
        job = await _stt_job(cl)
    finally:
        await cl.aclose()
    assert "adaptive_poll" not in job
    cl = clients(Gateway(0.3), stt_adaptive_poll=True, stt_poll_first_s=0.05, stt_poll_step_s=0.03)   # and the STT flag does not switch a voice job
    try:
        j2: dict = {"words": 4}
        await cl._runpod_call("tts", cl.endpoint("tts"), {}, {"input": {}}, job=j2)
    finally:
        await cl.aclose()
    assert "adaptive_poll" not in j2


async def test_the_flag_defaults_off_and_survives_get_config():
    from app.ai.service import LATENCY_FLAGS, AiService
    assert "stt_adaptive_poll" in LATENCY_FLAGS

    def svc(doc):
        coll = MagicMock()
        coll.find_one = AsyncMock(return_value=doc)
        db = MagicMock()
        db.__getitem__.return_value = coll
        return AiService(db, None, Settings(_env_file=None), None)
    assert (await svc(None).get_config("t"))["stt_adaptive_poll"] is False
    assert (await svc({"tenant_id": "t", "stt_adaptive_poll": True}).get_config("t"))["stt_adaptive_poll"] is True, "dropped by the allowlist"


def test_the_api_model_accepts_the_flag():
    from app.api.routes_platform import AiConfigIn
    assert AiConfigIn(stt_adaptive_poll=True).stt_adaptive_poll is True
