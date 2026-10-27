"""RtModelClients and the tenant config of the persistent transport. SIMULATED: in-memory sockets to the real worker protocol server with fake engines, httpx.MockTransport for the
queue path; no network, no GPU, no RunPod. Covers the fallback, the kill switches, the circuit breaker, per-call isolation, readiness, and (the key_refs lesson) that every new key
survives AiService.get_config and is validated on put."""
from __future__ import annotations

import base64
import itertools
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.ai import turn_trace
from app.ai.clients import AiError, ModelClients
from app.ai.rt import resilience
from app.ai.rt.clients import RtModelClients
from app.ai.rt.connection import RtConfig
from app.ai.service import RT_KEYS, AiService
from app.core.errors import Unprocessable
from config.settings import Settings
from tests.unit.test_rt_connection import FakeSTT, FakeTTS, connector_for

_ids = itertools.count(1)
QUEUE_TTS_BYTES = b"\xff" * 800


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    resilience._BREAKERS.clear()
    monkeypatch.setenv("RT_TRANSPORT_SECRET", "unit-test-secret")
    tok = turn_trace._current.set(None)
    yield
    turn_trace._current.reset(tok)
    resilience._BREAKERS.clear()


class QueueSide:
    """The legacy path: /run + /status of two RunPod endpoints (ep1 = voice, ep2 = STT); records the voice text it was sent."""

    def __init__(self):
        self.tts_texts, self.runs = [], 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/run"):
            self.runs += 1
            body = json.loads(req.content)
            if "text" in body.get("input", {}):
                self.tts_texts.append(body["input"]["text"])
            return httpx.Response(200, json={"id": "q" + req.url.path.split("/")[-2], "status": "IN_QUEUE"})
        out = ({"audio": base64.b64encode(QUEUE_TTS_BYTES).decode(), "format": "ulaw8k"} if "ep1" in req.url.path else {"text": "queue path text", "language": "hi"})
        return httpx.Response(200, json={"id": "q", "status": "COMPLETED", "delayTime": 5, "executionTime": 50, "output": out})


def make(connector=None, queue=None, settings=None, **extra):
    n = next(_ids)
    cfg = {"tts_url": "https://api.example.invalid/v2/ep1/runsync", "stt_url": "https://api.example.invalid/v2/ep2/runsync",
           "brain_url": "https://api.example.invalid/v2/ep3/runsync", "keys": {"tts": "k", "stt": "k", "brain": "k"}, "tts_ulaw8k": True, "tts_voice": "utkarsh",
           "runpod_poll_cap_s": 0.03, "rt_transport": {"tts": "persistent", "stt": "persistent"},
           "rt_endpoints": {"tts": f"wss://t{n}.example.invalid/ws", "stt": f"wss://s{n}.example.invalid/ws", "brain": f"https://b{n}.example.invalid"}}
    cfg.update(extra)
    return RtModelClients(settings or Settings(_env_file=None), cfg, transport=httpx.MockTransport(queue or QueueSide()), connector=connector,
                          rt_cfg=RtConfig(request_timeout_s=2.0, cancel_grace_s=0.2, hello_timeout_s=1.0, connect_timeout_s=1.0))


async def refusing(url):
    raise ConnectionRefusedError()


async def test_the_persistent_voice_path_returns_pcm_and_records_a_numbers_only_job():
    eng = FakeTTS(chunks=2, size=1600)
    tr = turn_trace.TurnTrace(t_speech_end=0.0)
    turn_trace.start(tr)
    rc = make(connector=connector_for(eng))
    rc.set_call("call-9")
    pcm, rate = await rc.synthesize("w1 w2 w3,", "hi")
    assert rate == 8000 and len(pcm) == 2 * 3200                                   # 3200 mu-law bytes -> 6400 bytes of PCM16
    j = tr.jobs[0]
    assert j["service"] == "tts" and j["transport"] == "rt" and j["status"] == "ok" and j["words"] == 3 and j["polls"] == 0
    assert all(isinstance(j[k], (int, float)) for k in ("first_byte_ms", "total_ms", "audio_s", "delay_ms", "exec_ms", "remainder_ms"))
    assert rc.rt_counters["rt_ok"] == 1 and rc.readiness.ready("tts")
    await rc.aclose()


async def test_the_persistent_path_prepares_the_text_exactly_like_the_queue_path():
    q, eng = QueueSide(), FakeTTS(chunks=1, size=1600)
    rc_q = make(queue=q, rt_transport={})                                          # no persistent service: the queue path
    await rc_q.synthesize("Fees hai ₹5,000 per year", "hinglish")
    rc_rt = make(connector=connector_for(eng))
    await rc_rt.synthesize("Fees hai ₹5,000 per year", "hinglish")
    assert q.tts_texts and eng.texts_seen == len(q.tts_texts[0])                   # the same spoken text (normalised) reaches the voice either way
    await rc_rt.aclose()


async def test_when_the_persistent_server_is_unreachable_the_request_is_answered_by_the_queue_path():
    q = QueueSide()
    tr = turn_trace.TurnTrace(t_speech_end=0.0)
    turn_trace.start(tr)
    rc = make(connector=refusing, queue=q)
    pcm, rate = await rc.synthesize("w1 w2 w3,", "hi")
    assert rate == 8000 and len(pcm) == 2 * len(QUEUE_TTS_BYTES) and q.runs == 1
    assert rc.rt_counters["rt_fallback"] == 1 and tr.jobs[0]["status"] == "error:rt_connect" and tr.jobs[1]["status"] == "ok"
    await rc.aclose()


async def test_the_breaker_opens_after_three_failures_and_later_requests_do_not_try_the_dead_server():
    attempts = []

    async def counting_refuse(url):
        attempts.append(1)
        raise ConnectionRefusedError()
    q = QueueSide()
    rc = make(connector=counting_refuse, queue=q)
    rc._rt_cfg.max_connect_attempts = 1
    for _ in range(3):
        await rc.synthesize("w1 w2", "hi")
    before = len(attempts)
    await rc.synthesize("w1 w2", "hi")
    assert before == 3 and len(attempts) == 3 and rc.rt_counters["rt_breaker_open"] == 1 and q.runs == 4   # the fourth went straight to the queue path
    await rc.aclose()


async def test_the_per_call_kill_switch_and_the_global_setting_send_everything_to_the_queue_path():
    q, eng = QueueSide(), FakeTTS(chunks=1, size=1600)
    rc = make(connector=connector_for(eng), queue=q)
    rc.rt_kill()
    await rc.synthesize("w1 w2", "hi")
    assert eng.started == 0 and q.runs == 1
    q2, eng2 = QueueSide(), FakeTTS(chunks=1, size=1600)
    rc2 = make(connector=connector_for(eng2), queue=q2, settings=Settings(_env_file=None, ai_rt_kill=True))
    await rc2.synthesize("w1 w2", "hi")
    assert eng2.started == 0 and q2.runs == 1
    await rc.aclose()
    await rc2.aclose()


async def test_a_missing_secret_falls_back_without_a_connection(monkeypatch):
    monkeypatch.delenv("RT_TRANSPORT_SECRET", raising=False)
    q, eng = QueueSide(), FakeTTS(chunks=1, size=1600)
    rc = make(connector=connector_for(eng), queue=q)
    await rc.synthesize("w1 w2", "hi")
    assert eng.started == 0 and q.runs == 1 and rc.rt_counters["rt_fallback"] == 1
    await rc.aclose()


async def test_the_secret_env_name_is_configurable_and_the_value_is_not_in_the_config(monkeypatch):
    monkeypatch.setenv("MY_RT_SECRET", "unit-test-secret")
    monkeypatch.delenv("RT_TRANSPORT_SECRET", raising=False)
    eng = FakeTTS(chunks=1, size=1600)
    rc = make(connector=connector_for(eng), rt_secret_env="MY_RT_SECRET")
    await rc.synthesize("w1 w2", "hi")
    assert eng.started == 1 and "unit-test-secret" not in json.dumps(rc.cfg)
    await rc.aclose()


async def test_persistent_stt_and_its_fallback():
    eng = FakeSTT(text="persistent text")
    rc = make(connector=connector_for(eng, service="stt"))
    assert (await rc.transcribe(b"\x01\x00" * 4000, 8000, "hinglish")) == {"text": "persistent text", "language": "hi"}
    assert eng.sizes == [8000] and rc.readiness.ready("stt")
    q = QueueSide()
    rc2 = make(connector=refusing, queue=q)
    assert (await rc2.transcribe(b"\x01\x00" * 4000, 8000, "auto"))["text"] == "queue path text" and rc2.rt_counters["rt_fallback"] == 1
    await rc.aclose()
    await rc2.aclose()


async def test_each_call_gets_its_own_connections_carrying_its_own_call_id():
    a, b = make(connector=connector_for(FakeTTS(chunks=1, size=1600))), make(connector=connector_for(FakeTTS(chunks=1, size=1600)))
    a.set_call("call-A")
    b.set_call("call-B")
    await a.synthesize("w1 w2", "hi")
    await b.synthesize("w1 w2", "hi")
    assert a._conn("tts").call_id == "call-A" and b._conn("tts").call_id == "call-B" and a._conn("tts") is not b._conn("tts")
    await a.aclose()
    await b.aclose()
    assert a._conn("tts")._closed and b._conn("tts")._closed


async def test_readiness_comes_from_a_completed_warmup_request_never_from_a_health_listing():
    eng = FakeTTS()
    rc = make(connector=connector_for(eng))
    assert not rc.readiness.ready("tts")
    assert await rc.rt_warmup("tts") is True and rc.readiness.ready("tts") and eng.started == 0
    assert await rc.rt_warmup("brain") is False                                    # the Brain has no warmup request here
    bad = make(connector=refusing)
    assert await bad.rt_warmup("tts") is False and not bad.readiness.ready("tts")
    await rc.aclose()
    await bad.aclose()


async def test_the_brain_uses_the_always_on_server_while_the_flag_is_set_and_falls_back_before_the_first_token(monkeypatch):
    calls = []

    async def fake_chat_stream(self, messages, tools=None, temperature=None):
        calls.append(self.endpoint("brain").url)
        if len(calls) == 1:
            raise AiError("brain", "http_error", "503")                            # the persistent server fails before any token
        yield {"delta": "w1 "}
        yield {"done": True, "text": "w1 "}
    monkeypatch.setattr(ModelClients, "chat_stream", fake_chat_stream)
    rc = make(rt_transport={"brain": "persistent"})
    out = [i async for i in rc.chat_stream([{"role": "user", "content": "x"}])]
    assert out[0] == {"delta": "w1 "} and rc.rt_counters["rt_fallback"] == 1
    assert calls[0].startswith("https://b") and calls[0].endswith(".example.invalid") and "api.example.invalid" in calls[1]       # first the Pod, then the queue endpoint
    assert rc._brain_rt is False
    await rc.aclose()


async def test_a_half_spoken_brain_answer_is_never_replayed_on_the_other_path(monkeypatch):
    calls = []

    async def fake_chat_stream(self, messages, tools=None, temperature=None):
        calls.append(1)
        yield {"delta": "w1 "}
        raise AiError("brain", "stream_broken")
    monkeypatch.setattr(ModelClients, "chat_stream", fake_chat_stream)
    rc = make(rt_transport={"brain": "persistent"})
    got = []
    with pytest.raises(AiError):
        async for i in rc.chat_stream([{"role": "user", "content": "x"}]):
            got.append(i)
    assert got == [{"delta": "w1 "}] and len(calls) == 1 and rc.rt_counters["rt_fallback"] == 0
    await rc.aclose()


# ------------------------------------------------------------------ the tenant config: defaults, allowlist, validation, masking
def _svc(doc=None, allowed="rt.example.invalid"):
    coll = MagicMock()
    coll.find_one = AsyncMock(return_value=doc)
    coll.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    db = MagicMock()
    db.__getitem__.return_value = coll
    return AiService(db, None, Settings(_env_file=None, ai_allowed_hosts=allowed), None), coll


async def test_the_defaults_are_off_and_every_new_key_survives_get_config():
    svc, _ = _svc(None)
    cfg = await svc.get_config("t")
    assert cfg["rt_transport"] == {} and cfg["rt_endpoints"] == {} and cfg["rt_secret_env"] is None
    doc = {"tenant_id": "t", "rt_transport": {"tts": "persistent"}, "rt_endpoints": {"tts": "wss://rt.example.invalid/ws"}, "rt_secret_env": "MY_RT_SECRET"}
    svc2, _ = _svc(dict(doc))
    cfg2 = await svc2.get_config("t")
    assert all(cfg2[k] == doc[k] for k in RT_KEYS), "a key of the allowlist was dropped between the document and the runtime config"


async def test_clients_are_the_plain_queue_clients_unless_a_service_is_persistent_and_the_kill_switch_is_off():
    svc, _ = _svc(None)
    assert type(svc._clients({"rt_transport": {}})) is ModelClients
    assert type(svc._clients({"rt_transport": {"tts": "queue"}})) is ModelClients
    assert type(svc._clients({"rt_transport": {"tts": "persistent"}})) is RtModelClients
    killed = AiService(MagicMock(), None, Settings(_env_file=None, ai_rt_kill=True), None)
    assert type(killed._clients({"rt_transport": {"tts": "persistent"}})) is ModelClients


async def test_put_config_validates_rt_keys():
    svc, coll = _svc({"tenant_id": "t"})
    saved = await svc.put_config("t", {"rt_transport": {"tts": "persistent"}, "rt_endpoints": {"tts": "wss://rt.example.invalid/ws/", "brain": "https://rt.example.invalid"},
                                       "rt_secret_env": "MY_RT_SECRET"}, "op")
    assert saved["rt_transport"] == {"tts": "persistent"} and saved["rt_endpoints"] == {"tts": "wss://rt.example.invalid/ws", "brain": "https://rt.example.invalid"}
    for bad in ({"rt_transport": {"gpu": "persistent"}}, {"rt_transport": {"tts": "fast"}}, {"rt_transport": ["tts"]}, {"rt_endpoints": {"tts": "https://rt.example.invalid/ws"}},
                {"rt_endpoints": {"brain": "wss://rt.example.invalid"}}, {"rt_endpoints": {"tts": "wss://other.example.invalid/ws"}}, {"rt_endpoints": {"x": "wss://rt.example.invalid"}},
                {"rt_secret_env": "lower case"}, {"rt_secret_env": "A-B"}):
        with pytest.raises(Unprocessable):
            await svc.put_config("t", bad, "op")


async def test_a_viewer_does_not_see_the_endpoint_urls():
    svc, _ = _svc(None)
    cfg = {**await svc.get_config("t"), "rt_endpoints": {"tts": "wss://rt.example.invalid/ws"}}
    assert svc.public_config(cfg, detail=False)["rt_endpoints"] == {"tts": "configured"}
    assert svc.public_config(cfg, detail=True)["rt_endpoints"] == {"tts": "wss://rt.example.invalid/ws"}


def test_the_config_route_model_accepts_the_new_fields_and_rejects_unknown_ones():
    from pydantic import ValidationError
    from app.api.routes_platform import AiConfigIn
    ok = AiConfigIn(rt_transport={"tts": "persistent"}, rt_endpoints={"tts": "wss://rt.example.invalid/ws"}, rt_secret_env="MY_RT_SECRET")
    assert ok.rt_transport == {"tts": "persistent"}
    with pytest.raises(ValidationError):
        AiConfigIn(rt_transport_typo={"tts": "persistent"})
