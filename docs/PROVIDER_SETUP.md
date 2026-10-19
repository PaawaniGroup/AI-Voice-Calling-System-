# Provider setup (Plivo)

**Status: nothing in this document has been exercised against a real Plivo account.** No credentials, numbers or account limits were available. Everything below is either read from the installed `plivo` SDK (4.55.0) or Plivo's public docs, or is explicitly marked UNVERIFIED.

## Two call paths (choose per deployment: `MME_TEL_PROVIDER`)
| Path | How calls flow | Events come from | Status |
|---|---|---|---|
| `freeswitch_gateway` | FreeSWITCH originates via a SIP gateway (e.g. Plivo Zentrunk SIP trunk) | ESL | Controller/ESL/FreeSWITCH logic tested against local FreeSWITCH using loopback legs; **gateway to a real carrier NOT tested** (no gateway exists) |
| `plivo_api` | Service calls Plivo REST `calls.create`; Plivo posts to `/v1/providers/plivo/{answer,status}` | signed webhooks | Webhook verification/mapping tested with locally computed V3 signatures; **REST originate/hangup NOT tested** |

Pick one primary path. The `plivo_api` path has no media on FreeSWITCH (audio is on Plivo), which matters for Phase 3.

## Facts taken from the SDK / docs
* Make-call parameters used (SDK `Calls.create`): `from_`, `to_`, `answer_url`, `ring_url`, `hangup_url` (+ `_method=POST`), `ring_timeout`, `time_limit`. Response field used: `request_uuid`.
* Callback signing: headers `X-Plivo-Signature-V3` and `X-Plivo-Signature-V3-Nonce`, validated with `plivo.utils.signature_v3.validate_v3_signature(method, url, nonce, auth_token, signature, params)` over the **public URL** (so `MME_TEL_WEBHOOK_PUBLIC_BASE_URL` must equal what Plivo calls, including path).
* Plivo may send duplicate callbacks; `CallUUID` is the documented idempotency key. The service dedups on `CallUUID + CallStatus + Event` and stores nonces.
* Callback field names read by the code: `CallUUID`, `RequestUUID`, `CallStatus` (`ringing`, `in-progress`, `completed`, `busy`, `no-answer`, `cancel`, `failed`, `timeout`), `Event`, `HangupCause`, `HangupCauseName`, `Direction`, `To`, `From`. **UNVERIFIED against live traffic**: the docs pages I could retrieve did not enumerate these fields (the make-a-call reference returned 404); validate with a real test call before relying on `RequestUUID` mapping or the cancel-by-request-uuid behaviour (`calls.delete(request_uuid)` is used for hangup and is **UNVERIFIED** for not-yet-answered calls).

## To go live (operator checklist)
1. Create/verify the Plivo account, KYC/entity registration and numbers required for India (DLT/TRAI). **Compliance not reviewed by this work.**
2. Confirm account **concurrent-call and CPS limits** and set `MME_TEL_PROVIDER_MAX_CONCURRENT` / `MME_TEL_PROVIDER_MAX_CPS` to those values (defaults 10 / 1 are placeholders).
3. Set `MME_TEL_PLIVO_AUTH_ID`, `MME_TEL_PLIVO_AUTH_TOKEN` (secret store), `MME_TEL_WEBHOOK_PUBLIC_BASE_URL=https://<host>`; TLS must terminate at a proxy forwarding `/v1/providers/plivo/*`.
4. For the gateway path: add a FreeSWITCH gateway named `MME_TEL_FS_GATEWAY` (`sofia_profiles/external/<name>.xml`) with the trunk credentials from the Plivo console; set `context` to `mme_inbound`; open SIP/RTP only to Plivo's published ranges.
5. Add the tenant policy (`PUT /v1/admin/tenants/{t}/policy`) with approved caller IDs and `enabled:true`; set `MME_TEL_REAL_CALLS_ENABLED=true` only for an approved test number first.
6. Run the opt-in real test (not in CI): one outbound call to an approved destination, verify `ringing → answered → completed`, durations and webhook delivery; then one inbound call.
7. Compare `/v1/usage` measured minutes with the Plivo invoice for the same window.

## Costs and rate limits
Each real call is billed by Plivo. The service never dials unless `REAL_CALLS_ENABLED=true` and a tenant policy allows it; provider limits are enforced as hard ceilings (`min(MAX_ACTIVE_GLOBAL, PROVIDER_MAX_CONCURRENT)`, token bucket `min(MAX_INITIATION_RATE, PROVIDER_MAX_CPS)`).
