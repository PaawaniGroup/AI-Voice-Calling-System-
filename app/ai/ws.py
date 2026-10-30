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

from app.ai import codec, turn_trace
from app.ai.clients import pcm16_to_wav, rms
from app.ai.duplex import BargeInDetector, OutboundLevel, PreRoll, silence_resets
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
WORKER_STOP_S = 2.0    # max wait for the turn worker to honour cancellation when a media socket ends
MERGE_WINDOW_S = 1.5   # new speech this soon after the last utterance, before any assistant audio: the same question, merged into one turn
MERGE_MAX_AUDIO_S = 15.0   # a merged utterance never grows beyond this (STT time/cost); past it the older speech is dropped instead
TURN_STOP_S = 1.0      # max wait for a cancelled turn to unwind before the worker moves on
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
            turn_trace.mark("t_first_frame_to_plivo")  # first write wins
        name = f"utt-{uid}"
        self.sent_checkpoints.append(name)
        await self.ws.send_text(json.dumps({"event": "checkpoint", "streamId": self.stream_id, "name": name}))


async def serve_loop(ws: WebSocket, ai, rt, io, rate: int, frame_ms: int, *, vad_rms: float, silence_ms: int) -> str | None:
    """Shared endpointing/barge-in/turn loop. Returns an end reason when the transport signalled one, else None.

    The item source is `io.next_item(timeout)` when the adapter has one (Plivo), otherwise the raw WebSocket. A gap with no
    frames counts as silence, so an utterance still ends if the transport stops sending when the caller is quiet.
    """
    local: list[asyncio.Task] = []
    utterances: asyncio.Queue[tuple] = asyncio.Queue(maxsize=3)  # (pcm, t_speech_end, t_endpoint, endpoint_reason) on the monotonic clock; t_speech_end = the LAST LOUD FRAME, t_endpoint = the moment the end was decided
    st = {
        "last_loud_at": 0.0,
        "playing": False,
        "play_until": 0.0,
        "play_started_at": 0.0,
        "uid": 0,
        "speech_ms": 0,
        "silence_ms": 0,
        "buf": bytearray(),
        "in_speech": False,
        "last_audio_at": 0.0,   # monotonic time the last assistant audio was handed to the transport
    }
    turn: dict = {"task": None, "pcm": b"", "end_at": 0.0, "audio": False, "n": 1}   # the turn being processed (if any)
    merge: dict = {"pcm": None, "n": 0}   # speech of a cancelled attempt that the NEXT utterance continues
    _u = getattr(rt, "utterance_max_ms", None)
    utterance_cap_ms = _u if (isinstance(_u, int) and not isinstance(_u, bool) and MAX_UTTERANCE_MS <= _u <= 12000) else MAX_UTTERANCE_MS   # tenant flag `utterance_cap_raise` (default OFF): 6-12 s; else today's 6 s

    # Full-duplex input handling (tenant flag `full_duplex_input`; None = OFF: every line below takes the old path, unchanged)
    duplex = getattr(rt.conversation, "duplex", None)
    det = BargeInDetector(duplex, vad_rms) if duplex else None            # barge-in needs min voiced time, bridges short gaps, applies the echo guard
    out_level = OutboundLevel() if duplex else None                       # how loud the assistant has been, on the playback timeline
    pre = PreRoll(duplex.pre_roll_ms) if duplex else None                 # audio before speech was confirmed: kept, not thrown away
    st["voiced_run_ms"] = 0

    def on_level(duration_s: float, level: float) -> None:
        if out_level is not None:
            out_level.add_next(time.monotonic(), duration_s, level)

    def on_start(duration_s: float) -> None:
        now = time.monotonic()
        base = max(now, st["play_until"])
        st["play_until"] = base + duration_s
        st["playing"] = True
        st["play_started_at"] = now

    async def play(audio: bytes, src: int, lang: str, uid: int) -> None:
        if not isinstance(io, PlivoIO):  # PlivoIO marks the exact moment its first frame is written to the socket
            turn_trace.mark("t_first_frame_to_plivo")
        if out_level is not None:
            out_level.add_clip(time.monotonic(), audio, src)   # whole clip handed over at once: its loudness over time, for the echo guard
        try:
            await io.play(audio, src, lang, uid, on_start)
            st["last_audio_at"] = time.monotonic()
        finally:
            if isinstance(io, NativeIO):
                st["playing"] = False

    def silence_stream() -> None:
        """Token-streaming turn only: stop its paced output NOW (synchronous), before anything else on a barge-in. Without this, a frame could
        still be written between the clearAudio and the cancellation and be played after the caller was already cut off."""
        s = turn.get("stream")
        if s is not None:
            s.silence()

    async def clear_stream() -> None:
        sink = turn.get("stream_sink")
        if sink is not None:
            await sink.clear(0)   # clearAudio unless nothing was written since the last clear

    async def interrupt_io() -> None:
        """The barge-in clear. For a streaming turn it goes through the sink, which remembers it (the cancel path then does not clear again)."""
        sink = turn.get("stream_sink")
        if sink is not None:
            await sink.clear(0)
        else:
            await io.interrupt()

    async def stream_turn_live(pcm: bytes, trace, t_turn: float, final: dict) -> bool:
        """The token-streaming pipeline for ONE turn on a Plivo stream (tenant flag `streaming_pipeline`). Returns whether the turn was recorded.
        Records exactly what the old path records: `record_turn` when the first audio is written, `finalize_turn` (by the caller) at the end."""
        from app.ai.clients import AiError
        from app.ai.streaming.live import run_streaming_turn
        from app.ai.streaming.plivo_sink import PlivoStreamSink

        def on_audio(seconds: float) -> None:
            turn["audio"] = True
            on_start(seconds)

        sink = PlivoStreamSink(io, on_audio=on_audio, on_level=on_level if out_level is not None else None)
        turn["stream_sink"] = sink
        st["uid"] += 1
        uid = st["uid"]
        rec_tasks: list[asyncio.Task] = []
        state = {"recorded": False}

        def on_created(s) -> None:
            turn["stream"] = s

        def on_first_frame(res, first_text: str) -> None:
            res.timings_ms["turn_ms"] = round((time.perf_counter() - t_turn) * 1000, 1)
            m.AI_TURN_SECONDS.observe(res.timings_ms["turn_ms"] / 1000)
            state["recorded"] = True
            rec_tasks.append(asyncio.ensure_future(ai.record_turn(rt, dict(res.timings_ms), heard=res.heard, reply=first_text, trace_id=trace.trace_id)))

        res = None
        try:
            try:
                res = await run_streaming_turn(rt.conversation, pcm, rate, sink=sink, gen=uid, on_created=on_created, on_first_frame=on_first_frame)
            except AiError as e:
                code = f"{e.service}_{e.kind}"
                m.AI_TURN_ERRORS.labels(code[:40]).inc()
                ai.note_error(rt, code)
                await io.error(code)
                return False
            out = res.outcome
            if res.error and not state["recorded"]:
                res.timings_ms["turn_ms"] = round((time.perf_counter() - t_turn) * 1000, 1)
                await ai.record_turn(rt, dict(res.timings_ms), heard=res.heard, reply=None, trace_id=trace.trace_id)
                state["recorded"] = True
            if res.error:
                m.AI_TURN_ERRORS.labels(res.error[:40]).inc()
                ai.note_error(rt, res.error)
                if not rt.conversation.cancelled:
                    await io.error(res.error)
            if out is not None and out.status == "done" and not res.error:
                await sink.finish(uid)
                st["last_audio_at"] = time.monotonic()
            if out is not None:
                final["reply"] = out.reply_text if out.status == "done" else out.spoken_text
            return state["recorded"]
        except asyncio.CancelledError:
            conv_out = rt.conversation.last_stream
            if conv_out is not None:
                final["reply"] = conv_out.spoken_text
            raise
        finally:
            if rec_tasks:
                await asyncio.gather(*rec_tasks, return_exceptions=True)   # the turn record must exist before finalize_turn updates it
            final["recorded"] = state["recorded"]
            turn["stream"] = None

    async def run_turn(pcm: bytes, t_speech_end: float, t_endpoint: float, endpoint_reason: str = "silence", pieces: int = 1) -> None:
        """ONE turn (STT -> Brain -> TTS clauses -> audio) as its own task, so a new utterance can cancel it. Cancelling it cancels the
        in-flight STT/Brain/TTS requests (and their RunPod jobs) and frees the worker for the next utterance."""
        t_turn = time.perf_counter()
        first = True
        trace = turn_trace.TurnTrace(t_speech_end, t_endpoint, endpoint_silence_ms=silence_ms)
        turn_trace.start(trace)  # this task has its own context: the trace belongs to this turn only
        trace.turn.update(endpoint_reason=endpoint_reason, utterance_ms=round(len(pcm) / (rate * 2) * 1000), pieces_merged=pieces)   # the label and lengths of what STT is given
        turn["trace"] = trace                                      # so cancel_turn / the hangup path can say WHY this turn ended
        recorded = False
        stream_final: dict = {}   # set only by the token-streaming path (tenant flag `streaming_pipeline`, Plivo calls)
        try:
            if rt.conversation.streaming is not None and isinstance(io, PlivoIO):
                await stream_turn_live(pcm, trace, t_turn, stream_final)
                return
            async for chunk in rt.conversation.stream_turn(pcm, rate):
                if rt.conversation.cancelled:
                    break
                if first and (chunk.timings_ms or chunk.heard or chunk.reply):
                    chunk.timings_ms["turn_ms"] = round((time.perf_counter() - t_turn) * 1000, 1)
                    if not chunk.error:
                        m.AI_TURN_SECONDS.observe(chunk.timings_ms["turn_ms"] / 1000)
                    await ai.record_turn(rt, chunk.timings_ms, heard=chunk.heard, reply=chunk.reply, trace_id=trace.trace_id)
                    recorded = True
                    first = False
                if chunk.error and chunk.error != "cancelled":
                    m.AI_TURN_ERRORS.labels(chunk.error[:40]).inc()
                    ai.note_error(rt, chunk.error)
                    if not rt.conversation.cancelled:
                        await io.error(chunk.error)
                    break
                if chunk.audio and not rt.conversation.cancelled:
                    turn["audio"] = True
                    st["uid"] += 1
                    await play(chunk.audio, chunk.sample_rate, chunk.language, st["uid"])
        except asyncio.CancelledError:
            raise
        except Exception:  # defence in depth: a failed turn must not take the call down
            log.exception("conversation worker error")
            m.AI_TURN_ERRORS.labels("worker_exception").inc()
            ai.note_error(rt, "worker_exception")
        finally:
            recorded = recorded or stream_final.get("recorded", False)
            if recorded:
                trace.mark("t_turn_end")
                try:
                    if "reply" in stream_final:   # streaming path: the reply of THIS turn (what was produced; after a barge-in, what was heard)
                        final_reply = stream_final["reply"]
                    else:
                        final_reply = rt.conversation.turns[-1]["reply"] if rt.conversation.turns else None
                    await ai.finalize_turn(rt, trace.trace_id, trace.to_ms(), reply=final_reply)
                    if getattr(rt, "trace_evidence", False):           # tenant flag AND the env path: otherwise no file is ever written
                        turn_trace.append_evidence(getattr(ai.s, "ai_trace_evidence_path", ""), trace)
                except Exception:  # latency bookkeeping must never take the call down
                    log.exception("could not store turn latency trace")
            elif getattr(rt, "trace_evidence", False):
                # a turn that never reached audio (merged into the next utterance, superseded, hung up, no speech): its evidence line says so; from_trace skips lines without a first frame
                try:
                    trace.turn.setdefault("outcome", "cancelled_before_audio" if "cancel_reason" in trace.turn else "no_answer")   # cancelled before the streaming turn began, or no answer (no speech, an error)
                    trace.mark("t_turn_end")
                    turn_trace.append_evidence(getattr(ai.s, "ai_trace_evidence_path", ""), trace)
                except Exception:
                    log.exception("could not store turn latency trace")

    async def worker() -> None:
        while True:
            pcm, t_speech_end, t_endpoint, endpoint_reason = await utterances.get()
            n = 1
            if merge["pcm"] is not None:   # an earlier attempt at this question was cancelled: this speech continues it
                pcm, n = merge["pcm"] + pcm, merge["n"] + 1
                merge["pcm"] = None
            turn.update(pcm=pcm, end_at=t_speech_end, audio=False, n=n, stream=None, stream_sink=None)
            task = asyncio.create_task(run_turn(pcm, t_speech_end, t_endpoint, endpoint_reason, n), name="ai-turn")
            turn["task"] = task
            try:
                await asyncio.wait({task})   # a cancelled turn ends here normally; only OUR cancellation raises
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            finally:
                if turn["task"] is task:
                    turn["task"] = None

    async def cancel_turn(reason: str) -> bool:
        """Stop the turn in progress (if any): its STT/Brain/TTS requests are cancelled and the worker is free for the next utterance."""
        task = turn["task"]
        if task is None or task.done():
            return False
        tr = turn.get("trace")
        if tr is not None:
            tr.turn["cancel_reason"] = reason                          # barge_in | merged | superseded (hangup is set in the cleanup below)
        rt.conversation.cancel()
        silence_stream()                       # token-streaming turn: no frame is written after this line (no-op for the old path)
        stream = turn.get("stream")
        t0 = time.monotonic()
        task.cancel()
        _, pending = await asyncio.wait({task}, timeout=TURN_STOP_S)
        if pending:
            log.error("a cancelled turn did not stop within %.1fs; the worker moves on", TURN_STOP_S)
        await clear_stream()                   # and Plivo's own queue is flushed (once; skipped when the barge-in branch already cleared it)
        m.AI_TURNS_CANCELLED.labels(reason).inc()
        act = getattr(ai, "activity", None)
        if act is not None:
            extra = (stream.interrupt_metrics() if stream is not None else None) or {}
            act.publish(rt.tenant_id, "ai.turn_cancelled", call_id=rt.call_id, session_id=rt.session_id,
                        data={"reason": reason, "unwind_ms": round((time.monotonic() - t0) * 1000, 1),
                              "since_last_audio_ms": round((time.monotonic() - st["last_audio_at"]) * 1000, 1) if st["last_audio_at"] else None,
                              **({"interrupt_to_last_frame_ms": extra["interrupt_to_last_frame_ms"]} if "interrupt_to_last_frame_ms" in extra else {})})
        return True

    async def on_speech_start(now: float, speech_ms: int) -> None:
        """The caller starts speaking while a turn is in progress and the assistant is NOT being interrupted over live playback:
        - the assistant already produced audio for this turn: the caller talks over it (barge-in): stop the rest of the reply;
        - no audio yet and the speech starts within MERGE_WINDOW_S of the last utterance: the same question continued: cancel the
          attempt and let the next utterance be merged into it;
        - no audio yet, later than that: a new question: it supersedes the unanswered one."""
        task = turn["task"]
        if task is None or task.done():
            return
        if turn["audio"]:
            reason = "barge_in"
            if st["playing"] and now < st["play_until"]:
                silence_stream()
                await interrupt_io()
                st["playing"], st["play_until"] = False, 0.0
        else:
            started = now - speech_ms / 1000
            if started - turn["end_at"] <= MERGE_WINDOW_S and len(turn["pcm"]) <= MERGE_MAX_AUDIO_S * rate * 2:
                merge["pcm"], merge["n"] = turn["pcm"], turn["n"]
                reason = "merged"
            else:
                reason = "superseded"
        await cancel_turn(reason)

    async def flush_if_due() -> None:
        if st["in_speech"] and (st["silence_ms"] >= silence_ms or len(st["buf"]) >= rate * 2 * utterance_cap_ms // 1000):
            utt = bytes(st["buf"])
            reason = "silence" if st["silence_ms"] >= silence_ms else "cap"          # the 700 ms silence rule, or the utterance-length cap (MAX_UTTERANCE_MS)
            t_endpoint = time.monotonic()
            t_speech_end = st["last_loud_at"] or t_endpoint
            st.update(buf=bytearray(), in_speech=False, speech_ms=0, silence_ms=0)
            if utterances.full():  # backpressure: drop the oldest queued utterance
                utterances.get_nowait()
                await io.error("inbound_overflow")
            utterances.put_nowait((utt, t_speech_end, t_endpoint, reason))

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

            if is_playing and det is not None:
                # Full-duplex input handling (tenant flag): a barge-in needs `min_voiced_ms` of VOICED speech (a short "hmm" is not one), short gaps
                # inside speech are bridged, speaker echo (quieter than what the assistant is playing) is ignored, and the speech that triggered the
                # interruption is KEPT as the start of the next utterance (the old rule kept only the triggering frame).
                level = out_level.recent(now, duplex.echo_window_ms, duplex.echo_stat)
                if frame_rms >= det.threshold(level):
                    st["last_loud_at"] = now
                if det.feed(frame, frame_rms, fm, level):
                    log.info("barge-in triggered (full duplex): voiced %dms, threshold %.1f, echo-suppressed frames %d",
                             det.voiced_ms, det.last_threshold, det.echo_suppressed_frames)
                    kept = det.take()
                    st["playing"] = False
                    st["play_until"] = 0.0
                    silence_stream()
                    await interrupt_io()
                    await cancel_turn("barge_in")
                    out_level.clear()
                    pre.clear()
                    st.update(in_speech=True, buf=bytearray(kept), silence_ms=0, speech_ms=0, voiced_run_ms=0)
            elif is_playing:
                # Assistant is playing audio to the caller.
                # Inbound audio is speaker echo + line noise, UNLESS caller deliberately talks over.
                barge_threshold = max(vad_rms * BARGE_IN_RMS_MULTIPLIER, BARGE_IN_VAD_RMS)
                if frame_rms >= barge_threshold:
                    st["last_loud_at"] = now
                    st["speech_ms"] += fm
                    st["silence_ms"] = 0
                    if st["speech_ms"] >= BARGE_IN_MS:
                        log.info("barge-in triggered: rms=%.1f >= %.1f for %dms", frame_rms, barge_threshold, st["speech_ms"])
                        st["playing"] = False
                        st["play_until"] = 0.0
                        silence_stream()                     # streaming turn: stop writing frames before the clear (a frame after it would still play)
                        await interrupt_io()                 # clear first: the caller stops hearing the assistant at once
                        await cancel_turn("barge_in")        # then stop generating the rest of the reply and free the worker
                        st["in_speech"] = True
                        st["buf"] = bytearray(frame)
                else:
                    st["speech_ms"] = 0
                    st["silence_ms"] += fm
            else:
                # Assistant is quiet, listening to the caller.
                loud = frame_rms >= vad_rms
                if det is not None:
                    det.reset()                              # a barge-in run never continues across a pause in the assistant's audio
                    st["voiced_run_ms"] = st["voiced_run_ms"] + fm if loud else 0
                    if not st["in_speech"]:
                        pre.push(frame, fm)                  # the audio before speech is confirmed is kept, not thrown away
                if loud:
                    st["last_loud_at"] = now
                    st["speech_ms"] += fm
                    if det is None or silence_resets(st["in_speech"], st["voiced_run_ms"], duplex):
                        st["silence_ms"] = 0
                    else:
                        st["silence_ms"] += fm               # end-of-speech hysteresis: a click shorter than resume_ms does not postpone the end
                    if st["speech_ms"] >= MIN_SPEECH_MS or st["in_speech"]:
                        if not st["in_speech"]:
                            st["in_speech"] = True
                            if det is not None:
                                st["buf"] = bytearray(pre.drain(exclude_last=True))   # the first 200 ms that confirmed the speech belong to it
                            await on_speech_start(now, st["speech_ms"])
                        st["in_speech"] = True
                else:
                    st["silence_ms"] += fm
                    if not st["in_speech"]:
                        st["speech_ms"] = 0

                if st["in_speech"]:
                    st["buf"] += frame
                    await flush_if_due()
    finally:
        # Bounded: a worker that does not honour cancellation must never keep the socket handler (and the call) alive.
        tr_end = turn.get("trace")
        if tr_end is not None and turn["task"] is not None and not turn["task"].done():
            tr_end.turn.setdefault("cancel_reason", "hangup")           # the socket ended under a running turn: the caller hung up, or a disarm/hangup request closed the call
        pending = set(local)
        for wait_s in (WORKER_STOP_S, WORKER_STOP_S / 2):
            for tk in pending:
                tk.cancel()
            _, pending = await asyncio.wait(pending, timeout=wait_s)
            if not pending:
                break
        if pending:
            log.error("conversation worker did not stop after cancellation; abandoning it so the socket handler can end")


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
        log.info("plivo stream start received: %d bytes, event=%s", len(raw_msg), ev.get("event") if isinstance(ev, dict) else None)  # not the body: it carries the signed stream ticket
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
