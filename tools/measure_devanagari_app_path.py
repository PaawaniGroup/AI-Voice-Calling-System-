# -*- coding: utf-8 -*-
"""
tools/measure_devanagari_app_path.py - Devanagari flow measurement through the APP's own reply path (Conversation.brain_reply), CHARGEABLE (Brain only).

    python tools/measure_devanagari_app_path.py warm                  # wake the Brain, print its delay (queue/cold start) and execution time
    python tools/measure_devanagari_app_path.py run --budget 0.50 --yes   # 100 utterances x {flag off, flag on}, sequential

Keys come from the tenant's ai_config (never printed). Every Brain call goes through scripts/e2e_latency_bench.BenchClients, whose spend ledger
(delay + exec + idle gaps + one idle tail) halts the run at --budget. Output (JSON + summary) holds lengths, counts, problem KINDS and timings only:
no caller text, no reply text, no words.
"""
import argparse, asyncio, json, logging, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "tools"))
from e2e_latency_bench import BenchClients, Ledger, load_cfg, pct  # noqa: E402
from app.ai.pipeline import Conversation  # noqa: E402
from app.ai.script_enforcement import check_reply, problem_kinds  # noqa: E402
from config.settings import Settings  # noqa: E402
from measure_roman_hindi import load_utterances, stats  # noqa: E402

PRICE = {"stt": 0.0, "brain": 0.000486, "tts": 0.0}


def make(args, reserve):
    s = Settings()
    cfg = load_cfg_brain(s, args.tenant)
    ledger = Ledger(PRICE, {"stt": 0.0, "brain": args.idle, "tts": 0.0}, args.budget, reserve)
    return s, cfg, BenchClients(s, cfg, ledger, start_zero=False)


def load_cfg_brain(s, tenant):
    from pymongo import MongoClient
    c = MongoClient(s.mongo_uri.get_secret_value(), serverSelectionTimeoutMS=3000)
    try:
        doc = c[s.mongo_db]["ai_config"].find_one({"tenant_id": tenant})
    finally:
        c.close()
    if not doc:
        raise SystemExit(f"no ai_config for tenant {tenant!r}")
    cfg = {k: v for k, v in doc.items() if k != "_id"}
    cfg["timeouts"] = {**(cfg.get("timeouts") or {}), "brain": 400.0}     # a cold start must not abort silently
    return cfg


async def warm(args):
    s, cfg, cl = make(args, reserve=200.0)
    t0 = time.monotonic()
    try:
        cl.cfg["max_tokens"] = 1
        await cl.chat([{"role": "user", "content": "hi"}])
    finally:
        await cl.aclose()
    rec = cl.calls[-1]
    print(json.dumps({"brain_delay_ms": rec["delay_ms"], "brain_exec_ms": rec["exec_ms"], "client_ms": rec["client_ms"], "error": rec["error"],
                      "est_spend_usd": round(cl.ledger.spent(), 5), "wall_s": round(time.monotonic() - t0, 1)}))


async def run(args):
    if not args.yes:
        raise SystemExit("--yes required: chargeable")
    s, cfg, cl = make(args, reserve=0.0)
    utts = load_utterances(args.n)
    rows = []
    try:
        for flag in (False, True):
            for u in utts:
                conv = Conversation(cl, "auto", devanagari_hindi=flag, counsellor_gender=s.ai_counsellor_gender, counsellor_name=s.ai_counsellor_name)
                n0 = len(cl.calls)
                try:
                    r = await conv.brain_reply(u["text"])
                    err = None
                except Exception as e:                                   # counted, never replaced by invented text
                    r, err = {"text": "", "mode": None, "ms": None, "report": None}, type(e).__name__
                text = r["text"]
                final_ok, final_problems = check_reply(text, r["mode"] or "hi") if text else (None, [])
                rep = r["report"] or {}
                rows.append({"flag": flag, "id": u["id"], "style": u["style"], "error": err, "mode": r["mode"], "ms": r["ms"], "reply_chars": len(text),
                             "brain_calls": len(cl.calls) - n0, "first_ok": rep.get("first_ok"), "retried": rep.get("retried", False),
                             "retry_ok": rep.get("retry_ok"), "retry_ms": rep.get("retry_ms"), "first_kinds": rep.get("kinds"),
                             "final_ok": final_ok, "final_kinds": problem_kinds(final_problems),
                             "brain_delay_ms": [c["delay_ms"] for c in cl.calls[n0:]]})
                if cl.halted:
                    raise SystemExit(f"halted: {cl.halted}")
    finally:
        await cl.aclose()
        out = {"rows": rows, "cost": cl.ledger.report(), "halted": cl.halted}
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    summarize(rows, cl.ledger.report())


def has_roman(kinds):
    return bool((kinds or {}).get("roman_hindi_word"))


def summarize(rows, cost):
    print("\n=== summary (counts, lengths, kinds only) ===")
    for flag in (False, True):
        rs = [r for r in rows if r["flag"] == flag and not r["error"]]
        err = sum(1 for r in rows if r["flag"] == flag and r["error"])
        # the legacy (flag off) path has no check of its own: classify its replies with the same check, hi/en by caller style
        fin = [r for r in rs if r["style"] != "english"]
        bad_final = [r for r in rs if has_roman(r["final_kinds"])]
        print(f"flag {'ON ' if flag else 'OFF'}: replies={len(rs)} errors={err} | replies with Roman-letter Hindi (final, spoken text): {len(bad_final)}/{len(rs)} = {100*len(bad_final)/max(1,len(rs)):.1f}%")
        for st in ("roman", "devanagari", "mixed", "english"):
            sr = [r for r in rs if r["style"] == st]
            print(f"   {st:<10} n={len(sr)} roman_final={sum(has_roman(r['final_kinds']) for r in sr)} final_not_ok={sum(1 for r in sr if r['final_ok'] is False)}")
        ms = [r["ms"] for r in rs if r["ms"] is not None]
        print(f"   latency ms: p50={pct(ms,50)} p95={pct(ms,95)} max={max(ms) if ms else None} mean={round(sum(ms)/len(ms),1) if ms else None}")
        if flag:
            first_bad = [r for r in rs if r["first_ok"] is False]
            first_roman = [r for r in rs if has_roman(r["first_kinds"])]
            retried = [r for r in rs if r["retried"]]
            fixed = [r for r in retried if r["retry_ok"]]
            print(f"   first reply Roman-Hindi (before retry): {len(first_roman)}/{len(rs)}; first reply failed any check: {len(first_bad)}/{len(rs)}")
            print(f"   needed retry: {len(retried)}/{len(rs)} = {100*len(retried)/max(1,len(rs)):.1f}%; retry fixed it: {len(fixed)}; retry failed/timed out: {len(retried)-len(fixed)}")
            rm = [r["retry_ms"] for r in retried if r["retry_ms"] is not None]
            print(f"   retry time ms: p50={pct(rm,50)} max={max(rm) if rm else None}; reply latency of retried turns p50={pct([r['ms'] for r in retried if r['ms']],50)}, of clean turns p50={pct([r['ms'] for r in rs if not r['retried'] and r['ms']],50)}")
    worst = sorted([r for r in rows if r["flag"] and not r["error"] and r["final_ok"] is False],
                   key=lambda r: -sum((r["final_kinds"] or {}).values()))[:10]
    print("10 worst final replies with the flag on (style, reply chars, final problem kinds/counts, retried):")
    for r in worst:
        print(f"   {r['style']:<10} chars={r['reply_chars']:<4} kinds={r['final_kinds']} retried={r['retried']} retry_ok={r['retry_ok']}")
    print("estimated spend:", cost["usd_estimated_total"], "of cap", cost["budget_usd"])


# ------------------------------------------------------------------------------------------------ suite: en-off row, multi-turn, adversarial
def jl(name):
    return [json.loads(l) for l in open(ROOT / "lexicon" / name, encoding="utf-8") if l.strip()]


def roman_tokens(text, lx):
    """Roman-letter Hindi tokens in a reply, by the lexicon (independent of the reply mode)."""
    st = stats([("x", text)], lx).get("all") if text else None
    return st["roman_tokens"] if st else 0


def conv_for(cl, s, flag):
    return Conversation(cl, "auto", devanagari_hindi=flag, counsellor_gender=s.ai_counsellor_gender, counsellor_name=s.ai_counsellor_name)


def row_of(r, text, mode, lx, **extra):
    rep = r["report"] or {}
    ok, probs = check_reply(text, mode) if text else (None, [])
    return {"mode": mode, "ms": r["ms"], "reply_chars": len(text), "roman_tokens": roman_tokens(text, lx), "check_ok": ok, "kinds": problem_kinds(probs),
            "first_ok": rep.get("first_ok"), "first_kinds": rep.get("kinds"), "retried": rep.get("retried", False), "retry_ok": rep.get("retry_ok"),
            "retry_ms": rep.get("retry_ms"), **extra}


async def brain_workers(cl):
    ep = cl.endpoint("brain")
    r = await cl._http.get(f"{ep.url}/health", headers=cl._auth_headers("brain"), timeout=30)
    j = r.json() if r.status_code < 400 else {}
    w = j.get("workers", {}) if isinstance(j, dict) else {}
    return {k: w.get(k) for k in ("idle", "ready", "running", "initializing")}


async def suite(args):
    from mme_lexicon import Lexicon
    if not args.yes:
        raise SystemExit("--yes required: chargeable")
    lx = Lexicon()
    out = {"phases": {}}
    path = Path(args.out_suite)

    def save():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    # 0. warm (health is free; a 1-token ping only if no worker is up). Its cost is reported separately and is NOT charged to the phase caps.
    if path.exists() and args.phases != "all":                        # keep the phases already measured BEFORE anything is saved
        prev = json.loads(path.read_text(encoding="utf-8"))
        out["phases"] = prev.get("phases", {})
    args.budget = args.warm_budget
    s, cfg, cl = make(args, reserve=200.0)
    try:
        w0 = await brain_workers(cl)
        up = sum(int(w0.get(k) or 0) for k in ("idle", "ready", "running"))
        if not up and args.no_warm:
            raise SystemExit(f"Brain is cold (workers {w0}) and --no-warm was given: nothing was sent, nothing was spent")
        if not up:
            cl.cfg["max_tokens"] = 1
            await cl.chat([{"role": "user", "content": "hi"}])
            c = cl.calls[-1]
            out["warmup"] = {"was_cold": True, "brain_delay_ms": c["delay_ms"], "brain_exec_ms": c["exec_ms"], "est_spend_usd": round(cl.ledger.spent(), 5)}
        else:
            out["warmup"] = {"was_cold": False, "workers_before": w0}
    finally:
        await cl.aclose()
    print("warmup:", json.dumps(out["warmup"]), flush=True)
    save()

    async def phase(name, budget, body):
        args.budget = budget
        s, cfg, cl = make(args, reserve=0.0)
        rows, halted = [], None
        try:
            rows = await body(cl, s)
        except SystemExit as e:
            halted = str(e)
        finally:
            await cl.aclose()
        halted = halted or cl.halted
        out["phases"][name] = {"rows": rows, "cost": cl.ledger.report(), "halted": halted}
        save()
        print(f"phase {name}: rows={len(rows)} est_spend=${cl.ledger.spent():.4f} (cap ${budget}) halted={halted}", flush=True)

    async def one(cl, conv, text, **extra):
        try:
            r = await conv.brain_reply(text)
        except Exception as e:                                       # counted, never replaced by invented text
            return None, {"error": type(e).__name__, **extra}
        row = row_of(r, r["text"], r["mode"] or "hi", lx, **extra)
        if cl.halted:
            raise SystemExit(f"halted: {cl.halted}")
        return r, row

    async def en_off(cl, s):                                         # item 1a: flag OFF, English callers, judged with the en-mode check
        rows = []
        for u in [u for u in load_utterances(100) if u["style"] == "english"]:
            r, row = await one(cl, conv_for(cl, s, False), u["text"], id=u["id"])
            row["mode"] = "en"
            row["check_ok_en"] = check_reply(r["text"], "en")[0] if r else None
            rows.append(row)
        return rows

    async def multiturn(cl, s):                                      # item 1b: flag ON, 20 conversations x 10 turns, history kept (no tag in it)
        rows = []
        for c in jl("multiturn_conversations.jsonl"):
            conv = conv_for(cl, s, True)
            for n, t in enumerate(c["turns"], 1):
                r, row = await one(cl, conv, t["text"], cid=c["id"], turn=n, style=t["style"])
                rows.append(row)
                if r:
                    conv.messages.append({"role": "user", "content": t["text"]})
                    conv.messages.append({"role": "assistant", "content": r["text"]})
                    conv._trim()
        return rows

    async def adversarial(cl, s):                                    # item 2: flag ON; context/tool text sits before the caller turn as a system message
        rows = []
        for p in jl("adversarial_prompts.jsonl"):
            conv = conv_for(cl, s, True)
            if p.get("context"):
                conv.messages.append({"role": "system", "content": p["context"]})
            r, row = await one(cl, conv, p["text"], id=p["id"], kind=p["kind"])
            rows.append(row)
        return rows

    todo = args.phases.split(",") if args.phases != "all" else ["en_off", "multiturn", "adversarial"]
    for name, cap, body in (("en_off", args.cap_en_off, en_off), ("multiturn", args.cap_multiturn, multiturn), ("adversarial", args.cap_adv, adversarial)):
        if name in todo:
            await phase(name, cap, body)
    summarize_suite(out)


def summarize_suite(out):
    print("\n=== suite summary (counts, lengths, kinds only) ===")
    P = out["phases"]
    rs = [r for r in P.get("en_off", {}).get("rows", []) if not r.get("error")]
    if rs:
        ms = [r["ms"] for r in rs if r["ms"]]
        print(f"[1a] flag OFF, English callers, en-mode check: replies={len(rs)} | contain Devanagari (en check fails)={sum(1 for r in rs if r['check_ok_en'] is False)} | "
              f"contain Roman-letter Hindi (lexicon)={sum(1 for r in rs if r['roman_tokens'])} | latency p50={pct(ms,50)}")
    rows = [r for r in P.get("multiturn", {}).get("rows", []) if not r.get("error")]
    if rows:
        print(f"[1b] multi-turn, flag ON: turns run={len(rows)} errors={sum(1 for r in P['multiturn']['rows'] if r.get('error'))} halted={P['multiturn']['halted']}")
        print("   turn | replies | Roman-Hindi (final) | retried | check failed")
        for n in range(1, 11):
            t = [r for r in rows if r["turn"] == n]
            if t:
                print(f"   {n:>4} | {len(t):>7} | {sum(1 for r in t if r['roman_tokens']):>19} | {sum(1 for r in t if r['retried']):>7} | {sum(1 for r in t if r['check_ok'] is False):>12}")
        print(f"   overall Roman-Hindi share: {sum(1 for r in rows if r['roman_tokens'])}/{len(rows)}; retries: {sum(1 for r in rows if r['retried'])}; "
              f"around the switch (turn 5 vs 6, wrong-language replies): {sum(1 for r in rows if r['turn'] in (5, 6) and r['check_ok'] is False)}")
    ad = [r for r in P.get("adversarial", {}).get("rows", []) if not r.get("error")]
    if ad:
        rt = [r for r in ad if r["retried"]]
        rm = [r["retry_ms"] for r in rt if r["retry_ms"] is not None]
        print(f"[2] adversarial, flag ON: prompts run={len(ad)} | first reply failed check={sum(1 for r in ad if r['first_ok'] is False)} | retry fired={len(rt)}/{len(ad)} | "
              f"retry fixed={sum(1 for r in rt if r['retry_ok'])} | retry failed or timed out (spoken as is)={sum(1 for r in rt if not r['retry_ok'])} | "
              f"retry time ms p50={pct(rm,50)} max={max(rm) if rm else None} | final still failing={sum(1 for r in ad if r['check_ok'] is False)}")
        for k in ("long_reply", "tool_result_english", "mixed_script", "instruction_conflict"):
            g = [r for r in ad if r["kind"] == k]
            print(f"   {k:<22} n={len(g)} retried={sum(1 for r in g if r['retried'])} final_roman={sum(1 for r in g if r['roman_tokens'])} final_check_failed={sum(1 for r in g if r['check_ok'] is False)}")
        for r in sorted(rt, key=lambda r: -(r["retry_ms"] or 0))[:10]:
            print(f"   retried: {r['kind']:<22} chars={r['reply_chars']:<4} first_kinds={r['first_kinds']} retry_ok={r['retry_ok']} retry_ms={r['retry_ms']} final_kinds={r['kinds']}")
    spend = sum(P[k]["cost"]["usd_estimated_total"] for k in P)
    print(f"estimated spend: phases ${round(spend,4)} (+ warm-up {out['warmup'].get('est_spend_usd', 0)})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("warm", "run", "suite"))
    ap.add_argument("--phases", default="all", help="suite: all, or a comma list of en_off,multiturn,adversarial (earlier results in the output file are kept)")
    ap.add_argument("--no-warm", action="store_true", help="suite: abort (free) if the Brain has no worker up instead of paying for a cold start")
    ap.add_argument("--warm-budget", type=float, default=0.15); ap.add_argument("--cap-en-off", type=float, default=0.05)
    ap.add_argument("--cap-multiturn", type=float, default=0.20); ap.add_argument("--cap-adv", type=float, default=0.15)
    ap.add_argument("--out-suite", default=str(ROOT / "docs" / "evidence" / "devanagari_suite.json"))
    ap.add_argument("--tenant", default="mme-test"); ap.add_argument("--budget", type=float, default=0.50)
    ap.add_argument("--idle", type=float, default=60.0, help="Brain idle timeout in s (assumed; used only for the spend estimate)")
    ap.add_argument("--n", type=int, default=100); ap.add_argument("--yes", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "docs" / "evidence" / "devanagari_app_path.json"))
    a = ap.parse_args()
    logging.getLogger().setLevel(logging.WARNING)
    asyncio.run({"warm": warm, "run": run, "suite": suite}[a.cmd](a))


if __name__ == "__main__":
    main()
