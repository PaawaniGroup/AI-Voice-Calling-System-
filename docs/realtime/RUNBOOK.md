# Runbook (grows with each stage; nothing here has been switched on)

## Measure (free, offline)
- Simulated run, one scenario: `python tools/realtime/harness.py --scenario ramp --workers 2 --n 12 --evidence out.json`
- Compare runs with the baseline and the budget: `python tools/realtime/table.py a.json b.json`
- Gate: `python tools/realtime/gate.py out.json [--baseline previous.json --max-regress-pct 5]` (exit 0 = no FAIL; 1 = FAIL or regression; 2 = bad input). A SIMULATED file is always NOT_EVALUATED against the MEASURED gates.

## Measure (REAL; chargeable; REQUEST + "APPROVED:" with a cap first)
`python tools/realtime/harness.py --real --recordings <folder outside every repo> --approved "<id>" --cap-usd 0.30 --voice-workers-declared 2 --evidence out.json`
Keys come from the environment variables `RUNPOD_STT_KEY`, `RUNPOD_BRAIN_KEY`, `RUNPOD_TTS_KEY` (put there from the key files for that one process; never printed). Stops at the cap, at the first HTTP 401 or 403, after 3 consecutive errors. Readiness is judged only from completed jobs and their queue times (under 5 s = already warm; 20 s or more = cold start), never from /health worker counts.

## Flags (tenant `ai_config`, all default OFF; set and verified through `AiService.get_config`)
Existing on `integration`: `stream_ramp`, `tts_adaptive_poll`, `trace_evidence`, `stt_raw_pcm`, `glossary_correction`, `strict_grounding`.
Switch on for a supervised call and back: `scripts/prepare_supervised_call_tenant.py --apply`, `scripts/pre_call_checklist.py`, `scripts/plivo_testcall.py status|prove|disarm --yes`, `scripts/rollback_supervised_call_tenant.py --backup <the backup file printed by prepare>` (disarm FIRST; the rollback reports whether the tenant is still armed).
Flags of Stages 2-4 (`rt_transport` and others) are added here with their on/off commands when they exist.

## After every paid step
State what must be off (Pods, Active workers) and verify it read-only (list Pods at start and end; the console setting for Active workers is the operator's). If a Pod might still be running: "ACTION NEEDED: DELETE POD NOW".
