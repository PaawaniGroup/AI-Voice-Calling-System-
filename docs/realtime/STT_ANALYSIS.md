# STT: what the live worker log says, and what it changes (27 Oct 2026; REAL facts from the operator's worker log, which holds no words)

## Facts (REAL, from the live STT worker log)
1. Startup line: `[STT] model=mmeog/mme_stt_model loaded in 1.7-4.2s cuda=True language=auto`. Per job: `[STT_TIMING] audio_s preprocess_s inference_s total_s text_chars`. Both match the GitHub `handler.py` the operator pasted earlier (the one with `audio_pcm16_b64`).
   **Status of the live handler: VERY LIKELY handler (7)-compatible; UNVERIFIED until one real job proves `audio_pcm16_b64`.** (The log line format matches; a log line cannot prove which request fields the code accepts.)
2. `WHISPER_LANGUAGE` is empty (`language=auto`) and the worker IGNORES the request's `language` field. Only an environment variable on the endpoint changes the decoder language.
3. Fit over 60 jobs: `inference_s = 0.13 + 0.0063 x output characters` (R2 0.94), nearly independent of the audio length. The 7.34 s utterance: 96 characters, 0.693 s of inference; the platform reported 945 ms of execution, so about 250 ms is platform time around the handler.
4. Worker start: model load 1.7-4.2 s; the warm-up step at start 1.1-17.7 s depending on the host.
5. The worker that handled call 1 booted about 4 s before the utterance reached it: the 13.46 s STT queue of call 1 was that worker's boot (load + warm-up + platform), not a busy GPU.

## What this changes
- **Compute is not the STT bottleneck.** Inference was 0.69 s of the 3.08 s STT stage (22 %). A faster engine would realistically save 20-30 % of that, 0.14-0.21 s (ESTIMATE): **dropped** (operator decision: under 0.2 s is not worth a new engine).
- The rest is gateway and transfer: the fixed submit (about 0.35 s MEASURED on 215-byte voice requests), the upload of the audio (the audio travels as base64 in THREE fields today: 470 KB for 7.34 s), the final status request (0.22 s: a gateway round trip), the polling slack (0.06-0.28 s) and about 0.25 s of platform time.
- **STT cold start, MEASURED: 21-32 s** (the STT wake job of 27 Oct: 20.9 s queue, 23.6 s wall; the operator's measurements 21-32 s), and **13.46 s on call 1** (the worker had begun booting about 4 s before the utterance arrived). The worker log explains the spread: load 1.7-4.2 s plus a warm-up step of 1.1-17.7 s depending on the host, plus platform time. The first utterance of any call on a scaled-to-zero STT endpoint pays it. Active workers = 1 for the speech endpoint during a supervised call is the only fix for that.

## Revised STT time model (ESTIMATE; one MEASURED job calibrates it; the upload rate comes from one STT job against three tiny voice requests)
`T = 0.35 (submit, fixed) + u x L + 0.38 + 0.0063 x chars + 0.22 (final status request) + 0.15 (polling slack) + queue`, i.e. `T = 1.10 + u x L + 0.0063 x chars + queue`, with L = seconds of audio, chars = output characters (about 13 per second of speech, MEASURED 96 per 7.34 s), u = upload seconds per second of audio: **0.09 with the three base64 copies, 0.03 with the raw PCM field** (1.4 ms per KB of body, from 470 KB taking about 650 ms more than a tiny request). Check: L = 7.34, chars 96: 1.10 + 0.66 + 0.60 = 2.36 + queue 0.84 = 3.20 s against 3.08 s MEASURED (+4 %). The upload dominates the length dependence; the compute hardly depends on length.

### `stt_raw_pcm` saving, revised
Body 470 KB -> about 157 KB (one field instead of three). Saving `(0.09 - 0.03) x L` = 0.06 s per second of audio: **0.30 s at 5 s, 0.44 s at 7.34 s, 0.60 s at 10 s** (ESTIMATE; linear in the body size; from a cloud host with a faster uplink than the home PC the saving shrinks). A mu-law upload (half the bytes again) would save a further 0.1 s or so but needs a worker change (UNVERIFIED, not planned).
Safety: the flag stays default OFF; draft with an automatic ONE-SHOT FALLBACK (below).

### The 6 s utterance cap, revised with the fit (excluding the queue; "merged" = how it works today)
| question | today (merged, 3 base64 copies) | + raw PCM | tail only (3 copies) | raw PCM + tail only |
|---|---|---|---|---|
| 5 s, 65 chars (one job, no cap) | 1.96 s | 1.66 s | n/a | n/a |
| 7.34 s, 96 chars (cap + 1.34 s tail) | 2.36 s | 1.92 s | 1.33 s | 1.25 s |
| 10 s, 130 chars (cap + 4 s tail) | 2.82 s | 2.22 s | 1.79 s | 1.55 s |
Saving against today: raw PCM alone 0.44 s / 0.60 s; tail only about 1.0 s at both lengths; both together 1.1 s / 1.3 s (ESTIMATE). Tail-only needs a natural-pause cut at the cap, the first piece's STT NOT cancelled by the continuing speech, and a safe join of the two texts. Raising the cap changes the time little (the merged job is the same size); its saving is the wasted first job's contention (call 1: the second job queued 0.84 s against about 0.1 s typical: 0-0.8 s, ESTIMATE). The earlier model (exec about 0.13 s per audio second) is replaced by this one: compute is per output character, the length cost is upload.

## Language test plan (auto vs hi vs en) -- a REQUEST later; nothing is created or run
- Why: language=auto lets the decoder pick; a forced `hi` or `en` may help or hurt Hinglish and English terms (UNKNOWN: this is a fine-tuned model). The request's `language` is ignored, so the only way is an environment variable on the endpoint.
- Conditions: A = the existing endpoint (WHISPER_LANGUAGE empty); B = a new test endpoint from the SAME image with WHISPER_LANGUAGE=hi; C = another with WHISPER_LANGUAGE=en. Both new endpoints: min workers 0, max workers 1, idle timeout 5 s (the operator's console actions; deleted afterwards).
- Inputs: the operator's recordings (none exist yet: needs the 12-line recordings folder outside every repository, checked free with `tools/recording_intake_check.py`).
- Jobs: 12 recordings x 3 conditions x 2 repeats = 72 jobs (24 per endpoint), in one session, one tool run (REAL mode of the harness or a small script: to be written offline, counts only).
- Scoring: expected terms per line from the operator's own reference file (outside the repo); printed: per condition the counts of terms found, of lines identical across repeats, the language field returned, `[STT_TIMING]`-style numbers. No transcript is printed or stored.
- Cost, ALL THREE endpoints (ESTIMATE; Billing is authoritative). Idle timeouts: existing endpoint 300 s, the two new ones 5 s. Billed seconds: each new endpoint = cold start 21-32 s (MEASURED; 32 s used) + 24 jobs x about 1.0 s + idle tail 5 s = **61 s** (x2 = 122 s); the existing endpoint = 24 jobs x about 1.0 s + idle tail 300 s = **324 s** if it is already warm (**356 s** if it starts cold: + 32 s). Total **446 s** (478 s if the existing one is cold).
  | | $0.00019/s | $0.0003/s |
  |---|---|---|
  | 446 s (existing endpoint warm) | $0.085 | $0.134 |
  | 478 s (existing endpoint cold) | $0.091 | $0.143 |
  Cap: **$0.20** (the worst case $0.143 plus room for one retried job and for idle-timeout rounding). Needs from the operator: the recordings, the two endpoints (console), the key files for them, "APPROVED:" with the exact action and cap.
  (If the existing endpoint's 300 s idle timeout were lowered to 5 s for the test the total would be 61 x 2 + 29 = 151 s: $0.029 / $0.045; not assumed.)

## Draft: `stt_raw_pcm` with an automatic one-shot fallback (callbuild; compile-checked, tests written, NOT RUN)
- On a worker ERROR that names the audio fields (a failed job or HTTP 400/422 whose text contains `audio_b64`, `audio_pcm16`, `audio_base64`, `pcm16` or "provide audio"), the client retries ONCE with the old three-field body and keeps the flag off for the rest of THAT call (the clients object is per call). Timeouts, unreachable endpoints, auth errors and 5xx never trigger it (retrying those would hide an outage). The retry is one-shot: a second failure is raised as usual.
- Evidence: the retry job carries `raw_fallback: true`; the turn carries `turn.stt_raw_fallback = 1`; the log line names the flag and the error kind only (no content, no audio).
- Tests (`tests/unit/test_stt_raw_pcm_fallback.py`, SIMULATED with a mock worker that rejects the new body): a FAILED job and an HTTP 400 both trigger one retry and then the old body for the next utterance of the call; an unrelated error (out of memory) and an unreachable worker do not; two rejections raise after exactly two attempts; flag off never retries; a worker that accepts causes no fallback; a new call tries the raw body again; the classification of errors.

## REQUEST (not run): MATCH TEST of the raw PCM body
- Action: `RUNPOD_STT_KEY=<key file content, this process only> python tools/realtime/stt_match_test.py --clip <WAV outside every repository: mono, 16-bit, 8000 Hz, the operator's OWN voice, a few seconds> --approved "<id>" --cap-usd 0.03` (built on `callbuild`, SIMULATED tests written, NOT RUN).
- Two STT jobs on a WARM speech worker with the SAME clip: job 1 the old three-field body, job 2 the raw body (ONE field `audio_pcm16_b64` + `sample_rate` = 8000; the automatic fallback is active, and if it fires the verdict is FAIL because the raw body was not what was tested).
- Printed (numbers only, no text): the character-level similarity of the two transcripts (difflib ratio after collapsing whitespace), the character counts, and both jobs' timings (body bytes, submit, queue, execution, polls, total).
- **PASS = similarity >= 0.95** (and both jobs ok, no fallback, job 1 queue under 5 s). A cold worker (queue 5 s or more) stops after job 1 and concludes nothing (NOT_EVALUATED). The flag `stt_raw_pcm` stays OFF unless it passes; the tool never changes a flag.
- Cost (ESTIMATE): two jobs of about 1 s plus gateway on a worker that is already warm: about 5 s of billed time at most = $0.001 at $0.00019/s, $0.002 at $0.0003/s; the warm worker itself (Active = 1) is billed by the console setting, outside this request. Cap $0.03 (the tool stops when (elapsed + 5 s) x price reaches it; a cold start is never paid for beyond job 1).
- Minimum the operator must do: record the clip (their own voice), set the speech endpoint Active = 1 and wake it, give the key file, reply "APPROVED: run stt_match_test.py once, cap $0.03, key file <path>, clip <path>", set Active back to 0.
- Why `sample_rate` matters: the live handler defaults a missing `sample_rate` to 16000 and would silently decode 8 kHz audio as garbage. The client therefore ALWAYS sends `sample_rate` equal to the capture rate (8000 for Plivo) with the raw body, never sends the raw body without a usable rate, and tests fail if the body omits it or carries another value.

## Cap decision: only the cheap cap raise is drafted (tail-only waits for recordings)
- Per-tenant flag `utterance_cap_raise` (default OFF, in the config allowlist) + value `utterance_max_s` (6.0-12.0, default 6.0 = today). With the flag ON, one utterance may last up to that maximum before the cap splits it; the 700 ms silence rule is unchanged; the merge limit (15 s) is unchanged. Switching the flag on without setting a value changes nothing.
- Draft state: code and tests written on `callbuild`, compile-checked, NOT RUN, NOT COMMITTED. Expected effect (ESTIMATE): no change in STT time for a given length (one job on the whole audio either way); it removes the cancelled first job and its contention (0-0.8 s on turns longer than 6 s) and the merge-window dependence. Risk: more buffering for noisy lines, which the hard maximum bounds.
- Tail-only (decode only the new tail, keep the first piece's text) waits for the recordings.