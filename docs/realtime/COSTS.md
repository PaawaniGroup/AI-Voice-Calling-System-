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
