# Environment audit

Audit date: 2026-10-15. Method: read-only inspection of the Windows project folder and the WSL distribution; no secret values were printed.

## Host
| Item | Finding |
|---|---|
| Workspace | `C:\Users\Admin\Desktop\MME_Telephony` (Windows 11, **not a git repository**, was empty) |
| WSL | `Ubuntu-24.04`, WSL2, user `mme` (sudo requires a password; root reachable only through `wsl -u root` from the Windows host) |
| Hardware | 4 vCPU, ~5.9 GB RAM (WSL2) |
| Python | 3.12.3 (system, WSL). Venv at `~/MME_TELEPHONY/.venv` already existed with only `python-dotenv` |
| Prior prototype | `~/MME_TELEPHONY/test_esl.py` only: a raw-socket ESL auth + `api version` check. Retained untouched. No FastAPI prototype existed, so there was nothing to preserve beyond the ESL probe |
| **WSL wall clock** | **Not stable**: a 45 s probe showed a 50 s wall-vs-monotonic step. Durations derived from wall timestamps are unreliable on this host (see below) |

## FreeSWITCH
| Item | Finding |
|---|---|
| Version | `1.11.4-dev+git~20261009...12ac48ab9e` built from source, prefix `/usr/local/freeswitch` |
| Config root | `/usr/local/freeswitch/etc/freeswitch` (root-owned; world-readable) |
| Process | single instance (`freeswitch -np -c`, console mode, explicit `-conf/-mod/-log/-run/-db` paths under `/usr/local/freeswitch`); no supervisor/systemd unit found |
| Modules present | `mod_sofia`, `mod_event_socket`, `mod_commands`, `mod_dptools`, `mod_dialplan_xml`, `mod_opus`, `mod_sndfile`, `mod_native_file`, `mod_local_stream`, `mod_logfile`, `mod_console`. **Not present**: `mod_verto`, `mod_av`, `mod_curl`, `mod_xml_curl` |
| ESL | `listen-ip=127.0.0.1`, `port=8021`, `apply-inbound-acl=loopback.auto`, `stop-on-bind-error=true`; password is **not** the default `ClueCon` (not recorded here) |
| SIP | internal profile on `172.19.94.142:5080` (WSL NAT address) and `[::1]:5060/5080`; **no gateways configured** (`external/example.xml` only) |
| RTP range | commented out in `switch.conf.xml` (FreeSWITCH default 16384-32768) |
| Dialplan | stock `default` and `public`. **Added by this work** (additive, one new file each): `dialplan/mme_dialplan.xml` (contexts `mme_inbound`, `mme_transfer`) and `dialplan/mme_test_dialplan.xml` (isolated loopback test context) |
| Backup | full copy of the conf tree taken before changes: `~/freeswitch-conf-backup-20261015-173154` |
| Rollback | delete the two `mme_*dialplan.xml` files and run `reloadxml` over ESL |

## Application / data infrastructure
| Item | Finding |
|---|---|
| Existing MME repository (Express, Next.js) | **Not present on this machine.** Existing auth, tenant model, S3 conventions could not be inspected |
| MongoDB Atlas | no connection string on this machine (no `MONGO*` env vars); `mongod` not installed |
| AWS / S3 | no AWS variables or CLI configured |
| Plivo | no credentials configured; no account limits known |
| Docker | not installed |
| Local test database | `mongod 7.0.14` binary downloaded to `~/mongo` (user space), started on `127.0.0.1:27117` by `scripts/dev_mongo.sh` |

## Consequences / decisions
* **Authentication**: the existing MME identity could not be inspected. Phase 2 implements HS256 JWT verification with configurable claim names (`tenant_id`, `roles`) so it can accept tokens issued by the Express backend *if* that backend uses a shared HS256 secret. **Unverified**; see SECURITY.md "Integration with MME identity".
* **Database**: dedicated `mme_telephony` database (namespaced, separate from the 115+ existing collections). The Atlas URI is an operator input.
* **Clock**: because the dev host clock steps, the load harness measures with a monotonic clock and the persistence layer never stores negative durations (`clock_anomaly` flag). Production hosts must run chrony/systemd-timesyncd.
* **WebRTC**: `mod_verto` is not built; browser calling is therefore **not implemented** (documented as deferred).
* **Recording**: `uuid_record` is available via `mod_dptools/mod_commands` + `mod_sndfile`; storage to S3 requires `boto3` and AWS configuration that does not exist here, so recording ships disabled and is tested only against a local storage backend.
