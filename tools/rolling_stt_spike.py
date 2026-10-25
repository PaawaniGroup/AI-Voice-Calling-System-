# -*- coding: utf-8 -*-
"""
tools/rolling_stt_spike.py - does decoding only the TAIL of an utterance shorten the final-text delay, and what does it cost in word errors?

    python tools/rolling_stt_spike.py DIR --budget 0.20 --yes [--tail-s 1.5] [--ctx-s 0.7] [--drop-words 1] [--lang hi] [--tenant mme-test]

Chargeable (the tenant's STT endpoint; the key is read from the tenant's ai_config, or from RUNPOD_STT_KEY when that is set; never printed). It stops at --budget.
Nothing in this file runs until the recordings of docs/RECORDING_PROTOCOL.md exist. Output: numbers and file ids only; the transcripts stay in memory.

Every recording runs in TWO versions, reported separately and sharing the one budget: "clean8k" (8 kHz mono) and "phone" (8 kHz, 300-3400 Hz, mu-law: tools/telephone_chain.py).
For each recording version (one utterance, 8 kHz) three decodes are made:
  whole   the whole utterance                                  -> the reference text and today's final-text delay (what the app waits for)
  prefix  everything up to (end - tail + ctx)                  -> would be decoded WHILE the caller is still finishing the sentence
  tail    the last (tail + ctx) seconds                        -> the only decode left after the end of the turn
The rolling text is prefix (its last --drop-words words removed, they may be cut mid-word) stitched to the tail by word overlap.
Final-text delay of the rolling scheme = the wall time of the tail decode, plus any part of the prefix decode that did not finish before the end of the turn was
confirmed. Word error = word edit distance of the rolling text against the WHOLE-utterance text, over its length.
Go/no-go of the plan: final text within 0.25 s after the end of speech (p50) with a word error no worse than 2 points above the plain decode. Decoding only a tail
helps only if the decode time grows with the audio length: the spike measures exactly that (compare whole and tail wall times).
"""
import argparse
import asyncio
import json
import statistics as st
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tools"), str(ROOT / "scripts")]
from telephone_chain import load_variants  # noqa: E402

PRICE_PER_S = 0.000192            # USD per second of a 24 GB worker (the price the plan assumes for STT; verify in the console)
IDLE_TAIL_S = 60.0                # one idle tail, as the other ledgers assume (the real idle behaviour is unverified)
RATE = 8000


# ------------------------------------------------------------------------------------------------------ pure helpers
def words(text: str) -> list:
    """Lower-case words with punctuation and symbols removed (Devanagari vowel signs are kept: only categories P* and S* are dropped)."""
    kept = "".join(" " if unicodedata.category(c)[0] in "PS" else c for c in (text or "").lower())
    return kept.split()


def wer(ref: list, hyp: list) -> float:
    """Word error rate of hyp against ref (Levenshtein over words / len(ref)). 0.0 for two empty texts, 1.0 per missing reference word."""
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / len(ref)


def stitch(prefix: list, tail: list, max_overlap: int = 8) -> list:
    """Join two word lists that cover an overlapping stretch of audio: the longest suffix of `prefix` equal to a prefix of `tail` is counted once."""
    for k in range(min(max_overlap, len(prefix), len(tail)), 0, -1):
        if prefix[-k:] == tail[:k]:
            return prefix + tail[k:]
    return prefix + tail


def pct(xs: list, p: float):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))], 3)


def split_audio(pcm: bytes, tail_s: float, ctx_s: float):
    """(prefix audio, tail audio) of one utterance, or None when it is too short to split (needs tail + ctx + 1 s)."""
    dur = len(pcm) / 2 / RATE
    if dur < tail_s + ctx_s + 1.0:
        return None
    cut = dur - tail_s
    a, b = int((cut + ctx_s) * RATE) * 2, int((cut - ctx_s) * RATE) * 2
    return pcm[:a], pcm[b:]


def summarize(rows: list) -> dict:
    ok = [r for r in rows if "error" not in r and "skipped" not in r]
    errs = [r["wer"] for r in ok]
    return {"n_files": len(rows), "n_measured": len(ok), "n_skipped_too_short": sum(1 for r in rows if "skipped" in r), "n_errors": sum(1 for r in rows if "error" in r),
            "whole_wall_s": {"p50": pct([r["whole_wall_s"] for r in ok], .5), "p95": pct([r["whole_wall_s"] for r in ok], .95)},
            "tail_wall_s": {"p50": pct([r["tail_wall_s"] for r in ok], .5), "p95": pct([r["tail_wall_s"] for r in ok], .95)},
            "rolling_final_delay_s": {"p50": pct([r["final_delay_s"] for r in ok], .5), "p95": pct([r["final_delay_s"] for r in ok], .95)},
            "prefix_ready_in_time_share": round(sum(1 for r in ok if r["prefix_in_time"]) / len(ok), 3) if ok else None,
            "wer_vs_whole": {"mean": round(st.mean(errs), 3) if errs else None, "median": pct(errs, .5), "p95": pct(errs, .95), "max": round(max(errs), 3) if errs else None,
                             "share_over_0.25": round(sum(1 for e in errs if e > 0.25) / len(errs), 3) if errs else None}}


def verdict(summary: dict) -> str:
    """The plan's go/no-go on the measured numbers."""
    d, w = summary["rolling_final_delay_s"]["p50"], summary["wer_vs_whole"]["mean"]
    if d is None or w is None:
        return "NO MEASUREMENT"
    return "GO" if d <= 0.25 and w <= 0.02 else "NO-GO"


# ------------------------------------------------------------------------------------------------------ the measurement
class Spend:
    def __init__(self, budget: float):
        self.budget, self.wall_s = budget, 0.0

    def projected(self) -> float:
        return (self.wall_s + IDLE_TAIL_S) * PRICE_PER_S

    def can_afford(self, next_s: float = 5.0) -> bool:
        return self.projected() + next_s * PRICE_PER_S <= self.budget


async def timed(clients, pcm: bytes, lang: str, spend: Spend):
    t0 = time.perf_counter()
    r = await clients.transcribe(pcm, RATE, lang)
    wall = time.perf_counter() - t0
    spend.wall_s += wall
    return (r or {}).get("text", ""), wall


async def measure(clients, files: dict, spend: Spend, *, tail_s=1.5, ctx_s=0.7, drop_words=1, lang="hi", confirm_s=0.7, warmup=True) -> dict:
    rows = []
    if warmup and files:
        first = next(iter(files.values()))
        await timed(clients, first[:RATE * 2], lang, spend)          # the first call may be a cold start: it is not part of any row
    for fid, pcm in files.items():
        parts = split_audio(pcm, tail_s, ctx_s)
        if parts is None:
            rows.append({"file": fid, "skipped": "too short to split"})
            continue
        if not spend.can_afford(3 * 5.0):
            rows.append({"file": fid, "error": "budget reached before this file"})
            break
        try:
            whole, ww = await timed(clients, pcm, lang, spend)
            pre, pw = await timed(clients, parts[0], lang, spend)
            tail, tw = await timed(clients, parts[1], lang, spend)
        except Exception as e:                                         # noqa: BLE001: one failed file must not hide the others
            rows.append({"file": fid, "error": type(e).__name__})
            continue
        pw_words = words(pre)
        rolling = stitch(pw_words[:-drop_words] if drop_words and len(pw_words) > drop_words else pw_words, words(tail))
        available = tail_s - ctx_s + confirm_s                          # time between the end of the prefix audio and the confirmed end of the turn
        rows.append({"file": fid, "audio_s": round(len(pcm) / 2 / RATE, 2), "whole_wall_s": round(ww, 3), "prefix_wall_s": round(pw, 3), "tail_wall_s": round(tw, 3),
                     "prefix_in_time": pw <= available, "final_delay_s": round(tw + max(0.0, pw - available), 3), "wer": round(wer(words(whole), rolling), 3),
                     "whole_words": len(words(whole)), "rolling_words": len(rolling)})
    summ = summarize(rows)
    return {"rows": rows, "summary": summ, "verdict": verdict(summ), "estimated_cost_usd": round(spend.projected(), 4), "budget_usd": spend.budget,
            "basis": f"estimate: wall seconds of every decode x {PRICE_PER_S} USD/s plus one {IDLE_TAIL_S:.0f} s idle tail; NOT an invoice"}


def report(res: dict) -> None:
    s = res["summary"]
    print(f"\n=== rolling-window STT spike: {s['n_measured']} measured of {s['n_files']} files ({s['n_skipped_too_short']} too short, {s['n_errors']} errors) ===")
    print(f"whole-utterance decode wall s : p50 {s['whole_wall_s']['p50']}  p95 {s['whole_wall_s']['p95']}   <- the final-text delay today")
    print(f"tail-only decode wall s       : p50 {s['tail_wall_s']['p50']}  p95 {s['tail_wall_s']['p95']}")
    print(f"rolling final-text delay s    : p50 {s['rolling_final_delay_s']['p50']}  p95 {s['rolling_final_delay_s']['p95']}   (prefix finished in time: {s['prefix_ready_in_time_share']})")
    w = s["wer_vs_whole"]
    print(f"word error vs whole decode    : mean {w['mean']}  median {w['median']}  p95 {w['p95']}  max {w['max']}  share over 0.25: {w['share_over_0.25']}")
    print(f"VERDICT {res['verdict']} (go: final delay p50 <= 0.25 s and mean word error <= 0.02) | cost estimate ${res['estimated_cost_usd']} of cap ${res['budget_usd']}")
    n = s["n_measured"]
    print(f"HOW MUCH THIS PROVES: these are medians and means over n={n} recordings" + ("; below about 20 the p95 is close to the maximum and only indicative" if n < 20 else "") +
          ". Recordings of one speaker are not independent: read the verdict as a range, not a guarantee.")


def make(a):
    from app.ai.clients import ModelClients
    from config.settings import Settings
    from measure_devanagari_app_path import load_cfg_brain
    import os
    s = Settings()
    cfg = load_cfg_brain(s, a.tenant)
    cfg["timeouts"] = {**(cfg.get("timeouts") or {}), "stt": 300.0}
    if os.environ.get("RUNPOD_STT_KEY"):
        cfg["keys"] = {**(cfg.get("keys") or {}), "stt": os.environ["RUNPOD_STT_KEY"]}
    return ModelClients(s, cfg)


def load_dir_variants(d: Path) -> dict:
    """{variant: {file id: pcm}} with both versions of every recording: "clean8k" and "phone" (8 kHz, 300-3400 Hz, mu-law)."""
    by_variant = {"clean8k": {}, "phone": {}}
    for p in sorted(d.glob("*.wav")):
        if p.stem.lower().startswith("roomtone"):
            continue
        for name, pcm in load_variants(p).items():
            by_variant[name][p.stem] = pcm
    return {k: v for k, v in by_variant.items() if v}


async def run(a) -> dict:
    d = Path(a.dir)
    by_variant = load_dir_variants(d)
    if not by_variant:
        raise SystemExit(f"no .wav files in {d}")
    cl, spend, res = make(a), Spend(a.budget), {}
    try:
        for i, (name, files) in enumerate(by_variant.items()):            # both versions share ONE budget; they are reported separately
            res[name] = await measure(cl, files, spend, tail_s=a.tail_s, ctx_s=a.ctx_s, drop_words=a.drop_words, lang=a.lang, warmup=(i == 0))
    finally:
        await cl.aclose()
    for name, r in res.items():
        print(f"\n######## version: {name} ########")
        report(r)
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir"); ap.add_argument("--tenant", default="mme-test"); ap.add_argument("--lang", default="hi")
    ap.add_argument("--budget", type=float, default=0.20); ap.add_argument("--tail-s", type=float, default=1.5); ap.add_argument("--ctx-s", type=float, default=0.7)
    ap.add_argument("--drop-words", type=int, default=1); ap.add_argument("--out", default=""); ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    if not a.yes:
        raise SystemExit("--yes required: chargeable")
    asyncio.run(run(a))


if __name__ == "__main__":
    main()
