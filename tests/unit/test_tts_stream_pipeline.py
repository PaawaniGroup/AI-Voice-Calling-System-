"""Conversation(tts_stream=True): audio goes to playback chunk by chunk while the worker is still generating the clause. FAKE clients (no GPU/network)."""
import asyncio
import time

from app.ai import pipeline
from app.ai.clients import AiError, TtsAudioChunk
from app.ai.pipeline import Conversation
from tests.unit.test_tts_overlap import SENTENCES, FakeClients


class StreamingClients(FakeClients):
    def __init__(self, n_chunks=4, gap=0.1, fail_at=None):
        super().__init__()
        self.n_chunks, self.gap, self.fail_at = n_chunks, gap, fail_at
        self.stream_calls, self.closed, self.produced, self.clause_done_at = 0, [], 0, {}

    async def synthesize_stream(self, text, language):
        self.stream_calls += 1
        try:
            for i in range(self.n_chunks):
                if self.gap:
                    await asyncio.sleep(self.gap)
                if self.fail_at == i:
                    raise AiError("tts", "unreachable")
                self.produced += 1
                yield TtsAudioChunk(i, (text[:8] + f"#{i}").encode(), 8000, False)
            yield TtsAudioChunk(self.n_chunks, b"", 8000, True)       # empty final marker: must not reach playback
            self.clause_done_at[text] = time.monotonic()
        finally:
            self.closed.append(text)


def conv(fake, **kw):
    return Conversation(fake, "hi", tts_stream=True, **kw)


async def test_first_chunk_reaches_playback_before_the_clause_is_finished():
    fake = StreamingClients(n_chunks=4, gap=0.1)
    c = conv(fake)
    first_at = {}
    async for ch in c.stream_turn(b"\0" * 640, 16000):
        if ch.audio and ch.sentence not in first_at:
            first_at[ch.sentence] = time.monotonic()
    s0 = SENTENCES[0].strip()
    assert first_at[s0] < fake.clause_done_at[s0] - 0.25     # delivered ~0.1 s in; the clause needed ~0.4 s


async def test_chunks_and_clauses_stay_in_order_with_overlap_on():
    fake = StreamingClients(n_chunks=3, gap=0.02)
    c = conv(fake, tts_overlap=True)
    audio = [ch.audio async for ch in c.stream_turn(b"\0" * 640, 16000) if ch.audio]
    expected = [(s.strip()[:8] + f"#{i}").encode() for s in SENTENCES for i in range(3)]
    assert audio == expected                                   # clause 1 fully, then clause 2 ...; chunks 0,1,2 within each
    assert all(a for a in audio)                               # the empty final markers were not played


async def test_barge_in_mid_clause_closes_the_stream_and_plays_nothing_more():
    fake = StreamingClients(n_chunks=6, gap=0.05)
    c = conv(fake)
    got = []
    async for ch in c.stream_turn(b"\0" * 640, 16000):
        if ch.audio:
            got.append(ch)
            c.cancel()
    await asyncio.sleep(0.05)
    assert len(got) == 1                                       # exactly the chunk that was already in flight
    assert fake.closed and all(t in [s.strip() for s in SENTENCES] for t in fake.closed)   # the generator (= remote job cancel) was closed
    assert fake.in_flight == 0


async def test_error_in_the_middle_of_a_clause_ends_the_turn_with_a_tts_error():
    fake = StreamingClients(n_chunks=4, gap=0.01, fail_at=2)
    c = conv(fake)
    chunks = [ch async for ch in c.stream_turn(b"\0" * 640, 16000)]
    assert chunks[-1].error == "tts_unreachable"
    assert [m.get("role") for m in c.messages] == ["system", "user"]   # caller text kept, no assistant reply committed
    await asyncio.sleep(0.05)
    assert fake.closed                                         # nothing left streaming


async def test_slow_playback_applies_backpressure_to_the_worker_poll():
    fake = StreamingClients(n_chunks=200, gap=0)
    c = conv(fake)
    consumed, worst_lead = 0, 0
    async for ch in c.stream_turn(b"\0" * 640, 16000):
        if ch.audio:
            consumed += 1
            await asyncio.sleep(0.002)                         # playback is slower than generation
            worst_lead = max(worst_lead, fake.produced - consumed)
        if consumed >= 40:
            c.cancel()
    # the worker may run ahead of playback by at most the bounded queue per active clause (+ items in transit), never by 200 chunks
    assert worst_lead <= (pipeline.TTS_STREAM_QUEUE + 3) * pipeline.TTS_PREFETCH


async def test_off_by_default_the_non_streaming_call_is_used():
    fake = StreamingClients()
    c = Conversation(fake, "hi")                               # tts_stream not set
    chunks = [ch async for ch in c.stream_turn(b"\0" * 640, 16000)]
    assert fake.stream_calls == 0 and [ch.sentence for ch in chunks if ch.audio] == [s.strip() for s in SENTENCES]


async def test_a_client_without_synthesize_stream_falls_back_to_one_chunk_per_clause():
    fake = FakeClients(synth_delay=0.01)                       # no synthesize_stream attribute
    c = conv(fake)
    chunks = [ch async for ch in c.stream_turn(b"\0" * 640, 16000)]
    assert [ch.sentence for ch in chunks if ch.audio] == [s.strip() for s in SENTENCES]
