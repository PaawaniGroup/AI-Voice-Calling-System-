# Platform architecture (calls, sessions, dashboard, AI contracts)

```
 Browser (dashboard, static JS)            MME AI counselling app (JWT, service-to-service)
        │ fetch + Bearer JWT, SSE                │ REST /v1, webhooks in, media WebSocket
        ▼                                         ▼
 ┌──────────────────────────── MME_TELEPHONY  (FastAPI, 1 worker, 127.0.0.1:8800) ─────────────────────────────┐
 │ routes.py (calls, usage, admin, webhooks, Plivo callbacks)   routes_platform.py (auth, sessions, dashboard,   │
 │                                                              SSE, AI, logs)   ws.py (AI media WebSocket)     │
 │ security: JWT · roles viewer/operator/admin/platform · tenant from token · rate limits · scrypt users        │
 │ services: calls (policy, admission, idempotency) · sessions (state machine) · recordings                     │
 │ controller: ESL events → CAS transitions · dispatcher/queue · timeouts · reconciliation · observe-only inbound│
 │ ai: ModelClients (STT/Brain/TTS/RAG over HTTP) · Conversation pipeline · AiService · MediaBridge boundary     │
 │ events: webhook outbox + signed delivery worker           observability: metrics, redacted logs, error ring   │
 └───────┬───────────────────────────────┬──────────────────────────────┬────────────────────────────────────────┘
   ESL 127.0.0.1:8021 (control+events)   MongoDB `mme_telephony`       model servers (STT / Brain / TTS / RAG)
         │                               calls, call_events, sessions,   SEPARATE processes, URLs from config,
         ▼                               ai_sessions, ai_config, users,    allow-listed hosts — NOT reachable today
   FreeSWITCH ──SIP/RTP── gateway plivo-mme ── Plivo ── PSTN              (media: needs a fork module — NOT installed)
   contexts: public (your DID, observed) · mme_inbound · mme_transfer · mme_test(dev)
```

## End-to-end counselling flow and where each step stands
| Step | Component | Status |
|---|---|---|
| 1 call arrives / is placed | FreeSWITCH + Plivo | loopback verified; **real carrier not run** |
| 2 events persisted, call + exactly one session | controller, sessions | verified (real FreeSWITCH loopback, tests) |
| 3 answered, session `answered` | controller | verified |
| 4 AI start preconditions (flag, tenant config, answered, bridge ready) | AiService | verified; **bridge not ready → refusal** |
| 5 caller audio → server | media fork module + WebSocket | server side verified with synthetic client; **module absent** |
| 6 STT → RAG → Brain → TTS | `Conversation` | verified with **mock** servers; real models unreached |
| 7 audio back, barge-in, cancel on hangup | `ws.py` | verified with synthetic client |
| 8 session ends cleanly on hangup, AI stopped, webhooks | controller/AiService | verified |

## Data
New collections: `sessions` (unique `(tenant, call_ref)`), `ai_sessions` (unique per session), `ai_config`, `users` (scrypt hashes). Schema version 3 (additive). Retention via TTL (`purge_at`). See `DATA_MODEL.md` for the rest.

## Clock domains (important)
FreeSWITCH's own event timestamps were observed **2.5 days behind** the host wall clock. All stored times and sweeper comparisons use the service clock; durations use FreeSWITCH-timestamp *differences* (same domain) when plausible, else service-clock differences (`duration_source` on the call).

## Security model
Tenant is taken only from the JWT; viewers see masked numbers and masked service URLs; operators place/hang up/transfer and start/stop AI; admins manage users, AI config, webhooks and read logs; platform sets tenant policy and inbound routes (users with that role are created only from the host CLI). Dashboard: strict CSP (`script-src 'self'`, no inline), `X-Frame-Options: DENY`, no HTML-string rendering, token kept in `sessionStorage` (cleared on logout; no cookies, so no CSRF surface). ESL stays on loopback; the dashboard never reaches FreeSWITCH.
