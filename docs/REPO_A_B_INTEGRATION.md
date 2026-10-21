# Repo A (voice prototype) -> Repo B (durable service): the integration boundary

Status: implemented and tested locally with fakes (see the end). **No real call, real model or DID change was involved.**

## What was already converged (before this change)

Repo B already contains Repo A's voice engine, ported: the STT -> RAG -> Brain -> TTS pipeline (`app/ai/pipeline.py`, `clients.py`), energy endpointing and barge-in (`serve_loop` in `app/ai/ws.py`), the `mod_audio_stream` protocol (`handle_fs_media`) and the Plivo stream protocol (`PlivoIO`), all running on B's MongoDB-backed call/session lifecycle. Repo A's remaining unique behaviour was **one thing**: starting the media stream automatically when a flagged FreeSWITCH call is answered (`MediaService.maybe_start`).

## The gap

In Repo B an observed or managed inbound FreeSWITCH call was recorded but AI never started by itself: observed calls were created with `ai_enabled: False`, and route action `ai` was honoured only for Plivo-delivered calls. A manual `POST /v1/ai/sessions/{id}/start` was the only way. So the DID path still depended on Repo A's `MediaService` for AI, which means two services owning one call.

## The change

`MME_TEL_AI_FS_AUTOSTART` (default **false**, so nothing changes unless an operator turns it on). When true, on the first applied `CHANNEL_ANSWER` of an **inbound FreeSWITCH call whose route action is `ai`**, the controller:

1. runs `AiService.preflight` (global AI switch, tenant AI enabled, STT/Brain/TTS configured, `mod_audio_stream` really loaded). If it fails, the call is left alone and an `ai.autostart_skipped` event with the reason is recorded.
2. checks the channel with `uuid_getvar <uuid> STREAM_EXTRA_HEADERS`. If it is already set, another owner (Repo A's media service, or an operator) attached the media module first, so B does **not** attach a second one (`media_already_attached`). Verified against the real local FreeSWITCH: unset returns `_undef_`, set returns the value.
3. flags the call `ai_enabled` and calls the normal `AiService.start`: same preconditions, same `ai_sessions` row (unique per session), same media token and `uuid_audio_stream` start, same watchdog. The session becomes `active` only when the media socket really connects.
4. never raises into the call: any refusal or failure is an event plus `mme_tel_ai_autostart_total{result}`, and the call continues without AI. Hangup stops the AI session as before.

The trigger is the **route** (`PUT /v1/admin/tenants/{t}/routes`, action `ai`, tenant-scoped and audited), not a channel variable or an environment regex. Observed calls now also store `route_action`.

Config rule (checked at startup): `AI_FS_AUTOSTART` requires `AI_ENABLED=true` and `AI_MEDIA_BRIDGE=ws_fork`.

## Ownership rules (avoid duplicate call/session ownership)

* One owner of FreeSWITCH media per environment. When Repo B auto-start is on, **leave `MME_FS_MEDIA_ENABLED` unset in Repo A** (its default). The `uuid_getvar` guard is a best-effort safety net for a race, not permission to run both.
* One session per call: unchanged (`SessionService.create` dedups by call id / FreeSWITCH UUID; `ai_sessions` is unique per session).
* The prototype lifecycle listener (`mme_telephony.esl_events`) posting to Repo A on :8090 is a separate recorder; per `MIGRATION_FROM_PROTOTYPE.md` stop it when Repo B observes the context.
* Not touched: DID routing, dialplan, gateway, Plivo configuration.

## Enabling it later (needs operator approval; not done)

1. Models reachable and tenant AI config saved; `AI_ENABLED=true`, `AI_MEDIA_BRIDGE=ws_fork`, `AI_FS_AUTOSTART=true`.
2. Route for the test DID with action `ai` for the tenant.
3. The DID dialplan must answer and park the call (today it plays a tone and hangs up). That is a live dialplan change: separate approval.
4. One approved test call; evidence per `LIVE_VERIFICATION_PLAN.md`.

## Tests

`tests/integration/test_ai_fs_autostart.py` (9): starts exactly one conversation; repeated answer events do not start a second; flag off = unchanged; non-AI route sends no FreeSWITCH command; unconfigured AI skips with a reason and the call continues; stream already attached elsewhere is not duplicated; bridge failure never breaks the call; hangup stops the AI session; config rule. FreeSWITCH and the bridge are fakes in these tests, models are mocks: they prove the decisions, not audio or real-model behaviour.
