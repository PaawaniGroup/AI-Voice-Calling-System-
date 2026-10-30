"""The TTS benchmark harness itself (mock endpoint: this checks the harness, it measures nothing about a real worker)."""
import base64
import json

import httpx

from app.ai.clients import ModelClients, pcm16_to_wav
from scripts.bench_tts_stream import pct, run
from tests.conftest import make_settings
from tests.helpers import pcm_tone
from tests.unit.test_tts_stream_client import BASE, Worker, out, ulaw_item


def test_percentile_is_nearest_rank():
    assert pct([1, 2, 3, 4, 5], 50) == 3 and pct([1, 2, 3, 4, 5], 99) == 5 and pct([7], 95) == 7


class Endpoint(Worker):
    """Streams on /run when the body asks for it, otherwise answers the non-streaming /run + /status like today's worker."""

    def __call__(self, req):
        if req.url.path.endswith("/run"):
            self.stream = json.loads(req.content)["input"].get("stream", False)
        if self.stream and "/stream/" in req.url.path:
            self.polls = [out(ulaw_item(0, 30), ulaw_item(1, 30), ulaw_item(2, 30, final=True))]
        if not self.stream and "/status/" in req.url.path:
            wav = pcm16_to_wav(pcm_tone(300), 24000)
            self.status_output = {"audio_base64": base64.b64encode(wav).decode(), "format": "wav"}
        return super().__call__(req)

    stream = False


async def test_harness_runs_both_modes_and_reports_percentiles_and_chunk_counts():
    s = make_settings(ai_allowed_hosts="api.runpod.ai")
    c = ModelClients(s, {"tts_url": BASE + "/runsync", "keys": {"tts": "k"}}, httpx.MockTransport(Endpoint([])))
    rep = await run(c, runs=2, warmup=1, prompts={"p": ("hi", "namaste")})
    await c.aclose()
    comp, strm = rep["results"]["p/complete"], rep["results"]["p/stream"]
    assert comp["chunks_p50"] == 1 and strm["chunks_p50"] == 3
    assert strm["first_audio_s"]["n"] == 2 and strm["rtf"]["p95"] >= 0 and "warning" not in strm
    assert strm["first_audio_s"]["p50"] <= strm["total_s"]["p50"]
