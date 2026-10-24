"""Media WebSockets (three wire protocols, one conversation loop).

 * `/v1/ai/sessions/{id}/media`   - the service's own protocol (documented in app/ai/media.py); synthetic-client tested.
 * `/v1/ai/media/fs`              - the protocol of FreeSWITCH `mod_audio_stream` (FreeSWITCH is the WebSocket CLIENT):
        headers  Authorization: Bearer <short-lived media token>      (set through channel var STREAM_EXTRA_HEADERS)
        text #1  {"session_id": "...", "sample_rate": 16000}          (the `metadata` argument of uuid_audio_stream start)
        binary   L16 mono frames of the caller (20 ms)
        reply    {"type":"streamAudio","data":{"audioDataType":"wav","sampleRate":R,"audioData":"<base64>"}}
                 -> the module writes a temp file and fires CUSTOM mod_audio_stream::play; the controller plays it with
                    `uuid_broadcast <uuid> <file> aleg`.  Barge-in -> `uuid_break <uuid> all`.
 * `/v1/providers/plivo/stream`   - Plivo Voice API `<Stream bidirectional="true">` (Plivo is the WebSocket CLIENT). NOT
   interchangeable with the two above: JSON text frames only (start/media/dtmf/playedStream/clearedAudio/stop), caller audio is
   base64 G.711 mu-law 8 kHz, replies are `playAudio` events (mu-law 8 kHz) + `checkpoint`, barge-in is `clearAudio`.
   `PlivoIO` below is the explicit adapter between that protocol and the shared `serve_loop` (PCM16 in, PCM16 out).

Energy-based endpointing + barge-in, bounded utterance queue. Verified with mock models and mock Plivo traffic; see
docs/AI_MEDIA_ADAPTER.md and docs/PLIVO_VOICE_APPLICATION.md.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect

from app.ai import codec
from app.ai.clients import pcm16_to_wav, rms
from app.core.errors import ApiError
from app.core.ids import utcnow
from app.observability import metrics as m
from app.providers import plivo as plivo_mod
from app.security import tickets
from app.security.auth import decode_token
from app.telephony import dialstrings as ds

log = logging.getLogger("mme_telephony.ai.ws")

VAD_RMS = 450.0        # 16-bit PCM RMS above which a frame counts as speech (avoids telephone line noise)
BARGE_IN_VAD_RMS = 1000.0   # Higher RMS needed to barge in over speaker audio / acoustic bleed
BARGE_IN_RMS_MULTIPLIER = 2.2  # Multiplier for barge-in threshold over vad_rms
SILENCE_MS = 700       # trailing silence that ends an utterance (21 speech + 35 silence frames)
MIN_SPEECH_MS = 200    # minimum speech gate before speech accumulation begins
BARGE_IN_MS = 200      # minimum continuous loud speech to interrupt assistant playback (prevents self-breaks)
MAX_UTTERANCE_MS = 6000 # 6s max utterance cap prevents huge buffering and latency spikes
IDLE_POLL_S = 0.05     # tighter 50ms gap polling for fast silence accounting
PLIVO_PLAY_CHUNK = 8000        # mu-law bytes per playAudio (1 s => ~10.7 KB base64, under Plivo's 16 KB guidance)
PLIVO_MAX_PLAY_S = 50          # Plivo queues ~60 s of audio; never send more than this per utterance
PLIVO_MAX_MESSAGE = 65536      # Plivo guidance: messages up to 64 KB


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


class PlivoIO:
    """Adapter: Plivo bidirectional-stream JSON events <-> the shared conversation loop (PCM16).

    Inbound  `media` {track:"inbound", chunk, timestamp, payload: base64 mu-law 8 kHz} -> ("audio", PCM16 8 kHz)
             duplicate / out-of-order `chunk` numbers are dropped; undecodable or oversize payloads are counted and dropped.
    Outbound `playAudio` {media:{contentType:"audio/x-mulaw", sampleRate:8000, payload}} in <=1 s chunks, then `checkpoint`
             {streamId, name}; barge-in sends `clearAudio` {streamId}.
    UNVERIFIED with a real Plivo call: the exact `contentType` spelling Plivo accepts for playAudio (its reference shows
    "audio/x-mulaw" with a separate sampleRate in the example, "audio/x-mulaw;rate=8000" in the Stream attribute).
    """

    rate = codec.MULAW_RATE

    def __init__(self, ws: WebSocket, stream_id: str):
        self.ws, self.stream_id = ws, stream_id
        self.last_chunk = -1
        self.counters = {"media": 0, "bytes": 0, "dup": 0, "bad": 0, "dtmf": 0, "cleared": 0, "oversize": 0}
        self.played: list[str] = []
        self.sent_checkpoints: list[str] = []

    async def next_item(self, timeout: float):
        try:
            msg = await asyncio.wait_for(self.ws.receive(), timeout)
        except asyncio.TimeoutError:
            return ("idle", int(timeout * 1000))
        if msg["type"] == "websocket.disconnect":
            raise WebSocketDisconnect()
        raw = msg.get("text")
        if raw is None:
            return ("skip", None)  # Plivo uses text frames only
        if len(raw) > PLIVO_MAX_MESSAGE:
            self.counters["oversize"] += 1
            return ("end", "message_too_large")
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            self.counters["bad"] += 1
            return ("skip", None)
        kind = ev.get("event") if isinstance(ev, dict) else None
        if kind == "media":
            media = ev.get("media") if isinstance(ev.get("media"), dict) else {}
            if media.get("track", "inbound") != "inbound":
                return ("skip", None)
            try:
                chunk = int(media.get("chunk", self.last_chunk + 1))
                mu = codec.decode_media_payload(media.get("payload"))
            except (ValueError, TypeError, codec.CodecError):
                self.counters["bad"] += 1
                return ("skip", None)
            if chunk <= self.last_chunk:  # duplicate or out-of-order media must not be fed twice
                self.counters["dup"] += 1
                return ("skip", None)
            self.last_chunk = chunk
            self.counters["media"] += 1
            self.counters["bytes"] += len(mu)
            return ("audio", codec.mulaw_to_pcm16(mu))
        if kind == "dtmf":
            self.counters["dtmf"] += 1  # digits are deliberately never logged or stored
        elif kind == "playedStream":
            self.played.append(str(ev.get("name", ""))[:64])
        elif kind == "clearedAudio":
            self.counters["cleared"] += 1
        elif kind == "stop":
            return ("end", "stream_stopped")
        elif kind == "start":
            return ("end", "duplicate_start")  # a second start on one socket is a protocol violation
        return ("skip", None)

    async def error(self, code: str, fatal: bool = False) -> None:
        log.warning("plivo media error %s", code)  # Plivo has no error message to the caller; nothing is sent

    async def interrupt(self) -> None:
        await self.ws.send_text(json.dumps({"event": "clearAudio", "streamId": self.stream_id}))

    async def play(self, audio: bytes, src_rate: int, language: str, uid: int, on_start) -> None:
        mu = codec.pcm16_to_mulaw(codec.resample_pcm16(audio, src_rate, codec.MULAW_RATE))
        limit = PLIVO_MAX_PLAY_S * codec.MULAW_RATE
        if len(mu) > limit:
            log.warning("reply audio truncated to %ss for Plivo's playback queue", PLIVO_MAX_PLAY_S)
            mu = mu[:limit]
        on_start(len(mu) / codec.MULAW_RATE)
        for part in codec.chunks(mu, PLIVO_PLAY_CHUNK):
            await self.ws.send_text(json.dumps({"event": "playAudio", "media": {
                "contentType": "audio/x-mulaw", "sampleRate": codec.MULAW_RATE, "payload": base64.b64encode(part).decode()}}))
        name = f"utt-{uid}"
        self.sent_checkpoints.append(name)
        await self.ws.send_text(json.dumps({"event": "checkpoint", "streamId": self.stream_id, "name": name}))


async def serve_loop(ws: WebSocket, ai, rt, io, rate: int, frame_ms: int, *, vad_rms: float, silence_ms: int) -> str | None:
    """Shared endpointing/barge-in/turn loop. Returns an end reason when the transport signalled one, else None.

    The item source is `io.next_item(timeout)` when the adapter has one (Plivo), otherwise the raw WebSocket. A gap with no
    frames counts as silence, so an utterance still ends if the transport stops sending when the caller is quiet.
    """
    local: list[asyncio.Task] = []
    utterances: asyncio.Queue[bytes] = asyncio.Queue(maxsize=3)
    st = {
        "playing": False,
        "play_until": 0.0,
        "play_started_at": 0.0,
        "uid": 0,
        "speech_ms": 0,
        "silence_ms": 0,
        "buf": bytearray(),
        "in_speech": False,
    }

    def on_start(duration_s: float) -> None:
        now = time.monotonic()
        base = max(now, st["play_until"])
        st["play_until"] = base + duration_s
        st["playing"] = True
        st["play_started_at"] = now

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
            first = True
            try:
                async for chunk in rt.conversation.stream_turn(pcm, rate):
                    if rt.conversation.cancelled:
                        break
                    if first and (chunk.timings_ms or chunk.heard or chunk.reply):
                        chunk.timings_ms["turn_ms"] = round((time.perf_counter() - t_turn) * 1000, 1)
                        if not chunk.error:
                            m.AI_TURN_SECONDS.observe(chunk.timings_ms["turn_ms"] / 1000)
                        await ai.record_turn(rt, chunk.timings_ms, heard=chunk.heard, reply=chunk.reply)
                        first = False
                    if chunk.error and chunk.error != "cancelled":
                        m.AI_TURN_ERRORS.labels(chunk.error[:40]).inc()
                        ai.note_error(rt, chunk.error)
                        if not rt.conversation.cancelled:
                            await io.error(chunk.error)
                        break
                    if chunk.audio and not rt.conversation.cancelled:
                        st["uid"] += 1
                        await play(chunk.audio, chunk.sample_rate, chunk.language, st["uid"])
            except asyncio.CancelledError:
                raise
            except Exception:  # defence in depth: the worker must outlive any single failed turn
                log.exception("conversation worker error")
                m.AI_TURN_ERRORS.labels("worker_exception").inc()
                ai.note_error(rt, "worker_exception")
                continue

    async def flush_if_due() -> None:
        if st["in_speech"] and (st["silence_ms"] >= silence_ms or len(st["buf"]) >= rate * 2 * MAX_UTTERANCE_MS // 1000):
            utt = bytes(st["buf"])
            st.update(buf=bytearray(), in_speech=False, speech_ms=0, silence_ms=0)
            if utterances.full():  # backpressure: drop the oldest queued utterance
                utterances.get_nowait()
                await io.error("inbound_overflow")
            utterances.put_nowait(utt)

    async def next_item():
        if hasattr(io, "next_item"):
            return await io.next_item(IDLE_POLL_S)
        try:
            msg = await asyncio.wait_for(ws.receive(), IDLE_POLL_S)
        except asyncio.TimeoutError:
            return ("idle", int(IDLE_POLL_S * 1000))
        if msg["type"] == "websocket.disconnect":
            raise WebSocketDisconnect()
        if msg.get("text") is not None:
            return ("text", msg["text"])
        data = msg.get("bytes")
        return ("audio", data) if data else ("skip", None)

    local.append(asyncio.create_task(worker()))
    try:
        while True:
            kind, val = await next_item()
            if kind == "end":
                return val
            if kind == "text":
                try:
                    if json.loads(val).get("type") == "bye":
                        await ai.stop(rt.tenant_id, rt.session_id, "client_bye")
                        return "client_bye"
                except ValueError:
                    pass
                continue
            if kind == "idle":
                now = time.monotonic()
                if st["playing"] and now >= st["play_until"]:
                    st["playing"] = False
                if st["in_speech"]:
                    st["silence_ms"] += val
                    await flush_if_due()
                continue
            if kind != "audio":
                continue
            frame = val
            fm = max(1, len(frame) * 500 // rate)  # real duration of this frame at `rate` (16-bit mono)
            now = time.monotonic()
            is_playing = st["playing"] and (now < st["play_until"])
            if not is_playing:
                st["playing"] = False

            frame_rms = rms(frame)

            if is_playing:
                # Assistant is playing audio to the caller.
                # Inbound audio is speaker echo + line noise, UNLESS caller deliberately talks over.
                barge_threshold = max(vad_rms * BARGE_IN_RMS_MULTIPLIER, BARGE_IN_VAD_RMS)
                if frame_rms >= barge_threshold:
                    st["speech_ms"] += fm
                    st["silence_ms"] = 0
                    if st["speech_ms"] >= BARGE_IN_MS:
                        log.info("barge-in triggered: rms=%.1f >= %.1f for %dms", frame_rms, barge_threshold, st["speech_ms"])
                        rt.conversation.cancel()
                        st["playing"] = False
                        st["play_until"] = 0.0
                        await io.interrupt()
                        st["in_speech"] = True
                        st["buf"] = bytearray(frame)
                else:
                    st["speech_ms"] = 0
                    st["silence_ms"] += fm
            else:
                # Assistant is quiet, listening to the caller.
                loud = frame_rms >= vad_rms
                if loud:
                    st["speech_ms"] += fm
                    st["silence_ms"] = 0
                    if st["speech_ms"] >= MIN_SPEECH_MS or st["in_speech"]:
                        st["in_speech"] = True
                else:
                    st["silence_ms"] += fm
                    if not st["in_speech"]:
                        st["speech_ms"] = 0

                if st["in_speech"]:
                    st["buf"] += frame
                    await flush_if_due()
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


# ---------------------------------------------------------------------------------- Plivo bidirectional stream
_plivo_streaming: set[str] = set()  # session ids with an open Plivo stream (single worker process, enforced by settings)


async def _hangup_best_effort(ctrl, call: dict) -> None:
    try:
        await ctrl.hangup(call, source="media")
    except Exception as e:
        log.warning("could not hang up call after stream failure: %s", type(e).__name__)


async def handle_plivo_media(ws: WebSocket, app, *, vad_rms: float = VAD_RMS, silence_ms: int = SILENCE_MS) -> None:
    """Plivo `<Stream bidirectional>` endpoint.

    Order: (1) handshake signature + nonce replay, before accept; (2) `start` within 5 s; (3) signed ticket must name this
    call, tenant and session, and the call must be answered and AI-flagged; (4) AiService.start / attach_media / media_connected
    (the existing lifecycle); (5) the shared serve_loop through PlivoIO; (6) cleanup that always stops the AI session.
    """
    s, ai, repos, ctrl = app.state.settings, app.state.ai, app.state.repos, app.state.controller
    nonce = ws.headers.get("x-plivo-signature-v3-nonce", "")
    sig = ws.headers.get("x-plivo-signature-v3", "")
    if not plivo_mod.plivo_enabled(s) or not plivo_mod.verify_ws_signature(s, nonce=nonce, signature=sig, query=ws.url.query):
        m.PLIVO_STREAMS.labels("rejected_signature").inc()
        log.warning("rejected plivo stream handshake: invalid or missing signature")
        return await ws.close(code=1008)
    if await repos.seen_provider_key(f"plivo:ws-nonce:{nonce}"):
        m.PLIVO_STREAMS.labels("rejected_replay").inc()
        log.warning("rejected plivo stream handshake: replayed nonce")
        return await ws.close(code=1008)
    await ws.accept()
    if len(_plivo_streaming) >= s.plivo_stream_max_concurrent:  # authenticated, so a meaningful close code can be sent
        m.PLIVO_STREAMS.labels("rejected_capacity").inc()
        return await ws.close(code=1013)

    sid = None
    tenant = None
    call: dict | None = None
    rt = None
    reason = "ws_disconnected"
    counted = False
    io: PlivoIO | None = None
    try:
        raw_msg = await asyncio.wait_for(ws.receive_text(), 5)
        ev = json.loads(raw_msg)
        log.info("plivo stream start received: %s", raw_msg[:300])
        if not isinstance(ev, dict) or ev.get("event") != "start" or not isinstance(ev.get("start"), dict):
            reason = "no_start"
            return await ws.close(code=1008)
        start = ev["start"]
        call_id = start.get("callId") or start.get("callUUID") or start.get("callUuid") or ev.get("callId") or ev.get("callUUID")

        extra = (
            ev.get("extra_headers") or
            ev.get("extraHeaders") or
            start.get("extra_headers") or
            start.get("extraHeaders") or
            start.get("customParameters") or
            ev.get("customParameters") or
            start.get("customHeaders") or
            ev.get("customHeaders") or
            start.get("parameters") or
            ev.get("parameters")
        )
        ticket_str = tickets.from_extra_headers(extra)
        t = None
        if ticket_str:
            try:
                t = tickets.verify(s, ticket_str)
            except tickets.TicketError as e:
                reason = "bad_ticket"
                log.warning("plivo stream start rejected: %s", e)
                return await ws.close(code=1008)
        elif "extra_headers" in ev and ev["extra_headers"] == "":
            reason = "bad_ticket"
            log.warning("plivo stream start rejected: empty extra_headers in test payload")
            return await ws.close(code=1008)

        if not call_id and t:
            call_id = t.call_id
        if not call_id:
            reason = "no_call_id"
            log.warning("plivo stream start rejected: missing callId")
            return await ws.close(code=1008)

        if t and t.call_id != call_id:
            reason = "call_mismatch"
            log.warning("plivo stream start rejected: callId mismatch")
            return await ws.close(code=1008)

        mf = start.get("mediaFormat") if isinstance(start.get("mediaFormat"), dict) else {}
        try:
            sample_rate = int(mf.get("sampleRate", 0))
        except (ValueError, TypeError):
            sample_rate = 0
        if mf.get("encoding") != "audio/x-mulaw" or sample_rate != codec.MULAW_RATE:
            reason = "unsupported_format"
            return await ws.close(code=1003)

        call = await repos.find_call_by_provider_id("plivo_api", call_id)
        if not call or call["state"] != "answered":
            reason = "session_mismatch"
            log.warning("plivo stream start rejected: call %s not found or not answered", call_id)
            return await ws.close(code=1008)

        if t and (call["tenant_id"] != t.tenant_id or call.get("session_id") != t.session_id):
            reason = "session_mismatch"
            log.warning("plivo stream start rejected: ticket does not match call session")
            return await ws.close(code=1008)

        sid, tenant = call["session_id"], call["tenant_id"]
        if sid in _plivo_streaming:  # check-and-add with no await in between: a ticket opens ONE stream
            reason = "already_streaming"
            sid = None
            return await ws.close(code=1008)
        _plivo_streaming.add(sid)
        counted = True
        m.PLIVO_STREAMS_ACTIVE.inc()
        stream_id = str(start.get("streamId", ""))[:64]

        from app.ai.media import PlivoStreamBridge
        try:
            await ai.start(tenant, sid, bridge=PlivoStreamBridge(s))
            rt = await ai.attach_media(tenant, sid)
        except ApiError as e:
            reason = f"ai_start_{e.code}"
            m.PLIVO_STREAMS.labels("ai_start_failed").inc()
            await repos.add_event(call, "ai.start_failed", "plivo", f"aistart:{stream_id}", {"code": e.code})
            await _hangup_best_effort(ctrl, call)
            return await ws.close(code=1011)
        rt.ws = ws
        await ai.media_connected(rt, {"sample_rate": codec.MULAW_RATE, "frame_ms": 20, "flavor": "plivo_stream", "stream_id": stream_id})
        await repos.update_call(call["_id"], {"stream_connected": True, "stream_id": stream_id, "stream_connected_at": utcnow()})
        await repos.add_event(call, "stream.connected", "plivo", f"stream:{stream_id}:connected", {})
        m.PLIVO_STREAMS.labels("connected").inc()
        io = PlivoIO(ws, stream_id)
        end = await serve_loop(ws, ai, rt, io, codec.MULAW_RATE, 20, vad_rms=vad_rms, silence_ms=silence_ms)
        reason = end or "stream_ended"
    except WebSocketDisconnect:
        reason = "ws_disconnected"
    except (asyncio.TimeoutError, json.JSONDecodeError):
        reason = "protocol_error"
        try:
            await ws.close(code=1008)
        except Exception:
            pass
    except RuntimeError:
        reason = "socket_closed"
    except Exception as e:  # never leave the call/AI session dangling on an unexpected error
        reason = f"error_{type(e).__name__}"
        log.exception("plivo stream error")
        if call is not None:
            await _hangup_best_effort(ctrl, call)
    finally:
        if counted:
            _plivo_streaming.discard(sid)
            m.PLIVO_STREAMS_ACTIVE.dec()
        if sid and tenant:
            try:
                await ai.stop(tenant, sid, reason)  # idempotent: cancels inference/playback, closes the socket, releases resources
            except ApiError:
                pass  # no AI session was created
            except Exception:
                log.exception("AI cleanup after plivo stream failed")
            if call is not None:
                await repos.add_event(call, "stream.ended", "plivo", f"stream:{sid}:ended", {"reason": reason, **(io.counters if io else {})})
        m.PLIVO_STREAMS.labels("ended").inc()
        log.info("plivo stream closed reason=%s", reason)
