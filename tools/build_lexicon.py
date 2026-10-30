# -*- coding: utf-8 -*-
"""
tools/build_lexicon.py - one-time SEED of the lexicon data files from the tables in mme_hinglish.py plus the words reported by the listener.

  python tools/build_lexicon.py            # writes lexicon/*.csv|txt only if they do not exist yet
  python tools/build_lexicon.py --force    # overwrites them (this DESTROYS review statuses: use only to re-seed)

After the seed, lexicon/hindi_lexicon.csv is the source of truth: reviewers and developers edit the CSV, not this script.
Every row is written with status "unreviewed". NOTHING here is verified by a native speaker.
"""
import argparse, csv, sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import mme_hinglish as H  # noqa: E402

OUT = ROOT / "lexicon"

# --- words with a CONTEXT RULE: never grouped automatically. roman -> (devanagari options "a|b", rule, notes, variants) ---------------------------------
SPECIAL = OrderedDict([
    ("main", ("मैं|में", "main", "AMBIGUOUS: मैं (I) or में (in). Also an English word (main reason, main page). Rule picks from neighbours; unsure -> logged.", "")),
    ("me", ("में|मैं", "main", "English 'me' too. Converted only inside a Hindi context; prior reading is में.", "")),
    ("mai", ("मैं|में", "main", "Usually मैं; sloppy spelling of में also occurs. Same rule as main but no English reading.", "")),
    ("mein", ("में", "", "always में", "")),
    ("men", ("में", "homograph", "English 'men'. Converted only next to a Hindi word.", "")),
    ("ki", ("कि|की", "ki", "AMBIGUOUS: कि (that) or की (of). Rule: after pata/hai/lagta.. or before aap/main/yeh.. -> कि, else की.", "")),
    ("kee", ("की", "", "", "")),
    ("to", ("तो", "enclitic", "English 'to'. Converted only right after a Hindi word (or clause-initial before a Hindi word).", "")),
    ("toh", ("तो", "", "unambiguous Roman spelling of तो", "tho")),
    ("hi", ("ही", "enclitic", "English greeting 'Hi'. Converted only right after a Hindi word.", "")),
    ("is", ("इस", "proclitic", "English 'is'. Converted only before a Hindi word and not after an English word.", "")),
    ("us", ("उस", "proclitic", "English 'us'. Converted only before a Hindi word and not after an English word.", "")),
    ("the", ("थे", "enclitic", "English 'the'. Converted only right after a Hindi word (hote the).", "")),
    ("do", ("दो", "do", "English 'do'. दो (two / give). Converted before a Hindi word or a counted noun (do din), or after a Hindi verb (bata do).", "")),
    ("are", ("अरे", "interj", "English 'are'. Converted only clause-initially before a Hindi word.", "")),
    ("par", ("पर", "homograph", "English 'par'.", "")),
    ("per", ("पर", "homograph", "English 'per' (per month). Converted only next to a Hindi word.", "")),
    ("ho", ("हो", "homograph", "", "")),
    ("man", ("मन", "homograph", "English 'man'.", "")),
    ("bas", ("बस", "homograph", "", "")),
    ("ban", ("बन", "homograph", "English 'ban'.", "")),
    ("mat", ("मत", "homograph", "English 'mat'.", "")),
    ("tab", ("तब", "homograph", "English 'tab'.", "")),
    ("log", ("लोग", "homograph", "English 'log'.", "")),
    ("hum", ("हम", "homograph", "English 'hum'.", "")),
    ("hai", ("है", "hai", "", "")),
    ("hain", ("हैं", "", "", "hein")),
])
# English words that must stay Latin even though a Hindi homograph exists: NOT in the lexicon (see english_known.txt): so, pan, any, am, we, he, she ...

# --- extra spelling variants for canonical entries (canonical roman -> space separated variants) -------------------------------------------------------
EXTRA_VARIANTS = {
    "chahiye": "chaiya chaiye chahie chahiyae chahiyee chahiyey chaahiye chahiyen",
    "baare": "bare baarey barey baaray baarein",
    "hoon": "hun hu hoo hun",
    "aap": "ap", "aapka": "apka aapkaa", "aapki": "apki", "aapke": "apke", "aapko": "apko aapku", "aapse": "apse", "aapne": "apne_",
    "sakta": "sakata saktaa", "sakti": "sakati", "sakte": "sakate",
    "hota": "hotaa", "hoti": "hotee", "deta": "detaa", "dete": "detey", "nahi": "nhi nahee nai_", "kaise": "kese kaisey", "bahut": "bahot bhut bohut",
    "theek": "thik theak", "achha": "acha accha achcha", "zaroor": "jaroor zarur jarur", "dhanyavaad": "dhanyawad dhanyavad", "kyun": "kyu kyon",
    "mujhe": "mujhey", "humein": "hume hamein", "samajh": "samaj", "karna": "krna", "kijiye": "kijiyeh", "bataiye": "bataye_ batayiye batayie",
    "kya": "kyaa", "yahan": "yaha yahaan", "wahan": "waha wahaan", "kuch": "kuchh", "bilkul": "bilkull", "padhai": "padhaai", "paise": "paisey",
}
EXTRA_VARIANTS = {k: " ".join(w for w in v.split() if not w.endswith("_")) for k, v in EXTRA_VARIANTS.items()}

# --- new Hindi words (not in mme_hinglish.py): roman -> (devanagari, variants, notes). All unreviewed. -----------------------------------------------
NEW = {
    "han": ("हाँ", "haan haa haanji hanji", "reported by listener; confirm हाँ vs हां"),
    "haan": None,   # placeholder so the grouping below ignores it (variant of han)
    "chaiya": None,
    "maine": ("मैंने", "mene mainey", "I (ergative)"),
    "mujhse": ("मुझसे", "", ""), "tumse": ("तुमसे", "", ""), "aapse": None,
    "bataunga": ("बताऊँगा", "bataoonga", ""), "bataungi": ("बताऊँगी", "bataoongi", ""), "batata": ("बताता", "bataata", ""), "batati": ("बताती", "bataati", ""),
    "sakta_hoon": None,
    "karunga": None, "dunga": ("दूँगा", "doonga", ""), "dungi": ("दूँगी", "doongi", ""), "aaunga": ("आऊँगा", "aaoonga", ""), "aaungi": ("आऊँगी", "aaoongi", ""),
    "milta": ("मिलता", "", ""), "milti": ("मिलती", "", ""), "milte": ("मिलते", "", ""), "chahenge": ("चाहेंगे", "", ""),
    "kehte": ("कहते", "kehte", ""), "kehna": ("कहना", "", ""), "kaha": ("कहा", "", ""), "kahiye": ("कहिए", "", ""), "kehta": ("कहता", "", ""), "kehti": ("कहती", "", ""),
    "pata": ("पता", "", "I know/ address; check"), "maloom": ("मालूम", "", ""), "samjhe": ("समझे", "", ""), "samjhenge": ("समझेंगे", "", ""),
    "yaar": ("यार", "", ""), "ji": ("जी", "", ""), "haanji": None, "sir": None,
    "kripya": None, "shukriya": None, "bataiye": None,
    "yeh": None, "isliye": None, "ladka": ("लड़का", "", ""), "ladki": ("लड़की", "", ""), "parivaar": ("परिवार", "", ""), "maa": ("माँ", "", ""), "papa": ("पापा", "", ""),
    "course": None, "college": None,
    "padhega": ("पढ़ेगा", "", ""), "padhenge": ("पढ़ेंगे", "", ""), "karenge": None, "karega": ("करेगा", "", ""), "karegi": ("करेगी", "", ""), "kare": ("करे", "", ""), "karein": ("करें", "kren", ""),
    "chahiyega": None, "dikhaiye": ("दिखाइए", "", ""), "dikhata": ("दिखाता", "", ""), "dikhati": ("दिखाती", "", ""),
    "lagenge": ("लगेंगे", "", ""), "lagte": ("लगते", "", ""), "lagne": ("लगने", "", ""), "lagegi": ("लगेगी", "", ""),
    "jaanna": ("जानना", "", ""), "jaante": ("जानते", "", ""), "jaanta": ("जानता", "", ""), "jaankari": ("जानकारी", "", "information"), "jaanu": None,
    "sahayata": ("सहायता", "", ""), "salaah": ("सलाह", "", "advice"), "salah": None, "sujhav": ("सुझाव", "", ""),
    "avashyak": ("आवश्यक", "", ""), "sambandhit": ("संबंधित", "", ""), "uplabdh": ("उपलब्ध", "", ""),
    "nahin": None, "bilkul": None,
    "wapas": ("वापस", "vapas", ""), "pehle": None, "dobara": None, "shayad": ("शायद", "", ""), "sirf": None,
    "mujhe": None, "hamein": None,
    "thodi": None, "kyunki": None, "isiliye": ("इसीलिए", "", ""), "jaise": None,
    "ghar": None, "school": None,
    "admission": None,
}
NEW = {k: v for k, v in NEW.items() if v}
# Second batch: everyday Hinglish words found missing in carrier sentences and in a brainstorm of counselling-call vocabulary. All UNREVIEWED proposals.
_BATCH = """
aa आ | aao आओ | jao जाओ | chalo चलो | ruko रुको | rukiye रुकिए | suno सुनो | dekho देखो | bolo बोलो | likho लिखो | padho पढ़ो | samjho समझो | socho सोचो | lo लो
kaunsa कौनसा | kaunsi कौनसी | kaunse कौनसे | kisliye किसलिए | kisme किसमें | kidhar किधर
puchna पूछना poochna | puchiye पूछिए poochiye | puchte पूछते | puchta पूछता | pucho पूछो
padhaya पढ़ाया | padhate पढ़ाते | padhta पढ़ता | padhti पढ़ती | padega पड़ेगा | padegi पड़ेगी | padenge पड़ेंगे | padta पड़ता | padti पड़ती
sochte सोचते | sochta सोचता | sochti सोचती | bhar भर | bhare भरे | bhariye भरिए | bhara भरा | bhejenge भेजेंगे | bhejunga भेजूँगा | bhejungi भेजूँगी
jaoge जाओगे | aaoge आओगे | karoge करोगे | karoon करूँ | bataoon बताऊँ | dikhaoon दिखाऊँ | lenge लेंगे | lunga लूँगा | lungi लूँगी | rahega रहेगा | rahegi रहेगी | rahenge रहेंगे
milenge मिलेंगे | milegi मिलेगी | batayenge बताएँगे bataenge | samjhayenge समझाएँगे samjhaenge | samjhaunga समझाऊँगा | sunenge सुनेंगे | dekhenge देखेंगे
dekhiyega देखिएगा | bataiyega बताइएगा | kijiyega कीजिएगा | aaiyega आइएगा | jaiyega जाइएगा | chahun चाहूँ | mujhko मुझको | unko उनको | usko उसको | isko इसको | tumko तुमको | humko हमको | inko इनको
isme इसमें | usme उसमें | inme इनमें | kahin कहीं | turant तुरंत | fauran फ़ौरन | ne ने | kitab किताब | kitaab किताब | shikshak शिक्षक | vidyarthi विद्यार्थी
sheher शहर shahar | rajya राज्य | desh देश | sawal सवाल | jawab जवाब | samasya समस्या | hal हल | tareeka तरीका tarika | vikalp विकल्प | faisla फ़ैसला | mauka मौका
mahina महीना | mahine महीने | teesra तीसरा | aakhir आख़िर | shuruaat शुरुआत | kaagaz कागज़ | dastavez दस्तावेज़ | suvidha सुविधा | khaana खाना khana | paani पानी
sasta सस्ता | mehenga महँगा mehnga | behtareen बेहतरीन | ekdum एकदम | khaas ख़ास | kaafi काफ़ी kafi | adhik अधिक | arre अरे are_ | wah वाह | namaskar नमस्कार | alvida अलविदा
pichle पिछले | agle अगले | somvar सोमवार | mangalvar मंगलवार | budhvar बुधवार | guruvar गुरुवार | shukravar शुक्रवार | shanivar शनिवार | ravivar रविवार
dauran दौरान | alawa अलावा | mutabik मुताबिक़ | anusaar अनुसार | upar ऊपर | neeche नीचे | saamne सामने | nazdeek नज़दीक | bhitar भीतर
nahi_hai_skip x | aapka_skip x
"""
for _line in _BATCH.strip().splitlines():
    for _item in _line.split("|"):
        _p = _item.split()
        if len(_p) < 2 or _p[0].endswith("_skip") or _p[1] == "x":
            continue
        NEW[_p[0]] = (_p[1], " ".join(v for v in _p[2:] if not v.endswith("_")), "")

# Regular verb forms generated from consonant-final stems: roman suffix -> Devanagari matra string. UNREVIEWED proposals, like everything else.
_SUFFIXES = {"na": "ना", "ne": "ने", "ta": "ता", "ti": "ती", "te": "ते", "a": "ा", "i": "ी", "e": "े", "ega": "ेगा", "egi": "ेगी", "enge": "ेंगे", "unga": "ूँगा", "ungi": "ूँगी",
             "un": "ूँ", "oge": "ोगे", "ogi": "ोगी", "o": "ो", "iye": "िए", "ein": "ें"}
_STEMS = {"dekh": "देख", "bol": "बोल", "padh": "पढ़", "likh": "लिख", "sun": "सुन", "samajh": "समझ", "soch": "सोच", "bhej": "भेज", "mil": "मिल", "rakh": "रख", "chun": "चुन",
          "seekh": "सीख", "bhar": "भर", "puch": "पूछ", "lag": "लग", "chal": "चल", "badal": "बदल", "dikh": "दिख", "nikal": "निकल", "pahunch": "पहुँच", "jaan": "जान", "maang": "माँग",
          "bana": "बना", "uthaa": "उठा", "pehchaan": "पहचान", "shuru_": "शुरू", "likh_": "लिख", "baith": "बैठ", "khel": "खेल", "chhod": "छोड़", "jod": "जोड़", "tod": "तोड़", "dhoond": "ढूँढ",
          "puchh": "पूछ", "bhool": "भूल", "yaad_": "याद", "kharid": "ख़रीद", "bech": "बेच", "bhej_": "भेज", "paa": "पा"}
_STEMS = {k: v for k, v in _STEMS.items() if not k.endswith("_") and k not in ("uthaa", "paa", "bana")}
_MANUAL_FORMS = """
aayenge आएँगे | aaoge आओगे | aaya आया | aayi आई | jayenge जाएँगे | jaunga जाऊँगा | jaungi जाऊँगी | jayein जाएँ | dega देगा | degi देगी | lega लेगा | legi लेगी | dein दें | lein लें
batana बताना | batane बताने | bataya बताया | batayi बताई | batayega बताएगा bataega | batayegi बताएगी bataegi | bataoge बताओगे | aayenge_x x
"""
for _item in _MANUAL_FORMS.replace("\n", " ").split("|"):
    _p = _item.split()
    if len(_p) >= 2 and _p[1] != "x" and not _p[0].endswith("_x"):
        NEW[_p[0]] = (_p[1], " ".join(_p[2:]), "")


def _english_words():
    p = OUT / "english_known.txt"
    return {l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")} if p.exists() else set()


def generated_verbs(existing: set):
    eng = _english_words()
    rows = []
    for stem, dev in _STEMS.items():
        for suf, matra in _SUFFIXES.items():
            roman = stem + suf
            if roman in existing or roman in eng or roman in NEW:
                continue
            rows.append((roman, dev + matra))
    return rows

ENGLISH_FUNCTION = """a an the and or but so because if then than that this these those there here when while where which who whom whose what how why
i me my mine we us our you your yours he him his she her it its they them their is are was were be been being am do does did done have has had having will would shall should can could may might must
of in on at by for with without about into onto from to up down out over under again further once not no nor only own same too very just also even still yet already
please thank thanks sorry hello hi hey ok okay yes sure well now today tomorrow yesterday tonight morning evening night
let lets us make made take took give gave get got go going went come came see saw know knew think thought want wanted need needed tell told say said ask asked call called
let's don't doesn't didn't can't won't isn't aren't wasn't weren't i'm i'll i've i'd you're you'll you've we're we'll we've they're it's that's there's here's what's
one two three four five six seven eight nine ten first second third last next more most less least much many few some any all both each every other another such
good great better best bad worse worst new old young big small long short high low right left early late easy hard simple clear free open ready sure
if else after before during until since between among through across against around along within without
like likes liked love feel feeling feelings feels felt help helps helped helping start started starting stop stopped keep kept look looked looking find found try tried trying use used using work works worked working
share shared sharing talk talked talking speak spoke spoken listen listened ask asking answer answered answers question questions
student students parent parents teacher teachers friend friends family course courses college colleges university universities school exam exams test tests marks result results
career careers interest interests goal goals plan plans option options choice choices decision decisions stream streams subject subjects skill skills
fee fees cost costs price scholarship scholarships admission admissions application applications document documents form forms process details detail information
session counselling counseling counsellor counsellors confidential confidentially openly honestly really actually exactly clearly properly slowly quickly carefully
repeatedly repeated repeat wins win won winning more than less like unlike up down""".split()



def hindi_rows():
    """Rows [roman, variants, devanagari, notes, context_rule, status, reviewed_by]; the seed tables are grouped by Devanagari so spelling variants share one entry."""
    skip = set(SPECIAL) | {"hein", "tho", "kee"}
    groups = OrderedDict()
    for roman, dev in H.HINDI_ROMAN.items():
        if roman not in skip:
            groups.setdefault(dev, []).append(roman)
    rows = [[romans[0], " ".join(romans[1:]), dev, "seed: mme_hinglish.py", "", "unreviewed", ""] for dev, romans in groups.items()]
    rows += [[roman, variants, dev, notes, rule, "unreviewed", ""] for roman, (dev, rule, notes, variants) in SPECIAL.items()]
    rows.append(["kee", "", "की", "", "", "unreviewed", ""])
    for roman, (dev, variants, notes) in NEW.items():
        rows.append([roman, variants, dev, notes or "new: reported/common word, not in the old table", "", "unreviewed", ""])
    have = {r[0] for r in rows} | {v for r in rows for v in r[1].split()}
    for roman, dev in generated_verbs(have):
        rows.append([roman, "", dev, "generated: regular verb form (stem + suffix)", "", "unreviewed", ""])
    return rows


def finish(rows):
    """Attach EXTRA_VARIANTS and the 'reported' notes, then drop any spelling that already belongs to an earlier row (one spelling -> one entry)."""
    by_key = {}
    for r in rows:
        for w in [r[0]] + r[1].split():
            by_key.setdefault(w, r)
    for key, vs in EXTRA_VARIANTS.items():
        row = by_key.get(key)
        if row is not None:
            row[1] = " ".join(row[1].split() + [v for v in vs.split() if v not in by_key])
            for v in vs.split():
                by_key.setdefault(v, row)
    for k in ("chahiye", "aapko", "hoon", "han", "sakta", "baare", "aapka", "hota", "hai", "deta"):
        row = by_key.get(k)
        if row is not None and "reported" not in row[3]:
            row[3] = (row[3] + "; reported by listener").strip("; ")
    seen, final = set(), []
    for r in rows:
        if r[0] in seen:
            continue
        r[1] = " ".join(dict.fromkeys(v for v in r[1].split() if v not in seen and v != r[0]))
        seen.update([r[0]] + r[1].split())
        final.append(r)
    return final


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    lex = OUT / "hindi_lexicon.csv"
    if lex.exists() and not a.force:
        raise SystemExit(f"{lex} exists: it holds review statuses. Use --force only to re-seed.")
    final = finish(hindi_rows())
    with open(lex, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["roman", "variants", "devanagari", "notes", "context_rule", "status", "reviewed_by"])
        w.writerows(final)
    with open(OUT / "english_respell.csv", "w", encoding="utf-8-sig", newline="") as f:      # Approach C2 proposals
        w = csv.writer(f)
        w.writerow(["roman", "devanagari", "notes", "status", "reviewed_by"])
        for k, v in {**H.ENGLISH_DEV, **H.ENGLISH_DEV_EXTRA}.items():
            w.writerow([k, v, "seed: mme_hinglish.py", "unreviewed", ""])
    eng = sorted(set(ENGLISH_FUNCTION))
    (OUT / "english_known.txt").write_text("# English words that may stay Latin inside a Hindi sentence (counted as KNOWN by the sentence gate). One per line.\n# The keys of english_respell.csv are known as well.\n" + "\n".join(eng) + "\n", encoding="utf-8")
    print(f"wrote {len(final)} Hindi entries, {len(H.ENGLISH_DEV) + len(H.ENGLISH_DEV_EXTRA)} respell rows, {len(eng)} English words -> {OUT}")


if __name__ == "__main__":
    main()
