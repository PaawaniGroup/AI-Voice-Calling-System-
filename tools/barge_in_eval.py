# -*- coding: utf-8 -*-
"""
tools/barge_in_eval.py - false barge-ins per minute, and missed / late true interruptions, of three detectors on the SAME caller audio:
  legacy     the rule that runs today: 200 ms of CONTINUOUS audio above max(VAD x 2.2, 1000)   (mirrors app/ai/ws.py; the constants are imported from it)
  duplex     full-duplex input with the echo guard (the provisional defaults of app/ai/duplex.py)
  duplex-0   full-duplex input with the echo guard OFF (echo_ratio 0), to show what the guard buys and what it costs

    python tools/barge_in_eval.py --speech path/to/speech.wav [--minutes 3] [--out docs/evidence/barge_in_eval_synthetic.json]

IMPORTANT: this is NOT a measurement on real calls. Without call recordings the caller audio is SYNTHETIC: excerpts of one clean speech recording placed on a timeline
(backchannel-length bursts, longer interruptions), line noise, and a speaker-echo model (the assistant's own audio, attenuated and delayed). The levels are
ASSUMPTIONS, printed with the results. It tells you how the detectors compare on this material; the real false-barge-in rate needs real recordings:
  python tools/barge_in_eval.py --speech speech.wav --calls recorded_call_1.wav recorded_call_2.wav ...    (each file: the CALLER side of a call, with the assistant playing)
For --calls the assistant's playing level is taken as 3000 RMS wherever the file is quiet-to-loud alike, so give recordings where the assistant was playing throughout.
Output: counts, rates and times only. No audio is stored.
"""
import argparse, audioop, json, random, sys, wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.ai import ws as ws_mod  # noqa: E402  (the constants of the rule that runs today)
from app.ai.duplex import BargeInDetector, DuplexConfig, OutboundLevel  # noqa: E402

RATE = 8000
FRAME_MS = 20
FRAME = RATE * FRAME_MS // 1000 * 2          # bytes of one frame
VAD = ws_mod.VAD_RMS
REFRACTORY_S = 2.0                           # after an interruption the (simulated) assistant starts a new reply a moment later


# ------------------------------------------------------------------------------------------------ audio helpers
def load_wav8k(path: str) -> bytes:
    w = wave.open(path)
    raw, sw, ch, rate = w.readframes(w.getnframes()), w.getsampwidth(), w.getnchannels(), w.getframerate()
    if ch == 2:
        raw = audioop.tomono(raw, sw, 0.5, 0.5)
    if sw != 2:
        raw = audioop.lin2lin(raw, sw, 2)
    return audioop.ratecv(raw, 2, 1, rate, RATE, None)[0] if rate != RATE else raw


def scaled(pcm: bytes, target_rms: float) -> bytes:
    r = audioop.rms(pcm, 2) or 1
    return audioop.mul(pcm, 2, min(target_rms / r, 20.0))


def noise(rng, n_samples: int, target_rms: float) -> bytes:
    import struct
    raw = struct.pack("<%dh" % n_samples, *[int(rng.gauss(0, 1000)) for _ in range(n_samples)])
    return scaled(raw, target_rms) if target_rms > 0 else b"\x00\x00" * n_samples


def mix(a: bytes, b: bytes) -> bytes:
    n = min(len(a), len(b))
    return audioop.add(a[:n], b[:n], 2)


def voiced_excerpt(rng, speech: bytes, ms: int) -> bytes:
    """An excerpt of `ms` milliseconds from a voiced place of the speech recording (its own RMS above half the file's)."""
    n = RATE * ms // 1000 * 2
    floor = audioop.rms(speech, 2) * 0.5
    for _ in range(200):
        s = rng.randrange(0, max(2, len(speech) - n - 2)) // 2 * 2
        if audioop.rms(speech[s:s + n], 2) >= floor:
            return speech[s:s + n]
    return speech[:n]


# ------------------------------------------------------------------------------------------------ the detectors
class Legacy:
    """The rule of app/ai/ws.py today (while the assistant plays): consecutive loud frames; a quiet frame resets the count."""
    name = "legacy"

    def __init__(self):
        self.ms = 0

    def feed(self, frame, rms, level):
        if rms >= max(VAD * ws_mod.BARGE_IN_RMS_MULTIPLIER, ws_mod.BARGE_IN_VAD_RMS):
            self.ms += FRAME_MS
            if self.ms >= ws_mod.BARGE_IN_MS:
                return True, FRAME_MS                         # fires; keeps only this frame as the start of the next utterance
        else:
            self.ms = 0
        return False, 0

    def reset(self):
        self.ms = 0


class Duplex:
    def __init__(self, name, **cfg):
        self.name, self.det = name, BargeInDetector(DuplexConfig(**cfg), VAD)
        self.stat = self.det.cfg.echo_stat

    def feed(self, frame, rms, level):
        if self.det.feed(frame, rms, FRAME_MS, level):
            kept = len(self.det._run) // FRAME * FRAME_MS      # the whole run is kept for speech-to-text
            self.det.reset()
            return True, kept
        return False, 0

    def reset(self):
        self.det.reset()


def detectors():
    return [Legacy(), Duplex("duplex"), Duplex("duplex-0", echo_ratio=0.0)]


# ------------------------------------------------------------------------------------------------ a scenario = inbound frames + the assistant's level
def run(frames_in, out_levels, events, det):
    """frames_in: list of 20 ms frames; out_levels: assistant RMS per frame (the playback timeline); events: [(start_frame, end_frame)] of REAL interruptions.
    Returns false triggers (outside any event), detected events with latency, and kept audio."""
    lvl = OutboundLevel(keep_s=5.0)
    false, hits, kept_ms, lat = 0, 0, [], []
    quiet_until = -1
    det.reset()
    hit_events = set()
    for i, f in enumerate(frames_in):
        t = i * FRAME_MS / 1000
        lvl.add_next(t, FRAME_MS / 1000, out_levels[i])
        if i < quiet_until:
            continue
        level = lvl.recent(t, 500, getattr(det, "stat", "max"))
        fired, kept = det.feed(f, audioop.rms(f, 2), level)
        if not fired:
            continue
        ev = next((k for k, (s, e) in enumerate(events) if s <= i <= e), None)
        if ev is None:
            false += 1
        elif ev not in hit_events:
            hit_events.add(ev)
            lat.append((i - events[ev][0]) * FRAME_MS)
            kept_ms.append(kept)
        quiet_until = i + int(REFRACTORY_S / (FRAME_MS / 1000))
        det.reset()
    minutes = len(frames_in) * FRAME_MS / 60000
    return {"false_triggers": false, "false_per_min": round(false / minutes, 2), "events": len(events), "detected": len(hit_events),
            "detection_rate": round(len(hit_events) / len(events), 2) if events else None,
            "latency_ms_p50": sorted(lat)[len(lat) // 2] if lat else None, "kept_ms_mean": round(sum(kept_ms) / len(kept_ms), 0) if kept_ms else None}


def timeline(rng, speech, minutes):
    """The assistant is playing all the time: its audio is the speech recording, looped. Returns (pcm bytes of the assistant, per-frame RMS)."""
    n = int(minutes * 60 * RATE) * 2
    out = (scaled(speech, 3000) * (n // len(speech) + 1))[:n]
    frames = [out[i:i + FRAME] for i in range(0, n - FRAME + 1, FRAME)]
    return out, [audioop.rms(f, 2) for f in frames]


def to_frames(pcm):
    return [pcm[i:i + FRAME] for i in range(0, len(pcm) - FRAME + 1, FRAME)]


def place(rng, base: bytearray, clips, every_s, jitter_s=0.4, start_s=2.0):
    """Mix `clips` (a function ms -> pcm) into base once every `every_s` seconds; returns the event list as (start_frame, end_frame)."""
    events, t = [], start_s
    while True:
        ms, clip = clips()
        s = int(t * RATE) * 2 // 2 * 2
        if s + len(clip) >= len(base):
            break
        base[s:s + len(clip)] = mix(bytes(base[s:s + len(clip)]), clip)
        events.append((s // FRAME, (s + len(clip)) // FRAME))
        t += every_s + rng.uniform(-jitter_s, jitter_s)
    return events


def scenarios(speech, minutes, seed=7):
    rng = random.Random(seed)
    asst, levels = timeline(rng, speech, minutes)
    n = len(asst)
    silent = lambda: bytearray(noise(rng, n // 2, 250))                                   # a quiet line: 250 RMS of noise
    out = {}
    for rms in (200, 500, 900):                                                           # S1: line noise only
        out[f"S1 line noise rms {rms}"] = (to_frames(noise(rng, n // 2, rms)), levels, [])
    base = silent()
    ev = place(rng, base, lambda: (0, (lambda ms: (ms, scaled(voiced_excerpt(rng, speech, ms), 2500)))(rng.choice([80, 120, 160, 200, 230]))[1]), 4.0)
    out["S2 backchannel bursts 80-230 ms (caller level 2500), none a real interruption"] = (to_frames(bytes(base)), levels, [])
    delay = RATE * 150 // 1000 * 2
    for db in (-6, -10, -14, -20):                                                        # S3: speaker echo of the assistant
        echo = audioop.mul(b"\x00" * delay + asst[:n - delay], 2, 10 ** (db / 20))
        out[f"S3 speaker echo {db} dB + noise 250"] = (to_frames(mix(echo, bytes(silent()))), levels, [])
    base = silent()
    ev = place(rng, base, lambda: (0, (lambda ms: (ms, scaled(voiced_excerpt(rng, speech, ms), 2500)))(rng.choice([700, 900, 1200, 1500]))[1]), 8.0)
    out["S4 caller talks over the assistant (700-1500 ms, level 2500), quiet line"] = (to_frames(bytes(base)), levels, ev)
    for db in (-10, -6):                                                                  # S5: the same, WITH speaker echo: the guard's cost
        echo = audioop.mul(b"\x00" * delay + asst[:n - delay], 2, 10 ** (db / 20))
        base = bytearray(mix(echo, bytes(silent())))
        ev = place(rng, base, lambda: (0, (lambda ms: (ms, scaled(voiced_excerpt(rng, speech, ms), 2500)))(rng.choice([700, 900, 1200, 1500]))[1]), 8.0)
        out[f"S5 caller talks over the assistant, WITH echo {db} dB"] = (to_frames(bytes(base)), levels, ev)
    return out


def evaluate(speech, minutes, seed=7):
    res = {}
    for name, (frames_in, levels, events) in scenarios(speech, minutes, seed).items():
        res[name] = {d.name: run(frames_in, levels, events, d) for d in detectors()}
    return res


def print_report(res, minutes):
    print(f"\n=== barge-in detectors on SYNTHETIC caller audio ({minutes} min per scenario; assistant playing throughout at RMS ~3000) ===")
    print("false = interruptions that were NOT real (per minute); for S4/S5 also: real interruptions detected, median latency, speech kept for STT\n")
    print(f"{'scenario':<88} {'legacy':>20} {'duplex':>20} {'duplex-0':>20}")
    for name, r in res.items():
        cells = []
        for d in ("legacy", "duplex", "duplex-0"):
            x = r[d]
            c = f"{x['false_per_min']}/min"
            if x["events"]:
                c += f" | {x['detected']}/{x['events']} @{x['latency_ms_p50']}ms keep {x['kept_ms_mean']:.0f}ms" if x["detected"] else f" | 0/{x['events']}"
            cells.append(c)
        print(f"{name[:87]:<88} {cells[0]:>20} {cells[1]:>20} {cells[2]:>20}")


def sweep(speech, minutes, seed=7):
    """The echo guard's two knobs on the scenarios that decide them: how often echo is mistaken for the caller (S3, per minute) against how often the
    real caller is still heard (S4 quiet line, S5 with echo) and how late."""
    sc = scenarios(speech, minutes, seed)
    pick = {k: v for k, v in sc.items() if k.startswith(("S3 speaker echo -6", "S3 speaker echo -10", "S3 speaker echo -14", "S4", "S5 caller talks over the assistant, WITH echo -10"))}
    rows = []
    for stat in ("max", "mean"):
        for ratio in (0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0):
            d = Duplex(f"{stat} {ratio}", echo_ratio=ratio, echo_stat=stat)
            r = {k: run(f, l, e, d) for k, (f, l, e) in pick.items()}
            g = lambda prefix, key: next(v[key] for k, v in r.items() if k.startswith(prefix))   # noqa: E731
            rows.append({"stat": stat, "ratio": ratio, "false_echo_-6dB": g("S3 speaker echo -6", "false_per_min"), "false_echo_-10dB": g("S3 speaker echo -10", "false_per_min"),
                         "false_echo_-14dB": g("S3 speaker echo -14", "false_per_min"), "heard_quiet_line": g("S4", "detection_rate"), "latency_quiet_ms": g("S4", "latency_ms_p50"),
                         "heard_with_echo_-10dB": g("S5", "detection_rate"), "false_in_S5": g("S5", "false_per_min")})
    print(f"\n=== echo guard sweep (synthetic, {minutes} min per scenario) ===")
    print(f"{'stat':<5} {'ratio':>5} | {'echo -6dB':>9} {'echo -10dB':>10} {'echo -14dB':>10} (false/min) | {'heard, quiet':>12} {'latency':>8} | {'heard, echo -10':>15} {'false':>6}")
    for x in rows:
        print(f"{x['stat']:<5} {x['ratio']:>5} | {x['false_echo_-6dB']:>9} {x['false_echo_-10dB']:>10} {x['false_echo_-14dB']:>10}             | {x['heard_quiet_line']:>12} {x['latency_quiet_ms']!s:>6}ms | {x['heard_with_echo_-10dB']:>15} {x['false_in_S5']:>6}")
    return rows


def evaluate_calls(speech, paths):
    """Recorded calls: the caller side only. No ground truth of real interruptions is available, so everything that fires is reported as a trigger count per minute
    (review the timestamps by listening); the assistant is assumed to play at ~3000 RMS throughout."""
    res = {}
    for p in paths:
        pcm = load_wav8k(p)
        frames = to_frames(pcm)
        levels = [3000.0] * len(frames)
        res[Path(p).name] = {d.name: run(frames, levels, [], d) for d in detectors()}
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--speech", required=True, help="one clean speech recording (any rate, mono or stereo WAV): the material of the synthetic scenarios")
    ap.add_argument("--minutes", type=float, default=3.0)
    ap.add_argument("--calls", nargs="*", default=[], help="recorded CALLER-side call audio to run the detectors on")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--sweep", action="store_true", help="only the echo-guard sweep (ratio x level statistic)")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    speech = load_wav8k(a.speech)
    if a.sweep:
        rows = sweep(speech, a.minutes, a.seed)
        if a.out:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out).write_text(json.dumps({"sweep": rows, "minutes": a.minutes, "seed": a.seed, "note": "synthetic caller audio; levels are assumptions"}, indent=1), encoding="utf-8")
        return
    res = evaluate(speech, a.minutes, a.seed)
    print_report(res, a.minutes)
    out = {"synthetic": res, "minutes": a.minutes, "seed": a.seed, "note": "synthetic caller audio built from one speech recording; levels are assumptions"}
    if a.calls:
        out["recorded"] = evaluate_calls(speech, a.calls)
        print("\n=== recorded calls: triggers per minute (no ground truth) ===")
        for f, r in out["recorded"].items():
            print(f"{f}: " + " | ".join(f"{d} {r[d]['false_per_min']}/min" for d in ("legacy", "duplex", "duplex-0")))
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(out, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
