"""Media bridge boundary.

ESL carries NO audio. Real two-way audio needs a FreeSWITCH media-fork module (e.g. mod_audio_fork /
mod_audio_stream) that streams the call's PCM to a WebSocket and plays audio sent back. That module is NOT
installed on the audited FreeSWITCH (`mod_av`, `mod_opus`, `mod_tone_stream`, `mod_local_stream` only), so
`MediaBridge.ready()` reports not-ready with the exact reason and AI sessions refuse to start. Nothing here
fakes audio.

Wire protocol of the media WebSocket (`/v1/ai/sessions/{session_id}/media`), implemented in `app/ai/ws.py`
and tested with a synthetic client (NOT with FreeSWITCH):
  client -> server  text  {"type":"auth","token":"<JWT>"}                         first message, 5 s deadline
  server -> client  text  {"type":"auth_ok"}
  client -> server  text  {"type":"hello","encoding":"pcm_s16le","sample_rate":8000|16000,"channels":1,"frame_ms":20}
  server -> client  text  {"type":"ready"}
  client -> server  binary  raw PCM frames (frame_ms each, no header), in order
  server -> client  text  {"type":"playback_start","utterance_id":N,"sample_rate":R,"language":"hi|en|..."}
  server -> client  binary  PCM frames (frame_ms each) paced by the server at <= real time
  server -> client  text  {"type":"playback_end","utterance_id":N}
  server -> client  text  {"type":"interrupt"}       barge-in: the client MUST drop queued playback audio
  server -> client  text  {"type":"error","code":"...","fatal":bool}
  either            text  {"type":"bye"}
Backpressure: the server keeps a bounded inbound queue; on overflow the OLDEST frames are dropped and
`{"type":"error","code":"inbound_overflow","fatal":false}` is sent once per burst.
Reconnect: if the socket drops, the AI session stays `media_disconnected` for MEDIA_RECONNECT_S; reconnecting
with a valid token resumes the same conversation; afterwards the AI session fails.
"""
from __future__ import annotations

from typing import Protocol


class MediaBridge(Protocol):
    name: str

    async def ready(self) -> tuple[bool, str]:
        """(ready, human-readable reason). Must reflect a real check, never a constant True."""


class UnavailableBridge:
    name = "none"

    async def ready(self) -> tuple[bool, str]:
        return False, ("no media bridge configured (AI_MEDIA_BRIDGE=none); ESL cannot carry audio and no FreeSWITCH "
                       "media-fork module is installed")


class WsForkBridge:
    """Ready only when a FreeSWITCH media-fork module is actually loaded (checked over ESL)."""

    name = "ws_fork"
    MODULES = ("mod_audio_fork", "mod_audio_stream")

    def __init__(self, esl_cmd):
        self.esl = esl_cmd

    async def ready(self) -> tuple[bool, str]:
        if self.esl is None:
            return False, "FreeSWITCH control connection not configured"
        try:
            for mod in self.MODULES:
                if (await self.esl.api(f"module_exists {mod}")).strip().lower() == "true":
                    return True, f"{mod} loaded"
        except Exception as e:  # ESL down
            return False, f"cannot query FreeSWITCH: {type(e).__name__}"
        return False, "none of mod_audio_fork / mod_audio_stream is loaded in FreeSWITCH"
