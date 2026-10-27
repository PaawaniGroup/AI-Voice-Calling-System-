"""Protocol server of the persistent real-time transport: ONE function, `serve_connection`, shared by the STT and voice servers. Engine-agnostic and torch-free: the models plug
in as `Engine` objects, so this file (the part that decides auth, cancellation, limits and isolation) is tested without a GPU.

Per connection (= one call, one service): hello with a signed short-lived token bound to the call id, then requests. The server
  * never logs text, audio or tokens: lengths, timings and error codes only;
  * runs at most `limits.max_inflight` requests of one connection (default 1) and at most `limits.max_global` across connections (a semaphore shared by the process);
  * stops a generation on `cancel` AND when the connection closes (the engine's async generator is closed, which is what must stop the GPU work);
  * answers `ping` at any time, also while a generation runs;
  * refuses oversize text or audio and unknown message types with `error`.
The protocol helpers come from rt_protocol.py (a copy of app/ai/rt/protocol.py placed next to this file at build time) or, in the repository, from app.ai.rt.protocol.

Engines:
  TTS engine   `async def synth_stream(text, voice, fmt, lang) -> AsyncIterator[bytes]`   (ulaw8k chunks, first chunk as early as the model produces it)
  STT engine   `async def transcribe(pcm, rate, lang, prompt) -> dict`                    ({"text", "language"})
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

try:                                    # in the worker image the protocol file sits next to this one
    import rt_protocol as P             # type: ignore
except ImportError:                     # in the repository
    from app.ai.rt import protocol as P

log = logging.getLogger("rt_worker")


@dataclass
class Limits:
    hello_timeout_s: float = 5.0
    max_inflight: int = 1
    max_global: int = 4                 # simultaneous model runs in the whole process (ESTIMATE: set from the concurrency probe)
    idle_timeout_s: float = 300.0       # a connection with no message for this long is closed


def _closed(exc: BaseException) -> bool:
    return isinstance(exc, (EOFError, ConnectionError)) or "ConnectionClosed" in type(exc).__name__


async def serve_connection(ws, *, service: str, engine, secret: bytes, limits: Limits | None = None, global_sem: asyncio.Semaphore | None = None, clock=time.monotonic) -> dict:
    """Serve one connection until it closes. Returns counters (numbers only). `ws` needs `send(str|bytes)`, `recv()`, `close()`."""
    limits = limits or Limits()
    sem = global_sem or asyncio.Semaphore(limits.max_global)
    stats = {"requests": 0, "cancelled": 0, "errors": 0, "auth_failures": 0}
    tasks: dict[int, asyncio.Task] = {}
    uploads: dict[int, dict] = {}           # STT: seq -> {"meta", "buf"}

    async def send(msg: dict) -> None:
        await ws.send(P.dumps(msg))

    async def fail(seq: int | None, code: str) -> None:
        stats["errors"] += 1
        await send({"type": "error", **({"seq": seq} if seq is not None else {}), "code": code})

    # ------------------------------------------------------------------ hello
    try:
        first = P.parse(await asyncio.wait_for(ws.recv(), limits.hello_timeout_s))
    except Exception:
        stats["auth_failures"] += 1
        await _quiet(ws.close())
        return stats
    if first["type"] != "hello" or first.get("service") != service or first.get("v") != P.VERSION:
        stats["auth_failures"] += 1
        await fail(None, "bad_hello")
        await _quiet(ws.close())
        return stats
    bad = P.verify_token(secret, str(first.get("token", "")), str(first.get("call", "")))
    if bad:
        stats["auth_failures"] += 1
        await fail(None, bad)
        await _quiet(ws.close())
        return stats
    await send({"type": "ready", "v": P.VERSION})

    # ------------------------------------------------------------------ work
    async def run_synth(seq: int, m: dict) -> None:
        text = m.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > P.MAX_TEXT_CHARS or m.get("format") != "ulaw8k":
            await fail(seq, "bad_request")
            return
        t_in = clock()
        async with sem:                                           # a model slot of the whole process
            queue_ms = round((clock() - t_in) * 1000, 1)
            await send({"type": "audio_start", "seq": seq, "queue_ms": queue_ms, "format": "ulaw8k", "rate": P.ULAW_RATE})
            t0, total = clock(), 0
            gen = engine.synth_stream(text, str(m.get("voice") or ""), "ulaw8k", str(m.get("lang") or ""))
            try:
                async for chunk in gen:
                    total += len(chunk)
                    for i in range(0, len(chunk), P.ULAW_FRAME_BYTES):
                        await ws.send(P.pack_frame(seq, chunk[i:i + P.ULAW_FRAME_BYTES]))
            finally:
                aclose = getattr(gen, "aclose", None)
                if aclose is not None:
                    await _quiet(aclose())                        # closing the generator is what stops the model: on cancel, on a closed socket, on error
        await send({"type": "audio_end", "seq": seq, "exec_ms": round((clock() - t0) * 1000, 1), "audio_s": round(total / P.ULAW_RATE, 3), "bytes": total})

    async def run_transcribe(seq: int) -> None:
        up = uploads.pop(seq)
        t_in = clock()
        async with sem:
            queue_ms = round((clock() - t_in) * 1000, 1)
            t0 = clock()
            out = await engine.transcribe(bytes(up["buf"]), int(up["meta"].get("rate") or 8000), up["meta"].get("lang"), up["meta"].get("prompt"))
        await send({"type": "final", "seq": seq, "text": str(out.get("text", "")), "language": out.get("language"), "queue_ms": queue_ms,
                    "exec_ms": round((clock() - t0) * 1000, 1)})

    def spawn(seq: int, coro) -> None:
        async def wrapper():
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:                                # a failed request must not kill the connection
                log.warning("request failed: %s", type(e).__name__)
                await _quiet(fail(seq, "engine_error"))
            finally:
                tasks.pop(seq, None)
        tasks[seq] = asyncio.ensure_future(wrapper())

    # ------------------------------------------------------------------ message loop
    last = clock()
    try:
        while True:
            try:
                m = await asyncio.wait_for(ws.recv(), max(0.01, limits.idle_timeout_s - (clock() - last)))
            except asyncio.TimeoutError:
                break
            last = clock()
            if isinstance(m, (bytes, bytearray)):
                try:
                    seq, payload = P.unpack_frame(bytes(m))
                except P.ProtocolError:
                    await fail(None, "bad_frame")
                    continue
                up = uploads.get(seq)
                if up is None or len(up["buf"]) + len(payload) > P.MAX_AUDIO_BYTES:
                    await fail(seq, "bad_frame" if up is None else "audio_too_long")
                    uploads.pop(seq, None)
                    continue
                up["buf"] += payload
                continue
            try:
                d = P.parse(m)
            except P.ProtocolError:
                await fail(None, "bad_message")
                continue
            t, seq = d["type"], d.get("seq")
            if t == "ping":
                await send({"type": "pong", "n": d.get("n")})
            elif t == "warmup":
                await send({"type": "warm", "seq": seq})
            elif t == "cancel" and isinstance(seq, int):
                task = tasks.pop(seq, None)
                uploads.pop(seq, None)
                if task is not None:
                    task.cancel()
                    await asyncio.wait({task}, timeout=0.5)               # wait until the run has really unwound (its generator closed), not for ever
                stats["cancelled"] += 1
                await send({"type": "cancelled", "seq": seq})
            elif t == "synth" and service == "tts" and isinstance(seq, int):
                if len(tasks) >= limits.max_inflight:
                    await fail(seq, "busy")
                    continue
                stats["requests"] += 1
                spawn(seq, run_synth(seq, d))
            elif t == "transcribe" and service == "stt" and isinstance(seq, int):
                if len(tasks) + len(uploads) >= limits.max_inflight or int(d.get("nbytes") or 0) > P.MAX_AUDIO_BYTES:
                    await fail(seq, "busy" if int(d.get("nbytes") or 0) <= P.MAX_AUDIO_BYTES else "audio_too_long")
                    continue
                stats["requests"] += 1
                uploads[seq] = {"meta": d, "buf": bytearray()}
            elif t == "end" and service == "stt" and seq in uploads:
                spawn(seq, run_transcribe(seq))
            else:
                await fail(seq if isinstance(seq, int) else None, "unknown_type")
    except Exception as e:
        if not _closed(e):
            log.warning("connection error: %s", type(e).__name__)
    finally:
        for t in list(tasks.values()):                            # the connection is gone: stop every run that belongs to it
            t.cancel()
        if tasks:
            await _quiet(asyncio.gather(*tasks.values(), return_exceptions=True))
        await _quiet(ws.close())
    return stats


async def _quiet(aw) -> None:
    try:
        await aw
    except BaseException as e:           # noqa: BLE001 - cleanup must never raise; cancellation of the caller is re-raised below
        if isinstance(e, asyncio.CancelledError):
            raise
