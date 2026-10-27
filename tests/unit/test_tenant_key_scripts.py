"""Unit tests for tenant key scripts: backup, apply, rollback, strip, and supervised call."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from scripts.backup_ai_config import backup_tenant_ai_config, get_default_backup_dir, validate_backup_path, ROOT
from scripts.apply_tenant_key_refs import apply_key_refs, STANDARD_KEY_REFS
from scripts.rollback_tenant_key_refs import rollback_key_refs
from scripts.strip_tenant_plaintext_keys import strip_plaintext_keys
from scripts.rollback_supervised_call_tenant import rollback_supervised_call


@pytest.fixture
def sample_tenant_doc():
    return {
        "tenant_id": "test-tenant-1",
        "enabled": True,
        "keys": {
            "stt": "rpa_STT_SEC_SECRET_TESTKEY_9876",
            "brain": "rpa_BRAIN_SEC_SECRET_TESTKEY_5432",
            "tts": "rpa_TTS_SEC_SECRET_TESTKEY_1098"
        },
        "stt_api_key": "rpa_STT_SEC_SECRET_TESTKEY_9876",
        "brain_api_key": "rpa_BRAIN_SEC_SECRET_TESTKEY_5432",
        "tts_api_key": "rpa_TTS_SEC_SECRET_TESTKEY_1098"
    }


def test_default_backup_dir_is_outside_repo():
    bdir = get_default_backup_dir()
    assert bdir.is_absolute()
    # Must not be inside ROOT repo
    with pytest.raises(ValueError):
        bdir.resolve().relative_to(ROOT.resolve())


def test_backup_path_refuses_inside_repo():
    in_repo = ROOT / "scripts" / "test_backup.json"
    with pytest.raises(ValueError, match="SAFETY ERROR"):
        validate_backup_path(in_repo)


@pytest.mark.asyncio
async def test_backup_creates_timestamped_file_with_masked_keys(tmp_path, sample_tenant_doc):
    mock_db = MagicMock()
    mock_coll = MagicMock()
    mock_coll.find_one = AsyncMock(return_value=dict(sample_tenant_doc))
    mock_db.__getitem__.return_value = mock_coll

    with patch("scripts.backup_ai_config.AsyncMongoClient") as mock_client, \
         patch("scripts.backup_ai_config.get_default_backup_dir", return_value=tmp_path):
        mock_client.return_value = MagicMock()
        mock_client.return_value.__getitem__.return_value = mock_db
        mock_client.return_value.close = AsyncMock()

        out_path = await backup_tenant_ai_config("test-tenant-1")

        assert out_path.exists()
        assert out_path.name.startswith("ai_config_test-tenant-1_")
        assert out_path.suffix == ".json"

        content = json.loads(out_path.read_text(encoding="utf-8"))
        assert content["tenant_id"] == "test-tenant-1"
        assert "backup_timestamp" in content

        cfg = content["config"]
        # Keys must be strictly masked
        for service, masked in cfg["keys"].items():
            assert masked.startswith("...")
            assert "SECRET" not in masked

        assert cfg["stt_api_key"].startswith("...")
        assert "SECRET" not in cfg["stt_api_key"]


@pytest.mark.asyncio
async def test_apply_key_refs_dry_run_and_apply(tmp_path, sample_tenant_doc):
    mock_db = MagicMock()
    mock_coll = MagicMock()
    mock_coll.find_one = AsyncMock(return_value=dict(sample_tenant_doc))
    mock_coll.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    mock_db.__getitem__.return_value = mock_coll

    with patch("scripts.apply_tenant_key_refs.AsyncMongoClient") as mock_client, \
         patch("scripts.apply_tenant_key_refs.backup_tenant_ai_config", new=AsyncMock(return_value=tmp_path / "b.json")):
        mock_client.return_value = MagicMock()
        mock_client.return_value.__getitem__.return_value = mock_db
        mock_client.return_value.close = AsyncMock()

        # 1. Dry run
        dry = await apply_key_refs("test-tenant-1", apply=False)
        assert dry["status"] == "dry_run"
        mock_coll.update_one.assert_not_called()

        # 2. Apply
        applied = await apply_key_refs("test-tenant-1", apply=True)
        assert applied["status"] == "applied"
        assert applied["modified_count"] == 1
        mock_coll.update_one.assert_called_once()
        args, kwargs = mock_coll.update_one.call_args
        assert args[1]["$set"]["key_refs"] == STANDARD_KEY_REFS


@pytest.mark.asyncio
async def test_rollback_key_refs(sample_tenant_doc):
    doc = dict(sample_tenant_doc)
    doc["key_refs"] = STANDARD_KEY_REFS

    mock_db = MagicMock()
    mock_coll = MagicMock()
    mock_coll.find_one = AsyncMock(return_value=doc)
    mock_coll.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    mock_db.__getitem__.return_value = mock_coll

    with patch("scripts.rollback_tenant_key_refs.AsyncMongoClient") as mock_client:
        mock_client.return_value = MagicMock()
        mock_client.return_value.__getitem__.return_value = mock_db
        mock_client.return_value.close = AsyncMock()

        res = await rollback_key_refs("test-tenant-1", apply=True)
        assert res["status"] == "rolled_back"
        args, _ = mock_coll.update_one.call_args
        assert "$unset" in args[1]
        assert "key_refs" in args[1]["$unset"]


@pytest.mark.asyncio
async def test_strip_plaintext_keys_refuses_without_key_refs(sample_tenant_doc):
    mock_db = MagicMock()
    mock_coll = MagicMock()
    mock_coll.find_one = AsyncMock(return_value=dict(sample_tenant_doc))
    mock_db.__getitem__.return_value = mock_coll

    with patch("scripts.strip_tenant_plaintext_keys.AsyncMongoClient") as mock_client:
        mock_client.return_value = MagicMock()
        mock_client.return_value.__getitem__.return_value = mock_db
        mock_client.return_value.close = AsyncMock()

        with pytest.raises(RuntimeError, match="SAFETY REFUSAL"):
            await strip_plaintext_keys("test-tenant-1", apply=True)


@pytest.mark.asyncio
async def test_strip_plaintext_keys_unsets_only_when_key_refs_present(tmp_path, sample_tenant_doc):
    doc = dict(sample_tenant_doc)
    doc["key_refs"] = STANDARD_KEY_REFS

    mock_db = MagicMock()
    mock_coll = MagicMock()
    mock_coll.find_one = AsyncMock(return_value=doc)
    mock_coll.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    mock_db.__getitem__.return_value = mock_coll

    with patch("scripts.strip_tenant_plaintext_keys.AsyncMongoClient") as mock_client, \
         patch("scripts.strip_tenant_plaintext_keys.backup_tenant_ai_config", new=AsyncMock(return_value=tmp_path / "b.json")):
        mock_client.return_value = MagicMock()
        mock_client.return_value.__getitem__.return_value = mock_db
        mock_client.return_value.close = AsyncMock()

        res = await strip_plaintext_keys("test-tenant-1", apply=True)
        assert res["status"] == "stripped"
        args, _ = mock_coll.update_one.call_args
        assert "keys" in args[1]["$unset"]
        assert "stt_api_key" in args[1]["$unset"]


@pytest.mark.asyncio
async def test_rollback_supervised_call_restores_previous(tmp_path):
    backup_file = tmp_path / "ai_config_mme-test_20261026T000000Z.json"
    backup_file.write_text(json.dumps({
        "tenant_id": "mme-test",
        "config": {
            "tts_ulaw8k": False,
            "tts_voice": "default"
        }
    }), encoding="utf-8")

    mock_db = MagicMock()
    mock_coll = MagicMock()
    mock_coll.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    mock_db.__getitem__.return_value = mock_coll

    with patch("scripts.rollback_supervised_call_tenant.AsyncMongoClient") as mock_client:
        mock_client.return_value = MagicMock()
        mock_client.return_value.__getitem__.return_value = mock_db
        mock_client.return_value.close = AsyncMock()

        res = await rollback_supervised_call(backup_file=backup_file)
        assert res["status"] == "rolled_back"
        args, _ = mock_coll.update_one.call_args
        op = args[1]
        assert op["$set"]["tts_ulaw8k"] is False
        assert op["$set"]["tts_voice"] == "default"
        assert "devanagari_hindi" in op["$unset"]
        assert "streaming_pipeline" in op["$unset"]
        assert "full_duplex_input" in op["$unset"]


@pytest.mark.asyncio
async def test_ai_service_get_config_includes_key_refs():
    from app.ai.service import AiService
    from config.settings import Settings

    s = Settings()
    mock_db = MagicMock()
    mock_coll = MagicMock()
    mock_coll.find_one = AsyncMock(return_value={
        "tenant_id": "mme-test",
        "key_refs": {
            "stt": "RUNPOD_STT_KEY",
            "brain": "RUNPOD_BRAIN_KEY",
            "tts": "RUNPOD_TTS_KEY"
        }
    })
    mock_db.__getitem__.return_value = mock_coll

    svc = AiService(mock_db, None, s, None)
    cfg = await svc.get_config("mme-test")
    assert "key_refs" in cfg
    assert cfg["key_refs"]["tts"] == "RUNPOD_TTS_KEY"
