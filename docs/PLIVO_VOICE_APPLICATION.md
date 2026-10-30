# Plivo Voice Application → MME_Telephony → AI pipeline

Implemented inside the existing service (`C:\Users\Admin\Desktop\MME_Telephony`, FastAPI on :8800). No second application, no parallel
telephony stack. Verified **only with mocked Plivo and mocked models** (see §12); **no real Plivo call has been made**.

> **Status box**
> | Question | Answer |
> |---|---|
> | Code implemented and covered by automated tests | **Yes**: 69 new tests; full suite 238 passed, 1 skipped (the opt-in real-Plivo test) |
> | Real Plivo call (inbound) | **Not done**: needs your Plivo Application, a public HTTPS/WSS URL and an approved test call |
> | Real STT/LLM/TTS | **Not done**: no endpoints configured; with none configured the route refuses to open a stream |
> | Number `+918065530418` / SIP trunk / dialplan | **Untouched**. It still routes Plivo → SIP → FreeSWITCH. Nothing here re-points it |
> | Public tunnel | **Not started** (see §9 gate) |

## 1. Call flow

```
 Caller ─PSTN─► Plivo number ─► Plivo Application (Answer URL, Hangup URL)
                                   │ ① POST https://<public>/v1/providers/plivo/answer     (X-Plivo-Signature-V3 + nonce)
                                   ▼
   MME_TELEPHONY  routes.plivo_answer ─► verify signature (public URL from config, never Host/X-Forwarded-*) ─► nonce replay store
     └─ services/plivo_calls.handle_answer
          • CallUUID valid?  existing call? (duplicate/retry/replay ⇒ same decision, no 2nd call)
          • To → inbound route → tenant policy enabled → capacity → (route action "ai") AiService.preflight
          • durable call (unique per Plivo CallUUID) → Controller.apply(ANSWERED) → session created/linked, webhooks, metrics
          • XML:  <Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000"
                          extraHeaders="mme_ticket=<signed ticket>" statusCallbackUrl="…/stream-status">wss://<public>/v1/providers/plivo/stream</Stream>
                                   │ ② WSS handshake (X-Plivo-Signature-V3, method GET)
                                   ▼
   ws.handle_plivo_media: verify handshake + nonce ─► accept ─► `start` event ─► ticket must name THIS call/tenant/session,
        call must be `answered`, mediaFormat must be mulaw/8000 ─► AiService.start(bridge=PlivoStreamBridge) ─► attach_media
        ─► media_connected  (AI session `active`, session `active`)                          [existing AI lifecycle]
                                   │ ③ caller audio:  media{track:inbound, chunk, payload=base64 μ-law 8 kHz}
                                   ▼
   PlivoIO.next_item ─ base64 strict · chunk order (dups dropped) · μ-law→PCM16 ─► shared serve_loop (VAD/endpointing/barge-in)
        ─► Conversation: STT ─► [RAG] ─► Brain ─► TTS (WAV, validated) ─► PlivoIO.play: resample→8 kHz→μ-law→ playAudio chunks + checkpoint
        barge-in ─► clearAudio{streamId}                                                        [existing STT/Brain/TTS adapters]
                                   │ ④ caller hangs up: Plivo POST /v1/providers/plivo/hangup (completed)  and/or WS `stop`
                                   ▼
   Controller.apply(COMPLETED) ─► session ended ─► AiService.on_call_ended ─► inference cancelled, socket closed, runtime freed
   WS finally: ai.stop() (idempotent) · `stream.ended` event · stream set released
```
Duplicate callbacks, backstops and failures: §8.

## 2. How it reuses the existing system

| Concern | Existing component reused (unchanged unless noted) |
|---|---|
| Durable call + session, state machine, event dedup | `Repos`, `Controller.apply`, `SessionService` (session created/linked by the controller on the `answered` transition) |
| Tenant routing / policy / capacity | `inbound_routes`, `tenant_policies`, `count_active`; route action **`ai` added** (`park`/`bridge`/`hangup` behave exactly as before) |
| Signature validation | `plivo.verify_signature` (SDK `validate_v3_signature`), nonce store `provider_dedup` |
| AI lifecycle | `AiService.start/attach_media/media_connected/stop/on_call_ended`, per-tenant `ai_config`, health. **Added:** `preflight()` and an optional `bridge=` argument (Plivo opens the socket itself) |
| STT/Brain/TTS/RAG | `ModelClients`, `Conversation` (untouched) |
| Conversation loop (VAD, barge-in, queue, backpressure) | `ws.serve_loop`. **Generalised:** accepts an adapter item source, counts real frame duration, treats a gap with no frames as silence (also benefits the FreeSWITCH path) |
| Webhooks, audit, metrics, dashboard, API | unchanged; Plivo calls appear in `/v1/calls`, `/v1/sessions`, SSE, dashboard, webhooks (`call.*`, `session.*`, `ai.session.*`) |
| FreeSWITCH / SIP / outbound / queues | unchanged. `Controller.provider_for(call)` routes a Plivo call back through the Plivo adapter, everything else through the configured provider |

**Not interchangeable with FreeSWITCH media:** Plivo uses JSON text frames (base64 μ-law 8 kHz, `playAudio`/`clearAudio`); `mod_audio_stream` uses binary L16 frames and `streamAudio` JSON. The Plivo path therefore has its own adapter (`PlivoIO`) and its own route, and the FreeSWITCH media endpoints refuse Plivo calls (a test asserts `POST /v1/ai/sessions/{id}/start` on a Plivo call returns `media_bridge_unavailable`).

## 3. Files

| File | Change | Why |
|---|---|---|
| `app/services/plivo_calls.py` | **new** | Answer / status / stream-status logic (idempotent, policy, XML) |
| `app/ai/ws.py` | changed | `PlivoIO` adapter, `handle_plivo_media`, `serve_loop` item-source + idle handling |
| `app/ai/codec.py` | **new** | μ-law ↔ PCM16, strict payload decode, fast resample |
| `app/security/tickets.py` | **new** | purpose-limited HMAC stream ticket (deliberately not a JWT) |
| `app/providers/plivo.py` | changed | `plivo_enabled`, public-URL helpers, WS handshake signature |
| `app/ai/media.py`, `app/ai/service.py` | changed | `PlivoStreamBridge`, `preflight`, `start(bridge=)`, health entry |
| `app/api/routes.py` | changed | callbacks moved to thin routes; added `/hangup`, `/stream-status`; signature URL now includes the query string |
| `app/api/routes_platform.py` | changed | `WS /v1/providers/plivo/stream` |
| `app/api/schemas.py` | changed | route action `ai` |
| `app/telephony/controller.py` | changed | `providers` map + `provider_for`; sweeper backstops (§8) |
| `app/main.py`, `app/persistence/db.py`, `app/observability/metrics.py` | changed | wiring; schema v4 (unique inbound Plivo CallUUID index); counters |
| `config/settings.py`, `.env.example`, `docs/CONFIGURATION_*.md`, `docs/ROUTES.md` | changed/regenerated | 5 new settings + validation |
| `tests/…` | 3 files new, `helpers.py` small addition | §12 |

## 4. Routes (all public provider routes are signed; none uses a JWT)

| Route | Purpose | Security |
|---|---|---|
| `POST /v1/providers/plivo/answer` | Answer URL → Plivo XML | V3 signature + nonce; `404` unless `plivo_enabled`; `400 invalid_payload` for a bad `CallUUID`; `413` over the body limit |
| `POST /v1/providers/plivo/hangup` | Hangup URL → call completion | same; unknown call ⇒ `{"status":"ignored"}` so Plivo does not retry forever |
| `POST /v1/providers/plivo/status` | ring / progress (same handler) | same |
| `POST /v1/providers/plivo/stream-status` | `<Stream statusCallbackUrl>` started/stopped/failed | same; deduplicated per `(StreamID, Event)`; **never changes call state**; `failed` ends the AI session and asks Plivo to hang up |
| `WS /v1/providers/plivo/stream` | bidirectional media | handshake V3 signature (GET) + nonce replay + 5 s `start` + signed ticket bound to call/tenant/session + one stream per session + `PLIVO_STREAM_MAX_CONCURRENT` |

Answer decisions (`<Response>`): `ai` → `<Stream>`; `park` → `<Wait length>`; `bridge` → `<Dial>`; `hangup` → `<Hangup/>`; unrouted, tenant disabled, bad number, replay for an unknown call → `<Hangup reason="rejected"/>`; capacity → `<Hangup reason="busy"/>`; **AI not ready** (global switch, tenant switch, URLs, Plivo config) → the call is **recorded as `failed` with `ai_unavailable:<code>`** and rejected (never left on a dead stream). Calls we placed through the Plivo REST API keep their old behaviour (`<Dial>`/`<Wait>`); AI on outbound Plivo calls is not implemented.

## 5. Plivo protocol facts used (and what is unverified)

Taken from Plivo's published audio-streaming reference: `<Stream>` attributes `bidirectional`, `keepCallAlive`, `contentType` (`audio/x-mulaw;rate=8000`, `audio/x-l16;rate=8000|16000`), `statusCallbackUrl/Method`, `extraHeaders` (`k=v;k=v`), URL ≤ 2048 chars; inbound events `start{callId,streamId,mediaFormat,…}+extra_headers`, `media{track,timestamp,chunk,payload}` (~20 ms), `dtmf`, `playedStream{name}`, `clearedAudio`; outbound `playAudio{media:{contentType,sampleRate,payload}}`, `checkpoint{streamId,name}`, `clearAudio{streamId}`; handshake headers `X-Plivo-Signature-V3` / `-Nonce`, validate with method **GET** over the full URI; queue ≈ 60 s; keep messages ≤ 64 KB / ≤ 16 KB base64.

**UNVERIFIED against a real Plivo call (first live test will settle them):**
1. `playAudio.media.contentType` spelling: we send `"audio/x-mulaw"` + `sampleRate: 8000` (the reference's example); the Stream attribute uses `;rate=8000`.
2. Which URI form Plivo signs for the WSS handshake: the SDK helper only accepts `http(s)`. We accept the HMAC over `wss://…` **or** its `https://` twin (same host, path, query, secret) and nothing else.
3. That Plivo returns `extraHeaders` inside the `start` event as the reference states (the ticket relies on it; without it every stream is refused: **fail closed**).
4. Behaviour of `keepCallAlive` after the stream ends, and Plivo's close codes.
5. That the Application's Hangup URL receives `CallStatus=completed` with `CallUUID` for inbound calls.

## 6. Configuration

New (all validated at startup; defaults are safe, feature **off**):

| Variable | Default | Meaning |
|---|---|---|
| `MME_TEL_PLIVO_INBOUND_ENABLED` | `false` | serve the Plivo callbacks for **inbound** calls without changing the outbound provider |
| `MME_TEL_PLIVO_STREAM_TICKET_TTL_S` | 120 | ticket lifetime (30-900) |
| `MME_TEL_PLIVO_STREAM_CONNECT_S` | 20 | stream must connect within this time or the call is hung up (5-120) |
| `MME_TEL_PLIVO_STREAM_MAX_CONCURRENT` | 10 | simultaneous Plivo AI streams in this process |
| `MME_TEL_PLIVO_ORPHAN_GRACE_S` | 60 | past max duration, an unconfirmed Plivo hangup is closed locally after this (10-900) |

Required when enabled (startup fails with a message naming the variable, never its value): `MME_TEL_PLIVO_AUTH_TOKEN` (secret; verifies signatures), `MME_TEL_WEBHOOK_PUBLIC_BASE_URL` (`https://…`, no trailing slash; the **only** source of the signed URL and of the `wss://` URL), `MME_TEL_JWT_SECRET` (the ticket key is derived from it; rotating it invalidates outstanding tickets). Recommended: `MME_TEL_PLIVO_AUTH_ID` (secret; without it REST hangups are impossible and you get a startup warning). AI: `MME_TEL_AI_ENABLED=true` and the tenant's AI config (STT/Brain/TTS URLs on `MME_TEL_AI_ALLOWED_HOSTS`). Full reference: `docs/CONFIGURATION_REFERENCE.md` (139 parameters). Python 3.13 needs `audioop-lts` (in `requirements.txt`).

Tenant setup (placeholders; platform admin token needed for the first two):
```bash
H='Authorization: Bearer <PLATFORM_ADMIN_JWT>'; U=http://127.0.0.1:8800
curl -X PUT $U/v1/admin/tenants/<tenant>/policy -H "$H" -H 'Content-Type: application/json' \
     -d '{"enabled":true,"max_active_calls":5,"max_duration_s":900}'
curl -X PUT $U/v1/admin/tenants/<tenant>/routes -H "$H" -H 'Content-Type: application/json' \
     -d '{"number":"+91XXXXXXXXXX","action":"ai"}'                     # <- the Plivo number you assign to the Application
curl -X PUT $U/v1/ai/config -H 'Authorization: Bearer <TENANT_ADMIN_JWT>' -H 'Content-Type: application/json' \
     -d '{"stt_url":"http://<host>:<port>","brain_url":"http://<host>:<port>","tts_url":"http://<host>:<port>","enabled":true}'
curl $U/v1/ai/health -H 'Authorization: Bearer <JWT>'                  # every service must be "healthy"; plivo_stream.ready must be true
```

## 7. Plivo Console configuration (operator action: nothing here is done automatically)

In the Plivo console create (or edit) a **Voice Application** (Voice → Applications; menu names may differ slightly):

| Field | Value |
|---|---|
| Answer URL | `https://<public-host>/v1/providers/plivo/answer` |
| Answer Method | `POST` |
| Hangup URL | `https://<public-host>/v1/providers/plivo/hangup` |
| Hangup Method | `POST` |
| Ring / Fallback URL | not used (leave empty) |
Then assign the **test number** to that Application. The Stream's `statusCallbackUrl` and the WebSocket URL are generated into the Answer XML from `MME_TEL_WEBHOOK_PUBLIC_BASE_URL`; there is nothing to configure for them in the console. `<public-host>` must be exactly the host in `MME_TEL_WEBHOOK_PUBLIC_BASE_URL` (signatures are computed over that URL).

**Do not re-assign `+918065530418`** (currently routed Plivo → SIP trunk → FreeSWITCH) as a first step. Use a separate Plivo test number/Application, or explicitly accept that moving the live DID changes production routing. Re-assigning is reversible in the console, but it is **your** decision and was not performed.

## 8. Failure and duplicate behaviour

| Situation | Behaviour (tested) |
|---|---|
| Duplicate / retried Answer, same nonce (replay) or new nonce, 10 concurrent | exactly one call and one session (unique index + idempotent apply); every response is the same decision (`<Stream>`, never `<Hangup>`); a replay of an *unknown* call creates nothing |
| Bad / missing signature, wrong host, tampered body, forged forwarding headers | `401 invalid_signature`; nothing created; token never echoed |
| Malformed CallUUID / oversize body / invalid `To` | `400 invalid_payload` / `413` / rejected XML |
| Unrouted, tenant disabled, capacity | rejected / busy XML, no call record (as before) |
| AI not ready at Answer | call recorded `failed` (`ai_unavailable:…`), caller rejected |
| AI becomes unavailable before the stream starts | stream closed `1011`, `ai.start_failed` event, Plivo asked to hang up |
| Stream never connects | sweeper hangs the call up once (`stream.not_connected`) |
| Bad ticket / other call / other tenant / expired / wrong format / second use | closed `1008`/`1003`; nothing started |
| Duplicate or out-of-order `media` chunk, bad base64, oversize payload, wrong track | dropped and counted; stream continues |
| Model error / timeout | no audio, stream continues, next turn recovers |
| Barge-in | `clearAudio`; call continues |
| Caller hangs up (Hangup callback) | call completed, session ended, **in-flight inference cancelled, no audio afterwards**, socket closed |
| `stop` event or socket drop | AI session stopped (`stream_stopped`/`ws_disconnected`), runtime freed; the call itself is ended only by Plivo's Hangup callback |
| Plivo REST hangup unavailable/failing | API answers `502 hangup_failed` (no invented success); past max duration + grace the call is closed locally (`TIMEOUT_UNCONFIRMED`) and its AI session cleaned |
| Answer received but process died before completing | sweeper fails it (`answer_not_completed`) |

## 9. Gate before exposing the service publicly (Cloudflare Tunnel or any reverse proxy)

Do **not** start the tunnel until all hold on the running instance: (1) `python scripts/validate_config.py` → `config OK`; (2) `curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8800/v1/providers/plivo/answer -d CallUUID=x` → **401** (not 200/503); (3) `GET /v1/ai/health` shows `plivo_stream.ready: true`; (4) only `/v1/providers/plivo/*` is routed publicly (the dashboard, `/v1/*` management routes, `/metrics` and `/docs` stay private, or behind the proxy's own auth); (5) TLS terminates at the proxy and the public host equals `MME_TEL_WEBHOOK_PUBLIC_BASE_URL`; (6) the proxy passes WebSocket upgrades and the `X-Plivo-*` headers unchanged and does **not** rewrite the request path (the signature covers `<base-url><path>`; a rewritten path fails closed with 401).

## 10. Run and test

```bash
cd /mnt/c/Users/Admin/Desktop/MME_Telephony
bash scripts/dev_mongo.sh                                  # test/dev MongoDB (127.0.0.1:27117)
python scripts/validate_config.py
bash scripts/run.sh uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8800 --workers 1   # stop: Ctrl-C / SIGTERM
bash scripts/run.sh pytest -q tests/unit/test_plivo_stream.py tests/integration/test_plivo_voice.py   # Plivo feature (69 tests)
bash scripts/run.sh pytest -q                                                                          # everything incl. real-FreeSWITCH + browser tests
python scripts/gen_config_docs.py && python scripts/gen_routes_doc.py                                  # regenerate reference docs
```

## 11. Safe manual test: one inbound call (needs your approval and the §9 gate)

Prereqs: a **non-production** Plivo number assigned to the Application (§7), `PLIVO_*`/`WEBHOOK_PUBLIC_BASE_URL`/`AI_*` set, three model endpoints reachable, tenant policy + `ai` route + AI config (§6), `MME_TEL_MAX_ACTIVE_GLOBAL=1`, `MME_TEL_PLIVO_STREAM_MAX_CONCURRENT=1`, `MME_TEL_MAX_CALL_DURATION_S=120`.
1. Watch: `tail` the service log and `GET /v1/dashboard/overview` (or the dashboard's Live calls).
2. Call the number from **your own phone** (one call, cost = one short inbound call). Expect: call `answered` immediately; AI session `starting → active` within `PLIVO_STREAM_CONNECT_S`.
3. Say a short sentence in Hindi, English and Hinglish in turn. Record `GET /v1/ai/sessions/<sid>` → `timings_ms` (`stt`, `brain`, `tts`, `turn_ms`) and your own stopwatch for silence-to-first-audio.
4. Interrupt the assistant mid-sentence; it should stop within a fraction of a second.
5. Hang up from the phone. Expect within ~2 s: call `completed`, session `ended`, AI session `stopped (call_ended)`, `ai.runtime` empty (no leftover task), no `stream.not_connected`.
6. Record every observation, including the five unverified items in §5, in a results note. Any `1008` close or `bad_ticket` log line right after `start` means item 3 of §5 (extra headers) needs a design change; **stop and tell me**.
7. Stop here. Re-point nothing else.

## 12. Test evidence (what is mocked)

| Suite | Tests | Mocked | Real |
|---|---|---|---|
| `tests/unit/test_plivo_stream.py` | 25 | none needed | codec, ticket, signature helper, adapter parsing/formatting, config |
| `tests/integration/test_plivo_voice.py` | 44 | **Plivo** (SDK-signed requests, fake token, test WS client), **models** (httpx mock), Plivo REST client (fake) | service, controller, MongoDB, AiService, conversation loop, real HTTP + WebSocket server |
| Existing suite (regression) | 169 passed + 1 skipped | as before | real FreeSWITCH + `mod_audio_stream`, headless browser |
Total last run: **238 passed, 1 skipped**, 0 leftover FreeSWITCH channels; `pyflakes` clean for `app/`, `config/`, `scripts/` (5 old cosmetic warnings in untouched test files). No static type checker is configured in this repository.
What the 69 tests assert: valid Answer + XML structure and attributes; signature failures (missing, wrong token, tampered body, other URL/host, spoofed forwarding headers); malformed payloads; replay, retry and 10-way concurrent duplicates; UUID↔call↔session↔stream mapping; tenant isolation; denied/busy/disabled/unrouted inbound; AI-not-ready paths (3 causes); other route actions unchanged; status/hangup dedup and complete cleanup; stream-status; handshake auth and replay; 7 ticket-rejection cases; one-stream-per-ticket and cross-tenant ticket; full STT→Brain→TTS turn with the **8 kHz WAV reaching STT** and μ-law `playAudio` + `checkpoint` coming back; second turn context; media robustness; barge-in `clearAudio`; 55 s reply truncation; `stop`/drop cleanup; hangup mid-inference (no audio after); model failure recovery; AI-start failure; capacity; watchdogs (never-connected stream, orphan, incomplete answer); provider routing (Plivo hangup never goes to FreeSWITCH; honest `502`); regression of outbound Plivo callbacks, health, AI health, and the FreeSWITCH-only AI start refusal.

## 13. Not supported / known limitations
* **Recording** of Plivo-delivered calls: not implemented (recording is `uuid_record` on FreeSWITCH). No recording rows are created; the dashboard shows none. Plivo-side recording (`<Record>` / REST) would be a separate feature.
* **Transfer** of Plivo-delivered calls: unsupported (`502 transfer_failed`; the call survives). **AI on outbound Plivo calls**: not implemented. **DTMF**: counted, ignored.
* After the media socket ends the AI cannot be restarted on the same call.
* Single worker process (as for the whole service): the stream set, queues and rate limiter are per process.
* Energy-based VAD (RMS 500) is untuned for real PSTN audio; 8 kHz μ-law quality and echo are untested with humans.
* Timings are not yet measured on a real call; the transport adds only milliseconds, the 700 ms end-of-speech wait and real STT+LLM+TTS time dominate.
* Plivo account limits (concurrency, CPS, regions, pricing for streaming) are **unverified**; set `PROVIDER_MAX_*` from your account.
* Compliance (TRAI/DoT, call-recording consent, DLT) is not reviewed.

## Outbound AI calls (Plivo)

`POST /v1/calls` with `"ai_enabled": true` on a deployment whose `provider` is `plivo_api` places the call and, when the recipient answers, opens the SAME bidirectional AI stream as an inbound `ai` route (`plivo_calls._ai_stream_xml`, `ws.handle_plivo_media`, one conversation pipeline).

* Creation refuses with 409 `ai_not_ready` unless `AiService.preflight` passes (AI enabled globally and for the tenant, STT/Brain/TTS URLs set, Plivo stream configuration present). An AI call cannot also set `bridge_to` (422 `ai_bridge_unsupported`).
* The call is stored with `ai_outbound: true`; the Answer handler returns `<Stream>` (with the signed ticket and the stream watchdog) for it and `<Wait>`/`<Dial>` for every other outbound call, exactly as before. The stream handler refuses an outbound AI stream without a valid ticket.
* An Answer that arrives before the originate response stored the RequestUUID waits up to 3 s for it instead of hanging up.
* A Plivo outbound call whose originate outcome is unknown is failed (`origination_uncertain_no_channel`) after `initiating_timeout_s`, so it cannot hold a capacity slot forever.
* Safety controls (all fail-closed, none is a financial cap):
  * Tenant opt-in: policy `outbound_ai_enabled` (default false) and `outbound_ai_destinations`, the EXACT E.164 numbers an AI call may dial (no prefixes or wildcards; empty = none). A policy PUT that omits them resets them to closed.
  * Daily quota per tenant: policy `outbound_ai_daily_quota`, default `MME_TEL_OUTBOUND_AI_DAILY_QUOTA` (20, UTC day, 0 blocks). Taken atomically in MongoDB (`usage_counters`), given back if the call cannot be stored; idempotent replays do not consume it.
  * Duration: policy `outbound_ai_max_duration_s`, default `MME_TEL_OUTBOUND_AI_MAX_DURATION_S` (300), always combined with the ordinary call cap.
  * Emergency pause of NEW outbound AI calls (all tenants): `MME_TEL_OUTBOUND_AI_PAUSED=true` or, without a restart, `PUT /v1/admin/outbound-ai/pause` (platform role); `GET` shows the state. Calls already running are not touched.
  * Audit/accounting: `outbound_ai.rejected`, `outbound_ai.quota_exhausted`, `outbound_ai.pause|resume` audit records; an `ai.usage` call event and `ai_usage` on the call (AI seconds, turns, STT/RAG/Brain/TTS requests started) when the AI session ends.
* Not covered: FreeSWITCH/mock providers (they still only store `ai_enabled`), recordings for Plivo calls, an outbound-specific greeting (it reuses `AI_GREETING`), a hard financial cap, per-minute/hour rate limits for inbound.
* Evidence: `tests/integration/test_outbound_ai.py` (mocks only; nothing here has been tried with real Plivo or a real phone).
