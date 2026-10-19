# AI media adapter and model configuration

**Honest status (2026-10-19, updated):**
* **Media transport: built and verified on your real FreeSWITCH, with mock models and tone "speech".** `mod_audio_stream` (amigniter, AGPL-3.0 + FreeSWITCH exception, commit `ff9cbdc`) was reviewed, built, installed and loaded (`scripts/build_mod_audio_stream.sh`; `load mod_audio_stream` registers `uuid_audio_stream`; `<load module="mod_audio_stream"/>` was added to `modules.conf.xml`, backup `modules.conf.xml.bak-before-audio-stream-*`, **not yet exercised by a FreeSWITCH restart**).
* With `AI_MEDIA_BRIDGE=ws_fork`, `GET /v1/ai/health` reports the bridge ready **only because** `module_exists mod_audio_stream` is true. Tested through the real module (`tests/integration/test_ai_freeswitch.py`): AI session becomes `active` only when FreeSWITCH's socket connects; caller audio -> VAD -> mock STT/Brain/TTS -> `streamAudio` -> module temp file -> `uuid_broadcast` -> **audible on the caller leg** (recorded, 0.28-0.30 s of a 0.30 s reply); barge-in via `uuid_break` cut a 6 s reply to ~2.1-2.3 s; hangup stops the AI session and the stream; sockets without a valid token are refused.
* **Still NOT verified:** the three real models (no server reachable), real human speech, 8 kHz telephone codecs/echo on a real PSTN call, any carrier leg, real end-to-end latency. Transport numbers below are with mock models (~0 ms inference) and loopback legs, so they are a **floor**, not a prediction.
* AI stays off by default (`AI_ENABLED=false`, `AI_MEDIA_BRIDGE=none`).

## Measured transport overhead (real FreeSWITCH + real module, mock models, loopback leg)
Runs of `test_real_audio_roundtrip_through_freeswitch_module` (7 consecutive passing runs, 1 intermittent failure in ~14 runs, see below):
| Metric | Observed |
|---|---|
| `start` -> AI session `active` (module connects) | 0.14-0.15 s |
| utterance end (VAD) -> reply ready (mock models) `turn_ms` | 4-11 ms |
| reply handed to module -> `mod_audio_stream::play` event back in controller `module_return_ms` | 8-26 ms |
| `uuid_broadcast` command `broadcast_cmd_ms` | 1-4 ms |
| VAD end-of-speech wait (configured) | 700 ms of trailing silence is the dominant fixed cost |
| barge-in: caller talks over a 6 s reply | reply truncated; caller heard 2.1-2.3 s total |
Real latency = these ~25-45 ms of transport + 700 ms endpointing + the **real** STT + LLM + TTS times, which are unknown.

**Intermittent failure:** one run in ~14 heard silence on the caller leg (no diagnostics at the time; the assertion now dumps recordings and events). A neighbouring run took 54-58 s, matching the WSL VM stalls seen elsewhere, so a stall is suspected but **not proven**. Treat the transport as "working, with one unexplained failure to watch".

## Remaining blockers
| Blocker | Evidence | What unblocks it |
|---|---|---|
| No model servers reachable | no listener on common model ports; no GPU (`nvidia-smi` absent), 6 GB RAM | run STT/Brain/TTS on GPU hosts (`mme_runpod_test` in the SIP ACL suggests RunPod, not inspected) and give their URLs |
| `Qwen3-30B-A3B-Instruct-2507` cannot run here | memory/GPU | remote inference server |
| Real model API shapes unknown | servers unreachable | adapters default to OpenAI-compatible routes (`/v1/audio/transcriptions`, `/v1/chat/completions`, `/v1/audio/speech`, `/search`); per-service `paths` override. **Unverified against the real servers** |
| `mod_audio_stream` limits | upstream README: no automatic reconnect; bidirectional streaming/mixing are commercial-edition features; playback is file-based (whole utterance, not streamed) | acceptable for turn-based replies; streaming TTS needs the commercial edition or another module |
| AGPL-3.0 module | licence | review with whoever owns your licensing; it is loaded as a separate module (linking exception applies to FreeSWITCH) |

## Components (`app/ai/`)
* `clients.py` – `ModelClients`: bounded concurrency (`AI_MAX_CONCURRENCY`), per-service timeout, error classes (`AiTimeout`, `AiUnavailable`), URL allow-list (`AI_ALLOWED_HOSTS`, no credentials/queries, metadata blocked, plain http only to loopback in production), optional bearer key from `MME_TEL_AI_API_KEY` (env only, never returned or shown).
* `pipeline.py` – `Conversation` per AI session: STT → optional RAG (degrades if down) → Brain with history and an approved-tool allow-list (none registered by default; unregistered tool calls are refused) → TTS. Each turn runs in its own task; `cancel()` aborts in-flight inference; late TTS output is discarded. System prompt asks for replies in the caller's language (Hindi / English / Hinglish) and forbids inventing fees; **multilingual quality is untested with the real models**.
* `service.py` – config, health, lifecycle, runtime registry, webhooks (`ai.session.started|stopped|failed`), cleanup on hangup.
* `ws.py` / `media.py` – the media WebSocket and bridge readiness.

## Configuration
Dashboard → *AI configuration* (admin) or `PUT /v1/ai/config`: `stt_url`, `brain_url`, `tts_url`, `rag_url`, `enabled`, `default_language`, `models{}`, `timeouts{}`, `paths{}`. Server env: `MME_TEL_AI_ENABLED` (global, default **false**), `MME_TEL_AI_MEDIA_BRIDGE`, `MME_TEL_AI_ALLOWED_HOSTS`, `MME_TEL_AI_API_KEY`, `MME_TEL_AI_REQUEST_TIMEOUT_S`, `MME_TEL_AI_MAX_CONCURRENCY`, model id defaults (informational: `Oriserve/Whisper-Hindi2Hinglish-Prime`, `Qwen3-30B-A3B-Instruct-2507`, `Qwen/Qwen3-TTS-12Hz-1.7B-Base`). Every change is audited; secrets are never stored in the config document.

## Media WebSocket contract — `/v1/ai/sessions/{session_id}/media`
Authentication: first text message `{"type":"auth","token":"<JWT with operator role, same tenant>"}` within 5 s (never in the URL). Format negotiated by `hello`: `pcm_s16le`, mono, 8000 or 16000 Hz, `frame_ms` 10/20/30/40 — other formats get `unsupported_format` (fatal).

| Direction | Message | Meaning |
|---|---|---|
| C→S | `{"type":"hello","encoding":"pcm_s16le","sample_rate":16000,"channels":1,"frame_ms":20}` | start; server replies `ready` and the AI session becomes `active` |
| C→S | binary frames | raw caller PCM, one frame per message, in order |
| S→C | `{"type":"playback_start","utterance_id":n,"sample_rate":R,"language":"…"}` + binary frames + `{"type":"playback_end","utterance_id":n}` | assistant speech, paced at real time |
| S→C | `{"type":"interrupt"}` | caller barged in (≥120 ms speech while playing); drop queued playback |
| S→C | `{"type":"error","code":"stt_timeout|brain_unreachable|tts_…|inbound_overflow|…","fatal":false}` | non-fatal model/pressure errors; session continues |
| either | `{"type":"bye"}` | end the AI session |

Behaviour: energy-based endpointing (RMS threshold 500, 700 ms trailing silence, ≥200 ms speech, 15 s cap); bounded utterance queue (2, oldest dropped with `inbound_overflow`); TTS audio resampled to the client rate (linear); socket drop → `media_disconnected` and **30 s** to reconnect with the same session (conversation resumes), else `failed`; no media within 30 s of `start` → `failed` (`media_connect_timeout`); hangup → AI session `stopped` (`call_ended`), socket closed, no audio is sent afterwards. Energy VAD is a placeholder: **real call noise, telephony codecs (8 kHz G.711) and echo were not tested**.

## What you must do to reach a verified live call
1. Install and load a FreeSWITCH media-fork module; add the dialplan/ESL action that starts forking for answered AI calls (the controller does not issue it yet — that call belongs next to `start()` once the module's API is known).
2. Point the module at `wss://<service>/v1/ai/sessions/{id}/media` (or add a thin relay that performs the `auth`/`hello` handshake above).
3. Stand up the three model servers and set the URLs; check `/v1/ai/health`.
4. Place an approved test call with `ai_enabled:true`; measure STT/Brain/TTS timings (`GET /v1/ai/sessions/{sid}` → `timings_ms`) and barge-in; only then set `MME_TEL_AI_ENABLED=true` and `AI_MEDIA_BRIDGE=ws_fork` in production. **Do not claim real-time operation before this.**
