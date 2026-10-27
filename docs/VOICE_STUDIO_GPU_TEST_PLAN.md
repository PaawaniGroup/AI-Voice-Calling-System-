# Voice Studio worker: real-GPU test plan (PLAN ONLY, nothing here has been run)

Nothing in this plan may start without a message from the owner that contains **APPROVED** plus the exact action (for example "APPROVED: push tag v5-studio from branch tts-voices",
"APPROVED: create the test endpoint with max 1 worker", "APPROVED: run the measuring script with MAX_USD=1.50"). Each step below needs its own approval.
No real voice audio is used: reference clips are generated tones/noise made by the script itself. The live endpoint and the armed state are never touched.

## 0. Why a separate endpoint
The live endpoint serves calls. The test uses a NEW serverless endpoint (name `mme-tts-studio-test`), its own endpoint id and API key, so a bad image, a stuck job or the LRU test cannot affect calls,
and its spend can be read on its own. The app is **not** pointed at it: the script talks to RunPod directly (a throw-away tenant is not needed). The tenant's `tts_url` is not changed.

## 1. Build (needs approval; the repo worktree is not used to build)
1. In the worker's GitHub repo (`aitechteammme-dev/mme-tts-runpod`), branch `tts-voices` (never `main`: pushes to main publish `latest`/`v2`).
2. Replace `handler.py` with `deploy/tts_worker_runpod/handler.py` from this worktree (the whole file). `requirements.txt` keeps `scipy>=1.11`. No other file changes (the Dockerfile still copies only `handler.py`).
3. The workflow `build-image-v3-test.yml` only pushes the tag `v4` and a test asserts that. A new tag needs an approved change to both the workflow and that test; proposed tag: `v5-studio`.
   The image `latest`, `v2`, the broken `v3` and the accepted `v4` are never overwritten.
4. Before pushing: `python handler.py --self-test` on a machine that has numpy, scipy, soundfile (it is not possible in the service's venv). Expected: all checks pass, including the new "voice tasks" block. If it cannot be run, say so and do not push.
5. Create the endpoint from `ghcr.io/aitechteammme-dev/mme-tts-runpod:v5-studio`: GPU type as the live endpoint (to make timings comparable), **max workers 1**, **min workers 0** (so cold start is measured), idle timeout 5 s for the cold-start run and 60 s for the others,
   execution timeout 120 s, environment: `MAX_REGISTERED_VOICES=4` (small on purpose, so the eviction test needs few clips), `MAX_REGISTER_BYTES` default, `ALLOW_REQUEST_REF` unset, `DEFAULT_VOICE=utkarsh` with a **generated** baked reference (a tone file, never `ref_voice.wav` of a real person) so the test image holds no real voice.

## 2. Caps (all enforced by the script itself and by RunPod settings)
* Max workers 1; endpoint spend limit set in RunPod to the approved amount if the console offers one.
* The script takes `--max-usd` (required, no default) and the price per GPU second from the owner; it keeps a running total from `delayTime + executionTime` of each response and **stops before the next job** when the total reaches 80 % of the cap.
* At most 60 jobs and 20 minutes of wall time in all; each job has a 120 s client deadline and is cancelled (`/cancel/<id>`) when it passes it. Every status poll is free; polling is 1 s.
* The script runs at most one job at a time, and prints the running total after every job.
* Stop file: creating `STOP` next to the script ends the run after the current job.

## 3. What is measured (each with the number of repetitions)
| # | Measurement | How | Repetitions |
| --- | --- | --- | --- |
| 1 | Cold start of the new image | `{"task":"voices"}` after the worker was idle past the idle timeout; read `delayTime`, `executionTime`, and the worker log line `[TTS] ready: ...` | 3 |
| 2 | `register_voice` time per voice | register generated clips of 3 s, 10 s, 30 s under names `t_a ... t_d`; read `prepare_s` from the answer and `executionTime`; compare 1st, 2nd, 3rd, 4th voice (does it grow?) | 4 voices x 3 lengths |
| 3 | Same name + same clip again | must answer `cached: true`, `prepare_s: 0` and `executionTime` far below a register | 3 |
| 4 | Voice switching | alternate `t_a`, `t_b` 20 times with the same short text; compare `generate_s` with 20 requests of a single voice | 2 x 20 |
| 5 | `needs_reference` path | synth with a never-registered name: expect `code: needs_reference` in well under a second of GPU time; then register and synth again; total time of the three jobs | 3 |
| 6 | Eviction | with `MAX_REGISTERED_VOICES=4`: register 6 voices; the first two must be evicted (`voices` task shows 4 registered, `evicted` lists them), a synth of an evicted voice answers `needs_reference`, the baked voice is still there | 1 |
| 7 | Memory with N voices | read GPU memory from the worker log after each registration (add a `nvidia-smi --query-gpu=memory.used` line to the test image's log through the RunPod console log view or `torch.cuda.memory_allocated()` printed by the handler's `[TTS_REGISTER]` line once approved) for N = 0, 1, 2, 4 | per step |
| 8 | Worker restart | stop the worker (idle timeout), then synth a registered voice: must answer `needs_reference` (cache is in RAM) | 1 |
| 9 | Rejections cost nothing | send a 2 s clip, a 31 s clip, a bad name: each must answer with its code and `executionTime` near zero, and the voice list unchanged | 1 each |

## 4. What to read in the worker log (RunPod console, "Logs")
* `[TTS] model loaded in ...s (t3_model=...)` and `[TTS] voice 'utkarsh' prepared in ...s`: start-up costs.
* `[TTS] ready: voices=[...] default=utkarsh ... request_reference=ignored`: must say `ignored`.
* `[TTS_REGISTER] voice=<name> duration_s=.. prepare_s=.. registered=N evicted=K`: per registration. No audio and no base64 may appear anywhere in the log (check by searching for `UklGR`, the start of base64 WAV).
* `[TTS_TIMING] ... voice=<name> generate_s=.. rtf=..`: compare switching vs single-voice.
* `[TTS_ERROR] register_voice failed: <ErrorType>`: any `prepare_failed`. A traceback is printed to stderr for it; read it, do not paste clips.
* A `CUDA out of memory` line means the cap `MAX_REGISTERED_VOICES` is too high for this GPU: record N at which it happened.

## 5. Pass / fail
Pass: tasks behave as in the unit tests (error codes identical), the `needs_reference` -> register -> synth chain works, eviction keeps the baked voice, memory per voice is known, no audio in logs, total spend under the cap.
Fail or stop: any worker crash at start-up (the v3 deepcopy failure showed in the first lines), `prepare_failed` on a valid clip, out-of-memory, spend at 80 % of the cap.
Deliverable: a short report with the table above filled in, the raw JSON answers (no audio), the log excerpt, the money spent. Numbers from this plan then replace the placeholders `voice_studio_est_cold_start_s` and `voice_studio_gpu_usd_per_s` (the price comes from the owner's RunPod bill).

## 6. After the test
Scale the test endpoint to 0 workers or delete it (needs approval); remove its API key from wherever it was put. Nothing is merged to `main` and the live endpoint keeps its current image until a separate APPROVED rollout.
