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
from measure_roman_hindi import load_utterances  # noqa: E402

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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("warm", "run"))
    ap.add_argument("--tenant", default="mme-test"); ap.add_argument("--budget", type=float, default=0.50)
    ap.add_argument("--idle", type=float, default=60.0, help="Brain idle timeout in s (assumed; used only for the spend estimate)")
    ap.add_argument("--n", type=int, default=100); ap.add_argument("--yes", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "docs" / "evidence" / "devanagari_app_path.json"))
    a = ap.parse_args()
    logging.getLogger().setLevel(logging.WARNING)
    asyncio.run(warm(a) if a.cmd == "warm" else run(a))


if __name__ == "__main__":
    main()
