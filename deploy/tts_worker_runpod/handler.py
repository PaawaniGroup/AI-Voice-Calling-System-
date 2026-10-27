"""MME TTS worker for RunPod Serverless (Chatterbox Multilingual).

Voice Studio (offline change, not built, not deployed; the GPU paths are UNVERIFIED): task "register_voice" (name, reference_audio_base64: a 3-30 s WAV; prepared once, cached by
name and checksum, least-recently-used cap MAX_REGISTERED_VOICES), task "voices" (list the names) and, for a synth request that names an unknown voice, the structured answer
{"error": "needs_reference", "code": "needs_reference", ...} instead of a crash. ALLOW_REQUEST_REF stays off by default. See docs/VOICE_STUDIO_DESIGN.md.

What changed against the previous handler:
  * The voice is prepared ONCE per worker, at start-up, and kept by NAME (the baked /app/ref_voice.wav as DEFAULT_VOICE, plus any /app/voices/<name>.wav).
    A request picks it with {"input": {"voice": "<name>"}}; no reference audio is read, hashed or prepared per request.
  * Reference audio in the request (reference_audio_base64) is IGNORED unless ALLOW_REQUEST_REF=1. Old clients that still send it keep working
    (the baked voice is used) and simply stop paying for it on the worker; the client should stop sending it to save ~0.77 MB of upload per request.
  * New output_format "ulaw8k": 8 kHz G.711 mu-law, 8000 bytes per second of speech, ready for Plivo. "wav" (default) and "pcm16" are unchanged.
  * Start-up logs the voice name and how long preparing it took.
  * `python handler.py --self-test` checks everything that does not need a GPU (no torch, no chatterbox, no network, no model).

Request:  {"input": {"text": "...", "language": "hi", "voice": "utkarsh", "output_format": "ulaw8k",
                     "exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8}}      (voice / output_format / knobs optional)
Response: {"audio_base64", "format", "sample_rate", "language", "voice", "timing": {...}}  or  {"error": "..."}
"""
import array
import base64
import binascii
import collections
import contextlib
import copy
import glob
import hashlib
import inspect
import io
import math
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import warnings
import wave

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

# Reference voice baked into the image. Override with env vars.
DEFAULT_REF_WAV = os.getenv("DEFAULT_REF_WAV", "/app/ref_voice.wav")
DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "utkarsh")          # the name of the voice made from DEFAULT_REF_WAV
VOICES_DIR = os.getenv("VOICES_DIR", "/app/voices")            # optional: more voices, one <name>.wav each
# Legacy: accept reference_audio_base64 in a request (0 = ignored). When on, that voice is prepared on EVERY such request (slow).
ALLOW_REQUEST_REF = os.getenv("ALLOW_REQUEST_REF", "0") == "1"
WARMUP_TEXT = "नमस्ते, कैसे हैं आप?"
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "600"))
MAX_REF_BYTES = 15 * 1024 * 1024
OUTPUT_FORMATS = ("wav", "pcm16", "ulaw8k")

# Voice Studio (task register_voice): voices registered at run time. UNVERIFIED on a GPU: only the logic around prepare_conditionals() is tested (fake model).
MAX_REGISTERED_VOICES = max(1, int(os.getenv("MAX_REGISTERED_VOICES", "8")))   # least-recently-used cap on registered voices (baked voices are never evicted)
MAX_REGISTER_BYTES = int(os.getenv("MAX_REGISTER_BYTES", str(8 * 1024 * 1024)))
MIN_REGISTER_S, MAX_REGISTER_S = 3.0, 30.0
VOICE_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
RESERVED_VOICE_NAMES = {"request"}                             # "request" is the slot of the legacy ALLOW_REQUEST_REF path

ALLOWED_LANGUAGES = {
    "ar", "da", "de", "el", "en", "es", "fi", "fr",
    "he", "hi", "it", "ja", "ko", "ms", "nl", "no",
    "pl", "pt", "ru", "sv", "sw", "tr", "zh",
}

GEN_LOCK = threading.Lock()
MODEL = None            # the Chatterbox model (a fake one in --self-test)
_torch = None           # set by load_model(); None means "no torch": generate_audio then uses the model's output as it is (self-test)
VOICES = {}             # voice name -> prepared conditionals, built ONCE per worker at start-up (or by register_voice)
BAKED = set()           # names of the voices made at start-up (image + VOICES_DIR): never evicted, never overwritten
REGISTERED = collections.OrderedDict()   # name -> {"checksum", "duration_s"} of voices made by register_voice, least recently used first
_active_voice = None


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------------------------------------- model and voices
def load_kwargs(from_pretrained, env):
    """The arguments for `from_pretrained`: `t3_model` (env T3_MODEL, default "v3") is passed ONLY when the installed Chatterbox names that parameter.
    Returns (kwargs, label for the log line, warning text or None)."""
    want = env.get("T3_MODEL", "v3")
    try:
        params = inspect.signature(from_pretrained).parameters
    except (TypeError, ValueError):
        params = {}
    if "t3_model" in params:
        return {"device": "cuda", "t3_model": want}, want, None
    return ({"device": "cuda"}, "not-supported",
            f"WARNING: this Chatterbox version's from_pretrained has no t3_model parameter: T3_MODEL={want!r} is ignored (the library default model is used)")


def loaded_line(seconds: float, t3_label: str) -> str:
    return f"[TTS] model loaded in {seconds:.2f}s (t3_model={t3_label})"


def load_model():
    """Load the model ONCE at start-up (never inside a request). Needs a CUDA GPU."""
    global MODEL, _torch
    import torch
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU is not available")
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    kwargs, t3_label, warning = load_kwargs(ChatterboxMultilingualTTS.from_pretrained, os.environ)
    if warning:
        log(f"[TTS] {warning}")
    t = time.perf_counter()
    MODEL = ChatterboxMultilingualTTS.from_pretrained(**kwargs)
    _torch = torch
    log(loaded_line(time.perf_counter() - t, t3_label))


def _clone_conds(conds):
    """A copy of a prepared voice that the model may change without touching the stored one. SHALLOW on purpose: Chatterbox's conditionals hold tensors that
    are results of computations (not leaves), and PyTorch refuses to deep-copy those ("Only Tensors created explicitly by the user (graph leaves) support the
    deepcopy protocol"); that error crashed every worker at start-up in the first v3 image. generate() REBINDS `conds.t3` when the exaggeration differs (never
    edits the tensors) and S3Gen converts the entries of `conds.gen` in place: so the object and the small `gen` dict are copied, the tensors are shared."""
    c = copy.copy(conds)
    if isinstance(getattr(c, "gen", None), dict):
        c.gen = dict(c.gen)
    return c


def prepare_voice(name: str, wav_path: str) -> float:
    """Prepare one voice (the slow part of Chatterbox) and keep its conditionals under `name`. Returns the seconds it took."""
    global _active_voice
    _active_voice = None   # prepare_conditionals() overwrites MODEL.conds (even when it fails): nothing is "active" until select_voice() runs again
    t0 = time.perf_counter()
    MODEL.prepare_conditionals(wav_path)
    conds = getattr(MODEL, "conds", None)
    if conds is None:   # fail at start-up, loudly, instead of serving the wrong voice
        raise RuntimeError("the model has no .conds after prepare_conditionals(): this Chatterbox version cannot cache voices by name")
    VOICES[name] = _clone_conds(conds)
    return time.perf_counter() - t0


def select_voice(name: str) -> None:
    """Make `name` the active voice. A dictionary lookup: no audio is read and nothing is prepared. Call inside GEN_LOCK."""
    global _active_voice
    if name != _active_voice:
        MODEL.conds = _clone_conds(VOICES[name])
        _active_voice = name


def init_voices() -> None:
    """Prepare every voice once and log its name and how long it took."""
    if os.path.exists(DEFAULT_REF_WAV):
        spent = prepare_voice(DEFAULT_VOICE, DEFAULT_REF_WAV)
        BAKED.add(DEFAULT_VOICE)
        log(f"[TTS] voice '{DEFAULT_VOICE}' prepared in {spent:.2f}s (source {DEFAULT_REF_WAV})")
    else:
        log(f"[TTS] WARNING: default reference not found at {DEFAULT_REF_WAV}")
    for path in sorted(glob.glob(os.path.join(VOICES_DIR, "*.wav"))):
        name = os.path.splitext(os.path.basename(path))[0]
        if name in VOICES:
            log(f"[TTS] WARNING: voice '{name}' in {VOICES_DIR} is skipped: the name is already taken")
            continue
        spent = prepare_voice(name, path)
        BAKED.add(name)
        log(f"[TTS] voice '{name}' prepared in {spent:.2f}s (source {path})")
    if not VOICES:
        log("[TTS] WARNING: no voice is available: every request will fail until a voice is baked into the image")


def generate_audio(text, language, **gen_kwargs):
    if _torch is None:   # no torch (self-test): the model returns a numpy array
        return np.atleast_1d(np.asarray(MODEL.generate(text, language_id=language, **gen_kwargs), dtype=np.float32))
    with _torch.inference_mode():
        wav = MODEL.generate(text, language_id=language, **gen_kwargs)
    _torch.cuda.synchronize()
    return np.atleast_1d(wav.detach().float().cpu().squeeze().numpy())


def warm_up() -> None:
    """Warm up the CUDA kernels with the default voice so the first real request is fast."""
    if not VOICES:
        return
    with GEN_LOCK:
        select_voice(DEFAULT_VOICE if DEFAULT_VOICE in VOICES else next(iter(VOICES)))
        t = time.perf_counter()
        generate_audio(WARMUP_TEXT, "hi")
        generate_audio(WARMUP_TEXT, "hi")
    log(f"[TTS] warmup done in {time.perf_counter() - t:.2f}s")


# ---------------------------------------------------------------------------------------------------------- Voice Studio tasks
def task_error(code: str, message: str, **extra) -> dict:
    """The structured error of the voice tasks: `error` and `code` are the same short word, `message` is for people. Never holds audio."""
    return {"error": code, "code": code, "message": message, **extra}


def needs_reference_response(voice) -> dict:
    return task_error("needs_reference", "this worker has no such voice: send register_voice with the reference clip and ask again",
                      voice=voice if isinstance(voice, str) and len(voice) <= 40 else None, voices=sorted(n for n in VOICES if n not in RESERVED_VOICE_NAMES))


def valid_voice_name(name) -> bool:
    return isinstance(name, str) and VOICE_NAME_RE.fullmatch(name) is not None


def wav_duration_s(data: bytes) -> float:
    """Seconds of audio in a PCM WAV, read from its header with the standard library. Raises ValueError when the bytes are not a readable WAV."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            frames, rate = w.getnframes(), w.getframerate()
    except (wave.Error, EOFError, ValueError, OverflowError) as exc:
        raise ValueError(f"not a readable WAV ({type(exc).__name__})") from None
    if rate <= 0 or frames <= 0:
        raise ValueError("the WAV holds no audio")
    return frames / rate


def list_voices() -> dict:
    """Task `voices`: the names this worker can speak with now. Baked voices first-class, registered ones in least-recently-used order (oldest first)."""
    with GEN_LOCK:
        return {"ok": True, "voices": sorted(n for n in VOICES if n not in RESERVED_VOICE_NAMES), "baked": sorted(BAKED), "registered": list(REGISTERED),
                "max_registered": MAX_REGISTERED_VOICES}


def register_voice(data: dict) -> dict:
    """Task `register_voice`: validate, prepare ONCE and cache by name and checksum. UNVERIFIED on a GPU (prepare_conditionals with an arbitrary clip).
    Everything that can be checked from the request alone is checked BEFORE the GPU is touched."""
    global _active_voice
    name = data.get("name")
    if not valid_voice_name(name):
        return task_error("invalid_name", "name must be 1-40 letters, digits, '_' or '-'")
    if name in BAKED or name in RESERVED_VOICE_NAMES:
        return task_error("name_reserved", "this name belongs to a voice that is part of the worker and cannot be replaced")
    b64 = data.get("reference_audio_base64")
    if not isinstance(b64, str) or not b64:
        return task_error("invalid_audio", "reference_audio_base64 is required")
    if len(b64) > MAX_REGISTER_BYTES * 4 // 3 + 8:
        return task_error("too_large", f"the reference audio must be {MAX_REGISTER_BYTES} bytes or smaller")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return task_error("invalid_audio", "reference_audio_base64 is not valid base64")
    if len(raw) > MAX_REGISTER_BYTES:
        return task_error("too_large", f"the reference audio must be {MAX_REGISTER_BYTES} bytes or smaller")
    try:
        duration = wav_duration_s(raw)
    except ValueError as exc:
        return task_error("invalid_audio", str(exc))
    if duration < MIN_REGISTER_S:
        return task_error("too_short", f"the reference audio must be at least {MIN_REGISTER_S:g} seconds")
    if duration > MAX_REGISTER_S:
        return task_error("too_long", f"the reference audio must be at most {MAX_REGISTER_S:g} seconds")
    checksum = hashlib.sha256(raw).hexdigest()
    try:
        with GEN_LOCK:
            known = REGISTERED.get(name)
            if known is not None and known["checksum"] == checksum and name in VOICES:   # same name, same audio: nothing to prepare
                REGISTERED.move_to_end(name)
                return {"ok": True, "name": name, "checksum": checksum, "duration_s": round(duration, 3), "cached": True, "prepare_s": 0.0,
                        "registered": len(REGISTERED), "max_registered": MAX_REGISTERED_VOICES}
            with tempfile.NamedTemporaryFile(suffix=".wav") as f:
                f.write(raw)
                f.flush()
                spent = prepare_voice(name, f.name)
            REGISTERED[name] = {"checksum": checksum, "duration_s": duration}
            REGISTERED.move_to_end(name)
            evicted = []
            while len(REGISTERED) > MAX_REGISTERED_VOICES:
                old = next(iter(REGISTERED))
                del REGISTERED[old]
                VOICES.pop(old, None)
                if _active_voice == old:
                    _active_voice = None
                evicted.append(old)
        log(f"[TTS_REGISTER] voice={name} duration_s={duration:.2f} prepare_s={spent:.3f} registered={len(REGISTERED)} evicted={len(evicted)}")
        return {"ok": True, "name": name, "checksum": checksum, "duration_s": round(duration, 3), "cached": False, "prepare_s": round(spent, 3),
                "registered": len(REGISTERED), "max_registered": MAX_REGISTERED_VOICES, "evicted": evicted}
    except Exception as exc:
        traceback.print_exc()
        log(f"[TTS_ERROR] register_voice failed: {type(exc).__name__}")
        return task_error("prepare_failed", "the worker could not prepare this voice", type=type(exc).__name__)


# ---------------------------------------------------------------------------------------------------------- audio encoding
def pcm16_to_ulaw(pcm: np.ndarray) -> bytes:
    """G.711 mu-law, the Sun 14-bit reference algorithm: bit-identical to Python's audioop.lin2ulaw (checked on all 65536 int16 values)."""
    seg_end = np.array([0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF], dtype=np.int32)
    v = pcm.astype(np.int32) >> 2
    mask = np.where(v < 0, 0x7F, 0xFF)
    v = np.minimum(np.abs(v), 8159) + 33
    seg = (v[:, None] > seg_end[None, :]).sum(axis=1)
    uval = np.where(seg >= 8, 0x7F, (seg << 4) | ((v >> (seg + 1)) & 0x0F))
    return (uval ^ mask).astype(np.uint8).tobytes()


def encode_audio(audio: np.ndarray, sr: int, output_format: str) -> str:
    if output_format == "ulaw8k":   # telephony: 8 kHz mu-law, 8000 bytes per second of speech (the model rate is a multiple of 8 kHz)
        a8 = resample_poly(audio, 1, sr // 8000)
        return base64.b64encode(pcm16_to_ulaw((np.clip(a8, -1.0, 1.0) * 32767.0).astype(np.int16))).decode("ascii")
    if output_format == "pcm16":
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
        return base64.b64encode(pcm.tobytes()).decode("ascii")
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------------------------------------- the RunPod handler
def handler(job):
    data = job.get("input", {}) or {}

    # Cheap ping so your server can pre-warm without synthesizing anything
    if data.get("warmup"):
        return {"ok": True}

    task = data.get("task")
    if task == "voices":
        return list_voices()
    if task == "register_voice":
        return register_voice(data)
    if task not in (None, "synth"):
        return task_error("unknown_task", "task must be 'synth', 'register_voice' or 'voices'")

    total_start = time.perf_counter()

    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return {"error": "Provide a non-empty input.text"}
    text = text.strip()[:MAX_TEXT_CHARS]

    language = data.get("language", "hi")
    if language not in ALLOWED_LANGUAGES:
        return {"error": f"Unsupported language: {language}"}

    output_format = data.get("output_format", "wav")  # "wav" (default), "pcm16" (model rate) or "ulaw8k" (8 kHz mu-law)
    if output_format not in OUTPUT_FORMATS:
        return {"error": "output_format must be 'wav', 'pcm16' or 'ulaw8k'"}
    if output_format == "ulaw8k" and MODEL.sr % 8000:
        return {"error": f"ulaw8k needs a model rate that is a multiple of 8000 (got {MODEL.sr})"}

    voice = data.get("voice", DEFAULT_VOICE)
    if not isinstance(voice, str):
        return task_error("invalid_voice", "voice must be a string")
    if voice not in VOICES and not (ALLOW_REQUEST_REF and data.get("reference_audio_base64")):
        return needs_reference_response(voice)   # a structured answer, not a crash: the client registers the voice and asks again

    # Optional generation knobs (only passed when provided)
    gen_kwargs = {}
    for key in ("exaggeration", "cfg_weight", "temperature"):
        if key in data:
            gen_kwargs[key] = float(data[key])

    try:
        with GEN_LOCK:
            ref_s = 0.0
            ref_b64 = data.get("reference_audio_base64") if ALLOW_REQUEST_REF else None
            if ref_b64:   # legacy path (off by default): a voice sent in the request is prepared on EVERY such request (slow)
                ref_bytes = base64.b64decode(ref_b64, validate=True)
                if len(ref_bytes) > MAX_REF_BYTES:
                    return {"error": "Reference audio must be 15 MB or smaller"}
                with tempfile.NamedTemporaryFile(suffix=".wav") as f:
                    f.write(ref_bytes)
                    f.flush()
                    ref_s = prepare_voice("request", f.name)
                voice = "request"
            if voice not in VOICES:   # evicted by a register_voice between the check above and now
                return needs_reference_response(voice)
            if voice in REGISTERED:
                REGISTERED.move_to_end(voice)   # used: it becomes the most recently used
            select_voice(voice)   # by name: no reference audio is read or prepared per request

            t0 = time.perf_counter()
            audio = generate_audio(text, language, **gen_kwargs)
            generate_s = time.perf_counter() - t0

        t0 = time.perf_counter()
        audio_b64 = encode_audio(audio, MODEL.sr, output_format)
        encode_s = time.perf_counter() - t0

        audio_s = len(audio) / MODEL.sr
        total_s = time.perf_counter() - total_start
        log(
            "[TTS_TIMING] "
            f"chars={len(text)} audio_s={audio_s:.2f} ref_s={ref_s:.3f} voice={voice} "
            f"generate_s={generate_s:.3f} encode_s={encode_s:.3f} "
            f"total_s={total_s:.3f} rtf={generate_s / max(audio_s, 1e-6):.2f} fmt={output_format}"
        )

        return {
            "audio_base64": audio_b64,
            "format": output_format,
            "sample_rate": 8000 if output_format == "ulaw8k" else MODEL.sr,
            "language": language,
            "voice": voice,
            "timing": {
                "generate_s": round(generate_s, 3),
                "total_s": round(total_s, 3),
                "audio_s": round(audio_s, 3),
            },
        }

    except Exception as exc:
        traceback.print_exc()
        log(f"[TTS_ERROR] {exc}")
        return {"error": str(exc), "type": type(exc).__name__}


# ---------------------------------------------------------------------------------------------------------- self-test (no GPU)
def selftest_voice_tasks(check) -> None:
    """The register_voice / voices / needs_reference checks with a FAKE model. Needs the standard library only (no numpy: the audio is built with `wave`),
    so tests can run it with the heavy imports stubbed. It restores every module setting it changes."""
    global MODEL, _torch, VOICES, _active_voice, MAX_REGISTERED_VOICES, MAX_REGISTER_BYTES, ALLOW_REQUEST_REF, log
    saved = (MODEL, _torch, VOICES, _active_voice, MAX_REGISTERED_VOICES, MAX_REGISTER_BYTES, ALLOW_REQUEST_REF, log, set(BAKED), collections.OrderedDict(REGISTERED))

    def tone(seconds, amp=0.3, rate=24000, freq=220):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(array.array("h", (int(32767 * amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(int(rate * seconds)))).tobytes())
        return buf.getvalue()

    def b64(b):
        return base64.b64encode(b).decode("ascii")

    class FakeVoiceModel:
        sr = 24000

        def __init__(self):
            self.conds, self.prepares = None, []

        def prepare_conditionals(self, path):
            with wave.open(path, "rb") as w:
                frames = array.array("h", w.readframes(min(w.getnframes(), 2400)))
            self.prepares.append(os.path.basename(path))
            self.conds = {"amp": max(abs(s) for s in frames) / 32768.0}

    def register(name, audio, **extra):
        return handler({"input": {"task": "register_voice", "name": name, "reference_audio_base64": b64(audio) if isinstance(audio, bytes) else audio, **extra}})

    try:
        log = lambda m: None   # noqa: E731
        MODEL, _torch, VOICES, _active_voice, ALLOW_REQUEST_REF = FakeVoiceModel(), None, {}, None, False
        BAKED.clear()
        REGISTERED.clear()
        MAX_REGISTERED_VOICES, MAX_REGISTER_BYTES = 2, 8 * 1024 * 1024
        VOICES["utkarsh"] = {"amp": 0.2}
        BAKED.add("utkarsh")
        ten = tone(10.0, 0.3)
        prepares = lambda: len(MODEL.prepares)   # noqa: E731

        # --- validation happens before the GPU is touched
        for bad in ("", "a b", "x" * 41, "../x", "a/b", "é", None, 5):
            r = register(bad, ten)
            check(f"register_voice: name {str(bad)[:12]!r} is refused (invalid_name)", r.get("code") == "invalid_name", str(r)[:120])
        check("register_voice: a name of exactly 40 letters, digits, _ and - is valid", valid_voice_name("a" * 36 + "_-9Z"))
        check("register_voice: a baked voice cannot be replaced (name_reserved)", register("utkarsh", ten).get("code") == "name_reserved")
        check("register_voice: the legacy 'request' slot is reserved", register("request", ten).get("code") == "name_reserved")
        check("register_voice: missing audio is refused", register("v_a", "").get("code") == "invalid_audio")
        check("register_voice: invalid base64 is refused", register("v_a", "!!!not base64!!!").get("code") == "invalid_audio")
        check("register_voice: bytes that are not a WAV are refused", register("v_a", b"this is not a wav file").get("code") == "invalid_audio")
        check("register_voice: 2 s is too short", register("v_a", tone(2.0)).get("code") == "too_short")
        check("register_voice: 31 s is too long", register("v_a", tone(31.0, rate=8000)).get("code") == "too_long")
        MAX_REGISTER_BYTES = 1000
        check("register_voice: audio over MAX_REGISTER_BYTES is refused (too_large)", register("v_a", ten).get("code") == "too_large")
        MAX_REGISTER_BYTES = 8 * 1024 * 1024
        check("register_voice: refused requests prepared nothing", prepares() == 0, str(MODEL.prepares))

        # --- prepare once, cache by name and checksum
        r = register("v_a", ten)
        check("register_voice: a valid clip is prepared", r.get("ok") is True and r.get("cached") is False and prepares() == 1 and "v_a" in VOICES, str(r)[:160])
        check("register_voice: the answer carries the checksum, the duration and the time", len(r.get("checksum", "")) == 64 and abs(r.get("duration_s", 0) - 10.0) < 0.01 and "prepare_s" in r)
        r2 = register("v_a", ten)
        check("register_voice: the same name and checksum again prepares nothing (cached)", r2.get("cached") is True and prepares() == 1 and r2["checksum"] == r["checksum"])
        r3 = register("v_a", tone(10.0, 0.6))
        check("register_voice: the same name with other audio is prepared again and replaces the voice",
              r3.get("cached") is False and prepares() == 2 and r3["checksum"] != r["checksum"] and abs(VOICES["v_a"]["amp"] - 0.6) < 0.02)
        check("register_voice: preparing leaves no voice marked active (the next synth selects its own)", _active_voice is None)

        # --- the list of voices
        v = handler({"input": {"task": "voices"}})
        check("voices: lists baked and registered names and the cap", v.get("voices") == ["utkarsh", "v_a"] and v.get("baked") == ["utkarsh"] and v.get("registered") == ["v_a"] and v.get("max_registered") == 2, str(v))

        # --- least-recently-used cap (2): the baked voice is outside it
        register("v_b", tone(10.0, 0.4))
        register("v_a", tone(10.0, 0.6))            # touches v_a: v_b is now the oldest
        r = register("v_c", tone(10.0, 0.5))
        check("LRU: the cap evicts the least recently used registered voice", r.get("evicted") == ["v_b"] and sorted(VOICES) == ["utkarsh", "v_a", "v_c"], str(r)[:160])
        check("LRU: the baked voice is never evicted", "utkarsh" in VOICES and "utkarsh" in BAKED)
        e = handler({"input": {"text": "namaste", "language": "hi", "voice": "v_b"}})
        check("an evicted voice answers needs_reference (structured, not a crash)", e.get("error") == "needs_reference" and e.get("code") == "needs_reference" and e.get("voice") == "v_b" and "message" in e, str(e))

        # --- needs_reference and the legacy reference switch
        before = prepares()
        e = handler({"input": {"text": "namaste", "language": "hi", "voice": "nobody", "reference_audio_base64": b64(ten)}})
        check("a reference inside a synth request is ignored by default (ALLOW_REQUEST_REF off): needs_reference, nothing prepared", e.get("code") == "needs_reference" and prepares() == before)
        check("a voice name that is not a string is an error, not a crash", handler({"input": {"text": "namaste", "voice": ["x"]}}).get("code") == "invalid_voice")
        check("an unknown task is an error", handler({"input": {"task": "delete_everything"}}).get("code") == "unknown_task")
        check("the answers of the voice tasks never hold the audio", "reference_audio_base64" not in str(register("v_a", tone(2.0))) and "reference_audio_base64" not in str(v))
    except Exception as exc:                                                                          # noqa: BLE001: a crash IS the failure this case exists to catch
        check("voice tasks: no exception", False, f"{type(exc).__name__}: {str(exc)[:110]}")
    finally:
        MODEL, _torch, VOICES, _active_voice, MAX_REGISTERED_VOICES, MAX_REGISTER_BYTES, ALLOW_REQUEST_REF, log = saved[:8]
        BAKED.clear()
        BAKED.update(saved[8])
        REGISTERED.clear()
        REGISTERED.update(saved[9])


def run_self_test() -> int:
    """`python handler.py --self-test`: checks voice caching, the request contract, the three output formats and the mu-law encoder with a FAKE
    model. Needs numpy, scipy and soundfile only: no torch, no chatterbox, no GPU, no network. Exit code 0 = all passed."""
    global MODEL, _torch, VOICES, _active_voice, DEFAULT_REF_WAV, DEFAULT_VOICE, VOICES_DIR, ALLOW_REQUEST_REF, log
    results, lines = [], []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""), flush=True)

    class GraphConds:
        """Stands in for Chatterbox's Conditionals: its tensors are results of computations (graph tensors) and PyTorch refuses to deep-copy them."""

        def __init__(self, amp):
            self.t3, self.gen = ("t3", amp), {"amp": amp}

        def __deepcopy__(self, memo):
            raise RuntimeError("Only Tensors created explicitly by the user (graph leaves) support the deepcopy protocol at the moment")

    class FakeModel:
        sr = 24000

        def __init__(self, graph=False):
            self.conds, self.prepares, self.graph = None, [], graph

        def prepare_conditionals(self, path):
            data, _ = sf.read(path, dtype="float32")
            self.prepares.append(os.path.basename(path))
            amp = float(np.abs(data).max())                      # the "voice" of the fake model is its loudness
            self.conds = GraphConds(amp) if self.graph else {"amp": amp}

        def generate(self, text, language_id="hi", **kw):
            c = self.conds
            amp = c["amp"] if isinstance(c, dict) else c.gen["amp"]
            if not isinstance(c, dict):
                c.gen["_touched"] = True                         # S3Gen converts the entries of the reference dict in place
                if "exaggeration" in kw:
                    c.t3 = ("t3-exaggeration", kw["exaggeration"])   # generate() REBINDS conds.t3 when the exaggeration differs
            n = int(self.sr * 0.1 * max(1, len(text.split())))
            t = np.arange(n) / self.sr
            return (amp * np.sin(2 * np.pi * 300 * t)).astype(np.float32)

    def ulaw_ref(v):   # the Sun reference algorithm, one sample, plain Python
        seg_end = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)
        x = v >> 2
        mask = 0x7F if x < 0 else 0xFF
        x = min(-x if x < 0 else x, 8159) + 33
        seg = sum(1 for t in seg_end if x > t)
        return (0x7F if seg >= 8 else ((seg << 4) | ((x >> (seg + 1)) & 0x0F))) ^ mask

    def ulaw_decode(b):   # standard G.711 mu-law decode
        u = ~np.frombuffer(b, dtype=np.uint8).astype(np.int32) & 0xFF
        mag = (((u & 0x0F) << 3) + 0x84 << ((u >> 4) & 7)) - 0x84
        return np.where(u & 0x80, -mag, mag)

    print("self-test (no GPU, fake model)")
    # --- 0. the shipped defaults (read BEFORE the test overrides any setting)
    check("default: a reference sent in the request is ignored (ALLOW_REQUEST_REF is off unless the environment turns it on)",
          ALLOW_REQUEST_REF == (os.getenv("ALLOW_REQUEST_REF", "0") == "1"))
    check("default: the voice is named by DEFAULT_VOICE and read from DEFAULT_REF_WAV (baked into the image)",
          DEFAULT_VOICE == os.getenv("DEFAULT_VOICE", "utkarsh") and DEFAULT_REF_WAV == os.getenv("DEFAULT_REF_WAV", "/app/ref_voice.wav"))
    # --- 1. mu-law encoder
    allv = np.arange(-32768, 32768, dtype=np.int16)
    mine = pcm16_to_ulaw(allv)
    check("mu-law: all 65536 int16 values match the reference algorithm", mine == bytes(ulaw_ref(int(v)) for v in allv))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import audioop
        check("mu-law: identical to audioop.lin2ulaw", mine == audioop.lin2ulaw(allv.tobytes(), 2))
    except ImportError:
        print("  [SKIP] audioop is not available in this Python (removed in 3.13)")
    sr = 24000
    t = np.arange(sr) / sr
    tone = (0.5 * np.sin(2 * np.pi * 300 * t) + 0.2 * np.sin(2 * np.pi * 1200 * t)).astype(np.float32)
    u = base64.b64decode(encode_audio(tone, sr, "ulaw8k"))
    check("ulaw8k: 1 s of 24 kHz audio is exactly 8000 bytes", len(u) == 8000, f"{len(u)} bytes")
    ref8 = np.clip(resample_poly(tone, 1, 3), -1, 1) * 32767
    dec = ulaw_decode(u)
    if len(dec) == len(ref8):
        snr = 10 * np.log10((ref8 ** 2).mean() / max(((dec - ref8) ** 2).mean(), 1e-9))
        check("ulaw8k: decodes back to the 8 kHz signal (SNR above 25 dB)", snr > 25, f"{snr:.1f} dB")
    else:
        check("ulaw8k: decodes back to the 8 kHz signal (SNR above 25 dB)", False, f"length {len(dec)} != {len(ref8)}")

    # --- 2. voices: prepared once, selected by name
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "voices"))
        sf.write(os.path.join(d, "ref.wav"), 0.2 * np.sin(2 * np.pi * 200 * np.arange(8000) / 16000), 16000)
        sf.write(os.path.join(d, "voices", "priya.wav"), 0.6 * np.sin(2 * np.pi * 200 * np.arange(8000) / 16000), 16000)
        MODEL, _torch, VOICES, _active_voice = FakeModel(), None, {}, None
        BAKED.clear()
        REGISTERED.clear()
        DEFAULT_REF_WAV, DEFAULT_VOICE, VOICES_DIR, ALLOW_REQUEST_REF = os.path.join(d, "ref.wav"), "utkarsh", os.path.join(d, "voices"), False
        real_log, log = log, (lambda m: lines.append(m))
        init_voices()
        warm_up()
        log = lambda m: None   # the per-request timing lines are not part of the report   # noqa: E731
        check("start-up logs each voice with its name and preparation time",
              any(l.startswith("[TTS] voice 'utkarsh' prepared in ") and l.split(" prepared in ")[1].split("s")[0].replace(".", "").isdigit() for l in lines)
              and any(l.startswith("[TTS] voice 'priya' prepared in ") for l in lines), "; ".join(lines))
        check("each voice is prepared exactly once at start-up", MODEL.prepares == ["ref.wav", "priya.wav"], str(MODEL.prepares))

        def peak(resp):
            data, _ = sf.read(io.BytesIO(base64.b64decode(resp["audio_base64"])), dtype="float32")
            return float(np.abs(data).max())

        base = {"text": "यह एक छोटा सा परीक्षण वाक्य है", "language": "hi"}
        before = len(MODEL.prepares)
        peaks = {"utkarsh": [], "priya": []}
        for i in range(20):                                        # alternate voices 20 times: no preparation may happen
            v = "priya" if i % 2 else "utkarsh"
            peaks[v].append(peak(handler({"input": {**base, "voice": v}})))
        check("20 requests alternating two voices prepare nothing", len(MODEL.prepares) == before, str(MODEL.prepares))
        check("each request is synthesized with the voice it named",
              all(abs(p - 0.2) < 0.02 for p in peaks["utkarsh"]) and all(abs(p - 0.6) < 0.02 for p in peaks["priya"]), str({k: (min(v), max(v)) for k, v in peaks.items()}))
        r = handler({"input": base})
        check("no voice named = the default voice", r.get("voice") == "utkarsh" and abs(peak(r) - 0.2) < 0.02)
        e = handler({"input": {**base, "voice": "nobody"}})
        check("an unknown voice is a structured needs_reference answer that lists the voices",
              e.get("error") == "needs_reference" and e.get("code") == "needs_reference" and e.get("voice") == "nobody" and e.get("voices") == ["priya", "utkarsh"], str(e))

        # --- 3. a reference sent in the request
        junk = base64.b64encode(b"this is not a wav file").decode()
        r = handler({"input": {**base, "reference_audio_base64": junk}})
        check("reference_audio_base64 is ignored by default (old clients keep working, nothing is decoded or prepared)",
              "error" not in r and r["voice"] == "utkarsh" and len(MODEL.prepares) == before)
        ALLOW_REQUEST_REF = True
        with open(os.path.join(d, "voices", "priya.wav"), "rb") as fh:
            wavb64 = base64.b64encode(fh.read()).decode()
        r1, r2 = handler({"input": {**base, "reference_audio_base64": wavb64}}), handler({"input": {**base, "reference_audio_base64": wavb64}})
        check("ALLOW_REQUEST_REF=1: the legacy path works and prepares on every such request",
              r1.get("voice") == "request" and r2.get("voice") == "request" and len(MODEL.prepares) == before + 2 and abs(peak(r1) - 0.6) < 0.02)
        with contextlib.redirect_stderr(io.StringIO()):   # the handler prints the traceback of the failure it reports
            e = handler({"input": {**base, "reference_audio_base64": junk}})
        check("ALLOW_REQUEST_REF=1: an undecodable reference returns an error, not a crash", "error" in e, str(e))
        ALLOW_REQUEST_REF = False

        # --- 4. contract and formats
        check("warmup ping", handler({"input": {"warmup": True}}) == {"ok": True})
        check("empty text is an error", "error" in handler({"input": {"text": "  "}}))
        check("unsupported language is an error", "error" in handler({"input": {**base, "language": "xx"}}))
        check("unsupported output_format is an error", "error" in handler({"input": {**base, "output_format": "mp3"}}))
        n_words = len(base["text"].split())
        n = int(24000 * 0.1 * n_words)
        w = handler({"input": {**base, "output_format": "wav"}})
        data, wsr = sf.read(io.BytesIO(base64.b64decode(w["audio_base64"])), dtype="float32")
        check("wav (default): a WAV at the model rate", w["format"] == "wav" and w["sample_rate"] == wsr == 24000 and len(data) == n)
        p = handler({"input": {**base, "output_format": "pcm16"}})
        check("pcm16: raw 16-bit samples at the model rate", p["format"] == "pcm16" and p["sample_rate"] == 24000 and len(base64.b64decode(p["audio_base64"])) == 2 * n)
        ul = handler({"input": {**base, "output_format": "ulaw8k"}})
        check("ulaw8k: 8 kHz, one byte per sample, a third of the samples",
              ul["format"] == "ulaw8k" and ul["sample_rate"] == 8000 and len(base64.b64decode(ul["audio_base64"])) == n // 3, str((len(base64.b64decode(ul["audio_base64"])), n // 3)))
        check("response fields", set(ul) == {"audio_base64", "format", "sample_rate", "language", "voice", "timing"} and
              set(ul["timing"]) == {"generate_s", "total_s", "audio_s"})

        # --- 5. the voice object of the REAL model refuses deepcopy (graph tensors): the voice cache must not depend on it. The first v3 image deep-copied it and
        #        every worker crashed at start-up with "Only Tensors created explicitly by the user (graph leaves) support the deepcopy protocol".
        try:
            MODEL, VOICES, _active_voice = FakeModel(graph=True), {}, None
            init_voices()
            r1 = handler({"input": {**base, "voice": "utkarsh", "exaggeration": 0.9}})        # generate() rebinds .t3 on the ACTIVE copy
            r2 = handler({"input": {**base, "voice": "priya"}})
            r3 = handler({"input": {**base, "voice": "utkarsh"}})
            stored = VOICES["utkarsh"]
            check("real-like voice object that refuses deepcopy: both voices prepare at start-up and every request is answered",
                  all("error" not in r for r in (r1, r2, r3)) and abs(peak(r1) - 0.2) < 0.02 and abs(peak(r2) - 0.6) < 0.02 and abs(peak(r3) - 0.2) < 0.02)
            check("a request that changes the exaggeration does not alter the stored voice, nor the next request's voice",
                  stored.t3[0] == "t3" and MODEL.conds.t3[0] == "t3", str((stored.t3, MODEL.conds.t3)))
            check("the stored voice's reference dict is not shared with the active copy", "_touched" not in stored.gen and "_touched" in MODEL.conds.gen)
        except Exception as e:                                                                  # noqa: BLE001: a crash IS the failure this case exists to catch
            check("real-like voice object that refuses deepcopy: start-up and requests work", False, f"{type(e).__name__}: {str(e)[:110]}")

    # --- 5b. Voice Studio tasks (register_voice, voices, needs_reference, LRU): the logic that needs no numpy is one function that pytest also runs with stubs
    selftest_voice_tasks(check)

    # --- 6. load_model(): t3_model is passed only when from_pretrained names it
    def fp_new(device, t3_model=None):
        return None

    def fp_old(device):
        return None
    kw, label, warn = load_kwargs(fp_new, {})
    check("load_model: t3_model is passed (default v3) when from_pretrained names it", kw == {"device": "cuda", "t3_model": "v3"} and label == "v3" and warn is None, str((kw, label, warn)))
    kw, label, warn = load_kwargs(fp_new, {"T3_MODEL": "t3_mtl23ls_v3"})
    check("load_model: the environment variable T3_MODEL overrides it", kw.get("t3_model") == "t3_mtl23ls_v3" and label == "t3_mtl23ls_v3" and warn is None)
    kw, label, warn = load_kwargs(fp_old, {"T3_MODEL": "v3"})
    check("load_model: not passed, and a WARNING, when this Chatterbox has no t3_model parameter",
          kw == {"device": "cuda"} and label == "not-supported" and warn is not None and warn.startswith("WARNING") and "T3_MODEL" in warn, str((kw, label, warn)))
    check("load_model: the 'model loaded' line names the t3_model used", loaded_line(1.234, "v3") == "[TTS] model loaded in 1.23s (t3_model=v3)")
    ok = all(results)
    print(f"{sum(results)}/{len(results)} checks passed" + ("" if ok else "  -> FAILED"))
    return 0 if ok else 1


def main() -> int:
    if "--self-test" in sys.argv or os.getenv("SELF_TEST") == "1":
        return run_self_test()
    import runpod
    load_model()
    init_voices()
    warm_up()
    log(f"[TTS] ready: voices={sorted(VOICES)} default={DEFAULT_VOICE} sample_rate={MODEL.sr} formats={','.join(OUTPUT_FORMATS)} "
        f"request_reference={'allowed' if ALLOW_REQUEST_REF else 'ignored'}")
    runpod.serverless.start({"handler": handler})
    return 0


if __name__ == "__main__":
    sys.exit(main())
