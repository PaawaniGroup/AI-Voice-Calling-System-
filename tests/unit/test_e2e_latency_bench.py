"""Bookkeeping of scripts/e2e_latency_bench.py: RunPod delay/exec/request-id/usage capture, cold flags, spend cap, 3-error halt.
The RunPod API is a MOCK (httpx.MockTransport): this tests the harness, it produces NO latency evidence."""
import importlib.util
import time
from pathlib import Path

import httpx
import pytest

from app.ai.clients import AiUnavailable
from tests.conftest import make_settings
from tests.helpers import pcm_tone

_spec = importlib.util.spec_from_file_location("e2e_bench", Path(__file__).resolve().parents[2] / "scripts" / "e2e_latency_bench.py")
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)

BASE = "https://api.runpod.ai/v2/ep1"
PRICES = {"stt": 0.0002, "brain": 0.0005, "tts": 0.0002}
IDLE = {"stt": 5.0, "brain": 5.0, "tts": 5.0}


def _clients(handler, budget=1.5, start_zero=True):
    s = make_settings(ai_allowed_hosts="api.runpod.ai")
    cfg = {"stt_url": BASE + "/runsync", "brain_url": BASE + "/runsync", "tts_url": BASE + "/runsync",
           "keys": {"stt": "k", "brain": "k", "tts": "k"}}
    ledger = bench.Ledger(PRICES, IDLE, budget, cold_reserve_s=60)
    return bench.BenchClients(s, cfg, ledger, start_zero, transport=httpx.MockTransport(handler))


async def test_captures_delay_exec_request_id_usage_and_cold_flags():
    def handler(req):
        return httpx.Response(200, json={"id": "job-b", "status": "COMPLETED", "delayTime": 7300, "executionTime": 900,
                                         "output": [{"choices": [{"message": {"content": "ji"}}], "usage": {"prompt_tokens": 321, "completion_tokens": 4}}]})
    c = _clients(handler)
    await c.chat([{"role": "user", "content": "hi"}])
    await c.chat([{"role": "user", "content": "hi"}])
    await c.aclose()
    first, second = c.calls
    assert (first["request_id"], first["delay_ms"], first["exec_ms"], first["prompt_tokens"]) == ("job-b", 7300.0, 900.0, 321)
    assert first["cold_delay"] and first["cold_inferred"] and first["cold"]   # >5 s delay AND first request after zero
    assert second["cold_delay"] and not second["cold_inferred"]               # still a >5 s delay, but not the first request
    assert 0 < c.ledger.job_s["brain"] == pytest.approx(2 * 8.2)


async def test_three_consecutive_errors_halt_without_retries():
    n = {"calls": 0}

    def handler(req):
        n["calls"] += 1
        return httpx.Response(500)
    c = _clients(handler)
    for _ in range(3):
        with pytest.raises(Exception):
            await c.synthesize("namaste", "hi")
    assert c.halted and "3 consecutive errors on tts" in c.halted
    with pytest.raises(AiUnavailable):
        await c.synthesize("namaste", "hi")
    await c.aclose()
    assert n["calls"] == 3   # the 4th call was refused locally, nothing was sent


async def test_budget_cap_refuses_before_sending():
    sent = []
    c = _clients(lambda r: (sent.append(1), httpx.Response(200, json={}))[1], budget=0.001)
    with pytest.raises(AiUnavailable) as e:
        await c.transcribe(pcm_tone(200), 16000, "auto")
    await c.aclose()
    assert e.value.kind == "budget" and not sent and "budget cap" in c.halted


def test_ledger_idle_gap_is_capped_at_the_idle_timeout_and_tail_counted_once():
    led = bench.Ledger(PRICES, IDLE, 1.5, 0)
    led.begin("tts", 100.0)
    led.end("tts", 101.0, 1.0)
    led.begin("tts", 103.0)       # 2 s gap < 5 s idle timeout: billed as idle
    led.end("tts", 104.0, 1.0)
    led.begin("tts", 204.0)       # 100 s gap: worker scaled down after 5 s, only 5 s billed
    led.end("tts", 205.0, 1.0)
    rep = led.report()["per_service"]["tts"]
    assert rep["job_s"] == 3.0 and rep["idle_s"] == 7.0 and rep["tail_s"] == 5.0
    assert rep["usd"] == pytest.approx((3 + 7 + 5) * 0.0002)


def test_trailing_silence_is_trimmed_to_last_voiced_sample():
    voiced = pcm_tone(400)
    out = bench.trim_trailing_silence(voiced + b"\x00\x00" * bench.RATE)   # + 1 s digital silence
    assert abs(len(out) - len(voiced)) <= bench.RATE // 50 * 2 * 2          # within ~2 windows of 20 ms


def test_refuses_to_send_without_both_confirmation_flags(capsys):
    rc = bench.main(["--scenario", "s1", "--clips-dir", "none", "--idle", "stt=5,brain=5,tts=5", "--price", "stt=0.0002"])
    assert rc == 2 and "refusing to send anything" in capsys.readouterr().out
    rc = bench.main(["--scenario", "s1", "--clips-dir", "none", "--idle", "stt=5,brain=5,tts=5", "--price", "stt=0.0002", "--yes"])
    assert rc == 2
