"""Full-duplex input (per-tenant flag `full_duplex_input`, default OFF) on the REAL service stack: Plivo stream handler, serve_loop, MongoDB, AiService, with MOCK Plivo
and MOCK models. The caller's audio is synthetic tones: what is verified is the decision logic and what reaches speech-to-text, NOT how it sounds or how real
speakerphone echo behaves (that needs recordings)."""
import asyncio
import json
import time

import pytest

from tests.conftest import auth
from tests.helpers import pcm_silence, pcm_tone
from tests.integration.test_plivo_voice import (RATE, ai_doc, answered_stream, connect, pv, recv_until, send_media, until, utterance)  # noqa: F401

ADMIN = lambda: auth("t1", ("telephony:admin",))  # noqa: E731
LONG = 3500           # ms of assistant audio per reply (the mock TTS tone has an RMS of about 4240)


async def _cfg(pv, **extra):
    r = await pv.c.put("/v1/ai/config", json=extra, headers=ADMIN())
    assert r.status_code == 200, r.text


async def _call(pv, uuid):
    a = await answered_stream(pv, call_uuid=uuid)
    ws = await connect(pv, a)

    async def active():
        d = await ai_doc(pv, a.sid)
        return d if d and d["state"] == "active" else None
    await until(active)
    return a, ws


async def _clear_within(ws, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            m = json.loads(await asyncio.wait_for(ws.recv(), max(0.05, end - time.monotonic())))
        except asyncio.TimeoutError:
            return False
        if m.get("event") == "clearAudio":
            return True
    return False


async def _interrupt_attempt(pv, uuid, cfg, amp, ms):
    """The assistant starts a long reply; the caller then makes a `ms` ms sound of amplitude `amp`. True = the assistant was interrupted."""
    pv.models.tts_ms = LONG
    await _cfg(pv, **cfg)
    a, ws = await _call(pv, uuid)
    n = await send_media(ws, utterance())
    await recv_until(ws, lambda m: m.get("event") == "playAudio", timeout=15)
    await asyncio.sleep(0.2)
    await send_media(ws, pcm_tone(ms, RATE, amp=amp), first_chunk=n)
    got = await _clear_within(ws, 1.5)
    await ws.close()
    return got


# ------------------------------------------------------------------ the setting
async def test_default_is_off_and_the_settings_are_validated_and_stored_per_tenant(pv):
    cfg = (await pv.c.get("/v1/ai/config", headers=ADMIN())).json()
    assert cfg["full_duplex_input"] is False and cfg["duplex_min_voiced_ms"] is None and cfg["duplex_echo_ratio"] is None
    for bad in ({"duplex_min_voiced_ms": 50}, {"duplex_min_voiced_ms": 5000}, {"duplex_echo_ratio": 3.0}, {"duplex_dropout_ms": 900},
                {"duplex_resume_ms": 400}, {"duplex_pre_roll_ms": 2000}, {"full_duplex_input": "maybe"}):
        assert (await pv.c.put("/v1/ai/config", json=bad, headers=ADMIN())).status_code == 422, bad
    await _cfg(pv, full_duplex_input=True, duplex_min_voiced_ms=300, duplex_echo_ratio=0.0)
    got = (await pv.c.get("/v1/ai/config", headers=ADMIN())).json()
    assert got["full_duplex_input"] is True and got["duplex_min_voiced_ms"] == 300 and got["duplex_echo_ratio"] == 0.0
    assert (await pv.ai.get_config("t2"))["full_duplex_input"] is False                       # another tenant is unaffected
    a, ws = await _call(pv, "DX-CFG")
    d = pv.ai.runtime[a.sid].conversation.duplex
    assert (d.min_voiced_ms, d.echo_ratio, d.dropout_ms, d.pre_roll_ms) == (300, 0.0, 100, 300)    # tenant values + provisional defaults
    await ws.close()


async def test_with_the_flag_off_the_conversation_has_no_duplex_settings_and_the_old_rule_runs(pv):
    a, ws = await _call(pv, "DX-OFF")
    assert pv.ai.runtime[a.sid].conversation.duplex is None
    await ws.close()


# ------------------------------------------------------------------ a short sound is not an interruption
async def test_a_220_ms_sound_interrupts_the_old_rule_but_not_full_duplex(pv):
    assert await _interrupt_attempt(pv, "DX-BC-OLD", {}, amp=6000, ms=220) is True              # the old rule fires at 200 ms


async def test_full_duplex_ignores_a_220_ms_sound(pv):
    assert await _interrupt_attempt(pv, "DX-BC-NEW", {"full_duplex_input": True}, amp=6000, ms=220) is False


async def test_full_duplex_still_interrupts_for_real_speech(pv):
    assert await _interrupt_attempt(pv, "DX-REAL", {"full_duplex_input": True}, amp=6000, ms=500) is True


async def test_the_minimum_voiced_time_is_configurable(pv):
    assert await _interrupt_attempt(pv, "DX-MIN", {"full_duplex_input": True, "duplex_min_voiced_ms": 600}, amp=6000, ms=500) is False


# ------------------------------------------------------------------ speaker echo
async def test_echo_quieter_than_the_assistant_does_not_interrupt_with_the_guard(pv):
    # the assistant's tone has an RMS of ~4240; this caller-side sound has ~1410 (above the 1000 floor, below 0.8 x 4240 = 3390)
    assert await _interrupt_attempt(pv, "DX-ECHO-ON", {"full_duplex_input": True}, amp=2000, ms=800) is False


async def test_the_same_sound_interrupts_when_the_guard_is_off(pv):
    assert await _interrupt_attempt(pv, "DX-ECHO-OFF", {"full_duplex_input": True, "duplex_echo_ratio": 0.0}, amp=2000, ms=800) is True


async def test_the_same_sound_interrupts_the_old_rule(pv):
    assert await _interrupt_attempt(pv, "DX-ECHO-OLD", {}, amp=2000, ms=800) is True


async def test_a_caller_louder_than_the_echo_level_interrupts_even_with_the_guard(pv):
    assert await _interrupt_attempt(pv, "DX-LOUD", {"full_duplex_input": True}, amp=8000, ms=500) is True


# ------------------------------------------------------------------ the guard follows the streamed audio too
async def test_the_guard_works_with_the_streaming_pipeline_and_a_real_interruption_still_cancels_the_turn(pv):
    pv.models.brain_sse = True
    pv.models.reply = "जी, मैं आपको admission की पूरी जानकारी देता हूँ। B.Tech चार साल का course है, और admission entrance exam से होता है।"
    assert await _interrupt_attempt(pv, "DX-STREAM-ECHO", {"full_duplex_input": True, "streaming_pipeline": True}, amp=2000, ms=800) is False
    assert await _interrupt_attempt(pv, "DX-STREAM-REAL", {"full_duplex_input": True, "streaming_pipeline": True}, amp=8000, ms=500) is True


# ------------------------------------------------------------------ what reaches speech-to-text
async def _stt_sizes_after_barge_in(pv, uuid, cfg):
    pv.models.tts_ms = LONG
    await _cfg(pv, **cfg)
    a, ws = await _call(pv, uuid)
    n = await send_media(ws, utterance())
    await recv_until(ws, lambda m: m.get("event") == "playAudio", timeout=15)
    await asyncio.sleep(0.2)
    before = len(pv.models.stt_uploads)
    n = await send_media(ws, pcm_tone(600, RATE, amp=8000) + pcm_silence(1000, RATE), first_chunk=n)    # 600 ms of speech over the assistant, then silence
    assert await until(lambda: _upload_after(pv, before), timeout=10)
    size = pv.models.stt_uploads[before][0]
    await ws.close()
    return size


async def _upload_after(pv, before):
    return len(pv.models.stt_uploads) > before


async def test_the_speech_that_triggered_the_interruption_is_kept_for_speech_to_text(pv):
    old = await _stt_sizes_after_barge_in(pv, "DX-KEEP-OLD", {"duplex_pre_roll_ms": None})
    new = await _stt_sizes_after_barge_in(pv, "DX-KEEP-NEW", {"full_duplex_input": True})
    extra = new - old
    # the old rule kept only the frame that crossed 200 ms (it lost the 9 frames before it = 180 ms = 2880 bytes at 8 kHz, 16-bit); full duplex keeps them all
    assert 2000 <= extra <= 4200, f"expected about 2880 bytes more audio, got {extra}"


async def _seconds_until_speech_to_text(pv, uuid, cfg):
    """Speech, then 400 ms of silence, a 40 ms click, 400 ms more silence. The old rule restarts its 700 ms silence timer at the click (the
    utterance ends ~700 ms after it); with hysteresis the click is ignored (the utterance ends 700 ms after the speech)."""
    await _cfg(pv, **cfg)
    a, ws = await _call(pv, uuid)
    before = len(pv.models.stt_uploads)
    pcm = pcm_tone(600, RATE, amp=6000) + pcm_silence(400, RATE) + pcm_tone(40, RATE, amp=6000) + pcm_silence(400, RATE)
    await send_media(ws, pcm)
    t0 = time.monotonic()
    assert await until(lambda: _upload_after(pv, before), timeout=10, step=0.02)
    took = time.monotonic() - t0
    await ws.close()
    return took


async def test_a_40_ms_click_does_not_postpone_the_end_of_the_utterance_with_hysteresis(pv):
    old = await _seconds_until_speech_to_text(pv, "DX-HYS-OLD", {})
    new = await _seconds_until_speech_to_text(pv, "DX-HYS-NEW", {"full_duplex_input": True})
    assert old - new >= 0.15, f"old {old:.2f}s, new {new:.2f}s: the click should postpone the old rule's end by about 0.3 s"
    off = await _seconds_until_speech_to_text(pv, "DX-HYS-ZERO", {"full_duplex_input": True, "duplex_resume_ms": 0})
    assert off - new >= 0.15                                                                   # resume_ms 0 = hysteresis off: the click postpones the end again


async def _upload_size_of_first_utterance(pv, uuid, cfg):
    await _cfg(pv, **cfg)
    a, ws = await _call(pv, uuid)
    before = len(pv.models.stt_uploads)
    await send_media(ws, utterance(600, 900))
    assert await until(lambda: _upload_after(pv, before), timeout=10)
    size = pv.models.stt_uploads[before][0]
    await ws.close()
    return size


async def test_pre_roll_puts_the_first_200_ms_of_speech_back_into_the_utterance(pv):
    old = await _upload_size_of_first_utterance(pv, "DX-PRE-OLD", {})
    new = await _upload_size_of_first_utterance(pv, "DX-PRE-NEW", {"full_duplex_input": True})
    assert 2000 <= new - old <= 4200, f"expected about 2880 bytes more audio, got {new - old}"
    zero = await _upload_size_of_first_utterance(pv, "DX-PRE-ZERO", {"full_duplex_input": True, "duplex_pre_roll_ms": 0})
    assert abs(zero - old) <= 400                                                              # pre-roll 0 = the old behaviour for the start of an utterance
