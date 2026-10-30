# -*- coding: utf-8 -*-
"""
tools/eot_replay.py - end-of-turn replay on recordings. FREE and OFFLINE: it reads WAV files and nothing else (no network, no model, no key).

    python tools/eot_replay.py DIR [--thresholds 700,550,450,350] [--vad-rms 450] [--no-hysteresis] [--out report.json]

Every recording is run in TWO versions, reported separately: "clean8k" (converted to 8 kHz mono) and "phone" (8 kHz, 300-3400 Hz band-pass, mu-law: tools/telephone_chain.py).
Every file in DIR is ONE intended utterance (docs/RECORDING_PROTOCOL.md: one question or one answer per file, natural hesitations included, a silent tail after the true end).
The end-of-turn logic of the app (app/ai/ws.py: 20 ms frames, a frame is speech above VAD_RMS, speech is confirmed after MIN_SPEECH_MS, the utterance ends after `silence_ms`
of silence, optionally the Phase 2 hysteresis of app/ai/duplex.py) is replayed at each silence threshold, with 2 s of silence appended. Since a file holds a single utterance,
the right answer is exactly ONE end-of-turn decision caused by silence, after the last speech. More than one means the speaker was CUT OFF at a pause: a false cut-off.
A decision forced by the 6 s utterance cap (MAX_UTTERANCE_MS) does not depend on the silence threshold and is counted separately, never as a false cut-off.

HOW MUCH A RESULT PROVES. Zero cut-offs in n utterances only bounds the true cut-off rate: with 95% confidence it is below 1 - 0.05^(1/n)
(39% at n=6, 9.5% at n=30, 2% at n=150). The 2% go/no-go threshold therefore needs about 150 clean utterances. With fewer, this tool reports a RANGE (the exact
binomial interval) and says so; it prints PASS or NO-GO only when the range is on one side of the threshold.

Output (numbers and file ids only, never audio or words): per version, per group and overall, per threshold: the files with a false cut-off, the counts and the range.
"""
import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from app.ai.duplex import DuplexConfig, silence_resets  # noqa: E402
from app.ai.ws import MAX_UTTERANCE_MS, MIN_SPEECH_MS, VAD_RMS, rms  # noqa: E402
from telephone_chain import load_variants, to_8k_mono  # noqa: E402

RATE, FRAME_MS = 8000, 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2
GO_RATE, NO_GO_RATE, CONF = 0.02, 0.05, 0.95
N_FOR_GO = 150


# ------------------------------------------------------------------------------------------------------ statistics
def binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p)."""
    if p <= 0:
        return 1.0
    if p >= 1:
        return 1.0 if k >= n else 0.0
    return min(1.0, sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(0, k + 1)))


def upper_bound(k: int, n: int, conf: float = CONF) -> float:
    """One-sided exact (Clopper-Pearson) upper bound of the true rate after k events in n trials. For k = 0 it is 1 - (1-conf)^(1/n)."""
    if n <= 0:
        return 1.0
    if k >= n:
        return 1.0
    lo, hi = k / n, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if binom_cdf(k, n, mid) > 1 - conf:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def lower_bound(k: int, n: int, conf: float = CONF) -> float:
    """One-sided exact lower bound of the true rate after k events in n trials."""
    if n <= 0 or k <= 0:
        return 0.0
    lo, hi = 0.0, k / n
    for _ in range(80):
        mid = (lo + hi) / 2
        if 1 - binom_cdf(k - 1, n, mid) < 1 - conf:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def judge(k: int, n: int) -> dict:
    """The honest reading of k cut-off files in n files against the plan's thresholds (go: at most 2%, no-go: above 5%)."""
    up, low = upper_bound(k, n), lower_bound(k, n)
    if n and up <= GO_RATE:
        v = "PASS"
    elif n and low >= NO_GO_RATE:
        v = "NO-GO"
    else:
        v = "RANGE ONLY"
    note = ""
    if v == "RANGE ONLY":
        note = (f"n={n} is too small to decide: the true rate is between {low:.1%} and {up:.1%} (95%). "
                f"Showing 2% or less needs about {N_FOR_GO} clean utterances with zero cut-offs.")
    return {"n": n, "k": k, "observed": round(k / n, 4) if n else None, "lower_95": round(low, 4), "upper_95": round(up, 4), "verdict": v, "note": note}


# ------------------------------------------------------------------------------------------------------ audio
def load_wav(path: Path) -> bytes:
    """16-bit mono PCM at 8 kHz (what the phone path carries): other rates and stereo are converted."""
    return to_8k_mono(path)


def frames_of(pcm: bytes, tail_s: float = 2.0):
    pcm = pcm + b"\x00" * (RATE * 2 * int(tail_s * 1000) // 1000)
    for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
        yield pcm[i:i + FRAME_BYTES]


def simulate(pcm: bytes, silence_ms: int, vad_rms: float = VAD_RMS, hysteresis: bool = True, cfg: DuplexConfig | None = None) -> list:
    """End-of-turn decisions as the app would make them: a list of {at_ms, utterance_ms, reason} with reason "silence" or "max_utterance"."""
    cfg = cfg or DuplexConfig()
    in_speech, speech_ms, silence, voiced_run, buf_ms, t, out = False, 0, 0, 0, 0, 0, []
    for frame in frames_of(pcm):
        t += FRAME_MS
        loud = rms(frame) >= vad_rms
        voiced_run = voiced_run + FRAME_MS if loud else 0
        if loud:
            speech_ms += FRAME_MS
            silence = 0 if (not hysteresis or silence_resets(in_speech, voiced_run, cfg)) else silence + FRAME_MS
            if speech_ms >= MIN_SPEECH_MS or in_speech:
                in_speech = True
        else:
            silence += FRAME_MS
            if not in_speech:
                speech_ms = 0
        if in_speech:
            buf_ms += FRAME_MS
            if silence >= silence_ms or buf_ms >= MAX_UTTERANCE_MS:
                out.append({"at_ms": t, "utterance_ms": buf_ms, "reason": "silence" if silence >= silence_ms else "max_utterance"})
                in_speech, speech_ms, silence, voiced_run, buf_ms = False, 0, 0, 0, 0
    return out


def longest_internal_pause_ms(pcm: bytes, vad_rms: float = VAD_RMS) -> int:
    """Longest run of quiet frames between the first and the last loud frame."""
    loud = [rms(f) >= vad_rms for f in frames_of(pcm, 0.0)]
    if not any(loud):
        return 0
    first, last = loud.index(True), len(loud) - 1 - loud[::-1].index(True)
    best = run = 0
    for v in loud[first:last + 1]:
        run = 0 if v else run + 1
        best = max(best, run)
    return best * FRAME_MS


def speech_span_ms(pcm: bytes, vad_rms: float = VAD_RMS) -> int:
    """Milliseconds from the first to the last speech frame (0 when there is no speech)."""
    loud = [rms(f) >= vad_rms for f in frames_of(pcm, 0.0)]
    if not any(loud):
        return 0
    return (len(loud) - loud[::-1].index(True) - loud.index(True)) * FRAME_MS


def group_of(fid: str) -> str:
    """scripted / pause / spontaneous, from the file name of docs/RECORDING_PROTOCOL.md (u01..u15 lines, p01..p20 pause lines, s01..s10 open answers)."""
    f = fid.lower()
    if f.startswith("s") and f[1:3].isdigit():
        return "spontaneous"
    if f.startswith("p") and f[1:3].isdigit():
        return "pause"
    if f.startswith("u") and f[1:3].isdigit():
        return "pause" if int(f[1:3]) >= 13 else "scripted"
    return "other"


def analyze(files: dict, thresholds, vad_rms: float = VAD_RMS, hysteresis: bool = True) -> dict:
    """files: {id: pcm}. Returns the report (numbers and ids only)."""
    res = {"vad_rms": vad_rms, "hysteresis": hysteresis, "n_files": len(files), "thresholds": {}, "internal_pause_ms": {}}
    for fid, pcm in files.items():
        res["internal_pause_ms"][fid] = longest_internal_pause_ms(pcm, vad_rms)
    over = sorted(fid for fid, pcm in files.items() if speech_span_ms(pcm, vad_rms) > MAX_UTTERANCE_MS)
    res["over_6s_cap"] = {"n": len(over), "of": len(files), "files": over,
                          "note": "the app cuts an utterance at 6 s whatever the silence wait; these recordings are reported separately and their forced cut is never counted as a false cut-off"}
    groups = sorted({group_of(f) for f in files})
    for T in thresholds:
        per = {fid: simulate(pcm, T, vad_rms, hysteresis) for fid, pcm in files.items()}
        sil = {fid: sum(1 for d in ds if d["reason"] == "silence") for fid, ds in per.items()}
        cut = {fid: n - 1 for fid, n in sil.items() if n > 1}
        capped = sorted(fid for fid, ds in per.items() if any(d["reason"] == "max_utterance" for d in ds))
        none = sorted(fid for fid, ds in per.items() if not ds)
        by_group = {g: judge(sum(1 for f in cut if group_of(f) == g), sum(1 for f in files if group_of(f) == g)) for g in groups}
        res["thresholds"][str(T)] = {"false_cutoff_files": sorted(cut), "n_files_with_false_cutoff": len(cut), "extra_decisions": sum(cut.values()),
                                     "share_files_cut": round(len(cut) / max(1, len(files)), 3), "files_without_any_decision": none,
                                     "files_with_a_6s_cap_decision": capped, "dead_air_saved_vs_700_ms": 700 - T,
                                     "overall": judge(len(cut), len(files)), "by_group": by_group}
        res["thresholds"][str(T)]["without_the_files_over_the_6s_cap"] = judge(sum(1 for f in cut if f not in over), len(files) - len(over))
    res["reading"] = reading(res)
    return res


def reading(res: dict) -> dict:
    """The plain-language reading of a whole report: what a PASS needs and what the silence wait does for now."""
    n = res["n_files"]
    verdicts = {T: r["overall"]["verdict"] for T, r in res["thresholds"].items()}
    passes = [T for T, v in verdicts.items() if v == "PASS"]
    return {"n": n, "pass_possible": n >= N_FOR_GO - 1, "verdicts": verdicts,
            "pass_rule": f"A PASS is impossible below about {N_FOR_GO} clean utterances (exactly {N_FOR_GO - 1} with zero cut-offs); this run has n={n}.",
            "silence_wait": ("A threshold reached PASS: " + ", ".join(f"{T} ms" for T in passes) + ". Even so the choice is made by the owner after the supervised call.") if passes else
                            "No threshold reached PASS, so the silence wait STAYS AT 700 ms for now. A RANGE ONLY result is not evidence for a shorter wait."}


def run_dir(d: Path, thresholds, vad_rms: float = VAD_RMS, hysteresis: bool = True) -> dict:
    """Every recording in `d` in both versions: "clean8k" (8 kHz mono) and "phone" (8 kHz, 300-3400 Hz, mu-law). Returns {variant: report}, reported separately."""
    by_variant = {"clean8k": {}, "phone": {}}
    for p in sorted(d.glob("*.wav")):
        if p.stem.lower().startswith("roomtone"):
            continue
        for name, pcm in load_variants(p).items():
            by_variant[name][p.stem] = pcm
    return {name: analyze(files, thresholds, vad_rms, hysteresis) for name, files in by_variant.items() if files}


def report(res: dict, name: str = "") -> None:
    print(f"\n=== end-of-turn replay{' [' + name + ']' if name else ''}: {res['n_files']} files, VAD rms {res['vad_rms']}, hysteresis {res['hysteresis']} ===")
    print("silence ms | files cut off | extra decisions | share cut | true-rate range (95%) | reading | dead air saved vs 700 ms")
    for T, r in res["thresholds"].items():
        o = r["overall"]
        print(f"{T:>10} | {r['n_files_with_false_cutoff']:>13} | {r['extra_decisions']:>15} | {r['share_files_cut']:>9} | {o['lower_95']:.1%} - {o['upper_95']:.1%}".ljust(78)
              + f" | {o['verdict']:<10} | {r['dead_air_saved_vs_700_ms']:>6}")
        for g, j in r["by_group"].items():
            print(f"           {g:<12} n={j['n']:<3} cut={j['k']:<3} range {j['lower_95']:.1%} - {j['upper_95']:.1%}  {j['verdict']}")
        if r["files_without_any_decision"]:
            print(f"           no decision at all (no speech found?): {r['files_without_any_decision']}")
        if r["files_with_a_6s_cap_decision"]:
            print(f"           decisions forced by the 6 s utterance cap (not counted as cut-offs): {r['files_with_a_6s_cap_decision']}")
    worst = sorted(res["internal_pause_ms"].items(), key=lambda kv: -kv[1])[:8]
    print("longest pause inside speech (ms), top files:", ", ".join(f"{k}={v}" for k, v in worst))
    cap = res["over_6s_cap"]
    print(f"RECORDINGS OVER THE 6 s CAP (reported separately): {cap['n']} of {cap['of']}" + (f": {', '.join(cap['files'])}" if cap["files"] else "") +
          ". The app cuts an utterance at 6 s whatever the silence wait; that forced cut is never counted as a false cut-off.")
    n = res["n_files"]
    print(f"HOW MUCH THIS PROVES: zero cut-offs in n utterances only bounds the true rate below 1-0.05^(1/n): 39% at n=6, 9.5% at n=30, 2% at n=150. Here n={n}: "
          f"with zero cut-offs the bound is {upper_bound(0, n):.1%}.")
    rd = res["reading"]
    print("PLAINLY: " + rd["pass_rule"])
    print("PLAINLY: " + rd["silence_wait"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir"); ap.add_argument("--thresholds", default="700,550,450,350"); ap.add_argument("--vad-rms", type=float, default=VAD_RMS)
    ap.add_argument("--no-hysteresis", action="store_true"); ap.add_argument("--out", default="")
    a = ap.parse_args()
    d = Path(a.dir)
    res = run_dir(d, [int(x) for x in a.thresholds.split(",")], a.vad_rms, not a.no_hysteresis)
    if not res:
        raise SystemExit(f"no .wav files in {d}")
    for name, r in res.items():
        report(r, name)
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
