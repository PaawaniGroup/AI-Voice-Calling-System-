"""Per-tenant flag `tts_ulaw8k` on the REAL service stack (routes, MongoDB, AiService, Plivo stream handler, conversation loop) with MOCK Plivo, STT, Brain and a MOCK
RunPod-style TTS worker (v3: voices by name + 8 kHz mu-law). Default OFF; nothing here enables it for a real tenant."""
import audioop
import base64

from tests.conftest import auth
from tests.helpers import pcm_tone
from tests.integration.test_plivo_voice import (RATE, answered_stream, connect, pv, recv_until, send_media, until, utterance, ai_doc)  # noqa: F401

ADMIN = lambda: auth("t1", ("telephony:admin",))  # noqa: E731
RUNPOD = {"stt_url": "http://stt.test:9000/v2/ep/runsync", "brain_url": "http://brain.test:9000/v2/ep/runsync",
          "tts_url": "http://tts.test:9000/v2/ep/runsync", "rag_url": "http://rag.test:9000", "enabled": True}


async def _cfg(pv, **extra):
    r = await pv.c.put("/v1/ai/config", json={**RUNPOD, **extra}, headers=ADMIN())
    assert r.status_code == 200, r.text


async def _call(pv, uuid):
    a = await answered_stream(pv, call_uuid=uuid)
    ws = await connect(pv, a)
    async def active():
        d = await ai_doc(pv, a.sid)
        return d if d and d["state"] == "active" else None
    await until(active)
    await _settle_warmup(pv)
    return a, ws


async def _settle_warmup(pv):
    """Answering a call sends warm-up jobs to RunPod-style endpoints; wait until they are done, then forget them."""
    import asyncio
    await asyncio.sleep(0.8)


def tts_inputs(pv):
    return [b["input"] for k, b in pv.models.calls if k == "tts"]


# ------------------------------------------------------------------ the setting
async def test_default_is_off_and_the_setting_is_validated_and_stored_per_tenant(pv):
    cfg = (await pv.c.get("/v1/ai/config", headers=ADMIN())).json()
    assert cfg["tts_ulaw8k"] is False and cfg["tts_voice"] is None
    assert (await pv.c.put("/v1/ai/config", json={"tts_voice": "bad voice!"}, headers=ADMIN())).status_code == 422
    assert (await pv.c.put("/v1/ai/config", json={"tts_voice": "x" * 41}, headers=ADMIN())).status_code == 422
    assert (await pv.c.put("/v1/ai/config", json={"tts_ulaw8k": "yes please"}, headers=ADMIN())).status_code == 422
    r = await pv.c.put("/v1/ai/config", json={"tts_ulaw8k": True, "tts_voice": "priya"}, headers=ADMIN())
    assert r.status_code == 200 and r.json()["tts_ulaw8k"] is True and r.json()["tts_voice"] == "priya"
    stored = await pv.db["ai_config"].find_one({"tenant_id": "t1"})
    assert stored["tts_ulaw8k"] is True and stored["tts_voice"] == "priya"
    other = await pv.ai.get_config("t2")                                                    # another tenant is unaffected
    assert other["tts_ulaw8k"] is False and other["tts_voice"] is None
    await pv.c.put("/v1/ai/config", json={"tts_ulaw8k": False}, headers=ADMIN())
    assert (await pv.c.get("/v1/ai/config", headers=ADMIN())).json()["tts_ulaw8k"] is False


# ------------------------------------------------------------------ a call
async def test_flag_off_keeps_the_old_request_and_the_wav_path(pv):
    await _cfg(pv)
    a, ws = await _call(pv, "ULAW-OFF")
    pv.models.calls.clear()
    await send_media(ws, utterance())
    await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=15)
    inputs = tts_inputs(pv)
    assert inputs and all("voice" not in i and "output_format" not in i for i in inputs)
    await ws.close()


async def test_flag_on_a_call_asks_for_a_named_voice_in_ulaw8k_sends_no_reference_and_plays_the_answer(pv):
    pv.s.ai_tts_reference_wav = "/nonexistent/reference.wav"       # any attempt to read the reference would fail the turn
    pv.models.tts_ms = 400
    await _cfg(pv, tts_ulaw8k=True)
    a, ws = await _call(pv, "ULAW-ON")
    pv.models.calls.clear()
    await send_media(ws, utterance())
    msgs = await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=15)
    inputs = tts_inputs(pv)
    assert inputs and all(i["voice"] == "utkarsh" and i["output_format"] == "ulaw8k" and "reference_audio_base64" not in i for i in inputs)
    mu = b"".join(base64.b64decode(m["media"]["payload"]) for m in msgs if m["event"] == "playAudio")
    worker = audioop.lin2ulaw(pcm_tone(400, 8000), 2)
    assert len(mu) == len(worker) * len(inputs) or len(mu) >= len(worker)             # whole replies of 8 kHz mu-law: no resampling, no stretching
    same = sum(1 for x, y in zip(mu[:len(worker)], worker) if x == y)
    assert same >= 0.99 * len(worker)                                                  # the worker's mu-law came out the other side (decode -> encode round trip)
    assert (await ai_doc(pv, a.sid))["state"] == "active"
    await ws.close()


async def test_flag_on_with_the_streaming_pipeline_the_clauses_are_8khz_and_nothing_is_resampled(pv):
    pv.s.ai_tts_reference_wav = "/nonexistent/reference.wav"
    pv.models.brain_sse, pv.models.tts_ms = True, 300
    pv.models.reply = "जी, मैं आपको admission की पूरी जानकारी देता हूँ। B.Tech चार साल का course है, और admission entrance exam से होता है।"
    await _cfg(pv, tts_ulaw8k=True, streaming_pipeline=True)
    a, ws = await _call(pv, "ULAW-STREAM")
    pv.models.calls.clear()
    await send_media(ws, utterance())
    await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=20)
    inputs = tts_inputs(pv)
    assert len(inputs) >= 2 and all(i["voice"] == "utkarsh" and i["output_format"] == "ulaw8k" and "reference_audio_base64" not in i for i in inputs)
    conv = pv.ai.runtime[a.sid].conversation
    assert conv.last_stream is not None and conv.last_stream.stream["underruns"] >= 0
    assert all(c["audio_ms"] == 300.0 for c in conv.last_stream.stream["clauses"] if c["audio_ms"] is not None)    # 300 ms in, 300 ms out
    await ws.close()


async def test_a_worker_that_does_not_know_the_voice_ends_the_turn_with_an_error_not_with_silence_forever(pv):
    pv.s.ai_tts_reference_wav = "/nonexistent/reference.wav"
    await _cfg(pv, tts_ulaw8k=True, tts_voice="nobody")
    a, ws = await _call(pv, "ULAW-BADVOICE")
    pv.models.calls.clear()
    await send_media(ws, utterance())
    codes = await until(lambda: _turn_errors(pv, a.sid), timeout=10)
    assert codes == ["tts_worker_error"]                                                  # this session's feed shows WHY the turn made no audio
    assert (await ai_doc(pv, a.sid))["state"] == "active"                                  # and the call goes on
    await ws.close()


async def _turn_errors(pv, sid):
    import asyncio
    await asyncio.sleep(0.05)
    codes = [i["data"]["code"] for i in pv.app.state.activity.recent("t1", 500) if i["kind"] == "ai.turn_error" and i.get("session_id", sid) == sid]
    return codes or None
