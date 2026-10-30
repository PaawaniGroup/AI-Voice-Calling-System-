# -*- coding: utf-8 -*-
"""
tools/recording_intake_check.py - free intake check of a recordings folder (docs/RECORDING_PROTOCOL.md). OFFLINE: it reads WAV headers and samples; no network, no model, no key.

    python tools/recording_intake_check.py DIR [--out report.json]

For every .wav file it checks: the name (pattern, language code, _N / _SP flag for its line), the duration (at least 3 s; open answers 5-60 s; room tone at least 4 s), the sample
rate (at least 8 kHz), clipping, silence (no speech frame at all), a silent lead (0.3 s) and tail (0.4 s; 1.5 s for open answers: the tail marks their true end), and, for pause lines,
an internal pause of 0.6-1.0 s (and none longer than 1.3 s, which would count as the end of the turn). It counts the recordings whose speech is longer than the app's 6 s utterance cap
and lists them separately (not an error). It also lists the files of the protocol that are missing or unexpected.
It prints counts and file ids only: no audio, no words. Exit code 0 = no error; warnings do not fail.
"""
import argparse
import array
import audioop
import json
import re
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from app.ai.ws import MAX_UTTERANCE_MS, VAD_RMS, rms  # noqa: E402
from telephone_chain import to_8k_mono  # noqa: E402

FRAME_MS = 20
MIN_DURATION_S, SPONT_MIN_S, SPONT_MAX_S, ROOMTONE_MIN_S = 3.0, 5.0, 60.0, 4.0
MIN_LEAD_S, MIN_TAIL_S, SPONT_TAIL_S = 0.3, 0.4, 1.5
PAUSE_MIN_MS, PAUSE_MAX_MS, PAUSE_TOO_LONG_MS = 600, 1000, 1300
CLIP_LEVEL, CLIP_SHARE = 32700, 0.001
NAME = re.compile(r"^(?P<kind>[ups])(?P<num>\d\d)_(?P<lang>hi|hg|en)_(?P<spk>S[1-9])(?P<flag>_N|_SP)?$")
ROOMTONE = re.compile(r"^roomtone_(?P<spk>S[1-9])$")

# the protocol's tables
U_LANG = {1: "hi", 2: "hg", 3: "hi", 4: "hg", 5: "en", 6: "hi", 7: "hg", 8: "hi", 9: "en", 10: "hg", 11: "hi", 12: "hg", 13: "hi", 14: "hg", 15: "en"}
U_FLAG = {3: "_N", 7: "_N", 10: "_N", 9: "_SP"}
P_LANG = {**{i: "hi" for i in range(1, 8)}, **{i: "hg" for i in range(8, 15)}, **{i: "en" for i in range(15, 21)}}
SPEAKER_LINES = {"S1": range(1, 11), "S2": range(6, 16), "S3": list(range(1, 6)) + list(range(11, 16))}
SPEAKERS = tuple(SPEAKER_LINES)


def expected_names() -> set:
    """Every file stem the protocol asks for: 30 scripted/pause lines, 20 extra pause lines, 30 open answers (language free: checked by pattern only) and 3 room tones."""
    names = set()
    for spk, lines in SPEAKER_LINES.items():
        for n in lines:
            names.add(f"u{n:02d}_{U_LANG[n]}_{spk}{U_FLAG.get(n, '')}")
        names.add(f"roomtone_{spk}")
    for i in range(1, 21):
        names.add(f"p{i:02d}_{P_LANG[i]}_S{(i - 1) % 3 + 1}")
    return names


def is_pause_line(m) -> bool:
    return (m["kind"] == "p") or (m["kind"] == "u" and int(m["num"]) >= 13)


def internal_pauses_ms(pcm8k: bytes, vad_rms: float = VAD_RMS, min_run_ms: int = 200) -> list:
    """Lengths (ms) of the quiet runs between the first and the last speech frame, those of at least `min_run_ms`."""
    fb = 8000 * FRAME_MS // 1000 * 2
    loud = [rms(pcm8k[i:i + fb]) >= vad_rms for i in range(0, len(pcm8k) - fb + 1, fb)]
    if not any(loud):
        return []
    first, last = loud.index(True), len(loud) - 1 - loud[::-1].index(True)
    runs, run = [], 0
    for v in loud[first:last + 1]:
        if v:
            if run * FRAME_MS >= min_run_ms:
                runs.append(run * FRAME_MS)
            run = 0
        else:
            run += 1
    return runs


def lead_tail_s(pcm8k: bytes, vad_rms: float = VAD_RMS):
    """(seconds of silence before the first speech frame, seconds of silence after the last one), or (None, None) when there is no speech."""
    fb = 8000 * FRAME_MS // 1000 * 2
    loud = [rms(pcm8k[i:i + fb]) >= vad_rms for i in range(0, len(pcm8k) - fb + 1, fb)]
    if not any(loud):
        return None, None
    first, last = loud.index(True), len(loud) - 1 - loud[::-1].index(True)
    return first * FRAME_MS / 1000, (len(loud) - 1 - last) * FRAME_MS / 1000


def clipping(raw: bytes, width: int) -> tuple:
    """(share of samples at full scale, longest run of consecutive full-scale samples), on the original samples."""
    if width != 2:
        raw = audioop.lin2lin(raw, width, 2)
    s = array.array("h")
    s.frombytes(raw[: len(raw) // 2 * 2])
    if not s:
        return 0.0, 0
    at, run, longest = 0, 0, 0
    for v in s:
        if abs(v) >= CLIP_LEVEL:
            at += 1
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return at / len(s), longest


def check_file(path: Path) -> dict:
    """{"id", "errors": [codes], "warnings": [codes], "info": {...}} for one file. Codes only: never audio or words."""
    fid, errors, warnings, info = path.stem, [], [], {}
    m, rt = NAME.match(fid), ROOMTONE.match(fid)
    if not m and not rt:
        errors.append("bad_name")
    elif m:
        n = int(m["num"])
        if m["kind"] == "u":
            if not 1 <= n <= 15:
                errors.append("bad_line_number")
            else:
                if U_LANG[n] != m["lang"]:
                    errors.append("wrong_language_code")
                if (m["flag"] or "") != U_FLAG.get(n, ""):
                    errors.append("wrong_flag")
        elif m["kind"] == "p":
            if not 1 <= n <= 20:
                errors.append("bad_line_number")
            elif P_LANG[n] != m["lang"]:
                errors.append("wrong_language_code")
            if m["flag"]:
                errors.append("wrong_flag")
        else:
            if not 1 <= n <= 10:
                errors.append("bad_line_number")
            if m["flag"]:
                errors.append("wrong_flag")
    try:
        with wave.open(str(path), "rb") as w:
            ch, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(frames)
    except (wave.Error, EOFError, OSError):
        return {"id": fid, "errors": errors + ["unreadable"], "warnings": warnings, "info": info}
    dur = frames / rate if rate else 0.0
    info.update(duration_s=round(dur, 2), sample_rate=rate, channels=ch)
    if rate < 8000:
        errors.append("sample_rate_below_8000")
    share, run = clipping(raw, width)
    info["clipped_share"] = round(share, 5)
    if share > CLIP_SHARE or run >= 3:
        errors.append("clipped")
    pcm = to_8k_mono(path)
    loud_frames_present = any(rms(pcm[i:i + 320]) >= VAD_RMS for i in range(0, len(pcm) - 319, 320))
    kind = "roomtone" if rt else (m["kind"] if m else None)
    if kind == "roomtone":
        if dur < ROOMTONE_MIN_S:
            errors.append("too_short")
        if loud_frames_present:
            errors.append("room_not_quiet")
    else:
        min_d = SPONT_MIN_S if kind == "s" else MIN_DURATION_S
        if dur < min_d:
            errors.append("too_short")
        if kind == "s" and dur > SPONT_MAX_S:
            warnings.append("long_answer")
        if not loud_frames_present:
            errors.append("no_speech")
        else:
            peak = max(rms(pcm[i:i + 320]) for i in range(0, len(pcm) - 319, 320))
            if peak < 2 * VAD_RMS:
                warnings.append("quiet")
            lead, tail = lead_tail_s(pcm)
            speech_s = dur - lead - tail
            info.update(lead_s=round(lead, 2), tail_s=round(tail, 2), speech_s=round(speech_s, 2), over_6s_cap=speech_s > MAX_UTTERANCE_MS / 1000)
            if lead < MIN_LEAD_S:
                errors.append("lead_silence_too_short")
            if tail < (SPONT_TAIL_S if kind == "s" else MIN_TAIL_S):
                errors.append("tail_silence_too_short")
            if m and is_pause_line(m):
                runs = internal_pauses_ms(pcm)
                info["internal_pauses_ms"] = runs
                if not any(PAUSE_MIN_MS <= r <= PAUSE_MAX_MS for r in runs):
                    errors.append("no_pause_of_0.6_to_1_s")
                if any(r > PAUSE_TOO_LONG_MS for r in runs):
                    errors.append("pause_longer_than_1.3_s")
    return {"id": fid, "errors": sorted(set(errors)), "warnings": warnings, "info": info}


def check_dir(d: Path) -> dict:
    files = sorted(d.glob("*.wav"))
    results = [check_file(p) for p in files]
    found = {r["id"] for r in results}
    exp = expected_names()
    # open answers: any language code is fine, so only their numbers are expected
    spont_found = {(mm["spk"], mm["num"]) for mm in (NAME.match(f) for f in found) if mm and mm["kind"] == "s"}
    missing = sorted(exp - found)
    missing += sorted(f"s{n:02d}_*_{spk}" for spk in SPEAKERS for n in range(1, 11) if (spk, f"{n:02d}") not in spont_found)
    unexpected = sorted(f for f in found - exp if not (NAME.match(f) and NAME.match(f)["kind"] == "s" and NAME.match(f)["spk"] in SPEAKERS))
    by_code = {}
    for r in results:
        for c in r["errors"]:
            by_code.setdefault(c, []).append(r["id"])
    warn_by_code = {}
    for r in results:
        for c in r["warnings"]:
            warn_by_code.setdefault(c, []).append(r["id"])
    per_speaker = {}
    for r in results:
        mm = NAME.match(r["id"]) or ROOMTONE.match(r["id"])
        if mm:
            per_speaker[mm["spk"]] = per_speaker.get(mm["spk"], 0) + 1
    over = sorted(r["id"] for r in results if r["info"].get("over_6s_cap"))
    return {"n_files": len(results), "n_ok": sum(1 for r in results if not r["errors"]), "n_with_errors": sum(1 for r in results if r["errors"]),
            "over_6s_cap": {"n": len(over), "of": len(results), "files": over,
                            "note": "not an error: the app cuts an utterance at 6 s whatever the silence wait; the replay reports these recordings separately"},
            "errors_by_code": by_code, "warnings_by_code": warn_by_code, "missing": missing, "unexpected": unexpected, "files_per_speaker": dict(sorted(per_speaker.items())),
            "files": {r["id"]: {"errors": r["errors"], "warnings": r["warnings"], **r["info"]} for r in results}}


def report(res: dict) -> None:
    print(f"\n=== recording intake check: {res['n_files']} files, {res['n_ok']} without errors, {res['n_with_errors']} with errors ===")
    print("files per speaker:", res["files_per_speaker"])
    for code, ids in sorted(res["errors_by_code"].items()):
        print(f"ERROR {code}: {len(ids)} file(s): {', '.join(ids)}")
    for code, ids in sorted(res["warnings_by_code"].items()):
        print(f"warning {code}: {len(ids)} file(s): {', '.join(ids)}")
    cap = res["over_6s_cap"]
    print(f"RECORDINGS OVER THE 6 s CAP (counted separately, not an error): {cap['n']} of {cap['of']}" + (f": {', '.join(cap['files'])}" if cap["files"] else ""))
    print(f"missing from the protocol ({len(res['missing'])}): {', '.join(res['missing']) or 'none'}")
    print(f"not in the protocol ({len(res['unexpected'])}): {', '.join(res['unexpected']) or 'none'}")
    print("(ids and counts only: no audio and no words are printed or stored)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir"); ap.add_argument("--out", default="")
    a = ap.parse_args()
    res = check_dir(Path(a.dir))
    report(res)
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
    return 1 if res["n_with_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
