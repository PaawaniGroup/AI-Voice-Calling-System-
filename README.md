# MME_TELEPHONY — telephony backend, sessions, dashboard, AI contracts

Standalone service for Make My Education: FreeSWITCH control over ESL, durable call + counselling-session lifecycle, tenant-isolated REST API (54 routes, [docs/ROUTES.md](docs/ROUTES.md)), a web **management dashboard** at `/dashboard/`, Plivo/gateway provider adapters, signed webhooks, usage/analytics, and **AI session contracts** (model adapters, media WebSocket protocol). **AI is off by default and cannot start a session until a real media bridge exists (none is installed); real STT/Brain/TTS models have not been reached.**

Plivo Voice Application (inbound call → AI stream, mock-verified): [docs/PLIVO_VOICE_APPLICATION.md](docs/PLIVO_VOICE_APPLICATION.md).
Start here: [docs/SYSTEM_ARCHITECTURE_COMPLETE.md](docs/SYSTEM_ARCHITECTURE_COMPLETE.md) (both codebases, every route, every flow);  [docs/ACCEPTANCE_AI_PLATFORM.md](docs/ACCEPTANCE_AI_PLATFORM.md) (what works / what is blocked), [docs/API_INTEGRATION_GUIDE.md](docs/API_INTEGRATION_GUIDE.md), [docs/MIGRATION_FROM_PROTOTYPE.md](docs/MIGRATION_FROM_PROTOTYPE.md).

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

## Dashboard
`http://127.0.0.1:8800/dashboard/` — Overview, Live calls (SSE with polling fallback), Call details, Call controls, Sessions, Recordings, AI configuration, Logs & system, Users. Create the first user on the host: `MME_TEL_NEW_USER_PASSWORD='…' python scripts/create_user.py <name> <tenant> <viewer|operator|admin|platform>`. Roles: viewer (read, numbers masked) < operator (place/hang up/transfer, AI start/stop) < admin (users, AI config, webhooks, logs); platform = cross-tenant (CLI-created only). Tested in real headless Chromium (`tests/e2e`; install with `pip install playwright && PLAYWRIGHT_BROWSERS_PATH=~/pw-browsers python -m playwright install chromium` and system libs via `playwright install-deps`).

## Record your existing inbound DID calls (no dialplan change)
`MME_TEL_OBSERVE_CONTEXTS=public` + an inbound route for the DID: the service records the call and session and sends **no** command to FreeSWITCH. Check on loopback: `bash scripts/verify_observe_inbound.sh`.

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
