# FreeSWITCH + Plivo integration guide

Everything below was derived from inspecting this host on 2026-10-19 (secrets omitted). **No real Plivo call was placed by this work.** Your earlier real calls were observed by the prototype listener, not by this service.

## What is installed (verified)
* FreeSWITCH 1.11.4-dev under `/usr/local/freeswitch` (source build), config `/usr/local/freeswitch/etc/freeswitch`; no systemd unit (started by hand, not supervised — it did not survive a WSL restart).
* ESL on `127.0.0.1:8021`, ACL `loopback.auto`, non-default password.
* SIP profile `external` on `192.168.1.22:5080` (LAN address of the WSL VM), ACL `mme_runpod_test`, context `public`, `auth-calls=false`.
* Gateway `plivo-mme` (`register=false`, Plivo Zentrunk-style IP/credential trunk) in `sip_profiles/external/plivo-mme.xml` — state `NOREG` is expected for a non-registering trunk.
* `public/00_inbound_did.xml`: DID `+918065530418` → answer, 1 s tone, hangup (a test dialplan, not a counselling flow).
* Modules: `mod_sofia`, `mod_event_socket`, `mod_opus`, `mod_av`, `mod_tone_stream`, `mod_local_stream`, `mod_sndfile`, `mod_dptools`. **No** `mod_audio_fork` / `mod_audio_stream` / `mod_verto` (see AI_MEDIA_ADAPTER.md).
* Added by this work (additive, backed up; `~/backups/freeswitch-conf-*.tgz` and `~/freeswitch-conf-backup-*`): `dialplan/mme_dialplan.xml` (contexts `mme_inbound`, `mme_transfer`) and `dialplan/mme_test_dialplan.xml` (isolated loopback test context). Your files were not edited by this session.

## Call paths and who controls what
```
PSTN ─► Plivo ─SIP─► FreeSWITCH external profile ─► context public (today: tone) ─► [observed] this service records call + session
                                              └──► context mme_inbound (opt-in) ─► service answers / parks / bridges / rejects
service ─ESL bgapi originate ─► FreeSWITCH ─► gateway plivo-mme ─SIP─► Plivo ─► PSTN
```
Only the service talks to ESL (loopback). The dashboard and API never expose `api`/`bgapi`; commands are built from validated values (`app/telephony/dialstrings.py`).

## Enable outbound through your gateway
1. Server env: `MME_TEL_PROVIDER=freeswitch_gateway`, `MME_TEL_FS_GATEWAY=plivo-mme`, keep `MME_TEL_FS_DIAL_TEMPLATE=sofia/gateway/{gateway}/{destination}` (default).
2. Plivo side (operator task, **unverified here**): the trunk must authorise your public IP, outbound caller ID must be a number you own (`+918065530418`), and the destination country must be enabled on the account. Confirm concurrent-call and CPS limits and set `MME_TEL_PROVIDER_MAX_CONCURRENT` / `MME_TEL_PROVIDER_MAX_CPS` accordingly (defaults 10 / 1 are placeholders).
3. Tenant policy with `caller_ids:["+918065530418"]`; `MME_TEL_REAL_CALLS_ENABLED=true` **only** for the first approved test number.
4. Manual test (not automated, costs money): place one call to an approved number via `POST /v1/calls`, watch `/dashboard/#/live`, hang up from the call page; compare `/v1/usage` with the Plivo CDR.

## Inbound
* **Keep today's dialplan, add visibility** (recommended first step): `MME_TEL_OBSERVE_CONTEXTS=public`, add the route `PUT /v1/admin/tenants/<t>/routes {"number":"+918065530418","action":"park"}`. Calls are recorded; FreeSWITCH behaviour is unchanged. Verified on loopback with `bash scripts/verify_observe_inbound.sh`.
* **Let the service handle the call**: change the DID extension to `<action application="transfer" data="${destination_number} XML mme_inbound"/>` (your decision, not made for you). The service then answers and applies the route action (`park` holds the call — callers hear silence until something attaches; `bridge` forwards to a target; `hangup`).
* NAT: the SIP profile advertises the LAN address; a public deployment needs `ext-sip-ip`/`ext-rtp-ip`, RTP range forwarding and TLS-capable edge. Nothing in this repo changes SIP/RTP settings.

## Plivo Voice-API path (alternative, `MME_TEL_PROVIDER=plivo_api`)
Plivo originates and calls back `POST <WEBHOOK_PUBLIC_BASE_URL>/v1/providers/plivo/answer` and `/status`; signatures (`X-Plivo-Signature-V3` + nonce) are verified with the SDK and replays rejected. There is no media on FreeSWITCH in this mode. Callback field names are partly unverified (see PROVIDER_SETUP.md).

## Security checklist
ESL loopback only (service refuses non-private ESL unless explicitly allowed) · firewall SIP/RTP to Plivo ranges only · never commit `plivo-mme.xml` credentials · rotate the trunk password if it was ever pasted into chat or logs · keep the API behind TLS (reverse proxy) before exposing `/v1/providers/plivo/*`.
