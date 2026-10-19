# AI media adapter and model configuration

**Honest status:** the AI pipeline code, contracts and a WebSocket media protocol exist and are tested with **mock model servers and a synthetic audio client**. There is **no working audio bridge to FreeSWITCH** and the three real models were **never reached**. `AI_MEDIA_BRIDGE=none` is the default and AI sessions refuse to start (`409 media_bridge_unavailable`) until a bridge is verified.

## Why there is no live audio yet (blockers, measured)
| Blocker | Evidence | What unblocks it |
|---|---|---|
| ESL carries no audio | protocol fact | a media-fork module |
| No media-fork module in FreeSWITCH | `mod_audio_fork`, `mod_audio_stream` absent from `lib/freeswitch/mod` (`mod_av`, `mod_opus`, `mod_tone_stream`, `mod_local_stream` only) | build/install one (e.g. `mod_audio_fork` from the drachtio/FreeSWITCH-modules project or `mod_audio_stream`) against this FreeSWITCH, load it, set `MME_TEL_AI_MEDIA_BRIDGE=ws_fork`; **its WebSocket framing must then be adapted or verified against `app/ai/media.py`** — not done |
| No model servers reachable | no listener on the usual model ports; no GPU (`nvidia-smi` absent), 6 GB RAM, no Hugging Face cache | run STT/Brain/TTS on GPU hosts (your RunPod setup is referenced by the `mme_runpod_test` ACL but was not inspected) and give their URLs |
| `Qwen3-30B-A3B-Instruct-2507` cannot run on this machine | memory/GPU | remote inference server |
| Real model API shapes unknown | servers unreachable | the adapters default to OpenAI-compatible routes (`/v1/audio/transcriptions`, `/v1/chat/completions`, `/v1/audio/speech`, `/search`); per-service `paths` override in AI config. **Unverified against the real servers** |

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
