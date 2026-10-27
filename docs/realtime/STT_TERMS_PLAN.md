# Course-name recognition: glossary, hint, term-level evaluation, confidence (27 Oct 2026; plan; nothing here has run)

Fact from the operator's own test call (REAL): saying "B.Tech" is written by the recogniser as "edm"; call B's glossary counts were 0 because that form was not in the glossary.

## 1. Glossary sound-alikes with a context guard (built on `callbuild`, compile-checked, tests written, NOT RUN, NOT COMMITTED)
- Built-in guarded sound-alikes of B.Tech: edm, e d m, the Devanagari spelling of e-d-m, bitech, b tech, beetech, bee tech, be tech, bi tech, btech, bitek, beetek. The existing always-on mappings (beetaik, bee tek, btek, ...) are unchanged.
- **Guard:** a guarded form is rewritten only when the utterance carries an education cue (admission, fee(s), course(s), college, eligibility, branch, cutoff / cut off, ke liye, and the Devanagari equivalents), or is a very short course-name question (at most 4 words with a question marker). Latin forms sit on word boundaries (so "feel" is not the cue "fee", and "bitechnology" is not touched).
- **Per-tenant list, outside the repo:** setting `MME_TEL_AI_GLOSSARY_PATH` (read at the start of each call of a tenant with `glossary_correction`). Pairs are never in the repo or in a log line. Protected everyday words (the existing NEVER_REWRITE list) can never become a pair; at most 500 pairs; malformed lines are counted (`bad_lines:N`), never applied. Reason codes only, never a path or a term.
- **False-positive risk (qualitative; no measurement exists):** low for a form that is not an everyday word, because it needs a cue. Known residual: a four-word question containing a question word, e.g. a music-genre question, is rewritten (a documented test pins it). The cue list is deliberately limited to education words. The count of corrections per turn goes into the evidence line, so any over-correction would show.
- **Where the pairs go (item 5):** `C:\mme_glossary\pairs.tsv` (WSL `/mnt/c/mme_glossary/pairs.tsv`), outside every repository. Format: UTF-8, ONE pair per line, tab separated: `said<TAB>heard`, optionally `<TAB>guard` (`cue`, the default, or `always`) and `<TAB>tenant_id`. Lines starting with `#` and blank lines are ignored. Example shape (generic): `B.Tech<TAB>edm`. The service needs `MME_TEL_AI_GLOSSARY_PATH=/mnt/c/mme_glossary/pairs.tsv` in its environment (a restart, the operator's approval).

### Letter-name families (PROPOSAL ONLY, NOT applied)
On a phone line the letter names B, C, D, E, G, P, T, V sound alike ("bee/be/bi", "see/si", "dee/di", "ee/i", "gee/ji", "pee/pi", "tee/ti", "vee/vi"). Conservative rules if applied: only SPACED SEQUENCES of two or more letter-name sounds, only with the education-cue guard, never a single letter, never `ji`/`si`/`pi` alone (everyday words), `bi si` stays the existing hint (BCA or BC category).
| said | heard forms proposed (spaced letter names) | risk |
|---|---|---|
| BCA | bi si e, be si e, b c a | collides with BC category; keep the cue guard and the existing `bi si` hint |
| BBA | bi bi e, b b a | low |
| MBA | em bi e, m b a | low |
| BSc | bi es si, b s c | low |
| MSc | em es si, m s c | low |
| PCM | pi si em, p c m | low |
| CUET | si yu i ti, c u e t | low |
| JEE | je ee, ji ee ee | the existing `je ee men` -> JEE Main stays |
| NEET | en ee ee ti, n e e t | do NOT map `nit` (NIT is a real institute name) |
These stay a proposal until the operator's real said/heard pairs show which forms actually occur.

## 2. Brain hint block `glossary_hint` (own flag, default OFF; drafted, NOT RUN)
Text (524 characters): a SPEECH RECOGNISER NOTE saying the recogniser often mishears course names (edm or bitech mean B.Tech; bi si e may mean BCA); if a word does not fit, ask ONE short confirming question in the caller's language and do not answer about a guessed course until confirmed; a confirming-question reply still starts with the opening phrase when there is an OPENING RULE; never state fees, cut-offs or dates unless they come from the provided knowledge.
- **Size:** hint 524 characters; with strict grounding (536) and the ramp opening rule (798): 1858 characters appended in total in the worst case.
- **No contradiction (checked by a test):** strict grounding says to ask the caller to clarify an unrecognised term and never to state facts without CONTEXT: the hint agrees and only adds the "ONE short question, no guessing" shape. The ramp rule demands a neutral 2-4 word opening and forbids agree/deny/promise words: the hint contains none of those words, promises nothing, and says the confirming question comes after the opening phrase. When both "term unclear" and "facts missing" apply, the hint's confirming question comes first (a reply is one or two sentences in strict grounding: the question fits).
- Order in the system message: base, strict grounding (at construction), ramp rule, hint. Applied once per call.

## 3. Term-level evaluation on the operator's recordings (REQUEST ONLY; not built beyond this plan; no code until the inputs exist)
- **Inputs the operator provides (outside every repository):** the 12 recorded lines (folder, checked free with `tools/recording_intake_check.py`); a term list file: per line, the line id and the expected terms (course names, acronyms), marking which are letter-name terms; the said/heard pairs file above (optional).
- **Conditions:** audio clean vs telephone chain (`tools/telephone_chain.py`: 8 kHz mono, the phone band) x language auto / hi / en x prompt none / domain = 12 conditions. The language is an environment variable of the worker (the request's `language` is ignored), so each language is its own endpoint; `prompt` needs the patched handler of `docs/worker_stt` (`input.prompt` of at most 200 characters; UNVERIFIED with this model: it degrades to no prompt and says `prompt_used: false`).
- **Endpoints the operator must create:** THREE from the patched worker image, same model: WHISPER_LANGUAGE empty (auto), `hi`, `en`; min workers 0, max 1, idle timeout 5 s; deleted afterwards. (The existing endpoint is not patched and is not needed.)
- **Jobs:** per endpoint 12 lines x 2 audio variants x 2 prompts = 48 jobs; decoding is greedy (deterministic), so ONE run per condition is enough: 144 jobs in total.
- **Scoring (counts only, no transcript printed or stored):** per term: found / not found (normalised: case, spacing, dots); per line and per condition: terms found of terms expected; a **letter-confusion table** for the letter-name terms (B, C, D, E, G, P, T, V): for each expected letter, how often the recogniser wrote that letter's sound and how often another letter's (a matrix of counts); the language field returned; the worker's `[STT_TIMING]` numbers. This supersedes the earlier auto/hi/en language test (it is a subset of it).
- **Cost (ESTIMATE; Billing is authoritative).** Billed time per endpoint = cold start 21-32 s (MEASURED; 32 s used) + 48 jobs x about 3 s each (the STT job wall time MEASURED in call B is 2-3 s and the worker stays billed between sequential jobs) + 5 s idle = about **181 s**; three endpoints = **543 s**.
  | | $0.00019/s | $0.0003/s |
  |---|---|---|
  | 543 s | **$0.103** | **$0.163** |
  Cap proposed: **$0.25**. Stops: the cap, the first 401/403, 3 consecutive errors.
- **CORRECTION of an earlier estimate:** the earlier language-test cost counted about 1 s per job; the worker stays billed between sequential jobs, so about 3 s per job is the honest figure. The earlier 24-jobs-per-endpoint plan then costs about 590 s = $0.112 / $0.177 (not $0.085 / $0.134); its cap of $0.20 still holds but is tight. This plan replaces it.
- **Minimum the operator must do:** record the 12 lines and write the term list; build or deploy the three endpoints from the patched image (console) and give their ids and key files; reply "APPROVED: run the term evaluation once, cap $0.25, endpoints <ids>, recordings <folder>".

## 4. Confidence output and a low-confidence clarification rule (DESIGN ONLY, no code)
- **Worker change** (`docs/worker_stt/handler.py` and the live worker): return, next to the text, `avg_logprob` (mean log probability of the generated tokens, from the generation scores), `no_speech_prob` (the probability of the no-speech token at the first decoding step) and optionally `min_token_prob`. The old fields stay; a worker without them returns nothing extra and the app rule is simply off.
- **App rule (behind a new default-OFF tenant flag, e.g. `stt_confidence_gate`):** after the STT result, if the fields exist and `avg_logprob` is below T1 or `no_speech_prob` above T2, do NOT call the Brain for a long answer: play a short cached neutral clarification clip ("could you say that again?" in the call's language; a new cached-audio REQUEST like the acknowledgement clips) or let the Brain ask ONE confirming question. At most once per two consecutive turns, never for a barge-in turn, never when the text is empty (that case already exists). T1 and T2 are CALIBRATED on the operator's recordings against the term evaluation (found/not found), never guessed.
- **Evidence:** the two numbers per turn in the evidence line, and a count of clarifications.
- **Tests:** unit tests of the rule with mock STT results (below / above / missing fields / the once-per-two-turns limit / flag off); a handler test with a fake model that exposes scores (SIMULATED); the calibration script on the recordings (counts only).
- **Risks:** Whisper confidence is poorly calibrated for a fine-tuned model and for Hindi/Hinglish; a false clarification annoys the caller and costs a turn; a threshold tuned on 12 lines may not transfer; no-speech probability from a fine-tuned model may be flat (UNVERIFIED); the worker change needs a redeploy of the live endpoint; the extra fields add a few bytes; a clarification clip adds a new cached asset and a chargeable generation REQUEST.

## 5. Raw caller text for quality review: DESIGN NOTE (no build) -- NEEDS CONSENT TEXT AND A RETENTION DECISION FROM THE OWNER
Today the raw STT text exists only in memory inside the running turn and is overwritten by the corrected text; nothing stores or logs it (history, evidence file, dashboard feed and logs carry the corrected text, counts or lengths only). If the owner wants raw text for review:
- **Separate default-OFF tenant flag** (e.g. `store_raw_stt`), independent of `glossary_correction`; never enabled by the supervised-call scripts.
- **Consent:** a consent text announced at the start of the call (wording and language: the owner's decision) and a per-call consent marker; no marker, nothing stored. Callers who decline are never recorded.
- **Storage:** an access-controlled store OUTSIDE the repo and outside the normal call record, holding only the raw text, the corrected text, the call id and a timestamp; never in the evidence file, the dashboard feed or logs.
- **Retention:** a fixed period chosen by the owner with automatic expiry, and a delete-by-call path; a documented access list; an audit log of reads.
- **Redaction on write:** phone numbers and other identifiers; no audio.
- **Tests:** flag off stores nothing; no consent marker stores nothing; the evidence file, dashboard and logs stay free of raw text; expiry and deletion work.
- **Open decisions for the owner:** the consent wording, who may read, the retention period, where the store lives.