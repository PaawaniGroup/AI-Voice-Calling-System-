# Deployment

**The WSL development machine is not a production telephony host** (NAT address, unstable clock, single machine, no TLS). Production needs its own Linux server; nothing here exposes the dev host.

## Requirements for a production host (not provisioned by this work)
* Linux server with a public IP (or an explicit NAT/ext-rtp-ip plan), DNS name, TLS certificate, time sync (chrony), 4+ vCPU.
* FreeSWITCH (same build or package) with the carrier gateway; firewall: SIP (5060/5061 or provider-specific) and RTP (16384-32768/udp) **only from carrier IP ranges**; **ESL 8021 loopback only**; API port reachable only from the reverse proxy.
* MongoDB Atlas (existing) — create a user restricted to the `mme_telephony` database; allow the host's IP.
* Reverse proxy (nginx/Caddy/ALB) terminating TLS → `127.0.0.1:8800`; forward `/v1/providers/plivo/*` publicly, keep the rest private if possible.

## Install
```bash
sudo useradd --system --home /opt/mme-telephony --shell /usr/sbin/nologin mme-telephony
sudo install -d -o mme-telephony -g mme-telephony /opt/mme-telephony /var/lib/mme-telephony
# release layout: /opt/mme-telephony/releases/<version>, symlink /opt/mme-telephony/current
sudo -u mme-telephony python3 -m venv /opt/mme-telephony/venv
sudo -u mme-telephony /opt/mme-telephony/venv/bin/pip install -r requirements.txt   # pinned
sudo install -d -m 0750 /etc/mme-telephony
sudoedit /etc/mme-telephony/mme-telephony.env   # start from .env.example; chmod 0600, owner root
sudo cp deploy/mme-telephony.service /etc/systemd/system/ && sudo systemctl daemon-reload
# FreeSWITCH dialplan (additive; backs up first). Production: omit the test context.
sudo scripts/install_fs_dialplan.sh
python scripts/validate_config.py        # must say "config OK"
sudo systemctl enable --now mme-telephony
curl -s localhost:8800/health/ready
```
Required env for production: `MME_TEL_ENV=production`, `MONGO_URI`, `JWT_SECRET`, `WEBHOOK_SIGNING_MASTER`, `ESL_PASSWORD`, `BIND_HOST=127.0.0.1`, `DOCS_ENABLED=false`, plus provider variables (PROVIDER_SETUP.md). Startup fails with a sanitized message if any is missing (`ExecStartPre` runs the validator).

## Logs
JSON on stdout → journald. Configure `SystemMaxUse`/`MaxRetentionSec` in `journald.conf` (rotation is journald's job). FreeSWITCH logs: `/usr/local/freeswitch/var/log/freeswitch` with its own rotation.

## Upgrade / rollback
1. Unpack new release to `releases/<v2>`, `pip install -r requirements.txt` into a **new** venv if dependencies changed, run `scripts/validate_config.py` with the production env file.
2. Drain: `GET /v1/telephony/status` → `active_calls` low (live calls are not dropped by a restart; the new process reconciles them), then `ln -sfn releases/<v2> current && systemctl restart mme-telephony`.
3. Verify `/health/ready`, run a smoke call on an approved number.
4. Rollback: `ln -sfn releases/<v1> current && systemctl restart mme-telephony`. Database changes are additive (schema v2); a service older than the stored schema refuses to start, so rolling back across a schema bump requires restoring the backup.

## Backup / restore (database `mme_telephony` only)
```bash
export MME_TEL_MONGO_URI=...           # from the secret store
scripts/backup_mongo.sh /var/backups/mme-telephony
MME_TEL_RESTORE_DB=mme_telephony_restore scripts/restore_mongo.sh /var/backups/mme-telephony/<file>.archive.gz
```
Needs `mongodump`/`mongorestore` (MongoDB Database Tools). Restore into a scratch database first and compare counts; `RESTORE_DROP=1` overwrites. Atlas continuous backups are an alternative. Verification status: see `docs/TEST_REPORT.md` ("backup/restore").

## Container
No Dockerfile is provided: Docker is not installed on the audit host so an image could not be built or tested. The service is a plain `uvicorn app.main:create_app --factory --workers 1` and containerises trivially, but that is **unverified**.
