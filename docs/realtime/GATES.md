# Gates of the real-time latency program

Metric: the time from the caller's **last voiced frame** to the **first audio frame sent to Plivo**, from REAL traces only. Audibility at the ear is not measured.
Machine-readable thresholds: `docs/realtime/gates.json` (read by `tools/realtime/gate.py`; pinned by `tests/unit/test_rt_gate.py`, so changing one is a deliberate edit of a test).
Rules: a SIMULATED run can never pass a MEASURED gate; a gate is only judged when the run's scenario and sample size match; nobody tunes a threshold or a test to make a gate pass.
If a gate fails, that workstream stops and the evidence and the options are reported.

## The goal, with the scenario it is stated for (correction a)

"Real answer <= 2.2 s median and <= 3.0 s p90" depends on three things that must be fixed when the number is quoted, otherwise it can be met by cheating the scenario:

| fixed by the gate | value | why it matters |
|---|---|---|
| reply length | about **23 words** (run median 20-26; MEASURED median reply today 23 words) | a longer reply needs more clauses; the later clauses must be gapless |
| clause plan | **ramp**: first clause 2-4 words, second 6-8, later 12-14 | a 9-word first clause costs about 1 s more first audio (SIMULATED) |
| voice workers | **2 warm** voice workers | with one worker the ramp creates silences of about 2.3 s after the first clause (SIMULATED with the MEASURED 1.2 s queue) |

| gate | metric (stat) | threshold | needs | status |
|---|---|---|---|---|
| A-median | last voiced frame -> first audio frame, median | <= 2200 ms | MEASURED, n >= 12, scenario above | NOT YET MEASURED |
| A-p90 | the same, p90 | <= 3000 ms | MEASURED, n >= 20 (PROPOSED: a p90 of 12 samples is one sample), scenario above | NOT YET MEASURED |

### The separate first-clause figures (not part of the goal; each has its own gate)

| gate | stage | budget (ESTIMATE, to be proven) | today (MEASURED) | status |
|---|---|---|---|---|
| FC-stt | STT submit -> final text | 150 ms | 1490 ms | NOT YET MEASURED |
| FC-brain | Brain request -> first clause | 250 ms | first token 740 ms (first clause 240 ms later, UNKNOWN where it sits in the trace) | NOT YET MEASURED |
| FC-voice | first clause sent -> its audio ready | 450 ms | 4380 ms (queue 1100 + run 2380 + transport 970 in the last call; queue 1.2 s again in the 27 Oct test) | NOT YET MEASURED |
| (input) end of turn | last voiced frame -> end-of-turn decision | 300 ms | 700 ms | RANGE ONLY until about 150 clean utterances exist (`tools/eot_replay.py`); keep 700 ms |
| (not measured) path to the ear | first frame sent -> heard | 150 ms | about 200 ms | ESTIMATE only |

The stage budget sums to **1.30 s** (300 + 150 + 250 + 450 + 150, ESTIMATE). The 2.2 s median keeps 0.9 s of room for the second clause, jitter and tails; it does not follow from the budget, it has to be measured.

## First sound is proven separately (correction b)

| gate | metric | threshold | status |
|---|---|---|---|
| FS-first-sound | last voiced frame -> first sound of any kind, median | <= 1200 ms | **BLOCKED** |

The "about 0.9 s" figure is an ESTIMATE: end of turn 0.30 + STT final 0.15 + the acknowledgement's wait after the STT final 0.30 + path to the ear 0.15 = 0.90 s. It exists only if a **cached acknowledgement audio** is played, and:
- **that audio does not exist yet.** Generating it is a chargeable REQUEST (5 neutral clips, cap $0.05, voice endpoint warm; the phrases must not agree, deny or promise anything);
- **the acknowledgement code is not in this tree** (it lives in `quality-work` and was excluded from the integration); it has to be ported and tested before any trace can contain a first sound;
- it is therefore **never part of A-median**: the real answer is judged on its own, and the acknowledgement is judged on a REAL trace in which one was played, with the wait counted from the STT final (`ack_after_ms`).
Under 1 s to the REAL answer is not promised.

## Quality gates (apply to every stage; a faster path that fails one is rejected)
Hindi pronunciation (the operator's listening); STT term accuracy; grounding (no invented fees, cut-offs or dates); barge-in correctness; call safety (allowlist, cap, kill switch). Latency never trades against them.

## What the SIMULATED harness says today (orchestration only, MEASURED inputs; not a result)
`python tools/realtime/harness.py --scenario ramp --workers 2` with the MEASURED medians (STT 1446 ms, Brain first token 737 ms, voice queue 1.2 s, voice wall times of 26 Oct): first audio 6.0 s (today's plan with one worker: 7.7 s; the baseline is 7.31 s MEASURED). The harness is 5.3 % above the baseline (7.70 s against 7.31 s; the test allows 10 %), SIMULATED. The goal of 2.2 s is therefore far away with today's models and transport: Stages 2 and 3 have to remove the queue and the gateway floor and make the voice first audio fast. This is SIMULATED and proves nothing about the models.

## Status update (27 Oct 2026, evening)
- **FS-first-sound** is still BLOCKED, but two of its three blockers are gone: the acknowledgement code is ported and committed (flag `ack_enabled`, default OFF) and five clips exist outside the repo (`C:\mme_ack_audio\`; four as generated, en_1 as a trimmed copy). It stays BLOCKED until the operator has listened, the directory is configured, and a REAL trace shows a played acknowledgement (`t_ack_first_frame`).
- **G1-roundtrip** (rt_roundtrip_ms median <= 40 ms, n >= 100, `measured_from: cloud`) is defined in `gates.json`; not measured (no Pod key; the host the service runs on is undecided, the PC answer being "stay for now"; a PC run is informational and cannot pass).
- Every other gate: unchanged, NOT YET MEASURED.