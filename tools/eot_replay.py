# -*- coding: utf-8 -*-
"""
tools/eot_replay.py - end-of-turn replay on recordings. FREE and OFFLINE: it reads WAV files and nothing else (no network, no model, no key).

    python tools/eot_replay.py DIR [--thresholds 700,550,450,350] [--vad-rms 450] [--no-hysteresis] [--out report.json]

Every recording is run in TWO versions, reported separately: "clean8k" (converted to 8 kHz mono) and "phone" (8 kHz, 300-3400 Hz band-pass, mu-law: tools/telephone_chain.py).
Every file in DIR is ONE intended utterance (the recording protocol: one question per file, natural hesitations included). The end-of-turn logic of the app
(app/ai/ws.py: 20 ms frames, a frame is speech above VAD_RMS, speech is confirmed after MIN_SPEECH_MS, the utterance ends after `silence_ms` of silence,
optionally the Phase 2 hysteresis of app/ai/duplex.py) is replayed at each silence threshold, with 2 s of silence appended. Since a file holds a single
utterance, the right answer is exactly ONE end-of-turn decision, after the last speech. More than one means the speaker was CUT OFF at a pause: a false cut-off.

Output (numbers and file ids only, never audio or words): per threshold the files with a false cut-off and the number of extra decisions, and per file the
longest pause inside the speech (it explains which threshold splits which file).
"""
import argparse
import json
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


def load_wav(path: Path) -> bytes:
    """16-bit mono PCM at 8 kHz (what the phone path carries): other rates and stereo are converted."""
    return to_8k_mono(path)


def run_dir(d: Path, thresholds, vad_rms: float = VAD_RMS, hysteresis: bool = True) -> dict:
    """Every recording in `d` in both versions: "clean8k" (8 kHz mono) and "phone" (8 kHz, 300-3400 Hz, mu-law). Returns {variant: report}, reported separately."""
    by_variant = {"clean8k": {}, "phone": {}}
    for p in sorted(d.glob("*.wav")):
        if p.stem.lower().startswith("roomtone"):
            continue
        for name, pcm in load_variants(p).items():
            by_variant[name][p.stem] = pcm
    return {name: analyze(files, thresholds, vad_rms, hysteresis) for name, files in by_variant.items() if files}


def frames_of(pcm: bytes, tail_s: float = 2.0):
    pcm = pcm + b"\x00" * (RATE * 2 * int(tail_s * 1000) // 1000)
    for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
        yield pcm[i:i + FRAME_BYTES]


def simulate(pcm: bytes, silence_ms: int, vad_rms: float = VAD_RMS, hysteresis: bool = True, cfg: DuplexConfig | None = None) -> list:
    """End-of-turn decisions as the app would make them: a list of dicts {at_ms, utterance_ms}."""
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
                out.append({"at_ms": t, "utterance_ms": buf_ms})
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


def analyze(files: dict, thresholds, vad_rms: float = VAD_RMS, hysteresis: bool = True) -> dict:
    """files: {id: pcm}. Returns the report (numbers and ids only)."""
    res = {"vad_rms": vad_rms, "hysteresis": hysteresis, "n_files": len(files), "thresholds": {}, "internal_pause_ms": {}}
    for fid, pcm in files.items():
        res["internal_pause_ms"][fid] = longest_internal_pause_ms(pcm, vad_rms)
    for T in thresholds:
        per = {fid: simulate(pcm, T, vad_rms, hysteresis) for fid, pcm in files.items()}
        cut = {fid: len(d) - 1 for fid, d in per.items() if len(d) > 1}
        none = [fid for fid, d in per.items() if not d]
        res["thresholds"][str(T)] = {"false_cutoff_files": sorted(cut), "n_files_with_false_cutoff": len(cut), "extra_decisions": sum(cut.values()),
                                     "share_files_cut": round(len(cut) / max(1, len(files)), 3), "files_without_any_decision": sorted(none),
                                     "dead_air_saved_vs_700_ms": 700 - T}
    return res


def report(res: dict, name: str = "") -> None:
    print(f"\n=== end-of-turn replay{' [' + name + ']' if name else ''}: {res['n_files']} files, VAD rms {res['vad_rms']}, hysteresis {res['hysteresis']} ===")
    print("silence ms | files cut off | extra decisions | share cut | dead air saved vs 700 ms")
    for T, r in res["thresholds"].items():
        print(f"{T:>10} | {r['n_files_with_false_cutoff']:>13} | {r['extra_decisions']:>15} | {r['share_files_cut']:>9} | {r['dead_air_saved_vs_700_ms']:>6}")
        if r["files_without_any_decision"]:
            print(f"           no decision at all (no speech found?): {r['files_without_any_decision']}")
    worst = sorted(res["internal_pause_ms"].items(), key=lambda kv: -kv[1])[:8]
    print("longest pause inside speech (ms), top files:", ", ".join(f"{k}={v}" for k, v in worst))
    print("go/no-go (plan): at most 2% of files cut off at the chosen threshold WITH speculation; no-go above 5%. Here speculation is not simulated: this is the plain cut-off rate.")


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
