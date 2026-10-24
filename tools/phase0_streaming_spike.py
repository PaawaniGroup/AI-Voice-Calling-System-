# -*- coding: utf-8 -*-
"""
tools/phase0_streaming_spike.py - Phase 0 of the streaming-pipeline work: REAL warm endpoints, CHARGEABLE (Brain + TTS only), report only.

    python tools/phase0_streaming_spike.py --budget 0.30 --yes

Measures (a) Brain stream=true on RunPod's OpenAI route: time to first token, tokens/s, inter-token gaps; (b) TTS execution time AND audio length per clause
for 30 clauses of 4/8/12/16 words -> real-time factor by clause length; (c) RunPod dispatch overhead per job (wall - delayTime - executionTime) for /run
polling vs /runsync, on TTS and on 1-token Brain jobs; (d) bytes per clause as mu-law 8 kHz. Keys come from the tenant's ai_config and are never printed.
Spend is tracked with the same ledger as the e2e benchmark and the run stops at --budget. Output: timings, lengths, counts; no audio, no keys.
"""
import argparse, asyncio, audioop, base64, json, statistics, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "tools"))
from e2e_latency_bench import Ledger, pct, StopBench  # noqa: E402
from app.ai import tts_frontend  # noqa: E402
from app.ai.clients import ModelClients, wav_to_pcm16  # noqa: E402
from app.ai.pipeline import Conversation  # noqa: E402
from app.ai.script_enforcement import inject_turn_hint  # noqa: E402
from config.settings import Settings  # noqa: E402
from measure_devanagari_app_path import load_cfg_brain  # noqa: E402
from spike_clauses import CLAUSES  # noqa: E402

PRICE = {"stt": 0.0, "brain": 0.000486, "tts": 0.000192}
BRAIN_PROMPTS = [("short", "B.Tech ke liye kaun si branch achhi hai?"), ("short", "Mujhe course ki fees janni hai."),
                 ("short", "Which branch is good for B.Tech?"), ("short", "Hostel ki facility hai kya?"),
                 ("long", "Mujhe poora admission process step by step detail mein samjhao, documents, fees, hostel, scholarship sab kuch."),
                 ("long", "Placement ke baare mein lamba jawab do, kaun si companies aati hain, package kitna hota hai.")]


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


class Spike:
    def __init__(self, cl, ledger):
        self.cl, self.ledger, self.inflight = cl, ledger, {}

    def base(self, svc):
        return self.cl.endpoint(svc).url                    # https://api.runpod.ai/v2/<id>

    async def job(self, svc, body, mode, timeout=300):
        """One RunPod job, timed. mode: run (POST /run + poll /status, as the app does) or runsync. Dispatch overhead = wall - delayTime - executionTime."""
        self.ledger.before(svc)
        cl, hdrs, base = self.cl, self.cl._auth_headers(svc), self.base(svc)
        t0 = time.perf_counter()
        self.ledger.begin(svc, t0)
        polls, t_sub, j = 0, None, None
        try:
            if mode == "runsync":
                r = await cl._http.post(base + "/runsync", json=body, headers=hdrs, timeout=timeout)
                t_sub = time.perf_counter()
            else:
                r = await cl._http.post(base + "/run", json=body, headers=hdrs, timeout=timeout)
                t_sub = time.perf_counter()
            r.raise_for_status()
            j = r.json()
            if j.get("id"):
                self.inflight[j["id"]] = (svc, base, hdrs)
            pd = 0.06
            while j.get("status") in ("IN_QUEUE", "IN_PROGRESS"):
                await asyncio.sleep(pd); pd = min(pd * 1.5, 0.4); polls += 1
                sr = await cl._http.get(f"{base}/status/{j['id']}", headers=hdrs, timeout=30)
                sr.raise_for_status(); j = sr.json()
            self.inflight.pop(j.get("id"), None)
            if j.get("status") != "COMPLETED":
                raise RuntimeError(f"job status {j.get('status')}")
        finally:
            t1 = time.perf_counter()
            d, e = (j or {}).get("delayTime"), (j or {}).get("executionTime")
            self.ledger.end(svc, t1, ((d or 0) + (e or 0)) / 1000 if d is not None else (t1 - t0))
        wall = (t1 - t0) * 1000
        return {"mode": mode, "wall_ms": round(wall, 1), "delay_ms": d, "exec_ms": e, "polls": polls,
                "submit_ms": round((t_sub - t0) * 1000, 1),
                "overhead_ms": round(wall - (d or 0) - (e or 0), 1) if d is not None and e is not None else None}, j.get("output")

    async def cancel_all(self):
        for jid, (svc, base, hdrs) in list(self.inflight.items()):
            try:
                await self.cl._http.post(f"{base}/cancel/{jid}", headers=hdrs, timeout=10)
            except Exception:
                pass

    # ---------------------------------------------------------------- (a) Brain stream
    async def brain_models(self):
        r = await self.cl._http.get(self.base("brain") + "/openai/v1/models", headers=self.cl._auth_headers("brain"), timeout=60)
        return [m.get("id") for m in (r.json().get("data") or [])] if r.status_code < 400 else []

    async def brain_stream(self, model, text, max_tokens):
        self.ledger.before("brain")
        conv = Conversation(None, "auto", devanagari_hindi=True)
        msgs = inject_turn_hint([*conv.messages, {"role": "user", "content": text}], mode="hi" if any(c.isalpha() and ord(c) < 128 for c in text) else "hi", placement="user_suffix", style="short")
        body = {"model": model, "messages": msgs, "temperature": 0.3, "max_tokens": max_tokens, "stream": True, "stream_options": {"include_usage": True}}
        t0 = time.perf_counter()
        self.ledger.begin("brain", t0)
        t_hdr = t_first = t_last = None
        stamps, usage, text_chars = [], None, 0
        try:
            async with self.cl._http.stream("POST", self.base("brain") + "/openai/v1/chat/completions", json=body,
                                            headers=self.cl._auth_headers("brain"), timeout=300) as resp:
                t_hdr = time.perf_counter()
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    ch = json.loads(data)
                    if ch.get("usage"):
                        usage = ch["usage"]
                    c = (ch.get("choices") or [{}])[0].get("delta", {}).get("content")
                    if c:
                        now = time.perf_counter()
                        t_first = t_first or now
                        t_last = now
                        stamps.append(now); text_chars += len(c)
        finally:
            t1 = time.perf_counter()
            self.ledger.end("brain", t1, t1 - t0)
        gaps = [(b - a) * 1000 for a, b in zip(stamps, stamps[1:])]
        ntok = (usage or {}).get("completion_tokens")
        return {"ttft_ms": round((t_first - t0) * 1000, 1) if t_first else None, "headers_ms": round((t_hdr - t0) * 1000, 1),
                "total_ms": round((t1 - t0) * 1000, 1), "completion_tokens": ntok, "chunks": len(stamps), "reply_chars": text_chars,
                "sse": "text/event-stream" in ctype,
                "tokens_per_s": round((ntok - 1) / (t_last - t_first), 1) if ntok and t_last and t_first and t_last > t_first else None,
                "gap_p50_ms": pct(gaps, 50), "gap_p95_ms": pct(gaps, 95), "gap_max_ms": round(max(gaps), 1) if gaps else None}

    # ---------------------------------------------------------------- (b)(d) TTS
    def tts_body(self, text):
        s, cl = self.cl.s, self.cl
        lang = tts_frontend.detect_lang(text) if s.ai_tts_normalize else "hi"
        spoken = tts_frontend.normalize(text, lang) if s.ai_tts_normalize else text
        inp = {"text": spoken, "prompt": spoken, "language": lang, "model": cl.cfg.get("models", {}).get("tts", s.ai_tts_model)}
        ref = cl._tts_reference_b64()
        if ref:
            inp["reference_audio_base64"] = ref
        return {"input": inp}

    async def tts_clause(self, text, mode):
        rec, out = await self.job("tts", self.tts_body(text), mode)
        b64 = out.get("audio") or out.get("audio_base64") if isinstance(out, dict) else None
        pcm, rate = wav_to_pcm16(base64.b64decode(b64))
        dur = len(pcm) / 2 / rate
        p8 = audioop.ratecv(pcm, 2, 1, rate, 8000, None)[0]
        rec.update(audio_s=round(dur, 3), sample_rate=rate, wav_bytes=len(base64.b64decode(b64)), ulaw8k_bytes=len(audioop.lin2ulaw(p8, 2)),
                   rtf=round((rec["exec_ms"] or 0) / 1000 / dur, 3) if dur and rec["exec_ms"] is not None else None,
                   words=len(text.split()))
        return rec


async def workers(cl, svc):
    r = await cl._http.get(f"{cl.endpoint(svc).url}/health", headers=cl._auth_headers(svc), timeout=30)
    w = (r.json() or {}).get("workers", {}) if r.status_code < 400 else {}
    return {k: w.get(k) for k in ("idle", "ready", "running", "initializing")}


async def main_async(a):
    if not a.yes:
        raise SystemExit("--yes required: chargeable")
    s = Settings()
    cfg = load_cfg_brain(s, a.tenant)
    cfg["timeouts"] = {**(cfg.get("timeouts") or {}), "tts": 400.0, "brain": 400.0}
    ledger = Ledger(PRICE, {"stt": 0.0, "brain": 60.0, "tts": 60.0}, a.budget, 0.0)
    cl = ModelClients(s, cfg)
    sp = Spike(cl, ledger)
    out = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        out["workers_before"] = {svc: await workers(cl, svc) for svc in ("brain", "tts")}
        print("workers before:", json.dumps(out["workers_before"]), flush=True)
        for svc in ("brain", "tts"):
            if not sum(int(out["workers_before"][svc].get(k) or 0) for k in ("idle", "ready", "running")):
                if a.no_warm:
                    raise SystemExit(f"{svc} is cold and --no-warm was given: nothing sent")
                ledger.cold_reserve_s = 200.0
                print(f"{svc} cold: warming (counted in the budget)", flush=True)
                body = {"input": {"prompt": "hi", "messages": [{"role": "user", "content": "hi"}], "sampling_params": {"max_tokens": 1}}} if svc == "brain" else sp.tts_body(CLAUSES[4][0])
                rec, _ = await sp.job(svc, body, "run", timeout=400)
                out.setdefault("warmup", {})[svc] = {k: rec[k] for k in ("delay_ms", "exec_ms", "wall_ms")}
                print("  warmed:", json.dumps(out["warmup"][svc]), flush=True)
                ledger.cold_reserve_s = 0.0
        # (a) Brain stream
        models = await sp.brain_models()
        out["brain_models_listed"] = len(models)
        model = cfg.get("models", {}).get("brain", s.ai_brain_model)
        if models and model not in models:
            model = models[0]
        out["brain_stream"] = []
        for kind, text in BRAIN_PROMPTS:
            r = await sp.brain_stream(model, text, 200 if kind == "long" else 120)
            r["kind"] = kind
            out["brain_stream"].append(r)
            print("brain stream:", json.dumps(r), flush=True)
        # (c) Brain 1-token dispatch: /run vs /runsync, alternating
        out["brain_dispatch"] = []
        for i in range(8):
            mode = "run" if i % 2 == 0 else "runsync"
            rec, _ = await sp.job("brain", {"input": {"prompt": "hi", "messages": [{"role": "user", "content": "hi"}], "sampling_params": {"max_tokens": 1}}}, mode)
            out["brain_dispatch"].append(rec)
        # (b)(c)(d) TTS clauses, round-robin over the lengths, /run and /runsync alternating within each length
        out["tts"] = []
        pos = {n: 0 for n in CLAUSES}
        for j in range(max(len(v) for v in CLAUSES.values())):
            for n, items in CLAUSES.items():
                if j < len(items):
                    mode = "run" if j % 2 == 0 else "runsync"
                    rec = await sp.tts_clause(items[j], mode)
                    out["tts"].append(rec)
                    print(f"tts {n:>2}w {mode:<7} exec={rec['exec_ms']} audio={rec['audio_s']}s rtf={rec['rtf']} overhead={rec['overhead_ms']} ulaw={rec['ulaw8k_bytes']}", flush=True)
    except StopBench as e:
        out["halted"] = str(e)
    except BaseException as e:
        out["halted"] = f"{type(e).__name__}"
        await sp.cancel_all()
        raise
    finally:
        out["cost"] = ledger.report()
        await cl.aclose()
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    report(out)


def report(out):
    print("\n=== PHASE 0 summary (timings, lengths, counts only) ===")
    b = out.get("brain_stream", [])
    for kind in ("short", "long"):
        g = [r for r in b if r["kind"] == kind]
        if g:
            print(f"(a) Brain stream [{kind}] n={len(g)}: TTFT p50={med([r['ttft_ms'] for r in g])} ms, headers p50={med([r['headers_ms'] for r in g])} ms, "
                  f"tokens/s p50={med([r['tokens_per_s'] for r in g])}, completion tokens={[r['completion_tokens'] for r in g]}, inter-token gap p95={med([r['gap_p95_ms'] for r in g])} ms, SSE={all(r['sse'] for r in g)}")
    t = out.get("tts", [])
    print("(b) TTS by clause length: words | n | exec ms p50 | audio s p50 | RTF p50 | RTF max | ulaw8k bytes p50")
    for n in (4, 8, 12, 16):
        g = [r for r in t if r["words"] == n]
        if g:
            print(f"    {n:>2} | {len(g)} | {med([r['exec_ms'] for r in g])} | {med([r['audio_s'] for r in g])} | {med([r['rtf'] for r in g])} | {max(r['rtf'] for r in g)} | {med([r['ulaw8k_bytes'] for r in g])}")
    allrtf = [r["rtf"] for r in t if r["rtf"]]
    if allrtf:
        print(f"    all clauses: RTF p50={pct(allrtf,50)} p95={pct(allrtf,95)}; sample rate {sorted({r['sample_rate'] for r in t})}")
    print("(c) dispatch overhead (wall - delayTime - executionTime), ms:")
    for svc, rows in (("tts", t), ("brain 1-token", out.get("brain_dispatch", []))):
        for mode in ("run", "runsync"):
            g = [r for r in rows if r["mode"] == mode and r.get("overhead_ms") is not None]
            if g:
                print(f"    {svc:<14} {mode:<8} n={len(g)} overhead p50={med([r['overhead_ms'] for r in g])} p95={pct([r['overhead_ms'] for r in g],95)} | "
                      f"delayTime p50={med([r['delay_ms'] for r in g])} | wall p50={med([r['wall_ms'] for r in g])} | polls p50={med([r['polls'] for r in g])}")
    print("cost:", json.dumps(out.get("cost", {}).get("usd_estimated_total")), "of cap", out.get("cost", {}).get("budget_usd"), "| halted:", out.get("halted"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", default="mme-test"); ap.add_argument("--budget", type=float, default=0.30)
    ap.add_argument("--yes", action="store_true"); ap.add_argument("--no-warm", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "docs" / "evidence" / "phase0_streaming_spike.json"))
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
