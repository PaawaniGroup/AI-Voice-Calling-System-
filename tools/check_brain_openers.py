"""CAPPED real Brain test of the opening rule (flag `stream_ramp`): does the Brain obey it? NOT RUN.

Creates up to 20 real Brain calls (billed), ONE per question, on a warm Brain, with the tenant's real system prompt (the Devanagari variant) plus the ramp rule block
and the per-turn `(reply_mode: hi|en)` tag, exactly as the app builds the request. No document context is given, so ANY fee, cut-off or date in a reply is ungrounded.

Privacy: the replies are written to ONE local file that must lie OUTSIDE the repository (JSON lines, mode 0600) and are never printed or logged. The screen shows only counts.
It refuses to run without --approved "<the operator's APPROVED: message id>", stops at the first 401/403, after 3 consecutive errors, and when the estimated spend
(RunPod delay + execution seconds x --price-per-s) reaches --cap-usd (default 0.05).

  RUNPOD_BRAIN_KEY=... python tools/check_brain_openers.py --reply-file /path/outside/repo/replies.jsonl --approved "<id>"

Counts printed: opener obeyed (2-4 words ending in punctuation, cut at a boundary), forbidden word used (an agree/deny/promise word in the opener), language matched
(the opener's script fits the reply mode) and whole reply script ok, and replies that state a fee, a cut-off or a date (regex counts).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, ".")

# id, language of the caller (hi = Devanagari Hindi, hg = Roman Hinglish, en = English), topic, the question (synthetic: written for this test, not a caller's words)
QUESTIONS = [
    (1, "hi", "fees", "B.Tech कंप्यूटर साइंस की सालाना फीस कितनी है?"),
    (2, "hi", "eligibility", "B.Tech में admission के लिए कितने प्रतिशत चाहिए?"),
    (3, "hi", "scholarship", "क्या scholarship मिल सकती है?"),
    (4, "hi", "hostel", "होस्टल की सुविधा है या नहीं?"),
    (5, "hi", "greeting", "नमस्ते, क्या आप मेरी आवाज़ सुन पा रहे हैं?"),
    (6, "hi", "repeat", "आपने क्या कहा, फिर से बोलिए।"),
    (7, "hg", "placements", "placement kaisa hota hai is college mein?"),
    (8, "hg", "abroad", "mujhe New Zealand mein padhai ke baare mein batao"),
    (9, "hg", "fees", "MBA ki fees kitni hai aur kab tak bharni hai?"),
    (10, "hg", "cutoff", "cutoff kitna chahiye admission ke liye?"),
    (11, "hg", "callback", "kya koi mujhe kal call kar sakta hai?"),
    (12, "hg", "garbled", "ye wo kya... course ka naam... uh"),
    (13, "hg", "date", "last date kya hai apply karne ki?"),
    (14, "hg", "hostel", "hostel mein khana milta hai kya?"),
    (15, "en", "eligibility", "What is the eligibility for the MBA programme?"),
    (16, "en", "scholarship", "Can I get a scholarship if I am from a small town?"),
    (17, "en", "abroad", "I want to study in New Zealand after graduation. What are my options?"),
    (18, "en", "callback", "Can someone call me back tomorrow?"),
    (19, "en", "greeting", "Hello, is this the admissions helpline?"),
    (20, "en", "repeat", "Sorry, could you repeat that?"),
]

_MONTHS = (r"january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|"
           r"जनवरी|फ़रवरी|फरवरी|मार्च|अप्रैल|मई|जून|जुलाई|अगस्त|सितंबर|सितम्बर|अक्टूबर|नवंबर|नवम्बर|दिसंबर|दिसम्बर")
FEE_RE = re.compile(r"(₹|\brs\.?|\binr\b|रु(?:पये|पए|पया)?)\s*\d|\d[\d,]*\s*(?:₹|rupees?|lakh|lac|लाख|हज़ार|हजार|thousand|रुपये|रुपए)", re.I)
CUTOFF_RE = re.compile(r"(cut[\s-]?off|कट[\s-]?ऑफ|कटऑफ)[^.।?!]{0,60}\d|\d+\s*(?:%|percent|प्रतिशत)", re.I)
DATE_RE = re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s*(?:{_MONTHS})\b|(?:{_MONTHS})\s*\d{{1,2}}\b|\b\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}}\b", re.I)


def _letters(s: str) -> tuple[int, int]:
    dev = sum(1 for c in s if "ऀ" <= c <= "ॿ")
    lat = sum(1 for c in s if c.isascii() and c.isalpha())
    return dev, lat


def analyze(reply: str, mode: str) -> dict:
    """0/1 flags for one reply (pure function; the reply text never leaves it)."""
    from app.ai.script_enforcement import check_reply
    from app.ai.streaming.ramp import FORBIDDEN_OPENER_WORDS
    from app.ai.streaming.segmenter import ClauseSegmenter, SegmenterConfig
    seg = ClauseSegmenter(SegmenterConfig(ramp=True, first_min_words=4, min_words=12, max_words=14))
    clauses = seg.feed(reply) + seg.flush()
    first = clauses[0] if clauses else None
    words = len(str(first).split()) if first else 0
    obeyed = bool(first) and getattr(first, "reason", None) == "punct" and 2 <= words <= 4 and str(first)[-1] in ",.।"
    toks = {t.strip(",.।!?").lower() for t in str(first or "").split()}
    forbidden = any(w.lower() in toks for w in FORBIDDEN_OPENER_WORDS)
    dev, lat = _letters(str(first or ""))
    opener_lang_ok = (dev > 0 and dev >= lat) if mode == "hi" else (dev == 0 and lat > 0)
    try:
        script_ok = not [p for p in check_reply(reply, mode)[1] if p != "no_devanagari_in_hi_mode"]     # same rule as the live clause gate
    except Exception:
        script_ok = False
    return {"obeyed": int(obeyed), "forbidden": int(forbidden), "opener_lang_ok": int(opener_lang_ok), "script_ok": int(bool(script_ok)),
            "fee": int(bool(FEE_RE.search(reply))), "cutoff": int(bool(CUTOFF_RE.search(reply))), "date": int(bool(DATE_RE.search(reply)))}


def refuse_reason(args, repo_root: pathlib.Path) -> str | None:
    if not args.approved.strip():
        return "refused: no --approved id (this test creates billed Brain calls)"
    if not 1 <= args.questions <= 20:
        return "refused: 1 to 20 questions"
    p = pathlib.Path(args.reply_file).resolve()
    if repo_root.resolve() in p.parents or p == repo_root.resolve():
        return "refused: the reply file must be OUTSIDE the repository"
    return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reply-file", required=True)
    ap.add_argument("--tenant", default="mme-test")
    ap.add_argument("--mongo", default="mongodb://127.0.0.1:27117")
    ap.add_argument("--key-env", default="RUNPOD_BRAIN_KEY")
    ap.add_argument("--questions", type=int, default=20)
    ap.add_argument("--max-tokens", type=int, default=60)
    ap.add_argument("--cap-usd", type=float, default=0.05)
    ap.add_argument("--price-per-s", type=float, default=0.0006, help="assumed GPU price per second (UNVERIFIED; the default is deliberately high)")
    ap.add_argument("--without-rule", action="store_true", help="control: the same questions without the opening rule")
    ap.add_argument("--approved", default="")
    a = ap.parse_args()
    why = refuse_reason(a, pathlib.Path(__file__).resolve().parent.parent)
    if why:
        print(why)
        return 2
    from pymongo import AsyncMongoClient
    from app.ai import turn_trace
    from app.ai.clients import AiError, ModelClients
    from app.ai.pipeline import system_prompt
    from app.ai.script_enforcement import build_reply_mode_hint, reply_mode
    from app.ai.streaming import ramp
    from config.settings import Settings
    doc = await AsyncMongoClient(a.mongo, serverSelectionTimeoutMS=3000)["mme_telephony"]["ai_config"].find_one({"tenant_id": a.tenant}) or {}
    cfg = {k: doc.get(k) for k in ("brain_url", "models", "timeouts", "paths", "runpod_mode")}
    cfg.update(key_refs={"brain": a.key_env}, max_tokens=a.max_tokens)
    cl = ModelClients(Settings(_env_file=None), cfg)
    sysmsg = system_prompt(devanagari_hindi=True)
    conv = type("C", (), {"messages": [{"role": "system", "content": sysmsg}]})()
    if not a.without_rule:
        ramp.apply_to_conversation(conv)
    out = pathlib.Path(a.reply_file)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    totals = {k: 0 for k in ("obeyed", "forbidden", "opener_lang_ok", "script_ok", "fee", "cutoff", "date")}
    by_lang: dict = {}
    calls = errors = consecutive = 0
    spent_s = 0.0
    stop = None
    try:
        for qid, lang, _topic, q in QUESTIONS[: a.questions]:
            mode = reply_mode(q)
            msgs = [conv.messages[0], {"role": "user", "content": f"{q}\n\n{build_reply_mode_hint(mode)}"}]
            tr = turn_trace.TurnTrace(t_speech_end=0.0)
            turn_trace.start(tr)
            try:
                res = await cl.chat(msgs)
                reply = (res.get("text") or "").strip()
                consecutive = 0
            except AiError as e:
                errors += 1
                consecutive += 1
                if getattr(e, "status_code", None) in (401, 403):
                    stop = "stopped at HTTP %s" % e.status_code
                    break
                if consecutive >= 3:
                    stop = "stopped after 3 consecutive errors"
                    break
                continue
            finally:
                for j in tr.jobs:
                    spent_s += ((j.get("delay_ms") or 0) + (j.get("exec_ms") or 0)) / 1000
            calls += 1
            os.write(fd, (json.dumps({"id": qid, "mode": mode, "reply": reply}, ensure_ascii=False) + "\n").encode("utf-8"))     # the ONLY place a reply is written
            flags = analyze(reply, mode)
            for k, v in flags.items():
                totals[k] += v
                by_lang.setdefault(lang, {k2: 0 for k2 in totals})[k] += v
            if spent_s * a.price_per_s >= a.cap_usd:
                stop = "stopped at the cost cap"
                break
    finally:
        os.close(fd)
        await cl.aclose()
    print(json.dumps({"calls": calls, "errors": errors, "stopped": stop, "estimated_gpu_seconds": round(spent_s, 1),
                      "estimated_cost_usd_at_price_per_s": round(spent_s * a.price_per_s, 4), "rule": not a.without_rule,
                      "counts_over_calls": totals, "by_language": by_lang}))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
