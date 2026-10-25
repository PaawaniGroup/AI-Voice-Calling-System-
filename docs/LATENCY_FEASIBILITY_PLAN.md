# Latency feasibility plan: first sound under 1 s, the real answer as fast as possible

Status: **design only.** Nothing here changes live behaviour; every flag stays OFF. No spike runs, and nothing is spent, until the owner says so.
**M** = measured, **A** = assumed. The measurements are in `docs/evidence/` (phase0, phase0b_tts_v3.json, phase0b_cold_starts_v2.json).

## 1. Budget, revised

| Stage | Budget (s) | Today (s) | Spike go/no-go threshold (s) |
|---|---|---|---|
| End-of-turn decision | 0.30 | 0.70 (M, `SILENCE_MS`) | 0.40 |
| Speech-to-text, final | 0.15 | 1.50 (M, only 3 old turns) | 0.25 |
| Brain, first clause | 0.25 | 0.99 (M 0.87 first token + A 0.12) | 0.40 |
| Voice, first audio | 0.20 | 3.59 (M, 4 words, includes 1.1 s warm pickup) | 0.35 |
| Path to the ear | **0.30** (was 0.10) | about 1.15 (M 0.8 transport + 0.15 prebuffer + A 0.2 call leg) | 0.30 |
| **Total** | **1.20** (was 1.00) | **about 7.1** | **1.70** |

The ear-path budget is 0.3 s because the call leg alone is assumed at 0.2 s. The 1.20 s total is the target; if every spike only just passes, the real answer starts at about 1.7 s. Under 1 s is only reachable for a **first sound** (filler or cached answer), not for the real answer.

## 2. Path 1: filler (design only, default OFF, built after the first supervised call)

**When it plays.** Only after the end of turn is **confirmed**, that is after the normal silence window has run out. It is never played on a speculative trigger: if STT or Brain were started early at a shorter silence and the caller resumes, no filler frame may have been sent.
Heard at about `confirmed end-of-turn + 0.15 s prebuffer + 0.2 s call leg`: 1.05 s at 700 ms silence, 0.90 s at 550 ms, 0.80 s at 450 ms (A). The real answer time does not change.

**What it plays.** Two or three short phrases in the "utkarsh" voice, rotated, never the same one twice in a row, each under 0.8 s, stored as `ulaw8k` in a directory outside the repository, generated once with the voice endpoint (a few cents). **Neutral phrases only:** no "yes", no "no", no "okay", nothing that agrees or disagrees before the question is understood (for example "जी।", "एक सेकंड।", "देखता हूँ।" — the owner chooses). The phrase list is also checked by a test against a deny-list of affirmations and negations.

**How it plays.** Through the same single playout timeline as the real clauses, as a clause marked `filler`, so it cannot overlap real speech. If real audio is ready while it is playing, the real clause waits for the filler to end (at most the phrase length, under 0.8 s) and never cuts it mid-word. It is skipped on the greeting turn and when a cached answer (path 2) is already ready.

**Barge-in.** The same silence, then clear, then cancel path stops it. Frames already sent are cleared at Plivo; none follow. It is never recorded in the conversation history, the transcript or the "heard" part of a turn; only a counter is kept.

**Switches.** Per-tenant flag `filler_enabled`, default OFF; `filler_dir` setting; if the files are missing the feature turns itself off without an error on the call path.

**Tests, one per rule (to be written with the feature):**
1. `test_filler_not_played_on_speculative_trigger`: an early speculative end-of-turn followed by speech within the window sends no filler frame.
2. `test_filler_plays_only_after_confirmed_end_of_turn`: the first filler frame is queued at the confirmed end of the turn, not before.
3. `test_filler_cancelled_by_barge_in`: with the filler playing, a barge-in silences it, clears the far end, sends no further frame, and leaves the history unchanged.
4. `test_filler_never_overlaps_real_audio`: a real clause that becomes ready mid-filler starts after the filler's last frame.
5. `test_filler_phrases_are_neutral`: no configured phrase contains an affirmation or a negation from the deny-list.
6. `test_filler_off_by_default_and_missing_files_are_silent`.
7. `test_filler_skipped_on_greeting_and_when_a_cached_answer_is_ready`.
8. `test_filler_adds_at_most_its_own_length_to_the_real_answer`.

## 3. Free spikes (run only after the recordings exist: `docs/RECORDING_PROTOCOL.md`)

| Spike | Tool | Cost | What it measures | Go / no-go |
|---|---|---|---|---|
| (a) End-of-turn replay at 700 / 550 / 450 / 350 ms | `tools/eot_replay.py` | $0, offline | Files cut off at a pause, per threshold, and the longest pause inside speech | Go: at most 2% of files cut off at the chosen threshold with speculation; no-go above 5%. Zero cut-offs in n utterances only bounds the true rate below 1 - 0.05^(1/n) (39% at 6, 9.5% at 30, 3.7% at 80, 2% at 150), so **a PASS needs about 150 clean utterances**; the 80 of the protocol give a range or a clear no-go, and below 150 the tool reports a range, not a pass. The tool counts the plain cut-off rate; speculation is judged on top of it. |
| (b) Rolling-window STT | `tools/rolling_stt_spike.py` | cap $0.20 (RunPod STT) | Final-text delay of decoding only the tail, and the word error against the whole-utterance decode | Go: final delay p50 at most 0.25 s and mean word error at most 2 points above the plain decode |

Every recording is run in two versions, reported separately: `clean8k` and `phone` (8 kHz, 300–3400 Hz band-pass, mu-law; `tools/telephone_chain.py`). The tools are tested offline with synthetic signals and a fake recogniser. Neither has touched a real recording or endpoint yet. The recording set is 80 recordings plus 3 room tones: 30 scripted lines (the owner's 12 caller lines, 3 pause lines, noise and speakerphone lines), 20 extra pause lines and 30 open answers (see `docs/RECORDING_PROTOCOL.md` and the one-page sheet per speaker in `docs/RECORDING_SHEET.md`). `tools/recording_intake_check.py` checks the files first (free, ids and counts only).

## 4. Pod spikes, ordered by cost

All three need one temporary RunPod **account** key; the endpoint keys cannot create Pods (401). **No key is requested yet.**

| Order | Spike | Pod | Cap | Deadline | Balance needed at start | Account permissions |
|---|---|---|---|---|---|---|
| 1 | Transport: round trip server → Pod WebSocket → server (Phase 4 question) | the cheapest 24 GB-class Pod; at $0.000192/s a $0.10 cap is about 520 s | $0.10 | 8 min | $0.35 | Pods: create, read, stop, terminate. Billing/account: read (balance). Nothing else. |
| 2 | Brain: first-token and first-clause time with prefix caching on a persistent GPU | a GPU large enough for the Brain model (assumed 48 GB class); at the Brain's $0.000486/s a $0.50 cap is about 1,030 s, and model loading may take much of it | $0.50 | 15 min (abort if the model is not loaded by minute 8) | $0.75 | same as above |
| 3 | Voice: chunked Chatterbox first-audio time (Phase 3, prepared) | 24 GB; $1.00 cap is about 5,200 s | $1.00 | the launcher's own hard deadline | $1.25 | same as above |
| (4) | Another streaming TTS engine | not a RunPod Pod: needs the vendor's key | $0.50 | n/a | n/a | vendor key only; asked for later |

- **Total of the three Pod caps: $1.60.** The launcher refuses to start if the balance is below the cap plus $0.25. With a balance of about $1.5 each spike can start on its own, but if all three run the balance before spike 3 can be below $1.25, so a top-up of about $0.50 is needed first (A).
- **Permissions:** create the key as restricted if the console offers per-resource scopes (Pods read/write; billing read; every other scope none). I could not verify the exact labels in the console. If only a full-access key is offered, create it for one session and revoke it right after. Put it in a file outside the repository; never paste it in a chat.
- **Spend limit:** RunPod is prepaid, so the balance is the hard ceiling: keep auto-pay off and top up only the amount needed. If the console offers an account or key spending limit, set it to the cap of the spike being run. On top of that the launcher enforces the cap, a hard deadline and verified deletion of the Pod.
- **Risk of spike 1:** a Pod's HTTPS proxy may add latency that a production connection would not have. The spike reports the proxy and, if possible, a direct TCP port separately.

## 5. Path 2: intent router, shadow mode first (design only)

**Goal.** Repeated, static questions (hours, address, how to enrol) are answered from a short reviewed list with pre-generated audio, skipping the Brain and the voice model.

**Shadow mode.** The router runs on the STT text of live calls and **plays nothing**. For each turn it records only: the matched intent id (or "none"), the best and second-best score, and a timestamp. No transcript, no audio, no phone number, no caller identity. A human samples a few matches and compares them with what the normal path answered. Flag per tenant, default OFF.

**Matching safely.** Best score above a threshold and a margin over the runner-up; utterances with numbers, names, dates, fees or course names never match; negations and unusual question words fall back to the normal path; only intents marked static can match.

**Answers.** Written or approved by a counsellor, versioned with an owner and an expiry date, kept outside the code. Audio is pre-generated with the voice endpoint and checked by listening. The Brain may draft from the knowledge base, but only reviewed text ships.

**Preventing a wrong match.** Before shadow mode ends: an offline set of at least 200 labelled questions including near-misses, with zero false matches at the chosen threshold; per-intent and per-tenant switches; any doubt falls back to the normal path.

**What real callers need before any call is analysed** (practical checklist, **not legal advice**; have it reviewed by the data-protection adviser):
1. **A notice at the start of the call** that it is handled by an automated assistant and may be analysed to improve the service, in the caller's language.
2. **A stated purpose and a retention rule:** per-turn rows (intent id, scores, time) are deleted after 7 days (A); only daily counts per intent are kept.
3. **A way to opt out:** a caller who asks not to be analysed is excluded from the shadow log.
4. **No new personal data:** no transcript, no audio and no number is stored by the router; the existing rule that logs never carry words stays.
5. **Written sign-off** from the owner (and counsel, if the owner wants it), and an updated privacy policy, before the flag is turned on for real traffic.
6. **No analysis of the supervised test calls beyond the test caller's own number** until this is in place.

**How many questions cover most calls: unknown.** The development database holds 13 AI sessions with 19 turns in total (5 sessions have none), all from our own test calls. At least about 300 real turns are needed before sizing; shadow mode produces them at no cost, as counts per intent id, from which the cumulative coverage curve is read.

## 6. Paths compared

| Path | Build time (A) | Cost | First sound | Answer |
|---|---|---|---|---|
| Today, warm, streaming defaults | done | workers only | about 7.1 s (M/A) | about 7.1 s |
| Path 1 filler | 2–3 days | a few cents to generate; $0 to run | about 1.05 s at 700 ms silence (A) | unchanged |
| Path 2, matched questions | 1–2 weeks incl. content review | CPU embedding and audio storage | about 2.6 s today, about 1.5 s with streaming STT (A) | same as first sound |
| Path 2, other questions | | | today's | about 7.1 s |
| All stage spikes pass | weeks | spikes about $2.30 in total; always-on workers $2,255/month, business hours $814/month at one worker each (assumed prices) | the filler is still needed for under 1 s | about 1.3–1.7 s (A) |
