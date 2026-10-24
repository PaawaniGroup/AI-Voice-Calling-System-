# -*- coding: utf-8 -*-
"""
make_ab_page.py - blind A/B listening test between two TTS systems (e.g. Chatterbox vs Sarvam), for native-speaker listeners.

  python make_ab_page.py --a /path/to/chatterbox_wavs --b /path/to/sarvam_wavs --name-a Chatterbox --name-b Sarvam --out ab_test

Both folders must contain one wav per sentence. Files are matched by their LEADING NUMBER (01_hindi_short_x.wav in folder A matches 01.wav or 01_sarvam.wav in folder B); if there is no leading number the full name must match. Optional --texts file.jsonl (id,text) shows the sentence on the page.
Writes  <out>/index.html  (open in a browser; send the whole <out> folder to listeners; no server needed)  and  <out>_KEY.json NEXT TO the folder (keep it private: it says which clip was which system).
Each listener picks the better clip per sentence and clicks "Copy my answers"; they send you the text. tally_ab.py counts the votes.
The order of A/B is randomised per sentence and per listener-independent (fixed by --seed), so the listener cannot tell which system is which.
"""
import argparse, html, json, os, random, re, shutil
from pathlib import Path

PAGE = """<!doctype html><html lang="en"><meta charset="utf-8"><title>Which voice is better?</title>
<style>body{font-family:sans-serif;max-width:860px;margin:24px auto;padding:0 12px}.item{border:1px solid #ccc;border-radius:8px;padding:12px;margin:14px 0}
.clips{display:flex;gap:18px;flex-wrap:wrap}.clip{flex:1;min-width:300px}.t{color:#333;margin:6px 0 10px;font-size:18px}label{margin-right:14px}</style>
<h2>Which voice sounds better?</h2>
<p>Listen to Clip 1 and Clip 2 for each sentence (as often as you like). Choose the one that is <b>clearer, more natural, and says the right words</b>. If you cannot decide, choose "same".
Do not worry about the speaker's voice itself: judge pronunciation, clarity, rhythm and whether anything is wrong, missing or repeated.</p>
<p>Your name or ID: <input id="who" size="24"> &nbsp; Is Hindi your first language? <label><input type="radio" name="native" value="yes"> yes</label><label><input type="radio" name="native" value="no"> no</label></p>
%ITEMS%
<p><button onclick="done()" style="font-size:18px;padding:8px 18px">Copy my answers</button> <span id="msg"></span></p>
<textarea id="out" rows="8" style="width:100%" placeholder="your answers appear here: send them to the person who gave you this page"></textarea>
<script>
const IDS=%IDS%;
function done(){const r={who:document.getElementById('who').value,native:(document.querySelector('input[name=native]:checked')||{}).value||'',choices:{}};let miss=0;
for(const id of IDS){const c=document.querySelector('input[name=c_'+id+']:checked');r.choices[id]=c?c.value:'';if(!c)miss++;}
const s=JSON.stringify(r);document.getElementById('out').value=s;try{navigator.clipboard.writeText(s);}catch(e){}
document.getElementById('msg').textContent=miss?('copied, but '+miss+' sentence(s) have no answer yet'):'copied. Please send the text in the box.';}
</script></html>"""

ITEM = """<div class="item"><div class="t">%N%. %TEXT%</div><div class="clips">
<div class="clip">Clip 1<br><audio controls preload="none" src="%S1%"></audio></div><div class="clip">Clip 2<br><audio controls preload="none" src="%S2%"></audio></div></div>
<p><label><input type="radio" name="c_%ID%" value="1"> Clip 1 is better</label><label><input type="radio" name="c_%ID%" value="2"> Clip 2 is better</label><label><input type="radio" name="c_%ID%" value="same"> same</label></p></div>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", required=True); ap.add_argument("--b", required=True)
    ap.add_argument("--name-a", default="A"); ap.add_argument("--name-b", default="B")
    ap.add_argument("--out", default="ab_test"); ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--texts", help="JSONL with id,text (optional)")
    a = ap.parse_args()
    A, B, out = Path(a.a), Path(a.b), Path(a.out)
    def keyed(folder):
        d = {}
        for p in sorted(folder.glob("*.wav")):
            m = re.match(r"\d+", p.name)
            d[m.group(0) if m else p.name] = p
        return d
    KA, KB = keyed(A), keyed(B)
    ids = sorted(k for k in KA if k in KB)
    if not ids:
        raise SystemExit("no sentences are common to both folders (match is by leading number, else by full file name)")
    texts = {}
    if a.texts:
        for l in open(a.texts, encoding="utf-8"):
            r = json.loads(l); texts[r["id"]] = r["text"]
    (out / "audio").mkdir(parents=True, exist_ok=True)
    rnd, key, items = random.Random(a.seed), {}, []
    for n, sid in enumerate(ids, 1):
        flip = rnd.random() < 0.5
        first, second = ((a.name_b, B), (a.name_a, A)) if flip else ((a.name_a, A), (a.name_b, B))
        f1, f2 = f"audio/{sid}_1.wav", f"audio/{sid}_2.wav"
        src = {a.name_a: KA[sid], a.name_b: KB[sid]}; shutil.copy(src[first[0]], out / f1); shutil.copy(src[second[0]], out / f2)
        key[sid] = {"1": first[0], "2": second[0]}
        txt = texts.get(sid) or next((t for k, t in texts.items() if sid.startswith(k)), "")
        items.append(ITEM.replace("%N%", str(n)).replace("%TEXT%", html.escape(txt)).replace("%S1%", f1).replace("%S2%", f2).replace("%ID%", sid))
    (out / "index.html").write_text(PAGE.replace("%ITEMS%", "\n".join(items)).replace("%IDS%", json.dumps(ids)), encoding="utf-8")
    keyfile = out.parent / (out.name + "_KEY.json")
    json.dump({"systems": [a.name_a, a.name_b], "key": key}, open(keyfile, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(f"{len(ids)} sentences -> {out}/index.html  (send listeners the {out} folder; keep {keyfile} private)")


if __name__ == "__main__":
    main()
