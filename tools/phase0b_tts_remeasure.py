# -*- coding: utf-8 -*-
"""
tools/phase0b_tts_remeasure.py - re-measure the TTS clauses of Phase 0 against the v3 TEST endpoint, then probe concurrency. CHARGEABLE (TTS only).

    python tools/phase0b_tts_remeasure.py --endpoint-id <id of mme-tts-v3-test> --budget 0.15 --yes                # the 30 clauses (+ variants B and C)
    python tools/phase0b_tts_remeasure.py --endpoint-id <id> --probe --budget 0.05 --idle 10 --yes                  # concurrency probe: 2 and 3 jobs at once

The key is read from the tenant's ai_config (never printed). The ledger counts delayTime + executionTime + the idle tail and stops at --budget.

Variants, on the SAME worker and the SAME clauses, to separate where the time goes:
  A  ulaw8k + named voice + no reference    the new request (what the app sends with tts_ulaw8k)
  B  wav (24 kHz) + named voice, no ref     = A but a 6x bigger result: A vs B isolates the result size / encoding
  C  wav (24 kHz) + the 768 KB reference    the OLD request; the v3 worker ignores the reference: B vs C isolates the reference upload and its decoding
Phase 0 (the old worker, the old request) is compared in the report. Output: timings, lengths, counts, byte sizes. No keys, no text.
Listening files (5 mu-law replies as WAV) are written to --listen-dir, which must be OUTSIDE the repository (the commit guard refuses audio).
"""
import argparse, asyncio, audioop, base64, json, os, statistics, sys, time, wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "tools"))
from e2e_latency_bench import Ledger, StopBench, pct  # noqa: E402
from app.ai.clients import ModelClients, wav_to_pcm16  # noqa: E402
from config.settings import Settings  # noqa: E402
from measure_devanagari_app_path import load_cfg_brain  # noqa: E402
from phase0_streaming_spike import PRICE, Spike  # noqa: E402
from spike_clauses import CLAUSES  # noqa: E402

VARIANTS = ("A", "B", "C")
POLL_CAP = 0.15          # the app's poll cap since Phase 1 (Phase 0 polled with a 0.4 s cap: it is a difference between the two runs)


# ------------------------------------------------------------------------------------------------ pure analysis (unit-tested offline)
def linfit(xs, ys):
    """Least squares y = a + b*x. Returns (a, b) or None."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return round(my - b * mx, 3), round(b, 3)


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 3) if xs else None


def summarize_rows(rows):
    """rows: dicts with words, audio_s, exec_ms, wall_ms, overhead_ms, delay_ms. Returns the fits and the per-length table."""
    ok = [r for r in rows if r.get("audio_s") and r.get("exec_ms") is not None]
    out = {"n": len(ok),
           "exec_fit_s_vs_audio_s": linfit([r["audio_s"] for r in ok], [r["exec_ms"] / 1000 for r in ok]),                     # exec = a + b * audio
           "overhead_fit_s_vs_audio_s": linfit([r["audio_s"] for r in ok if r.get("overhead_ms") is not None],
                                              [r["overhead_ms"] / 1000 for r in ok if r.get("overhead_ms") is not None]),
           "by_words": {}}
    for w in sorted({r["words"] for r in ok}):
        g = [r for r in ok if r["words"] == w]
        out["by_words"][w] = {"n": len(g), "audio_s": med([r["audio_s"] for r in g]), "exec_s": med([r["exec_ms"] / 1000 for r in g]),
                              "wall_s": med([r["wall_ms"] / 1000 for r in g]), "overhead_s": med([r["overhead_ms"] / 1000 for r in g if r.get("overhead_ms") is not None]),
                              "wall_over_audio": med([r["wall_ms"] / 1000 / r["audio_s"] for r in g]),
                              "exec_over_audio_rtf": med([r["exec_ms"] / 1000 / r["audio_s"] for r in g])}
    return out


def concurrency_verdict(single_wall_s, walls, delays_ms):
    """walls: wall seconds of the jobs started together. 'parallel' if the slowest is close to one job's wall time."""
    slow = max(walls)
    ratio = round(slow / single_wall_s, 2) if single_wall_s else None
    queued = [d for d in delays_ms if d is not None]
    return {"n": len(walls), "slowest_wall_s": round(slow, 2), "slowest_over_single": ratio, "max_delay_ms": max(queued) if queued else None,
            "verdict": ("parallel" if ratio is not None and ratio <= 1.35 else
                        "serialised" if ratio is not None and ratio >= len(walls) * 0.8 else "partly parallel")}


# ------------------------------------------------------------------------------------------------ the measurement
class V3Spike(Spike):
    def __init__(self, cl, ledger, endpoint_url, voice):
        super().__init__(cl, ledger)
        self.url, self.voice = endpoint_url, voice

    def base(self, svc):
        return self.url if svc == "tts" else super().base(svc)

    def body(self, text, variant):
        inp = self.tts_body(text)["input"]                                  # the app's own normalisation, language tag and model field
        if variant == "C":                                                  # the old request: reference included, no voice, WAV answer
            return {"input": inp}
        inp.pop("reference_audio_base64", None)
        inp["voice"] = self.voice
        if variant == "A":
            inp["output_format"] = "ulaw8k"
        return {"input": inp}

    async def clause(self, text, variant, mode="run"):
        body = self.body(text, variant)
        rec, out = await self.job("tts", body, mode, poll_cap=POLL_CAP)
        if isinstance(out, dict) and out.get("error"):
            raise RuntimeError(f"worker error: {str(out['error'])[:80]}")
        b64 = (out or {}).get("audio_base64") or (out or {}).get("audio")
        raw = base64.b64decode(b64)
        fmt = (out or {}).get("format", "wav")
        if fmt == "ulaw8k":
            audio_s, pcm8 = len(raw) / 8000, audioop.ulaw2lin(raw, 2)
        else:
            pcm, rate = wav_to_pcm16(raw)
            audio_s, pcm8 = len(pcm) / 2 / rate, audioop.ratecv(pcm, 2, 1, rate, 8000, None)[0]
        rec.update(variant=variant, words=len(text.split()), audio_s=round(audio_s, 3), format=fmt, request_bytes=len(json.dumps(body)),
                   result_b64_bytes=len(b64), worker_timing=(out or {}).get("timing"), voice=(out or {}).get("voice"))
        return rec, pcm8


def write_wav(path: Path, pcm8: bytes):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(pcm8)


async def workers(sp):
    r = await sp.cl._http.get(f"{sp.url}/health", headers=sp.cl._auth_headers("tts"), timeout=30)
    w = (r.json() or {}).get("workers", {}) if r.status_code < 400 else {}
    return {k: w.get(k) for k in ("idle", "ready", "running", "initializing")}, r.status_code


def make(a, reserve):
    s = Settings()
    cfg = load_cfg_brain(s, a.tenant)
    cfg["timeouts"] = {**(cfg.get("timeouts") or {}), "tts": 900.0}
    if os.environ.get("RUNPOD_TTS_KEY"):   # a key made for the test endpoint wins over the one stored for the live voice endpoint
        cfg["keys"] = {**(cfg.get("keys") or {}), "tts": os.environ["RUNPOD_TTS_KEY"]}
    url = f"https://api.runpod.ai/v2/{a.endpoint_id}"
    cfg["tts_url"] = url + "/runsync"
    cl = ModelClients(s, cfg)
    ledger = Ledger(PRICE, {"stt": 0.0, "brain": 0.0, "tts": a.idle}, a.budget, reserve)
    return cl, V3Spike(cl, ledger, url, a.voice)


async def run_measure(a):
    out = {"endpoint": a.endpoint_id, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "poll_cap_s": POLL_CAP, "voice": a.voice, "rows": []}
    cl, sp = make(a, reserve=400.0)                     # a first start can pull the image: reserve for it, the ledger still stops at the budget
    listen = {}
    try:
        w, code = await workers(sp)
        out["workers_before"], out["health_http"] = w, code
        print("workers before:", json.dumps(w), "health http", code, flush=True)
        rec, _ = await sp.job("tts", {"input": {"warmup": True}}, "run", timeout=900, poll_cap=POLL_CAP)
        out["warmup"] = {k: rec[k] for k in ("delay_ms", "exec_ms", "wall_ms")}
        print("warm-up job:", json.dumps(out["warmup"]), flush=True)
        sp.ledger.cold_reserve_s = 0.0
        # A for all 30 clauses, round-robin over the lengths (as in Phase 0); B and C for the first 3 clauses of each length
        for j in range(max(len(v) for v in CLAUSES.values())):
            for n, items in CLAUSES.items():
                if j >= len(items):
                    continue
                for variant in (("A", "B", "C") if j < 3 else ("A",)):
                    rec, pcm8 = await sp.clause(items[j], variant)
                    out["rows"].append(rec)
                    if variant == "A" and len(listen) < 5 and (n, j) in ((4, 1), (8, 0), (12, 0), (12, 3), (16, 0)):
                        listen[(n, j)] = pcm8
                    print(f"{variant} {n:>2}w exec={rec['exec_ms']} audio={rec['audio_s']}s wall={rec['wall_ms']} overhead={rec['overhead_ms']} req={rec['request_bytes']}B res={rec['result_b64_bytes']}B", flush=True)
    except StopBench as e:
        out["halted"] = str(e)
    finally:
        out["cost"] = sp.ledger.report()
        await cl.aclose()
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    if listen and a.listen_dir:
        d = Path(a.listen_dir)
        if ROOT in d.resolve().parents or d.resolve() == ROOT:
            raise SystemExit("--listen-dir must be outside the repository (the commit guard refuses audio files)")
        d.mkdir(parents=True, exist_ok=True)
        gap, parts = b"\x00\x00" * 5600, []
        for i, (key, pcm8) in enumerate(sorted(listen.items()), 1):
            write_wav(d / f"reply{i}_{key[0]}words_ulaw8k.wav", pcm8)
            parts += [pcm8, gap]
        write_wav(d / "all5_ulaw8k.wav", b"".join(parts))
        out["listen_files"] = sorted(p.name for p in d.iterdir())
        Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    report(out, a.phase0)


def report(out, phase0_path):
    rows = out["rows"]
    print("\n=== PHASE 0b: TTS v3 re-measurement (timings, lengths, byte sizes only) ===")
    print(f"endpoint {out['endpoint']} | poll cap {out['poll_cap_s']} s | halted: {out.get('halted')} | cost est ${out.get('cost', {}).get('usd_estimated_total')} (cap ${out.get('cost', {}).get('budget_usd')})")
    summ = {}
    for v, label in (("A", "A ulaw8k + voice, no reference"), ("B", "B wav 24k + voice, no reference"), ("C", "C wav 24k + 768 KB reference (old request)")):
        g = [r for r in rows if r["variant"] == v]
        if not g:
            continue
        s = summ[v] = summarize_rows(g)
        print(f"\n[{label}] n={s['n']}  exec = {s['exec_fit_s_vs_audio_s']} (a s, b s per audio s)  overhead = {s['overhead_fit_s_vs_audio_s']}  | request {med([r['request_bytes'] for r in g])} B, result {med([r['result_b64_bytes'] for r in g])} B (b64)")
        print("   words | n | audio s | exec s | RTF | wall s | wall/audio | overhead s")
        for w, t in s["by_words"].items():
            print(f"   {w:>5} | {t['n']} | {t['audio_s']} | {t['exec_s']} | {t['exec_over_audio_rtf']} | {t['wall_s']} | {t['wall_over_audio']} | {t['overhead_s']}")
    if phase0_path and Path(phase0_path).exists():
        p0 = [r for r in json.loads(Path(phase0_path).read_text(encoding="utf-8"))["tts"] if r["mode"] == "run"]
        s0 = summarize_rows([{**r, "words": r["words"]} for r in p0])
        print(f"\n[Phase 0, OLD worker, /run, old request] n={s0['n']}  exec = {s0['exec_fit_s_vs_audio_s']}  overhead = {s0['overhead_fit_s_vs_audio_s']}")
        for w, t in s0["by_words"].items():
            print(f"   {w:>5} | {t['n']} | {t['audio_s']} | {t['exec_s']} | {t['exec_over_audio_rtf']} | {t['wall_s']} | {t['wall_over_audio']} | {t['overhead_s']}")
        if "A" in summ:
            print("\nA vs Phase 0 by clause length: wall s (Phase 0 -> v3 A) and the change:")
            for w, t in summ["A"]["by_words"].items():
                b = s0["by_words"].get(w)
                if b:
                    print(f"   {w:>2} words: wall {b['wall_s']} -> {t['wall_s']} ({round(t['wall_s'] - b['wall_s'], 2):+}) | exec {b['exec_s']} -> {t['exec_s']} | overhead {b['overhead_s']} -> {t['overhead_s']}")
    if "A" in summ and "B" in summ:
        a_, b_ = summ["A"]["by_words"], summ["B"]["by_words"]
        print("\nWhere the time goes (medians per length; B-A = result size and encoding, C-B = reference upload and decode):")
        for w in a_:
            if w in b_:
                c_ = summ.get("C", {}).get("by_words", {}).get(w)
                print(f"   {w:>2} words: wall A {a_[w]['wall_s']} | B {b_[w]['wall_s']} (B-A {round(b_[w]['wall_s'] - a_[w]['wall_s'], 2):+})" +
                      (f" | C {c_['wall_s']} (C-B {round(c_['wall_s'] - b_[w]['wall_s'], 2):+})" if c_ else ""))


def classify_probe(out):
    """What a ONE-request probe with output_format ulaw8k and no reference tells about the worker behind the endpoint. Returns (verdict, detail)."""
    if not isinstance(out, dict):
        return "unexpected", "the output is not an object"
    err = str(out.get("error") or "")
    if err:
        if "output_format must be" in err and "ulaw8k" not in err:
            return "old_worker", "refuses ulaw8k (the handler (5) generation: voice from the request, wav/pcm16 only)"
        if "reference_audio_base64" in err or "reference" in err.lower() and "provide" in err.lower():
            return "old_worker", "requires a reference in the request (older than handler (5))"
        if err.startswith("Unknown voice"):
            return "new_handler_other_voice", f"v3 handler, but it has no voice of that name; it offers {out.get('voices')}"
        return "error", err[:100]
    if out.get("format") == "ulaw8k":
        return "new_handler", "answered ulaw8k"
    return "old_or_unknown", f"answered format {out.get('format')!r} instead of ulaw8k"


WARM_RESERVE_S = 30.0


def warm_reserve_s(workers_health: dict, cold_reserve_s: float) -> float:
    """Seconds of billing the ledger reserves before a call: a worker that /health shows as idle, ready or running means no cold start to pay for. NOTE: the
    first pickup on such a worker can still take about 50 s (Phase 0b, 25 Oct), so callers must still wait long enough."""
    up = sum(int((workers_health or {}).get(k) or 0) for k in ("idle", "ready", "running"))
    return min(cold_reserve_s, WARM_RESERVE_S) if up else cold_reserve_s


async def run_ulaw_probe(a):
    """ONE request: output_format ulaw8k, a named voice, NO reference. An old worker can only refuse it (cheap); a v3 worker answers with 8 kHz mu-law."""
    cl, sp = make(a, reserve=200.0)
    out_rec = {"endpoint": a.endpoint_id, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        w, code = await workers(sp)
        out_rec["workers_before"], out_rec["health_http"] = w, code
        print("workers before:", json.dumps(w), "health http", code, flush=True)
        sp.ledger.cold_reserve_s = warm_reserve_s(w, sp.ledger.cold_reserve_s)
        inp = sp.tts_body(CLAUSES[4][0])["input"]
        inp.pop("reference_audio_base64", None)
        inp.update(voice=a.voice, output_format="ulaw8k")
        try:
            rec, out = await asyncio.wait_for(sp.job("tts", {"input": inp}, "run", timeout=a.probe_timeout, poll_cap=1.0), a.probe_timeout)   # polled about once a second
            out_rec.update(status_first_seen_s=getattr(sp, "status_log", None), worker_id=getattr(sp, "worker_id", None))
        except asyncio.TimeoutError:
            await sp.cancel_all()
            out_rec.update(verdict="timeout", job_id=getattr(sp, "last_id", None), status_first_seen_s=getattr(sp, "status_log", None), worker_id=getattr(sp, "worker_id", None))
            print(f"no answer within {a.probe_timeout:.0f} s: the job was cancelled. job id {out_rec['job_id']} | status first seen at (s): {out_rec['status_first_seen_s']} "
                  "(IN_QUEUE only = no worker took it; IN_PROGRESS = a worker started and did not finish). NOTE: cancelling does not stop a running handler.", flush=True)
            return out_rec
        verdict, detail = classify_probe(out)
        raw = base64.b64decode((out or {}).get("audio_base64") or "") if isinstance(out, dict) and not out.get("error") else b""
        out_rec.update(job_id=getattr(sp, "last_id", None), verdict=verdict, detail=detail, delay_ms=rec["delay_ms"], exec_ms=rec["exec_ms"], wall_ms=rec["wall_ms"], overhead_ms=rec["overhead_ms"],
                       sample_rate=(out or {}).get("sample_rate") if isinstance(out, dict) else None, format=(out or {}).get("format") if isinstance(out, dict) else None,
                       voice=(out or {}).get("voice") if isinstance(out, dict) else None, audio_bytes=len(raw), audio_s=round(len(raw) / 8000, 3) if raw else None,
                       worker_timing=(out or {}).get("timing") if isinstance(out, dict) else None, error=(str((out or {}).get("error"))[:120] if isinstance(out, dict) and out.get("error") else None))
        print("PROBE RESULT:", json.dumps({k: v for k, v in out_rec.items() if k not in ("started",)}, ensure_ascii=False), flush=True)
        return out_rec
    finally:
        out_rec["cost"] = sp.ledger.report()
        await cl.aclose()
        print("estimated cost of the probe: $", out_rec["cost"]["usd_estimated_total"], "(delay + execution + one idle tail of", a.idle, "s)", flush=True)
        Path(a.out_probe1).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out_probe1).write_text(json.dumps(out_rec, ensure_ascii=False, indent=1), encoding="utf-8")


async def run_old_style_probe(a):
    """ONE request exactly as the telephony app sends it today: the reference voice in the request, no voice name, the default (wav) output. It shows whether the worker
    behind the endpoint is healthy at all. Sent only when a worker is ALREADY warm (a cold start alone could exceed a small cap); nothing is sent otherwise."""
    cl, sp = make(a, reserve=0.0)
    rec_out = {"endpoint": a.endpoint_id, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "style": "old (reference in the request, wav)"}
    try:
        w, code = await workers(sp)
        rec_out["workers_before"] = w
        print("workers before:", json.dumps(w), "health http", code, flush=True)
        if not sum(int(w.get(k) or 0) for k in ("idle", "ready", "running")) and not a.allow_cold:
            print("no worker is warm: a cold start could exceed the cap, so NOTHING was sent (use --allow-cold to accept that)", flush=True)
            rec_out["verdict"] = "not_sent_no_warm_worker"
            return rec_out
        body = {"input": sp.tts_body(CLAUSES[4][0])["input"]}                 # the app's own request (with the reference)
        rec_out["request_fields"] = sorted(body["input"])
        try:
            rec, out = await asyncio.wait_for(sp.job("tts", body, "run", timeout=a.probe_timeout, poll_cap=POLL_CAP), a.probe_timeout)
        except asyncio.TimeoutError:
            await sp.cancel_all()
            rec_out.update(verdict="timeout", job_id=getattr(sp, "last_id", None), status_first_seen_s=getattr(sp, "status_log", None))
            print(f"no answer within {a.probe_timeout:.0f} s: cancelled. job id {rec_out['job_id']} | status first seen at (s): {rec_out['status_first_seen_s']}", flush=True)
            return rec_out
        rate, dur = None, None
        if isinstance(out, dict) and not out.get("error"):
            b64 = out.get("audio_base64") or out.get("audio")
            pcm, rate = wav_to_pcm16(base64.b64decode(b64))
            dur = round(len(pcm) / 2 / rate, 3)
        rec_out.update(job_id=getattr(sp, "last_id", None), verdict="error" if isinstance(out, dict) and out.get("error") else "ok", delay_ms=rec["delay_ms"], exec_ms=rec["exec_ms"],
                       wall_ms=rec["wall_ms"], overhead_ms=rec["overhead_ms"], sample_rate=rate, audio_s=dur, format=(out or {}).get("format") if isinstance(out, dict) else None,
                       worker_timing=(out or {}).get("timing") if isinstance(out, dict) else None, error=(str(out.get("error"))[:120] if isinstance(out, dict) and out.get("error") else None))
        print("OLD-STYLE PROBE RESULT:", json.dumps({k: v for k, v in rec_out.items() if k != "started"}, ensure_ascii=False), flush=True)
        return rec_out
    except StopBench as e:
        rec_out["verdict"] = "refused_by_ledger"
        print("refused by the spend ledger BEFORE sending:", e, flush=True)
        return rec_out
    finally:
        rec_out["cost"] = sp.ledger.report()
        await cl.aclose()
        print("estimated cost: $", rec_out["cost"]["usd_estimated_total"], "of cap $", a.budget, f"(delay + execution + one idle tail of {a.idle:.0f} s)", flush=True)
        Path(a.out_probe1).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out_probe1).write_text(json.dumps(rec_out, ensure_ascii=False, indent=1), encoding="utf-8")


async def run_probe(a):
    """2 and 3 jobs started at the same moment on warm workers. The workers are warmed first by concurrent warm-up pings (cheap)."""
    out = {"endpoint": a.endpoint_id, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rounds": []}
    cl, sp = make(a, reserve=0.0)
    text = CLAUSES[8][1]
    try:
        w, code = await workers(sp)
        out["workers_before"] = w
        print("workers before:", json.dumps(w), flush=True)
        t0 = time.monotonic()
        warm = await asyncio.gather(*[sp.job("tts", {"input": {"warmup": True}}, "run", timeout=900, poll_cap=POLL_CAP) for _ in range(3)])
        out["warm_wall_s"] = round(time.monotonic() - t0, 1)
        out["warm_delay_ms"] = [r[0]["delay_ms"] for r in warm]
        print("3 warm-up pings: delay ms", out["warm_delay_ms"], "wall s", out["warm_wall_s"], flush=True)
        singles = []
        for _ in range(2):
            rec, _ = await sp.clause(text, "A")
            singles.append(rec["wall_ms"] / 1000)
        single = statistics.median(singles)
        out["single_wall_s"], out["single_walls"] = round(single, 2), [round(x, 2) for x in singles]
        print("single job wall s:", out["single_walls"], flush=True)
        for n in (2, 3):
            for rnd in range(2):
                t = time.monotonic()
                recs = await asyncio.gather(*[sp.clause(text, "A") for _ in range(n)])
                walls = [r[0]["wall_ms"] / 1000 for r in recs]
                v = concurrency_verdict(single, walls, [r[0]["delay_ms"] for r in recs])
                v.update(round=rnd + 1, walls_s=[round(x, 2) for x in sorted(walls)], delays_ms=sorted(r[0]["delay_ms"] for r in recs), exec_ms=sorted(r[0]["exec_ms"] for r in recs),
                         total_s=round(time.monotonic() - t, 2))
                out["rounds"].append(v)
                print(f"{n} at once, round {rnd + 1}: walls {v['walls_s']} s | delay ms {v['delays_ms']} | exec ms {v['exec_ms']} -> {v['verdict']} (slowest = {v['slowest_over_single']}x one job)", flush=True)
    except StopBench as e:
        out["halted"] = str(e)
    finally:
        out["cost"] = sp.ledger.report()
        await cl.aclose()
        Path(a.out_probe).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out_probe).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\ncost est $", out["cost"]["usd_estimated_total"], "of cap $", out["cost"]["budget_usd"], "| halted:", out.get("halted"))
    print("Note: if the 2nd/3rd job shows a large delay (queue) the autoscaler had not started another worker yet (scaling policy), which is different from one worker serialising jobs.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint-id", required=True); ap.add_argument("--tenant", default="mme-test"); ap.add_argument("--voice", default="utkarsh")
    ap.add_argument("--budget", type=float, default=0.15); ap.add_argument("--idle", type=float, default=60.0, help="the test endpoint's idle timeout in s (spend estimate)")
    ap.add_argument("--probe", action="store_true"); ap.add_argument("--yes", action="store_true")
    ap.add_argument("--ulaw-probe", action="store_true", help="ONE request with output_format ulaw8k and no reference: is the worker behind this endpoint the v3 handler?")
    ap.add_argument("--old-style-probe", action="store_true", help="ONE request as the app sends it today (reference in the request, wav): is the worker healthy at all?")
    ap.add_argument("--allow-cold", action="store_true", help="old-style probe: send even when no worker is warm (a cold start may exceed a small cap)")
    ap.add_argument("--probe-timeout", type=float, default=240.0)
    ap.add_argument("--out-probe1", default=str(ROOT / "docs" / "evidence" / "phase0b_ulaw_probe.json"))
    ap.add_argument("--listen-dir", default=""); ap.add_argument("--phase0", default=str(ROOT / "docs" / "evidence" / "phase0_streaming_spike.json"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "evidence" / "phase0b_tts_v3.json"))
    ap.add_argument("--out-probe", default=str(ROOT / "docs" / "evidence" / "phase0b_tts_v3_concurrency.json"))
    a = ap.parse_args()
    if not a.yes:
        raise SystemExit("--yes required: chargeable")
    asyncio.run(run_old_style_probe(a) if a.old_style_probe else run_ulaw_probe(a) if a.ulaw_probe else run_probe(a) if a.probe else run_measure(a))


if __name__ == "__main__":
    main()
