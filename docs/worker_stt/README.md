# STT worker test version (language per request, optional vocabulary hint)

Status: files only. Nothing here was built, deployed or run on RunPod. `handler.py` is `handler (7).py` (Downloads, 24 Oct 2026) plus four changes; which
handler is LIVE is UNVERIFIED until the operator pastes worker log lines.

## What changed against handler (7)

| change | detail |
|---|---|
| language per request | `input.language` = `hi`, `en` or `auto` (also `hinglish` = `hi`). Absent = the worker's `WHISPER_LANGUAGE` default. Explicit `auto` = auto-detect even if the default is `hi`. `ALLOW_REQUEST_LANGUAGE=0` turns the per-request option off. handler (7) ignored the request's language completely. |
| vocabulary hint | optional `input.prompt`, at most 200 characters (longer is refused), given to the decoder with `prompt_ids`. **UNVERIFIED with `mmeog/mme_stt_model`** (a fine-tuned model: its tokenizer, generation config and transformers version must support `get_prompt_ids`). If it fails the job runs WITHOUT the hint and says `prompt_used: false`; it never fails the call. `ALLOW_PROMPT=0` turns it off. |
| startup log | one line: model, cuda, default language, whether the per-request options are open. |
| timing | `timing.audio_s` (seconds of audio as received, before resampling) next to `inference_s` and `total_s`. |

Not changed: `audio_b64` (WAV), `audio_pcm16_b64` + `sample_rate` (what the app's flag `stt_raw_pcm` sends), `health`, `warmup`, greedy decoding, the 16 kHz resampling in the worker.
The worker never logs the transcript, the prompt or the audio: lengths and timings only.

`stt_logic.py` holds every decision that does not need a GPU, with the standard library only. `python docs/worker_stt/selftest.py` runs 25 checks without torch
(run on 26 Oct 2026 in the WSL venv: `worker_stt selftest: 25 checks passed`, SIMULATED = pure functions, no model).

## Build notes (a TEST endpoint only)

1. In the worker's source repository (the folder with the Dockerfile that builds handler (7); its location is UNKNOWN to me) create a branch, for example `stt-worker-test`.
2. Add `stt_logic.py` next to the handler; replace the handler with `handler.py`; make sure the Dockerfile copies both files.
3. Build and push an image with a TEST-ONLY tag, for example `<registry>/<worker>:test-only-2026-10-<n>`. Never tag it `latest` or the tag the live endpoint uses.
4. Create a NEW serverless endpoint from that tag (console action by the operator): Max workers 1, Active 0, idle timeout 60 s, same GPU class and model environment variables as the live STT endpoint, `WHISPER_LANGUAGE` empty, `ALLOW_REQUEST_LANGUAGE=1`, `ALLOW_PROMPT=1`.
5. The live STT endpoint, its image and every tenant's `stt_url` stay untouched. Point only a TEST tenant at the new endpoint.
6. Check the startup line in the worker log (`default_language=...`, `request_language=on`, `prompt=on`) before any evaluation.

## No-code alternative (no new image)

`WHISPER_LANGUAGE` is already an environment variable of handler (7). Create TWO extra test endpoints from the SAME live image: one with `WHISPER_LANGUAGE=hi`, one with `WHISPER_LANGUAGE=en` (Max 1, Active 0, idle 60 s).
This tests the language effect on accuracy and speed without touching code, but it cannot choose the language per call and gives no vocabulary hint.

## Evaluation plan (plan only; nothing run, no approval given)

Clips: the operator's own recordings (never added to the repository), a fixed list of N clips (suggest 30) with a reference transcript the operator writes. Report numbers only.
Conditions per clip, always through the same app path: A live endpoint (auto), B language `hi`, C language `en`, D `hi` + hint (a list of course and college names, at most 200 characters, supplied by the operator), E `auto` + hint.
Metrics: word error rate and character error rate against the reference (computed offline), `timing.inference_s` and `total_s`, and for D/E whether `prompt_used` is true on every clip.
Cost (ESTIMATE, GPU price per second UNVERIFIED): N x conditions x about 0.7 s of execution = 150 jobs, about 105 GPU-seconds plus one cold start (about 41 s) = about $0.05 at an assumed $0.0003 per GPU-second. A REQUEST with the exact cap follows when the endpoint exists.
Decision rule (proposed): adopt a condition only if its error rate is lower than A on at least 70 % of the clips and its median `total_s` is not more than 0.1 s higher. The hint (D, E) additionally needs `prompt_used: true` on all clips and no clip where the hint text appears in the transcript.
