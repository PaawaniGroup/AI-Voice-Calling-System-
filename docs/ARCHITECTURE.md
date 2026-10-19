# Architecture (Phase 2)

```
                 MME Express / Next.js (existing, untouched)
                         │  HTTPS + Bearer JWT (tenant_id, roles)
                         ▼
 ┌──────────────────────────── MME_TELEPHONY (FastAPI, ONE process) ────────────────────────────┐
 │ api/        routes, schemas, error envelope, request-id, body limit, rate limit               │
 │ security/   JWT verify, roles, tenant from token, per-principal limiter                        │
 │ services/   calls (policy+admission+idempotency), recordings (policy, upload, presign)         │
 │ telephony/  state_machine · controller (events, dispatch, hangup/transfer, timeouts, reconcile)│
 │             esl (event client + command client) · dialstrings (validated command builders)     │
 │ providers/  base (FreeSwitchGateway, Mock) · plivo (REST + V3 signature)                       │
 │ events/     webhooks (emitter outbox, signer, SSRF guard, retrying worker)                     │
 │ persistence/ db (indexes, schema version) · repos (CAS transitions, dedup events, idempotency) │
 │ observability/ Prometheus metrics, JSON logs with redaction                                     │
 └───────────┬───────────────────────────────┬─────────────────────────────┬────────────────────┘
        ESL 127.0.0.1:8021               MongoDB (dedicated DB)        HTTPS outbound
    (control + events only,            `mme_telephony`                (Plivo REST, webhooks,
     NOT media)                                                         S3 presign/upload)
             ▼
        FreeSWITCH  ── SIP/RTP ──► carrier gateway (e.g. Plivo Zentrunk)  /  Plivo Voice API path (REST+webhooks)
```

## Key decisions
| Decision | Rationale |
|---|---|
| Single worker process (`workers=1`, enforced) | the controller owns one ESL event stream, dispatch queue and rate limiter; horizontal scale needs leader election (documented limit) |
| MongoDB, dedicated database, no transactions | matches existing MME stack; correctness comes from CAS (`state_version`), unique indexes (idempotency, event dedup) and reconciliation, so it also runs on a standalone mongod |
| Queue = `calls` with `state=queued` (DB outbox) | an accepted request survives crashes; the dispatcher claims `queued→initiating` by CAS |
| FreeSWITCH UUID generated + persisted **before** `bgapi originate` | uncertain outcomes are resolved by looking up the channel; never blind retry |
| Two ESL connections | commands cannot starve events; command timeout tears down only the command socket |
| Controller-owned dialplan contexts | the API can only select validated E.164 values; it cannot inject dialplan, variables or ESL commands |
| Two provider paths behind one interface | `freeswitch_gateway` (SIP trunk, ESL events) and `plivo_api` (REST originate + signed webhooks). Mock is dev/test only |
| Tenant from JWT only | tenant is part of every primary lookup; cross-tenant ids return 404 |
| Webhook secrets derived (HMAC of master + endpoint id + version) | no signing secret is stored in the database |

## Integration boundaries
* **ESL is control/event only.** No audio flows through it.
* **AI (Phase 3) is not wired.** `ai_enabled` is forced `false`; no STT/Brain/TTS code or endpoints exist.
* Extension points for Phase 3 are listed in `PHASE_2_ACCEPTANCE.md` (media attach interface; parked legs; `mme_leg`/`mme_call_id` channel variables; lifecycle webhooks).
* **Deferred**: browser calling (needs `mod_verto` or WSS/SIP over TLS + ICE/TURN), multi-node HA, carrier-grade compliance sign-off.

## Process model
`lifespan` starts: DB connect (fail fast) + indexes → ESL command client → ESL event client → controller loops (dispatch, timeouts, reconcile) → webhook worker → housekeeping (recording upload, retention, stuck webhook claims). Shutdown reverses it; calls stay in FreeSWITCH and are reconciled by the next start.
