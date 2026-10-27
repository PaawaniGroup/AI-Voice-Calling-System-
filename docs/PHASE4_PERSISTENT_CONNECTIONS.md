# Phase 4 (decision document): persistent connections to the models

Status: **not started, on purpose.** Phase 4 is only worth building if the measurements say so. This page says what would be built, what it would cost, and the
rule that decides. Numbers marked *measured* come from `docs/evidence/`; numbers marked *assumed* are assumptions to be replaced by measurements.

## What it would change
Today each sentence is one RunPod serverless job: the service POSTs `/run`, polls `/status`, and the result comes back inside the poll response.
Phase 4 replaces that for TTS (and possibly STT) with **one standing WebSocket per call** to a model server that stays up on a GPU **Pod**: text goes in,
audio frames come out, a cancel message stops the generation. The Brain already streams (its first token arrives in ~0.9 s), so it stays on serverless.

## What it can save, and what it cannot
*Measured in Phase 0* (TTS, `/run`, per sentence): execution 2.3 s (4 words) to 4.9 s (16 words), **plus 2.1 to 3.0 s of overhead around the job** (median per clause length)
(wall time minus RunPod's own `delayTime` + `executionTime`; the fit is 1.7 s + 0.17 s per second of audio). A 1-token Brain job has only 0.36-0.56 s.
The overhead is therefore not just network (the TCP connect to RunPod is 45 ms), and part of it grows with the size of the audio that comes back.
* A standing connection removes the per-job dispatch, queueing and polling: **some or all of that 1.7-3 s per clause.** How much is unknown until the v3
  re-measurement (`tools/phase0b_tts_remeasure.py`) has separated the three suspects: the 768 KB reference upload, the size of the result, and the job machinery.
* It does **not** make Chatterbox generate faster: the 1.4 s + 0.58 s per audio-second of execution stays. Only Phase 3 (chunked synthesis) or another voice can cut that.

## The decision rule (all must hold, after the v3 measurement)
1. The overhead outside the worker's own execution time is still **at least 0.8 s per clause** with `ulaw8k` and no reference upload (variant A of the tool).
2. The concurrency probe shows what one Pod can carry (below); without it the cost cannot be stated.
3. The Phase 3 result is known (a go there changes what runs on the Pod: a chunked generator behind the same socket).
4. You accept an always-on cost and a single point of failure per Pod.
If rule 1 fails, the overhead was the upload and the result size and v3 already removed it: **do not build Phase 4.**

**Rule 1 on 26 Oct 2026: holds** (MEASURED, see the next section): with `ulaw8k`, a named voice and no reference upload, the part of a first voice clause that is outside the worker's execution is **2.07 s** in the last real call (queue 1.10 + transport 0.97), and 0.77 s of transport alone in the controlled benchmark (variant A, 4 words). Rules 2-4 are still open (capacity probe, Phase 3 result, your acceptance of an always-on cost).

## Cost (estimate, to be replaced by the probe)
* Serverless price given by you: 24 GB at 0.000192 USD/s (0.69 USD/h). The live TTS endpoint already has **1 active worker**, i.e. it already pays for an always-on GPU.
* One call minute costs about `(assistant speaking seconds per minute x 0.58 + clauses per minute x 1.4 s) x 0.000192`. *Assumed* 20 s of speech and 5 clauses a minute:
  (11.6 + 7.0) s = 18.6 s -> **about 0.0036 USD per call minute** on serverless (TTS only).
* A 24 GB Pod running all day: about 0.69 USD/h *if* the Secure-cloud price equals the serverless one (*assumed*; the real price needs the account API) = **0.0115 USD per minute, 16.6 USD/day, about 500 USD/month per Pod.**
* Break-even: the Pod is cheaper than serverless only above about **3 call-minutes of TTS work per wall-clock minute** (0.0115 / 0.0036), i.e. about 3 calls speaking at the same moment on average.
  Below that, serverless stays cheaper; **but if the active worker is kept anyway, the Pod replaces it and costs about the same.**
* How many calls one GPU carries is **not known** (the probe measures whether jobs run in parallel on a worker; real capacity needs a load test on a Pod). If TTS runs at 0.58 x real time while
  speaking, one GPU can in principle serve about 1.7 simultaneous speakers, fewer with the fixed 1.4 s per clause. That is thin for the 10 concurrent calls the service is configured for.

## What would be built (about a week, with tests on a fake server first)
* A small TTS server on the Pod: WebSocket, JSON control messages (`start`, `text`, `cancel`, `ping`) and binary `ulaw8k` frames back; one generation at a time per connection, a global queue.
* A client in the service (`PersistentTTS`), behind a per-tenant flag (default OFF), with a **fallback to the serverless job** whenever the socket is down, so a dead Pod degrades speed and never breaks a call.
* Authentication (a short-lived token per call), heartbeats, back-pressure, reconnect with backoff, health and metrics, cancel that stops the GPU work within one chunk.
* Deployment: image, restart policy, a standby Pod. **Open question:** RunPod's HTTP proxy puts a Cloudflare layer in front of WebSockets (idle and request time limits); a direct TCP port may be needed. Not verified.

## Update 26 Oct 2026: what the last real call shows
MEASURED in the stored trace of the last call (7 turns, 6 warm; numbers only in `docs/evidence/last_call_trace_2026-10-26.json`; the full reading is in `docs/LATENCY_NEXT_STEPS_2026-10-26.md`). Warm medians:

| Stage | Time | What a persistent connection changes |
|---|---|---|
| Silence window | 0.70 s | nothing (it is the end-of-turn rule) |
| Speech-to-text, client time | 1.48 s = queue 0.14 + run 0.64 + **about 0.70 s** submit/poll/result | the 0.70 s goes if the speech-to-text runs on the Pod too |
| Brain, first token / first clause | 0.74 s / 0.98 s | nothing (stays on serverless, streams already) |
| **Voice, first clause** | **4.38 s = queue 1.10 + run 2.38 + remainder 0.97** | queue and remainder go: **-2.07 s**; run stays |
| Voice to voice | 7.51 s | about 5.4 s with the voice on the Pod (ESTIMATE), about 4.8 s with the speech-to-text there too |

The remainder (0.97 s) is network and polling (POST `/run` 0.32 s, polls and result 0.51 s, both MEASURED in the 30-row benchmark; decoding, codec and playout cost under 1 ms). The queue (1.10 s) is a warm pickup that also appears after 150 s idle gaps and is 0.14 s for back-to-back jobs: **a keep-alive ping (built offline, section 4 of the next-steps document) may remove it for about USD 0.007 per supervised call; measure that first, it is much cheaper than a Pod.** The first clause of the last call was 8-14 words (RECONSTRUCTED), not 4; a short first clause (draft in section 3 there) cuts the run time on the same worker, and it also helps on a Pod.

## Monthly cost of one 24 GB Pod (ESTIMATE; the Pod price is not read from the account, it is the serverless rate of the live endpoint, USD 0.000192/s)
| Case | Hours | USD |
|---|---|---|
| Always on, 30 days | 720 | **about 498** (0.6912 per hour) |
| Business hours, 10 h x 22 days | 220 | about 152 |
| A second standby Pod, always on | 720 | about 498 more (about 995 for the pair) |
| Network volume for the model weights (assumed 50 GB at an assumed USD 0.07 per GB-month) | | about 3.5 |
| Pod time for one measurement (spike 1 of the feasibility plan, cap) | 520 s | 0.10 |

If the voice endpoint keeps its **active** worker anyway, that worker already costs about the same USD 498 a month, so the Pod **replaces** it at about the same price. If the worker scales to zero, pay-per-use serverless (about USD 0.0036 per call minute for the voice alone at 20 s of speech and 5 clauses a minute) stays cheaper than the Pod below about 3 simultaneous speaking calls (see Cost). One Chatterbox GPU runs one job at a time (the worker's `GEN_LOCK`), about 1.7 simultaneous speakers at best: the configured 10 calls need several Pods or a smaller model, which multiplies the figure above.

## Security (design; nothing created)
* **Private first.** Best: the service host and the Pod on one private network (a WireGuard/Tailscale tunnel, or RunPod's own private networking if the account offers it: not verified). Then the Pod listens on the private address only and nothing is exposed.
* **If the Pod must be reached over the internet:** `wss://` only (TLS from the RunPod HTTPS proxy, or the server's own certificate on a direct TCP port), **plus** a per-call token: a JWT signed with a secret that exists only in the service and the Pod server, claims `call`, `tenant`, `exp` (60 s to connect, a lifetime for the call, never in the URL query: send it in the first message), rejected on any mismatch; the secret in a file outside the repository, never in a chat; rotate by redeploying. Optional: the Pod accepts only the service's egress IP (not available behind a changing home address).
* The proxy's URL is guessable: **never** rely on obscurity. No unauthenticated endpoint, including health (return nothing but "ok" without auth).
* The Pod holds the reference voices: the volume is private to the account; the voice files are not served; logs on the Pod carry sizes and timings only, never text (the same rule as the service).
* A **dead-man switch** so a forgotten Pod cannot bill for days: the launcher's hard deadline (already in `tools/phase3/launch_pod.py`) and an auto-stop on no connection for N minutes.
* No RunPod account key lives on the service host: only the per-call secret. The account key is needed once, to create the Pod, and is revoked afterwards. **No key is requested now.**

## What must change in the client (when and if this is built)
Today: `StreamingTurn._synth` calls `tts(text, mode)` (`app/ai/streaming/live.py`), which calls `ModelClients.synthesize` (`app/ai/clients.py`), which POSTs `/run`, polls `/status`, decodes `audio_base64`. `synthesize` is the **single entry point**, so a second transport can sit behind it:
1. **A `PersistentTTS` client** (per call, behind a new tenant flag `tts_transport`, default `runpod`): connect when the media connects (in parallel with the warm-up), send the token as the first message, keep one socket for the call, close it in `AiService._finish` (where the keep-alive is stopped today).
2. **Protocol:** `text` (id, text, language, voice) -> binary `ulaw8k` frames and a final `done` (id, `audio_s`, `gen_s`); `cancel` (id) replaces the RunPod `/cancel` call (`clients.py:260-265`) and must stop the GPU within one chunk; `ping`/`pong` every 20-25 s (the proxy drops idle sockets after about 100 s: unverified).
3. **Step 1, no change in `StreamingTurn`:** `synthesize` returns the same `(pcm16, 8000)` after `done`: the queue and the remainder go, nothing else moves. **Step 2:** feed frames into the playout as they arrive (`TurnPlayout.enqueue` takes whole clauses today): only worth it with chunked generation on the Pod (Phase 3).
4. **Fallback:** if the connect fails or the socket drops, the next clause goes through the existing `/run` path (kept as is) and the socket is retried with backoff; a dead Pod costs speed, never the call.
5. **Trace:** the Pod's own `gen_s` and `audio_s` are stored as numbers (they close the gap that the audio length is not recorded today); `note_runpod_response` is not used for this path.
6. **Tests:** a fake WebSocket server first (mock; labelled SIMULATED), the same barge-in, fallback and no-words-in-logs tests as for the RunPod path; the keep-alive ping is not needed for this path (the socket's ping/pong replaces it).

## Risks
* **Single point of failure** per Pod and no autoscaling: a crash or a Pod reclaim takes the voice away (fallback to serverless works, at today's speed and cost).
* **Capacity:** one GPU job at a time; a second call's first clause waits behind the first call's clauses (head-of-line blocking) unless there are several Pods. Not measured under load.
* **Proxy behaviour unverified:** Cloudflare idle and request limits on WebSockets, added latency of the proxy, whether a direct TCP port is available on the chosen cloud type. The one-hour echo test (spike 1) answers it, with a cap.
* **Cold boot** of a Pod is minutes (image pull, model load): a standby Pod or a long-lived Pod is required; a restart during a call drops it.
* **Always-on cost** with no call traffic (about USD 498 a month) before the product has real volume.
* **Security surface** grows from RunPod's authenticated API to a socket we operate (token handling, TLS, patching of the Pod image).
* **Gain is bounded:** it does not make Chatterbox faster (run 2.38 s stays): without a shorter first clause or chunked generation the real answer stays at about 5.4 s.

## What I will NOT do without your go
Start a Pod, build any of the above, or change the live endpoints. The first step, if the rule says yes, is a one-hour Pod test with a throw-away echo server to measure the real round trip through RunPod's proxy and a TCP port, with a cap.
