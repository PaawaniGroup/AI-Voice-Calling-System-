"""Progressive TTS client (`ModelClients.synthesize_stream`): ordering, early delivery, cancel, fallback and protocol errors.
RunPod is a MOCK (httpx.MockTransport). The `/stream/{id}` response shape ({"status", "stream": [{"output": item}]}) is an ASSUMPTION that no
page I could read documents: the first live run must confirm it."""
import base64
import json

import httpx
import pytest

from app.ai import codec
from app.ai.clients import AiError, ModelClients, pcm16_to_wav
from tests.conftest import make_settings
from tests.helpers import pcm_tone

BASE = "https://api.runpod.ai/v2/abc123"


def ulaw_item(seq, ms=40, final=False):
    mu = codec.pcm16_to_mulaw(pcm_tone(ms))   # tone content is irrelevant; mu-law is 1 byte per sample
    return {"seq": seq, "audio_base64": base64.b64encode(mu).decode(), "format": "ulaw8k", "sample_rate": 8000, "final": final}


class Worker:
    """A scripted RunPod endpoint: each GET /stream returns the next scripted poll."""

    def __init__(self, polls, status_output=None):
        self.polls, self.log, self.status_output, self.n = list(polls), [], status_output, 0

    def __call__(self, req: httpx.Request):
        p = req.url.path
        self.log.append((req.method, p.replace("/v2/abc123", "")))
        if p.endswith("/run"):
            self.body = json.loads(req.content)
            return httpx.Response(200, json={"id": "job1", "status": "IN_QUEUE"})
        if "/stream/" in p:
            self.n += 1
            return httpx.Response(200, json=self.polls.pop(0) if self.polls else {"status": "IN_PROGRESS", "stream": []})
        if "/status/" in p:
            return httpx.Response(200, json={"id": "job1", "status": "COMPLETED", "output": self.status_output})
        if "/cancel/" in p:
            return httpx.Response(200, json={"id": "job1", "status": "CANCELLED"})
        return httpx.Response(404)


def make(worker):
    s = make_settings(ai_allowed_hosts="api.runpod.ai")
    return ModelClients(s, {"tts_url": BASE + "/runsync", "keys": {"tts": "k"}}, httpx.MockTransport(worker))


def out(*items, status="IN_PROGRESS"):
    return {"status": status, "stream": [{"output": i} for i in items]}


async def test_chunks_arrive_in_order_and_the_first_is_delivered_before_the_next_poll():
    w = Worker([out(ulaw_item(0)), out(ulaw_item(1), ulaw_item(2, final=True))])
    c = make(w)
    got, polls_when_seen = [], []
    async for ch in c.synthesize_stream("namaste", "hi"):
        got.append(ch)
        polls_when_seen.append(w.n)
    await c.aclose()
    assert [g.seq for g in got] == [0, 1, 2] and got[-1].final
    assert polls_when_seen == [1, 2, 2]               # chunk 0 was handed over after ONE poll: nothing waited for the rest of the utterance
    assert all(g.sample_rate == 8000 and len(g.pcm) == len(pcm_tone(40)) for g in got)   # mu-law bytes -> same number of 16-bit samples
    assert w.body["input"]["stream"] is True and w.body["input"]["output_format"] == "ulaw8k"
    assert not any(m == "POST" and "/cancel/" in p for m, p in w.log)           # finished normally: nothing to cancel


async def test_a_repeated_item_is_dropped_not_played_twice():
    w = Worker([out(ulaw_item(0), ulaw_item(1)), out(ulaw_item(1), ulaw_item(2, final=True))])
    c = make(w)
    seqs = [ch.seq async for ch in c.synthesize_stream("x", "hi")]
    await c.aclose()
    assert seqs == [0, 1, 2]


async def test_a_gap_in_the_sequence_is_an_error():
    w = Worker([out(ulaw_item(0), ulaw_item(2))])
    c = make(w)
    with pytest.raises(AiError) as e:
        _ = [ch async for ch in c.synthesize_stream("x", "hi")]
    await c.aclose()
    assert e.value.kind == "bad_response"


async def test_closing_the_stream_early_cancels_the_runpod_job():
    w = Worker([out(ulaw_item(0), ulaw_item(1)), out(ulaw_item(2, final=True))])
    c = make(w)
    gen = c.synthesize_stream("x", "hi")
    first = await gen.__anext__()
    assert first.seq == 0
    await gen.aclose()                                # barge-in: the consumer stops listening
    await c.aclose()                                  # waits for the background cancel call
    assert ("POST", "/cancel/job1") in w.log


async def test_a_worker_that_does_not_stream_still_works_as_one_final_chunk():
    wav = pcm16_to_wav(pcm_tone(200), 24000)
    w = Worker([{"status": "COMPLETED", "stream": []}], status_output={"audio_base64": base64.b64encode(wav).decode(), "format": "wav"})
    c = make(w)
    got = [ch async for ch in c.synthesize_stream("x", "hi")]
    await c.aclose()
    assert len(got) == 1 and got[0].final and got[0].streamed is False and got[0].sample_rate == 24000


async def test_a_failed_job_is_an_error():
    c = make(Worker([{"status": "FAILED", "stream": []}]))
    with pytest.raises(AiError) as e:
        _ = [ch async for ch in c.synthesize_stream("x", "hi")]
    await c.aclose()
    assert e.value.kind == "runpod_error"


async def test_a_worker_error_item_is_an_error():
    c = make(Worker([out({"error": "CUDA out of memory"})]))
    with pytest.raises(AiError) as e:
        _ = [ch async for ch in c.synthesize_stream("x", "hi")]
    await c.aclose()
    assert e.value.kind == "runpod_error"


async def test_stream_that_completes_without_a_final_chunk_is_an_error():
    c = make(Worker([out(ulaw_item(0), status="COMPLETED")]))
    with pytest.raises(AiError) as e:
        _ = [ch async for ch in c.synthesize_stream("x", "hi")]
    await c.aclose()
    assert e.value.kind == "bad_response"


@pytest.mark.parametrize("bad", [
    {"seq": 0, "audio_base64": "!!!", "format": "ulaw8k", "final": False},
    {"seq": 0, "audio_base64": "", "format": "ulaw8k", "final": False},                 # empty and not final
    {"seq": "0", "audio_base64": "AAAA", "format": "ulaw8k", "final": False},
    {"seq": 0, "audio_base64": "AAAA", "format": "mp3", "final": False},
    {"seq": 0, "audio_base64": "AAAA", "format": "pcm16", "sample_rate": 24000, "final": False},   # 3 bytes: odd for 16-bit
    "not an object",
])
async def test_malformed_items_are_rejected(bad):
    c = make(Worker([{"status": "IN_PROGRESS", "stream": [{"output": bad}]}]))
    with pytest.raises(AiError) as e:
        _ = [ch async for ch in c.synthesize_stream("x", "hi")]
    await c.aclose()
    assert e.value.kind == "bad_response"


async def test_an_empty_final_chunk_ends_the_stream():
    w = Worker([out(ulaw_item(0), {"seq": 1, "audio_base64": "", "format": "ulaw8k", "final": True})])
    c = make(w)
    got = [ch async for ch in c.synthesize_stream("x", "hi")]
    await c.aclose()
    assert [g.seq for g in got] == [0, 1] and got[1].pcm == b"" and got[1].final
