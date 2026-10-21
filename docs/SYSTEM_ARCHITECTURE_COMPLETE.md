# MME AI Voice Counselling: complete code architecture, routes and end-to-end behaviour

Generated 2026-10-21 from the code as it exists on disk (route tables were dumped from the running app objects, file sizes from `wc -l`).
**Read the status box first. It says what has and has not been proven.**

> **Status, honestly.** Everything below is implemented and covered by automated tests. **Never done:** a real Plivo phone call, a real STT/LLM/TTS inference, a production host, MongoDB Atlas, S3. Tests drive the real local FreeSWITCH and the real `mod_audio_stream` module with a **tone** as the caller and **mock** model servers.
> Test totals last recorded: repo A **92 passed** (88 mock + 4 live FreeSWITCH); repo B **169 passed, 1 skipped** (the skip is the opt-in real-Plivo test).

---

## 0. The two codebases (read this first)

There are **two separate Python projects**. They overlap in purpose, were built in sequence, and are **not wired together**.

| | **Repo A: voice prototype** (yours, extended) | **Repo B: telephony service** (built by me) |
|---|---|---|
| Location | WSL `~/MME_TELEPHONY` | Windows `C:\Users\Admin\Desktop\MME_Telephony` (git, 6 commits), run from WSL |
| Port | `127.0.0.1:8090` (currently running OLD code until restarted) | `127.0.0.1:8800` (not running as a service; started by tests/smoke script) |
| Storage | in memory (lost on restart) | MongoDB (durable) |
| Auth | **none** on session routes (loopback-only); Plivo signatures on `/plivo/*`; ticket on media sockets | JWT roles + tenant isolation on everything except health and provider callbacks |
| Size | ~4,000 lines incl. tests | ~6,600 lines app/scripts + ~2,400 lines tests |
| Strength | actual voice loop on Plivo and FreeSWITCH paths | call lifecycle, queue, recovery, dashboard, webhooks, recording, RBAC |
| Routes | 11 | 55 + static `/dashboard` |
| Tests | 92 | 169 passed + 1 skipped |

**Overlap you should know about:** both implement the `mod_audio_stream` media protocol (repo A: `ai_counselling/fs_bridge.py`; repo B: `app/ai/ws.py::handle_fs_media`), both have a session model, both have Plivo handling. **Do not point both at the same FreeSWITCH call.** Converging them (repo A's voice engine inside repo B's durable lifecycle) is the logical next engineering step and is **not done**.

What is running on your machine today: FreeSWITCH, the lifecycle listener `mme_telephony.esl_events` (unchanged), and repo A's API on :8090 (still the pre-change code in memory until you restart it).

---

## 1. System context

```
                      ┌────────────────────────── public internet ──────────────────────────┐
                      │  Phone ──PSTN──► Plivo ──SIP trunk──► (your router/NAT) ──► FreeSWITCH │   PATH B (current DID routing)
                      │  Phone ──PSTN──► Plivo ──HTTPS answer_url / WSS stream──► (tunnel)    │   PATH A (needs public URL; NOT started)
                      └─────────────────────────────────────────────────────────────────────┘

   ┌───────────────────────────────── WSL Ubuntu 24.04 (one laptop) ─────────────────────────────────┐
   │  FreeSWITCH 1.11.4-dev  /usr/local/freeswitch                                                      │
   │    SIP 192.168.1.22:5080 (profile external, gateway plivo-mme)   ESL 127.0.0.1:8021 (loopback)    │
   │    modules: sofia, event_socket, opus, av, sndfile, mod_audio_stream (loaded, AGPL, commit ff9cbdc)│
   │         ▲ ESL events/commands                    ▲ module opens WebSocket CLIENT to the API         │
   │         │                                         │                                                  │
   │  mme_telephony.esl_events (process, unchanged) ──HTTP──► Repo A API :8090                           │
   │  Repo A API (uvicorn): sessions · /plivo/* · /fs/stream · MediaService (own ESL conns)              │
   │  Repo B service :8800 (uvicorn, 1 worker): REST · dashboard · controller · ESL · MongoDB            │
   │  MongoDB 127.0.0.1:27117 (dev mongod, for repo B tests)                                             │
   └──────────────────────────────────────────────────────────────────────────────────────────────────┘
   Model servers (STT / Brain / TTS / RAG): NOT PRESENT. No GPU, ~6 GB RAM. Adapters call them over HTTP when URLs are configured.
```
Hard rules observed everywhere: ESL stays on loopback and is never exposed; FreeSWITCH never loads models; ESL carries **control and events only, never audio**; no secret is stored in source (secrets come from environment / `.env`, which is currently empty).

---

# PART I: REPO A (`~/MME_TELEPHONY`): voice prototype

## 2. File map and responsibilities

| File | Lines | Role |
|---|---|---|
| `ai_counselling/app.py` | 96 | FastAPI app. The original 5 session endpoints (behaviour preserved) + `/health/voice`; lifespan starts/stops the FreeSWITCH media service; includes the two routers |
| `ai_counselling/session_store.py` | 118 | Thread-safe in-memory sessions + identifier alias map (provider call id, FreeSWITCH UUID, stream id → session) |
| `ai_counselling/registry.py` | 47 | `session_id → VoiceCall` map. `on_session_ended()` (callable from any thread) schedules `VoiceCall.stop()` on the main loop |
| `ai_counselling/plivo_bridge.py` | 303 | **Path A:** signed `/plivo/answer`, `/plivo/status`, `/plivo/hangup`, authenticated `WS /plivo/stream`, `PlivoTransport` |
| `ai_counselling/fs_bridge.py` | 258 | **Path B:** `MediaService` (ESL owner), `FsTransport`, `WS /fs/stream` |
| `mme_voice/config.py` | 162 | `VoiceConfig` dataclass from environment; validation; safe `describe()` |
| `mme_voice/audio.py` | 183 | μ-law/PCM16 conversion, resampling, WAV build/validate, `Endpointer` (VAD, barge-in, idle handling) |
| `mme_voice/adapters.py` | 217 | `ModelClients` (STT/Brain/TTS/RAG over HTTP, bounded concurrency, timeouts), `Conversation` (turn orchestration) |
| `mme_voice/call.py` | 155 | `VoiceCall`: transport-agnostic live call engine; owns and cancels its tasks |
| `mme_voice/tickets.py` | 60 | HMAC tickets binding a media stream to one session |
| `mme_voice/plivo_security.py` | 81 | Plivo V3 signature check, nonce replay cache, public-URL construction |
| `mme_voice/esl.py` | 248 | Async ESL: one command connection (`EslCommands`) + one event connection (`EslEvents`) |
| `mme_voice/runtime.py` | 30 | Shared `ModelClients` singleton + test override |
| `mme_telephony/esl_events.py` | 195 | **Your original lifecycle listener (unchanged):** CHANNEL_CREATE/ANSWER/HANGUP_COMPLETE → session HTTP API |
| `mme_telephony/counselling_api.py` | 72 | Your urllib client for the session API (unchanged) |
| `tests/` | ~1,460 | 92 tests (see §30) |
| `docs/VOICE_SETUP.md`, `.env.example` | | operator guide; placeholder env template |

Backups of every file I changed: `*.pre-voice-<timestamp>` next to the file and `~/backups/MME_TELEPHONY-pre-voice-*.tgz`.

## 3. Concurrency and process model (repo A)

* **Process 1: FreeSWITCH** (unchanged).
* **Process 2: lifecycle listener** `python -m mme_telephony.esl_events`: **synchronous**, one socket, blocking `urllib` calls with a 3 s timeout. Subscribes `CHANNEL_CREATE`, `CHANNEL_ANSWER`, `CHANNEL_HANGUP_COMPLETE`. Errors from the API are printed and **do not stop the loop**; it reconnects every 3 s. It creates a session for **every channel** (so a bridged call's two legs can give two sessions) and has no retry of a failed end-session call. These are original behaviours, left as they were.
* **Process 3: API** (`uvicorn ai_counselling.app:app`): a single asyncio loop. Sync routes (`/v1/sessions*`) run in FastAPI's threadpool and share `session_store` under an `RLock`; the voice work (`VoiceCall`, ESL media service, WebSockets) runs on the main loop. `registry.set_loop()` records the loop so a thread can safely schedule `call.stop()` via `run_coroutine_threadsafe`.
* **ESL ownership rule:** only `MediaService` (inside process 3, only when `MME_FS_MEDIA_ENABLED=1`) opens media ESL connections. It subscribes `CHANNEL_ANSWER CHANNEL_HANGUP_COMPLETE HEARTBEAT CUSTOM mod_audio_stream::{play,connect,disconnect,error}`. This overlaps the lifecycle listener on two events but both are independent read-only subscribers; neither sends commands on the other's socket. The flag keeps accidental double subscription impossible.
* Per call, `VoiceCall` owns 2 tasks (conversation worker, idle watcher) plus short-lived play tasks; `stop()` cancels all and awaits them.

## 4. Repo A: routes (11)

Authentication summary: the session routes have **no authentication** (the original prototype design; safe only because the server binds 127.0.0.1). The Plivo routes authenticate with Plivo's V3 signature; the media sockets with signed tickets.

### 4.1 `GET /health/live`
`200 {"status":"ok","service":"mme-ai-counselling"}`.

### 4.2 `GET /health/voice`
`200` with **no secret values**: `ai_enabled`, `env`, booleans for `stt/brain/tts/rag` configured, the three model ids, `model_api_key_set`, `plivo_auth_token_set`, `public_base_url_set`, `public_wss_url_set`, `media_token_secret_set`, `missing_for_models` (variable *names*), `missing_for_plivo` (names).

### 4.3 `POST /v1/sessions`
Body (unchanged schema): `tenant_id` (1-100, required), `caller_id` (≤50), `call_id` (≤150), `language` (≤30, default `auto`), `metadata` (object).
Behaviour: creates a session `{session_id (uuid4), tenant_id, caller_id, call_id, language, metadata, status:"active", created_at, ended_at:null, end_reason:null, provider_call_id, fs_uuid, stream_id}` and returns **201**. **Idempotent per (tenant, call_id):** a second request with the same call id returns the **existing** session with **200** (this removes the duplicate-session problem from repeated `CHANNEL_CREATE`). Validation errors are 422 (unchanged).

### 4.3b `GET /v1/sessions/{session_id}` → 200 session or 404 `{"detail":"Session not found"}`.

### 4.4 `POST /v1/sessions/{session_id}/answer`
`active → answered` (sets `answered_at`); idempotent; 404 if unknown.

### 4.5 `POST /v1/sessions/{session_id}/end`
Body `{reason}` (default `completed`). Ends an `active` **or `answered`** session (the prototype only ended `active`, which left every answered call's session stuck: **fixed**). Idempotent: the first reason and `ended_at` win; later calls return the stored session. When it newly ends a session it calls `registry.on_session_ended()` which cancels any live inference/playback for that session.

### 4.6 `POST /plivo/answer` (Plivo Voice API webhook)
Order of checks (each failure stops processing):
1. `plivo_problems()` non-empty → **503** `{"detail":{"error":"not_configured","missing":[names]}}`. The endpoint never answers unauthenticated.
2. body > 64 KB → **413**.
3. Plivo V3 signature over `POST <MME_PUBLIC_BASE_URL>/plivo/answer[?query]` with the form parameters, via the SDK's `validate_v3_signature`; nonce must be unseen (10 min cache) → else **403** `invalid signature` (reason logged without values).
4. `CallUUID` must match `^[0-9A-Za-z-]{8,64}$` → else **400**.
5. AI not enabled/configured → **200** with `<Hangup reason="rejected"/>` (the caller is not left on a dead stream).
6. Create-or-get the session by `CallUUID` (idempotent), link `provider_call_id`, mark answered, mint a **ticket** (kind `plivo-stream`).
7. Return XML: `<Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000" extraHeaders="mme_ticket=…" statusCallbackUrl="<base>/plivo/status" statusCallbackMethod="POST">wss://…/plivo/stream</Stream>`.

### 4.7 `POST /plivo/status` and `POST /plivo/hangup`
Same verification. Form `Event`/`CallStatus` in {stopped, failed, completed, hangup, busy, no-answer, cancel} ends the session found via the `CallUUID` alias (once; duplicates are no-ops) and cancels its live call. Returns `{"ok":true}`.

### 4.8 `WS /plivo/stream` (Plivo → us, bidirectional)
Handshake: the V3 signature is verified **before** `accept()` using method `GET` and the configured wss URL in its `https://` form (the SDK only accepts http/https). Failure → close **1008**. More than `MME_MAX_STREAMS` (default 5) concurrent → **1013**.

Messages received (Plivo reference):
| `event` | Handling |
|---|---|
| `start` | Requires `extra_headers` containing a valid unexpired ticket (kind `plivo-stream`) naming a live session whose `provider_call_id == start.callId`; `mediaFormat` must be `audio/x-mulaw` @ 8000 (else **1003**); a second `start` on the socket or a session already streaming → **1008**. Creates `VoiceCall(PlivoTransport)` and registers it |
| `media` | only `track=="inbound"`; base64 payload validated; `chunk` must increase (duplicates/out-of-order counted and **dropped**); payload ≤ 4000 bytes; μ-law → PCM16 → `VoiceCall.feed` with duration from the payload length |
| `dtmf` | counted only; digits are **not logged** |
| `playedStream` | recorded (checkpoint name) on the transport |
| `clearedAudio` | logged |
| `stop` | normal end |
| invalid JSON / oversize | counted / closed **1009** |

Messages sent: `playAudio {"event":"playAudio","media":{"contentType":"audio/x-mulaw","sampleRate":8000,"payload":<b64>}}` in 8000-byte μ-law chunks (~10.7 KB base64, under Plivo's 16 KB guidance), then `{"event":"checkpoint","streamId":…,"name":"utt-N"}`; on barge-in `{"event":"clearAudio","streamId":…}`.
On any exit: `VoiceCall.stop(reason)`, then the session is ended once (`stream_stopped`, `ws_disconnected`, `bad_ticket`, `error_<Type>` …). If *we* ended the call on error, `hangup_call()` uses the Plivo REST API **if** `PLIVO_AUTH_ID`+`PLIVO_AUTH_TOKEN` exist; otherwise it logs that it cannot.
**UNVERIFIED with a real Plivo call:** `playAudio` contentType spelling, handshake URI form, message-size limits, close codes, whether Plivo returns `extraHeaders` inside `start`.

### 4.9 `WS /fs/stream` (FreeSWITCH `mod_audio_stream` → us)
* Client must be loopback (unless `MME_FS_MEDIA_ALLOW_REMOTE=1`) and send `Authorization: Bearer <ticket kind fs-stream>` (set by us through channel variable `STREAM_EXTRA_HEADERS`). Missing/invalid/expired → close **1008** before accept.
* First text frame (the module's `metadata` argument): `{"session_id":…, "sample_rate":8000|16000}`; must match the ticket's session; the session must exist, not be ended, and not already streaming.
* Then binary frames = L16 mono PCM (20 ms) → `VoiceCall.feed`.
* Replies as text: `{"type":"streamAudio","data":{"audioDataType":"wav","sampleRate":R,"audioData":<base64 WAV>}}`. The module writes a temp file and fires `mod_audio_stream::play`; it does **not** play it (§8).
* Disconnect/protocol error → `VoiceCall.stop(reason)`.

## 5. Repo A: module-level detail

### 5.1 `mme_voice/config.py`: `VoiceConfig`
Frozen dataclass; `from_env()` reads (names only here, none has a real default): `MME_STT_URL, MME_BRAIN_URL, MME_TTS_URL, MME_RAG_URL`, `MME_*_PATH` (OpenAI-style defaults `/v1/audio/transcriptions`, `/v1/chat/completions`, `/v1/audio/speech`, `/search`), `MME_MODEL_API_KEY`, model ids (defaults = your three models), `MME_*_TIMEOUT_S` (15/30/20/5), `MME_MODEL_CONCURRENCY` (4), `MME_MODEL_ALLOWED_HOSTS`, `PLIVO_AUTH_TOKEN`, `MME_PUBLIC_BASE_URL`, `MME_PUBLIC_WSS_URL`, `MME_ALLOW_UNSIGNED_PLIVO` (honoured **only** when `MME_ENV=development`), `MME_FS_MEDIA_WS_URL` (default `ws://127.0.0.1:8090/fs/stream`), `MME_MEDIA_TOKEN_SECRET`, `MME_MEDIA_TOKEN_TTL_S` (120), VAD (`MME_VAD_RMS` 500, `MME_SILENCE_MS` 700, `MME_MIN_SPEECH_MS` 200, `MME_BARGE_IN_MS` 120, `MME_MAX_UTTERANCE_MS` 15000), `MME_AI_ENABLED`, `MME_TENANT_ID` (`mme-test`).
`model_problems()` → names of missing/invalid variables (URL must be http(s), no credentials, no query, host in the allow-list if one is set). `ConfigError` messages list names only, never values. `describe()` is the safe summary behind `/health/voice`.

### 5.2 `mme_voice/audio.py`
`mulaw_to_pcm16`, `pcm16_to_mulaw` (stdlib `audioop`; Python 3.13 needs `audioop-lts`), `resample` (linear), `rms`, `pcm16_to_wav`, `inspect_wav` (**validates before playback**: RIFF/WAVE, mono, 16-bit, rate in the allowed set, non-empty, ≤ 60 s → `AudioError` with a reason), `wav_to_pcm16`, `chunks`, test helpers `tone/silence`, and **`Endpointer`**:
* `feed(frame, ms)`: speech if RMS ≥ `vad_rms`; speech starts after `min_speech_ms`; the utterance ends after `silence_ms` of trailing silence or `max_utterance_ms`; while `playing` is true, `barge_in_ms` of speech emits `barge_in`.
* `idle(ms)`: counts wall-clock time with **no frames** as silence (the module sends nothing while a leg is idle; this was a real bug found on the live call).

### 5.3 `mme_voice/adapters.py`
* `ModelClients`: one shared `httpx.AsyncClient`, `Semaphore(max_concurrency)`, per-stage timeout via `asyncio.wait_for`. `transcribe` = multipart WAV POST + `model` (+ `language`; `hinglish` is sent as `hi`); `chat` = chat-completions body `{model, messages, temperature 0.3, max_tokens 400}`; `synthesize` = JSON `{model, input, response_format:"wav", language}` and the response **must pass `inspect_wav`**; `retrieve` = `{query, top_k}`; `health()` GETs `<url>/health` per stage → `healthy | unhealthy | unreachable | not_configured`.
* Errors: `AiError(stage, kind)` with kind `timeout | unreachable | http_error | bad_response | bad_audio`; an unconfigured stage raises `ConfigError([VAR])`. **No canned reply exists anywhere.**
* `Conversation`: system prompt (reply in the caller's language; 1-3 spoken sentences; use only provided CONTEXT for fees/eligibility; never invent), history capped at 12 messages. `turn()` runs `_turn()` in its own task: STT → (RAG, optional, degrades) → Brain → TTS. Empty transcript → `no_speech_recognised`; empty reply → `empty_reply` and the user message is rolled back; `cancel()` aborts in flight and a result that finishes after cancel is discarded (`cancelled`).
* **Assumption:** OpenAI-compatible request shapes are unverified against the real Oriserve / Qwen servers; override `MME_*_PATH` or replace the class.

### 5.4 `mme_voice/call.py`: `VoiceCall`
`feed()` → `Endpointer` → events: `barge_in` (cancel conversation, cancel playback task, `transport.interrupt()`), `utterance` (bounded queue of 2; oldest dropped, counted). Worker: `conv.turn()` → if the call stopped mid-inference the result is dropped → record timings (`stt, rag, brain, tts, turn_ms`) → play (a failed send is counted, never fatal). `stop(reason)` is idempotent: cancel conversation + tasks, close transport, unregister. Stats: frames, utterances, turns, errors, barge_ins, dropped_utterances.

### 5.5 `mme_voice/tickets.py`
`token = b64url(payload) + "." + b64url(HMAC-SHA256(secret, payload))`, payload `{s: session_id, k: kind, e: expiry}`; kinds `plivo-stream`, `fs-stream`; constant-time compare; expired / wrong kind / malformed → `TicketError`. Secret = `MME_MEDIA_TOKEN_SECRET`, or derived from `PLIVO_AUTH_TOKEN` (never the token itself). Characters are safe for Plivo `extraHeaders` (no `;` or `=`).

### 5.6 `mme_voice/plivo_security.py`
`external_url()` builds the signed URL **only** from `MME_PUBLIC_BASE_URL` (never Host / X-Forwarded-*). `verify()`: signature headers present and bounded, SDK `validate_v3_signature`, then nonce stored 600 s (max 20,000 entries) so a captured signed request cannot be replayed. `MME_ALLOW_UNSIGNED_PLIVO` works only in development and logs a warning.

### 5.7 `mme_voice/esl.py`
`EslCommands`: serialised `api` with per-command timeout; any timeout/IO error drops the socket (reply order unknowable) and reconnects with jittered exponential backoff; rejects newlines. `EslEvents`: one `event plain …` subscription per connect (counter `subscriptions` proves no duplicates), bounded queue (5000, drops counted), 4 worker tasks (the reader never awaits a handler), idle timeout 60 s detects half-open sockets. The password comes from FreeSWITCH's `event_socket.conf.xml` (or `FS_ESL_PASSWORD`) and is never logged.

### 5.8 `ai_counselling/fs_bridge.py`: `MediaService`
* `on_event` routes: `CHANNEL_ANSWER` → `maybe_start`; `CHANNEL_HANGUP_COMPLETE` → `on_hangup`; `CUSTOM mod_audio_stream::play` → `on_play`; `…::error/disconnect` → logged.
* `maybe_start` runs only when the channel is **flagged** (`variable_mme_ai == "1"` or `Caller-Destination-Number` matches `MME_FS_AI_MATCH`) **and** `MME_AI_ENABLED` + model URLs + a ticket secret exist; otherwise it logs the missing names and does nothing (an unflagged call is never touched). Then: find-or-create the session (alias = FreeSWITCH UUID), link, answer, mint ticket, `uuid_setvar <uuid> STREAM_EXTRA_HEADERS {"Authorization":"Bearer …"}`, `uuid_audio_stream <uuid> start <ws-url> mono 16000 {"session_id":…,"sample_rate":16000}`.
* `on_play` plays **only** a file that matches `<dir>/<this channel uuid>_<n>.tmp.<ext>` (no `..`) and only for a channel that has a live AI call: `uuid_broadcast <uuid> <file> aleg`.
* `on_hangup`: stop the `VoiceCall`, end the session once, delete the module's temp files.
* `FsTransport`: `play` builds a WAV and **validates it** before sending `streamAudio`; `interrupt` = `uuid_break <uuid> all`; `close` = `uuid_break` + `uuid_audio_stream <uuid> stop`.

---

## 6. Repo A: end-to-end flows

### 6.1 PATH B: call through FreeSWITCH (matches your DID routing)
```
 0 Caller dials +918065530418 ► Plivo ► SIP INVITE ► FreeSWITCH profile external (context public)
 1 Dialplan: set mme_ai=1 (or MME_FS_AI_MATCH matches) ; answer ; park/sleep        [YOUR dialplan currently plays a tone and hangs up]
 2 mme_telephony.esl_events (process 2) ── CHANNEL_CREATE ──► POST /v1/sessions {call_id=<uuid>}  → session S (or the existing one)
 3 CHANNEL_ANSWER arrives at BOTH listeners:
     lifecycle: POST /v1/sessions/S/answer
     MediaService: flagged? models configured? → find S by uuid alias, mint ticket(fs-stream, S)
                   ESL: uuid_setvar … STREAM_EXTRA_HEADERS ; uuid_audio_stream … start ws://127.0.0.1:8090/fs/stream mono 16000 {"session_id":S,...}
 4 mod_audio_stream (FreeSWITCH) ─WS client─► /fs/stream : Bearer ticket ✓, loopback ✓ → accept
     frame 1 (text) {"session_id":S,"sample_rate":16000} ✓  →  VoiceCall(S, FsTransport) registered + started   [~0.13 s after answer, measured]
 5 caller audio (L16 16 kHz, 20 ms binary frames) ─► Endpointer
     speech ≥200 ms … silence ≥700 ms ⇒ utterance (PCM)
 6 worker: STT (HTTP) ► [RAG] ► Brain ► TTS (WAV, validated)             ← needs MME_STT/BRAIN/TTS_URL (absent today)
 7 FsTransport.play ─ text frame streamAudio{wav,base64} ─► module writes <tmpdir>/<uuid>_<n>.tmp.wav and fires CUSTOM mod_audio_stream::play
 8 MediaService.on_play validates the path ► ESL: uuid_broadcast <uuid> <file> aleg ► caller hears the reply
 9 barge-in: speech ≥120 ms while a reply is playing ► cancel conversation + task ► ESL uuid_break <uuid> all
10 hangup: CHANNEL_HANGUP_COMPLETE ► lifecycle: POST /v1/sessions/S/end {reason=cause}
                                 ► MediaService.on_hangup: VoiceCall.stop (cancels inference/playback, closes WS, uuid_audio_stream stop), session end (no-op if already ended), delete temp files
```
Measured on the real FreeSWITCH + real module with a tone caller and mock models: module connected 0.13 s after answer; utterance → mock-model reply ready in 5-17 ms; reply audible (≈0.28 s of a 0.30 s reply); a 6 s reply cut to ≈1.5 s by barge-in; hang-up during inference left 0 channels and 0 sessions. **Not measured:** real speech, real models, 8 kHz PSTN audio, any carrier leg.

### 6.2 PATH A: call through the Plivo Voice API (not started; needs a public URL)
```
 Plivo POST /plivo/answer (signed) ► verify ► session ► XML <Stream …extraHeaders="mme_ticket=T">wss://host/plivo/stream</Stream>
 Plivo WS /plivo/stream (signed handshake) ► start{callId,streamId,mediaFormat,extra_headers} ► ticket T ↔ session ↔ callId ✓
 media(μ-law 8k, ~20 ms) ► μ-law→PCM16 ► VoiceCall ► STT ► Brain ► TTS ► resample 8k ► μ-law ► playAudio(8000 B chunks) + checkpoint
 barge-in ► clearAudio ;  stop / WS drop / POST /plivo/status(stopped) ► VoiceCall.stop + session end (once)
```
Use **either** path for a number, never both.

### 6.3 Failure and cleanup behaviour
| Situation | Behaviour |
|---|---|
| model not configured | Plivo: `<Hangup>`; FreeSWITCH: media never started, error logged with variable names; call otherwise unaffected |
| STT/Brain/TTS timeout or HTTP error | turn aborted with `<stage>_<kind>`, counted, **no audio sent**, call continues |
| hangup mid-inference | result dropped; no stale audio after hangup |
| API down while FreeSWITCH hangs up | lifecycle listener prints the error and continues (original behaviour: **no retry**) |
| WS drop | `VoiceCall.stop('ws_disconnected')`; session ended once |
| duplicate `CHANNEL_CREATE` / `end` / provider callbacks | same session returned / no-op |
| module temp files | removed at hangup |
| ESL down | commands fail with `EslUnavailable`, backoff reconnect; audio path degrades, session API unaffected |

---

# PART II: REPO B (`C:\Users\Admin\Desktop\MME_Telephony`): telephony service

## 7. Layered architecture

```
 HTTP clients / dashboard SPA / Plivo ───► app/main.py (create_app, lifespan, middleware, error handlers)
                                              │  request-id · body limit · CSP on /dashboard · ApiError/PyMongo → envelope
   app/api/routes.py ............ health, calls, recordings, usage, admin, webhooks, Plivo callbacks
   app/api/routes_platform.py ... auth/users, sessions, call-session link, system, overview, SSE, AI contracts, logs, AI media WS
   app/api/schemas.py ........... strict request models, CallOut (+ number masking)
        │ Depends(require_viewer|user|admin|platform)  ── app/security/{auth,users}.py (JWT HS256, roles, rate limiter, scrypt)
        ▼
   app/services/ ........ calls.py (policy, admission, idempotency, enqueue) · sessions.py · recordings.py
        ▼
   app/telephony/ ....... controller.py (events, dispatcher, hangup/transfer, timeouts, reconcile, inbound)
                           state_machine.py · esl.py (event + command clients) · dialstrings.py (validated command builders)
   app/providers/ ....... base.py (FreeSwitchGatewayProvider, MockProvider) · plivo.py (REST + V3 signature)
   app/ai/ .............. clients.py · pipeline.py · service.py · media.py · ws.py
   app/events/webhooks.py  emitter (DB outbox) · signer · SSRF guard · retrying worker
   app/persistence/ ..... db.py (indexes, schema v3) · repos.py (CAS transitions, dedup events, idempotency)
   app/observability/ ... metrics.py (Prometheus) ;  app/core/ (errors, ids, logging+redaction, phone policy)
   config/settings.py ... 134 validated parameters (generated reference: docs/CONFIGURATION_REFERENCE.md)
        ▼                                   ▼                          ▼
   MongoDB `mme_telephony`          FreeSWITCH via ESL         outbound HTTPS (webhooks, Plivo REST, models, S3)
```

### 7.1 Process model (one worker, enforced)
`lifespan` order: connect MongoDB (**fail fast**) + ensure indexes/schema → ESL command client → ESL event client → controller loops → webhook worker → housekeeping task. Shutdown reverses it; calls stay in FreeSWITCH and are reconciled by the next start. Controller loops: **dispatcher** (≈4×/s), **timeout sweeper** (every 2 s), **reconciler** (every 30 s and after every ESL reconnect). One worker is required because the dispatcher, rate limiter and ESL event stream are per-process (multi-node needs leader election: not built).

### 7.2 File map
`app/ai` clients 196 · media 95 · pipeline 132 · service 329 · ws 278 · `app/api` routes 510 · routes_platform 414 · schemas 117 · `app/core` errors 55, ids 23, logging 112, phone 69 · `app/events/webhooks.py` 233 · `app/main.py` 185 · `app/persistence` db 133, repos 248 · `app/providers` base 118, plivo 98 · `app/security` auth 104, users 95 · `app/services` calls 102, recordings 194, sessions 186 · `app/telephony` controller 597, dialstrings 97, esl 341, state_machine 93 · `app/static/dashboard` app.js 283, index.html 38, style.css 30 · `config/` settings 314, FreeSWITCH dialplans (`mme_dialplan.xml` 38, `mme_test_dialplan.xml` 64) · `scripts/` 14 tools · `deploy/mme-telephony.service`.

## 8. How `mod_audio_stream` really behaves (read from the installed source `~/mediafork-src/mod_audio_stream`)
* `uuid_audio_stream <uuid> start <ws(s)-url> mono|mixed|stereo <8000|16000> [metadata]`; FreeSWITCH is the WebSocket **client**; metadata = first **text** frame; audio = binary L16 (20 ms default); extra handshake headers from channel variable `STREAM_EXTRA_HEADERS` (JSON). Stop: `uuid_audio_stream <uuid> stop`.
* A server text frame `{"type":"streamAudio","data":{"audioDataType":"wav|raw|mp3|ogg|pcmu|pcma","sampleRate":N,"audioData":"<base64>"}}` makes the module **write a temp file** (`<tmpdir>/<uuid>_<n>.tmp.<ext>`) and **fire `CUSTOM mod_audio_stream::play`** with the data JSON + `file`; it does **not** play it. Playback is done by us with `uuid_broadcast <uuid> <file> aleg`.
* Events: `::connect`, `::disconnect`, `::error`, `::json`, `::play`. No automatic reconnect; no streamed playback; bidirectional streaming/mixing are commercial-edition features.

## 9. Repo B routes (55 API routes + static mount)

Roles: **viewer** (read; phone numbers masked) < **operator** (`telephony:user`) < **admin** (`telephony:admin`) < **platform** (`platform:admin`, cross-tenant; created only from the host CLI). Claim names are configurable; JWT HS256 with `sub`+`exp` required, `alg=none` rejected. **The tenant always comes from the verified token**, never from the request; every tenant-scoped lookup includes the tenant, so another tenant's id answers **404**. Error envelope everywhere: `{"error":{"code","message","details","request_id"}}`. Full per-route table (generated): `docs/ROUTES.md`.

### 9.1 Health and operations
| Route | Auth | Detail |
|---|---|---|
| `GET /health/live` | none | `{"status":"ok"}`; never fails because a dependency is down |
| `GET /health/ready` | none | 200/503; `checks.database`, `checks.freeswitch` (event stream connected **and** fresh). No hostnames or credentials |
| `GET /metrics` | metrics bearer token, else loopback only | Prometheus text |
| `GET /v1/telephony/status` | viewer | counts, connection flags, `real_calls_enabled`, `recording_enabled`, `ai_enabled` |
| `GET /v1/system/status` | viewer | every component with `up/down/…`, **`last_ok_at`** per component, limits |
| `GET /v1/system/queues` | viewer | queued/active calls, uncertain originations, webhook delivery states, ESL queue depth |
| `GET /v1/dashboard/overview` | viewer | health + today's counts by state, answered/missed/failed/completed, average connected seconds, active calls/sessions/AI sessions, recent errors (admins only) |
| `GET /v1/stream/events` | viewer | **SSE** snapshot of live calls pushed when it changes (≤5 streams per tenant, 1 h max); use `fetch` with the Authorization header |
| `GET /v1/logs/errors` | admin | in-memory redacted WARNING+ ring (resets on restart); `level`, `q`, `limit` |

### 9.2 Auth and users
| Route | Auth | Detail |
|---|---|---|
| `POST /v1/auth/login` | none, rate-limited per IP | scrypt-verified; same 401 `invalid_credentials` for unknown user, wrong password, disabled user; audited; returns a short-lived JWT |
| `GET /v1/auth/me` | viewer | `{sub, tenant_id, roles}` |
| `GET /v1/admin/users` | admin | own tenant |
| `POST /v1/admin/users` | admin | username `[a-z0-9._@-]{3,64}`, password 12-256, role viewer/operator/admin; cannot create in another tenant; **cannot create `platform` over HTTP** |
| `POST /v1/admin/users/{username}/disable` | admin | own tenant only (404 otherwise) |

### 9.3 Calls
| Route | Auth | Detail |
|---|---|---|
| `POST /v1/calls` | operator | Body `to`, `from`/`from_number`, optional `bridge_to`, `max_duration_s`, `metadata`, `ai_enabled`, `language`, `tenant_id` (must equal the token's), `idempotency_key`; `webhook_url` must be null. Header `Idempotency-Key` (8-128) **required** (or body field). **202** created, or **200** + `Idempotent-Replay: true` on replay. See §12.1 for the checks and error codes |
| `GET /v1/calls` | viewer | filters `state, direction, start, end`; cursor pagination (`limit ≤ 200`) → `{items, next_cursor}` |
| `GET /v1/calls/{id}` | viewer | detail (404 for other tenants) |
| `POST /v1/calls/{id}/hangup` | operator | idempotent; the **state changes only when FreeSWITCH confirms** (the response may still show the old state) |
| `POST /v1/calls/{id}/cancel` | operator | pre-connect only; 409 `already_connected` after answer |
| `POST /v1/calls/{id}/transfer` | operator | body `{destination}` must be in the tenant's `transfer_destinations`; call must be `answered`/`bridged`; **202** |
| `GET /v1/calls/{id}/events` | viewer | ordered lifecycle events (deduplicated, sanitised payloads) |
| `GET /v1/calls/{id}/session` | viewer | the linked session |
| `GET /v1/calls/{id}/recordings` | viewer | recording metadata |
| `GET /v1/calls/{id}/recording` | operator | like the above; 404 `recording_not_available` when none |
| `GET /v1/calls/{id}/transcript` | operator | **always 404 `transcript_not_available`**: transcripts are not stored (consent/retention policy pending) |
| `GET /v1/calls/{id}/summary` | operator | **always 404 `summary_not_available`** (not implemented) |

### 9.4 Sessions
`POST /v1/sessions` (operator; `tenant_id` must equal the token's else 403 `tenant_mismatch`; idempotent per call reference, also matches a FreeSWITCH UUID of a known call) · `GET /v1/sessions` (viewer; `status`, `q` over id/call/number/uuid, cursor) · `GET /v1/sessions/{id}` (viewer) · `POST …/answer` (operator, idempotent) · `POST …/end` (operator, terminal state immutable, first reason wins).

### 9.5 Recordings, usage, analytics
`GET /v1/recordings` (viewer list) · `GET /v1/recordings/{id}/download-url` (operator; presigned URL ≤1 h, default 5 min, `attachment`; **S3 backend only**, local backend refuses; audited) · `GET /v1/usage?start&end[&tenant_id]` (viewer; **measured** usage from persisted records; `estimated_charges` is `null` unless a rate is configured and is labelled an ESTIMATE; another tenant needs platform) · `GET /v1/analytics/calls?format=json|csv` (viewer).

### 9.6 Administration
`GET /v1/admin/policy` (admin, own tenant) · `PUT /v1/admin/tenants/{t}/policy` (**platform**: enabled, caller IDs, limits, country/prefix rules, transfer destinations, recording) · `PUT /v1/admin/tenants/{t}/routes` (platform: inbound number → tenant + action `park|bridge|hangup`; a number belongs to one tenant) · `GET /v1/admin/routes` (admin) · `DELETE /v1/admin/tenants/{t}/routes/{number}` (platform). **Tenants are denied until a policy with `enabled:true` exists.**

### 9.7 Webhook endpoints (admin)
`POST/GET /v1/webhooks/endpoints`, `DELETE …/{id}`, `GET /v1/webhooks/deliveries` (status filter, attempt history), `POST …/deliveries/{id}/replay`. Creating an endpoint validates the URL against SSRF rules and returns the **derived** signing secret.

### 9.8 AI contracts
| Route | Auth | Detail |
|---|---|---|
| `GET /v1/ai/config` | viewer | viewers see only `"configured"`/empty for URLs; admins see URLs |
| `PUT /v1/ai/config` | admin | STT/Brain/TTS/RAG URLs (host allow-list `AI_ALLOWED_HOSTS`), `enabled`, languages, model ids, timeouts (0.5-120 s), service paths; unknown keys 422; audited |
| `GET /v1/ai/health` | viewer | each service `healthy/unhealthy/unreachable/not_configured`, `media_bridge{name,ready,reason}`, `checked_at` |
| `POST /v1/ai/sessions/{sid}/start` | operator | **202**; refusals in fixed order (§12.6); state is `starting` until a media client really connects |
| `POST /v1/ai/sessions/{sid}/stop` | operator | idempotent |
| `GET /v1/ai/sessions/{sid}` | viewer | state, language, turns, `timings_ms`, error, media bridge status; endpoints only for admins |
| `WS /v1/ai/sessions/{sid}/media` | first message JWT (operator) | the service's own audio protocol (§13) |
| `WS /v1/ai/media/fs` | Bearer header | `mod_audio_stream` flavour of the same pipeline (§13) |

### 9.9 Provider callbacks
> **Updated 2026-10-21:** the Plivo Voice-Application path is now complete in repo B (answer → signed `<Stream>` → authenticated media WebSocket → existing AI pipeline → cleanup), **mock-verified only**. Routes now: `POST /v1/providers/plivo/{answer,status,hangup,stream-status}` and `WS /v1/providers/plivo/stream` (58 routes in total, see `ROUTES.md`). Served when `provider=plivo_api` or `MME_TEL_PLIVO_INBOUND_ENABLED=true`. Full design, files, configuration, Plivo Console settings, failure table and manual test: [PLIVO_VOICE_APPLICATION.md](PLIVO_VOICE_APPLICATION.md). The statements below describe the earlier, smaller behaviour.

`POST /v1/providers/plivo/status` and `/answer`: Plivo V3 signature + nonce replay store; unknown call → 200 `ignored`; duplicate → `duplicate`; only active when `provider=plivo_api`.

### 9.10 Static
`/dashboard/` serves the SPA (`index.html`, `app.js`, `style.css`) with `Content-Security-Policy: default-src 'self'… frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`; the JS uses DOM text nodes only (no HTML-string APIs). OpenAPI/Swagger at `/openapi.json`, `/docs` when `DOCS_ENABLED`.

## 10. Data model (MongoDB database `mme_telephony`, schema version 3)

| Collection | Key fields | Important indexes |
|---|---|---|
| `calls` | `_id, tenant_id, direction, provider, from/to_number, bridge_to, state, state_version, queued/initiated/ringing/answered/bridged/ended_at, ring_duration_s, connected_duration_s, duration_source, hangup_cause, failure_reason, fs_uuid, fs_uuids[], job_uuid, provider_call_id, origination_uncertain, cancel_requested, timed_out, transfer{current,attempts[]}, session_id, ai_enabled, language, recording_ids[], correlation_id, observed, purge_at` | `(tenant,created_at)`, `(tenant,state,created_at)`, `(state,queued_at)`, `fs_uuid`, `fs_uuids`, `(provider,provider_call_id)`, reporting, **unique inbound `fs_uuid`** (partial), TTL `purge_at` |
| `call_events` | append-only: `call_id, tenant_id, type, ts, source, dedup_id, payload, correlation_id` | **unique `(call_id, dedup_id)`**, `(tenant,call,ts)`, TTL |
| `idempotency` | `tenant, operation, key, fingerprint, call_id` | **unique `(tenant,operation,key)`**, TTL |
| `sessions` | `tenant, call_id, call_ref, fs_uuid, caller, destination, language, status, state_version, history[≤50], answered/ended_at, end_reason, source` | **unique `(tenant,call_ref)`** (partial), `(tenant,created_at)`, `(tenant,status)`, TTL |
| `ai_sessions` | `session_id (unique), tenant, state, language, models, turns, timings_ms, media{connected,…}, stop_reason, error` | unique session, `(tenant,state)` |
| `ai_config` | per-tenant URLs/models/timeouts/paths (no secrets) | unique tenant |
| `users` | `username (unique), password_hash (scrypt N=2^14), tenant_id, role, disabled, last_login_at` | unique username |
| `recordings` | `call_id, tenant, status (recording → available / failed / upload_failed / deleted), local_path, storage_key, size_bytes, consent_mode, retention_until` | `(tenant,call)`, `(status,created_at)` |
| `tenant_policies`, `inbound_routes`, `webhook_endpoints`, `webhook_deliveries` (unique `(endpoint,event)`), `provider_dedup`, `audit` | as named | tenant/number/due-time indexes, TTLs |
Consistency model: **no multi-document transactions** (works on standalone and Atlas). Correctness comes from compare-and-swap on `state_version`, unique indexes, and reconciliation. Retention via TTL indexes (`RETENTION_*` settings). Backup/restore: `scripts/backup_mongo.sh`, `restore_mongo.sh`, verified by `verify_backup_restore.sh` on local mongod (counts and index names identical).

## 11. State machines

**Call** (`app/telephony/state_machine.py`, full table `docs/CALL_STATE_MACHINE.md`):
`queued → initiating → ringing → answered → bridged ⇄ transferring → completed`; early exits `busy | no_answer | cancelled | failed`; terminal states are immutable. An event that implies an earlier state than the current one is recorded but ignored (`out_of_order`); a forbidden move records `invalid_transition` and keeps the state. Hangup-cause mapping: answered+`NORMAL_CLEARING` → completed; `USER_BUSY` → busy; `NO_ANSWER`/`NO_USER_RESPONSE` → no_answer; `ORIGINATOR_CANCEL` → cancelled; `CALL_REJECTED` → failed `rejected_by_callee`; unroutable → `invalid_destination`; gateway/congestion → `carrier_unavailable`.
**Session:** `created → answered → active (AI attached) → ended`, `failed` from any non-terminal state; `active → answered` when only the AI stops (the call continues); `ending` exists in the table but nothing sets it today. Synced after every applied call transition by `SessionService.on_call_state`.
**AI session:** `starting → active → media_disconnected → active …`, terminal `stopped` / `failed`. `active` is set **only** when a media client has connected and said hello.

## 12. Repo B: deep end-to-end flows

### 12.1 Outbound call (FreeSWITCH-gateway provider)
1. `POST /v1/calls` with JWT. Dependency order: authenticate → viewer/operator role → per-principal rate limit → per-tenant create-rate limit.
2. `create_call`: tenant policy must exist and be `enabled` (403 `tenant_calling_disabled`); PSTN-capable provider needs `REAL_CALLS_ENABLED` (403 `real_calls_disabled`); `tenant_id` in body must equal the token (403); `webhook_url` rejected; language must be `auto|hi|en|hinglish`; numbers normalised to E.164 (422 `invalid_number`); destination country allow-list (default `IN`) → `destination_country_denied`; deny prefixes (default `+911860,+911900,+91140` + policy file + tenant) → `destination_denied`; allow prefixes; caller ID must be one of the tenant's approved IDs (`caller_id_not_approved`); `bridge_to` must satisfy `transfer_destinations` (`transfer_not_permitted`).
3. **Idempotency:** claim `(tenant, "create_call", key)` by unique index with a request fingerprint. Replay → original call, 200 + header. Same key, different body → 409 `idempotency_key_reuse`. A concurrent duplicate waits ≤1 s for the first insert. If creation fails, the key is released so the client can retry.
4. **Admission:** active+queued vs `min(max_active_global, provider_max_concurrent)`, tenant cap, queue room (`MAX_QUEUED`, policy `queue|reject`) → 429 `capacity_exceeded` with `retry_after_s`; never a silent drop.
5. Persist the call as **`queued`** (this row *is* the durable queue), `state.queued` event, session created and linked, `call.created` webhook, audit row → **202**.
6. **Dispatcher** claims `queued → initiating` by CAS when: the ESL event stream is connected (queue is held, not lost, while FreeSWITCH is down), active < ceiling, the CPS token bucket allows, and the tenant is under its cap. Emits `call.initiated`.
7. **Originate:** generate `fs_uuid` and `job_uuid` and **persist them first**; then `bgapi originate {origination_uuid=…,mme_call_id=…,mme_tenant=…,mme_leg=a,origination_caller_id_number=…,originate_timeout=…,execute_on_answer='sched_hangup +N ALLOTTED_TIMEOUT'}<dial string> &park()` (or `&bridge(second party)`). Every interpolated value comes from allow-list builders (`dialstrings.py`); API input can never inject variables, applications or ESL lines. A lost/timed-out reply is **`ProviderUncertain` → flagged `origination_uncertain`, never retried**; a definite rejection → `failed` with a reason.
8. **ESL events** (deduplicated by `Event|uuid|sequence`): `CHANNEL_PROGRESS(_MEDIA)` → ringing; `CHANNEL_ANSWER` → answered (+ recording start if allowed); `CHANNEL_BRIDGE` → bridged (records the B-leg uuid; an implied answer is applied first); `CHANNEL_HANGUP(_COMPLETE)` or `BACKGROUND_JOB -ERR cause` → terminal via the cause table; `CUSTOM mme::transfer_failed` → transfer attempt closed and the state reverts.
9. Each event: insert into `call_events` (unique dedup) → CAS transition → metrics → webhook outbox rows → session sync → AI cleanup on terminal.
10. **Timestamps (important):** stored times use **this service's clock**. FreeSWITCH event timestamps were observed **2.5 days off** the host clock on this machine, which once caused the max-duration sweeper to hang up a live call; they are now used only as a same-domain delta (`duration_source: freeswitch_clock`). The WSL wall clock also steps by tens of seconds, so waits use monotonic time.
11. **Sweeper:** queue wait > `QUEUE_MAX_WAIT_S` → failed `queue_timeout`; `initiating` too long → hangup `PROGRESS_TIMEOUT` (uncertain ones are left to reconciliation); connected > `max_duration_s + 10 s` → `ALLOTTED_TIMEOUT`; `transferring` > `TRANSFER_TIMEOUT_S` → attempt `timeout`, state reverts to answered.
12. **Finish:** `ended_at`, `connected_duration_s`, `ring_duration_s`, `hangup_cause`, `failure_reason`; recording uploaded and size-verified; the call appears in history, usage and analytics.

### 12.2 Inbound call
*Managed context* `mme_inbound`: dialplan `ring_ready` + `park`; the controller sees `CHANNEL_CREATE/PARK`, normalises the dialled number, finds the route, checks tenant enabled and capacity, then either kills the channel (`UNALLOCATED_NUMBER`, `CALL_REJECTED`, `NORMAL_CIRCUIT_CONGESTION`; no record) or creates the call (`initiating → ringing`), `uuid_answer`, and applies the route action (park / bridge via `mme_transfer` / hangup).
*Record-only* (`MME_TEL_OBSERVE_CONTEXTS=public`): for contexts you already route yourself, the call and session are recorded and **no command is ever sent to FreeSWITCH**. Verified against your real `public` DID dialplan with a loopback call: one call + one session, `completed / NORMAL_CLEARING`, 0 channels left. Duplicate triggers (CREATE+PARK) are collapsed by an in-memory guard **and** a unique partial index.

### 12.3 Hangup and cancel
Tenant-scoped lookup → terminal: return as is (idempotent). `queued` (or claimed but not originated) → cancelled directly. Otherwise `uuid_kill` (`ORIGINATOR_CANCEL` pre-connect, `NORMAL_CLEARING` after). **The final state is taken from FreeSWITCH's hangup event (or reconciliation), never invented from our own request.**

### 12.4 Transfer
Destination checked against `transfer_destinations` → state must be answered/bridged → `transferring` (attempt recorded) → `uuid_setvar mme_xfer_dial <validated dial>` → `uuid_transfer <uuid> mme_xfer XML mme_transfer` into the fixed, controller-owned dialplan context (an earlier inline-dialplan approach killed the call and was replaced). Success = `CHANNEL_BRIDGE` (attempt `completed`, state bridged); failure/timeout = attempt `rejected`/`timeout`, state back to answered; call ends mid-transfer = attempt `call_ended`.

### 12.5 Recording
Starts only if **all** hold: `RECORDING_ENABLED`, a consent mode, a storage backend, tenant policy `recording=on`. `uuid_record start` to a spool path; after the call ends the housekeeper uploads, **verifies the stored size**, marks `available`, deletes the local file; access is a short-lived presigned URL (S3 only), audited; retention purge deletes object and marks `deleted`. Real FreeSWITCH recording to the **local** backend is tested; S3/KMS is not.

### 12.6 AI session start (all refusals are explicit; nothing pretends to start)
Order: session exists for this tenant (404 first) → global `AI_ENABLED` (409 `ai_disabled`) → tenant AI enabled (`ai_disabled_for_tenant`) → session answered (`session_not_answered`) → call answered/bridged (`call_not_answered`) → call flagged `ai_enabled` (`call_not_ai_enabled`) → **media bridge truly ready** (`media_bridge_unavailable`; `ws_fork` checks `module_exists mod_audio_stream`) → STT/Brain/TTS URLs set (`ai_not_configured`) → language valid. Then an `ai_sessions` row `starting` + runtime object + a 30 s watchdog; a media client must connect in 30 s (`media_connect_timeout` → `failed`), and after a drop has 30 s to reconnect (`media_reconnect_timeout`); the conversation resumes on reconnect. Hangup → `on_call_ended` cancels inference/playback, closes the socket, marks `stopped (call_ended)`; a stop/fail returns the session to `answered` because the call itself continues.
The FreeSWITCH media path in repo B (`WsForkBridge.begin`) mirrors repo A's: `uuid_setvar STREAM_EXTRA_HEADERS`, `uuid_audio_stream … start <AI_MEDIA_WS_URL> mono <rate> {session_id,…}`; playback via `mod_audio_stream::play` → `uuid_broadcast`; barge-in → `uuid_break`; stop → `uuid_audio_stream stop`.

### 12.7 Webhook delivery
Emitter writes one `webhook_deliveries` row per (endpoint, event) (unique index ⇒ re-emitting is harmless; queue bounded by `WEBHOOK_QUEUE_LIMIT`). Event types: `call.created|initiated|ringing|answered|bridged|transferring|completed|ended|failed|cancelled|busy|no_answer`, `session.created|answered|ended`, `ai.session.started|stopped|failed` (`transcript.ready`/`summary.ready` are declared but **never emitted**). Payload `{id,type,api_version:"2025-01",created_at,data{…}}`; numbers are masked; no recording URL. Headers: `X-MME-Event-Id`, `-Delivery-Id`, `-Timestamp`, `X-MME-Signature: v1=HMAC-SHA256(secret, "<ts>.<body>")`, `Idempotency-Key`. The per-endpoint secret is **derived** (`HMAC(master, endpoint_id:version)`), so no secret is stored. Worker: claim by CAS, re-validate the destination (SSRF: https only, no credentials in URL, private/loopback/link-local/metadata ranges blocked, redirects not followed), 2xx = delivered, else exponential backoff with jitter up to `WEBHOOK_MAX_ATTEMPTS` then `dead`; replay re-sends the same event id. Residual risk: DNS-rebinding between check and connect (mitigate with an egress firewall).

### 12.8 Recovery and reconciliation
On every ESL (re)connect and every 30 s: `show channels as json`. **If FreeSWITCH cannot be queried nothing changes.** A call with no channel is finalised only after its grace period (`max(grace, ring_timeout + 10 s)` for `initiating`/uncertain calls: a load test once exposed the premature-finalise bug); answered → `completed (reconciled_channel_missing)`, otherwise `failed (lost_before_answer | origination_uncertain_no_channel)`; live channels of already-terminal calls are hung up (orphans); missed ringing/answered states are caught up. A restart never re-originates, because the UUID was persisted before dispatch.

### 12.9 Dashboard
Login → JWT in `sessionStorage` → `GET /v1/auth/me` decides which nav items show. Pages: Overview (`/v1/dashboard/overview`), Live calls (SSE via `fetch`, automatic fallback to 3 s polling; search/filter), Call details (events timeline, session, AI pipeline, recordings; hang up / transfer / start-stop AI with `confirm()`), Call controls (outbound form with confirm), Sessions, Recordings, AI configuration (admin edit; banner when the media bridge is not ready), Logs & system, Users. Stale-render guard: each navigation renders into a detached box and attaches only if it is still the newest. Tested in real headless Chromium (3 tests, repeated).

## 13. Media WebSocket protocols (repo B `app/ai/ws.py`)
**Service protocol** `/v1/ai/sessions/{sid}/media`: C→S text `{"type":"auth","token":JWT}` (5 s) → S `auth_ok` → C `{"type":"hello","encoding":"pcm_s16le","sample_rate":8000|16000,"channels":1,"frame_ms":10|20|30|40}` → S `ready` (AI session becomes `active`) → C binary PCM frames; S `playback_start{utterance_id,sample_rate,language}` + paced binary frames + `playback_end`; S `interrupt` on barge-in; S `error{code,fatal}` (non-fatal examples `stt_timeout`, `brain_unreachable`, `inbound_overflow`); either side `bye`. Closes: 4401 unauthorized/auth required, 4403 forbidden (viewer role), 4404 no AI session, 4400 protocol/format.
**FreeSWITCH protocol** `/v1/ai/media/fs`: as §4.9 (Bearer ticket in the handshake header, metadata frame, binary frames, `streamAudio` replies, `uuid_break` for barge-in).
Shared loop (`serve_loop`): RMS endpointing (500 / 700 ms / ≥200 ms speech / 15 s cap), barge-in at ≥120 ms speech during playback, bounded queue of 2 with oldest-dropped, one worker task per connection, all tasks cancelled in `finally`.

## 14. Security model (both repos)
| Threat | Control |
|---|---|
| Unauthenticated REST (B) | JWT everywhere except health and signed provider callbacks; alg pinned; `alg=none` rejected |
| Cross-tenant access (B) | tenant only from token; every lookup scoped (404, not 403); tests attempt cross-tenant reads, hangups, transfers, webhooks, AI config, SSE, WS |
| Toll fraud (B) | tenant disabled by default; E.164 + country/prefix policy; approved caller IDs; transfer allow-list; `REAL_CALLS_ENABLED=false` by default; mock provider forbidden in staging/production |
| Command injection (A,B) | every FreeSWITCH string built by allow-list builders; transfers into fixed contexts; recording paths validated; ESL lines reject newlines; playback only of the module's own file for the same channel |
| Forged Plivo callbacks/streams (A,B) | V3 signature via SDK + nonce replay cache; public URL from config only; stream bound to a call by an HMAC ticket |
| Duplicate calls/sessions | unique-index idempotency; never retry uncertain origination; one session per call reference |
| Exposed ESL | loopback by default (settings reject non-private hosts unless explicitly overridden); password read from FreeSWITCH config, never logged |
| Secret leakage | `SecretStr` + `describe()` without values; log redaction of keys, URI credentials, inline `password=…`, bearer tokens, phone numbers; DTMF digits never logged; `.env` untouched/empty |
| SSRF (webhooks, model URLs) | https/host allow-lists, private ranges blocked, no URL credentials or query strings |
| DoS | body limits, rate limits (per principal, per tenant, per IP for login), bounded queues, `MAX_STREAMS`, per-tenant caps |
| Dashboard XSS/clickjacking | text-node rendering only, CSP `default-src 'self'`, `frame-ancestors 'none'` |
**Known weak points:** repo A's session routes are unauthenticated (loopback only); rate limiter/queues are per-process; JWT secret rotation needs a restart; MME's real identity format is **unverified** (assumes HS256 with `tenant_id`/`roles`); compliance (TRAI/DoT, recording consent) **not reviewed**; AGPL-3.0 licence of `mod_audio_stream` needs review.

## 15. Observability
JSON logs (service, env, request/correlation ids, call/session ids, redaction). Prometheus (`/metrics`): active/queued calls, calls created/terminal by state, hangup reasons, origination outcomes, setup/ring/connected histograms, provider errors, ESL connected/reconnects/lag/queue/dropped/command failures, events processed, webhook results/latency/dropped, DB latency/failures, API latency by route group, reconcile actions, recording failures, sessions, AI requests/sessions, logins, rate-limited requests. Labels are bounded enums only (never numbers or ids). Dashboard shows live health with `last_ok_at`. Alert thresholds and runbooks: `docs/OPERATIONS_RUNBOOK.md`.

## 16. Configuration
Repo B: 134 parameters, prefix `MME_TEL_`, validated at startup (cross-field rules; production demands real secrets ≥32 chars and forbids the mock provider); authoritative reference `docs/CONFIGURATION_REFERENCE.md`, template `.env.example`, `scripts/validate_config.py`. Repo A: ~35 variables listed in §5.1 and `.env.example`; `GET /health/voice` shows what is missing. **Both `.env` files are empty today.**

## 17. Tests
**Repo A (92):** sessions/config 14 · audio, endpointing, adapters, orchestration 27 (mock models) · Plivo (signatures, XML, WS start/media/stop, outbound audio, barge-in, disconnect, limits) 27 (mock Plivo) · ESL parsing/reconnect/bridge 20 (fake ESL) · live FreeSWITCH 4 (real FreeSWITCH + real module).
**Repo B (169 + 1 skipped):** unit (state machine, phone/dial strings, config), API integration, Plivo callbacks, real-FreeSWITCH (outbound, busy, cancel, transfer, inbound, reconnect, restart recovery, orphan cleanup, recording), AI contracts (mock models, synthetic client), AI through the real module, resilience (DB down, race, malformed events, timeouts), headless-browser dashboard (3), and synthetic load (`tests/load/run_load.py`: 1,000 mock calls, 300 loopback calls and 30 × 180 s through real FreeSWITCH at 30 concurrent, 0 errors; a **floor**, not a capacity guarantee).
Run: repo A `cd ~/MME_TELEPHONY && .venv/bin/python -m pytest -q --timeout=40`; repo B `bash scripts/run.sh pytest -q` (needs `bash scripts/dev_mongo.sh` and FreeSWITCH; live tests skip when unreachable).

## 18. Start and stop
```bash
# FreeSWITCH (unchanged; unsupervised: it does not restart itself after a WSL restart)
/usr/local/freeswitch/bin/freeswitch -nc -np …   # stop: fs_cli -x shutdown  /  kill <pid>
# Repo A
cd ~/MME_TELEPHONY
.venv/bin/uvicorn ai_counselling.app:app --host 127.0.0.1 --port 8090     # restart to load the new code
.venv/bin/python -u -m mme_telephony.esl_events                            # run exactly ONE copy
curl -s localhost:8090/health/voice
# Repo B
cd /mnt/c/Users/Admin/Desktop/MME_Telephony && bash scripts/dev_mongo.sh
bash scripts/run.sh uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8800 --workers 1
python scripts/create_user.py <name> <tenant> <viewer|operator|admin|platform>   # password via env or prompt
```
Never start a public tunnel until `/plivo/answer` answers **403** to an unsigned request in the production configuration.

## 19. What is verified, what is not
| Item | Status |
|---|---|
| Call lifecycle, queue, idempotency, recovery, tenant isolation, webhooks, RBAC, dashboard | verified (automated, real FreeSWITCH loopback, real browser) |
| `mod_audio_stream` round trip (module ⇄ our socket ⇄ playback ⇄ barge-in ⇄ hangup) | verified on loopback with a **tone caller and mock models** |
| Real Plivo call (either path) | **not done** |
| Real Hindi/English/Hinglish speech through real STT/LLM/TTS; real latency | **not done**: no model endpoints, no GPU |
| 8 kHz PSTN codec behaviour, echo, VAD tuning on real calls | **not done** |
| Plivo protocol corner cases (`playAudio` contentType spelling, wss handshake URI, limits) | **unverified** |
| MongoDB Atlas, S3/KMS, production host/TLS/firewall, compliance | **not done** |
| Browser/WebRTC calling, multi-node HA, transcripts/summaries | **not implemented** |

## 20. Next engineering steps (in order)
1. Provide the three model endpoints (+ key) and an approved test number; run the live procedure in `docs/LIVE_TEST_PROCEDURE.md` (and `~/MME_TELEPHONY/docs/VOICE_SETUP.md` §10) and record real latencies.
2. Change the DID dialplan from the tone to answer + park/sleep with `mme_ai=1`.
3. Converge repo A's voice engine into repo B's durable lifecycle (one session model, one media endpoint).
4. Persist transcripts only after a consent/retention policy exists; add summaries.
5. Production host (TLS, firewall, supervised FreeSWITCH), Atlas, S3, MME identity integration, compliance review.
