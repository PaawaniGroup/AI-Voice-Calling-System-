"""`stt_raw_pcm` one-shot fallback. SIMULATED: httpx.MockTransport as the STT worker (one that rejects the raw PCM body, as a worker without `audio_pcm16_b64` would), a synthetic
tone, no network, no RunPod. DRAFT: written, compiled, NOT RUN. Never any caller or reply word."""
from __future__ import annotations

import array
import json
import logging
import math

import httpx
import pytest

from app.ai import turn_trace
from app.ai.clients import AiError, AiTimeout, AiUnavailable, ModelClients, _rejects_audio_fields
from config.settings import Settings

RATE = 8000
OLD_FIELDS = {"audio_b64", "audio", "audio_base64", "language"}


def tone(seconds: float = 1.0) -> bytes:
    return array.array("h", [int(6000 * math.sin(2 * math.pi * 300 * i / RATE)) for i in range(int(RATE * seconds))]).tobytes()


class Worker:
    """mode: "accept" (reads both bodies), "reject_failed" (a FAILED job naming the audio fields), "reject_400" (HTTP 400 on /run), "reject_all" (rejects the old body too),
    "oom" (an unrelated FAILED job), "down" (the endpoint is unreachable)."""

    def __init__(self, mode: str):
        self.mode, self.bodies, self.fail_next = mode, [], set()

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.mode == "down":
            raise httpx.ConnectError("unreachable")
        if req.method == "POST" and req.url.path.endswith("/run"):
            inp = json.loads(req.content)["input"]
            self.bodies.append(inp)
            raw = "audio_pcm16_b64" in inp
            if self.mode == "reject_400" and raw:
                return httpx.Response(400, text="audio_pcm16_b64 is not a known input field")
            jid = f"j{len(self.bodies)}"
            self.fail_next.add(jid) if ((self.mode == "reject_failed" and raw) or self.mode in ("reject_all", "oom")) else None
            return httpx.Response(200, json={"id": jid, "status": "IN_QUEUE"})
        jid = req.url.path.rsplit("/", 1)[-1]
        if jid in self.fail_next:
            err = "CUDA out of memory" if self.mode == "oom" else "Provide audio_b64 (WAV) in the input"
            return httpx.Response(200, json={"id": jid, "status": "FAILED", "error": err})
        return httpx.Response(200, json={"id": jid, "status": "COMPLETED", "delayTime": 80, "executionTime": 400, "output": {"text": "ok", "language": "hi"}})


def make(mode: str, **cfg):
    w = Worker(mode)
    base = {"stt_url": "https://api.example.invalid/v2/ep/runsync", "keys": {"stt": "k"}, "runpod_poll_cap_s": 0.03, "stt_raw_pcm": True}
    base.update(cfg)
    return ModelClients(Settings(_env_file=None), base, transport=httpx.MockTransport(w)), w


@pytest.fixture(autouse=True)
def _trace():
    tr = turn_trace.TurnTrace(t_speech_end=0.0)
    tok = turn_trace._current.set(tr)
    yield tr
    turn_trace._current.reset(tok)


async def test_a_worker_that_rejects_the_raw_body_gets_one_retry_with_the_old_body_and_the_flag_is_off_for_the_rest_of_the_call(_trace, caplog):
    caplog.set_level(logging.DEBUG)
    cl, w = make("reject_failed")
    try:
        out = await cl.transcribe(tone(), RATE, "hinglish")
        assert out["text"] == "ok"
        assert "audio_pcm16_b64" in w.bodies[0] and set(w.bodies[1]) == OLD_FIELDS             # the new body first, then the old one
        assert cl._stt_raw_off is True and len(w.bodies) == 2
        await cl.transcribe(tone(), RATE, "hinglish")                                          # the next utterance of the SAME call goes straight to the old body
        assert len(w.bodies) == 3 and set(w.bodies[2]) == OLD_FIELDS
    finally:
        await cl.aclose()
    first, second = _trace.jobs[0], _trace.jobs[1]
    assert first["raw_pcm"] is True and first["status"].startswith("error:") and "raw_fallback" not in first
    assert second["raw_pcm"] is False and second["raw_fallback"] is True and second["status"] == "ok" and second["audio_fields"] == 3
    assert _trace.turn["stt_raw_fallback"] == 1                                                # counted once, no content
    assert "stt_raw_pcm" in caplog.text and "AAAA" not in caplog.text                           # the log line names the flag; no audio (base64) in it


async def test_an_http_400_that_names_the_audio_fields_is_also_a_rejection():
    cl, w = make("reject_400")
    try:
        out = await cl.transcribe(tone(), RATE, "auto")
    finally:
        await cl.aclose()
    assert out["text"] == "ok" and len(w.bodies) == 2                                         # the rejected raw request, then the retry
    assert "audio_pcm16_b64" in w.bodies[0] and set(w.bodies[1]) == OLD_FIELDS                # in this order: first the raw body, second the old three-field body
    assert cl._stt_raw_off is True


async def test_an_unrelated_worker_error_is_not_retried_and_does_not_switch_the_flag_off():
    cl, w = make("oom")
    try:
        with pytest.raises(AiError):
            await cl.transcribe(tone(), RATE, "auto")
    finally:
        await cl.aclose()
    assert len(w.bodies) == 1 and cl._stt_raw_off is False


async def test_an_unreachable_worker_is_not_retried_with_another_body():
    cl, w = make("down")
    try:
        with pytest.raises(AiError):
            await cl.transcribe(tone(), RATE, "auto")
    finally:
        await cl.aclose()
    assert cl._stt_raw_off is False


async def test_the_retry_is_one_shot_when_the_old_body_fails_too_the_error_is_raised_after_exactly_two_attempts():
    cl, w = make("reject_all")
    try:
        with pytest.raises(AiError):
            await cl.transcribe(tone(), RATE, "auto")
    finally:
        await cl.aclose()
    assert len(w.bodies) == 2 and "audio_pcm16_b64" in w.bodies[0] and set(w.bodies[1]) == OLD_FIELDS


async def test_with_the_flag_off_an_audio_field_error_is_raised_and_never_retried():
    cl, w = make("reject_all", stt_raw_pcm=False)
    try:
        with pytest.raises(AiError):
            await cl.transcribe(tone(), RATE, "auto")
    finally:
        await cl.aclose()
    assert len(w.bodies) == 1 and set(w.bodies[0]) == OLD_FIELDS


async def test_a_worker_that_accepts_the_raw_body_causes_no_fallback(_trace):
    cl, w = make("accept")
    try:
        await cl.transcribe(tone(), RATE, "auto")
        await cl.transcribe(tone(), RATE, "auto")
    finally:
        await cl.aclose()
    assert len(w.bodies) == 2 and all("audio_pcm16_b64" in b for b in w.bodies) and cl._stt_raw_off is False
    assert "stt_raw_fallback" not in _trace.turn and all("raw_fallback" not in j for j in _trace.jobs)


async def test_the_switch_is_per_call_a_new_clients_object_tries_the_raw_body_again():
    a, wa = make("reject_failed")
    b, wb = make("accept")
    try:
        await a.transcribe(tone(), RATE, "auto")
        assert a._stt_raw_off is True
        await b.transcribe(tone(), RATE, "auto")
        assert b._stt_raw_off is False and "audio_pcm16_b64" in wb.bodies[0]
    finally:
        await a.aclose()
        await b.aclose()


def test_which_errors_count_as_a_rejection_of_the_audio_fields():
    assert _rejects_audio_fields(AiError("stt", "runpod_error", "status=FAILED Provide audio_b64 (WAV) in the input"))
    assert _rejects_audio_fields(AiError("stt", "http_error", "400", status_code=400, body="unknown input audio_pcm16_b64"))
    assert not _rejects_audio_fields(AiError("stt", "runpod_error", "status=FAILED CUDA out of memory"))
    assert not _rejects_audio_fields(AiError("stt", "http_error", "500", status_code=500, body="audio_b64 server error"))      # a 5xx is an outage, not a body problem
    assert not _rejects_audio_fields(AiTimeout("stt", "timeout")) and not _rejects_audio_fields(AiUnavailable("stt", "unreachable", "ConnectError"))
    assert not _rejects_audio_fields(AiError("stt", "http_error", "401", status_code=401, body="audio_b64"))


@pytest.mark.parametrize("rate", [8000, 16000])
async def test_the_raw_body_always_carries_sample_rate_equal_to_the_capture_rate(rate):
    """The live handler defaults a missing sample_rate to 16000 and would silently decode garbage: omitting it, or sending any other value, must fail here."""
    cl, w = make("accept")
    try:
        await cl.transcribe(tone(0.5), rate, "auto")
    finally:
        await cl.aclose()
    body = w.bodies[0]
    assert "sample_rate" in body and body["sample_rate"] == rate and isinstance(body["sample_rate"], int) and not isinstance(body["sample_rate"], bool)


async def test_every_raw_body_of_a_call_has_the_rate_including_the_attempt_that_gets_rejected():
    cl, w = make("reject_failed")
    try:
        await cl.transcribe(tone(0.5), RATE, "auto")
    finally:
        await cl.aclose()
    raw = [b for b in w.bodies if "audio_pcm16_b64" in b]
    assert raw and all(b.get("sample_rate") == RATE for b in raw)


@pytest.mark.parametrize("bad", [0, -8000, None, 8000.0, True])
async def test_without_a_usable_capture_rate_the_raw_body_is_never_sent(bad):
    cl, w = make("accept")
    try:
        await cl.transcribe(tone(0.5), bad, "auto")
    except Exception:
        pass                                                                   # the old body's WAV wrapper may itself refuse a nonsense rate; what matters is below
    finally:
        await cl.aclose()
    assert not any("audio_pcm16_b64" in b for b in w.bodies)


async def test_a_plivo_call_sends_8000(_trace):
    cl, w = make("accept")
    try:
        await cl.transcribe(tone(0.5), 8000, "hinglish")
    finally:
        await cl.aclose()
    assert w.bodies[0]["sample_rate"] == 8000 and _trace.jobs[0]["sample_rate"] == 8000