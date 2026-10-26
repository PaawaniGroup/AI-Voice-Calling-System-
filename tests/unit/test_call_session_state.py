"""Unit tests for Voice AI Call Session State Machine and generation ID tracking."""
import pytest
from app.ai.service import CallSessionState, Runtime
from app.ai.pipeline import Conversation, TurnChunk


def test_call_session_state_enum_and_transitions():
    assert CallSessionState.IDLE == "IDLE"
    assert CallSessionState.LISTENING == "LISTENING"
    assert CallSessionState.TRANSCRIBING == "TRANSCRIBING"
    assert CallSessionState.THINKING == "THINKING"
    assert CallSessionState.GENERATING_TEXT == "GENERATING_TEXT"
    assert CallSessionState.GENERATING_AUDIO == "GENERATING_AUDIO"
    assert CallSessionState.SPEAKING == "SPEAKING"
    assert CallSessionState.INTERRUPTED == "INTERRUPTED"
    assert CallSessionState.ERROR == "ERROR"
    assert CallSessionState.ENDED == "ENDED"

    rt = Runtime(
        session_id="sess-123",
        tenant_id="tenant-abc",
        conversation=Conversation(clients=None),
        clients=None,
    )

    assert rt.call_state == CallSessionState.IDLE
    assert len(rt.state_history) == 0

    # Simulate call lifecycle state transitions
    rt.set_state(CallSessionState.LISTENING)
    assert rt.call_state == CallSessionState.LISTENING
    assert len(rt.state_history) == 1
    assert rt.state_history[-1]["state"] == CallSessionState.LISTENING

    rt.set_state(CallSessionState.TRANSCRIBING)
    assert rt.call_state == CallSessionState.TRANSCRIBING

    rt.set_state(CallSessionState.THINKING, {"generation_id": 1})
    assert rt.call_state == CallSessionState.THINKING
    assert rt.state_history[-1]["details"]["generation_id"] == 1

    rt.set_state(CallSessionState.GENERATING_TEXT, {"generation_id": 1})
    assert rt.call_state == CallSessionState.GENERATING_TEXT

    rt.set_state(CallSessionState.GENERATING_AUDIO, {"generation_id": 1})
    assert rt.call_state == CallSessionState.GENERATING_AUDIO

    rt.set_state(CallSessionState.SPEAKING, {"duration_s": 2.5})
    assert rt.call_state == CallSessionState.SPEAKING
    assert rt.state_history[-1]["details"]["duration_s"] == 2.5

    # Caller interrupts assistant playback
    rt.set_state(CallSessionState.INTERRUPTED, {"speech_ms": 250, "rms": 1200.0})
    assert rt.call_state == CallSessionState.INTERRUPTED
    assert rt.state_history[-1]["details"]["speech_ms"] == 250

    # Call teardown
    rt.set_state(CallSessionState.ENDED)
    assert rt.call_state == CallSessionState.ENDED


def test_state_history_bounding():
    rt = Runtime(
        session_id="sess-bound",
        tenant_id="tenant-abc",
        conversation=Conversation(clients=None),
        clients=None,
    )
    for i in range(150):
        rt.set_state(CallSessionState.LISTENING if i % 2 == 0 else CallSessionState.SPEAKING)
    assert len(rt.state_history) <= 100


def test_generation_id_increment_on_cancellation():
    conv = Conversation(clients=None)
    assert conv.generation_id == 0
    assert conv.cancelled is False

    conv.cancel()
    assert conv.cancelled is True
    assert conv.generation_id == 1

    conv.cancel()
    assert conv.generation_id == 2
