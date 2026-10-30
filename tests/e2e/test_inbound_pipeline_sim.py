"""Offline end-to-end simulation of the REAL inbound path (nothing external is called):

  Plivo `media` JSON event -> PlivoIO (strict base64, chunk order, mu-law -> PCM16) -> serve_loop (Capture: pre-roll, VAD, endpointing,
  barge-in; bounded UtteranceQueue; turn worker) -> Conversation.stream_turn (STT request, dedupe, glossary, history commit, LLM request,
  clause TTS dispatch) -> playback validation -> Plivo `playAudio` / `clearAudio` JSON.

Only the three model services are fakes (STT text script, LLM, TTS). Time is virtual: serve_loop's clock advances 20 ms per media event and
50 ms per idle tick, so the scenarios are deterministic and run in milliseconds. Fixtures are synthetic energy envelopes (tones), not speech.
"""
import asyncio
import base64
import json
import struct
import types

import pytest

from app.ai import codec
from app.ai import ws as W
from app.ai.capture import CANCELLED, EXPIRED, FAILED, PROCESSED, REJECTED
from app.ai.clients import rms
from app.ai.pipeline import Conversation

FM = 20
N = 160
TERMINAL = {PROCESSED, REJECTED, CANCELLED, FAILED, EXPIRED}


class Clock:
    t = 1000.0


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    c.t = 1000.0
    monkeypatch.setattr(W, "time", types.SimpleNamespace(monotonic=lambda: c.t, perf_counter=lambda: c.t, time=lambda: c.t))
    return c


def tone(level):
    amp = level * 2 ** 0.5
    return struct.pack("<%dh" % N, *[int(amp * __import__("math").sin(2 * 3.14159265 * 220 * i / 8000)) for i in range(N)])


def media_event(chunk, pcm):
    return {"event": "media", "media": {"track": "inbound", "chunk": chunk, "timestamp": str(chunk * 20),
                                         "payload": base64.b64encode(codec.pcm16_to_mulaw(pcm)).decode()}}


class FakeWS:
    """Scripted Plivo socket: ("s", ms, level) speech/quiet frames, ("i", ms) idle gap. Collects what the service sends back."""

    def __init__(self, clock, script, amp=1.0):
        self.clock, self.items, self.sent, self.chunk, self.amp = clock, list(script), [], 0, amp
        self.frames_in = []

    async def receive(self):
        for _ in range(3):
            await asyncio.sleep(0)
        while self.items:
            it = self.items[0]
            if it[0] == "s":
                if it[1] <= 0:
                    self.items.pop(0)
                    continue
                self.items[0] = ("s", it[1] - FM, it[2])
                self.chunk += 1
                self.clock.t += FM / 1000
                pcm = tone(it[2] * self.amp)
                self.frames_in.append(it[2])
                return {"type": "websocket.receive", "text": json.dumps(media_event(self.chunk, pcm))}
            if it[0] == "i":
                if it[1] <= 0:
                    self.items.pop(0)
                    continue
                self.items[0] = ("i", it[1] - 50)
                self.clock.t += 0.05
                raise asyncio.TimeoutError
            if it[0] == "raw":
                self.items.pop(0)
                return {"type": "websocket.receive", "text": json.dumps(it[1])}
        return {"type": "websocket.receive", "text": json.dumps({"event": "stop"})}

    async def send_text(self, s):
        self.sent.append((self.clock.t, json.loads(s)))


SPEECH, QUIET = 2500, 100


def S(ms, level=SPEECH):
    return ("s", ms, level)


class Fakes:
    """STT / LLM / TTS fakes with virtual latency. STT text comes from `texts` in submission order."""

    cfg: dict = {}

    def __init__(self, clock, texts, stt_s=0.0, llm_s=0.0, tts_s=0.0, reply_s=0.4):
        self.clock, self.texts, self.stt_s, self.llm_s, self.tts_s, self.reply_s = clock, list(texts), stt_s, llm_s, tts_s, reply_s
        self.stt_pcm, self.llm_msgs, self.tts_texts, self.n_reply = [], [], [], 0

    async def vsleep(self, s):
        t0 = self.clock.t
        while self.clock.t < t0 + s:
            await asyncio.sleep(0)

    async def transcribe(self, pcm, rate, language):
        self.stt_pcm.append(pcm)
        await self.vsleep(self.stt_s)
        text = self.texts.pop(0) if self.texts else "haan"
        return {"text": text, "language": "hi"}

    async def chat_stream(self, messages, tools=None, temperature=None):
        self.llm_msgs.append(json.loads(json.dumps(messages)))
        k = len(self.llm_msgs)
        await self.vsleep(self.llm_s)
        yield {"delta": f"Reply{k} bilkul sahi hai ji. "}
        yield {"done": True}

    async def synthesize(self, text, language):
        self.tts_texts.append(text)
        k = int(text.split()[0].replace("Reply", ""))
        await self.vsleep(self.tts_s)
        n = int(8000 * self.reply_s)
        return struct.pack("<%dh" % n, *([2000 * k] * n)), 8000


class FakeAI:
    def __init__(self): self.heard, self.errors = [], []
    async def record_user_utterance(self, rt, heard, stt_ms=0.0): self.heard.append(heard)
    async def record_turn(self, *a, **k): pass
    async def finalize_turn(self, *a, **k): pass
    def note_error(self, rt, code): self.errors.append(code)
    async def stop(self, *a, **k): pass


def loud_frames(pcm):
    return sum(1 for i in range(0, len(pcm), N * 2) if rms(pcm[i:i + N * 2]) >= 450)


def audio_marks(ws):
    """[(time, reply number)] for every playAudio chunk the service sent; reply number is decoded from the audio itself."""
    out = []
    for t, m in ws.sent:
        if m.get("event") == "playAudio":
            pcm = codec.mulaw_to_pcm16(base64.b64decode(m["media"]["payload"]))
            mid = pcm[len(pcm) // 2 - 2:len(pcm) // 2]
            out.append((t, round(abs(struct.unpack("<h", mid)[0]) / 2000)))
    return out


def events(ws, name):
    return [t for t, m in ws.sent if m.get("event") == name]


async def run_call(clock, script, fakes, amp=1.0, **loop_kw):
    ws = FakeWS(clock, script + [("i", 30000)], amp=amp)
    conv = Conversation(fakes, "hi", glossary_correction=loop_kw.pop("glossary", False))
    rt = types.SimpleNamespace(conversation=conv, tenant_id="t", session_id="s", call_id="c")
    ai = FakeAI()
    io = W.PlivoIO(ws, "stream1")
    end = await W.serve_loop(ws, ai, rt, io, 8000, FM, vad_rms=W.VAD_RMS, **loop_kw)
    return ws, conv, rt, ai, end


# ------------------------------------------------------------------ scenarios
async def test_s1_short_acknowledgement_reaches_stt(clock):
    f = Fakes(clock, ["haan"])
    ws, conv, rt, ai, _ = await run_call(clock, [S(400, QUIET), S(240), S(2000, QUIET)], f)
    assert len(f.stt_pcm) == 1
    assert loud_frames(f.stt_pcm[0]) >= 240 // FM - 1            # every loud frame (mu-law rounding aside), incl. the very first
    assert ai.heard == ["haan"] and audio_marks(ws)


async def test_s2_natural_800ms_pause_is_one_utterance(clock):
    f = Fakes(clock, ["haan mere ko B.Tech fees janni hai"])
    ws, conv, rt, ai, _ = await run_call(clock, [S(400, QUIET), S(400), S(800, QUIET), S(1500), S(2500, QUIET)], f)
    assert len(f.stt_pcm) == 1 and loud_frames(f.stt_pcm[0]) >= (400 + 1500) // FM - 2
    assert [m["role"] for m in f.llm_msgs[0] if m["role"] == "user"] == ["user"]


async def test_s3_rank_sentence_survives_every_stage(clock):
    said = "Mera JEE Main result aa chuka hai, meri rank ek lakh hai."
    f = Fakes(clock, [said])
    ws, conv, rt, ai, _ = await run_call(clock, [S(400, QUIET), S(3000), S(2500, QUIET)], f, glossary=True)
    assert ai.heard == [said]                                                      # dashboard event
    assert [m["content"] for m in conv.messages if m["role"] == "user"] == [said]  # committed history
    last_user = [m for m in f.llm_msgs[0] if m["role"] == "user"][-1]["content"]
    for must in ("JEE Main", "result", "rank", "ek lakh"):
        assert must in last_user                                                   # serialized LLM request
    assert conv.turns[0]["heard_raw"] == said


async def test_s4_repeated_digits_are_exact(clock):
    f = Fakes(clock, ["98 98 76 76."])
    ws, conv, rt, ai, _ = await run_call(clock, [S(400, QUIET), S(1500), S(2500, QUIET)], f)
    assert ai.heard == ["98 98 76 76."]
    assert [m["role"] for m in f.llm_msgs[0]].count("user") == 1
    assert "98 98 76 76" in f.llm_msgs[0][-1]["content"]


async def test_s5_barge_in_captures_new_question_and_only_the_new_reply_plays(clock):
    f = Fakes(clock, ["pehla sawaal", "doosra poora sawaal"], reply_s=6.0)
    script = [S(400, QUIET), S(1000), S(2500, QUIET),          # question 1 -> reply 1 (6 s) starts playing
              S(300, QUIET), S(1800), S(2500, QUIET)]          # caller talks over it with a complete question
    ws, conv, rt, ai, _ = await run_call(clock, script, f)
    assert len(f.stt_pcm) == 2 and loud_frames(f.stt_pcm[1]) >= 1800 // FM - 2   # new question fully captured
    marks = audio_marks(ws)
    clears = events(ws, "clearAudio")
    assert len(clears) == 1
    assert not [r for t, r in marks if r == 1 and t > clears[0]]                  # nothing of reply 1 after the clear
    assert [r for t, r in marks if t > clears[0]] and {r for t, r in marks if t > clears[0]} == {2}
    users = [m["content"] for m in conv.messages if m["role"] == "user"]
    assert users == ["pehla sawaal", "doosra poora sawaal"]                       # both caller turns survived


async def test_s6_ten_second_utterance_is_not_truncated(clock):
    f = Fakes(clock, ["lambi baat"])
    ws, conv, rt, ai, _ = await run_call(clock, [S(400, QUIET), S(10000), S(2500, QUIET)], f)
    assert len(f.stt_pcm) == 1                                                    # soft-cap segments merged back into ONE job
    assert loud_frames(f.stt_pcm[0]) >= 10000 // FM - 3
    assert len(f.stt_pcm[0]) // (N * 2) * FM >= 10000


async def test_s7_queue_pressure_is_bounded_explicit_and_loses_nothing(clock):
    f = Fakes(clock, [f"baat {i}" for i in range(10)], stt_s=20.0)               # STT takes 20 s while the caller keeps talking
    script = [S(400, QUIET)]
    for _ in range(7):
        script += [S(600), S(1400, QUIET)]
    ws, conv, rt, ai, _ = await run_call(clock, script, f, queue_max=2)
    q = rt.utt_queue
    sent_loud = sum(loud_frames(p) for p in f.stt_pcm)
    assert len(f.stt_pcm) >= 2 and max(len(q.items), 0) <= 2
    assert sent_loud >= 7 * (600 // FM) - 7 * 2                                   # every spoken frame reached STT (merged, never dropped)
    assert set(q.terminal.values()) <= TERMINAL and len(q.terminal) == q.new_id() - 1  # each utterance ended in exactly one terminal state
    assert not [e for e in q.log if e[1] == REJECTED]


async def test_s8_two_simultaneous_calls_are_isolated(clock):
    fa = Fakes(clock, ["caller A baat"])
    fb = Fakes(clock, ["caller B baat"])
    sa = [S(400, QUIET), S(1000, 1500), S(2500, QUIET)]
    sb = [S(400, QUIET), S(1000, 3500), S(2500, QUIET)]
    ra, rb = await asyncio.gather(run_call(clock, sa, fa), run_call(clock, sb, fb))
    (wa, ca, rta, aia, _), (wb, cb, rtb, aib, _) = ra, rb
    assert aia.heard == ["caller A baat"] and aib.heard == ["caller B baat"]
    assert [m["content"] for m in ca.messages if m["role"] == "user"] == ["caller A baat"]
    assert [m["content"] for m in cb.messages if m["role"] == "user"] == ["caller B baat"]
    levels_a = {round(rms(p[i:i + N * 2]) / 500) for p in fa.stt_pcm for i in range(0, len(p) - N * 2, N * 2) if rms(p[i:i + N * 2]) > 450}
    levels_b = {round(rms(p[i:i + N * 2]) / 500) for p in fb.stt_pcm for i in range(0, len(p) - N * 2, N * 2) if rms(p[i:i + N * 2]) > 450}
    assert levels_a and levels_b and not (levels_a & levels_b)                    # no audio crossed between the calls
    assert rta.capture is not rtb.capture and rta.utt_queue is not rtb.utt_queue


async def test_s9_cancelled_llm_keeps_the_callers_text(clock):
    f = Fakes(clock, ["pehli baat", "doosri baat"], llm_s=5.0)                    # LLM is slow; the caller speaks again while it thinks
    script = [S(400, QUIET), S(800), S(1500, QUIET), S(800), S(2500, QUIET)]
    ws, conv, rt, ai, _ = await run_call(clock, script, f)
    users = [m["content"] for m in conv.messages if m["role"] == "user"]
    assert users[0] == "pehli baat" and users[-1] == "doosri baat"
    assert [m["role"] for m in conv.messages].count("assistant") <= 1             # no assistant message for the cancelled turn
    last_req = [m["content"] for m in f.llm_msgs[-1] if m["role"] == "user"]
    assert "pehli baat" in "\n".join(last_req) and "doosri baat" in "\n".join(last_req)   # latest request carries both, in order
    assert "pehli baat" in ai.heard and "doosri baat" in ai.heard


async def test_s10_late_tts_of_an_old_turn_never_reaches_plivo(clock):
    f = Fakes(clock, ["pehla", "doosra"], tts_s=4.0)                              # reply 1's TTS completes AFTER the caller spoke again
    script = [S(400, QUIET), S(800), S(1500, QUIET), S(800), S(2500, QUIET)]
    ws, conv, rt, ai, _ = await run_call(clock, script, f)
    marks = audio_marks(ws)
    assert 1 not in {r for _, r in marks}                                          # stale reply 1 audio was rejected
    assert {r for _, r in marks} <= {2}


async def test_every_utterance_has_one_terminal_state_and_frames_balance(clock):
    f = Fakes(clock, ["ek", "do", "teen"])
    ws, conv, rt, ai, _ = await run_call(clock, [S(400, QUIET), S(600), S(2500, QUIET), S(600), S(2500, QUIET), S(600), S(2500, QUIET)], f)
    q = rt.utt_queue
    assert set(q.terminal.values()) <= TERMINAL and len(q.terminal) == q.new_id() - 1
    assert rt.capture.stats.balanced(rt.capture)


async def test_wire_garbage_is_counted_and_never_reaches_vad(clock):
    f = Fakes(clock, ["haan"])
    bad = [("raw", {"event": "media", "media": {"track": "inbound", "chunk": 1, "payload": "***"}}),
           ("raw", {"event": "media", "media": {"track": "inbound", "chunk": 2, "payload": ""}})]
    ws, conv, rt, ai, _ = await run_call(clock, bad + [S(400, QUIET), S(400), S(2500, QUIET)], f)
    assert len(f.stt_pcm) == 1
