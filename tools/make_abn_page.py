# -*- coding: utf-8 -*-
"""
tools/make_abn_page.py - blind listening test for N conditions (extends make_ab_page.py: 2 systems) with per-word error marks, for native Hindi listeners.

  python tools/make_abn_page.py --cond baseline=wavs/baseline --cond lexicon=wavs/lexicon --cond devanagari_en=wavs/devanagari_en --baseline baseline \
         --texts lexicon/listening_texts.jsonl --out listening_pack --listeners 3 --seed 7

Each --cond folder has one wav per sentence; files are matched by LEADING NUMBER (01_xxx.wav) like make_ab_page.py (else the full stem).
--texts is JSONL: {"id": "01", "display": "text shown to the listener", "words": ["aapko", "hoon"]}. "display" should be ONE neutral text (not the per-condition TTS input:
it would reveal the condition). "words" are the target words that get a "this word sounded wrong" box under each clip.
--design baseline   every other condition against --baseline (default; N-1 pairs per sentence)    --design all-pairs   every pair of conditions.

Output (zip the folder and send it, no server needed):
  <out>/listener_01.html ... listener_K.html     one page per listener: own item order, own left/right randomisation (both fixed by --seed)
  <out>/audio/<opaque>.wav                       clips are renamed to opaque hashes so file names do not reveal the condition
  <out>_KEY.json                                 NEXT TO the folder, PRIVATE: which clip is which condition and which side each condition was on
Each listener clicks "Copy my answers" or "Download answers" and sends the text/file back; tools/tally_abn.py counts.
"""
import argparse, hashlib, html, itertools, json, random, re, shutil
from pathlib import Path

PAGE = """<!doctype html><html lang="hi"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Listening test</title>
<style>body{font-family:sans-serif;max-width:900px;margin:20px auto;padding:0 12px}.item{border:1px solid #bbb;border-radius:8px;padding:12px;margin:14px 0}
.clips{display:flex;gap:16px;flex-wrap:wrap}.clip{flex:1;min-width:300px}.t{font-size:20px;margin:4px 0 10px}label{margin-right:12px;white-space:nowrap}.w{display:block;margin:2px 0}
.pref{margin-top:8px;padding-top:8px;border-top:1px dashed #ccc}small{color:#555}</style>
<h2>Listening test / सुनकर बताइए</h2>
<p>For each sentence you hear two clips of the same sentence. Please say <b>which one pronounces the Hindi words correctly and naturally</b>. Ignore the speaker's voice itself and the audio quality.
Under each clip, tick every listed word that sounds <b>wrong</b> (for example read with English sounds). If another word is wrong, tick "another word is wrong". If you cannot decide, choose "same".</p>
<p>हर वाक्य के दो clip सुनिए और बताइए कि किसमें हिंदी शब्दों का उच्चारण सही और स्वाभाविक है। आवाज़ या quality पर ध्यान न दें। जिस शब्द का उच्चारण ग़लत लगे उसे नीचे टिक कीजिए।</p>
<p>Your name or ID: <input id="who" size="22"> &nbsp; Hindi is my first language: <label><input type="radio" name="native" value="yes"> yes</label><label><input type="radio" name="native" value="no"> no</label></p>
%ITEMS%
<p><button onclick="done(false)" style="font-size:18px;padding:8px 16px">Copy my answers</button> <button onclick="done(true)" style="font-size:18px;padding:8px 16px">Download answers (file)</button> <span id="msg"></span></p>
<textarea id="out" rows="6" style="width:100%" placeholder="your answers appear here: send them to the person who gave you this page"></textarea>
<script>
const LISTENER=%LISTENER%, ITEMS=%ITEMS_JSON%;
function done(dl){const r={listener:LISTENER,who:document.getElementById('who').value,native:(document.querySelector('input[name=native]:checked')||{}).value||'',answers:{}};let miss=0;
for(const it of ITEMS){const a={pref:'',err:{left:[],right:[]},other:{left:false,right:false},note:''};
const c=document.querySelector('input[name="p_'+it.n+'"]:checked');a.pref=c?c.value:'';if(!c)miss++;
for(const side of ['left','right']){document.querySelectorAll('input[data-n="'+it.n+'"][data-side="'+side+'"].w').forEach(x=>{if(x.checked)a.err[side].push(x.value)});
a.other[side]=document.querySelector('input[data-n="'+it.n+'"][data-side="'+side+'"].o').checked;}
a.note=document.getElementById('note_'+it.n).value;r.answers[it.id]=a;}
const s=JSON.stringify(r);document.getElementById('out').value=s;
if(dl){try{const b=new Blob([s],{type:'application/json'});const u=URL.createObjectURL(b);const l=document.createElement('a');l.href=u;l.download='answers_listener_'+LISTENER+'.json';l.click();}catch(e){}}
else{try{navigator.clipboard.writeText(s);}catch(e){}}
document.getElementById('msg').textContent=miss?('saved, but '+miss+' sentence(s) have no choice yet'):'done. Please send the text/file back.';}
</script></html>"""

ITEM = """<div class="item"><div class="t">%N%. %TEXT%</div><div class="clips">
<div class="clip"><b>Clip 1</b><br><audio controls preload="none" src="%S1%"></audio>%WORDS1%<label class="w"><input type="checkbox" class="o" data-n="%N%" data-side="left"> another word is wrong</label></div>
<div class="clip"><b>Clip 2</b><br><audio controls preload="none" src="%S2%"></audio>%WORDS2%<label class="w"><input type="checkbox" class="o" data-n="%N%" data-side="right"> another word is wrong</label></div></div>
<div class="pref"><label><input type="radio" name="p_%N%" value="left"> Clip 1 is better</label><label><input type="radio" name="p_%N%" value="right"> Clip 2 is better</label><label><input type="radio" name="p_%N%" value="same"> same</label>
<br><small>Comment (optional):</small> <input id="note_%N%" size="50"></div></div>"""


def keyed(folder: Path):
    d = {}
    for p in sorted(folder.glob("*.wav")):
        m = re.match(r"\d+", p.name)
        d[m.group(0) if m else p.stem] = p
    return d


def build(conds: dict, texts: dict, out: Path, listeners: int, seed: int, design: str, baseline: str | None):
    names = list(conds)
    if len(names) < 2:
        raise SystemExit("need at least two --cond")
    if design == "baseline":
        if baseline not in conds:
            raise SystemExit("--baseline must be one of the --cond names")
        pairs = [(baseline, n) for n in names if n != baseline]
    else:
        pairs = list(itertools.combinations(names, 2))
    clips = {n: keyed(Path(f)) for n, f in conds.items()}
    ids = sorted(set.intersection(*(set(c) for c in clips.values())))
    if not ids:
        raise SystemExit("no sentence is present in every condition folder")
    (out / "audio").mkdir(parents=True, exist_ok=True)
    secret = f"{seed}-{random.Random(seed).random()}"
    opaque, key_clips = {}, {}
    for n in names:
        for sid in ids:
            h = hashlib.sha1(f"{secret}|{n}|{sid}".encode()).hexdigest()[:12]
            opaque[(n, sid)] = f"audio/{h}.wav"
            key_clips[h] = {"cond": n, "sid": sid}
            shutil.copy(clips[n][sid], out / f"audio/{h}.wav")
    items = [(sid, a, b) for sid in ids for a, b in pairs]
    key = {"conditions": names, "baseline": baseline, "design": design, "seed": seed, "clips": key_clips,
           "sentences": {sid: {"display": (texts.get(sid) or {}).get("display") or (texts.get(sid) or {}).get("text", ""), "words": (texts.get(sid) or {}).get("words", [])} for sid in ids},
           "listeners": {}}
    for k in range(1, listeners + 1):
        rnd = random.Random(f"{seed}-{k}")
        order = items[:]
        rnd.shuffle(order)
        html_items, js_items, sides = [], [], {}
        for n, (sid, a, b) in enumerate(order, 1):
            left, right = (a, b) if rnd.random() < 0.5 else (b, a)
            item_id = hashlib.sha1(f"{secret}|{k}|{sid}|{a}|{b}".encode()).hexdigest()[:10]      # opaque: the page never contains condition names
            sides[item_id] = {"sid": sid, "A": a, "B": b, "left": left, "right": right}
            t = texts.get(sid) or {}
            words = t.get("words", [])
            def wbox(side):
                return "".join(f'<label class="w"><input type="checkbox" class="w" data-n="{n}" data-side="{side}" value="{html.escape(w, quote=True)}"> the word "{html.escape(w)}" sounds wrong</label>' for w in words)
            html_items.append(ITEM.replace("%N%", str(n)).replace("%TEXT%", html.escape(t.get("display") or t.get("text", ""))).replace("%S1%", opaque[(left, sid)]).replace("%S2%", opaque[(right, sid)])
                              .replace("%WORDS1%", wbox("left")).replace("%WORDS2%", wbox("right")))
            js_items.append({"n": n, "id": item_id})
        page = PAGE.replace("%ITEMS%", "\n".join(html_items)).replace("%LISTENER%", json.dumps(k)).replace("%ITEMS_JSON%", json.dumps(js_items))
        (out / f"listener_{k:02d}.html").write_text(page, encoding="utf-8")
        key["listeners"][str(k)] = sides
    keyfile = out.parent / (out.name + "_KEY.json")
    keyfile.write_text(json.dumps(key, indent=1, ensure_ascii=False), encoding="utf-8")
    return len(ids), len(items), keyfile


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cond", action="append", required=True, help="name=folder (repeat)")
    ap.add_argument("--baseline"); ap.add_argument("--design", choices=("baseline", "all-pairs"), default="baseline")
    ap.add_argument("--texts", required=True); ap.add_argument("--out", default="listening_pack")
    ap.add_argument("--listeners", type=int, default=3); ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    conds = dict(c.split("=", 1) for c in a.cond)
    texts = {}
    for line in open(a.texts, encoding="utf-8"):
        if line.strip():
            r = json.loads(line)
            texts[str(r["id"])] = r
    n_sent, n_items, keyfile = build(conds, texts, Path(a.out), a.listeners, a.seed, a.design, a.baseline)
    print(f"{n_sent} sentences, {n_items} comparisons per listener, {a.listeners} listener pages -> {a.out}/listener_NN.html. Zip the folder for reviewers; keep {keyfile} PRIVATE.")


if __name__ == "__main__":
    main()
