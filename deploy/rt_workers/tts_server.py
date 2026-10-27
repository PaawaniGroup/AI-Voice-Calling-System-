"""Voice server: a standing WebSocket in front of the Chatterbox model on an ALWAYS-ON host (a Pod or a RunPod load-balancing endpoint). NOT built into any live image.

  python tts_server.py            serves ws://0.0.0.0:$PORT/ws (the TLS and the public name come from the host: a Pod TCP port or the load-balancing endpoint)
  RT_SECRET   the signing secret of the call tokens (an environment variable of the server; never logged)
  PORT, PORT_HEALTH, RT_MAX_GLOBAL

The model: `ChatterboxEngine.synth_stream` is the integration point of the chunked-synthesis spike (tools/phase3/chunked_tts_spike.py: a generator version of T3 plus
windowed S3Gen rendering, first audio chunk before the whole utterance exists). UNVERIFIED until the Stage 3 voice gate has passed: until then `synth_stream` renders the
whole clause with the model's normal call and yields it in 200 ms ulaw frames, which is correct but no faster than today's worker. The voice is fixed by NAME (prepared at
start-up); an unknown voice is an error, never a silent switch to another voice.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Limits, serve_connection              # noqa: E402


class ChatterboxEngine:
    """Imports torch and chatterbox lazily (the protocol layer above is tested without them)."""

    def __init__(self, voices: dict[str, str] | None = None):
        self.voices = voices or {"utkarsh": os.environ.get("REF_WAV", "")}
        self._model = None

    def _load(self):
        if self._model is None:
            import torch                                          # noqa: F401
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS   # the model class of the Chatterbox source the spike was written against (UNVERIFIED name here)
            self._model = ChatterboxMultilingualTTS.from_pretrained(device="cuda")
        return self._model

    async def synth_stream(self, text: str, voice: str, fmt: str, lang: str) -> AsyncIterator[bytes]:
        if voice not in self.voices:
            raise ValueError("unknown voice")                     # surfaced as engine_error: never a silent switch to another voice
        loop = asyncio.get_running_loop()
        pcm_ulaw = await loop.run_in_executor(None, self._render_whole, text, voice, lang)
        yield pcm_ulaw

    def _render_whole(self, text: str, voice: str, lang: str) -> bytes:
        import audioop
        import numpy as np
        wav = self._load().generate(text, language_id=lang or "hi", audio_prompt_path=self.voices[voice])
        pcm16 = (np.clip(wav.squeeze().cpu().numpy(), -1, 1) * 32767).astype("<i2")
        sr = self._model.sr
        pcm8, _ = audioop.ratecv(pcm16.tobytes(), 2, 1, sr, 8000, None)
        return audioop.lin2ulaw(pcm8, 2)


async def main() -> None:
    import websockets
    secret = os.environ["RT_SECRET"].encode()
    limits = Limits(max_global=int(os.environ.get("RT_MAX_GLOBAL", "2")))
    sem = asyncio.Semaphore(limits.max_global)
    engine = ChatterboxEngine()

    async def handler(ws):
        await serve_connection(ws, service="tts", engine=engine, secret=secret, limits=limits, global_sem=sem)

    async with websockets.serve(handler, "0.0.0.0", int(os.environ.get("PORT", "8000")), max_size=2 ** 22, ping_interval=None):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
