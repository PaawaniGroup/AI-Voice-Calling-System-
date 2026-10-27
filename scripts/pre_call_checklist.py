#!/usr/bin/env python3
"""Pre-call checklist for supervised call verification.

Validates settings from BOTH collections where the call path reads them:
  1. ai_config (ai models and experimental flags):
     - Flags (read THROUGH AiService.get_config, i.e. what the runtime sees; a flag stored but not returned fails): tts_ulaw8k, devanagari_hindi,
       streaming_pipeline, full_duplex_input, stream_ramp, tts_adaptive_poll, trace_evidence, glossary_correction, strict_grounding all True;
       stt_raw_pcm, faq_grounding, ack_enabled, keepalive_enabled must be off
     - Voice: tts_voice == "utkarsh"
     - Endpoints:
       - STT:   ua9l3gus6qjnmg
       - Brain: hu8qhrvlxbv314
       - TTS:   6bmxas8ccwzhrv
     - key_refs: indirect variable names configured

  2. tenant_policies (the exact collection app/services/plivo_calls.py:141-164 reads):
     - enabled == True
     - allowed_callers == [<expected_caller>] (or non-empty if not specified)
     - max_duration_s == 180

Strictly prints NO secrets, raw keys, or plain phone numbers.
Fails with non-zero exit code if anything differs.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pymongo import AsyncMongoClient

from app.ai.service import AiService
from config.settings import Settings

TENANT_ID = "mme-test"

EXPECTED_ENDPOINTS = {
    "stt": "ua9l3gus6qjnmg",
    "brain": "hu8qhrvlxbv314",
    "tts": "6bmxas8ccwzhrv",
}

EXPECTED_AI_FLAGS = {
    "tts_ulaw8k": True,
    "devanagari_hindi": True,
    "streaming_pipeline": True,
    "full_duplex_input": True,
    "tts_voice": "utkarsh",
    "stream_ramp": True,
    "tts_adaptive_poll": True,
    "trace_evidence": True,
    "glossary_correction": True,
    "strict_grounding": True,
}
# Must NOT be on for the call (absent counts as off). reply_guard has no flag in this tree.
MUST_BE_OFF = ("stt_raw_pcm", "faq_grounding", "ack_enabled", "keepalive_enabled", "tts_keepalive", "stt_adaptive_poll", "utterance_cap_raise")
ALLOWABLE_ON = ("stt_adaptive_poll", "utterance_cap_raise")      # off unless the operator says so for THIS call (--allow-on NAME); stt_raw_pcm, tts_keepalive, rt_transport can never be allowed here


def rt_transport_is_off(value) -> bool:
    """The persistent transport is OFF when nothing is set, or every service is "queue"."""
    return not value or (isinstance(value, dict) and all(v == "queue" for v in value.values()))


def mask_number(num: str | None) -> str:
    if not num:
        return "[none]"
    return "[ARMED_NUMBER]"


def extract_endpoint_id(url: str | None) -> str:
    if not url:
        return ""
    clean = url.rstrip("/")
    if clean.endswith("/runsync") or clean.endswith("/run"):
        return clean.split("/")[-2]
    return clean.split("/")[-1]


async def run_checklist(expected_caller: str | None = None, mongo_uri: str | None = None, db_name: str | None = None, expect_ack: bool = False, ack_dir: str | None = None,
                        allow_on: tuple = ()) -> bool:
    uri = mongo_uri or "mongodb://127.0.0.1:27117"
    expected_flags = dict(EXPECTED_AI_FLAGS)
    allow_on = tuple(f for f in allow_on if f in ALLOWABLE_ON)         # anything else is ignored: the always-off flags cannot be allowed
    must_be_off = tuple(f for f in MUST_BE_OFF if f not in allow_on)
    if expect_ack:                                                    # the operator said the acknowledgement is part of this call
        expected_flags["ack_enabled"] = True
        must_be_off = tuple(f for f in must_be_off if f != "ack_enabled")
    dbname = db_name or "mme_telephony"
    client = AsyncMongoClient(uri)
    db = client[dbname]

    try:
        ai_cfg = await db["ai_config"].find_one({"tenant_id": TENANT_ID})
        policy = await db["tenant_policies"].find_one({"tenant_id": TENANT_ID})

        failures = []

        print("=" * 70)
        print(f"PRE-CALL SUPERVISED CALL CHECKLIST ({TENANT_ID})")
        print("=" * 70)

        # -------------------------------------------------------------
        # Part A: ai_config (flags and endpoints)
        # -------------------------------------------------------------
        print("PART A: ai_config (AI Pipeline & Model Endpoints)")
        if not ai_cfg:
            failures.append("ai_config document missing for tenant")
            print("  [FAIL] ai_config document not found!")
        else:
            # 1. Endpoints
            stt_id = extract_endpoint_id(ai_cfg.get("stt_url"))
            brain_id = extract_endpoint_id(ai_cfg.get("brain_url"))
            tts_id = extract_endpoint_id(ai_cfg.get("tts_url"))

            for name, actual, expected in [("STT", stt_id, EXPECTED_ENDPOINTS["stt"]),
                                          ("Brain", brain_id, EXPECTED_ENDPOINTS["brain"]),
                                          ("TTS", tts_id, EXPECTED_ENDPOINTS["tts"])]:
                match = (actual == expected)
                status = "PASS" if match else "FAIL"
                print(f"  [{status}] Endpoint {name:6s}: actual={actual} | expected={expected}")
                if not match:
                    failures.append(f"{name} endpoint mismatch: {actual} != {expected}")

            # 2. Flags AS THE RUNTIME SEES THEM: through AiService.get_config (its allowlist silently drops any field it does not list: the key_refs bug)
            runtime = await AiService(db, None, Settings(_env_file=None), None).get_config(TENANT_ID)
            for flag, expected in expected_flags.items():
                actual = runtime.get(flag)
                match = (actual == expected)
                dropped = ai_cfg.get(flag) != actual                       # stored in the document but not what get_config returns
                status = "PASS" if match and not dropped else "FAIL"
                print(f"  [{status}] Flag {flag:20s}: via get_config={actual!r} | expected={expected!r}" + ("  (DROPPED by get_config)" if dropped else ""))
                if not match:
                    failures.append(f"flag {flag} via get_config mismatch: {actual!r} != {expected!r}")
                if dropped:
                    failures.append(f"flag {flag} is stored but get_config does not return it (allowlist)")
            # Dependencies between flags: a flag that is on but cannot take effect is a silent failure of the call's purpose
            if runtime.get("stream_ramp") and not runtime.get("streaming_pipeline"):
                print("  [FAIL] stream_ramp is ON but streaming_pipeline is OFF: the ramp only works on the streaming path")
                failures.append("stream_ramp needs streaming_pipeline")
            if runtime.get("tts_adaptive_poll") and str(runtime.get("runpod_mode") or "run").lower() == "runsync":
                print("  [FAIL] tts_adaptive_poll is ON but runpod_mode is runsync: the overlapping polls only exist in run mode")
                failures.append("tts_adaptive_poll needs runpod_mode run")
            if runtime.get("trace_evidence"):
                print("  [info] trace_evidence also needs MME_TEL_AI_TRACE_EVIDENCE_PATH in the SERVICE environment (not visible from here)")
            for flag in must_be_off:
                on = bool(runtime.get(flag)) or bool(ai_cfg.get(flag))
                print(f"  [{'FAIL' if on else 'PASS'}] Flag {flag:20s}: must be OFF -> {'ON' if on else 'off'}")
                if on:
                    failures.append(f"flag {flag} must be OFF for this call")
            rt_on = not rt_transport_is_off(ai_cfg.get("rt_transport")) or not rt_transport_is_off(runtime.get("rt_transport"))
            print(f"  [{'FAIL' if rt_on else 'PASS'}] Flag {'rt_transport':20s}: must be OFF (all services on the queue path) -> {'ON' if rt_on else 'off'}")
            if rt_on:
                failures.append("flag rt_transport must be OFF for this call")
            for flag in allow_on:                                            # allowed by the operator for this call: shown, with the value that matters
                print(f"  [info] {flag} is ALLOWED ON for this call (--allow-on): via get_config={runtime.get(flag)!r}")
            print(f"  [info] utterance_max_s: {runtime.get('utterance_max_s') if runtime.get('utterance_max_s') is not None else 'None (6.0 s, today)'}"
                  f" (read only with utterance_cap_raise, which is {'ON' if runtime.get('utterance_cap_raise') else 'off'})")
            # The acknowledgement: its wait and the clip set, always shown (numbers and a reason code only: no path content, no phrase)
            from app.ai.streaming.ack import AckSet
            ack_set, ack_why = AckSet.load(ack_dir if ack_dir is not None else Settings(_env_file=None).ai_ack_dir)
            wait = runtime.get("ack_after_ms")
            print(f"  [info] ack_after_ms: {wait if wait is not None else 'None (the built-in default 1500 ms applies)'}")
            print(f"  [info] ack clip set (this process's MME_TEL_AI_ACK_DIR; the SERVICE reads its own copy at its start): {ack_why}"
                  + (f", languages {ack_set.langs}" if ack_set is not None else ""))
            if expect_ack:
                if ack_why != "ok":
                    print("  [FAIL] --expect-ack: the clip set does not load")
                    failures.append(f"ack clip set status {ack_why}, expected ok")
                if not runtime.get("streaming_pipeline"):
                    print("  [FAIL] ack_enabled needs streaming_pipeline")
                    failures.append("ack_enabled needs streaming_pipeline")

            # 3. key_refs (through get_config too)
            key_refs = runtime.get("key_refs")
            refs_ok = isinstance(key_refs, dict) and all(k in key_refs for k in ("stt", "brain", "tts"))
            status = "PASS" if refs_ok else "FAIL"
            print(f"  [{status}] key_refs: {key_refs}")
            if not refs_ok:
                failures.append(f"key_refs incomplete or missing: {key_refs}")

        # -------------------------------------------------------------
        # Part B: tenant_policies (where call path plivo_calls.py:141-164 reads)
        # -------------------------------------------------------------
        print("\nPART B: tenant_policies (Call Path Enforcement: plivo_calls.py:141-164)")
        if not policy:
            failures.append("tenant_policies document missing for tenant")
            print("  [FAIL] tenant_policies document not found!")
        else:
            # 1. enabled
            pol_enabled = policy.get("enabled")
            match = (pol_enabled is True)
            status = "PASS" if match else "FAIL"
            print(f"  [{status}] policy.enabled:      actual={pol_enabled!r} | expected=True")
            if not match:
                failures.append(f"tenant_policies.enabled mismatch: {pol_enabled!r} != True")

            # 2. allowed_callers
            allowed = policy.get("allowed_callers")
            caller_match = bool(allowed and isinstance(allowed, list) and len(allowed) > 0)
            if expected_caller:
                clean_exp = expected_caller.strip()
                caller_match = bool(allowed and clean_exp in allowed)
            status = "PASS" if caller_match else "FAIL"
            n_allowed = len(allowed) if isinstance(allowed, list) else 0
            masked_exp = "the expected caller is in the list" if expected_caller else "non-empty list"
            print(f"  [{status}] policy.allowed_callers: COUNT={n_allowed} (no number is printed) | expected={masked_exp}")
            print(f"  [info] policy state: {'ENABLED' if pol_enabled is True else 'DISABLED'}")
            if not caller_match:
                failures.append("tenant_policies.allowed_callers mismatch with expected caller")

            # 3. max_duration_s
            max_dur = policy.get("max_duration_s")
            match = (max_dur == 180)
            status = "PASS" if match else "FAIL"
            print(f"  [{status}] policy.max_duration_s: actual={max_dur} | expected=180")
            if not match:
                failures.append(f"tenant_policies.max_duration_s mismatch: {max_dur} != 180")

        print("=" * 70)
        if failures:
            print("RESULT: CHECKLIST FAILED with discrepancies:", file=sys.stderr)
            for f in failures:
                print(f"  - {f}", file=sys.stderr)
            return False

        print("RESULT: PRE-CALL CHECKLIST PASSED. All flags, endpoints, and policies verified.")
        return True
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description="Pre-call checklist for supervised call verification")
    parser.add_argument("--caller", help="Expected caller phone number to verify")
    parser.add_argument("--mongo-uri", help="Optional MongoDB URI override")
    parser.add_argument("--db-name", help="Optional MongoDB database name override")
    parser.add_argument("--expect-ack", action="store_true", help="the acknowledgement is part of this call: ack_enabled must be ON and the clip set must load 'ok'")
    parser.add_argument("--allow-on", action="append", default=[], choices=ALLOWABLE_ON, help="a flag that is allowed to be ON for this call because the operator said so (repeatable)")
    args = parser.parse_args()

    ok = asyncio.run(run_checklist(expected_caller=args.caller, mongo_uri=args.mongo_uri, db_name=args.db_name, expect_ack=args.expect_ack, allow_on=tuple(args.allow_on)))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
