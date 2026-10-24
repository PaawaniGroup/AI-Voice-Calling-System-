"""Plivo transport sink for the paced playout: 20 ms PCM16 frames -> mu-law -> `playAudio`, batched to `batch_ms` so the socket is not flooded
with 50 tiny messages a second (the first burst and every pause flush at once, so batching never holds audio back), `clearAudio` on barge-in,
and the final `checkpoint`. UNVERIFIED with a real Plivo call: how Plivo's player treats many small playAudio messages (batching to 100 ms is a
conservative choice, the guidance is <= 1 s per message)."""
from __future__ import annotations

import base64
import json

from app.ai import codec, turn_trace


class PlivoStreamSink:
    def __init__(self, io, *, on_audio=None, batch_ms: int = 100, rate: int = codec.MULAW_RATE):
        self.io = io                       # the PlivoIO: .ws, .stream_id, .sent_checkpoints, .interrupt()
        self.on_audio = on_audio           # called with the seconds of audio just handed over (feeds the barge-in "assistant is playing" window)
        self.rate = rate
        self.batch_bytes = rate * batch_ms // 1000
        self.buf = bytearray()
        self.frames = 0
        self.writes = 0
        self.clears = 0
        self._since_clear = 0              # frames handed over since the last clear: a second clear with nothing in between is skipped

    async def send(self, gen: int, frame: bytes) -> None:
        self.buf += codec.pcm16_to_mulaw(frame)
        self.frames += 1
        self._since_clear += 1
        if self.on_audio is not None:
            self.on_audio(len(frame) / 2 / self.rate)
        if len(self.buf) >= self.batch_bytes:
            await self.flush()

    async def flush(self) -> None:
        if not self.buf:
            return
        payload, self.buf = bytes(self.buf), bytearray()
        await self.io.ws.send_text(json.dumps({"event": "playAudio", "media": {
            "contentType": "audio/x-mulaw", "sampleRate": self.rate, "payload": base64.b64encode(payload).decode()}}))
        self.writes += 1
        turn_trace.mark("t_first_frame_to_plivo")      # first write wins: the first audio really on the socket

    async def clear(self, gen: int) -> None:
        """Drop what was not yet written and tell Plivo to flush its queue; nothing is sent when nothing was handed over since the last clear."""
        self.buf.clear()
        if self._since_clear:
            self._since_clear = 0
            self.clears += 1
            await self.io.interrupt()

    async def finish(self, uid: int) -> None:
        """All audio of the turn is written: the checkpoint tells us when Plivo has played it (same contract as PlivoIO.play)."""
        await self.flush()
        name = f"utt-{uid}"
        self.io.sent_checkpoints.append(name)
        await self.io.ws.send_text(json.dumps({"event": "checkpoint", "streamId": self.io.stream_id, "name": name}))
