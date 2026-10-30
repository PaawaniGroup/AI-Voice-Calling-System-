# CALL B runbook (PLAN ONLY: nothing here is run; every step waits for the operator's word shown)

Call B = ONE supervised call on `callbuild` (tip 85a703a), all call flags on: an INTEGRATION CHECK, it cannot attribute effects to single flags (see CALL2_PLAN.md). Counts, times and labels only; never a key, any part of a phone number, or a caller/reply word.

## Flags
- SET by `prepare_supervised_call_tenant.py --apply --with-ack` (backup first): tts_ulaw8k, devanagari_hindi, streaming_pipeline, full_duplex_input, tts_voice utkarsh, **stream_ramp, tts_adaptive_poll, trace_evidence, glossary_correction, strict_grounding, ack_enabled**.
- OFF (listed, never written; the checklist fails if on): **stt_raw_pcm, stt_adaptive_poll, utterance_cap_raise** (unless the operator says; then `--allow-on NAME` for the checklist and the operator's own `put_config`), tts_keepalive, rt_transport. With `utterance_cap_raise` off the cap is today's 6 s: the long question WILL be split at 6 s (that is what it measures).

## Warm settings (the operator's console; undone at the end)
voice Active workers = 2; speech (STT) Active workers = 1; Brain warm (a wake job, or Active = 1 for the window). STT cold start MEASURED 21-32 s, Brain 140 s, voice 49 s.

## Handshakes in order
0. (me, read-only) callbuild tip 85a703a, status clean, no other pytest, which process holds 8800.
1. **ACTION NEEDED: set voice Active = 2, speech Active = 1 (and Brain Active = 1 if chosen) in the RunPod console (reply "active set").** The window starts here; if any later step fails I print "SET ACTIVE=0 NOW".
2. (read-only) `.env` present in `C:\Users\Admin\Desktop\MME_wt_call`: the check prints ONLY "yes" or "no" (no size, no content).
3. **Restart (needs "APPROVED: restart"):** stop the uvicorn on 8800 after confirming it is the `MME_wt_call` one (it runs the OLD code of f25c965; the new commits are not live until this restart); start with `scripts/start_service_with_keys.sh` (key files `/mnt/c/mme_keys/runpod_{stt,brain,tts}.txt`; its masked key lines are filtered out of the log), `MME_TEL_AI_ACK_DIR=/mnt/c/mme_ack_audio`, `MME_TEL_AI_TRACE_EVIDENCE_PATH=/mnt/c/mme_trace/callB_trace.jsonl` (a NEW file: clean counts). Report `/health/ready`, exactly one process on 8800, the variable NAMES set, and the build stamp (`plivo_testcall.py status`: head 85a703a, verdict exact_match).
4. **Wake (needs "APPROVED: wake, cap $0.20"):** Brain, voice, STT in parallel, one job each, completed jobs only (job id, delay, execution), no health gating; stop at the cap, the first 401/403, 3 errors. Estimate: well under $0.15 (call-1 wake: at most $0.13 at $0.0003/s with the Brain cold).
5. **Flags (needs "APPROVED: prepare"):** BEFORE the call I print the checklist result in full: the flags set (stream_ramp, tts_adaptive_poll, trace_evidence, glossary_correction, strict_grounding, ack_enabled, plus the base flags), the FIVE flags that must stay off with their state (stt_raw_pcm, stt_adaptive_poll, utterance_cap_raise, tts_keepalive, rt_transport: each must read off), `ack_after_ms`, the clip set status, `utterance_max_s`, and PASS/FAIL. No call handshake is printed unless it passed. `prepare_supervised_call_tenant.py --apply --with-ack` (backup path printed), then `pre_call_checklist.py --expect-ack` (must pass; prints `ack_after_ms`, the clip set status, `utterance_max_s`), then `plivo_testcall.py status` and `prove --yes`. **`prove` ends with the tenant DISARMED** (call 1), so:
6. **ACTION NEEDED: arm the tenant yourself: `python scripts/plivo_testcall.py arm --caller <your number> --yes --watch` from MME_wt_call (reply "armed").** I verify `status` (read-only: enabled, allowlist enforced, 180 s cap, no live calls, exact_match), then print:
7. **ACTION NEEDED: CALL NOW (reply "call done").** I do nothing else while waiting.
8. After "call done": `plivo_testcall.py disarm --yes`, `rollback_supervised_call_tenant.py --backup <the path printed by prepare>` (restores or unsets every call flag, including the new keys), verify policy disabled, flags gone, no live call. Then **ACTION NEEDED: set voice, speech (and Brain) Active workers = 0 (reply "active zero").**
9. Report (below). The service keeps running the new code until the operator decides otherwise.

## The caller's script (the operator writes and reads their own questions; I never see them)
- **7-8 turns in the 180 s cap** (about 17-22 s per turn): 6-7 normal questions, each under 5 s, plus ONE long question of 8-9 s of continuous speech, **turn 3** (so the key measurement exists even if the cap ends the call early). Do not plan a 9th turn.
- Mix (within the 7-8 turns): a short factual question; a question containing a glossary term; ONE deliberate barge-in (talk over a reply once; it uses the next question, no extra turn); the rest ordinary. Leave about 1 s between the end of a reply and the next question except for the barge-in.
- The last turn may be cut by the 180 s cap: it shows as `cancel_reason: hangup`.

## Report format (short; counts and times only)
Per turn (from the evidence file, `/mnt/c/mme_trace/callB_trace.jsonl`): voice-to-voice; first sound (`t_ack_first_frame` when the acknowledgement played); first-clause words and stop reason; voice queue / execution; STT stages (submit, queue, execution, polls, remainder, request bytes) with `endpoint_reason`, `utterance_ms`, `pieces_merged`; Brain first token and first clause; ack played yes/no with `ack_at_ms`, `ack_ms`, `ack_cut_ms`, `ack_overlap_ms` (must be 0), `ack_to_answer_ms`; glossary corrections (count); grounding replacements (0 by construction: no replacement code in this build); underruns, gaps, silence_ms; barge-in `interrupt_to_last_frame_ms`; `cancel_reason` and `outcome`. Then medians over turns 2 and later with ranges, against the MEASURED 7.51 s baseline (silence 0.70 + STT 1.49 + Brain 0.74 + voice 4.38) and call 1's stage figures; `from_trace.py` + `gate.py` verdicts with the reason for every NOT_EVALUATED (expected: n below 12 / 20, reply length outside 20-26 words, G1 not applicable); the long question's STT jobs and time against the model T = 1.10 + 0.09 x L + 0.0063 x chars + queue (ESTIMATE); what is still unproven; cost. No claim that any single flag helped.

## Chargeable calls by me: cap $0.45 in total (wake + anything else I run); the Active-worker window below is billed by the console setting, outside that cap.`n`n## Cost of the window (ESTIMATE; Billing is authoritative; Active-worker time comes from the console)
Per minute with voice Active 2, speech 1, Brain 1 (240 worker-seconds): $0.046 at $0.00019/s, $0.072 at $0.0003/s, and with the Brain at $0.000486/s (UNVERIFIED for this endpoint) $0.063 / $0.083 (voice and speech at the price in the column).
| window | $0.00019/s | $0.0003/s | Brain at $0.000486/s (voice, speech at $0.00019 / $0.0003) |
|---|---|---|---|
| 15 min (plan) | $0.68 | $1.08 | $0.95 / $1.25 |
| 25 min (a retry or slow steps) | $1.14 | $1.80 | $1.58 / $2.08 |
The wake (cap $0.20) and the call's own jobs add well under $0.05 beyond the window. Program cap $6.
