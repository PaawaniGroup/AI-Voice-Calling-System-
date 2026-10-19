# Live test procedure (manual, costs money, needs an approved number)

Nothing here runs automatically. Do not run it until every **Inputs** item exists. Stop at the first failed check and record the result in `docs/LIVE_TEST_RESULTS.md`.

## Inputs required from the operator
| Input | Why | Status |
|---|---|---|
| One **approved test destination** in E.164 (a phone you hold and consent to call) | outbound test | **missing** |
| A second phone (or the same one) to **dial the DID `+918065530418`** | inbound test | **missing** |
| Confirmation that the Plivo trunk allows your current public IP, outbound CLI `+918065530418` and the destination country | carrier acceptance | **unverified** |
| Plivo account concurrent/CPS limits | set `PROVIDER_MAX_*` | **unknown** |
| URLs of the STT, Brain and TTS servers (+ optional RAG) and, if needed, an API key | AI test | **missing** |
| Exposure of the service/model hosts to each other (the allow-list `MME_TEL_AI_ALLOWED_HOSTS` must contain their hostnames) | AI test | **missing** |

## A. Carrier test (no AI)
1. Env: `MME_TEL_PROVIDER=freeswitch_gateway`, `MME_TEL_FS_GATEWAY=plivo-mme`, `MME_TEL_OBSERVE_CONTEXTS=public`, `MME_TEL_REAL_CALLS_ENABLED=true`, `MME_TEL_MAX_ACTIVE_GLOBAL=1`, `MME_TEL_MAX_ACTIVE_PER_TENANT=1`.
2. Tenant policy: `enabled:true`, `caller_ids:["+918065530418"]`, `allowed_countries:["IN"]`, `max_active_calls:1`, `max_duration_s:60`; inbound route for the DID.
3. **Outbound:** `POST /v1/calls {"to":"<approved>","from":"+918065530418"}`. Expect `queued → initiating → ringing → answered`; answer the phone; hang up from the dashboard. Record: setup time (initiating→answered), hangup cause, `connected_duration_s`, and compare with the Plivo CDR.
4. **Inbound:** dial the DID from the second phone. Your current dialplan plays a 1 s tone and hangs up; expect one call + one session (`observed`), `completed`, `NORMAL_CLEARING`.
5. Pass criteria: states and durations match the carrier within ±2 s, one session per call, no leftover channel (`show channels count` = 0).

## B. AI test (only after A passes and the media module + models exist)
1. `MME_TEL_AI_ENABLED=true`, `MME_TEL_AI_MEDIA_BRIDGE=ws_fork`, `MME_TEL_AI_MEDIA_WS_URL=ws://127.0.0.1:8800/v1/ai/media/fs` (must be reachable from FreeSWITCH), `MME_TEL_AI_ALLOWED_HOSTS=<model hosts>`; confirm `module_exists mod_audio_stream` is `true`; dashboard → AI configuration: URLs + enable; `GET /v1/ai/health` must show every service `healthy` and the bridge `ready`.
2. Outbound call with `ai_enabled:true`; after answer `POST /v1/ai/sessions/{sid}/start`; expect `starting → active` only when the media client connects.
3. Speak Hindi, English and Hinglish prompts; record per-turn `timings_ms` (stt/rag/brain/tts) from `GET /v1/ai/sessions/{sid}` and end-to-end silence-to-first-audio by ear/stopwatch or a recording.
4. Interrupt the assistant mid-sentence: expect an `interrupt` and silence within ~300 ms (measure).
5. Hang up from the phone: the AI session must be `stopped (call_ended)` and no audio continues.
6. Pass criteria are the operator's: suggested ≤1.5 s turn latency median and ≤300 ms barge-in stop; report actual numbers, do not round them.
