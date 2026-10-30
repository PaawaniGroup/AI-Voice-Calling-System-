import asyncio
import logging

from app.ai import turn_trace
from app.ai.turn_trace import TurnTrace


def test_marks_are_relative_to_speech_end_first_write_wins():
    tr = TurnTrace(t_speech_end=100.0, t_endpoint=100.7, endpoint_silence_ms=700)
    tr.mark("t_stt_sent", at=100.8)
    tr.mark("t_stt_sent", at=105.0)               # ignored: first write wins
    tr.mark("t_llm_first_token", at=101.0)
    tr.mark("t_llm_first_token", overwrite=True, at=102.0)
    tr.mark("t_first_frame_to_plivo", at=103.5)
    ms = tr.to_ms()
    assert ms["t_speech_end"] == 0
    assert ms["t_endpoint"] == 700.0
    assert ms["t_stt_sent"] == 800.0
    assert ms["t_llm_first_token"] == 2000.0
    assert ms["voice_to_voice"] == ms["t_first_frame_to_plivo"] == 3500.0
    assert ms["endpoint_silence_ms"] == 700


def test_no_voice_to_voice_before_first_frame():
    tr = TurnTrace(t_speech_end=1.0)
    tr.mark("t_stt_sent", at=1.5)
    assert "voice_to_voice" not in tr.to_ms()


def test_runpod_delay_vs_execution_recorded_and_logged(caplog):
    tr = TurnTrace(t_speech_end=0.0)
    tok = turn_trace.start(tr)
    try:
        with caplog.at_level(logging.INFO, logger="mme_telephony.ai.trace"):
            turn_trace.note_runpod_response("tts", {"status": "COMPLETED", "delayTime": 7300, "executionTime": 4500})
            turn_trace.note_runpod_response("tts", {"delayTime": 10, "executionTime": 20})   # later clause: not the gating call
            turn_trace.note_runpod_response("stt", {"status": "COMPLETED"})                  # no timing fields: nothing invented
    finally:
        turn_trace._current.reset(tok)
    ms = tr.to_ms()
    assert ms["tts_delay_ms"] == 7300 and ms["tts_exec_ms"] == 4500
    assert "stt_delay_ms" not in ms
    assert any("service=tts delay_ms=7300" in r.getMessage() for r in caplog.records)


def test_helpers_are_noops_without_a_trace():
    assert turn_trace.current() is None
    turn_trace.mark("t_stt_sent")
    turn_trace.note_runpod_response("stt", {"delayTime": 1, "executionTime": 2})
    turn_trace.note_runpod_response("stt", "not a dict")


async def test_trace_is_isolated_per_task():
    a, b = TurnTrace(t_speech_end=0.0), TurnTrace(t_speech_end=0.0)

    async def run(tr, name):
        turn_trace.start(tr)
        await asyncio.sleep(0.01)
        turn_trace.mark(name)

    await asyncio.gather(asyncio.create_task(run(a, "t_stt_sent")), asyncio.create_task(run(b, "t_llm_sent")))
    assert "t_stt_sent" in a.marks and "t_llm_sent" not in a.marks
    assert "t_llm_sent" in b.marks and "t_stt_sent" not in b.marks
