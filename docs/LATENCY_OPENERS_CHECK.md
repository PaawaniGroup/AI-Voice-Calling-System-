# Opening phrases: offline check against 20 questions (no Brain call)

Run: `python tools/check_openers.py` (26 Oct 2026). The questions are synthetic (written for this check, not a caller's words). The fit codes come from RULES written by the reviewer (the assistant), not from a native-speaker panel; see the header of the tool. A native-speaker review of the OK pairs is still needed.

## The 20 questions

| # | language | kind | question |
|---|---|---|---|
| 1 | Hindi | topical | B.Tech कंप्यूटर साइंस की फीस कितनी है? |
| 2 | Hindi | topical | क्या बारहवीं में साठ प्रतिशत पर admission मिल जाएगा? |
| 3 | Hindi | topical | होस्टल की सुविधा है या नहीं? |
| 4 | Hindi | follow-up | और scholarship के बारे में भी बताइए। |
| 5 | Hindi | callback | मुझे किसी counsellor से बात करनी है। |
| 6 | Hindi | repeat | आपने क्या कहा, फिर से बोलिए। |
| 7 | Hindi | complaint | मुझे पिछली बार गलत जानकारी मिली थी। |
| 8 | Roman Hinglish | topical | MBA ke liye last date kya hai? |
| 9 | Roman Hinglish | topical | placement kaisa hota hai is college mein? |
| 10 | Roman Hinglish | topical | mujhe documents kaun kaun se chahiye honge? |
| 11 | Roman Hinglish | follow-up | aur agar mera percentage kam ho to? |
| 12 | Roman Hinglish | greeting | hello, kya aap meri baat sun pa rahe hain? |
| 13 | Roman Hinglish | garbled | ye wo kya... course ka naam... uh |
| 14 | Roman Hinglish | topical | online classes hoti hain ya sirf offline? |
| 15 | English | topical | What is the eligibility for the MBA programme? |
| 16 | English | topical | Can I get a scholarship if I am from a small town? |
| 17 | English | follow-up | And what about the hostel fees? |
| 18 | English | callback | Can someone call me back tomorrow? |
| 19 | English | greeting | Hello, is this the admissions helpline? |
| 20 | English | repeat | Sorry, could you repeat that? |

Roman-Hinglish callers get a Devanagari reply (the reply policy), so they need Devanagari openers.

## The check

Part A (mechanical): 2-4 words, ends in punctuation, does not start with हाँ, हां, नहीं, बिल्कुल, ज़रूर, जरूर, yes, no, sure, of course, no digit (no fact), no gendered verb form (the counsellor's gender is a tenant setting), the script the reply needs.
Part B (fit with a continuation): language/script match (an English opener before a Hindi reply makes the voice switch language in the middle of a sentence, because the voice picks hi/en per clause); referent (an opener that points at "this" or "your question" needs something asked); grammar (a relative clause needs its correlative).

## Result (20 questions: OK / CONSTRAINED / NO)

| opener | OK | CONSTRAINED | NO | comment |
|---|---|---|---|---|
| देखिए, इस बारे में, | 9 | 2 | 9 | fits every topical Hindi/Hinglish question; NO for greeting, garbled, repeat and every English question |
| आपके प्रश्न पर, | 9 | 2 | 9 | same pattern; the most formal |
| इस विषय पर, | 9 | 2 | 9 | same pattern; shortest |
| Regarding your question, | 3 | 1 | 16 | English topical questions only |
| On that point, | 1 | 3 | 16 | follow-up English questions only |
| आप कहिए, (non-topical) | 14 | 0 | 6 | fits every Hindi/Hinglish question, in particular greeting, garbled and repeat (replaces "जी, कहिए,": "जी" can mean "yes") |
| Please go on, (non-topical) | 6 | 0 | 14 | English questions: fits greeting and repeat, and loosely the others |

Decisions of the operator (26 Oct 2026): "आपने जो पूछा," is dropped (a relative clause: it forced a continuation starting with "वह" or "उसका"; 0 OK, 11 constrained); "जी, कहिए," is replaced by "आप कहिए,". The rule's no-topic sentence must never promise a callback or any action: it is now in the rule text (`app/ai/streaming/ramp.py`) and covered by a test.

Findings: 5 of the 20 questions (a greeting, an unclear utterance, a request to repeat) have no topic, so no topical opener fits them; the rule should say what to do then (proposal: "if nothing was asked about a topic, open with a short polite phrase that invites the caller to continue"). The opener must also match the reply language, so the Brain needs the language of the reply before it writes the opener (it already receives the `reply_mode` hint). There are no pairs in Part B marked OK for the callback and complaint kinds except the non-topical ones.

## Is a Brain test needed?

Yes, for the one thing this check cannot see: whether the Brain actually follows the rule (2-4 words, ends in punctuation, no forbidden word, correct language, continues grammatically). That needs real Brain calls.
REQUEST (built, NOT run): `tools/check_brain_openers.py`. Its own 20 questions (fees, eligibility, scholarship, hostel, placements, study abroad in a New Zealand style, callbacks, greetings, garbled speech, repeat requests; 7 Devanagari Hindi, 7 Roman Hinglish, 6 English; synthetic) are in the tool. One Brain call per question with the tenant's real Devanagari system prompt plus the ramp rule and the `(reply_mode: ..)` tag, `max_tokens` 60, no document context (so any fee, cut-off or date in a reply is ungrounded).
Screen output: counts only (opener obeyed, forbidden word used, opener language matched, whole-reply script ok, replies that state a fee, a cut-off or a date). The 20 replies go to ONE local file OUTSIDE the repository (mode 0600) and are never printed or logged. `--without-rule` runs the same questions as a control.
Refuses without `--approved "<id>"`, with a reply file inside the repository, and stops at the first 401/403, after 3 consecutive errors, and at the estimated cost cap.
Needs: the Brain endpoint warm (idle 300 s) and `RUNPOD_BRAIN_KEY` in the environment of the process (or the path of the Brain key file).
Cost (ESTIMATE; the Brain GPU price is UNKNOWN): 20 calls of 1-3 s of execution each = 20-60 GPU-seconds on a warm worker: **$0.004-$0.011 at $0.00019 per second, $0.006-$0.018 at $0.0003, up to $0.036 at an assumed $0.0006** (a larger GPU). Proposed cap $0.05 (the tool stops at it using `--price-per-s`, default $0.0006). A control run without the rule would cost the same again. Only Billing is authoritative.
