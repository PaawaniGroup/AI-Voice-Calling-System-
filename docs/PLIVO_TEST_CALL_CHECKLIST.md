# Plivo test call: controlled, one caller, 3 minutes

Scope: ONE inbound call from the operator's own phone to the Plivo number routed to the AI (`mme-test`, action `ai`). No outbound dialling,
no FreeSWITCH. Nothing here is a verification of production readiness: it is the first real-world look at the path.

## Safety design

| Control | How |
|---|---|
| Only my number is answered | `policy.allowed_callers=[me]`; any other caller is rejected before a call, AI session or model request exists |
| 3-minute cap | `policy.max_duration_s=180` (the controller hangs the call up) + the `--watch` dead-man switch (cap + 20 s grace) |
| One call at a time | `policy.max_active_calls=1` |
| Kill switch | `python scripts/plivo_testcall.py disarm --yes` (works without the API service): policy `enabled=false` (new calls rejected) and a Plivo REST hangup of every live call |
| Fallbacks | hang up on the phone; Plivo console -> Live calls -> Hangup |

`python scripts/plivo_testcall.py status` is read-only and masks numbers. It prints `"open_to_anyone": true` when an AI route has no allowlist.
**Before arming it was true: any caller of the Plivo number would have started an AI conversation (and RunPod spend).**

## Before the call (no call yet)

1. `status`: tenant AI enabled, stt/brain/tts URLs set, Plivo credentials set, `public_base_url_https` true, `live_calls` empty.
2. Plivo console: the Application's Answer URL = `<public base>/v1/providers/plivo/answer`, Hangup URL = `.../hangup`; the number is attached to it.
3. The public tunnel (ngrok) is up and the service is running with `AI_ENABLED`, `PLIVO_INBOUND_ENABLED`, `REAL_CALLS_ENABLED`.
4. RunPod: min workers confirmed, idle timeouts known, credit balance noted (first turn after idle can be a cold start of 1-3 minutes).
5. Dashboard (admin) open on **Live operations**; a second terminal ready with the disarm command.
6. `arm --caller <my number> --yes --watch`: confirm the printed status shows `restricted_to_allowlist: true`, `max_duration_s: 180`, `max_active_calls: 1`.

## During the call: what to watch

Live operations page (feed) and the service log (JSON lines):

| Expect | Where |
|---|---|
| Call accepted, state `answered`, `stream.started` / `stream.connected` | feed + `call_events` |
| `ai.session.started`, then per turn `ai.turn` with `stt_ms`, `brain_ms`, `tts_ms`, `turn_ms` (no words are shown by design) | feed |
| `runpod service=stt|brain|tts delay_ms=... exec_ms=...` (delay > 5000 = cold start) | log |
| First audio after your first sentence; later turns about 3 s | your ear + `turn_ms` |

**Press the kill switch if you see:** `ai.turn_error` twice in a row; `worker_exception`; the log line
`conversation worker did not stop after cancellation`; `stream.failed`; no audio 10 s after you spoke; a second call appearing;
any call from another number being answered (it must not happen); `plivo inbound call rejected: AI not ready`.

## After the call

1. `status`: `live_calls` empty, `live_ai_sessions` 0 (nothing left running); `/v1/live/snapshot` gauges `active_ai_sessions` 0.
2. `disarm --yes` if `--watch` did not already (it disarms on its own when the call ends).
3. Dashboard call page: Flow stages all done, per-turn timings; note first-turn latency and the slowest turn.
4. RunPod console: actual spend of the three endpoints vs the estimate; workers back to zero after the idle timeout.
5. Write down what happened (turn count, cold starts, anything odd) before changing anything.
