#!/usr/bin/env python3
"""Prepares tenant configuration for supervised call.

Sets ONLY these ai_config fields:
  - tts_ulaw8k, devanagari_hindi, streaming_pipeline, full_duplex_input: True; tts_voice: "utkarsh"   (as before)
  - latency work:  stream_ramp, tts_adaptive_poll, trace_evidence: True
  - quality work:  glossary_correction, strict_grounding: True

ONLY with `--with-ack` (the operator's word): ack_enabled: True (ack_after_ms is left alone: the default is 1500). Before that the script reads the clip directory setting
(the process environment MME_TEL_AI_ACK_DIR through Settings; .env is never opened; the SERVICE reads its own copy at its start) and loads the set through AckSet.load: anything
but "ok" refuses the option and nothing is written (the reason code is printed, never a path content or a phrase).

NOT set (listed in every run with their current values): stt_raw_pcm, faq_grounding, tts_keepalive, keepalive_enabled, rt_transport (all OFF), ack_enabled (unless --with-ack),
and reply_guard, which has NO tenant flag in this tree (reply_guard.py is an unwired file; there is nothing to switch).
Every flag set here is in the allowlist of AiService.get_config (tests/unit/test_tenant_key_scripts.py verifies it): a flag missing from that allowlist would be silently
dropped before it reaches the runtime, which is exactly how key_refs was lost once.

Does NOT set allowed_callers or max_duration_s (those live in tenant policy).
Performs automatic timestamped backup prior to applying.
Default is dry-run (--apply required to execute).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pymongo import AsyncMongoClient
from scripts.backup_ai_config import backup_tenant_ai_config

TENANT_ID = "mme-test"
SUPERVISED_FLAGS = {
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
# Flags that stay as they are: shown, never written. reply_guard has no flag (module not wired).
LISTED_NOT_SET = {"stt_raw_pcm": "default OFF", "faq_grounding": "default OFF (module not in this tree)", "ack_enabled": "default OFF; set only with --with-ack",
                  "tts_keepalive": "must stay OFF (not in this build)", "keepalive_enabled": "must stay OFF (not in this tree)",
                  "rt_transport": "must stay OFF (the persistent transport is not in this build)", "reply_guard": "no tenant flag exists; nothing to set",
                  "stt_adaptive_poll": "default OFF; never written here (the operator turns it on for a call by saying so)",
                  "utterance_cap_raise": "default OFF; never written here (the operator turns it on for a call by saying so)",
                  "glossary_hint": "default OFF; never written here (the operator turns it on for a call by saying so)",
                  "utterance_max_s": "never written here; None = 6.0 s (today); read only with utterance_cap_raise"}
ACK_FLAGS = {"ack_enabled": True}


def ack_clip_status(ack_dir: str | None = None) -> str:
    """The reason code of AckSet.load for the clip directory of THIS process (Settings reads MME_TEL_AI_ACK_DIR from the environment; .env is not opened)."""
    from app.ai.streaming.ack import AckSet
    from config.settings import Settings
    return AckSet.load(ack_dir if ack_dir is not None else Settings(_env_file=None).ai_ack_dir)[1]


async def prepare_supervised_call(apply: bool = False, mongo_uri: str | None = None, db_name: str | None = None, with_ack: bool = False, ack_dir: str | None = None) -> dict:
    flags = dict(SUPERVISED_FLAGS)
    if with_ack:
        why = ack_clip_status(ack_dir)
        print(f"Acknowledgement clip set (this process's environment): {why}")
        if why != "ok":
            print("REFUSED: --with-ack needs a clip set that loads 'ok'; nothing was read from or written to the database.")
            return {"status": "refused", "reason": why}
        flags.update(ACK_FLAGS)
    uri = mongo_uri or "mongodb://127.0.0.1:27117"
    dbname = db_name or "mme_telephony"
    client = AsyncMongoClient(uri)
    db = client[dbname]

    try:
        doc = await db["ai_config"].find_one({"tenant_id": TENANT_ID})
        if not doc:
            raise RuntimeError(f"Tenant '{TENANT_ID}' document not found in ai_config")

        current = {k: doc.get(k) for k in flags}
        print("=" * 65)
        print(f"TENANT SUPERVISED CALL PREPARATION ({'APPLY' if apply else 'DRY-RUN'})")
        print("=" * 65)
        print(f"Tenant:         {TENANT_ID}")
        print(f"Database:       {dbname}")
        print("Target Flags (ai_config ONLY):")
        for k, v in flags.items():
            print(f"  {k:22s}: current={current.get(k)!r} -> proposed={v!r}")
        print("Listed, NOT set:")
        for k, reason in LISTED_NOT_SET.items():
            if k not in flags:
                print(f"  {k:22s}: current={doc.get(k)!r}  ({reason})")
        print(f"  ack_after_ms          : current={doc.get('ack_after_ms')!r}  (never written here; None = the built-in 1500 ms)")

        if not apply:
            print("\n[DRY-RUN] No database modifications made.")
            print("Run with --apply to execute.")
            return {"status": "dry_run", "current": current, "proposed": flags}

        # 1. Automatic pre-change backup
        backup_path = await backup_tenant_ai_config(TENANT_ID, mongo_uri=uri, db_name=dbname)
        print(f"\nPre-change backup saved to:\n  {backup_path}")

        # 2. Update tenant document
        res = await db["ai_config"].update_one(
            {"tenant_id": TENANT_ID},
            {"$set": flags}
        )
        print(f"\nApplied successfully: {res.modified_count} document modified.")
        return {"status": "applied", "backup": str(backup_path), "proposed": flags}
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description="Prepare tenant ai_config for supervised call (Dry-run by default)")
    parser.add_argument("--apply", action="store_true", help="Apply the configuration (default is dry-run)")
    parser.add_argument("--mongo-uri", help="Optional MongoDB URI override")
    parser.add_argument("--db-name", help="Optional MongoDB database name override")
    parser.add_argument("--with-ack", action="store_true", help="ALSO set ack_enabled (only when the operator says so; needs a clip set that loads 'ok' from MME_TEL_AI_ACK_DIR)")
    args = parser.parse_args()

    asyncio.run(prepare_supervised_call(apply=args.apply, mongo_uri=args.mongo_uri, db_name=args.db_name, with_ack=args.with_ack))


if __name__ == "__main__":
    main()
