# Latency, next steps (26 Oct 2026): the last call, the cached acknowledgement, a short first clause, a keep-alive, the table

Status: **offline work only.** No paid call, no Pod, no key request, no config or database change, no restart, no commit. Every flag stays as it was.
Labels: **MEASURED** (a stored trace or a file in `docs/evidence/`), **RECONSTRUCTED** (computed offline from stored data with the live code), **ESTIMATE** (arithmetic or an assumption: replace it with a measurement before relying on it).
Numbers only: no caller or reply word, no phone number and no id appears here. The per-turn data is in `docs/evidence/last_call_trace_2026-10-26.json`.

## 1. The last call, turn by turn

Source: the stored trace of the last session (7 turns; turn 0 cold, turns 1-6 warm). Warm medians: voice to voice **7.51 s** = silence window 0.70 + speech-to-text 1.48 + Brain first clause 0.98 (first token 0.74 + 0.24 more to the first clause) + voice 4.38 (+ 0.001 to the first frame).

| Turn | First clause, words (RECONSTRUCTED) | Audio it made, s (ESTIMATE) | Voice queue, s (MEASURED) | Voice run, s (MEASURED) | Voice total, s (MEASURED) | Remainder = total - queue - run, s | Gap since the previous turn ended, s (lower bound, wall clock) |
|---|---|---|---|---|---|---|---|
| 0 (cold) | 10 | 3.6 | 7.92 | 2.70 | 11.44 | 0.82 | none |
| 1 | 8 | 2.3 | 0.84 | 1.96 | 3.74 | 0.94 | 27.9 |
| 2 | 14 | 4.5 | 0.13 | 3.23 | 4.57 | 1.21 | 11.7 |
| 3 | 8 | 2.6 | 1.11 | 2.16 | 4.19 | 0.91 | 6.8 |
| 4 | 10 | 3.8 | 1.09 | 2.81 | 5.33 | 1.43 | 7.3 |
| 5 | 9 | 3.2 | 1.14 | 2.50 | 4.64 | 1.00 | 7.0 |
| 6 | 9 | 2.8 | 1.11 | 2.26 | 4.13 | 0.77 | 18.8 |
| **warm median** | **9** | **3.0** | **1.10** | **2.38** | **4.38** | **0.97** | |

How each column was obtained, and what it cannot say:
* **Words (RECONSTRUCTED).** The trace does not store clause sizes (`TurnTrace.stream` is deliberately left out of the stored record, `app/ai/turn_trace.py:39`). The stored reply of that build was run through the live segmenter with the tenant's live settings (first 4, later 12, maximum 14). The result is what the segmenter does with that text; it assumes the Brain produced the same text in the same order. The clause words themselves are not printed anywhere.
* **Audio length (ESTIMATE).** Not recorded: the worker returns `timing.audio_s` (`deploy/tts_worker_runpod/handler.py:265-269`) and the client throws it away (`app/ai/clients.py:650-661`). The estimate inverts the measured fit `run = 0.665 s + 0.571 x audio_s` (30 rows, variant A of `phase0b_tts_v3.json`); the fit `audio_s = 0.62 + 0.299 x words` gives 3.0 to 4.8 s for the same clauses, the same order of size.
* **Queue and run** are RunPod's own `delayTime` and `executionTime` of the first finished voice job of the turn (`turn_trace.py:51-55`). With a prefetch depth of 2 this is almost always clause 0.
* **Gap.** Not recorded per voice job. The column is the time between the end of the previous turn's audio and this turn's voice job being sent, taken from the host wall clock (which steps by tens of seconds on this WSL host): an order of magnitude only. It is a lower bound on the gap since the previous voice job (the previous job ended before its audio did).

### Is the first clause about 4 words? No: 8 to 14, median 9.
`stream_first_min_words` (default 4, `app/ai/streaming/config.py:25`) is only the **lower** bound. The segmenter cuts at the first `, ; : । . ? !` that follows at least that many words (`segmenter.py:139-149`), or at `stream_max_words` (14) when there is no punctuation (`segmenter.py:150-151`). The Brain's replies start with a one-word "जी," and then run to the first full stop, so the first boundary after 4 words is late. What decides the size is therefore the Brain's punctuation, bounded by two settings: `stream_first_min_words` below and `stream_max_words` above. Setting the minimum to 2 would have changed only 1 of the 6 warm turns (turn 3: 8 words to 2), see section 3.

### Where the 0.97 s remainder goes (all of it is before `t_tts_first_chunk`)
| Step | File and line | Time | |
|---|---|---|---|
| Text front end (numbers, language tag) | `clients.py:617-623`, `tts_frontend.py:144-169` | 0.003 ms | MEASURED offline |
| POST `/run` to RunPod (the job id comes back) | `clients.py:249-251` | **0.32 s** (median of 30 benchmark jobs) | MEASURED |
| Waiting and polling `/status`: first poll after 60 ms, then 90, 135, 150 ms (the cap `runpod_poll_cap_s`, default 0.15, `clients.py:261,267-280`, `streaming/config.py:26`); each poll is one round trip to the RunPod API; the finished result (20 to 60 KB of base64) rides back on the last poll | `clients.py:272-280` | **0.51 s** median, p90 0.80 s (benchmark overhead minus submit; median 9 polls) | MEASURED |
| base64 decode, mu-law to PCM16 | `clients.py:656,660`, `codec.py:30-32` | 0.04-0.11 ms and 0.01-0.02 ms for 1.5-5.5 s of audio | MEASURED offline |
| Playout processing (level match, fade), pre-buffer, first frame | `playout.py:124-143,181-188`, `plivo_sink.py:26-44` | 0.1-0.4 ms; pre-buffer wait 0 (a clause is longer than 150 ms); enqueue to first write 1.3 ms median | MEASURED |
| Not separable from the stored trace | | 0.1-0.2 s (live 0.97 s against 0.87 s from the benchmark fit for 9 words) | |

So the remainder is **network and polling**, not the codec, and not the 150 ms pre-buffer. The same overhead sits around the speech-to-text job: client time 1.48 s against 0.14 s queue + 0.64 s run leaves about 0.7 s.

### The queue: 1.1 s in 5 of 6 warm turns
The same 1.1 s appears in the offline evidence after idle gaps (`phase0b_cold_starts_v2.json`: 1.11 s and 1.12 s after 150 s gaps) and the value is 0.14-0.15 s for back-to-back jobs (`phase0b_tts_v3.json`, median 152 ms). In the 6 live turns the gap was always at least 6.8 s and the queue was 1.1 s, except turn 2 (0.13 s after 11.7 s, which the wall-clock caveat cannot explain either way). **A ping can only help if the pickup is about recent use of the worker, which is not proven** (section 4).

## 2. Cached acknowledgement

### What is built (offline, in this worktree, flag default OFF, nothing generated)
| Part | Where |
|---|---|
| Tenant flag `ack_enabled` (default false) and wait `ack_after_ms` (default 300, range 0-3000); API model fields added to `AiConfigIn` | `app/ai/service.py`, `app/ai/streaming/config.py`, `app/api/routes_platform.py` |
| Directory setting `ai_ack_dir` (default empty), outside the repository | `config/settings.py` |
| Loader, neutrality deny-list, rotation, the proposed phrases | `app/ai/streaming/ack.py` |
| Playing it on the same timeline as the answer | `app/ai/streaming/turn.py` (`_ack`), `playout.py` (a clause with `real=False`) |
| Wiring, trace field, no effect on "voice to voice" | `app/ai/streaming/live.py`, `app/ai/ws.py` |
| Tests, labelled SIMULATED | `tests/unit/test_ack.py`, `test_ack_turn.py`, `tests/integration/test_ack_live.py` |

Rules, and the test that holds each one:
* **2-3 neutral phrases, rotated, never the same twice in a row.** The set needs at least 2 clips per language (`hi`, `en`, chosen by the turn's reply mode); `AckSet.pick` excludes the clip played last. Tests: `test_the_same_clip_is_never_played_twice_in_a_row_and_all_are_used`, `test_two_clips_alternate_strictly`.
* **Nothing that could claim agreement.** Each clip has its text in `manifest.json`; the text is checked against a deny-list (yes/no/ok/sure/right/true/fine, "of course", Roman and Devanagari haan, ji, nahin, theek, sahi, bilkul, achha, zaroor ...). One offending phrase refuses the **whole set** (the call goes on without it). Tests: `test_the_proposed_phrases_are_neutral`, `test_phrases_that_could_claim_agreement_are_caught` (18 cases), `test_a_set_with_one_non_neutral_phrase_is_refused_as_a_whole`.
* **About 1 s each, ulaw8k, outside the repo, path in a setting.** Files must be 0.3-2.0 s of raw 8 kHz mu-law; names cannot leave the directory. Tests: `test_clips_must_be_about_a_second`, `test_file_names_cannot_leave_the_directory`.
* **Played right after the speech-to-text final if the first real clause is not ready within a configurable time.** The wait is counted from the STT final (`ack_t0`, `live.py`), not from the turn start; if the first real clause is already handed to the playout, nothing is played and the rotation is not touched. Tests: `test_a_slow_first_clause_gets_the_acknowledgement_after_the_wait...`, `test_the_wait_is_counted_from_the_speech_to_text_final...`, `test_a_fast_first_clause_needs_no_acknowledgement...`, `test_a_fast_voice_gets_no_phrase`.
* **Never overlapping real speech.** One playout timeline: a real clause that becomes ready while the phrase plays waits for it (at most its length) and never cuts it. Test: the `accounts[0].start >= accounts[ACK].samples` assertion in the first test above.
* **Cancelled by barge-in.** The same `silence` then `clearAudio` then cancel path as any audio (`test_barge_in_during_the_acknowledgement_silences_it...`). If the caller talks over the phrase before any answer audio exists, the question is **merged**, not lost (`ws.py: merge_if_only_acknowledged`; integration test `test_talking_over_the_phrase_stops_it_and_the_question_is_merged_not_lost` checks one question in the history and a longer last STT upload).
* **Never written to history, never counted as heard.** It has no entry in `clause_texts`, so `truncate_spoken`, the history and the stored `reply` never contain it (`test_the_acknowledgement_is_never_part_of_the_reply...`, `test_a_slow_voice_gets_the_phrase_first_and_it_is_not_in_the_history_or_the_record`, which also checks that no phrase word reaches a log or the stored record).
* **Skipped if the files are missing.** `AckSet.load` never raises and returns a reason code (`no_dir`, `no_manifest`, `bad_manifest`, `bad_clip`, `not_neutral`, `too_few_clips`); the call proceeds without it (`test_missing_files_with_the_flag_on_never_break_the_call`).
* **It does not hide the real number.** `voice_to_voice` stays the first frame of the **answer**; when a phrase was played the stored trace gains one numeric field, `t_ack_first_frame`. A turn that played only the phrase and never got an answer is still an error (`test_when_the_answer_never_comes...`). Waiting for the first real clause after the phrase is not counted as an underrun.

### The audio (NOT generated: waiting for your APPROVED and a cap)
How: five one-off voice jobs on the live voice endpoint with the tenant's own settings: `output_format ulaw8k`, voice `utkarsh`, no reference upload (the same request the live path sends, `clients.py:626-640`), `language` hi for three phrases and en for two; the answer's `audio_base64` is written raw to `ai_ack_dir/hi_1.ulaw ...` and `manifest.json` is written next to it. I would add a 40-line tool modelled on `tools/phase0b_tts_remeasure.py` (hard cap, ledger, refuses to run without the cap on the command line); I have **not** written it, so nothing can spend by accident. You listen to the five clips before the flag is ever turned on.

Proposed phrases (`ack.PROPOSED`, gender neutral, none in the deny-list, you choose the final text): Hindi "एक सेकंड।", "एक पल।", "बस एक क्षण।"; English "One moment.", "Just a second."

Cost, ESTIMATE from the repo's own ledger rate (USD 0.000192 per second): a warm worker is about 1.7 s per job plus the 60 s idle tail the ledger assumes: 5 x 1.9 + 60 = about 70 s = **USD 0.013**; if the worker is cold add the 41 s measured on 25 Oct: **USD 0.021**; best-of-two takes (10 jobs): up to **USD 0.025**. Proposed cap: **USD 0.05**. I will not start without "APPROVED" and the cap you want.

### What it does to the call, ESTIMATE
First sound moves from 7.5 s to about **2.4-2.7 s**: 0.70 silence + 1.48 STT + `ack_after_ms` (0.3, or 0) + about 0.2 s call leg (assumed in `LATENCY_FEASIBILITY_PLAN.md`). The real answer does not change (7.5 s), so with today's numbers there is a **silent stretch of about 3.8 s** between the end of a 1 s phrase (at about 3.7 s) and the answer: the phrase hides the wait only partly. The plan in `LATENCY_FEASIBILITY_PLAN.md` plays its filler at the confirmed end of the turn (about 1.05 s); that variant needs the phrase to start before the speech-to-text and is not what was asked here.

## 3. Short first clause (DRAFT, nothing applied)

### 3.1 Brain rule (to append to `config/devanagari_rules.txt`, which the Devanagari prompt reads, `pipeline.py:48-56`)
> Start every reply with a very short first sentence of 2 to 4 words that ends with a comma or a full stop and says something true already (a lead-in to the answer, or the answer itself when it is that short). Then continue in the normal sentences. The first sentence must not agree or disagree before you have read the CONTEXT. Write it in the reply language and script like everything else. Right: "जी, बताता हूँ।"  "देखिए, फीस ₹1,25,000 है।" Wrong: a first sentence of 8 or more words; a first sentence in Roman letters for a Hindi reply.

The tenant is on the Devanagari prompt (`devanagari_hindi` true, `script_enforcement` off), so this is the file to change; the other prompts (`build_system_prompt`, rule 3 "Voice Constraints") would need the same sentence.

### 3.2 Segmenter
* **Setting that exists today:** `stream_first_min_words = 2` (tenant key, range 1-8). Offline, on the 6 warm replies it gives first clauses of 8, 14, 2, 10, 9, 9 words: **only turn 3 changes**. The Brain rule is the main lever; the setting only lets a 2-3 word first sentence be cut early.
* **New setting (draft, not applied):** `stream_first_max_words` (default None = same as `stream_max_words`), used only for clause 0:
```diff
--- a/app/ai/streaming/segmenter.py
@@ class SegmenterConfig
     first_min_words: int = 3
+    first_max_words: int | None = None   # clause 0 is cut at a word boundary at this size when it has no punctuation (protected spans still win)
@@ def _find_cut
-        minw, maxw = self._min(), self.cfg.max_words
+        minw = self._min()
+        maxw = (self.cfg.first_max_words or self.cfg.max_words) if self.emitted == 0 else self.cfg.max_words
```
  plus `stream_first_max_words: (2, 14)` in `STREAM_KEYS` and `streaming/config.py`. With a cap of 6 the six replies would start with clauses of 6, 6, 2, 6, 6, 6 words, but a cut at word 6 falls **inside a phrase**, which costs prosody: use it only as a safety net behind the Brain rule.

### 3.3 Interaction with the Devanagari script check
* The first clause is the **only** clause gated before it is spoken (`turn.py:272-300`): `check_reply` minus the whole-reply rule "Devanagari must be present" (`turn.py:250-258`), then **one** rewrite by the Brain with a 1.0 s limit (`turn.py:38,260-270`) **before any audio exists**. A 2-4 word first clause is mostly lead-in words ("जी", "देखिए"): if the Brain writes one in Roman letters, the check fails and the rewrite can add up to 1.0 s, which cancels the gain. Draft rule: for a first clause of at most 4 words skip the Brain rewrite and use `enforce_clause` (the verified lexicon, `script_enforcement.py:296-332`; no network); if that does not fix it, speak it as it is and log the kind and count only. **Decision for you:** it trades a possible Roman-letter pronunciation of 1-2 words for up to 1 s.
* The minimum-word count skips the one-word "जी," (1 < 2), so with a minimum of 2 the first cut still waits for the next punctuation: the Brain rule must put that punctuation at word 2-4.
* The numeric and unit protection (`segmenter.py: no_break_after`) is unchanged: "₹5,000" or "30 June" can never be split, so a first sentence made of an amount stays whole.
* A short English-only first clause ("B.Tech, MBA।") gets its voice language tag from `detect_lang` per clause (`clients.py:621-622`, `tts_frontend.py:144-149`): on 2-4 words that can flip to English inside a Hindi reply. Draft fix: for clauses under 5 words pass the turn's reply mode instead of detecting.

### 3.4 The catch: the second clause
Prefetch depth 2 sends clause 0 and clause 1 to the same single-job worker, one after the other. Today clause 0 is 9 words (about 3 s of audio) and clause 1 is ready about when it ends. With a 3-word clause 0 (1.5 s of audio, run 1.5 s) clause 1 (12-14 words, run about 3.5 s) is ready at about 7.0 s while clause 0 ends at about 5.0 s: **a gap of about 2 s inside the reply** (ESTIMATE from the fits above). Therefore the draft needs a **ramp**: clause sizes about 3, 6-8, then 12-14 (`stream_second_max_words`), so each clause is generated while the previous one plays. Without it, the saving at the start becomes a silence in the middle.

## 4. Keep-alive ping during a call (built offline, flag default OFF)

What it is: while a call's media is connected, a small task sends the voice worker's own no-op job (`{"input": {"warmup": true}}`, `handler.py:196-198`: it answers `{"ok": true}` without synthesizing) every `keepalive_interval_s` (default 5 s, range 2-30) for at most `keepalive_max_s` (default 300 s, range 30-900). Code: `app/ai/keepalive.py`, `ModelClients.ping_tts` and `last_tts_at` in `clients.py`, wiring in `service.py`; tenant flag `keepalive_enabled` (default false) and only for a RunPod `/runsync` voice URL.

* **Interval.** 5 s is a guess: the only data are 0.14 s (back to back) and 1.1 s (6.8 s or more after the last job). The paid measurement below picks the real value.
* **Cost, ESTIMATE.** A ping bills at most about 1 s of worker time: USD 0.000192; every 5 s for a 180 s call = 36 pings = **USD 0.007 per call** (the supervised-call cap). If the voice worker is an always-on **active** worker (as today) it is billed anyway and the ping adds only that. If it were a scale-to-zero worker, holding it up for the call is the real cost: **USD 0.0115 per call minute** (the same rate).
* **How it stops.** (1) `AiService._finish` stops it first, before the conversation is cancelled; (2) `media_disconnected` stops it, `media_connected` starts it again; (3) it stops by itself after `max_s`, whatever else happens; (4) it stops after 3 failed pings in a row; (5) a ping in flight is cancelled with it and the client then cancels the RunPod job (`clients.py:260-265`). Tests: `tests/unit/test_keepalive.py` (SIMULATED: virtual clock, mock ping).
* **How it avoids keeping workers up after the call.** Nothing is pinged after the first stop reason, so the worker idles out on the **endpoint's own idle timeout** (the ledger assumes 60 s; the real value is a RunPod setting I cannot read offline). That tail is the same as after any real job. A ping is also **skipped** while a real voice job ran in the last interval (`last_tts_at`): the real job keeps the worker as warm as a ping.
* **No effect on traces or metrics.** The task runs in a fresh context, so it cannot write into the current turn's trace (`test_a_ping_never_writes_into_the_trace...`); it bypasses `_call`, so it is not counted in the per-service request metrics.
* **What is not shown.** That a ping keeps the pickup at 0.14 s. One capped paid measurement is needed first (cap USD 0.03, about 3 minutes): idle the worker, then (a) one real clause after 15 s of silence, (b) pings every 5 s for 30 s and then one real clause, (c) pings every 2 s for 30 s and then one real clause; compare the `delayTime` of the three real clauses with the 1.1 s. Not run.

## 5. Pod design
Updated in `docs/PHASE4_PERSISTENT_CONNECTIONS.md` (this trace, monthly cost of one 24 GB Pod, security, client changes, risks). No key and no Pod request.

## 6. Expected savings (warm turn, median 7.51 s today; first sound today = 7.51 s)

| Change | Saves on first sound | Saves on voice to voice | Label | Basis and catch |
|---|---|---|---|---|
| Today | - | - | MEASURED | 7.51 s: 0.70 + 1.48 + 0.98 + 4.38 |
| Cached acknowledgement | to about **2.4-2.7 s** (saves about 4.8-5.1 s) | 0 | ESTIMATE | 0.70 + 1.48 + 0.3 (+0.2 call leg); leaves a silent stretch of about 3.8 s before the answer |
| Short first clause (3 words) + Brain rule | about 1.2 s (0.9-1.4) | about 1.2 s | ESTIMATE | run 2.38 to about 1.53 (fit), result and polling about 0.1-0.15 s smaller, first clause about 0.15 s earlier; **needs the ramp** (3.4) and the rewrite decision (3.3) |
| Keep-alive ping | about 0.95 s | about 0.95 s | ESTIMATE, unproven | queue 1.1 s to 0.14-0.15 s **only if** the pickup is about recent use; needs the paid measurement |
| `runpod_mode: runsync` for the voice (one blocking POST, no polling) | about 0.3-0.5 s | same | ESTIMATE | removes the poll lag and one status round trip; no runsync row exists in the evidence (all 54 rows are `run`); **no cancel** of a job in flight |
| Poll cap 0.15 to 0.05 s | 0.03-0.1 s | same | ESTIMATE | each poll costs a round trip (about 0.15-0.3 s): a lower cap cannot beat the round trip |
| Persistent connection to a Pod (voice only) | about 2.0 s | about 2.0 s | ESTIMATE | removes queue 1.10 and remainder 0.97 (leaves run 2.38 plus a local hop); with the speech-to-text on the same Pod about 0.6 s more |
| Chunked synthesis on the Pod (Phase 3) | more | more | not measured | first audio after the first chunk instead of the whole clause |

Expected totals (not additive, interactions noted above):
* **Without a Pod:** short first clause + keep-alive + runsync: 7.5 - 1.2 - 0.95 - 0.35 = about **5.0 s** voice to voice; with the acknowledgement the first sound is at about **2.5 s**.
* **With a Pod (voice) and the short first clause:** 7.5 - 2.0 - 1.2 = about **4.3 s**; first sound about 2.5 s with the acknowledgement. Under 3 s for the real answer needs the speech-to-text on the Pod, a shorter silence window (0.70 s today; the free replay spike has no recordings yet) and chunked synthesis.

## 7. Is the system armed now? Yes. (Read-only check at 15:50 IST; nothing changed.)
* Tenant policy `enabled`, **2 allowed callers**, 1 active call at most, 180 s cap (last updated today 06:17 UTC); inbound route to the AI **enabled**.
* Settings of the running service: `ai_enabled`, `plivo_inbound_enabled`, `real_calls_enabled` all true, provider `plivo_api`.
* The service (port 8800, started 15:16 IST, from this worktree) is up; an `ngrok http 8800` tunnel has been up for 2 days; FreeSWITCH is up.
* Tenant AI config: enabled, streaming pipeline on, ulaw8k on, Devanagari on; the three model URLs are set. No active call or AI session right now.
* So a call from either allowed number would be answered and **would spend** on the three RunPod endpoints. The new flags (`ack_enabled`, `keepalive_enabled`) are not in the stored config, so they are off; the running process has the **old** code in memory (it was started before these edits), so none of this work is live. `python scripts/plivo_testcall.py disarm --yes` is the existing kill switch; I did not run it.
