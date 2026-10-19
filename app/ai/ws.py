"""Media WebSocket handler (protocol in app/ai/media.py).

Energy-based endpointing + barge-in, bounded utterance queue, paced playback. Verified ONLY with a synthetic
client and mock model servers; not verified with FreeSWITCH audio or the real models.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from app.ai.clients import rms
from app.core.errors import ApiError
from app.security.auth import decode_token

log = logging.getLogger("mme_telephony.ai.ws")

VAD_RMS = 500.0        # 16-bit PCM RMS above which a frame counts as speech (tunable per deployment)
SILENCE_MS = 700       # trailing silence that ends an utterance
MIN_SPEECH_MS = 200
BARGE_IN_MS = 120
MAX_UTTERANCE_MS = 15000


def resample(pcm: bytes, src: int, dst: int) -> bytes:
    if src == dst or not pcm:
        return pcm
    import struct
    n = len(pcm) // 2
    xs = struct.unpack("<%dh" % n, pcm[: n * 2])
    m = max(1, int(n * dst / src))
    out = []
    for i in range(m):
        pos = i * (n - 1) / max(1, m - 1) if m > 1 else 0
        i0 = int(pos)
        i1 = min(n - 1, i0 + 1)
        frac = pos - i0
        out.append(int(xs[i0] * (1 - frac) + xs[i1] * frac))
    return struct.pack("<%dh" % len(out), *out)


async def handle_media(ws: WebSocket, app, session_id: str, *, vad_rms: float = VAD_RMS, silence_ms: int = SILENCE_MS,
                       realtime: bool = True) -> None:
    await ws.accept()
    s, ai = app.state.settings, app.state.ai

    async def send(obj: dict) -> None:
        await ws.send_text(json.dumps(obj))

    rt = None
    local: list[asyncio.Task] = []  # tasks owned by THIS connection; always cancelled when it ends
    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), 5))
        if first.get("type") != "auth":
            await send({"type": "error", "code": "auth_required", "fatal": True})
            return await ws.close(code=4401)
        try:
            p = decode_token(first.get("token", ""), s)
        except ApiError:
            await send({"type": "error", "code": "unauthorized", "fatal": True})
            return await ws.close(code=4401)
        if not any(p.has(r) for r in (s.role_user, s.role_admin, s.role_platform)):
            await send({"type": "error", "code": "forbidden", "fatal": True})
            return await ws.close(code=4403)
        try:
            rt = await ai.attach_media(p.tenant_id, session_id)  # tenant comes from the token only
        except ApiError:
            await send({"type": "error", "code": "no_ai_session", "fatal": True})
            return await ws.close(code=4404)
        await send({"type": "auth_ok"})
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), 5))
        rate = int(hello.get("sample_rate", 0))
        frame_ms = int(hello.get("frame_ms", 20))
        if hello.get("type") != "hello" or hello.get("encoding") != "pcm_s16le" or rate not in (8000, 16000) \
                or hello.get("channels", 1) != 1 or frame_ms not in (10, 20, 30, 40):
            await send({"type": "error", "code": "unsupported_format", "fatal": True})
            return await ws.close(code=4400)
        rt.ws = ws
        await ai.media_connected(rt, {"sample_rate": rate, "frame_ms": frame_ms})
        await send({"type": "ready"})

        frame_bytes = rate * frame_ms // 1000 * 2
        utterances: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2)
        state = {"playing": False, "uid": 0, "speech_ms": 0, "silence_ms": 0, "buf": bytearray(), "in_speech": False, "overflow": False}
        play_task: dict[str, asyncio.Task | None] = {"t": None}

        async def play(audio: bytes, src_rate: int, language: str, uid: int) -> None:
            state["playing"] = True
            try:
                await send({"type": "playback_start", "utterance_id": uid, "sample_rate": rate, "language": language})
                pcm = resample(audio, src_rate, rate)
                for i in range(0, len(pcm), frame_bytes):
                    await ws.send_bytes(pcm[i:i + frame_bytes])
                    if realtime:
                        await asyncio.sleep(frame_ms / 1000)
                await send({"type": "playback_end", "utterance_id": uid})
            finally:
                state["playing"] = False

        async def worker() -> None:
            while True:
                pcm = await utterances.get()
                res = await rt.conversation.turn(pcm, rate)
                if res.timings_ms:
                    await ai.record_turn(rt, res.timings_ms)
                if res.error and res.error != "cancelled":
                    await send({"type": "error", "code": res.error, "fatal": False})
                    continue
                if res.audio and not rt.conversation.cancelled:
                    state["uid"] += 1
                    play_task["t"] = asyncio.create_task(play(res.audio, res.sample_rate, res.language, state["uid"]))
                    local.append(play_task["t"])

        wt = asyncio.create_task(worker())
        local.append(wt)

        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()
            if msg.get("text") is not None:
                t = json.loads(msg["text"])
                if t.get("type") == "bye":
                    await ai.stop(rt.tenant_id, rt.session_id, "client_bye")
                    return
                continue
            frame = msg.get("bytes")
            if not frame:
                continue
            loud = rms(frame) >= vad_rms
            if loud:
                state["speech_ms"] += frame_ms
                state["silence_ms"] = 0
                if state["playing"] and state["speech_ms"] >= BARGE_IN_MS:  # caller interrupts the assistant
                    rt.conversation.cancel()
                    if play_task["t"]:
                        play_task["t"].cancel()
                    state["playing"] = False
                    await send({"type": "interrupt"})
                if state["speech_ms"] >= MIN_SPEECH_MS or state["in_speech"]:
                    state["in_speech"] = True
            else:
                state["silence_ms"] += frame_ms
                if not state["in_speech"]:
                    state["speech_ms"] = 0
            if state["in_speech"]:
                state["buf"] += frame
                if state["silence_ms"] >= silence_ms or len(state["buf"]) >= rate * 2 * MAX_UTTERANCE_MS // 1000:
                    utt = bytes(state["buf"])
                    state.update(buf=bytearray(), in_speech=False, speech_ms=0, silence_ms=0)
                    if utterances.full():  # backpressure: drop the oldest queued utterance, tell the client once
                        utterances.get_nowait()
                        await send({"type": "error", "code": "inbound_overflow", "fatal": False})
                    utterances.put_nowait(utt)
    except WebSocketDisconnect:
        if rt:
            await ai.media_disconnected(rt)
    except (asyncio.TimeoutError, json.JSONDecodeError, ValueError, KeyError):
        try:
            await send({"type": "error", "code": "protocol_error", "fatal": True})
            await ws.close(code=4400)
        except Exception:
            pass
        if rt and rt.ws is not None:
            await ai.media_disconnected(rt)
    except RuntimeError:  # socket already closed by a stop()
        pass
    finally:
        for tk in local:
            tk.cancel()
        if local:
            await asyncio.gather(*local, return_exceptions=True)
