"""WP1 instrumentation: per-job RunPod records, Brain call counts, numbers-only evidence, segmenter stop reason.
Everything here is SIMULATED (httpx.MockTransport, no network, no RunPod)."""
from __future__ import annotations

import json

import httpx
import pytest

from app.ai import turn_trace
from app.ai.clients import AiError, ModelClients
from app.ai.streaming.segmenter import Clause, ClauseSegmenter, SegmenterConfig
from config.settings import Settings


@pytest.fixture(autouse=True)
def _isolate_trace():
    """The tests start traces in the test's own context: put the context variable back afterwards so no other test sees them."""
    tok = turn_trace._current.set(None)
    yield
    turn_trace._current.reset(tok)


def _clients(handler, **cfg):
    base = {"tts_url": "https://api.example.invalid/v2/ep1/runsync", "stt_url": "https://api.example.invalid/v2/ep2/runsync",
            "keys": {"tts": "k", "stt": "k"}, "runpod_poll_cap_s": 0.03}
    base.update(cfg)
    return ModelClients(Settings(), base, transport=httpx.MockTransport(handler))


def _job_handler(polls_in_progress=2, delay=120, exec_=2400):
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/run"):
            return httpx.Response(200, json={"id": "job1", "status": "IN_QUEUE"})
        if req.url.path.endswith("/status/job1"):
            state["n"] += 1
            if state["n"] <= polls_in_progress:
                return httpx.Response(200, json={"id": "job1", "status": "IN_PROGRESS"})
            return httpx.Response(200, json={"id": "job1", "status": "COMPLETED", "delayTime": delay, "executionTime": exec_, "output": {"ok": True}})
        return httpx.Response(404)
    return handler


@pytest.mark.asyncio
async def test_runpod_job_record_fields_simulated():
    tr = turn_trace.TurnTrace(t_speech_end=0.0)
    turn_trace.start(tr)
    cl = _clients(_job_handler(polls_in_progress=2))
    try:
        ep = cl.endpoint("tts")
        job = {"words": 4, "chars": 20}
        await cl._runpod_call("tts", ep, {}, {"input": {"text": "x"}}, job=job)
        await cl._runpod_call("tts", ep, {}, {"input": {"text": "y"}})
    finally:
        await cl.aclose()
    assert len(tr.jobs) == 2
    a, b = tr.jobs
    assert a is job and a["status"] == "ok" and a["service"] == "tts" and a["words"] == 4
    assert a["polls"] == 3 and a["poll_ms"] > 0 and a["last_get_ms"] is not None and a["response_bytes"] > 0
    assert a["delay_ms"] == 120.0 and a["exec_ms"] == 2400.0
    assert a["submit_ms"] >= 0 and a["request_bytes"] > 0 and a["total_ms"] > 0
    assert a["remainder_ms"] == pytest.approx(a["total_ms"] - 2520.0, abs=0.2)
    assert a["gap_since_prev_ms"] is None
    assert b["gap_since_prev_ms"] is not None            # the second job knows when the first ended


@pytest.mark.asyncio
async def test_failed_job_is_recorded_with_status():
    tr = turn_trace.TurnTrace(t_speech_end=0.0)
    turn_trace.start(tr)
    cl = _clients(lambda req: httpx.Response(403, text="forbidden"))
    try:
        with pytest.raises(AiError):
            await cl._runpod_call("tts", cl.endpoint("tts"), {}, {"input": {}})
    finally:
        await cl.aclose()
    assert tr.jobs[0]["status"] == "error:http_error" and tr.jobs[0]["polls"] == 0


@pytest.mark.asyncio
async def test_job_tag_is_merged_and_task_local():
    import asyncio
    tr = turn_trace.TurnTrace(t_speech_end=0.0)
    turn_trace.start(tr)
    cl = _clients(_job_handler(polls_in_progress=0))

    async def tagged(i):
        turn_trace.job_tag.set({"clause_idx": i, "stop_reason": "punct"})
        await cl._runpod_call("tts", cl.endpoint("tts"), {}, {"input": {}})

    try:
        await asyncio.gather(asyncio.ensure_future(tagged(0)), asyncio.ensure_future(tagged(1)))
    finally:
        await cl.aclose()
    assert sorted(j["clause_idx"] for j in tr.jobs) == [0, 1]
    assert turn_trace.job_tag.get() is None               # the tag did not leak into the caller's context


def test_evidence_is_numbers_only():
    tr = turn_trace.TurnTrace(t_speech_end=10.0)
    tr.mark("t_stt_sent", at=10.5)
    tr.jobs.append({"service": "tts", "status": "ok", "t_submit": 11.0, "polls": 3, "text": "caller words must never appear", "words": 5})
    tr.stream = {"ttft_ms": 700.0, "clauses": [{"idx": 0}], "script": {"kinds": {"roman_hindi": 1}}, "note": "also a word"}
    ev = tr.evidence()
    blob = json.dumps(ev)
    assert "caller words" not in blob and "also a word" not in blob
    assert ev["jobs"][0]["submit_at_ms"] == 1000.0 and ev["jobs"][0]["service"] == "tts" and ev["jobs"][0]["status"] == "ok"
    assert "clauses" not in ev["stream"]


def test_append_evidence_off_without_path_and_numbers_only_with_it(tmp_path):
    tr = turn_trace.TurnTrace(t_speech_end=1.0)
    tr.jobs.append({"service": "stt", "status": "ok", "t_submit": 1.2, "polls": 2, "text": "never written"})
    turn_trace.append_evidence("", tr)                       # no path: nothing is written anywhere
    assert list(tmp_path.iterdir()) == []
    f = tmp_path / "ev.jsonl"
    turn_trace.append_evidence(str(f), tr)
    turn_trace.append_evidence(str(f), tr)
    lines = f.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and "never written" not in lines[0]
    assert json.loads(lines[0])["jobs"][0]["polls"] == 2


def test_append_evidence_never_raises_on_a_bad_path(tmp_path):
    turn_trace.append_evidence(str(tmp_path / "no_such_dir" / "x.jsonl"), turn_trace.TurnTrace(t_speech_end=0.0))   # logged, not raised


def test_latency_flags_default_off_in_the_tenant_config():
    from app.ai.service import LATENCY_FLAGS, AiService
    svc = AiService.__new__(AiService)
    svc.s = Settings()
    cfg = AiService.default_config(svc, "t1")
    assert all(cfg[k] is False for k in LATENCY_FLAGS)


def test_note_brain_call_counts_only():
    tr = turn_trace.TurnTrace(t_speech_end=0.0)
    turn_trace.start(tr)
    msgs = [{"role": "system", "content": "s" * 100},
            {"role": "user", "content": "CONTEXT:\nabc\n\nSTUDENT: hi"},
            {"role": "assistant", "content": "a" * 10},
            {"role": "user", "content": "CONTEXT:\n" + "c" * 50 + "\n\nSTUDENT: q"}]
    turn_trace.note_brain_call(msgs, rag_on=True)
    r = tr.brain_calls[0]
    assert r["messages"] == 4 and r["history_messages"] == 2 and r["system_chars"] == 100 and r["document_service_on"] is True
    assert r["context_chars_last"] == len("CONTEXT:\n" + "c" * 50) and r["context_chars_all"] == len("CONTEXT:\nabc") + r["context_chars_last"]
    assert all(not isinstance(v, str) for v in r.values())


def test_segmenter_stop_reasons():
    seg = ClauseSegmenter(SegmenterConfig(first_min_words=2, min_words=3, max_words=6))
    out = seg.feed("Haan ji, aap batao ") + seg.feed("kaunsa course chahiye aapko abhi tak ") + seg.flush()
    assert all(isinstance(c, Clause) for c in out)
    assert out[0] == "Haan ji," and out[0].reason == "punct"          # a Clause still compares as a plain str
    assert out[1].reason in ("max_words", "punct")
    assert out[-1].reason == "flush"
    assert " ".join(out) == " ".join(str(c) for c in out)
