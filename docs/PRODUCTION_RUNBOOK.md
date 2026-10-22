# Production runbook

Complements `OPERATIONS_RUNBOOK.md` (alert-by-alert actions) and `DEPLOYMENT.md`. Nothing here has been exercised on a production host yet (see `READINESS_MATRIX.md`).

## 1. Release checklist (every release)

1. `pip install -r requirements.txt` into a fresh venv; `pip freeze > requirements.lock.txt`.
2. `pip-audit -r requirements.lock.txt` must report no known vulnerabilities (record the date in the release note). A finding blocks the release unless a written exception exists.
3. `pyflakes app config scripts tests`, `pytest tests -q` (full suite, one run, all green; do not rerun until green).
4. `python scripts/validate_config.py` with the production environment file.
5. Regenerate `docs/CONFIGURATION_REFERENCE.md` (`scripts/gen_config_docs.py`) and `docs/ROUTES.md` if routes changed. Note: `gen_routes_doc.py` needs a fully configured environment; run it with the production-like env, and check the diff is not "0 routes".
6. Tag the commit; deploy the tagged commit only.

## 2. Start, stop, restart

* Start: `systemctl start mme-telephony`. `ExecStartPre` runs config validation; a bad config refuses to start.
* On start, `recover_on_startup()` marks AI sessions that lost their runtime and `recover_provider_streams()` hangs up Plivo calls that lost their stream. `mme_tel_recovery_total` counts these; a non-zero value after a deliberate restart is expected, after an unplanned one it is the evidence of what was lost.
* Stop/restart: `SIGTERM` triggers the drain: new Plivo answers get a busy reply, `/health/ready` reports `draining`, the service waits `DRAIN_TIMEOUT_S` (15 s) for calls to settle, stops AI runtimes, hangs up provider calls it still owns. systemd allows 45 s (`TimeoutStopSec`). Calls longer than the drain window **are ended**: schedule restarts in quiet periods; there is no live hand-over because the service runs a single worker by design.
* FreeSWITCH restart is a separate, disruptive operation: it ends every call it carries. Confirm `mod_audio_stream` is loaded afterwards (`fs_cli -x "module_exists mod_audio_stream"`); load-on-restart has not yet been exercised.

## 3. Backup, restore, RPO / RTO

* Backup: `scripts/backup_mongo.sh <dir>` (mongodump archive, gzip). Atlas deployments should additionally enable continuous cloud backup with point-in-time restore.
* Restore: `scripts/restore_mongo.sh <archive>` into a **scratch database first**; compare counts and index names; only then restore into place.
* Proven locally: `scripts/verify_backup_restore.sh` (counts and index names equal on 4 collections). Not yet proven: Atlas, large data, `sessions`/`ai_sessions`/`idempotency`.
* Targets to adopt (these are targets, not measured): RPO <= 24 h with nightly dumps, <= 5 min with Atlas PITR; RTO <= 60 min. **Measure RTO** by timing a full restore drill on the production host and record it in `docs/evidence/`. Until then RPO/RTO are unverified.
* Active calls are not recoverable from backups; recovery relies on the provider's call state plus the startup recovery above.

## 4. Secrets and rotation

* Secrets live only in the environment file (mode 600, owned by the service user). Never in the repo, tickets, chat or logs; logs redact known secret patterns, but do not rely on that.
* **Local `.env` (development host).** `MME_TEL_JWT_SECRET` (HS256 key: verifies API tokens, signs dashboard tokens, Plivo stream tickets and media tokens; no format rule, >= 32 chars required in staging/production) and `MME_TEL_WEBHOOK_SIGNING_MASTER` (per-endpoint webhook secret = `HMAC-SHA256(master, "<endpoint_id>:<version>")`; >= 32 chars in production) are generated as two independent `secrets.token_urlsafe(48)` values and written only to the git-ignored repo-root `.env`. Process environment overrides `.env`. Set them by editing only those two lines. Never paste, log or commit them. If MME's identity service issues the tokens, `JWT_SECRET` must be *its* shared key instead of a generated one (unverified).
* **File permissions on WSL/NTFS.** The repo lives on `/mnt/c` (drvfs): `chmod 600` has **no effect there** (mode stays 777). Restrict `.env` with a Windows ACL instead: `icacls .env /inheritance:r /grant:r "%USERNAME%:(R,W)"`, and verify with `icacls .env`. A production host should keep secrets in a mode-600 file owned by the service user, or a secret manager.
* **`.env` can be overwritten by copying the template.** During setup on 2026-10-22 a freshly written `.env` was replaced in place by a copy of `.env.example` (identical bytes and modification time, same ACL), by something outside this repo's scripts and tests, which wiped both secrets. Check `.env` lengths (not values) before a restart. Copying `.env.example` to `.env` is the documented first step and is now safe (a bug in `AI_MEDIA_SAMPLE_RATE` parsing that made the unmodified template crash the service is fixed and tested in `tests/unit/test_env_template.py`).
* Login rate limit: 10 attempts per minute per client IP (`LOGIN_RATE_LIMIT_PER_MIN`); the 11th returns 429.
* Rotate on: staff change, suspected exposure, and at least every 90 days: `JWT_SECRET` (invalidates sessions and stream tickets, in-flight calls lose their stream), `WEBHOOK_SIGNING_MASTER` (coordinate with webhook consumers), `AI_API_KEY`, `PLIVO_AUTH_TOKEN` (rotate in Plivo, update the env, restart in a quiet period).
* **Open action:** the token and Plivo auth token used by the `~/mme-plivo` test app were observed to be handled insecurely (query-string auth, logged). Rotate them.

## 5. Alerts (Prometheus rules to install; thresholds are starting points to tune)

| Alert | Expression | For |
|---|---|---|
| ESL down | `mme_tel_esl_connected == 0` | 1 m |
| DB failing | `increase(mme_tel_db_failures[5m]) > 0` or `mme_tel_db_ping_ms > 500` | 2 m |
| Provider errors | `rate(mme_tel_provider_errors_total[5m]) > 0.1` | 5 m |
| AI turn errors | `sum(rate(mme_tel_ai_turn_errors_total[5m])) / clamp_min(sum(rate(mme_tel_ai_turn_seconds_count[5m])),0.01) > 0.05` | 5 m |
| AI turn latency | `histogram_quantile(0.95, sum by (le)(rate(mme_tel_ai_turn_seconds_bucket[5m]))) > 4` | 10 m |
| Stuck calls | `mme_tel_active_calls > 0` with no `mme_tel_calls_terminal_total` change for 30 m | 30 m |
| Orphan streams | `mme_tel_plivo_streams_active` far above `mme_tel_active_calls` | 10 m |
| Unexpected draining | `mme_tel_draining == 1` | 2 m |
| Recovery after start | `increase(mme_tel_recovery_total[10m]) > 0` | info |
| Webhook backlog | `rate(mme_tel_webhook_dropped_total[10m]) > 0` | 10 m |

These rules are written but not loaded into a real Alertmanager (unverified). Validate expressions against the real exposed label sets once deployed.

## 6. Incident quick reference

* Callers hear silence after a greeting: check `mme_tel_ai_requests_total{status!="ok"}` per service; a model outage ends AI turns with an error event, the call itself continues. Decide: route to bridge/hangup, or fix the model.
* Calls not arriving: Plivo Console debug logs (signature failures show as 403 in `mme_tel_plivo_callbacks_total`), then `WEBHOOK_PUBLIC_BASE_URL` correctness (the signature covers the exact URL).
* Service will not start: `journalctl -u mme-telephony -n 100`; config validation prints the failing setting by name, never its value.
* Orphan Plivo call (billing running, no state): the sweeper hangs up after `PLIVO_ORPHAN_GRACE_S`; confirm in Plivo's call detail records and hang up manually if it persists.
