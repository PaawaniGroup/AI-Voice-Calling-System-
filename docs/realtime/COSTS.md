# Costs of the real-time latency program

Tags: ESTIMATE unless a Billing figure is quoted. Only Billing is authoritative. Two price points are always shown because the operator's price is unverified:
**$0.00019 per GPU-second ($0.684 per hour)** and **$0.0003 per GPU-second ($1.08 per hour)**. The program cap for all chargeable steps is **$6 total** (operator, 27 Oct 2026). If the balance may be below $1: stop and ask.

## One always-on GPU (a serverless Active worker, or a Pod at the same price: UNVERIFIED)

| hours per month | at $0.00019 per s | at $0.0003 per s |
|---|---|---|
| business hours, 260 h | $177.84 | $280.80 |
| 24/7, 730 h | $499.32 | $788.40 |
| one hour | $0.68 | $1.08 |
| a 10-minute test | $0.11 | $0.18 |

## The program's likely always-on set (ESTIMATE; one GPU per line, no sharing assumed)

| set | GPUs | 260 h at $0.00019 | 260 h at $0.0003 | 730 h at $0.00019 | 730 h at $0.0003 |
|---|---|---|---|---|---|
| voice x2 + STT + Brain | 4 | $711.36 | $1,123.20 | $1,997.28 | $3,153.60 |
| voice x1 + STT + Brain | 3 | $533.52 | $842.40 | $1,497.96 | $2,365.20 |

Sharing a GPU between models (for example STT and the voice) and the number of simultaneous calls one GPU carries are UNKNOWN until Stage 4(c). Cost per call and per month then follow from the measured capacity.

## Paid steps of the program (all REQUESTs, none run; caps from the operator)

| step | cap | estimate at $0.00019 / $0.0003 | status |
|---|---|---|---|
| Stage 0 supervised call (Active workers = 2 for 25 min + queue test + wake) | $0.45 for my jobs; Active-worker time is the console setting | 2 workers x 25 min = $0.57 / $0.90 (plus jobs) | waiting for "active set" |
| G1 persistent-connection round trip on a Pod | $0.10 | about 0.15 h of a Pod = $0.10 / $0.16 | REQUEST later |
| Stage 3 LLM spike (vLLM on a Pod) | $0.50 | | REQUEST later |
| Stage 3 voice chunked-synthesis spike | $1.00 | | REQUEST later |
| Stage 3 STT rolling decode (needs the operator's recordings) | $0.20 | | REQUEST later |
| Stage 4 cached acknowledgement generation (5 neutral clips) | $0.05 | | REQUEST later |
| Stage 4 concurrency probe | $0.30 | | REQUEST later |
| Stage 5 one supervised call with all rt flags | inside the $6 | | REQUEST later |

Every REQUEST will state the action, the estimate at BOTH prices, the endpoints that must be warm, the cap, the stop rules (the cap, the first 401 or 403, 3 consecutive errors) and how to undo it. After every paid step: what must be off (Pods, Active workers) and a read-only verification.

## Stage 2 additions

### Where the app itself runs: PC behind ngrok vs a small cloud server
Every price below is UNVERIFIED (not read from a vendor page; ranges from general knowledge, to be checked before use). A small always-on VM (2 vCPU / 4 GB, one region) typically costs on the order of USD 10-40 per month; a same-region-as-GPU choice and egress are extra. Plus a public IP/domain: UNVERIFIED, small. Removing ngrok (paid plan, UNVERIFIED) may offset part of it.

| | App on the PC behind ngrok | App on a cloud server near the GPUs |
|---|---|---|
| G1 (round trip to the GPU Pod) | PC to the GPU path includes the home link; PC to Cloudflare edge was 57 ms average earlier (MEASURED), so a Pod round trip of 40 ms or less is unlikely (ESTIMATE) | 40 ms or less is plausible only in the same region (UNKNOWN until measured) |
| Live path (Plivo audio in, TTS audio out) | audio crosses ngrok and the home uplink both ways, then the GPU hop; every added hop costs RTT on each exchange (ESTIMATE 50-150 ms per exchange, UNKNOWN) | Plivo to server and server to GPU are datacentre paths; home-link jitter removed |
| Availability | PC must stay on and online | independent of the PC |
The effect on the 2.2 s goal is UNKNOWN until a trace is taken from each location; a G1 probe run from the PC is informational only.

### Memory budget: which models could share one card (all UNVERIFIED; weights only, KV cache and activations extra)
| Model | Weights |
|---|---|
| Qwen3-30B-A3B FP8 | about 31 GB (operator's figure) |
| Whisper (large-v3 class, fp16) | about 3 GB (UNVERIFIED) |
| Chatterbox | about 3-6 GB (UNVERIFIED) |
- 24 GB card: Qwen3-30B-A3B FP8 does NOT fit. Whisper + Chatterbox together (about 6-9 GB of weights) would fit in memory, BUT sharing one 24 GB card between Whisper and Chatterbox is UNVERIFIED, and it is likely to contend under concurrent calls (the STT decode of one caller and the TTS clause of another want the GPU at the same moment), which would raise exactly the stage latencies this program is trying to cut. Treat "speech and voice on one card" as a cost-saving hypothesis for Stage 3 to disprove, not as a plan.
- 48 GB card: all three, about 37-40 GB of weights, leave about 8-11 GB for the LLM's KV cache and activations: tight; prefix caching and several concurrent calls will press it (UNKNOWN). Compute contention (three models on one GPU) can also raise each stage's latency: UNKNOWN until Stage 3 measures it.
- So the likely sets are: Brain alone on a 48 GB card plus STT and voice on a 24 GB card, or all three on one 48 GB card; neither is proven.

### Documentation read for Stage 2 (fetched 2026-10-27)
- https://docs.runpod.io/serverless/load-balancing/overview : endpoint URL `https://ENDPOINT_ID.api.runpod.ai/PATH`; no queue; `/ping` health (200 healthy, 204 initializing); request timeout 2 min (no worker), processing 5.5 min per request, payload 30 MB; described as beta. WebSocket support and scale-from-zero: NOT STATED on the page: UNVERIFIED.
- https://docs.runpod.io/pods/configuration/expose-ports : Cloudflare proxy max connection 100 s (524 after); "Expose TCP Ports" gives direct TCP with a public IP; external port mappings change when the Pod resets; public IP stable for Secure Cloud, may change on Community Cloud. Whether 100 s applies to direct TCP: not stated.
- A search for load-balancing WebSocket support returned no page that mentions it: UNVERIFIED. Do not plan production on a load-balancing WebSocket until RunPod confirms it or a free-text test shows it.

### Monthly GPU totals by layout (operator's prices; ARITHMETIC on UNVERIFIED prices: Pod prices are not confirmed to equal the serverless screenshots)
Prices: Brain on a 48 GB card $0.000486/s = $1.7496/h (operator's screenshots). A 24 GB card at the two program price points: $0.00019/s = $0.684/h and $0.0003/s = $1.08/h.

Per card, per month:
| Card | 260 h | 730 h |
|---|---|---|
| Brain, 48 GB, $0.000486/s | $454.90 | $1,277.21 |
| one 24 GB card, $0.00019/s | $177.84 | $499.32 |
| one 24 GB card, $0.0003/s | $280.80 | $788.40 |

Totals:
| Layout | 24 GB price | 260 h | 730 h |
|---|---|---|---|
| 2 GPUs: Brain 48 GB + one 24 GB card shared by speech and voice (sharing UNVERIFIED, contention likely) | $0.00019/s | $632.74 | $1,776.53 |
| | $0.0003/s | $735.70 | $2,065.61 |
| 3 GPUs: Brain 48 GB + speech 24 GB + voice 24 GB (no sharing) | $0.00019/s | $810.58 | $2,275.85 |
| | $0.0003/s | $1,016.50 | $2,854.01 |
Billing is the only authoritative cost; a serverless Active worker may bill differently from a Pod (UNVERIFIED).
### Monthly GPU totals by layout (ARITHMETIC on UNVERIFIED prices: Pod prices are not confirmed to equal the serverless screenshots)
Prices: Brain on a 48 GB card $0.000486/s = $1.7496/h (operator's screenshots). A 24 GB card at the two program price points: $0.00019/s = $0.684/h and $0.0003/s = $1.08/h.

| Card | 260 h | 730 h |
|---|---|---|
| Brain, 48 GB, $0.000486/s | $454.90 | $1,277.21 |
| one 24 GB card, $0.00019/s | $177.84 | $499.32 |
| one 24 GB card, $0.0003/s | $280.80 | $788.40 |

| Layout | 24 GB price | 260 h | 730 h |
|---|---|---|---|
| 2 GPUs: Brain 48 GB + one 24 GB card shared by speech and voice (sharing UNVERIFIED, contention likely) | $0.00019/s | $632.74 | $1,776.53 |
| | $0.0003/s | $735.70 | $2,065.61 |
| 3 GPUs: Brain 48 GB + speech 24 GB + voice 24 GB (no sharing) | $0.00019/s | $810.58 | $2,275.85 |
| | $0.0003/s | $1,016.50 | $2,854.01 |
Billing is the only authoritative cost; a serverless Active worker may bill differently from a Pod (UNVERIFIED).
## Actual and estimated: acknowledgement clips (27 Oct 2026)
One run of `tools/realtime/gen_ack_clips.py`: 1 warm-up job (cold start, 49.0 s MEASURED) + 5 clip jobs (2.1-2.9 s each) = 63 s wall, about 61 s of worker time. Cost (ESTIMATE; only Billing is authoritative): jobs alone about $0.012 at $0.00019/s and $0.018 at $0.0003/s; with the endpoint's idle tail (about 300 s) about $0.069 / $0.109 (the tool's own projection: $0.11 at the higher price; cap was $0.15). The trim of en_1 was free and offline.
The voice cold start is now 49.0 s in this run (earlier samples: 48.9 s and 41-52 s; MEASURED each).
Remaining requests are unchanged from the last table (G1 Pod, LLM Pod, voice Pod, concurrency, STT decode); the ack request is consumed: total of the others about $1.64 at $0.00019/s and $2.18 at $0.0003/s (the LLM row at the 48 GB price $0.000486/s: $0.73).
## STT language test (auto vs hi vs en): estimate only, not requested
72 jobs (12 recordings x 3 conditions x 2 repeats): about 426 s of billed time with the existing endpoint's 300 s idle timeout (about 131 s if it is lowered for the test): $0.081 (or $0.026) at $0.00019 per second, $0.128 (or $0.039) at $0.0003 per second. Proposed cap $0.25. Details in STT_ANALYSIS.md.