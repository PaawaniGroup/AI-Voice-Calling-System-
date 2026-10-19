# Security

## Threat → control → evidence
| Threat | Control | Test |
|---|---|---|
| Unauthenticated access | Bearer JWT (HS256, `exp`+`sub` required, alg pinned, `alg=none` rejected); `iss`/`aud` optional | `test_auth_required_and_validated` |
| Cross-tenant read/update/hangup/transfer/webhook access | tenant taken **only** from the verified token and included in every primary lookup (other tenant ⇒ 404); `?tenant_id=` ignored; cross-tenant usage needs `platform:admin`; admin writes need roles | `test_cross_tenant_access_denied` |
| Toll fraud / premium destinations | tenants disabled by default; E.164 normalisation; country allowlist (default `IN`); global + tenant deny prefixes; optional allow prefixes; approved caller IDs only (no spoofing); bridge/transfer targets must be on the tenant's `transfer_destinations` | `test_policy_rejections`, `test_destination_policy` |
| Accidental real calls / cost | `REAL_CALLS_ENABLED=false` refuses any PSTN-capable path; mock provider forbidden in staging/production; nothing dials at startup/CI/health | `test_configuration`, `test_policy_rejections` |
| Dialplan/ESL injection | every FreeSWITCH string comes from `dialstrings.py` allowlist builders; transfers go to a fixed controller-owned context; ESL lines reject newlines | `test_dial_strings_reject_injection` |
| Duplicate outbound calls | unique-index idempotency (fingerprint compared; key reuse with a different body ⇒ 409); never retry an uncertain origination; reconciliation resolves | `test_idempotency`, `test_concurrent_duplicate_requests_create_one_call`, `test_uncertain_origination_is_not_retried`, `test_state_survives_service_restart_and_idempotency_persists` |
| Webhook forgery (provider) | Plivo V3 signature + nonce via SDK `validate_v3_signature`; nonce store for replay; unknown calls acknowledged but ignored | `test_plivo_callbacks.py` |
| Forged/replayed outbound webhooks | HMAC-SHA256 over `timestamp.body`, per-endpoint derived secret, `Idempotency-Key` = event id | `test_webhook_delivery_signing_retry_and_replay` |
| SSRF via webhook URL | https only, no credentials in URL, DNS resolved and private/loopback/link-local/metadata ranges rejected at create **and** send time, redirects not followed | `test_ssrf_blocked` |
| Exposed ESL | loopback by default; settings reject non-private hosts unless `ESL_ALLOW_NON_PRIVATE`; FreeSWITCH `apply-inbound-acl=loopback.auto`; password never logged | `test_esl_must_be_private_unless_explicit`, `test_wrong_password_never_connects` |
| Secret leakage | `SecretStr` settings, redacted diagnostics, JSON log redaction (keys, URI credentials, phone numbers), `.env` ignored by git, no secret in error messages | `test_redaction`, `test_secret_not_in_repr_or_error`, `test_redacted_hides_secrets` |
| DoS (bodies, queues, rates) | body limit, per-principal and per-tenant call-create rate limits, bounded queue (`MAX_QUEUED`), per-tenant caps, ESL event queue bound, webhook queue bound | `test_invalid_bodies`, `test_rate_limit`, `test_capacity_queue_and_reject` |
| Recording access | tenant-scoped lookup, presigned URL (≤1 h, default 5 min), `attachment` disposition, audited, never public | `recordings` service (S3 path untested, see limits) |
| Info leakage in errors | uniform envelope; DB errors → 503 without driver text; health endpoints expose only booleans | `test_database_unavailable_returns_503_without_leaking` |

## Known limitations / residual risks
* **Rate limiter and queue are per-process** (service is single-worker by design). Do not run multiple replicas without moving them to shared storage.
* **Webhook SSRF**: DNS is re-checked immediately before the request, but the connection re-resolves; a DNS-rebinding race remains. Mitigate with an egress proxy/firewall that blocks RFC1918 and 169.254.0.0/16.
* **Integration with MME identity is unverified**: the Express backend was not available. Tokens must be HS256 with a shared secret and carry `tenant_id` and `roles`. If MME uses RS256/JWKS or opaque sessions, replace `security/auth.decode_token` (single function).
* JWT secret rotation requires a restart (two-key overlap is not implemented).
* No IP allowlist on the API itself; restrict at the reverse proxy/firewall. SIP exposure (fail2ban, SIP-over-TLS, ACLs on the SIP profile) is a FreeSWITCH/firewall task for the production host.
* Telecom compliance (DoT/TRAI registration, DLT/consent, recording consent law) is **not** verified; see PROVIDER_SETUP.md.

## Secret rotation
1. JWT secret: change in the secret store, restart (`systemctl restart mme-telephony`); issue tokens from the new secret simultaneously.
2. Webhook signing master: bump each endpoint's `secret_version` is not exposed yet; rotating the master invalidates all derived secrets, so re-create endpoints (the response returns the new secret).
3. ESL password: edit `event_socket.conf.xml`, restart FreeSWITCH, update `MME_TEL_ESL_PASSWORD`, restart service.
4. Plivo token: regenerate in the Plivo console, update env, restart; callbacks signed with the old token fail until then.

## Audit
Policy, route, webhook, call create/hangup/cancel/transfer and recording access are written to `audit` (retention `RETENTION_AUDIT_DAYS`, default 365).
