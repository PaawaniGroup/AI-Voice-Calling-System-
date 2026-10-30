"""STT server: a standing WebSocket in front of the speech model (handler (7) logic: transformers pipeline, 8 kHz PCM16 in, resampled to 16 kHz here) on an ALWAYS-ON host.
NOT built into any live image. Same protocol and tests as the voice server (common.py).

  RT_SECRET   the signing secret of the call tokens;  MODEL_ID, MODEL_REVISION, WHISPER_LANGUAGE as in docs/worker_stt/handler.py;  PORT, RT_MAX_GLOBAL
  RT_BIND (default 127.0.0.1), RT_TLS_CERT, RT_TLS_KEY: the server terminates TLS itself and refuses a non-loopback bind without a certificate

The request carries `lang` (hi|en|auto) and an optional `prompt` (<= 200 characters, UNVERIFIED with this model: see docs/worker_stt/README.md); neither is ever logged.
Rolling-window decoding (Stage 3 STT gate) will stream `partial` messages from here; until that gate has passed only `final` is sent.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Limits, serve_connection, serve_forever   # noqa: E402


class WhisperEngine:
    def __init__(self):
        self._asr = None

    def _load(self):
        if self._asr is None:
            import torch
            from transformers import pipeline
            self._asr = pipeline("automatic-speech-recognition", model=os.environ.get("MODEL_ID", "mmeog/mme_stt_model"), device=0 if torch.cuda.is_available() else -1,
                                 dtype=torch.float16 if torch.cuda.is_available() else torch.float32)
        return self._asr

    async def transcribe(self, pcm: bytes, rate: int, lang: str | None, prompt: str | None) -> dict:
        return await asyncio.get_running_loop().run_in_executor(None, self._decode, pcm, rate, lang)

    def _decode(self, pcm: bytes, rate: int, lang: str | None) -> dict:
        import numpy as np
        from scipy.signal import resample_poly
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if rate != 16000:
            audio = resample_poly(audio, 16000, rate).astype(np.float32)
        gk = {"task": "transcribe", "num_beams": 1, "do_sample": False}
        language = lang if lang in ("hi", "en") else (os.environ.get("WHISPER_LANGUAGE") or None)
        if language:
            gk["language"] = language
        text = self._load()({"raw": audio, "sampling_rate": 16000}, generate_kwargs=gk)["text"].strip()
        return {"text": text, "language": language or "auto"}


async def main() -> None:
    secret = os.environ["RT_SECRET"].encode()
    limits = Limits(max_global=int(os.environ.get("RT_MAX_GLOBAL", "2")))
    sem = asyncio.Semaphore(limits.max_global)
    engine = WhisperEngine()

    async def handler(ws):
        await serve_connection(ws, service="stt", engine=engine, secret=secret, limits=limits, global_sem=sem)

    await serve_forever(handler)                                  # TLS required on any non-loopback bind (common.check_bind)


if __name__ == "__main__":
    asyncio.run(main())
