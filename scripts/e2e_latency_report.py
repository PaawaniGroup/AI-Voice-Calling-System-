"""Markdown report from e2e_latency_bench.py result files. Reads measured JSON only; anything not measured says so.

    python scripts/e2e_latency_report.py docs/evidence/e2e_latency_s1_*.json [s2.json] [s3.json] --out docs/evidence/e2e_report.md
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

SERVICES = ("stt", "brain", "tts")
TARGET = {"warm_p50_ms": 3000, "warm_p95_ms": 4500, "first_turn_ms": 8000}


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 1)


def slope(points):
    if len(points) < 3:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return round(sum((x - mx) * (y - my) for x, y in points) / den, 1) if den else None


def turns_ok(res):
    return [t for t in res.get("turns", []) if t.get("latency_ms") is not None]


def first_turn_delays(res):
    t0 = res["turns"][0] if res.get("turns") else None
    out = {s: 0.0 for s in SERVICES}
    for c in (t0 or {}).get("calls", []):
        out[c["service"]] += c.get("delay_ms") or 0.0
    return out


def warm_delay_median(results, svc):
    xs = [c["delay_ms"] for r in results for t in r.get("turns", [])[1:] for c in t["calls"]
          if c["service"] == svc and c.get("delay_ms") is not None and not c["cold"]]
    return statistics.median(xs) if xs else None


def section_scenario(name, res):
    L = [f"## {name.upper()}  ({res['scenario']}, started {res['started_at']})", ""]
    if res.get("halted"):
        L += [f"**HALTED: {res['halted']}**", ""]
    rows = turns_ok(res)
    if not rows:
        return L + ["No completed turns.", ""]
    warm = [r["latency_ms"] for r in rows[1:]]
    L += ["### 1. First turn vs turns 2-N", "",
          f"- first turn: **{rows[0]['latency_ms']} ms** (target < {TARGET['first_turn_ms']} ms)",
          f"- turns 2-N: p50 **{pct(warm, 50)} ms** (target {TARGET['warm_p50_ms']}), p95 **{pct(warm, 95)} ms** (target {TARGET['warm_p95_ms']}), max {max(warm) if warm else None} ms"]
    d0 = first_turn_delays(res)
    cold = {s: [c["delay_ms"] for t in res["turns"] for c in t["calls"] if c["service"] == s and c["cold_delay"]] for s in SERVICES}
    L += [f"- first-turn RunPod delay per endpoint (ms): {', '.join(f'{s}={round(v)}' for s, v in d0.items())}"
          f" -> dominant: **{max(d0, key=d0.get)}**" if any(d0.values()) else "- first-turn RunPod delay: not reported by the endpoints",
          "- cold starts (delay > 5 s) seen: " + (", ".join(f"{s}: {[round(x) for x in v]}" for s, v in cold.items() if v) or "none"), ""]
    L += ["### 2. Per-turn latency over the conversation", "",
          "| turn | latency ms | prompt tokens | clauses | cold endpoints | pause before s | error |", "|---|---|---|---|---|---|---|"]
    for r in res["turns"]:
        L.append(f"| {r['turn'] + 1} | {r['latency_ms']} | {r['brain_prompt_tokens']} | {r['clauses']} | "
                 f"{','.join(sorted({c['service'] for c in r['calls'] if c['cold']})) or '-'} | {r['gap_before_s']} | {r['error'] or ''} |")
    allv = [r["latency_ms"] for r in rows]
    nowarmcold = [(r["turn"], r["latency_ms"]) for r in rows[1:] if not any(c["cold"] for c in r["calls"])]
    L += ["", f"- all turns: p50 {pct(allv, 50)} / p95 {pct(allv, 95)} / max {max(allv)} ms; turns > 3 s: **{sum(v > 3000 for v in allv)}**, > 6 s: **{sum(v > 6000 for v in allv)}**",
          f"- growth with history (warm turns only, least-squares slope): {slope(nowarmcold)} ms per turn" if slope(nowarmcold) is not None else "- growth with history: too few warm turns to fit",
          "- mid-call cold starts (turn >= 2, delay > 5 s): " + (", ".join(
              f"turn {r['turn'] + 1} {c['service']} (delay {round(c['delay_ms'])} ms, pause before {r['gap_before_s']} s)"
              for r in res["turns"][1:] for c in r["calls"] if c["cold_delay"]) or "none"), ""]
    ret = [r for r in rows if r["kind"] == "retrieval"]
    L += ["### 3. Tool / retrieval turns", "", (f"- retrieval turns: {len(ret)}; extra time (rag_ms): {[r['rag_ms'] for r in ret]}" if ret else
          f"- **not measured**: rag_url set = {res['retrieval']['rag_url_set']}; skipped clips: {len(res['retrieval']['skipped_clips'])}; {res['retrieval']['tools']}"), ""]
    gaps = [g for r in rows for g in r["speech_gaps_s"]]
    rtf = [x for r in rows for x in r["tts_rtf_per_clause"]]
    L += ["### 4. Gaps in speech and real-time factor (playback is SIMULATED from arrival times and clip lengths)", "",
          f"- turns with a gap: {sum(1 for r in rows if r['speech_gaps_s'])}/{len(rows)}; gap total {round(sum(gaps), 2)} s; worst gap {max(gaps) if gaps else 0} s",
          f"- TTS real-time factor per clause (executionTime / audio length): p50 {pct(rtf, 50)}, p95 {pct(rtf, 95)}, max {max(rtf) if rtf else None}", ""]
    c = res["cost"]
    L += ["### 5. Cost of this conversation (estimate)", "",
          f"- **${c['usd_estimated_total']}** of cap ${c['budget_usd']}; per endpoint: " + ", ".join(f"{s} ${v['usd']}" for s, v in c["per_service"].items()),
          f"- basis: {c['basis']}", ""]
    worst = sorted(rows, key=lambda r: -r["latency_ms"])[:5]
    L += ["### 7a. Worst 5 turns", "", "| turn | latency ms | dominant stage | stt / brain / tts-first client ms | delay ms (stt/brain/tts) |", "|---|---|---|---|---|"]
    for r in worst:
        def cm(s, k="client_ms"):
            xs = [x[k] for x in r["calls"] if x["service"] == s and x.get(k) is not None]
            return xs[0] if xs else 0
        stages = {"stt": cm("stt"), "brain": cm("brain"), "tts": cm("tts")}
        L.append(f"| {r['turn'] + 1} | {r['latency_ms']} | {max(stages, key=stages.get)} | {stages['stt']} / {stages['brain']} / {stages['tts']} | "
                 f"{cm('stt', 'delay_ms')} / {cm('brain', 'delay_ms')} / {cm('tts', 'delay_ms')} |")
    return L + [""]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out")
    ap.add_argument("--calls-per-day", type=int, default=100)
    args = ap.parse_args(argv)
    results = [json.loads(Path(f).read_text(encoding="utf-8")) for f in args.files]
    L = ["# End-to-end latency report (REAL RunPod endpoints; measured numbers only)", ""]
    for r in results:
        L += section_scenario(Path(r.get("scenario", "?")).name, r)
    price, idle = results[0]["price_per_s"], results[0]["idle_timeout_s"]
    per_call = statistics.fmean(r["cost"]["usd_estimated_total"] for r in results)
    month = per_call * args.calls_per_day * 30
    L += ["## 5b. Monthly cost at %d calls/day" % args.calls_per_day, "",
          f"- scale-to-zero: measured mean {round(per_call, 4)} USD/conversation x {args.calls_per_day} x 30 = **${round(month, 2)}/month** "
          f"(from {len(results)} measured conversation(s); includes idle timeouts)", ""]
    L += ["| endpoint | one ACTIVE worker, 24x7 (USD/month) |", "|---|---|"]
    for s in SERVICES:
        L.append(f"| {s} | {round(price[s] * 86400 * 30, 2)} |")
    L += ["", "Active-worker price assumes the same per-second rate as flex workers (RunPod may discount active workers: not verified). "
          "With an active worker its job time is already paid for, so scale-to-zero cost for that endpoint disappears.", ""]
    s1 = next((r for r in results if r["scenario"] in ("s1", "s3") and turns_ok(r)), None)
    L += ["## 6. What-if: one active worker", ""]
    if s1:
        base = turns_ok(s1)[0]["latency_ms"]
        d0 = first_turn_delays(s1)
        gains = {}
        for s in SERVICES:
            w = warm_delay_median(results, s)
            gains[s] = None if w is None else max(0.0, d0[s] - w)
        L += ["| endpoint kept warm | first-turn latency would be (ms) | saved (ms) |", "|---|---|---|"]
        for s, g in gains.items():
            L.append(f"| {s} | {round(base - g) if g is not None else 'n/a (no warm-delay measurement: run s2)'} | {round(g) if g is not None else 'n/a'} |")
        known = {s: g for s, g in gains.items() if g is not None}
        if known:
            L.append(f"\nBiggest gain: **{max(known, key=known.get)}**. Method: first-turn latency minus (that endpoint's first-turn RunPod delay minus its median warm delay); "
                     "the three stages run in sequence, so their delays add up. This is arithmetic on measured values, not a measurement of a warm-STT run.")
    else:
        L.append("not measured: needs an s1 or s3 result with completed turns.")
    L += ["", "## 7b. What could NOT be verified", "",
          "- The VAD end-of-speech window and the Plivo network hop are NOT in these latencies (no phone leg offline); add them for phone numbers.",
          "- The Brain is NOT streamed over RunPod `/runsync`: the first clause goes to TTS only after the whole reply exists, so a streaming benefit cannot be measured on this path.",
          "- Playback gaps are simulated from clip lengths and arrival times, not heard on a call.",
          "- Cost is an estimate from delayTime + executionTime + idle gaps/tails x the given price per second, not RunPod's invoice; cold-start billing of the delay window is assumed.",
          "- Early wake-up (s3) is a tiny real job per endpoint (0.5 s silence / 'hi' / one word): 'hidden cold start' = ping delay minus the first real call's delay.",
          "- Jobs inside a blocking `/runsync` request (first ~90 s) have no known id, so they cannot be cancelled on abort; later polled jobs can."]
    for r in results:
        if r["caller_audio"]["stt_numbers_optimistic"]:
            L.append(f"- {r['scenario']}: caller audio was synthesized: STT numbers are OPTIMISTIC.")
        if r.get("clips_reused"):
            L.append(f"- {r['scenario']}: {r['clips_reused']} clip(s) were reused to reach the turn count.")
        if r.get("halted"):
            L.append(f"- {r['scenario']}: run halted early: {r['halted']}")
    text = "\n".join(L)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
