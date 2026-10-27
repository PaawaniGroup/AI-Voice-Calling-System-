"""Clip preparation for Voice Studio: check an uploaded reference clip and turn it into mono, 24 kHz, 16-bit audio with the silence trimmed.

Standard library only (`wave`, `audioop`, `array`): the service's venv has no numpy, scipy, soundfile or ffmpeg. What this accepts without extra tools is
PCM WAV. When an `ffmpeg` executable is on PATH at run time it is used first, so other formats (MP3, M4A, OGG, ...) work too; `decoder_report()` says which case applies.
Resampling is `audioop.ratecv` (linear interpolation, no anti-alias filter): fine for speech, better with ffmpeg for 44.1/48 kHz sources.

Nothing here logs or keeps audio. Every refusal is a `ClipRejected` with a short code, a sentence for people and the measured report (numbers only)."""
from __future__ import annotations

import array
import audioop
import io
import math
import shutil
import subprocess
import wave
from dataclasses import dataclass

TARGET_RATE = 24000
MIN_S, MAX_S = 3.0, 30.0                 # the worker accepts 3-30 s of reference audio
MAX_INPUT_S = 120.0                      # longer input is refused before any analysis (cost guard)
FRAME_S = 0.02
PAD_S = 0.15                             # silence kept on each side after trimming
MAX_CLIPPED_FRACTION = 0.005             # more than 0.5% of the samples at full scale: refused
MIN_PEAK_DBFS = -40.0                    # quieter than this: treated as silent
MIN_SPEECH_S = 2.0                       # less speech than this in the whole clip: treated as silent
NOISY_FLOOR_DBFS = -40.0                 # warning: the background is loud
QUIET_PEAK_DBFS = -26.0                  # warning: the recording is quiet
MIN_FIRST6_SPEECH_S = 3.5                # warning: little speech in the first 6 s (the part the model leans on most)
SPEECH_ABOVE_FLOOR_DB = 9.0              # a frame is speech when it is this much above the noise floor (and above SPEECH_MIN_DBFS)
SPEECH_MIN_DBFS = -60.0
FFMPEG_TIMEOUT_S = 20


class ClipRejected(Exception):
    def __init__(self, code: str, message: str, report: dict | None = None):
        super().__init__(code)
        self.code, self.message, self.report = code, message, report or {}


@dataclass
class PreparedClip:
    wav: bytes          # mono, 24 kHz, 16-bit WAV, silence trimmed
    report: dict        # numbers and warnings only, never audio


def decoder_report(which=shutil.which) -> dict:
    """What this server can decode, for the dashboard and the design document. Reports presence only (no path)."""
    ff = bool(which("ffmpeg"))
    return {"wav_pcm": True, "ffmpeg": ff, "accepted": "any format ffmpeg can read, and PCM WAV" if ff else "PCM WAV only (ffmpeg is not installed on this server)",
            "resampler": "ffmpeg" if ff else "audioop.ratecv (linear interpolation)"}


def _db(rms: float) -> float:
    return 20.0 * math.log10(max(rms, 1.0) / 32768.0)


def _wav_bytes(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _decode_wav(data: bytes) -> tuple[bytes, int]:
    """PCM WAV -> (mono 16-bit PCM, sample rate). Raises ClipRejected for anything else."""
    is_riff = data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    if not is_riff:
        raise ClipRejected("clip_unsupported_format", "This server can read WAV files only (it has no audio converter). Please upload a PCM WAV recording.")
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            if rate <= 0 or n <= 0:
                raise ClipRejected("clip_unreadable", "The file holds no audio.")
            if n / rate > MAX_INPUT_S:
                raise ClipRejected("clip_too_long", f"The recording is longer than {MAX_INPUT_S:g} seconds; please upload about 10 seconds.", {"original_duration_s": round(n / rate, 1)})
            if channels not in (1, 2):
                raise ClipRejected("clip_unsupported_format", "Only mono or stereo recordings are supported.")
            frames = w.readframes(n)
    except ClipRejected:
        raise
    except wave.Error as e:
        if "unknown format" in str(e) or "unsupported" in str(e).lower():
            raise ClipRejected("clip_unsupported_format", "This WAV uses a compressed or floating-point format. Please save it as 16-bit PCM WAV.") from None
        raise ClipRejected("clip_unreadable", "The WAV file could not be read.") from None
    except (EOFError, ValueError, OverflowError):
        raise ClipRejected("clip_unreadable", "The WAV file could not be read.") from None
    if width == 1:                                   # WAV stores 8-bit audio unsigned; audioop wants signed
        frames = audioop.bias(frames, 1, -128)
    if channels == 2:
        frames = audioop.tomono(frames, width, 0.5, 0.5)
    if width != 2:
        frames = audioop.lin2lin(frames, width, 2)
    return frames, rate


def _ffmpeg_to_wav(data: bytes, ffmpeg: str, runner) -> bytes:
    """Any format -> mono 24 kHz 16-bit WAV through ffmpeg. The bytes go in and out through pipes (no file names, no network protocols)."""
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "pipe", "-i", "pipe:0", "-t", str(int(MAX_INPUT_S)),
           "-vn", "-ac", "1", "-ar", str(TARGET_RATE), "-acodec", "pcm_s16le", "-f", "wav", "pipe:1"]
    try:
        r = runner(cmd, input=data, capture_output=True, timeout=FFMPEG_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        raise ClipRejected("clip_unreadable", "The audio converter could not process this file.") from None
    if r.returncode != 0 or not r.stdout:
        raise ClipRejected("clip_unreadable", "The audio could not be read. Please upload a clear recording in a common format.")
    return r.stdout


def prepare_clip(data: bytes, *, which=shutil.which, runner=subprocess.run) -> PreparedClip:
    """Check and prepare an uploaded clip. CPU-bound (pure Python loops over the samples): call it from a worker thread."""
    if not data:
        raise ClipRejected("clip_unreadable", "The file is empty.")
    try:
        pcm, rate = _decode_wav(data)
    except ClipRejected as e:
        ff = which("ffmpeg") if e.code == "clip_unsupported_format" else None   # not a plain PCM WAV: ffmpeg, when installed, may still read it
        if not ff:
            raise
        pcm, rate = _decode_wav(_ffmpeg_to_wav(data, ff, runner))
    original_s = len(pcm) / 2 / rate
    if rate != TARGET_RATE:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, TARGET_RATE, None)

    # --- frames, noise floor, speech flags
    step = int(TARGET_RATE * FRAME_S) * 2
    levels = [_db(audioop.rms(pcm[i:i + step], 2)) for i in range(0, len(pcm) - step + 1, step)]
    if not levels:
        raise ClipRejected("clip_unreadable", "The recording is too short to analyse.", {"original_duration_s": round(original_s, 2)})
    floor = sorted(levels)[len(levels) // 10]      # the 10th percentile: the quiet background between words
    thr = max(floor + SPEECH_ABOVE_FLOOR_DB, SPEECH_MIN_DBFS)
    speech = [lv > thr for lv in levels]
    peak_db = _db(audioop.max(pcm, 2))
    speech_total_s = sum(speech) * FRAME_S
    report = {"original_duration_s": round(original_s, 2), "peak_dbfs": round(peak_db, 1), "noise_floor_dbfs": round(floor, 1),
              "speech_s_total": round(speech_total_s, 2), "sample_rate": TARGET_RATE, "channels": 1, "bits": 16, "warnings": []}
    if not any(speech) or peak_db < MIN_PEAK_DBFS or speech_total_s < MIN_SPEECH_S:
        raise ClipRejected("clip_silent", "No clear speech was found in this recording (it is silent or far too quiet). Please record again closer to the microphone.", report)

    # --- trim the silence at both ends (keep a short pad)
    pad = int(PAD_S / FRAME_S)
    first, last = speech.index(True), len(speech) - 1 - speech[::-1].index(True)
    s0, s1 = max(0, first - pad), min(len(speech), last + 1 + pad)
    trimmed = pcm[s0 * step:s1 * step]
    flags = speech[s0:s1]
    duration = len(trimmed) / 2 / TARGET_RATE
    first6 = sum(flags[:int(6 / FRAME_S)]) * FRAME_S
    first10 = sum(flags[:int(10 / FRAME_S)]) * FRAME_S
    samples = array.array("h")
    samples.frombytes(trimmed)
    clipped = sum(1 for x in samples if x >= 32700 or x <= -32700) / max(1, len(samples))
    speech_db = [lv for lv, f in zip(levels[s0:s1], flags) if f]
    report.update(duration_s=round(duration, 2), peak_dbfs=round(_db(audioop.max(trimmed, 2)), 1), speech_s_first_6s=round(first6, 2), speech_s_first_10s=round(first10, 2),
                  clipped_fraction=round(clipped, 4), snr_db=round(sum(speech_db) / len(speech_db) - floor, 1) if speech_db else None,
                  trimmed_start_s=round(s0 * FRAME_S, 2), trimmed_end_s=round((len(levels) - s1) * FRAME_S, 2))
    if clipped > MAX_CLIPPED_FRACTION:
        raise ClipRejected("clip_clipped", "The recording is distorted (the sound is cut off at the loudest points). Please record again, a little further from the microphone.", report)
    if duration < MIN_S:
        raise ClipRejected("clip_too_short", f"After removing the silence only {duration:.1f} seconds of speech are left; at least {MIN_S:g} seconds are needed (about 10 is best).", report)
    if duration > MAX_S:
        raise ClipRejected("clip_too_long", f"The recording is {duration:.0f} seconds long after trimming; at most {MAX_S:g} seconds are allowed (about 10 is best).", report)

    warns = report["warnings"]
    if floor > NOISY_FLOOR_DBFS:
        warns.append({"code": "noisy_room", "message": "There is a lot of background noise; the cloned voice may sound rough. A quiet room gives a better result."})
    if report["peak_dbfs"] < QUIET_PEAK_DBFS:
        warns.append({"code": "quiet_recording", "message": "The recording is quiet; speaking closer to the microphone gives a cleaner clone."})
    if duration >= 6.0 and first6 < MIN_FIRST6_SPEECH_S:
        warns.append({"code": "little_speech_first_6s", "message": "There is little speech in the first 6 seconds, the part the voice model relies on most. Start speaking right away."})
    return PreparedClip(_wav_bytes(trimmed, TARGET_RATE), report)
