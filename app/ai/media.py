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


class PlivoStreamBridge:
    """Media transport for calls that arrive through the Plivo Voice API (`<Stream bidirectional>`).

    There is nothing to start on a switch: Plivo itself opens the WebSocket. "Ready" therefore means the pieces that make a
    stream acceptable are configured; the AI session still becomes `active` only when a stream really connects.
    """

    name = "plivo_stream"

    def __init__(self, s):
        self.s = s

    async def ready(self) -> tuple[bool, str]:
        missing = [n for n, ok in (("PLIVO_AUTH_TOKEN", bool(self.s.plivo_auth_token.get_secret_value())),
                                   ("WEBHOOK_PUBLIC_BASE_URL (https)", self.s.webhook_public_base_url.startswith("https://")),
                                   ("JWT_SECRET", bool(self.s.jwt_secret.get_secret_value()))) if not ok]
        if missing:
            return False, "missing configuration: " + ", ".join(missing)
        return True, "Plivo opens the stream; nothing to start on a switch"


class WsForkBridge:
    """Ready only when FreeSWITCH `mod_audio_stream` is actually loaded (checked over ESL).

    begin(): hands the module a short-lived token (channel var STREAM_EXTRA_HEADERS) and starts the stream, so the module
    connects to this service's WebSocket. end(): stops the stream and any playback. No audio is faked at any point.
    """

    name = "ws_fork"
    MODULES = ("mod_audio_stream",)

    def __init__(self, esl_cmd, s=None):
        self.esl, self.s = esl_cmd, s

    async def begin(self, fs_uuid: str, tenant_id: str, session_id: str, token: str) -> None:
        import json as _json

        from app.telephony import dialstrings as ds
        u = ds.safe_uuid(fs_uuid)
        hdr = _json.dumps({"Authorization": f"Bearer {token}"}, separators=(",", ":"))
        if any(c in hdr for c in "\n\r"):
            raise ValueError("unsafe header")
        await self.esl.api(f"uuid_setvar {u} STREAM_EXTRA_HEADERS {hdr}")
        meta = _json.dumps({"session_id": session_id, "sample_rate": self.s.ai_media_sample_rate}, separators=(",", ":"))
        url = self.s.ai_media_ws_url
        if not url.startswith(("ws://", "wss://")) or any(c in url for c in " \n\r'\""):
            raise ValueError("unsafe media url")
        out = await self.esl.api(f"uuid_audio_stream {u} start {url} mono {self.s.ai_media_sample_rate} {meta}")
        if out.strip().startswith("-ERR"):
            raise RuntimeError(out.strip()[:100])

    async def end(self, fs_uuid: str) -> None:
        from app.telephony import dialstrings as ds
        u = ds.safe_uuid(fs_uuid)
        for cmd in (f"uuid_break {u} all", f"uuid_audio_stream {u} stop"):
            try:
                await self.esl.api(cmd)
            except Exception:
                pass  # channel may already be gone

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
