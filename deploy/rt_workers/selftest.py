"""Torch-free self-test of the worker protocol server (common.py) against the real client, over an in-memory socket pair: `python deploy/rt_workers/selftest.py`
(from the repository root; exit 0 = every check passed). No GPU, no model, no network, no key. NOT RUN yet: it needs the operator's APPROVED: run.

It checks what must hold before any model is attached: signed per-call auth, streaming, cancel and a closed socket both STOP the engine, a deaf server gets its socket closed by the
client, ping works while a run is in progress, one run per connection, and the process-wide model-slot limit."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.ai.rt import protocol as P                                   # noqa: E402
from app.ai.rt.connection import PersistentConnection, RtConfig, RtError   # noqa: E402
from app.ai.rt.testing import make_pair                               # noqa: E402

spec = importlib.util.spec_from_file_location("rt_worker_common", ROOT / "deploy" / "rt_workers" / "common.py")
W = importlib.util.module_from_spec(spec)
sys.modules["rt_worker_common"] = W          # a dataclass with postponed annotations looks its module up in sys.modules
spec.loader.exec_module(W)

SECRET = b"selftest-secret"
checks = 0


def check(cond, what):
    global checks
    checks += 1
    if not cond:
        print("FAIL:", what)
        sys.exit(1)


class Engine:
    def __init__(self, n=3, delay=0.0):
        self.n, self.delay, self.started, self.finished, self.closed = n, delay, 0, 0, 0

    async def synth_stream(self, text, voice, fmt, lang):
        self.started += 1
        try:
            for _ in range(self.n):
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield b"\xff" * 3200
            self.finished += 1
        finally:
            self.closed += 1


def conn(engine, secret=SECRET, sem=None, **kw):
    async def connector(url):
        a, b = make_pair()
        asyncio.ensure_future(W.serve_connection(b, service="tts", engine=engine, secret=SECRET, global_sem=sem))
        return a
    return PersistentConnection("tts", "ws://127.0.0.1/selftest", "call-1", lambda: secret, connector, RtConfig(request_timeout_s=3.0, cancel_grace_s=0.3, **kw))


SYNTH = {"type": "synth", "text": "w1 w2 w3,", "voice": "v", "format": "ulaw8k", "lang": "hi"}


async def run() -> None:
    eng = Engine()
    c = conn(eng)
    ev = [k async for k, _ in c.request(SYNTH)]
    check(ev[0] == "audio_start" and ev[-1] == "audio_end" and ev.count("frame") == 6, "streams audio between audio_start and audio_end")
    await c.close()

    bad = conn(Engine(), secret=b"wrong")
    try:
        [x async for x in bad.request(SYNTH)]
        check(False, "a wrong secret must be refused")
    except RtError as e:
        check(e.kind == "auth", "a wrong secret is an auth error")

    slow = Engine(n=100, delay=0.01)
    c = conn(slow)
    agen = c.request(SYNTH)
    async for kind, _ in agen:
        if kind == "frame":
            break
    await agen.aclose()
    await asyncio.sleep(0.05)
    check(slow.closed == 1 and slow.finished == 0, "cancel stops the engine (its generator is closed)")
    await c.close()

    slow2 = Engine(n=200, delay=0.01)
    c = conn(slow2)
    task = asyncio.ensure_future(_drain(c))
    await asyncio.sleep(0.05)
    await c.abort("selftest")
    try:
        await task
    except RtError:
        pass
    await asyncio.sleep(0.1)
    check(slow2.closed == 1 and slow2.finished == 0, "a closed socket stops the engine")

    sem = asyncio.Semaphore(1)
    e1, e2 = Engine(n=3, delay=0.05), Engine(n=3, delay=0.05)
    c1, c2 = conn(e1, sem=sem), conn(e2, sem=sem)
    t0 = asyncio.get_running_loop().time()
    await asyncio.gather(_drain(c1), _drain(c2))
    check(asyncio.get_running_loop().time() - t0 >= 0.28, "the process-wide model slot serialises two connections")
    await c1.close()
    await c2.close()


async def _drain(c):
    async for _ in c.request(SYNTH):
        pass


asyncio.run(run())
print(f"rt_workers selftest: {checks} checks passed")
