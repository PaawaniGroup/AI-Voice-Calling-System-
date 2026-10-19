"""Media WebSockets (two wire protocols, one conversation loop).

 * `/v1/ai/sessions/{id}/media`   - the service's own protocol (documented in app/ai/media.py); synthetic-client tested.
 * `/v1/ai/media/fs`              - the protocol of FreeSWITCH `mod_audio_stream` (FreeSWITCH is the WebSocket CLIENT):
        headers  Authorization: Bearer <short-lived media token>      (set through channel var STREAM_EXTRA_HEADERS)
        text #1  {"session_id": "...", "sample_rate": 16000}          (the `metadata` argument of uuid_audio_stream start)
        binary   L16 mono frames of the caller (20 ms)
        reply    {"type":"streamAudio","data":{"audioDataType":"wav","sampleRate":R,"audioData":"<base64>"}}
                 -> the module writes a temp file and fires CUSTOM mod_audio_stream::play; the controller plays it with
                    `uuid_broadcast <uuid> <file> aleg`.  Barge-in -> `uuid_break <uuid> all`.

Energy-based endpointing + barge-in, bounded utterance queue. Verified with mock models; see docs/AI_MEDIA_ADAPTER.md.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect

from app.ai.clients import pcm16_to_wav, rms
from app.core.errors import ApiError
from app.security.auth import decode_token
from app.telephony import dialstrings as ds

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


class NativeIO:
    """Service protocol: audio frames and control messages over the same socket."""

    def __init__(self, ws: WebSocket, rate: int, frame_ms: int, realtime: bool):
        self.ws, self.rate, self.frame_ms, self.realtime = ws, rate, frame_ms, realtime

    async def error(self, code: str, fatal: bool = False) -> None:
        await self.ws.send_text(json.dumps({"type": "error", "code": code, "fatal": fatal}))

    async def interrupt(self) -> None:
        await self.ws.send_text(json.dumps({"type": "interrupt"}))

    async def play(self, audio: bytes, src_rate: int, language: str, uid: int, on_start) -> None:
        on_start(len(audio) / 2 / src_rate)
        await self.ws.send_text(json.dumps({"type": "playback_start", "utterance_id": uid, "sample_rate": self.rate, "language": language}))
        pcm = resample(audio, src_rate, self.rate)
        fb = self.rate * self.frame_ms // 1000 * 2
        for i in range(0, len(pcm), fb):
            await self.ws.send_bytes(pcm[i:i + fb])
            if self.realtime:
                await asyncio.sleep(self.frame_ms / 1000)
        await self.ws.send_text(json.dumps({"type": "playback_end", "utterance_id": uid}))


class FsIO:
    """mod_audio_stream protocol: the module cannot receive control messages, so errors are logged and barge-in is an ESL command."""

    def __init__(self, ws: WebSocket, esl, fs_uuid: str, rt=None):
        self.ws, self.esl, self.fs_uuid, self.rt = ws, esl, fs_uuid, rt

    async def error(self, code: str, fatal: bool = False) -> None:
        log.warning("ai media error %s", code)

    async def interrupt(self) -> None:
        try:
            await self.esl.api(f"uuid_break {ds.safe_uuid(self.fs_uuid)} all")
        except Exception:
            log.warning("uuid_break failed")

    async def play(self, audio: bytes, src_rate: int, language: str, uid: int, on_start) -> None:
        on_start(len(audio) / 2 / src_rate)  # FreeSWITCH plays the file asynchronously; we track an estimated end for barge-in
        self.rt.sent_at = time.monotonic()
        msg = {"type": "streamAudio", "data": {"audioDataType": "wav", "sampleRate": src_rate,
                                                "audioData": base64.b64encode(pcm16_to_wav(audio, src_rate)).decode()}}
        await self.ws.send_text(json.dumps(msg))


async def serve_loop(ws: WebSocket, ai, rt, io, rate: int, frame_ms: int, *, vad_rms: float, silence_ms: int) -> None:
    """Shared endpointing/barge-in/turn loop. Returns when the socket ends."""
    local: list[asyncio.Task] = []
    utterances: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2)
    st = {"playing": False, "uid": 0, "speech_ms": 0, "silence_ms": 0, "buf": bytearray(), "in_speech": False}
    play_task: dict[str, asyncio.Task | None] = {"t": None}

    def on_start(duration_s: float) -> None:
        st["playing"] = True
        loop = asyncio.get_running_loop()
        loop.call_later(duration_s + 0.3, lambda: st.__setitem__("playing", False))

    async def play(audio: bytes, src: int, lang: str, uid: int) -> None:
        try:
            await io.play(audio, src, lang, uid, on_start)
        finally:
            if isinstance(io, NativeIO):
                st["playing"] = False

    async def worker() -> None:
        while True:
            pcm = await utterances.get()
            t_turn = time.perf_counter()
            res = await rt.conversation.turn(pcm, rate)
            if res.timings_ms:
                res.timings_ms["turn_ms"] = round((time.perf_counter() - t_turn) * 1000, 1)  # utterance end -> reply ready
                await ai.record_turn(rt, res.timings_ms)
            if res.error and res.error != "cancelled":
                await io.error(res.error)
                continue
            if res.audio and not rt.conversation.cancelled:
                st["uid"] += 1
                play_task["t"] = asyncio.create_task(play(res.audio, res.sample_rate, res.language, st["uid"]))
                local.append(play_task["t"])

    local.append(asyncio.create_task(worker()))
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()
            if msg.get("text") is not None:
                try:
                    if json.loads(msg["text"]).get("type") == "bye":
                        await ai.stop(rt.tenant_id, rt.session_id, "client_bye")
                        return
                except ValueError:
                    pass
                continue
            frame = msg.get("bytes")
            if not frame:
                continue
            loud = rms(frame) >= vad_rms
            if loud:
                st["speech_ms"] += frame_ms
                st["silence_ms"] = 0
                if st["playing"] and st["speech_ms"] >= BARGE_IN_MS:  # caller talks over the assistant
                    rt.conversation.cancel()
                    if play_task["t"]:
                        play_task["t"].cancel()
                    st["playing"] = False
                    await io.interrupt()
                if st["speech_ms"] >= MIN_SPEECH_MS or st["in_speech"]:
                    st["in_speech"] = True
            else:
                st["silence_ms"] += frame_ms
                if not st["in_speech"]:
                    st["speech_ms"] = 0
            if st["in_speech"]:
                st["buf"] += frame
                if st["silence_ms"] >= silence_ms or len(st["buf"]) >= rate * 2 * MAX_UTTERANCE_MS // 1000:
                    utt = bytes(st["buf"])
                    st.update(buf=bytearray(), in_speech=False, speech_ms=0, silence_ms=0)
                    if utterances.full():  # backpressure: drop the oldest queued utterance
                        utterances.get_nowait()
                        await io.error("inbound_overflow")
                    utterances.put_nowait(utt)
    finally:
        for tk in local:
            tk.cancel()
        await asyncio.gather(*local, return_exceptions=True)


# ---------------------------------------------------------------------------------- native protocol
async def handle_media(ws: WebSocket, app, session_id: str, *, vad_rms: float = VAD_RMS, silence_ms: int = SILENCE_MS,
                       realtime: bool = True) -> None:
    await ws.accept()
    s, ai = app.state.settings, app.state.ai

    async def send(obj: dict) -> None:
        await ws.send_text(json.dumps(obj))

    rt = None
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
        await serve_loop(ws, ai, rt, NativeIO(ws, rate, frame_ms, realtime), rate, frame_ms, vad_rms=vad_rms, silence_ms=silence_ms)
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


# ---------------------------------------------------------------------------------- mod_audio_stream protocol
async def handle_fs_media(ws: WebSocket, app, *, vad_rms: float = VAD_RMS, silence_ms: int = SILENCE_MS) -> None:
    s, ai = app.state.settings, app.state.ai
    auth = ws.headers.get("authorization", "")
    scheme, _, tok = auth.partition(" ")
    try:
        if scheme.lower() != "bearer":
            raise ApiError("no token")
        p = decode_token(tok.strip(), s)
        if not any(p.has(r) for r in (s.role_user, s.role_admin, s.role_platform)):
            raise ApiError("forbidden")
    except ApiError:
        return await ws.close(code=4401)  # refused during the handshake
    await ws.accept()
    rt = None
    try:
        meta = json.loads(await asyncio.wait_for(ws.receive_text(), 5))
        sid = str(meta["session_id"])
        rate = int(meta.get("sample_rate", 16000))
        if rate not in (8000, 16000):
            return await ws.close(code=4400)
        rt = await ai.attach_media(p.tenant_id, sid)  # tenant from the token; other tenants get 'no AI session'
        call = await app.state.repos.get_call((await ai.sessions.get(p.tenant_id, sid))["call_id"], p.tenant_id)
        rt.ws = ws
        await ai.media_connected(rt, {"sample_rate": rate, "frame_ms": 20, "flavor": "mod_audio_stream"})
        io = FsIO(ws, app.state.controller.esl_cmd, call["fs_uuid"], rt)
        await serve_loop(ws, ai, rt, io, rate, 20, vad_rms=vad_rms, silence_ms=silence_ms)
    except WebSocketDisconnect:
        if rt:
            await ai.media_disconnected(rt)
    except (asyncio.TimeoutError, json.JSONDecodeError, ValueError, KeyError, TypeError, ApiError):
        try:
            await ws.close(code=4400)
        except Exception:
            pass
        if rt and rt.ws is not None:
            await ai.media_disconnected(rt)
    except RuntimeError:
        pass
