"""Task cancellation must propagate out of Conversation.stream_turn.
It used to be swallowed (the generator yielded a "cancelled" chunk instead), so a worker cancelled mid-STT came back to life, the
media socket handler waited for it forever, and every call hung up mid-turn leaked a handler. Model services are MOCKS."""
import asyncio

import pytest

from app.ai.clients import ModelClients
from app.ai.pipeline import Conversation
from tests.conftest import make_settings
from tests.helpers import MockModels, pcm_tone


def _conv(models: MockModels) -> tuple[Conversation, ModelClients]:
    s = make_settings(ai_allowed_hosts="stt.test,brain.test,tts.test")
    cfg = {"stt_url": "http://stt.test:9000", "brain_url": "http://brain.test:9000", "tts_url": "http://tts.test:9000"}
    clients = ModelClients(s, cfg, models.transport())
    return Conversation(clients, "hi"), clients


async def _consume(conv: Conversation) -> list:
    return [c async for c in conv.stream_turn(pcm_tone(400), 16000)]


async def test_cancelling_the_task_mid_stt_raises_cancelled_and_rolls_back_history():
    m = MockModels()
    m.stt_delay = 5.0
    conv, clients = _conv(m)
    task = asyncio.create_task(_consume(conv))
    await asyncio.sleep(0.3)             # STT request is in flight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)  # not swallowed: the task ends within the bound instead of waiting for STT
    assert task.cancelled()
    assert [x["role"] for x in conv.messages] == ["system"]   # the half-done turn left no user message behind
    await clients.aclose()


async def test_flag_cancel_still_ends_the_turn_with_a_cancelled_chunk_and_no_audio():
    """Barge-in uses Conversation.cancel() (a flag), not task cancellation: that path must keep yielding `cancelled`."""
    m = MockModels()
    conv, clients = _conv(m)
    got = []

    async def run():
        async for c in conv.stream_turn(pcm_tone(400), 16000):
            got.append(c)
            conv.cancel()                # caller talks over the assistant as soon as the first thing arrives
    await asyncio.wait_for(run(), 5)
    assert not [c for c in got if c.audio][1:]       # at most the chunk that was already in flight; nothing after cancel()
    assert all(c.error in (None, "cancelled") for c in got)
    # the conversation is reusable afterwards (the worker keeps running between turns)
    again = await asyncio.wait_for(_consume(conv), 5)
    assert any(c.audio for c in again)
    await clients.aclose()
