# Operations runbook

Scrape `/metrics` (loopback or `MME_TEL_METRICS_TOKEN`). Metric labels are bounded enums only. Thresholds below are **starting points** to tune against measured traffic, not validated SLOs.

## Alerts
| Condition (suggested) | Metric | Meaning | Action |
|---|---|---|---|
| `mme_tel_esl_connected == 0` for >60 s | ESL | event stream down; queue is held, no new calls start | Runbook A |
| `mme_tel_esl_event_lag_seconds > 2` for 5 min or `mme_tel_esl_events_dropped` increasing | ESL | handler/DB too slow; state repaired only by reconcile | check DB latency, CPU; raise `ESL_EVENT_CONCURRENCY` |
| `mme_tel_db_ping_ms > 250` or `mme_tel_db_failures` increasing; `/health/ready` 503 `database` | DB | Atlas unreachable | Runbook B |
| `rate(mme_tel_provider_errors_total[5m]) > 0` sustained | provider | carrier/Plivo rejecting or timing out | Runbook C |
| failed/(failed+completed) > 20 % over 15 min (`mme_tel_calls_terminal_total`) | outcomes | routing/carrier problem | inspect `mme_tel_hangup_cause_total{reason}` |
| `mme_tel_queued_calls` > 80 % of `MAX_QUEUED` | queue | saturation | capacity / limits review |
| `mme_tel_active_calls` > provider ceiling or growing while `completed` flat | channels | stuck calls | Runbook D |
| `mme_tel_webhook_deliveries_total{result="dead"}` increasing | webhooks | consumer down | Runbook E |
| `mme_tel_recording_failures_total` increasing | recording | storage/FreeSWITCH issue | check disk (`FS_RECORDINGS_DIR`), S3 creds |
| `mme_tel_reconcile_total{action="orphan_channel"}` > 0 | reconcile | unattributable channel in `mme_inbound` | inspect `show channels`; unknown source |

## A. FreeSWITCH / ESL disconnected
1. `curl -s localhost:8800/health/ready`; `python scripts/esl_check.py status` (uses the service's ESL client).
2. If FreeSWITCH is down: start it; the service reconnects by itself (backoff ≤ `ESL_RECONNECT_MAX_S`) and runs reconciliation on reconnect.
3. If auth fails: password drift (`MME_TEL_ESL_PASSWORD` vs `event_socket.conf.xml`); fix and restart the service.
4. After recovery check `calls` stuck in `initiating`/`answered`: reconciliation finalizes missing channels after the grace period (`RING_TIMEOUT_S+10 s` for initiating). Queued calls continue automatically; they are never lost.

## B. Database unavailable
API returns 503 `database_unavailable`; liveness stays 200; ESL events that arrive meanwhile cannot be persisted and are not replayed; state is repaired afterwards by reconciliation against `show channels`. Restore connectivity (Atlas network access list, credentials). Afterwards run `python scripts/esl_check.py "show channels count"` and compare with `GET /v1/telephony/status` `active_calls`; reconciliation fixes drift within `RECONCILE_INTERVAL_S`.

## C. Provider failures
`calls.failure_reason` starts with `plivo_rejected:`/`esl_rejected:` for definite rejections and `origination_uncertain_*` for unknown outcomes (never auto-retried). Check Plivo console/status, credentials, balance and limits; reduce `MAX_INITIATION_RATE` if CPS limited. Uncertain calls are resolved by reconciliation (`origination_uncertain_no_channel`) — verify with the carrier CDR before re-dialing.

## D. Stuck / abnormal active calls
`mongosh`: `db.calls.find({state:{$in:["initiating","ringing","answered","bridged","transferring"]}}).sort({created_at:1})`. Compare with `show channels`. Hang up a channel: `python scripts/esl_check.py "uuid_kill <fs_uuid> NORMAL_CLEARING"` — the resulting hangup event finalizes the call. Do **not** edit state by hand.

## E. Webhook failures
`GET /v1/webhooks/deliveries?status=dead` (attempt history shows status code/error). Fix the consumer, then `POST /v1/webhooks/deliveries/{id}/replay` (same event id; consumers dedupe on `Idempotency-Key`).

## Routine
* Daily: error-rate dashboard, `queued_calls`, disk of `FS_RECORDINGS_DIR`.
* Weekly: backup restore drill into a scratch database.
* Before changing limits: confirm the provider's limits first; they are hard ceilings.
* Clock: keep chrony healthy — durations are wall-clock based (`clock_anomaly` flags a backwards step).

## Safe shutdown
`systemctl stop mme-telephony` (SIGTERM → graceful: loops stopped, ESL and DB closed). Live calls stay up in FreeSWITCH and are reconciled on next start. For a maintenance window set the tenant policies `enabled=false` or let `active_calls` reach 0 first.
