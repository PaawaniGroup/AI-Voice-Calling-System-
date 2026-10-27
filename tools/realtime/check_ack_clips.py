"""Free, offline intake check of a folder of cached acknowledgement clips (raw 8 kHz mu-law + manifest.json): the same criteria as tools/recording_intake_check.py (sample rate,
duration, clipping, silence) applied to this layout, because that tool is for the recording protocol's WAV files and its file-name patterns do not fit. Also loads the set through
AckSet.load (neutrality of every phrase, at least two clips per language). Prints counts and file ids only: no audio, no words. Exit code 0 = no error.

  python tools/realtime/check_ack_clips.py DIR
"""
from __future__ import annotations

import audioop
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.ai import codec                                          # noqa: E402
from app.ai.streaming.ack import MANIFEST, MAX_MS, MIN_MS, AckSet  # noqa: E402

RATE = codec.MULAW_RATE                                             # 8000: the sample rate of a raw mu-law clip is fixed by the format
CLIP_LEVEL, CLIP_SHARE = 32000, 0.001                               # share limit as in recording_intake_check.py; the level is 32000, not its 32700: a mu-law clip decodes to at most +-32124 (checked on the repo's codec), so 32700 could never fire
MIN_RMS, MIN_LEAD_MS = 200, 0                                       # a clip with no speech-level frame at all is an error


def check(directory) -> dict:
    d = pathlib.Path(directory)
    report = {"errors": [], "clips": [], "load": None}
    mf = d / MANIFEST
    if not mf.is_file():
        report["errors"].append("no_manifest")
        return report
    try:
        items = json.loads(mf.read_text(encoding="utf-8")).get("clips") or []
    except (ValueError, OSError):
        report["errors"].append("bad_manifest")
        return report
    for it in items:
        name = it.get("file") if isinstance(it, dict) else None
        row = {"file": name, "ms": None, "clipping_share": None, "peak_rms": None, "errors": []}
        p = d / name if isinstance(name, str) else None
        if p is None or not p.is_file():
            row["errors"].append("missing")
        else:
            raw = p.read_bytes()
            row["ms"] = len(raw) * 1000 // RATE
            if not (MIN_MS <= row["ms"] <= MAX_MS):
                row["errors"].append("duration")
            pcm = codec.mulaw_to_pcm16(raw)
            n = len(pcm) // 2
            hot = sum(1 for i in range(0, len(pcm) - 1, 2) if abs(int.from_bytes(pcm[i:i + 2], "little", signed=True)) >= CLIP_LEVEL)
            row["clipping_share"] = round(hot / n, 5) if n else None
            if n and hot / n > CLIP_SHARE:
                row["errors"].append("clipping")
            win = RATE // 50 * 2
            levels = [audioop.rms(pcm[i:i + win], 2) for i in range(0, len(pcm) - win + 1, win)]
            row["peak_rms"] = max(levels) if levels else 0
            if row["peak_rms"] < MIN_RMS:
                row["errors"].append("silent")
        report["clips"].append(row)
        report["errors"] += [f"{name}:{e}" for e in row["errors"]]
    s, why = AckSet.load(d)
    report["load"] = why
    if why != "ok":
        report["errors"].append(f"load:{why}")
    return report


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: check_ack_clips.py DIR")
        return 2
    r = check(argv[0])
    print(json.dumps({"clips": len(r["clips"]), "load": r["load"], "errors": r["errors"], "per_clip": r["clips"]}))
    return 0 if not r["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
