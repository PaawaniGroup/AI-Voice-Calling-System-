"""tools/realtime/stt_match_test.py: refusals, the similarity number, the verdict rules, and that no text is ever printed. SIMULATED: httpx.MockTransport as the worker, a synthetic
tone written to a temporary WAV outside the repository. NOT RUN."""
from __future__ import annotations

import array
import json
import math
import wave

import httpx
import pytest

from app.ai.clients import ModelClients
from config.settings import Settings
from tools.realtime import stt_match_test as M

SECRET_WORDS = "caller words must never be printed"


def write_wav(path, rate=8000, channels=1, seconds=1.0):
    n = int(rate * seconds)
    data = array.array("h", [int(5000 * math.sin(2 * math.pi * 300 * i / rate)) for i in range(n)]).tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data * channels)
    return str(path)


class Worker:
    def __init__(self, text_old=SECRET_WORDS, text_raw=SECRET_WORDS, raw_ok=True, queue_ms=100):
        self.text_old, self.text_raw, self.raw_ok, self.queue_ms, self.bodies = text_old, text_raw, raw_ok, queue_ms, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/run"):
            inp = json.loads(req.content)["input"]
            self.bodies.append(inp)
            return httpx.Response(200, json={"id": f"j{len(self.bodies)}", "status": "IN_QUEUE", "_raw": "audio_pcm16_b64" in inp})
        jid = req.url.path.rsplit("/", 1)[-1]
        raw = "audio_pcm16_b64" in self.bodies[int(jid[1:]) - 1]
        if raw and not self.raw_ok:
            return httpx.Response(200, json={"id": jid, "status": "FAILED", "error": "Provide audio_b64 (WAV)"})
        return httpx.Response(200, json={"id": jid, "status": "COMPLETED", "delayTime": self.queue_ms, "executionTime": 500,
                                         "output": {"text": self.text_raw if raw else self.text_old, "language": "hi"}})


def factory(worker):
    def make(raw: bool):
        return ModelClients(Settings(_env_file=None), {"stt_url": "https://api.example.invalid/v2/ep/runsync", "keys": {"stt": "k"}, "runpod_poll_cap_s": 0.02, "stt_raw_pcm": raw},
                            transport=httpx.MockTransport(worker))
    return make


def args(clip, **kw):
    a = ["--clip", clip, "--approved", "id"]
    for k, v in kw.items():
        a += [f"--{k.replace('_', '-')}", str(v)]
    return a


def test_similarity_is_a_number_over_characters_after_collapsing_whitespace():
    assert M.similarity("one two", "one  two") == 1.0 and M.similarity("abc", "") == 0.0 and 0.0 < M.similarity("abcdefgh", "abcdefgx") < 1.0
    assert M.similarity("", "") == 0.0                                          # nothing to compare is not a match


def test_the_verdict_rules():
    assert M.verdict(0.96, True, True, False, 100) == "PASS" and M.verdict(0.95, True, True, False, 100) == "PASS" and M.verdict(0.949, True, True, False, 100) == "FAIL"
    assert M.verdict(1.0, True, True, True, 100) == "FAIL"                      # the automatic fallback means the raw body was NOT what was tested
    assert M.verdict(1.0, True, False, False, 100) == "FAIL"
    assert M.verdict(1.0, True, True, False, 5000) == "NOT_EVALUATED" and M.verdict(1.0, True, True, False, None) == "NOT_EVALUATED"      # a cold worker concludes nothing


async def test_refusals(tmp_path, capsys):
    clip = write_wav(tmp_path / "c.wav")
    assert await M.main(["--clip", clip]) == 2 and "no --approved" in capsys.readouterr().out
    assert await M.main(["--clip", str(M.ROOT / "docs" / "nope.wav"), "--approved", "id"]) == 2 and "OUTSIDE" in capsys.readouterr().out
    assert await M.main(args(clip, cap_usd=0.5)) == 2 and "cap" in capsys.readouterr().out
    assert await M.main(args(write_wav(tmp_path / "s.wav", rate=16000))) == 2 and "8000" in capsys.readouterr().out
    assert await M.main(args(write_wav(tmp_path / "st.wav", channels=2))) == 2
    assert await M.main(args(str(tmp_path / "missing.wav"))) == 2


async def test_two_jobs_identical_transcripts_pass_and_no_text_is_printed(tmp_path, capsys):
    w = Worker()
    rc = await M.main(args(write_wav(tmp_path / "c.wav")), make_clients=factory(w))
    out = capsys.readouterr().out
    last = json.loads(out.strip().splitlines()[-1])
    assert rc == 0 and last["verdict"] == "PASS" and last["similarity"] == 1.0 and last["jobs_run"] == 2
    assert set(w.bodies[0]) >= {"audio_b64", "audio", "audio_base64"} and "audio_pcm16_b64" in w.bodies[1] and w.bodies[1]["sample_rate"] == 8000       # old body first, then raw
    assert SECRET_WORDS not in out and "caller words" not in out
    rows = [json.loads(x) for x in out.strip().splitlines()[:2]]
    assert rows[0]["raw_pcm"] is False and rows[0]["audio_fields"] == 3 and rows[1]["raw_pcm"] is True and rows[1]["audio_fields"] == 1 and rows[1]["request_bytes"] < rows[0]["request_bytes"]
    assert all(isinstance(r.get("delay_ms"), float) and isinstance(r.get("total_ms"), float) for r in rows)                                         # both jobs' timings


async def test_different_transcripts_below_the_threshold_fail(tmp_path, capsys):
    rc = await M.main(args(write_wav(tmp_path / "c.wav")), make_clients=factory(Worker(text_old="abcdefghij klmnop", text_raw="zzzzzzzz yyyyyy")))
    last = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1 and last["verdict"] == "FAIL" and last["similarity"] < 0.95


async def test_a_worker_that_rejects_the_raw_body_is_a_fail_not_a_pass_even_though_the_fallback_returns_text(tmp_path, capsys):
    rc = await M.main(args(write_wav(tmp_path / "c.wav")), make_clients=factory(Worker(raw_ok=False)))
    last = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1 and last["verdict"] == "FAIL"


async def test_a_cold_worker_stops_after_job_one_and_concludes_nothing(tmp_path, capsys):
    w = Worker(queue_ms=21000)
    rc = await M.main(args(write_wav(tmp_path / "c.wav")), make_clients=factory(w))
    last = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1 and last["verdict"] == "NOT_EVALUATED" and last["jobs_run"] == 1 and len(w.bodies) == 1 and "not warm" in last["stopped"]
