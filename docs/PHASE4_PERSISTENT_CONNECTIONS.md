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

## What I will NOT do without your go
Start a Pod, build any of the above, or change the live endpoints. The first step, if the rule says yes, is a one-hour Pod test with a throw-away echo server to measure the real round trip through RunPod's proxy and a TCP port, with a cap.
