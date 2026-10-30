"""Full-duplex input handling (per-tenant flag `full_duplex_input`, default OFF): the caller is listened to WHILE the assistant speaks.

Four parts, all pure (no I/O, no clock of their own: the caller passes the monotonic time):

  BargeInDetector  an interruption needs `min_voiced_ms` of VOICED speech (so a short "hmm" / "haan" / cough is ignored); a quiet gap shorter than
                   `dropout_ms` inside the speech does not break the run (syllable gaps); the speech that triggered it is KEPT (the old rule kept only the
                   triggering frame, so the first 200 ms or more of the interruption never reached speech-to-text).
  OutboundLevel    how loud the assistant has been in the last moments, on the playback timeline, so that
  echo guard       the caller's frame must also exceed `echo_ratio` x the recent outbound level: speaker echo (speakerphone) is loud on a phone line but
                   always quieter than what it echoes; a person talking over the assistant is not.
  PreRoll          the last `pre_roll_ms` of audio before speech is confirmed, put in front of the utterance (the old rule dropped the first MIN_SPEECH_MS).
  endpointing      `silence_resets()`: a loud blip shorter than `resume_ms` does not restart the end-of-speech silence timer (hysteresis).

Nothing here logs: it handles audio levels and counts, never words. The tuning values are PROVISIONAL until they are checked on real call recordings.
"""
from __future__ import annotations

import audioop
from collections import deque
from dataclasses import dataclass

# key -> (min, max); a float bound means a float value
DUPLEX_KEYS: dict[str, tuple] = {
    "duplex_min_voiced_ms": (100, 1000),
    "duplex_dropout_ms": (0, 500),
    "duplex_echo_ratio": (0.0, 2.0),
    "duplex_resume_ms": (0, 300),
    "duplex_pre_roll_ms": (0, 1000),
}
PROVISIONAL = {"duplex_min_voiced_ms": 250, "duplex_dropout_ms": 100, "duplex_echo_ratio": 0.8, "duplex_resume_ms": 60, "duplex_pre_roll_ms": 300}
# echo_ratio 0.8 with the AVERAGE assistant loudness was chosen on SYNTHETIC audio (docs/evidence/barge_in_echo_sweep_synthetic.json): 0 false interruptions per minute
# for speaker echo at -10 dB or quieter, 1.3 at -6 dB, 87% of real interruptions heard on a quiet line. 0.6 hears more of the caller but lets -6 dB echo through
# (12 false per minute). Tune on real call recordings (tools/barge_in_eval.py --calls ...).


@dataclass
class DuplexConfig:
    min_voiced_ms: int = 250        # voiced speech needed before the assistant is interrupted
    dropout_ms: int = 100           # a quiet gap up to this long does not end the voiced run
    echo_ratio: float = 0.8         # the caller's frame must exceed this x the assistant's recent loudness (0 = echo guard off)
    resume_ms: int = 60             # end-of-speech hysteresis: voiced audio shorter than this does not restart the silence timer
    pre_roll_ms: int = 300          # audio kept from before speech was confirmed
    rms_multiplier: float = 2.2     # barge-in threshold over the VAD threshold (as before)
    min_rms: float = 1000.0         # absolute floor of the barge-in threshold (as before)
    echo_window_ms: int = 500       # "recent": the echo of what is heard now left the assistant up to this long ago
    echo_stat: str = "mean"         # how "the assistant's loudness" is taken over that window: "mean" (average) or "max" (loudest piece: too strict, see PROVISIONAL)


def value(cfg: dict, key: str):
    v = (cfg or {}).get(key)
    return PROVISIONAL[key] if v is None else v


def duplex_config_from(cfg: dict) -> DuplexConfig:
    return DuplexConfig(min_voiced_ms=value(cfg, "duplex_min_voiced_ms"), dropout_ms=value(cfg, "duplex_dropout_ms"),
                        echo_ratio=float(value(cfg, "duplex_echo_ratio")), resume_ms=value(cfg, "duplex_resume_ms"),
                        pre_roll_ms=value(cfg, "duplex_pre_roll_ms"))


class OutboundLevel:
    """Loudness (RMS) of the audio handed to the caller, as spans on the PLAYBACK timeline: audio sent ahead of real time is not audible yet."""

    def __init__(self, keep_s: float = 3.0):
        self.keep_s = keep_s
        self._spans: deque[tuple[float, float, float]] = deque()   # (start, end, rms)
        self._end = 0.0

    def add_next(self, now: float, duration_s: float, rms: float) -> None:
        """A piece of audio queued now: it plays after what is already queued (the same rule as the 'assistant is playing' window)."""
        start = max(now, self._end)
        self._end = start + duration_s
        self._spans.append((start, self._end, float(rms)))
        while self._spans and self._spans[0][1] < now - self.keep_s:
            self._spans.popleft()

    def add_clip(self, now: float, pcm: bytes, rate: int, window_ms: int = 100) -> None:
        """A whole clip handed over at once (the non-streaming path): split in `window_ms` pieces so the loudness follows the speech."""
        step = max(2, rate * window_ms // 1000 * 2)
        for i in range(0, len(pcm) - 1, step):
            piece = pcm[i:i + step]
            self.add_next(now, len(piece) / 2 / rate, audioop.rms(piece, 2))

    def recent(self, now: float, window_ms: int, stat: str = "max") -> float:
        """The assistant's loudness over the last `window_ms`: the loudest piece ("max") or the duration-weighted average ("mean")."""
        lo = now - window_ms / 1000
        items = [(max(s, lo), min(e, now), r) for s, e, r in self._spans if e >= lo and s <= now]
        if not items:
            return 0.0
        if stat == "mean":
            total = sum(max(0.0, b - a) for a, b, _ in items)
            if total > 0:
                return sum(max(0.0, b - a) * r for a, b, r in items) / total
        return max(r for _, _, r in items)

    def recent_max(self, now: float, window_ms: int) -> float:
        return self.recent(now, window_ms, "max")

    def clear(self) -> None:
        self._spans.clear()
        self._end = 0.0


class BargeInDetector:
    def __init__(self, cfg: DuplexConfig, vad_rms: float):
        self.cfg, self.vad_rms = cfg, vad_rms
        self.voiced_ms = 0
        self.gap_ms = 0
        self._run = bytearray()          # the frames of the current run, quiet gap frames included (the speech is kept whole)
        self.last_threshold = 0.0
        self.echo_suppressed_frames = 0  # frames that passed the plain threshold but not the echo guard (a count for diagnostics)
        self.triggers = 0

    def threshold(self, outbound_level: float) -> float:
        c = self.cfg
        return max(self.vad_rms * c.rms_multiplier, c.min_rms, c.echo_ratio * outbound_level)

    def feed(self, frame: bytes, frame_rms: float, frame_ms: int, outbound_level: float = 0.0) -> bool:
        """One caller frame while the assistant is speaking. True = interrupt now (the run is then available through `take()`)."""
        c = self.cfg
        plain = max(self.vad_rms * c.rms_multiplier, c.min_rms)
        thr = self.last_threshold = self.threshold(outbound_level)
        if frame_rms >= thr:
            self.gap_ms = 0
            self.voiced_ms += frame_ms
            self._run += frame
        else:
            if frame_rms >= plain:
                self.echo_suppressed_frames += 1
            if self.voiced_ms:
                self.gap_ms += frame_ms
                if self.gap_ms > c.dropout_ms:
                    self.reset()
                else:
                    self._run += frame       # a short gap inside speech: bridged, and kept so the utterance has no hole
        if self.voiced_ms >= c.min_voiced_ms:
            self.triggers += 1
            return True
        return False

    def take(self) -> bytes:
        """The speech that triggered the interruption (everything since the run began); resets the detector."""
        run = bytes(self._run)
        self.reset()
        return run

    def reset(self) -> None:
        self.voiced_ms = 0
        self.gap_ms = 0
        self._run = bytearray()


class PreRoll:
    """The last `keep_ms` of caller audio before speech is confirmed."""

    def __init__(self, keep_ms: int):
        self.keep_ms = keep_ms
        self._frames: deque[tuple[bytes, int]] = deque()
        self._ms = 0

    def push(self, frame: bytes, frame_ms: int) -> None:
        if self.keep_ms <= 0:
            return
        self._frames.append((frame, frame_ms))
        self._ms += frame_ms
        while self._ms - self._frames[0][1] >= self.keep_ms and len(self._frames) > 1:
            self._ms -= self._frames.popleft()[1]

    def drain(self, exclude_last: bool = True) -> bytes:
        """The kept audio, oldest first. `exclude_last`: the newest frame is the one the caller handles itself."""
        frames = list(self._frames)[:-1] if (exclude_last and self._frames) else list(self._frames)
        self.clear()
        return b"".join(f for f, _ in frames)

    def clear(self) -> None:
        self._frames.clear()
        self._ms = 0


def silence_resets(in_speech: bool, voiced_run_ms: int, cfg: DuplexConfig) -> bool:
    """End-of-speech hysteresis: a loud frame restarts the silence timer only when the voiced run has lasted `resume_ms` (a click or a cough blip
    must not postpone the end of the utterance). Before speech is confirmed there is nothing to restart."""
    return (not in_speech) or voiced_run_ms >= cfg.resume_ms
