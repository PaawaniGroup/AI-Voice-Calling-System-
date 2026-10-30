# -*- coding: utf-8 -*-
"""
tools/make_condition_inputs.py - TEXT ONLY (no audio is generated here): for every listening condition, the exact text and language tag to send to the TTS.

  python tools/make_condition_inputs.py --sentences lexicon/carrier_sentences.jsonl --out lexicon/listening_inputs

Writes <out>/<condition>.jsonl ({"id","text","lang"} or {"id","segments":[{"lang","text"}]}) and <out>/listening_texts.jsonl (what the listener page shows).
Run the Colab notebook once per condition with the SAME fixed seed (e.g. 1234), same reference clip, same sampling settings, and save the wavs as <id>.wav in one folder per condition.

Conditions (Approaches B, C, D of the task; A needs the Brain and is separate):
  baseline_hi       today's behaviour: front-end only, Roman Hindi words as they are, language tag per tts_frontend.detect_lang (-> "hi")
  baseline_en       D: the same text, but language tag "en" for the Roman-only sentences
  lex_gated         B: lexicon, sentence gate on (min_known 0.9): only sentences whose Latin words are almost all known are converted
  lex_nogate        B': "convert every Hindi-looking token" (gate off): the half-conversion risk, to be compared with lex_gated by ear
  lex_respell       C2: lex_gated + English words of the respell table in Devanagari
  lex_segments      C3: lex_gated text split per script, Devanagari runs tagged "hi" and Latin runs tagged "en" (the TTS side must synthesize and join the segments)
C1 (English words left in Latin with the Hindi tag) is lex_gated itself.
Until native speakers verified the lexicon, every conversion here is a PROPOSAL. Listeners judge the result, nobody has to trust the table.
"""
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.ai import tts_frontend  # noqa: E402
from mme_lexicon import Lexicon, split_by_script  # noqa: E402

DEV_WORD = {"aapko": "आपको", "hoon": "हूँ", "han": "हाँ", "sakta": "सकता", "chahiye": "चाहिए", "baare": "बारे", "main-I": "मैं", "main-in": "में", "aapka": "आपका", "hota": "होता",
            "hai": "है", "deta": "देता", "course name": "course name", "to": "तो", "ki": "कि", "hi": "ही", "us/is": "इस", "do": "दो"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sentences", default=str(ROOT / "lexicon" / "carrier_sentences.jsonl"), help="jsonl with id,text (optional: word = target word key, as in carrier_sentences.jsonl)")
    ap.add_argument("--out", default=str(ROOT / "lexicon" / "listening_inputs"))
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.sentences, encoding="utf-8") if l.strip()]
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    gated, nogate, respell = Lexicon(), Lexicon(min_known=0.0), Lexicon(english="devanagari")
    files = {k: [] for k in ("baseline_hi", "baseline_en", "lex_gated", "lex_nogate", "lex_respell", "lex_segments")}
    shown = []
    for r in rows:
        t = tts_frontend.normalize(r["text"])
        base_lang = tts_frontend.detect_lang(r["text"])
        files["baseline_hi"].append({"id": r["id"], "text": t, "lang": base_lang})
        files["baseline_en"].append({"id": r["id"], "text": t, "lang": "en" if base_lang == "hi" and not any("ऀ" <= ch <= "ॿ" for ch in t) else base_lang})
        g = gated.convert(t).text
        files["lex_gated"].append({"id": r["id"], "text": g, "lang": "hi" if g != t or base_lang == "hi" else base_lang})
        files["lex_nogate"].append({"id": r["id"], "text": nogate.convert(t).text, "lang": base_lang})
        files["lex_respell"].append({"id": r["id"], "text": respell.convert(t).text, "lang": base_lang})
        files["lex_segments"].append({"id": r["id"], "segments": [{"lang": lg, "text": tx} for lg, tx in split_by_script(g)]})
        word = r.get("word", "")
        # the listener sees ONE neutral text: the converted sentence (Devanagari + English words), never a condition-specific one
        shown.append({"id": r["id"], "display": nogate.convert(t).text, "words": [DEV_WORD[word]] if word in DEV_WORD else []})
    for name, lst in files.items():
        (out / f"{name}.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lst) + "\n", encoding="utf-8")
    (out / "listening_texts.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in shown) + "\n", encoding="utf-8")
    differing = {k: sum(1 for x, b in zip(v, files["baseline_hi"]) if (x.get("text", x.get("segments")), x.get("lang")) != (b["text"], b["lang"])) for k, v in files.items()}
    print(f"{len(rows)} sentences x {len(files)} conditions -> {out}\nsentences that differ from baseline_hi: {differing}")


if __name__ == "__main__":
    main()
