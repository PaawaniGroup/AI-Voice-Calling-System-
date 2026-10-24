"""Does what the streaming sink and the 20 ms playout put on the Plivo socket match what Plivo's documentation allows? (Unit-test level: no Plivo, no network.)

Plivo's documentation, quoted (https://www.plivo.com/docs/voice-agents/audio-streaming/concepts/audio-streaming-guide, "Limits" table):
    "Maximum WebSocket message size"            64 KB
    "Recommended audio chunk size"              16 KB base64-encoded or less
    "Audio buffer size (playback queue)"        ~60 seconds of audio
  "Events to Plivo (Output)":  playAudio  "Send audio to the caller. Include base64 payload matching stream contentType."
                               checkpoint "Mark a point in audio queue. Receive `playedStream` when reached."
                               clearAudio "Clear all queued audio. Use for interruption handling."
  Quick Example (playAudio):   event: 'playAudio', media: { contentType: 'audio/x-mulaw', sampleRate: 8000, payload: base64EncodedAudio }
  "Events from Plivo (Input)": media "Sent continuously. Contains base64-encoded audio chunks (~20ms each)."
  "Supported Content Types":   "audio/x-mulaw;rate=8000" (Recommended), "audio/x-l16;rate=8000", "audio/x-l16;rate=16000"
Best practices (https://www.plivo.com/docs/voice/audio-streaming/best-practices, "WSS Socket Connection Failures"):
    "Plivo buffers audio packets for up to 40 seconds" ... DegradedStream when "the buffer - reaches 30%, 60%, or 90% capacity".

NOT STATED anywhere I could read: whether audio may be sent ahead of real time or must be paced, a maximum message RATE, and which of the 40 s / 60 s
figures applies to the outbound queue. Those three are therefore NOT claimed by this test; they are what the real-call test must check.
"""
import array
import asyncio
import base64
import json
import math

from app.ai.streaming.playout import PlayoutConfig, TurnPlayout
from app.ai.streaming.plivo_sink import PlivoStreamSink
from tests.fakeclock import FakeClock

RATE = 8000
DOC_MAX_MESSAGE_BYTES = 64 * 1024
DOC_RECOMMENDED_CHUNK_B64 = 16 * 1024
DOC_QUEUE_SECONDS = 60
DOC_QUICK_EXAMPLE_MEDIA_KEYS = {"contentType", "sampleRate", "payload"}


def tone(ms):
    n = RATE * ms // 1000
    return array.array("h", [int(8000 * math.sin(2 * math.pi * 300 * i / RATE)) for i in range(n)]).tobytes()


class Ws:
    def __init__(self, clock):
        self.clock, self.sent = clock, []

    async def send_text(self, s):
        self.sent.append((self.clock.now(), s))


class IO:
    def __init__(self, clock):
        self.ws, self.stream_id, self.sent_checkpoints = Ws(clock), "STREAM-1", []

    async def interrupt(self):
        await self.ws.send_text(json.dumps({"event": "clearAudio", "streamId": self.stream_id}))


async def run_reply(clock, clauses_ms, gaps_s=None, batch_ms=100, prebuffer_ms=150):
    io = IO(clock)
    sink = PlivoStreamSink(io, batch_ms=batch_ms)
    po = TurnPlayout(sink, clock, 1, PlayoutConfig(prebuffer_ms=prebuffer_ms))
    task = asyncio.ensure_future(po.run())
    for i, ms in enumerate(clauses_ms):
        po.enqueue(1, i, tone(ms), words=8)
        await clock.sleep((gaps_s or {}).get(i, ms / 1000 * 0.9))
    po.end_turn(1)
    rep = await task
    await sink.finish(1)
    return io, sink, po, rep


def messages(io):
    return [(t, s, json.loads(s)) for t, s in io.ws.sent]


async def test_every_message_is_within_the_documented_size_limits():
    clock = FakeClock()
    io, *_ = await clock.run(run_reply(clock, [4000, 5200, 3000, 6000, 2500, 7000]))
    for _, raw, m in messages(io):
        assert len(raw.encode()) <= DOC_MAX_MESSAGE_BYTES                       # "Maximum WebSocket message size: 64 KB"
        if m["event"] == "playAudio":
            assert len(m["media"]["payload"]) <= DOC_RECOMMENDED_CHUNK_B64       # "16 KB base64-encoded or less"
    biggest = max(len(m["media"]["payload"]) for _, _, m in messages(io) if m["event"] == "playAudio")
    assert biggest <= 1400                                                       # 100 ms of mu-law is 800 bytes = 1068 base64 chars: ~13x under the guidance


async def test_message_shapes_match_the_documented_events():
    clock = FakeClock()
    io, *_ = await clock.run(run_reply(clock, [1500, 1500]))
    ms = [m for _, _, m in messages(io)]
    plays = [m for m in ms if m["event"] == "playAudio"]
    assert plays and all(set(m) == {"event", "media"} and set(m["media"]) == DOC_QUICK_EXAMPLE_MEDIA_KEYS for m in plays)
    assert all(m["media"]["contentType"] == "audio/x-mulaw" and m["media"]["sampleRate"] == 8000 for m in plays)      # the Quick Example's values
    assert all(len(base64.b64decode(m["media"]["payload"])) % 160 == 0 for m in plays)                                # whole 20 ms mu-law frames
    cps = [m for m in ms if m["event"] == "checkpoint"]
    assert cps == [{"event": "checkpoint", "streamId": "STREAM-1", "name": "utt-1"}]


async def test_clear_audio_message_carries_the_stream_id():
    clock = FakeClock()
    io = IO(clock)
    sink = PlivoStreamSink(io)
    await sink.send(1, b"\x01\x02" * 160)
    await sink.clear(1)
    assert json.loads(io.ws.sent[-1][1]) == {"event": "clearAudio", "streamId": "STREAM-1"}


async def test_the_queue_we_build_at_plivo_never_approaches_the_documented_60_seconds():
    clock = FakeClock()
    io, sink, po, rep = await clock.run(run_reply(clock, [4000] * 12, gaps_s={i: 0.2 for i in range(12)}))   # a worst case: 48 s of audio handed over in 2.4 s
    sends = [(t, len(base64.b64decode(m["media"]["payload"]))) for t, _, m in messages(io) if m["event"] == "playAudio"]
    t0, queued_peak, total = sends[0][0], 0.0, 0
    for t, n in sends:
        total += n
        queued_peak = max(queued_peak, total / RATE - (t - t0))                  # seconds of audio at Plivo not yet played (it plays in real time)
    assert queued_peak <= 0.4                                                    # pacing keeps the lead at the configured pre-buffer, not tens of seconds
    assert queued_peak < DOC_QUEUE_SECONDS / 100


async def test_pacing_matches_the_20ms_unit_and_the_message_rate_stays_low():
    clock = FakeClock()
    io, sink, po, rep = await clock.run(run_reply(clock, [3000, 3000, 3000], gaps_s={0: 2.0, 1: 2.0}))
    plays = [(t, m) for t, _, m in messages(io) if m["event"] == "playAudio"]
    steady = [b[0] - a[0] for a, b in zip(plays[1:], plays[2:])]
    assert steady and max(steady) < 0.13 and min(steady) > 0.0                   # one message per ~100 ms while audio flows (batching of five 20 ms frames)
    per_second = {}
    for t, _ in plays:
        per_second[int(t - plays[0][0])] = per_second.get(int(t - plays[0][0]), 0) + 1
    assert max(per_second.values()) <= 14                                        # the first-second burst included: ~10 messages a second, not 50
    # inbound is documented as ~20 ms chunks; the outbound frame time of the playout is the same unit
    assert po.cfg.frame_ms == 20 and po.frame * 2 == 320


async def test_an_underrun_does_not_leave_audio_stuck_in_the_batch_buffer():
    clock = FakeClock()
    io, sink, po, rep = await clock.run(run_reply(clock, [330, 330], gaps_s={0: 3.0}))   # 330 ms is not a multiple of the 100 ms batch
    assert rep["underruns"] == 1 and sink.buf == bytearray()
    written = sum(len(base64.b64decode(m["media"]["payload"])) for _, _, m in messages(io) if m["event"] == "playAudio")
    assert written == sink.frames * 160                                          # every frame handed over was written: nothing was held back
    first_gap_msgs = [t for t, _, m in messages(io) if m["event"] == "playAudio"]
    assert min(t for t in first_gap_msgs) - clock.t0 < 0.5 and first_gap_msgs[1] - first_gap_msgs[0] < 0.2
