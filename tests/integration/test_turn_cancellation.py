"""A new utterance cancels the turn in progress instead of queueing behind it.

  * speech that CONTINUES within 1.5 s of the last utterance, before any assistant audio: the attempt is cancelled (its STT/Brain/TTS
    requests, and their RunPod jobs) and the next utterance is MERGED into it: ONE turn, ONE reply;
  * speech that starts later, still before any assistant audio: the new question SUPERSEDES the unanswered one;
  * the assistant is already speaking: BARGE-IN: the audio is cleared, the in-flight jobs are cancelled, the worker is free and the
    new turn starts at once (it used to wait for every remaining clause to be synthesised: 13 s to 33 s in the first live call).
Plivo and the models are MOCKS (the service, the media WebSocket and the conversation loop are real)."""
import asyncio
import json
import time

import pytest

from app.main import create_app  # noqa: F401  (keeps the import graph identical to the other Plivo tests)
from tests.helpers import pcm_silence, pcm_tone
from tests.integration.test_plivo_voice import (ADMIN, RATE, answered_stream, ai_doc, connect, pv, recv_until, send_media,  # noqa: F401
                                                until, utterance)

LONG_REPLY = "Namaste ji, aap kaise hain? BTech ke liye eligibility bata deta hoon. Fees bhi bata sakta hoon aapko."


async def _active(pv, sid):
    d = await ai_doc(pv, sid)
    return d if d and d["state"] == "active" else None


async def _call(pv, uuid):
    a = await answered_stream(pv, call_uuid=uuid)
    ws = await connect(pv, a)
    await until(lambda: _active(pv, a.sid))
    return a, ws


async def _drain_until_quiet(ws, quiet_s=0.8, timeout=15):
    """Every message until nothing arrives for `quiet_s` seconds: [(monotonic time, message)]."""
    out, end = [], time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            raw = await asyncio.wait_for(ws.recv(), quiet_s)
        except asyncio.TimeoutError:
            return out
        out.append((time.monotonic(), json.loads(raw)))
    raise AssertionError("the stream never went quiet")


def _events(msgs, name):
    return [(t, m) for t, m in msgs if m.get("event") == name]


async def _turn_count(pv, sid, expect, timeout=6):
    async def ready():
        d = await ai_doc(pv, sid)
        return d if d and len(d.get("transcript") or []) >= expect else None
    return await until(ready, timeout=timeout)


# ------------------------------------------------------------------ merge: the same question, continued within 1.5 s
async def test_speech_continued_within_1_5_s_is_merged_into_one_turn(pv):
    pv.models.stt_delay = 2.0                          # the first attempt is still in STT when the caller continues
    a, ws = await _call(pv, "MERGE-1")
    n = await send_media(ws, utterance(600, 800))      # first part of the question
    await asyncio.sleep(0.3)                           # the first STT request is in flight
    pv.models.stt_delay = 0.0
    await send_media(ws, utterance(500, 800), first_chunk=n)   # the caller carries on
    msgs = await _drain_until_quiet(ws)
    assert len(_events(msgs, "checkpoint")) == 1, "the merged question must get exactly ONE reply"
    assert pv.models.cancelled == ["stt"]              # the first attempt's request was cancelled, not left running
    kinds = [k for k, _ in pv.models.calls]
    assert kinds.count("stt") == 2 and kinds.count("brain") == 1 and kinds.count("tts") == 1
    first, second = (size for size, _ in pv.models.stt_uploads)
    assert second > first * 1.5, "the second STT request must carry BOTH parts of the question"
    doc = await _turn_count(pv, a.sid, 1)
    assert doc["turns"] == 1 and len(doc["transcript"]) == 1
    await ws.close()


async def test_a_merged_turn_can_be_extended_again(pv):
    pv.models.stt_delay = 2.0
    a, ws = await _call(pv, "MERGE-2")
    n = await send_media(ws, utterance(500, 800))
    await asyncio.sleep(0.3)
    n = await send_media(ws, utterance(500, 800), first_chunk=n)    # second part: cancels attempt 1
    await asyncio.sleep(0.3)
    pv.models.stt_delay = 0.0
    await send_media(ws, utterance(500, 800), first_chunk=n)        # third part: cancels attempt 2
    msgs = await _drain_until_quiet(ws)
    assert len(_events(msgs, "checkpoint")) == 1 and pv.models.cancelled == ["stt", "stt"]
    sizes = [size for size, _ in pv.models.stt_uploads]
    assert sizes[0] < sizes[1] < sizes[2], sizes                     # each attempt carries more of the question
    assert [k for k, _ in pv.models.calls].count("brain") == 1
    await ws.close()


# ------------------------------------------------------------------ supersede: a new question while the first is unanswered
async def test_a_later_question_before_any_audio_supersedes_the_unanswered_one(pv):
    pv.models.stt_delay = 5.0
    a, ws = await _call(pv, "SUPER-1")
    await send_media(ws, utterance(600, 800))
    await asyncio.sleep(2.2)                           # beyond the 1.5 s merge window, still no assistant audio
    pv.models.stt_delay = 0.0
    await send_media(ws, utterance(400, 800), first_chunk=200)
    msgs = await _drain_until_quiet(ws)
    assert len(_events(msgs, "checkpoint")) == 1 and pv.models.cancelled == ["stt"]
    first, second = (size for size, _ in pv.models.stt_uploads)
    assert second < first, "the new question is transcribed ALONE: the old audio was not merged"
    doc = await _turn_count(pv, a.sid, 1)
    assert doc["turns"] == 1
    await ws.close()


# ------------------------------------------------------------------ barge-in: the assistant is already speaking
async def test_barge_in_clears_audio_cancels_the_remaining_reply_and_frees_the_worker(pv, capsys):
    pv.models.reply, pv.models.tts_ms, pv.models.tts_delay = LONG_REPLY, 1500, 0.6      # 3 clauses, each 0.6 s to synthesise
    a, ws = await _call(pv, "BARGE-1")
    await send_media(ws, utterance(600, 800))
    await recv_until(ws, lambda m: m.get("event") == "playAudio", timeout=10)            # clause 1 is playing, clause 2 is being synthesised
    t_barge = time.monotonic()
    n = await send_media(ws, pcm_tone(400, RATE, amp=6000), first_chunk=1000)           # the caller talks over the assistant
    msgs = await _drain_until_quiet(ws, quiet_s=0.5)                                    # up to the clear (and the old turn's tail, if any)
    clears = _events(msgs, "clearAudio")
    assert clears, "no clearAudio was sent"
    t_clear = clears[0][0]
    assert pv.models.cancelled == ["tts"], "the clause being synthesised must be cancelled"
    old_audio_after_clear = [t for t, m in _events(msgs, "playAudio") if t > t_clear]
    assert old_audio_after_clear == [], "the interrupted reply kept sending audio after the clear"
    last_audio = max([t for t, m in _events(msgs, "playAudio")] or [t_barge])
    barge_to_clear_ms = (t_clear - t_barge) * 1000
    barge_to_last_audio_ms = (last_audio - t_barge) * 1000                              # <= 0: the last frame went out BEFORE the barge-in
    assert barge_to_clear_ms < 800 and last_audio <= t_clear
    # the worker is free: the interrupting utterance becomes the next turn and is answered without waiting for the old reply's clauses
    t_end = time.monotonic()
    await send_media(ws, pcm_silence(800, RATE), first_chunk=n)
    new = await recv_until(ws, lambda m: m.get("event") == "playAudio", timeout=10)
    new_first_audio_after_utterance_end_s = time.monotonic() - t_end
    assert new_first_audio_after_utterance_end_s < 1.5
    tail = await _drain_until_quiet(ws)
    kinds = [k for k, _ in pv.models.calls]
    assert kinds.count("stt") == 2 and kinds.count("brain") == 2 and kinds.count("tts") == 2 + 3   # old: clause 1 + the cancelled clause 2; new: 3 clauses
    doc = await _turn_count(pv, a.sid, 2)
    assert doc["state"] == "active"                                                      # the call goes on
    feed = [i for i in pv.app.state.activity.recent("t1", 500) if i["kind"] == "ai.turn_cancelled"]
    assert [f["data"]["reason"] for f in feed] == ["barge_in"] and feed[0]["data"]["unwind_ms"] < 500
    with capsys.disabled():
        print(f"\nBARGE-IN (mock models): barge-in -> clearAudio {barge_to_clear_ms:.0f} ms | barge-in -> last assistant audio frame {barge_to_last_audio_ms:.0f} ms "
              f"(<= 0 = nothing was sent after the barge-in) | cancel unwind {feed[0]['data']['unwind_ms']} ms | "
              f"new reply's first audio {new_first_audio_after_utterance_end_s * 1000:.0f} ms after the interrupting utterance ended")
    await ws.close()


async def test_barge_in_while_the_next_clause_is_still_being_made_also_stops_it(pv):
    """The first clause has already finished playing (nothing to clear) but the rest is being generated: new speech still stops it."""
    pv.models.reply, pv.models.tts_ms, pv.models.tts_delay = LONG_REPLY, 100, 0.8       # clause audio is short; synthesis is slow
    a, ws = await _call(pv, "BARGE-2")
    await send_media(ws, utterance(600, 800))
    await recv_until(ws, lambda m: m.get("event") == "playAudio", timeout=10)
    await asyncio.sleep(0.3)                                                             # the 100 ms of audio is over; clause 2 is in synthesis
    await send_media(ws, utterance(500, 800), first_chunk=1000)
    await _drain_until_quiet(ws, quiet_s=1.2)
    assert pv.models.cancelled == ["tts"]
    feed = [i["data"]["reason"] for i in pv.app.state.activity.recent("t1", 500) if i["kind"] == "ai.turn_cancelled"]
    assert feed == ["barge_in"]
    await ws.close()


# ------------------------------------------------------------------ no regression
async def test_sequential_turns_are_never_cancelled(pv):
    a, ws = await _call(pv, "SEQ-1")
    n = 1
    for _ in range(3):
        n = await send_media(ws, utterance(), first_chunk=n)
        await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=10)
        await asyncio.sleep(0.2)
    doc = await _turn_count(pv, a.sid, 3)
    assert doc["turns"] == 3 and pv.models.cancelled == []
    assert [i for i in pv.app.state.activity.recent("t1", 500) if i["kind"] == "ai.turn_cancelled"] == []
    await ws.close()


# ------------------------------------------------------------------ RunPod jobs are cancelled for real
async def _runpod_urls(pv):
    cfg = {"stt_url": "http://stt.test:9000/v2/ep/runsync", "brain_url": "http://brain.test:9000/v2/ep/runsync",
           "tts_url": "http://tts.test:9000/v2/ep/runsync", "rag_url": "http://rag.test:9000", "enabled": True}
    assert (await pv.c.put("/v1/ai/config", json=cfg, headers=ADMIN())).status_code == 200


async def test_barge_in_cancels_the_in_flight_runpod_job(pv):
    await _runpod_urls(pv)
    pv.models.reply, pv.models.tts_ms, pv.models.tts_delay = LONG_REPLY, 1500, 0.8
    a, ws = await _call(pv, "RPBARGE-1")
    await send_media(ws, utterance(600, 800))
    await recv_until(ws, lambda m: m.get("event") == "playAudio", timeout=10)
    await send_media(ws, pcm_tone(400, RATE, amp=6000), first_chunk=1000)
    await _drain_until_quiet(ws, quiet_s=0.5)
    await until(lambda: asyncio.sleep(0, result=bool(pv.models.cancel_calls)), timeout=3)
    assert pv.models.cancel_calls == ["tts"], "the TTS job being made for the interrupted reply must be cancelled on RunPod"
    assert sum(1 for j in pv.models.jobs.values() if j["cancelled"]) == 1
    await ws.close()


async def test_merge_cancels_the_first_attempts_stt_job_on_runpod(pv):
    await _runpod_urls(pv)
    a, ws = await _call(pv, "RPMERGE-1")
    await asyncio.sleep(0.6)                           # answering a call sends warm-up jobs to RunPod-style endpoints: let them finish
    pv.models.calls.clear()
    pv.models.cancel_calls.clear()
    pv.models.stt_delay = 3.0
    n = await send_media(ws, utterance(600, 800))
    await asyncio.sleep(0.5)
    pv.models.stt_delay = 0.0
    await send_media(ws, utterance(500, 800), first_chunk=n)
    msgs = await _drain_until_quiet(ws)
    assert len(_events(msgs, "checkpoint")) == 1
    assert pv.models.cancel_calls == ["stt"]
    kinds = [k for k, _ in pv.models.calls]
    assert kinds.count("stt") == 2 and kinds.count("brain") == 1
    await ws.close()
