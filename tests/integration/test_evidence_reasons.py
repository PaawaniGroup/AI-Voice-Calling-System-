"""The evidence line says WHY a turn ended and WHY an utterance ended, on the real service stack (routes, Plivo stream handler, MongoDB, serve_loop) with MOCK Plivo and MOCK models.
SIMULATED: nothing here measures real latency. Numbers and enumerated labels only; the privacy check reads the whole evidence file."""
import asyncio
import json

from tests.helpers import pcm_silence, pcm_tone
from tests.integration.test_plivo_voice import RATE, pv, recv_until, send_media, until, utterance  # noqa: F401  (pv is a fixture defined there)
from tests.integration.test_streaming_live import CALLER_WORDS, REPLY, REPLY_WORDS, _call, _enable


def _lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


async def _setup(pv, monkeypatch, tmp_path, **models):
    f = tmp_path / "evidence.jsonl"
    monkeypatch.setitem(pv.ai.s.__dict__, "ai_trace_evidence_path", str(f))          # the service reads this setting when a call starts
    pv.models.brain_sse, pv.models.reply = True, REPLY
    for k, v in models.items():
        setattr(pv.models, k, v)
    await _enable(pv, trace_evidence=True)
    return f


async def test_a_barge_in_turn_has_its_metrics_the_cancel_reason_and_an_endpoint_reason(pv, monkeypatch, tmp_path):
    f = await _setup(pv, monkeypatch, tmp_path, tts_ms=4000)
    a, ws = await _call(pv, "EV-BARGE")
    await send_media(ws, utterance())
    await recv_until(ws, lambda m: m.get("event") == "playAudio", timeout=15)
    await asyncio.sleep(0.3)
    await send_media(ws, pcm_tone(500, RATE, amp=6000), first_chunk=500)
    await recv_until(ws, lambda m: m.get("event") == "clearAudio", timeout=10)

    async def got():
        return next((x for x in _lines(f) if x["turn"].get("cancel_reason") == "barge_in"), None)
    line = await until(got, timeout=10)
    assert line is not None
    assert line["turn"]["endpoint_reason"] == "silence" and line["turn"]["outcome"] == "interrupted" and line["turn"]["utterance_ms"] > 0 and line["turn"]["pieces_merged"] == 1
    assert line["stream"] and "interrupt" in line["stream"] and line["stream"]["interrupt"]["interrupt_to_last_frame_ms"] >= 0          # it was empty before
    assert "underruns" in line["stream"] and "gaps" in line["stream"] and "silence_ms" in line["stream"]
    await ws.close()


async def test_a_turn_merged_into_the_next_utterance_leaves_a_cancelled_before_audio_line_with_the_merged_reason(pv, monkeypatch, tmp_path):
    f = await _setup(pv, monkeypatch, tmp_path, tts_delay=3.0)
    a, ws = await _call(pv, "EV-MERGE")
    n = await send_media(ws, utterance(speech_ms=600, silence_ms=800))
    await asyncio.sleep(0.5)
    pv.models.tts_delay = 0.0
    n = await send_media(ws, utterance(speech_ms=600, silence_ms=800), first_chunk=n)
    await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=20)

    async def got():
        ls = _lines(f)
        return ls if any(x["turn"].get("cancel_reason") == "merged" for x in ls) and any(x["marks_ms"].get("t_first_frame_to_plivo") is not None for x in ls) else None
    ls = await until(got, timeout=10)
    assert ls is not None
    merged = next(x for x in ls if x["turn"].get("cancel_reason") == "merged")
    assert merged["turn"]["outcome"] == "cancelled_before_audio" and merged["marks_ms"].get("t_first_frame_to_plivo") is None
    final = next(x for x in ls if x["marks_ms"].get("t_first_frame_to_plivo") is not None)
    assert final["turn"]["pieces_merged"] == 2 and final["turn"]["utterance_ms"] > merged["turn"]["utterance_ms"]          # the second line carries the merged audio
    await ws.close()


async def test_a_hangup_under_a_running_turn_is_labelled_hangup(pv, monkeypatch, tmp_path):
    f = await _setup(pv, monkeypatch, tmp_path, tts_delay=5.0)
    a, ws = await _call(pv, "EV-HANGUP")
    await send_media(ws, utterance())
    await asyncio.sleep(0.8)                                                        # the turn is running, no audio yet
    await ws.close()

    async def got():
        return next((x for x in _lines(f) if x["turn"].get("cancel_reason") == "hangup"), None)
    line = await until(got, timeout=10)
    assert line is not None and line["turn"]["outcome"] in ("cancelled_before_audio", "not_recorded", "interrupted", "cancelled")


async def test_a_hangup_before_any_audio_is_labelled_cancelled_before_audio(pv, monkeypatch, tmp_path):
    """The three cases of the outcome label: (1) audio sent then cancelled = interrupted (barge-in), (2) cancelled before audio, merged = test above, (3) cancelled before audio, hung up = here."""
    f = await _setup(pv, monkeypatch, tmp_path, tts_delay=5.0)
    a, ws = await _call(pv, "EV-HANGUP2")
    await send_media(ws, utterance())
    await asyncio.sleep(1.5)                                                        # STT and Brain are done, the voice is still working: no answer audio yet
    await ws.close()

    async def got():
        return next((x for x in _lines(f) if x["turn"].get("cancel_reason") == "hangup"), None)
    line = await until(got, timeout=10)
    assert line is not None and line["turn"]["outcome"] == "cancelled_before_audio" and line["marks_ms"].get("t_first_frame_to_plivo") is None


async def test_an_utterance_longer_than_the_cap_is_labelled_cap_not_silence(pv, monkeypatch, tmp_path):
    f = await _setup(pv, monkeypatch, tmp_path, tts_ms=300)
    a, ws = await _call(pv, "EV-CAP")
    await send_media(ws, pcm_tone(6600, RATE, amp=6000) + pcm_silence(800, RATE))   # speech past the 6 s cap, then the usual silence

    async def got():
        return [x for x in _lines(f) if x["turn"].get("endpoint_reason") == "cap"] or None
    assert await until(got, timeout=15)
    await ws.close()


async def test_the_whole_evidence_file_holds_no_caller_or_reply_words(pv, monkeypatch, tmp_path):
    f = await _setup(pv, monkeypatch, tmp_path, tts_ms=300)
    a, ws = await _call(pv, "EV-PRIVACY")
    await send_media(ws, utterance())
    await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=15)
    await until(lambda: _async(len(_lines(f)) >= 1), timeout=10)
    text = f.read_text(encoding="utf-8")
    for w in (*CALLER_WORDS, *REPLY_WORDS):
        assert w not in text, w
    await ws.close()


async def _async(v):
    return v
