"""LLM reading must overlap TTS: a slow synthesis may not stall token consumption, audio stays in sentence order, the number of
in-flight syntheses is bounded, and a barge-in / failure cancels the ones still running. Model services are FAKES (no GPU/network)."""
import asyncio
import time

from app.ai import pipeline
from app.ai.clients import AiError
from app.ai.pipeline import Conversation

SENTENCES = ["Namaste, aap kaise hain?", "Main aapki madad karungi.", "Kripya apna sawaal batayein.", "Dhanyavaad aapka."]


class FakeClients:
    cfg: dict = {}

    def __init__(self, synth_delay: float = 0.2, fail_on: int | None = None):
        self.synth_delay = synth_delay
        self.fail_on = fail_on
        self.token_times: list[float] = []
        self.synth_started: list[str] = []
        self.synth_cancelled: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def transcribe(self, pcm, rate, language):
        return {"text": "hello", "language": "hi"}

    async def chat_stream(self, messages, tools=None, temperature=None):
        for s in SENTENCES:
            for tok in (s + " ").split(" "):
                if tok:
                    await asyncio.sleep(0.005)
                    self.token_times.append(time.monotonic())
                    yield {"delta": tok + " "}
        yield {"done": True, "text": " ".join(SENTENCES)}

    async def synthesize(self, text, language):
        n = len(self.synth_started)
        self.synth_started.append(text)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.synth_delay)
            if self.fail_on == n:
                raise AiError("tts", "unreachable")
            return text.encode(), 16000
        except asyncio.CancelledError:
            self.synth_cancelled.append(text)
            raise
        finally:
            self.in_flight -= 1


def _conv(fake: FakeClients, overlap: bool = True) -> Conversation:
    return Conversation(fake, "hi", tts_overlap=overlap)


async def test_default_is_serial_and_unchanged():
    """Without the opt-in a clause is synthesized to completion before the next token is read (the original behaviour)."""
    fake = FakeClients(synth_delay=0.1)
    conv = _conv(fake, overlap=False)
    chunks = [c async for c in conv.stream_turn(b"\0" * 640, 16000)]
    assert fake.max_in_flight == 1
    assert max(b - a for a, b in zip(fake.token_times, fake.token_times[1:])) >= 0.1   # the LLM waited for TTS
    assert [c.sentence for c in chunks if c.audio] == [s.strip() for s in SENTENCES]


async def test_llm_tokens_keep_flowing_while_tts_runs():
    fake = FakeClients(synth_delay=0.3)
    conv = _conv(fake)
    chunks = [c async for c in conv.stream_turn(b"\0" * 640, 16000)]
    # Serial code would put >= 0.3 s between the token groups of sentence 1 and sentence 2; overlapped, every token arrives within
    # a few ms of the previous one except for the explicit sleeps.
    gaps = [b - a for a, b in zip(fake.token_times, fake.token_times[1:])]
    assert max(gaps) < 0.15
    assert [c.sentence for c in chunks if c.audio] == [s.strip() for s in SENTENCES]   # strict order, nothing lost


async def test_prefetch_is_bounded():
    fake = FakeClients(synth_delay=0.2)
    conv = _conv(fake)
    _ = [c async for c in conv.stream_turn(b"\0" * 640, 16000)]
    assert fake.max_in_flight == pipeline.TTS_PREFETCH   # 4 sentences queue up, yet only TTS_PREFETCH run at once


async def test_failure_cancels_syntheses_still_in_flight_and_reports_the_error():
    fake = FakeClients(synth_delay=0.2, fail_on=0)
    conv = _conv(fake)
    chunks = [c async for c in conv.stream_turn(b"\0" * 640, 16000)]
    assert chunks[-1].error == "tts_unreachable"
    await asyncio.sleep(0.05)
    assert fake.in_flight == 0                      # nothing left running after the turn ended
    assert [m.get("role") for m in conv.messages] == ["system", "user"]   # caller text kept, no assistant reply committed


async def test_flag_cancel_stops_pending_synthesis():
    fake = FakeClients(synth_delay=0.5)
    conv = _conv(fake)
    got = []
    async for c in conv.stream_turn(b"\0" * 640, 16000):
        got.append(c)
        conv.cancel()
    await asyncio.sleep(0.05)
    assert fake.in_flight == 0
    assert not [c for c in got if c.audio][1:]
