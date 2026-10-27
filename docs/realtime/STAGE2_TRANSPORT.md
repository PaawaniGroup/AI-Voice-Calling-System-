# Stage 2: persistent transport (offline build; nothing here has run against a network)

Status: code written and compiled, tests written and NOT run (they wait for the operator's "APPROVED: run"). Every latency figure below is an ESTIMATE or UNKNOWN until a MEASURED trace exists.

## What was built
- `app/ai/rt/` client: one authenticated WebSocket per call and service (STT, TTS, Brain streaming). `protocol.py` (messages, HMAC token bound to the call id, sequence-numbered binary frames), `connection.py` (reader, heartbeat, bounded queues, reconnect with backoff and jitter, cancel that really stops the server's work), `resilience.py` (circuit breaker, readiness from completed requests only, never from /health), `clients.py` (`RtModelClients`: falls back to the queue path on any transport error; a half-spoken Brain answer is never replayed).
- `deploy/rt_workers/`: the server side (`common.py` protocol server, `tts_server.py`, `stt_server.py`, `brain_vllm_launch.sh`, `selftest.py`). Engine code and vLLM flags are UNVERIFIED until Stage 3.
- `tools/realtime/rt_rtt_probe.py`: Gate G1 probe (the free `--selftest` is SIMULATED).

## Flags and kill switches (all default OFF)
Per tenant, in the AI config allowlist: `rt_transport` ({"stt"|"tts"|"brain": "queue"|"persistent"}), `rt_endpoints` (wss/ws URLs; https for Brain), `rt_secret_env` (NAME of the environment variable holding the shared secret; the secret is never stored in config). Kill switches: remove or set the tenant keys back to "queue"; the global setting `ai_rt_kill=true`; `rt_kill()` per call. With everything off the legacy /run + poll path is the only path.

## Host decision (what carries the always-on server)
| Option | For | Against | Status |
|---|---|---|---|
| A. Serverless load-balancing endpoint (HTTP/WebSocket, `/ping` health, scales from zero) | no Pod to babysit; same console | a WebSocket kept open all call long holds a worker; Active workers = billed 24/7; idle workers still billed when "Active" | UNVERIFIED: docs read 2026-10-27 (https://docs.runpod.io/serverless/load-balancing/overview) do NOT mention WebSockets or scale-from-zero; page says beta, no queue, `/ping` health, 2 min no-worker timeout, 5.5 min per-request processing timeout |
| B. Pod with an exposed TCP port (direct) | plain WebSocket, no proxy timeout, predictable RTT | billed while it exists; a restart can change the IP/port; needs a deadline and deletion discipline | docs read 2026-10-27 (https://docs.runpod.io/pods/configuration/expose-ports): "Expose TCP Ports" gives direct TCP with a public IP; external port changes on Pod reset; IP stable on Secure Cloud only. Whether the 100 s limit applies to direct TCP: not stated, UNVERIFIED |
| C. Pod behind the HTTP proxy | simplest URL | the proxy enforces a 100 s limit (Cloudflare, per the earlier network notes), calls last up to 180 s | rejected for calls; usable only for the G1 probe if every connection is under 100 s |

Recommendation (ESTIMATE-level, not proven): B for the G1 probe and the voice gate; decide A vs B for production after G1 and the Stage 3 numbers.

### Cost of one always-on GPU (arithmetic, one GPU, price per GPU-second)
| Duty | $0.00019/s | $0.0003/s |
|---|---|---|
| per hour | $0.684 | $1.08 |
| business hours, 260 h/month | $177.84 | $280.80 |
| 24/7, 730 h/month | $499.32 | $788.40 |
Three services (STT, Brain, voice) are three GPUs unless co-hosted: multiply accordingly; that figure is UNKNOWN until Stage 3 shows what fits on one card.

## Gate G1 (round trip over the persistent connection, median <= 40 ms)
Caveat I have not been able to resolve offline: the 40 ms is only reachable from a host near the GPUs. From the operator's PC the path to RunPod passes the home link first (PC to the Cloudflare edge measured about 57 ms average ICMP earlier, MEASURED), so a Pod round trip from the PC is likely above 40 ms (ESTIMATE). So the probe takes `--declared-from pc|cloud`; G1 is judged only for `cloud`; a `pc` run is informational. The operator must say where the service will really run.

### G1 REQUEST design
Action: one Pod (cheapest 24 GB-class, max price per hour enforced), start the probe server (`deploy/rt_workers/common.py` with a no-model engine), run `rt_rtt_probe.py` (200 pings + 200 warmups), delete the Pod. Hard deadline 5 minutes; cost $0.057 at $0.00019/s, $0.090 at $0.0003/s; cap $0.10. Stop at the cap, first 401/403, or 3 consecutive errors. Deletion is verified by listing Pods read-only at start and end; if a Pod might still run: "ACTION NEEDED: DELETE POD NOW". Undo: delete the Pod, revoke the key. Launch conventions follow `tools/phase3/launch_pod.py` (key as a file path only, balance margin check).

## A Pod reset changes its address
Per the Pod docs (2026-10-27): the external TCP port changes whenever the Pod resets; the public IP is stable on Secure Cloud and may change on Community Cloud. The client therefore accepts an optional per-service `resolver` (async, returns the current ws(s):// URL): it is asked before every RE-connect and between failed connect attempts; a failing, empty or non-websocket answer keeps the old address and never raises; the new address is kept for later reconnects (`counters["rediscovered"]`). The resolver itself (a read-only Pod lookup through the RunPod API, or a fixed Secure Cloud address) is Stage 3 work and is not wired to any endpoint; with none configured the old address is retried and the request falls back to the queue path. Tested with a mock address book (`test_rt_connection.py`, four tests).