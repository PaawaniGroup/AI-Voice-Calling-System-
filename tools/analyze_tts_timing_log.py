# -*- coding: utf-8 -*-
"""
tools/analyze_tts_timing_log.py - fit the TTS worker's own timing lines and compare them with RunPod's numbers from Phase 0. FREE: it only reads text.

    python tools/analyze_tts_timing_log.py worker_log.txt [--phase0 docs/evidence/phase0_streaming_spike.json]

Input: any text that contains the worker's lines
    [TTS_TIMING] chars=30 audio_s=2.12 ref_s=0.000 generate_s=2.05 encode_s=0.004 total_s=2.06 rtf=0.97          (older handlers; newer ones add voice= and fmt=)
(copied from the RunPod console: timestamps and other lines are ignored). It reports, as functions of the audio length a (seconds):
  handler   generate_s and total_s fits           what the Python handler itself spent
  RunPod    executionTime fit (Phase 0, /run)     what RunPod charged as execution: the handler PLUS whatever RunPod does around it
  outside   RunPod execution - handler total      time that is inside RunPod's executionTime but NOT in the handler (response serialisation, the runtime loop)
  dispatch  wall - delayTime - executionTime      the time around the job (Phase 0 fit): upload, queue, status polling, download
Phase 0's rows and these log lines are different requests (different sentences), so the comparison is by the FITS at equal audio lengths, not row by row.
"""
import argparse, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from phase0b_tts_remeasure import linfit, med  # noqa: E402

LINE = re.compile(r"\[TTS_TIMING\]\s*(.*)")
PAIR = re.compile(r"(\w+)=(\S+)")


def parse(text: str) -> list:
    rows = []
    for ln in text.splitlines():
        m = LINE.search(ln)
        if not m:
            continue
        d = dict(PAIR.findall(m.group(1)))
        try:
            rows.append({"chars": int(d["chars"]), "audio_s": float(d["audio_s"]), "ref_s": float(d.get("ref_s", 0)), "generate_s": float(d["generate_s"]),
                         "encode_s": float(d.get("encode_s", 0)), "total_s": float(d["total_s"]), "voice": d.get("voice"), "fmt": d.get("fmt")})
        except (KeyError, ValueError):
            continue
    return rows


def fit_value(fit, a):
    return None if fit is None else round(fit[0] + fit[1] * a, 2)


def analyze(rows: list, phase0: dict | None, lengths=(2.0, 3.0, 4.0, 5.0, 6.0)) -> dict:
    ok = [r for r in rows if r["audio_s"] > 0.2]
    out = {"n": len(rows), "n_used": len(ok)}
    if len(ok) >= 2:
        out["generate_fit"] = linfit([r["audio_s"] for r in ok], [r["generate_s"] for r in ok])
        out["handler_total_fit"] = linfit([r["audio_s"] for r in ok], [r["total_s"] for r in ok])
    out["ref_s"] = {"requests_with_reference_prepared": sum(1 for r in rows if r["ref_s"] > 0.01), "mean_when_prepared_s": med([r["ref_s"] for r in rows if r["ref_s"] > 0.01]),
                    "median_s": med([r["ref_s"] for r in rows])}
    out["encode_s_median"] = med([r["encode_s"] for r in rows])
    out["voices"] = sorted({r["voice"] for r in rows if r["voice"]}) or None
    out["formats"] = sorted({r["fmt"] for r in rows if r["fmt"]}) or None
    if phase0:
        p0 = [r for r in phase0["tts"] if r["mode"] == "run" and r.get("exec_ms") is not None]
        out["runpod_exec_fit"] = linfit([r["audio_s"] for r in p0], [r["exec_ms"] / 1000 for r in p0])
        out["dispatch_fit"] = linfit([r["audio_s"] for r in p0 if r.get("overhead_ms") is not None], [r["overhead_ms"] / 1000 for r in p0 if r.get("overhead_ms") is not None])
        out["table"] = []
        for a in lengths:
            h, e, d = fit_value(out.get("handler_total_fit"), a), fit_value(out["runpod_exec_fit"], a), fit_value(out["dispatch_fit"], a)
            out["table"].append({"audio_s": a, "handler_total_s": h, "runpod_exec_s": e, "outside_handler_s": round(e - h, 2) if (h is not None and e is not None) else None,
                                 "dispatch_s": d, "wall_s": round(e + d, 2) if (e is not None and d is not None) else None})
    return out


def report(res: dict) -> None:
    print(f"\n=== TTS worker timing lines: {res['n']} found, {res['n_used']} with audio >= 0.2 s ===")
    if "generate_fit" not in res:
        print("not enough lines to fit (need at least 2)")
        return
    g, t = res["generate_fit"], res["handler_total_fit"]
    print(f"handler generate_s = {g[0]} + {g[1]} x audio_s   | handler total_s = {t[0]} + {t[1]} x audio_s")
    r = res["ref_s"]
    print(f"reference prepared on {r['requests_with_reference_prepared']} of {res['n']} requests (mean {r['mean_when_prepared_s']} s when it was; median over all {r['median_s']} s) "
          f"| encode median {res['encode_s_median']} s | voices {res['voices']} | formats {res['formats']}")
    if "table" in res:
        rf, df = res["runpod_exec_fit"], res["dispatch_fit"]
        print(f"RunPod executionTime (Phase 0, /run) = {rf[0]} + {rf[1]} x audio_s   | dispatch (wall - delay - exec) = {df[0]} + {df[1]} x audio_s")
        print("\n audio s | handler total | RunPod exec | outside the handler | dispatch around the job | wall")
        for x in res["table"]:
            print(f" {x['audio_s']:>7} | {x['handler_total_s']!s:>13} | {x['runpod_exec_s']!s:>11} | {x['outside_handler_s']!s:>19} | {x['dispatch_s']!s:>23} | {x['wall_s']}")
        print("\nReading it: 'outside the handler' is time RunPod counts as execution that the Python handler did not spend; 'dispatch' is the time around the job.")
        print("A handler whose generate_s fit has a large intercept spends that time INSIDE the model call (prefill, flow steps, watermark), not in the network.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log"); ap.add_argument("--phase0", default=str(ROOT / "docs" / "evidence" / "phase0_streaming_spike.json"))
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    rows = parse(Path(a.log).read_text(encoding="utf-8", errors="replace"))
    p0 = json.loads(Path(a.phase0).read_text(encoding="utf-8")) if a.phase0 and Path(a.phase0).exists() else None
    res = analyze(rows, p0)
    report(res)
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
