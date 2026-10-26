"""Unit tests for WP1: Key Reference Lookup, Masked Pre-Change Backup, and Rollback Script (SIMULATED).

All tests use simulated in-memory state and a throwaway MongoDB test database.
"""
import os
import pytest
from app.ai.key_refs import mask_key, resolve_service_key
from scripts.rollback_tts_repoint import rollback_tts
from app.persistence.db import Database
from config.settings import Settings


def test_simulated_key_ref_resolution_precedence():
    """SIMULATED: Indirect key_ref from environment takes precedence over legacy keys."""
    os.environ["MME_TEST_TTS_ENV_VAR"] = "test-secret-from-env"

    cfg = {
        "key_refs": {"tts": "MME_TEST_TTS_ENV_VAR"},
        "keys": {"tts": "legacy-direct-key"},
        "tts_api_key": "legacy-single-key"
    }

    # 1. Resolves from env via key_refs
    resolved = resolve_service_key(cfg, "tts")
    assert resolved == "test-secret-from-env"

    # 2. Falls back to keys if key_refs absent
    cfg_no_ref = {"keys": {"tts": "legacy-direct-key"}}
    assert resolve_service_key(cfg_no_ref, "tts") == "legacy-direct-key"

    # 3. Falls back to tts_api_key if keys absent
    cfg_single = {"tts_api_key": "legacy-single-key"}
    assert resolve_service_key(cfg_single, "tts") == "legacy-single-key"

    # 4. None if not configured
    assert resolve_service_key({}, "tts") is None

    # Cleanup
    os.environ.pop("MME_TEST_TTS_ENV_VAR", None)


def test_simulated_mask_key_hygiene():
    """SIMULATED: mask_key only shows last 4 chars."""
    assert mask_key("rpa_1234567890abcdef") == "...cdef"
    assert mask_key(None) == "[none]"
    assert mask_key("abc") == "[masked]"


@pytest.mark.asyncio
async def test_simulated_rollback_script_on_throwaway_database():
    """SIMULATED: Test rollback_tts on a temporary throwaway test database."""
    throwaway_db_name = "mme_test_throwaway_rollback_tmp"
    s = Settings()
    s.mongo_db = throwaway_db_name

    db = Database(s)
    await db.connect()
    tenant = "tenant-throwaway-test"

    try:
        # Seed repointed state
        repointed_doc = {
            "tenant_id": tenant,
            "tts_url": "https://api.runpod.ai/v2/6bmxas8ccwzhrv/runsync",
            "models": {"tts": "Chatterbox Multilingual V3"},
            "key_refs": {"tts": "MME_TEL_KEY_TTS"}
        }
        await db.db["ai_config"].insert_one(repointed_doc)

        # Execute rollback
        ok = await rollback_tts(tenant_id=tenant, db_name=throwaway_db_name)
        assert ok is True

        # Verify restored document
        restored = await db.db["ai_config"].find_one({"tenant_id": tenant})
        assert restored is not None
        assert restored["tts_url"] == "https://api.runpod.ai/v2/93tqgnxb5dyhvx/runsync"
        assert restored["models"]["tts"] == "chatterbox"
        assert "key_refs" not in restored or "tts" not in restored.get("key_refs", {})

    finally:
        # Clean up throwaway database
        await db.client.drop_database(throwaway_db_name)
        await db.close()
