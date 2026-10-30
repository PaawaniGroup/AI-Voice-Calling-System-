"""PlivoStreamSink: batching, immediate flush when the playout pauses or ends, clear-once, checkpoint. And the playout's use of `flush`."""
import array
import asyncio
import base64
import json
import math

from app.ai.streaming.playout import PlayoutConfig, TurnPlayout
from app.ai.streaming.plivo_sink import PlivoStreamSink
from tests.fakeclock import FakeClock

RATE = 8000


def tone(ms):
    n = RATE * ms // 1000
    return array.array("h", [int(8000 * math.sin(2 * math.pi * 300 * i / RATE)) for i in range(n)]).tobytes()


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send_text(self, s):
        self.sent.append(json.loads(s))


class FakeIO:
    def __init__(self):
        self.ws, self.stream_id, self.sent_checkpoints, self.interrupts = FakeWs(), "S1", [], 0

    async def interrupt(self):
        self.interrupts += 1
        await self.ws.send_text(json.dumps({"event": "clearAudio", "streamId": self.stream_id}))


FRAME = b"\x01\x02" * 160


async def test_frames_are_batched_to_100ms_and_each_message_is_valid_playaudio():
    io = FakeIO()
    sink = PlivoStreamSink(io, batch_ms=100)
    for _ in range(12):
        await sink.send(1, FRAME)
    plays = [m for m in io.ws.sent if m["event"] == "playAudio"]
    assert len(plays) == 2 and sink.frames == 12                                   # 10 frames written (2 x 100 ms), 2 held for the next batch
    for m in plays:
        assert m["media"]["contentType"] == "audio/x-mulaw" and m["media"]["sampleRate"] == 8000
        assert len(base64.b64decode(m["media"]["payload"])) == 800
    await sink.flush()
    assert len([m for m in io.ws.sent if m["event"] == "playAudio"]) == 3         # the rest goes out on flush


async def test_the_audio_callback_reports_every_frame_so_the_playing_window_follows_the_stream():
    seen = []
    sink = PlivoStreamSink(FakeIO(), on_audio=seen.append)
    for _ in range(3):
        await sink.send(1, FRAME)
    assert seen == [0.02, 0.02, 0.02]


async def test_clear_drops_unwritten_audio_and_is_sent_only_once_per_audio():
    io = FakeIO()
    sink = PlivoStreamSink(io, batch_ms=100)
    for _ in range(3):
        await sink.send(1, FRAME)                                                  # 60 ms: still in the buffer
    await sink.clear(1)
    await sink.clear(1)                                                            # a second clear with nothing in between does nothing
    assert io.interrupts == 1 and not sink.buf
    await sink.send(2, FRAME)
    await sink.clear(2)
    assert io.interrupts == 2
    assert [m["event"] for m in io.ws.sent] == ["clearAudio", "clearAudio"]        # nothing was written between: the dropped audio never reached Plivo


async def test_finish_flushes_and_sends_the_checkpoint_in_the_same_form_as_the_old_path():
    io = FakeIO()
    sink = PlivoStreamSink(io, batch_ms=100)
    await sink.send(1, FRAME)
    await sink.finish(7)
    assert [m["event"] for m in io.ws.sent] == ["playAudio", "checkpoint"]
    assert io.ws.sent[-1] == {"event": "checkpoint", "streamId": "S1", "name": "utt-7"} and io.sent_checkpoints == ["utt-7"]


async def test_the_playout_flushes_the_sink_when_it_waits_for_a_late_clause_and_when_it_ends():
    clock = FakeClock()
    flushes = []

    class Sink:
        frames = 0

        async def send(self, gen, frame):
            Sink.frames += 1

        async def flush(self):
            flushes.append((clock.elapsed(), Sink.frames))

    async def scenario():
        po = TurnPlayout(Sink(), clock, 1, PlayoutConfig())
        task = asyncio.ensure_future(po.run())
        po.enqueue(1, 0, tone(300), words=3)
        await clock.sleep(2.0)                                                     # a long underrun
        po.enqueue(1, 1, tone(300), words=3)
        po.end_turn(1)
        return await task

    rep = await clock.run(scenario())
    assert rep["underruns"] == 1 and len(flushes) >= 2
    assert flushes[0][1] == 15 and flushes[0][0] < 0.5                             # flushed as soon as the first clause was out, long before the gap ended
    assert flushes[-1][1] == Sink.frames                                           # and again at the end: nothing is left in the sink's buffer
