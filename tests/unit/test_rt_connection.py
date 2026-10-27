"""PersistentConnection against the REAL worker protocol server (deploy/rt_workers/common.py) over an in-memory socket pair, with fake engines.
SIMULATED: no network, no GPU, no model; it tests auth, streaming, cancellation (which must stop the engine), heartbeats, reconnect, backpressure and limits."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from app.ai.rt import protocol as P
from app.ai.rt.connection import PersistentConnection, RtConfig, RtError
from app.ai.rt.testing import make_pair

_spec = importlib.util.spec_from_file_location("rt_worker_common", Path(__file__).resolve().parents[2] / "deploy" / "rt_workers" / "common.py")
W = importlib.util.module_from_spec(_spec)
sys.modules["rt_worker_common"] = W          # a dataclass with postponed annotations looks its module up in sys.modules
_spec.loader.exec_module(W)

SECRET = b"unit-test-secret"


class FakeTTS:
    """Yields `chunks` chunks of `size` bytes; counts how many runs started, finished, and were CLOSED (cancel / closed socket / error)."""

    def __init__(self, chunks=3, size=3200, delay=0.0, fail=False):
        self.chunks, self.size, self.delay, self.fail = chunks, size, delay, fail
        self.started = self.finished = self.closed = 0
        self.texts_seen = 0

    async def synth_stream(self, text, voice, fmt, lang):
        self.started += 1
        self.texts_seen += len(text)
        try:
            for _ in range(self.chunks):
                if self.delay:
                    await asyncio.sleep(self.delay)
                if self.fail:
                    raise RuntimeError("engine broke")
                yield b"\xff" * self.size
            self.finished += 1
        finally:
            self.closed += 1


class FakeSTT:
    def __init__(self, text="placeholder", delay=0.0):
        self.text, self.delay, self.calls, self.sizes = text, delay, 0, []

    async def transcribe(self, pcm, rate, lang, prompt):
        self.calls += 1
        self.sizes.append(len(pcm))
        if self.delay:
            await asyncio.sleep(self.delay)
        return {"text": self.text, "language": lang or "auto"}


class ServerSide:
    """The server's end of an in-memory socket, recording the `cancel` messages it RECEIVES (a server-side counter: what reached the server, not what the client claims to have sent).
    `drop_cancel` (False; the mutation check sets it) makes the server ignore cancel messages, as a broken server would."""
    drop_cancel = False

    def __init__(self, ws):
        self.ws, self.cancels, self.hello_tokens = ws, [], []

    async def send(self, data):
        await self.ws.send(data)

    async def recv(self):
        while True:
            m = await self.ws.recv()
            if isinstance(m, str) and '"type":"hello"' in m:
                self.hello_tokens.append(P.parse(m).get("token"))             # kept in memory for the single-use check; never printed
            if isinstance(m, str) and '"type":"cancel"' in m:
                self.cancels.append(P.parse(m).get("seq"))
                if ServerSide.drop_cancel:
                    continue
            return m

    async def close(self):
        await self.ws.close()


def connector_for(engine, service="tts", secret=SECRET, limits=None, sem=None, replay=None):
    servers, sides = [], []

    async def connector(url):
        a, b = make_pair()
        side = ServerSide(b)
        sides.append(side)
        servers.append(asyncio.ensure_future(W.serve_connection(side, service=service, engine=engine, secret=secret, limits=limits, global_sem=sem, replay=replay)))
        return a
    connector.servers, connector.sides = servers, sides
    return connector


def assert_cancelled_by_message(conn, eng, connector, seq):
    """The cancel went through the FAST path, inside the grace period: the server received the cancel for THAT request id, acknowledged it, its engine was closed (not finished),
    no abort was needed, and the socket is still alive for the next request. (Closing the socket would also stop the engine, but only after `cancel_grace_s`: that is the fallback.)"""
    assert connector.sides[0].cancels == [seq]
    assert eng.closed == 1 and eng.finished == 0
    assert conn.counters["cancel_aborts"] == 0 and conn.counters["cancels"] == 1
    assert conn._ws is not None and not conn._dead and not conn._ws.closed


def conn_for(connector, service="tts", call="call-1", secret=SECRET, **cfg):
    base = dict(hb_interval_s=5.0, hb_timeout_s=12.0, cancel_grace_s=0.3, request_timeout_s=3.0, hello_timeout_s=1.0, connect_timeout_s=1.0)
    base.update(cfg)
    return PersistentConnection(service, "ws://127.0.0.1/ws", call, lambda: secret, connector, RtConfig(**base))


async def collect(conn, msg, **kw):
    out = []
    async for kind, d in conn.request(msg, **kw):
        out.append((kind, d))
    return out


SYNTH = {"type": "synth", "text": "w1 w2 w3,", "voice": "utkarsh", "format": "ulaw8k", "lang": "hi"}


async def test_a_synth_request_streams_frames_between_audio_start_and_audio_end():
    eng = FakeTTS(chunks=3, size=3200)
    conn = conn_for(connector_for(eng))
    ev = await collect(conn, SYNTH)
    kinds = [k for k, _ in ev]
    assert kinds[0] == "audio_start" and kinds[-1] == "audio_end" and kinds.count("frame") == 6          # 3 x 3200 bytes = 6 frames of 1600
    assert ev[-1][1]["bytes"] == 9600 and ev[-1][1]["audio_s"] == 1.2 and isinstance(ev[0][1]["queue_ms"], float)
    assert eng.started == eng.finished == eng.closed == 1
    await conn.close()


async def test_a_wrong_secret_is_an_auth_error_and_is_not_retried():
    conn = conn_for(connector_for(FakeTTS(), secret=b"the-server-secret"), secret=b"the-client-secret", max_connect_attempts=3)
    with pytest.raises(RtError) as e:
        await collect(conn, SYNTH)
    assert e.value.kind == "auth" and e.value.detail == "bad_signature" and conn.counters["connect_failures"] == 0


async def test_no_secret_at_all_is_an_auth_error_before_any_connection():
    called = []

    async def connector(url):
        called.append(url)
    conn = PersistentConnection("tts", "ws://127.0.0.1/x", "c", lambda: None, connector, RtConfig())
    with pytest.raises(RtError) as e:
        await conn.ensure_ready()
    assert e.value.kind == "auth" and called == []


async def test_a_connection_that_cannot_be_made_is_retried_with_backoff_then_fails():
    attempts = []

    async def refusing(url):
        attempts.append(1)
        raise ConnectionRefusedError()
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    conn = PersistentConnection("tts", "ws://127.0.0.1/x", "c", lambda: SECRET, refusing, RtConfig(max_connect_attempts=3), sleep=fake_sleep)
    with pytest.raises(RtError) as e:
        await conn.ensure_ready()
    assert e.value.kind == "connect" and len(attempts) == 3 and len(slept) == 2 and slept[1] > slept[0] * 1.2          # exponential, jittered


async def test_leaving_a_request_early_cancels_it_and_the_engine_is_really_stopped():
    eng = FakeTTS(chunks=50, size=3200, delay=0.01)
    cn = connector_for(eng)
    conn = conn_for(cn, cancel_grace_s=0.5)
    agen = conn.request(SYNTH)
    seen = []
    async for kind, d in agen:
        seen.append(kind)
        if kind == "frame":
            break
    seq = conn._seq
    await agen.aclose()                                              # the consumer walks away: the client sends cancel and waits for the acknowledgement
    await asyncio.sleep(0.05)                                        # well inside cancel_grace_s (0.5 s)
    assert eng.started == 1
    assert_cancelled_by_message(conn, eng, cn, seq)                  # the generator was CLOSED by the cancel message: that is what stops the GPU work
    assert len(await collect(conn, SYNTH)) > 2                       # the same connection serves the next request
    await conn.close()


async def test_cancelling_the_awaiting_task_cancels_the_request_on_the_server():
    """The call-level cancel (barge-in: the caller speaks over the reply and the awaiting task is cancelled)."""
    eng = FakeTTS(chunks=100, size=1600, delay=0.01)
    cn = connector_for(eng)
    conn = conn_for(cn, cancel_grace_s=0.5)
    task = asyncio.ensure_future(collect(conn, SYNTH))
    await asyncio.sleep(0.08)
    seq = conn._seq
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.15)                                        # less than cancel_grace_s: only the cancel MESSAGE can have stopped the engine by now
    assert_cancelled_by_message(conn, eng, cn, seq)
    await conn.close()


async def test_a_server_that_ignores_cancel_gets_its_socket_closed_and_the_next_request_reconnects():
    async def deaf_server(b):
        await b.recv()                                               # hello
        await b.send(P.dumps({"type": "ready", "v": P.VERSION}))
        while True:
            m = P.parse(await b.recv())
            if m["type"] == "synth":
                await b.send(P.dumps({"type": "audio_start", "seq": m["seq"], "queue_ms": 0.0, "format": "ulaw8k", "rate": 8000}))
            # cancel is ignored, ping is ignored

    sockets = []

    async def connector(url):
        a, b = make_pair()
        sockets.append(a)
        asyncio.ensure_future(deaf_server(b))
        return a
    conn = conn_for(connector, cancel_grace_s=0.05)
    agen = conn.request(SYNTH)
    async for kind, d in agen:
        if kind == "audio_start":
            break
    await agen.aclose()
    assert conn.counters["cancel_aborts"] == 1 and sockets[0].closed        # no acknowledgement: the socket is closed, which stops a real server's work
    await conn.ensure_ready()
    assert conn.counters["connects"] == 2 and len(sockets) == 2
    await conn.close()


async def test_a_missed_pong_closes_the_connection_and_fails_the_request_in_flight_without_replaying_it():
    async def mute_server(b):
        await b.recv()
        await b.send(P.dumps({"type": "ready", "v": P.VERSION}))
        while True:
            await b.recv()                                           # never answers anything

    async def connector(url):
        a, b = make_pair()
        asyncio.ensure_future(mute_server(b))
        return a
    conn = conn_for(connector, hb_interval_s=0.05, hb_timeout_s=0.12, request_timeout_s=5.0)
    with pytest.raises(RtError) as e:
        await collect(conn, SYNTH)
    assert e.value.kind == "lost" and e.value.detail == "heartbeat" and conn.counters["lost"] == 1
    await conn.close()


async def test_a_closed_server_side_fails_the_request_as_lost_and_the_next_one_reconnects():
    eng = FakeTTS(chunks=100, size=1600, delay=0.01)
    conn = conn_for(connector_for(eng))
    task = asyncio.ensure_future(collect(conn, SYNTH))
    await asyncio.sleep(0.05)
    await conn._ws.close()                                           # the network drops
    with pytest.raises(RtError) as e:
        await task
    assert e.value.kind == "lost"
    eng.chunks = 2
    assert len(await collect(conn, SYNTH)) > 2 and conn.counters["connects"] == 2
    await conn.close()


async def test_closing_the_socket_stops_the_engine_on_the_server_side():
    eng = FakeTTS(chunks=200, size=1600, delay=0.01)
    conn = conn_for(connector_for(eng))
    task = asyncio.ensure_future(collect(conn, SYNTH))
    await asyncio.sleep(0.05)
    await conn.abort("test")
    with pytest.raises(RtError):
        await task
    await asyncio.sleep(0.1)
    assert eng.started == 1 and eng.closed == 1 and eng.finished == 0


async def test_a_slow_consumer_fails_that_request_as_stalled_instead_of_blocking_the_socket():
    eng = FakeTTS(chunks=300, size=3200, delay=0.01)                     # still producing (about 3 s of work) when the stall is detected
    cn = connector_for(eng)
    conn = conn_for(cn, queue_max=2, stall_s=0.1, cancel_grace_s=0.5)
    agen = conn.request(SYNTH)
    first = await agen.__anext__()
    assert first[0] == "audio_start"
    seq = conn._seq
    await asyncio.sleep(0.2)                                         # the consumer does not read: the bounded queue fills and the reader gives up on it (stall at about 0.13 s)
    with pytest.raises(RtError) as e:
        async for _ in agen:
            pass
    assert e.value.kind == "stalled"
    await asyncio.sleep(0.15)                                        # about 0.35 s after the start, less than stall + cancel_grace_s: only the cancel MESSAGE can have stopped the engine
    assert_cancelled_by_message(conn, eng, cn, seq)                  # the server was told to stop producing for a consumer that is not there
    await conn.close()


async def test_a_request_that_gets_no_answer_times_out_and_is_cancelled():
    async def silent_server(b):
        await b.recv()
        await b.send(P.dumps({"type": "ready", "v": P.VERSION}))
        while True:
            m = P.parse(await b.recv())
            if m["type"] == "cancel":
                await b.send(P.dumps({"type": "cancelled", "seq": m["seq"]}))

    async def connector(url):
        a, b = make_pair()
        asyncio.ensure_future(silent_server(b))
        return a
    conn = conn_for(connector, request_timeout_s=0.1)
    with pytest.raises(RtError) as e:
        await collect(conn, SYNTH)
    assert e.value.kind == "timeout" and conn.counters["cancels"] == 1 and conn.counters["cancel_aborts"] == 0
    await conn.close()


async def test_an_engine_error_is_an_error_answer_and_the_connection_survives():
    eng = FakeTTS(fail=True)
    conn = conn_for(connector_for(eng))
    with pytest.raises(RtError) as e:
        await collect(conn, SYNTH)
    assert e.value.kind == "server_error" and e.value.detail == "engine_error"
    eng.fail = False
    assert (await collect(conn, SYNTH))[-1][0] == "audio_end"
    await conn.close()


async def test_oversize_text_and_a_bad_format_are_refused_by_the_server():
    conn = conn_for(connector_for(FakeTTS()))
    with pytest.raises(RtError) as e:
        await collect(conn, {**SYNTH, "text": "x" * (P.MAX_TEXT_CHARS + 1)})
    assert e.value.detail == "bad_request"
    with pytest.raises(RtError) as e2:
        await collect(conn, {**SYNTH, "format": "wav"})
    assert e2.value.detail == "bad_request"
    await conn.close()


async def test_the_server_refuses_a_second_request_while_one_runs_on_the_same_connection():
    eng = FakeTTS(chunks=30, size=1600, delay=0.02)
    conn = conn_for(connector_for(eng, limits=W.Limits(max_inflight=1)))
    first = asyncio.ensure_future(collect(conn, SYNTH))
    await asyncio.sleep(0.05)
    with pytest.raises(RtError) as e:
        await collect(conn, SYNTH)
    assert e.value.detail == "busy"
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await conn.close()


async def test_the_global_limit_serialises_model_runs_across_connections():
    eng = FakeTTS(chunks=3, size=1600, delay=0.05)
    sem = asyncio.Semaphore(1)
    c1, c2 = conn_for(connector_for(eng, sem=sem), call="a"), conn_for(connector_for(eng, sem=sem), call="b")
    t0 = asyncio.get_running_loop().time()
    r1, r2 = await asyncio.gather(collect(c1, SYNTH), collect(c2, SYNTH))
    assert asyncio.get_running_loop().time() - t0 >= 0.28                    # two runs of 0.15 s one after the other, not together
    qs = sorted(next(d["queue_ms"] for k, d in r if k == "audio_start") for r in (r1, r2))
    assert qs[0] < 50 and qs[1] > 100                                        # the second one reports the time it waited for a model slot
    await c1.close()
    await c2.close()


async def test_each_call_has_its_own_connection_and_the_server_sees_the_call_id():
    eng = FakeTTS()
    conns = [conn_for(connector_for(eng), call=f"call-{i}") for i in range(2)]
    await asyncio.gather(*(collect(c, SYNTH) for c in conns))
    assert conns[0]._ws is not conns[1]._ws and conns[0].call_id != conns[1].call_id
    for c in conns:
        await c.close()


async def test_a_token_for_another_call_is_refused():
    a, b = make_pair()
    srv = asyncio.ensure_future(W.serve_connection(b, service="tts", engine=FakeTTS(), secret=SECRET))
    await a.send(P.dumps({"type": "hello", "v": P.VERSION, "service": "tts", "call": "call-1", "token": P.make_token(SECRET, "call-2")}))
    reply = P.parse(await a.recv())
    assert reply == {"type": "error", "code": "call_mismatch"}
    stats = await asyncio.wait_for(srv, 1.0)
    assert stats["auth_failures"] == 1


async def test_a_hello_for_the_wrong_service_or_version_is_refused():
    for hello in ({"type": "hello", "v": 99, "service": "tts"}, {"type": "hello", "v": P.VERSION, "service": "stt"}, {"type": "ping"}):
        a, b = make_pair()
        srv = asyncio.ensure_future(W.serve_connection(b, service="tts", engine=FakeTTS(), secret=SECRET))
        await a.send(P.dumps({**hello, "call": "c", "token": P.make_token(SECRET, "c")}))
        assert P.parse(await a.recv())["code"] == "bad_hello"
        assert (await asyncio.wait_for(srv, 1.0))["auth_failures"] == 1


async def test_a_silent_client_is_dropped_at_the_hello_timeout():
    a, b = make_pair()
    stats = await asyncio.wait_for(W.serve_connection(b, service="tts", engine=FakeTTS(), secret=SECRET, limits=W.Limits(hello_timeout_s=0.1)), 1.0)
    assert stats["auth_failures"] == 1 and b.closed


async def test_stt_upload_is_assembled_from_frames_and_answered_with_a_final():
    eng = FakeSTT(text="placeholder")
    conn = conn_for(connector_for(eng, service="stt"), service="stt")
    pcm = b"\x01\x00" * 20000                                         # 40 KB: three 16 KB frames
    frames = [pcm[i:i + P.PCM_FRAME_BYTES] for i in range(0, len(pcm), P.PCM_FRAME_BYTES)]
    ev = await collect(conn, {"type": "transcribe", "rate": 8000, "lang": "hi", "nbytes": len(pcm)}, frames=frames, trailer={"type": "end"})
    kind, d = ev[-1]
    assert kind == "final" and d["text"] == "placeholder" and d["language"] == "hi" and eng.sizes == [len(pcm)]
    assert isinstance(d["queue_ms"], float) and isinstance(d["exec_ms"], float)
    await conn.close()


async def test_stt_refuses_audio_over_the_limit():
    eng = FakeSTT(delay=0.2)
    conn = conn_for(connector_for(eng, service="stt"), service="stt")
    with pytest.raises(RtError) as e:
        await collect(conn, {"type": "transcribe", "rate": 8000, "lang": None, "nbytes": P.MAX_AUDIO_BYTES + 1})
    assert e.value.detail == "audio_too_long" and eng.calls == 0
    await conn.close()


async def test_the_server_answers_ping_while_a_run_is_in_progress():
    eng = FakeTTS(chunks=40, size=1600, delay=0.02)
    a, b = make_pair()
    srv = asyncio.ensure_future(W.serve_connection(b, service="tts", engine=eng, secret=SECRET))
    await a.send(P.dumps({"type": "hello", "v": P.VERSION, "service": "tts", "call": "c", "token": P.make_token(SECRET, "c")}))
    assert P.parse(await a.recv())["type"] == "ready"
    await a.send(P.dumps({**SYNTH, "seq": 1}))
    await a.send(P.dumps({"type": "ping", "n": 7}))
    got_pong = False
    for _ in range(60):
        m = await asyncio.wait_for(a.recv(), 1.0)
        if isinstance(m, str) and P.parse(m).get("type") == "pong":
            got_pong = P.parse(m)["n"] == 7
            break
    assert got_pong and eng.finished == 0                              # answered long before the 0.8 s run could end
    await a.close()
    await asyncio.wait_for(srv, 1.0)
    assert eng.closed == 1


async def test_warmup_is_answered_without_model_work():
    eng = FakeTTS()
    conn = conn_for(connector_for(eng))
    ev = await collect(conn, {"type": "warmup"})
    assert ev[-1][0] == "warm" and eng.started == 0
    await conn.close()


# ---- a Pod reset changes its external TCP port (and, on Community Cloud, its IP): the client must find it again, not retry a dead address
def _reset_world():
    """A mock of the Pod's address book: only `live` accepts connections; every other address refuses, as the old port does after a reset."""
    world = {"live": "wss://pod.example:13007/ws", "dialled": []}
    good = connector_for(FakeTTS(chunks=2, size=1600))

    async def connector(url):
        world["dialled"].append(url)
        if url != world["live"]:
            raise ConnectionRefusedError()
        return await good(url)
    return world, connector


async def test_after_a_pod_reset_the_client_rediscovers_the_new_address_and_reconnects():
    world, connector = _reset_world()

    async def resolver():
        return world["live"]
    conn = PersistentConnection("tts", world["live"], "call-1", lambda: SECRET, connector, RtConfig(max_connect_attempts=2, connect_timeout_s=1.0, hello_timeout_s=1.0),
                                sleep=lambda s: asyncio.sleep(0), resolver=resolver)
    assert len(await collect(conn, SYNTH)) > 2 and conn.counters["rediscovered"] == 0
    await conn._ws.close()                                           # the Pod resets: the connection drops ...
    world["live"] = "wss://pod.example:13999/ws"                      # ... and comes back on a different external port
    await asyncio.sleep(0.05)
    assert len(await collect(conn, SYNTH)) > 2
    assert conn.url == "wss://pod.example:13999/ws" and conn.counters["rediscovered"] == 1 and conn.counters["connects"] == 2
    assert world["dialled"][-1] == "wss://pod.example:13999/ws"
    await conn.close()


async def test_without_a_resolver_the_old_address_is_retried_and_the_request_falls_over_as_a_connect_error():
    world, connector = _reset_world()
    conn = PersistentConnection("tts", world["live"], "call-1", lambda: SECRET, connector, RtConfig(max_connect_attempts=2), sleep=lambda s: asyncio.sleep(0))
    await collect(conn, SYNTH)
    await conn._ws.close()
    world["live"] = "wss://pod.example:13999/ws"
    await asyncio.sleep(0.05)
    with pytest.raises(RtError) as e:
        await collect(conn, SYNTH)
    assert e.value.kind == "connect" and conn.url == "wss://pod.example:13007/ws"
    await conn.close()


async def test_a_failing_or_nonsense_resolver_keeps_the_old_address_and_never_raises():
    for answer in (RuntimeError("lookup failed"), "http://not-a-websocket", None, ""):
        world, connector = _reset_world()

        async def resolver(answer=answer):
            if isinstance(answer, Exception):
                raise answer
            return answer
        conn = PersistentConnection("tts", world["live"], "c", lambda: SECRET, connector, RtConfig(max_connect_attempts=2), sleep=lambda s: asyncio.sleep(0), resolver=resolver)
        await collect(conn, SYNTH)
        await conn._ws.close()
        await asyncio.sleep(0.05)
        assert len(await collect(conn, SYNTH)) > 2 and conn.url == "wss://pod.example:13007/ws" and conn.counters["rediscovered"] == 0
        await conn.close()


async def test_the_address_is_also_rediscovered_between_attempts_of_one_failing_connect():
    world, connector = _reset_world()
    world["live"] = "wss://pod.example:13999/ws"                      # the configured address was already stale at the first connect

    async def resolver():
        return world["live"]
    conn = PersistentConnection("tts", "wss://pod.example:13007/ws", "c", lambda: SECRET, connector, RtConfig(max_connect_attempts=2), sleep=lambda s: asyncio.sleep(0), resolver=resolver)
    assert len(await collect(conn, SYNTH)) > 2 and conn.counters["connect_failures"] == 1 and conn.counters["rediscovered"] == 1
    await conn.close()

# ---- security: what a stranger who reaches the port (or the wire) can and cannot do
async def _stranger(engine, first_message, secret=SECRET):
    """Connect to the real worker protocol server WITHOUT the client class, send `first_message` (or nothing), and return (what the server sent back, whether it closed)."""
    a, b = make_pair()
    task = asyncio.ensure_future(W.serve_connection(b, service="tts", engine=engine, secret=secret, limits=W.Limits(hello_timeout_s=0.2)))
    if first_message is not None:
        await a.send(first_message if isinstance(first_message, (str, bytes)) else P.dumps(first_message))
    out = []
    try:
        while True:
            out.append(P.parse(await asyncio.wait_for(a.recv(), 1.0)))
    except (EOFError, asyncio.TimeoutError):
        pass
    await a.close()                                                      # a genuine session stays open until the client leaves; a refused one is already closed by the server
    stats = await asyncio.wait_for(task, 1.0)
    return out, stats


async def test_plain_ws_to_a_public_address_is_refused_before_any_byte_is_sent_unless_the_code_says_unsafe():
    dialled = []

    async def connector(url):
        dialled.append(url)
        raise ConnectionRefusedError()
    for url in ("ws://203.0.113.7:13007/ws", "ws://host.example/ws"):
        with pytest.raises(RtError) as e:
            PersistentConnection("tts", url, "c", lambda: SECRET, connector, RtConfig())
        assert e.value.kind == "connect" and e.value.detail == "plain_ws_to_non_loopback"
    PersistentConnection("tts", "wss://host.example/ws", "c", lambda: SECRET, connector, RtConfig())               # TLS: accepted
    PersistentConnection("tts", "ws://127.0.0.1:9/ws", "c", lambda: SECRET, connector, RtConfig())                # loopback (a private tunnel's local end): accepted
    PersistentConnection("tts", "ws://203.0.113.7/ws", "c", lambda: SECRET, connector, RtConfig(), unsafe_allow_plain_ws=True)    # an explicit argument in code only
    assert dialled == []
    assert "unsafe" not in RtConfig.__dataclass_fields__ and "plain" not in RtConfig.__dataclass_fields__        # nothing in the (config-derived) RtConfig can switch it on


async def test_a_resolver_cannot_move_the_client_to_a_plain_public_address():
    async def resolver():
        return "ws://203.0.113.7:13999/ws"
    conn = PersistentConnection("tts", "wss://pod.example:13007/ws", "c", lambda: SECRET, connector_for(FakeTTS()), RtConfig(), resolver=resolver)
    await conn._rediscover()
    assert conn.url == "wss://pod.example:13007/ws" and conn.counters["rediscovered"] == 0


async def test_a_stranger_who_sends_requests_without_a_hello_gets_an_error_and_is_dropped_and_no_engine_runs():
    eng = FakeTTS()
    for first in ({"type": "synth", "seq": 1, "text": "w1 w2 w3,", "voice": "v", "format": "ulaw8k"}, {"type": "ping"}, {"type": "warmup", "seq": 1}):
        out, stats = await _stranger(eng, first)
        assert [m["type"] for m in out] == ["error"] and out[0]["code"] == "bad_hello" and stats["auth_failures"] == 1
    assert eng.started == 0


async def test_a_stranger_with_garbage_binary_oversize_or_no_message_at_all_gets_nothing_and_no_engine_runs():
    eng = FakeTTS()
    for first in ("not json", b"\x00\x00\x00\x01xx", "x" * 5000, None):
        out, stats = await _stranger(eng, first)
        assert all(m["type"] == "error" for m in out) and stats["auth_failures"] == 1           # at most one error, then the socket is closed (the 0.2 s hello timeout for silence)
    assert eng.started == 0


async def test_a_hello_without_a_valid_token_is_rejected_whatever_is_wrong_with_it():
    eng = FakeTTS()
    good = P.make_token(SECRET, "call-1")
    other_call = P.make_token(SECRET, "call-2")
    expired = P.make_token(SECRET, "call-1", ttl_s=-5)
    wrong_secret = P.make_token(b"another-secret", "call-1")
    for token, code in ((good[:-1] + ("0" if good[-1] != "0" else "1"), "bad_signature"), (other_call, "call_mismatch"), (expired, "expired_token"), (wrong_secret, "bad_signature"), ("", "malformed_token"), ("a.b", "malformed_token")):
        out, stats = await _stranger(eng, {"type": "hello", "v": P.VERSION, "service": "tts", "call": "call-1", "token": token})
        assert [m["type"] for m in out] == ["error"] and out[0]["code"] == code and stats["auth_failures"] == 1
    assert eng.started == 0
    out, stats = await _stranger(eng, {"type": "hello", "v": P.VERSION, "service": "tts", "call": "call-1", "token": good})        # the genuine one is still accepted
    assert out[0]["type"] == "ready" and stats["auth_failures"] == 0


async def test_a_token_captured_on_the_wire_cannot_be_replayed_on_a_new_connection():
    eng = FakeTTS()
    replay = P.ReplayCache()
    tok = P.make_token(SECRET, "call-1")
    hello = {"type": "hello", "v": P.VERSION, "service": "tts", "call": "call-1", "token": tok}
    results = []
    for _ in range(2):
        a, b = make_pair()
        task = asyncio.ensure_future(W.serve_connection(b, service="tts", engine=eng, secret=SECRET, limits=W.Limits(hello_timeout_s=0.5), replay=replay))
        await a.send(P.dumps(hello))
        results.append(P.parse(await asyncio.wait_for(a.recv(), 1.0)))
        await a.close()
        await asyncio.wait_for(task, 1.0)
    assert results[0]["type"] == "ready" and results[1]["type"] == "error" and results[1]["code"] == "replayed_token"


async def test_the_server_refuses_to_listen_on_a_public_address_without_tls():
    with pytest.raises(SystemExit):
        W.check_bind("0.0.0.0", None)
    with pytest.raises(SystemExit):
        W.check_bind("203.0.113.7", None)
    W.check_bind("127.0.0.1", None)                                      # behind a private tunnel
    W.check_bind("0.0.0.0", object())                                    # with a TLS context
    W.check_bind("0.0.0.0", None, unsafe_plain=True)                     # only by an explicit command-line flag (serve_forever reads "--unsafe-plain" from argv, never the environment)

async def test_a_reconnect_after_a_dropped_connection_succeeds_with_a_fresh_single_use_token():
    """The server keeps a replay cache (single-use tokens): a client that reconnects must send a NEW token, not the one the dropped connection used."""
    replay = P.ReplayCache()
    cn = connector_for(FakeTTS(chunks=2, size=1600), replay=replay)
    conn = conn_for(cn)
    assert len(await collect(conn, SYNTH)) > 2
    await conn._ws.close()                                           # the network drops
    await asyncio.sleep(0.05)
    assert len(await collect(conn, SYNTH)) > 2 and conn.counters["connects"] == 2
    tokens = [t for side in cn.sides for t in side.hello_tokens]
    assert len(tokens) == 2 and tokens[0] != tokens[1]               # two connections, two different tokens, both accepted by a server that refuses replays
    assert P.verify_token(SECRET, tokens[0], "call-1", replay=replay) == "replayed_token"      # and the first one cannot be used again
    await conn.close()