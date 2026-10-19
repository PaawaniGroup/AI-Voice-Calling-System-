# MME_TELEPHONY (Phase 2)

Standalone telephony service for Make My Education: FreeSWITCH control over ESL, durable call lifecycle, tenant-isolated REST API, Plivo/gateway provider adapters, webhooks, usage/analytics. **No AI/voice-agent code is included or active (Phase 3).**

Status and honest limits: `docs/PHASE_2_ACCEPTANCE.md`. Nothing has been verified against a real carrier, Plivo, Atlas or a production host.

## Layout
`app/` service code · `config/` settings, policy, FreeSWITCH dialplan · `scripts/` ops/dev tools · `tests/` unit, integration, resilience, load · `docs/` reports and runbooks · `deploy/` systemd unit.

## Requirements
Linux/WSL, Python 3.12, MongoDB (local `mongod` for tests/dev; Atlas in production), FreeSWITCH with `mod_event_socket` for the FreeSWITCH-backed features.

## Install (development, WSL)
```bash
cd ~/MME_TELEPHONY && python3 -m venv .venv        # or reuse the existing venv
.venv/bin/pip install -r requirements-dev.txt
bash scripts/dev_mongo.sh                          # local mongod on 127.0.0.1:27117 (binary in ~/mongo/bin)
cp .env.example .env && chmod 600 .env             # then edit; never commit .env
```
`scripts/run.sh <cmd>` runs a command in the repo root with the venv (override with `MME_VENV`).

## Configure
Authoritative reference: `docs/CONFIGURATION_REFERENCE.md` (generated from `config/settings.py`; 116 parameters, prefix `MME_TEL_`). Completeness status: `docs/CONFIGURATION_COMPLETENESS.md`. Regenerate with `python scripts/gen_config_docs.py`.
```bash
python scripts/validate_config.py            # exits 1 on errors; --strict also fails on warnings; never prints secrets
```
Minimum for local dev: `MME_TEL_MONGO_URI`, `MME_TEL_JWT_SECRET`, `MME_TEL_WEBHOOK_SIGNING_MASTER`, and `MME_TEL_ESL_PASSWORD` (when FreeSWITCH is enabled). Production additionally forbids the mock provider and requires secrets ≥ 32 chars.

## FreeSWITCH dialplan (once per FreeSWITCH)
`scripts/install_fs_dialplan.sh [--with-test-context]` backs up the conf tree, installs `config/freeswitch/mme_dialplan.xml` (+ the isolated loopback test context for development), then `python scripts/esl_check.py reloadxml`. Needs write access to the FreeSWITCH config directory.

## Run
```bash
bash scripts/run.sh uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8800 --workers 1
# one-shot local smoke test (starts, checks, stops; places no calls):
bash scripts/smoke_local.sh
curl -s localhost:8800/health/live ; curl -s localhost:8800/health/ready
python scripts/dev_token.py <tenant> telephony:user      # dev JWT (refuses in production)
```
Workers must be 1. Stop with Ctrl-C/SIGTERM (graceful; live calls persist in FreeSWITCH and are reconciled on next start). Production: `deploy/mme-telephony.service`, `docs/DEPLOYMENT.md`.

A tenant can place calls only after a platform admin enables it: `PUT /v1/admin/tenants/{tenant}/policy` with `enabled`, `caller_ids`, etc. Real PSTN dialing additionally needs `MME_TEL_REAL_CALLS_ENABLED=true`.

## Tests
```bash
bash scripts/dev_mongo.sh
bash scripts/run.sh pytest -q                                   # everything (FreeSWITCH tests skip if ESL is unreachable)
bash scripts/run.sh pytest -q tests/unit/test_configuration.py  # configuration
bash scripts/run.sh pytest -q -m freeswitch                     # only real-FreeSWITCH tests
bash scripts/run.sh python tests/load/run_load.py --mode fs --calls 300 --concurrency 30 --hold 5
bash scripts/verify_backup_restore.sh                           # needs MongoDB Database Tools (~/mongo-tools/bin)
```
No test places a paid call or touches an AI model. Details: `docs/TEST_REPORT.md`.

## Troubleshooting
| Symptom | Check |
|---|---|
| `/health/ready` 503 `freeswitch` | `python scripts/esl_check.py status`; password drift; ESL must be reachable on `ESL_HOST:ESL_PORT` |
| 503 `database_unavailable` | `MONGO_URI`, Atlas IP allow-list; `bash scripts/dev_mongo.sh` locally |
| 403 `tenant_calling_disabled` | create the tenant policy (admin API) |
| 403 `real_calls_disabled` | set `MME_TEL_REAL_CALLS_ENABLED=true` only for approved tests |
| 422 `caller_id_not_approved` | add the number to the tenant's `caller_ids` |
| Calls stay `queued` | FreeSWITCH event stream down, or capacity ceilings (`/v1/telephony/status`) |
| Startup error naming a variable | fix the named `MME_TEL_*` variable (values are never echoed) |

More: `docs/OPERATIONS_RUNBOOK.md`, `docs/SECURITY.md`, `docs/PROVIDER_SETUP.md`, `docs/ARCHITECTURE.md`.
