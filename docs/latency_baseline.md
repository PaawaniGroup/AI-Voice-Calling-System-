# Latency baseline (legacy path, before the realtime work)

Branch `realtime-latency`, measured on commit `4c9e83e` ("WIP baseline") plus the Step 0 instrumentation (`ca0b0f2`).
Legacy path = `REALTIME_STREAMING_ENABLED=false`: 700 ms end-of-speech silence, one blocking Brain call, sentences synthesized one after another.

## What this note is, and is not

| Item | Status |
|---|---|
| Historical turns from real calls recorded in `ai_sessions` (dev DB) | **Done** (table below) |
| 20-turn live run with `scripts/live_latency.py` (legacy path) | **NOT run yet.** It needs roughly 80-100 chargeable RunPod calls; only a handful of calls were approved. Waiting for a go-ahead and a number of calls. |
| First-turn-after-idle run | **NOT run yet** (same script, `--idle-s 900 --turns 3 --tag cold`, ~15 calls) |
| `voice_to_voice` from real phone calls | Not available: it did not exist before Step 0. Every call from now on records it. |

## Historical baseline: 14 recorded turns, 6 real calls (2026-10-23)

These turns pre-date the trace, so `turn_ms` here starts when STT is sent. **The caller also waited the 700 ms silence window before that, so real voice-to-voice was at least `turn_ms + ~700 ms`.** Sample sizes are small; treat as indicative, not statistical.

| Subset | n | STT p50 / p95 (ms) | Brain p50 / p95 | TTS first sentence p50 / p95 | turn_ms p50 / p95 |
|---|---|---|---|---|---|
| All turns | 14 (brain/tts: 11) | 13,428 / 39,095 | 2,321 / 3,325 | 5,168 / 49,185 | 19,252 / 86,047 |
| Warm (STT < 3 s) | 3 | 1,505 / 1,798 | 2,321 / 2,609 | 4,587 / 7,884 | 8,360 / 12,286 |
| Cold-ish (STT >= 3 s) | 8 | 19,169 / 40,686 | 2,629 / 3,344 | 5,558 / 50,503 | 26,771 / 86,583 |

Reading it:
- Warm turns match the 8-13 s you reported: Brain about 2.3 s, TTS about 4.6 s (first sentence only; later sentences add serially), STT about 1.5 s.
- Cold starts dominate the tail: STT alone took 8-42 s on the first turn of several calls, and TTS up to 54 s.
- Brain is the steadiest stage (2.0-3.4 s in every turn).
- RunPod `delayTime` vs `executionTime` was not recorded before Step 0, so queue/cold-start vs compute cannot be split for these turns. New turns store `stt_delay_ms`, `stt_exec_ms`, `brain_*`, `tts_*` (first call of each service per turn).

## Tenant configuration observed (`mme-test`, keys not shown)

| Service | RunPod endpoint | Path | Model id in config |
|---|---|---|---|
| STT | `kp9zlne48yhs80` | `/runsync` | `MmeOG/MME_STT_Model` |
| Brain | `zx6o64kfl8ecj0` | `/runsync` | `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8` |
| TTS | `tanq00wi13novj` | `/runsync` | `mme-tts-runpod` |

**Stale as of 24 Oct 2026:** the endpoint IDs above were the tenant's on 23 Oct; the tenant's `ai_config` in the database now holds different ones, so do not use them.

Notes: the settings default for the TTS model is `Qwen/Qwen3-TTS-12Hz-1.7B-Base`, but the tenant overrides it; the model id `mme-tts-runpod` is a worker name, not a model. Whether the worker is Chatterbox is **not confirmed by anything in this repo** (client comments say "Chatterbox"); it will be confirmed from the handler you paste. `Qwen3-30B-A3B-Instruct-2507` is the non-thinking variant, so there is no thinking mode to switch off on that checkpoint.

## Test baseline (before any realtime change)

Run in WSL (`~/venvs/mme-telephony`, Python 3.12.3) against the local test MongoDB on :27117, `--timeout 60`, markers `not freeswitch and not real_provider and not load and not browser`.

Included: `tests/integration/test_ai.py`, `test_plivo_voice.py`, `tests/unit/test_plivo_stream.py`, `test_runpod_clients.py`, `test_script_enforcement.py`, `test_tts_frontend.py`, `test_live_activity_flow.py`, `test_core.py`, `test_configuration.py` (codec is covered by `test_plivo_stream.py` / `test_core.py`).

Excluded, on purpose: `tests/e2e/` (browser test; fails immediately in this environment), `test_ai_freeswitch.py`, `test_ai_fs_autostart.py`, `test_freeswitch.py` (real FreeSWITCH), `tests/load`, `test_real_provider.py`, and every other file not listed above.

| Run | Result |
|---|---|
| Before (commit `4c9e83e`) | **172 passed, 0 failed, 2 errors** |
| After Step 0 | 178 passed (172 + 6 new), 0 failed, the same 2 errors |

The 2 errors are **pre-existing fixture-teardown hangs** (60 s timeout) after the tests themselves passed: `test_ai.py::test_hangup_cancels_ai_and_closes_media_no_stale_audio` and `test_plivo_voice.py::test_hangup_during_inference_cancels_it_and_no_audio_arrives_afterwards`. Not investigated yet; they cost about 2 minutes of the suite.

## How the trace is defined

All marks use `time.monotonic()` and are stored in ms relative to `t_speech_end` (last loud inbound frame), because the WSL wall clock steps. `voice_to_voice = t_first_frame_to_plivo - t_speech_end`. `t_endpoint - t_speech_end` is the silence window actually waited, and `endpoint_silence_ms` records the configured value per turn. On the legacy path `t_llm_first_token` equals the arrival of the whole reply, because the Brain call is not streaming.
