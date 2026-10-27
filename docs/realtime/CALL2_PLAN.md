# Plan for the second supervised call (PLAN ONLY: nothing here is approved or run)

Why: call 1 (27 Oct 2026) gave ONE recorded turn (n=1). No median, no p90, no gate verdict is possible from it. Findings that shape this plan (MEASURED unless marked):
- the SPEECH endpoint was cold for the first utterance: STT queue 13.46 s (exec 0.27 s); the second STT job (the merged audio) queued 0.84 s;
- the caller's speech reached the pipeline in three pieces; two attempts were cancelled as `merged` (service counter `turns_cancelled{reason="merged"} = 2`); the 7.34 s of audio in the recorded turn = about 6.0 s (the utterance cap, MAX_UTTERANCE_MS) + about 1.3 s (INFERENCE from the numbers; the new `endpoint_reason` / `utterance_ms` / `pieces_merged` labels will show it);
- `t_endpoint` came 40.8 ms after the last voiced frame, so that turn was NOT ended by the 700 ms silence rule; its voice-to-voice figure contains no 700 ms wait;
- the recorded turn ended `interrupted` 0.4 s before `plivo stream closed reason=ws_disconnected`: the third clause was cancelled by the hangup, not by a barge-in (new label: `hangup`).

## This is ONE call, call B (all new flags on), first: an INTEGRATION CHECK, not an experiment
Call A (the baseline, flags off) is run ONLY if B shows a surprise (a number that contradicts the SIMULATED expectation, a quality problem, a regression against the 7.51 s baseline). With one call there is no with/without comparison: B is an integration check with many flags on. It can show that the whole chain works and what the stages cost, but it CANNOT attribute any effect to a single flag (stream_ramp, tts_adaptive_poll, ack_enabled, glossary_correction and strict_grounding all change at once). The evidence is B's per-turn numbers against the MEASURED 7.51 s baseline and against the stage figures of call 1. Say so in the report, and never write 'flag X saved Y' from it.

### If B shows a surprise: the extra single-flag steps (each its own short call or SIMULATED check first)
1. Rerun with ONE flag changed at a time from the surprise's neighbourhood: stream_ramp alone (first-clause words and stop reasons, gaps), tts_adaptive_poll alone (voice polls, remainder), ack_enabled alone (t_ack_first_frame, overlap and cut numbers), glossary_correction alone (counts) , strict_grounding alone (quality, by listening).
2. Call A (all new flags off) as the baseline for the comparison.
3. For an STT surprise: the match test (stt_match_test.py) and, only then, stt_raw_pcm or stt_adaptive_poll as the only change of a call.
4. For a cap/merge surprise: utterance_cap_raise as the only change of a call.
Each step needs its own request (cost, cap, stop rules) before it runs.

### Flags in call B
on: streaming_pipeline, tts_ulaw8k, devanagari_hindi, full_duplex_input, trace_evidence, tts_voice utkarsh (as now); stream_ramp, tts_adaptive_poll, ack_enabled (clip set `ok`), glossary_correction, strict_grounding.
off: stt_raw_pcm, tts_keepalive, rt_transport, stt_adaptive_poll (the STT poll draft is not live: it needs tests and a decision), faq_grounding.

### The caller's script (the operator reads it; I never see or print the words)
- 8-10 turns of normal length. Every question under 5 s (the 6 s cap splits longer speech and the merge re-sends ALL the audio to STT).
- ONE longer question on purpose (about 8-9 s of continuous speech), placed as turn 5 or 6, so the cap/merge behaviour and its cost are measured on purpose: its evidence line shows `endpoint_reason`, `utterance_ms`, `pieces_merged` and the STT jobs.
- Mix: a short factual question, a question with a glossary term, one barge-in on purpose (talk over a reply once), the rest ordinary.
- The tenant policy caps a call at 180 s (the operator's policy): about 8-9 turns of 17-20 s. Ten turns need a cap change that is the operator's decision; otherwise stop at 8-9.

## Evidence to collect (numbers and labels only)
Per turn, from the evidence file: voice-to-voice; `turn.endpoint_reason` (silence | cap), `utterance_ms`, `pieces_merged`, `cancel_reason` (barge_in | merged | superseded | hangup), `outcome` (done | interrupted | cancelled_before_audio | no_answer); STT stages (submit, queue, exec, polls, poll time, remainder, request bytes); Brain first token and first clause; first-clause words and stop reason; voice queue, exec, polls, remainder per clause; gaps, underruns, silence_ms; barge-in `interrupt_to_last_frame_ms`; ack: `ack_at_ms`, `ack_ms`, `ack_cut_ms`, `ack_overlap_ms` (must be 0), `ack_to_answer_ms`, `t_ack_first_frame`; `turn.glossary_corrections` (count); strict grounding: no replacement code exists in this build (reply_guard not wired), so that count is 0 by construction. Then `from_trace.py` + `gate.py`. Plus the service counters (merged cancels). n will be about 8-9, below the gates' minimum (12 / 20): the gates stay NOT_EVALUATED; the report gives medians and ranges.

## Endpoints to keep warm (operator's console settings; undone afterwards)
- voice Active workers = 2 (queue 133-245 ms MEASURED with this setting, 1.2 s without);
- speech (STT) Active workers = 1 (call 1: a 13.5 s cold queue on the first utterance);
- Brain warm: a wake job just before (cold start 140.3 s MEASURED), or Active = 1 for the window (the operator's choice).
Steps as in Stage 0: env check, console settings, wake (parallel, completed jobs only), prepare flags, checklist (`--expect-ack`), arm, CALL, disarm + rollback, then Active workers back to 0 for voice, speech and Brain.

## Estimated cost of ONE call (ESTIMATE; Billing is authoritative; Active-worker time comes from the console setting)
Window about 15 min (wake 2.5-3 min for the Brain cold start, the call 3 min, setup and teardown) = 900 s per Active worker. Worker-seconds: voice 2 x 900 = 1800, speech 900, Brain 900.
| | $0.00019/s | $0.0003/s |
|---|---|---|
| all three endpoints at one price (3600 worker-s) | **$0.68** | **$1.08** |
| Brain at $0.000486/s (the operator's 48 GB figure, UNVERIFIED for this endpoint), voice and speech at the price in the column | **$0.95** | **$1.25** |
Brain alone at $0.000486/s for 900 s = $0.44. Wake jobs and the call's own jobs add well under $0.05. If call A is needed afterwards in the SAME window add about 8 minutes (about 55 % more). A 25-minute window (more caution, a retry) scales the table by 1.67. Program cap $6.

## Before this call (free, offline; each test run needs the operator's "APPROVED: run")
1. Re-run the evidence tests (one setup error of mine, the missing `pv` fixture import, is fixed), commit, restart the service (operator) so the code is live.
2. Decisions still open: the 6 s cap options (see the operator report), `stt_adaptive_poll` (draft only), the live STT handler (the operator pastes one worker log line).

## Update (27 Oct 2026, evening): STT facts that apply to call B
- The live STT worker is VERY LIKELY handler (7)-compatible (UNVERIFIED until one real job proves `audio_pcm16_b64`); keep `stt_raw_pcm` OFF in call B unless the operator decides to test the draft with its automatic one-shot fallback (then it is the ONLY STT change in that call; say so in the report).
- Speech Active = 1 is not optional: the STT cold start is MEASURED at 21-32 s (13.46 s on call 1) (STT_ANALYSIS.md).
- The longer question on purpose measures the cap/merge cost; STT time model: `T = 1.10 + 0.09 x L + 0.0063 x chars + queue` (ESTIMATE).