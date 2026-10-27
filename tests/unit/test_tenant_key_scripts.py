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
            "stt": "rpa_FAKE_STT_SEC_SECRET_TESTKEY_9876",
            "brain": "rpa_FAKE_BRAIN_SEC_SECRET_TESTKEY_5432",
            "tts": "rpa_FAKE_TTS_SEC_SECRET_TESTKEY_1098"
        },
        "stt_api_key": "rpa_FAKE_STT_SEC_SECRET_TESTKEY_9876",
        "brain_api_key": "rpa_FAKE_BRAIN_SEC_SECRET_TESTKEY_5432",
        "tts_api_key": "rpa_FAKE_TTS_SEC_SECRET_TESTKEY_1098"
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


# ====================================================================== supervised call: the extended flag set (S5)
NEW_FLAGS = ("stream_ramp", "tts_adaptive_poll", "trace_evidence", "glossary_correction", "strict_grounding")
NOT_SET = ("stt_raw_pcm", "faq_grounding", "ack_enabled", "keepalive_enabled", "reply_guard")
EP_URLS = {"stt_url": "https://api.runpod.ai/v2/ua9l3gus6qjnmg/runsync", "brain_url": "https://api.runpod.ai/v2/hu8qhrvlxbv314/runsync",
           "tts_url": "https://api.runpod.ai/v2/6bmxas8ccwzhrv/runsync"}


def _db_with(colls: dict):
    mock_db = MagicMock()
    mock_db.__getitem__.side_effect = lambda name: colls[name]
    return mock_db


def _coll(doc):
    c = MagicMock()
    c.find_one = AsyncMock(return_value=doc)
    c.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    return c


def test_the_prepare_script_sets_the_five_new_flags_and_never_the_ones_that_stay_off():
    from scripts.prepare_supervised_call_tenant import LISTED_NOT_SET, SUPERVISED_FLAGS
    for f in NEW_FLAGS:
        assert SUPERVISED_FLAGS[f] is True
    for f in NOT_SET:
        assert f not in SUPERVISED_FLAGS and f in LISTED_NOT_SET             # listed, never written
    assert "no tenant flag" in LISTED_NOT_SET["reply_guard"]


@pytest.mark.asyncio
async def test_every_flag_the_prepare_script_sets_survives_get_config_into_the_runtime_config():
    """The dropped key_refs bug was an allowlist: every field the script writes must come back from AiService.get_config."""
    from app.ai.service import AiService
    from config.settings import Settings
    from scripts.prepare_supervised_call_tenant import SUPERVISED_FLAGS
    doc = {"tenant_id": "mme-test", **SUPERVISED_FLAGS}
    svc = AiService(_db_with({"ai_config": _coll(dict(doc))}), None, Settings(_env_file=None), None)
    cfg = await svc.get_config("mme-test")
    missing = [k for k, v in SUPERVISED_FLAGS.items() if cfg.get(k) != v]
    assert missing == [], f"dropped or changed by get_config: {missing}"
    assert all(cfg[k] is False for k in NOT_SET if k in cfg)                       # whatever of the OFF set exists defaults to False


@pytest.mark.asyncio
async def test_the_defaults_of_the_five_new_flags_are_off_without_a_document():
    from app.ai.service import AiService
    from config.settings import Settings
    svc = AiService(_db_with({"ai_config": _coll(None)}), None, Settings(_env_file=None), None)
    cfg = await svc.get_config("t")
    assert all(cfg[f] is False for f in NEW_FLAGS) and cfg["stt_raw_pcm"] is False


@pytest.mark.asyncio
async def test_rollback_restores_the_previous_value_of_a_new_flag_and_unsets_the_ones_that_did_not_exist(tmp_path):
    bf = tmp_path / "ai_config_mme-test_20261027T000000Z.json"
    bf.write_text(json.dumps({"tenant_id": "mme-test", "config": {"stream_ramp": False, "devanagari_hindi": True}}), encoding="utf-8")
    coll = _coll(None)
    with patch("scripts.rollback_supervised_call_tenant.AsyncMongoClient") as mc:
        mc.return_value = MagicMock()
        mc.return_value.__getitem__.return_value = _db_with({"ai_config": coll})
        mc.return_value.close = AsyncMock()
        await rollback_supervised_call(backup_file=bf)
    op = coll.update_one.call_args[0][1]
    assert op["$set"] == {"stream_ramp": False, "devanagari_hindi": True}
    for f in ("tts_adaptive_poll", "trace_evidence", "glossary_correction", "strict_grounding"):
        assert f in op["$unset"]


async def _mongo_or_skip(uri):
    from pymongo import AsyncMongoClient
    c = AsyncMongoClient(uri, serverSelectionTimeoutMS=1500)
    try:
        await c.admin.command("ping")
    except Exception as e:
        await c.close()
        pytest.skip(f"test MongoDB not reachable: {type(e).__name__}")
    return c


@pytest.mark.asyncio
async def test_prepare_then_rollback_on_a_throwaway_database_restores_the_previous_state(tmp_path):
    """REAL pymongo against a THROWAWAY database (dropped afterwards); the backup goes to tmp_path, outside the repository."""
    import uuid
    from scripts.prepare_supervised_call_tenant import SUPERVISED_FLAGS, prepare_supervised_call
    uri = "mongodb://127.0.0.1:27117"
    c = await _mongo_or_skip(uri)
    name = f"mme_throwaway_{uuid.uuid4().hex[:10]}"
    try:
        coll = c[name]["ai_config"]
        await coll.insert_one({"tenant_id": "mme-test", "enabled": True, "stream_ramp": False, "tts_voice": "default", "devanagari_hindi": True,
                               "key_refs": {"stt": "RUNPOD_STT_KEY"}})
        with patch("scripts.backup_ai_config.get_default_backup_dir", return_value=tmp_path):
            res = await prepare_supervised_call(apply=True, mongo_uri=uri, db_name=name)
        assert res["status"] == "applied"
        now = await coll.find_one({"tenant_id": "mme-test"})
        assert all(now.get(k) == v for k, v in SUPERVISED_FLAGS.items())
        assert now["key_refs"] == {"stt": "RUNPOD_STT_KEY"} and now["enabled"] is True            # nothing else was touched
        await rollback_supervised_call(backup_file=Path(res["backup"]), mongo_uri=uri, db_name=name)
        back = await coll.find_one({"tenant_id": "mme-test"})
        assert back["stream_ramp"] is False and back["tts_voice"] == "default" and back["devanagari_hindi"] is True      # previous values restored
        for f in ("tts_adaptive_poll", "trace_evidence", "glossary_correction", "strict_grounding", "tts_ulaw8k", "streaming_pipeline", "full_duplex_input"):
            assert f not in back, f                                                                       # fields that did not exist are gone
        assert back["key_refs"] == {"stt": "RUNPOD_STT_KEY"} and back["enabled"] is True
    finally:
        await c.drop_database(name)
        await c.close()


def _checklist_docs(**ai_over):
    flags = {"tts_ulaw8k": True, "devanagari_hindi": True, "streaming_pipeline": True, "full_duplex_input": True, "tts_voice": "utkarsh",
             **{f: True for f in NEW_FLAGS}}
    ai = {"tenant_id": "mme-test", **EP_URLS, **flags, "key_refs": {"stt": "A", "brain": "B", "tts": "C"}, **ai_over}
    pol = {"tenant_id": "mme-test", "enabled": True, "allowed_callers": ["+000000000000"], "max_duration_s": 180}
    return ai, pol


async def _run_checklist(ai, pol, capsys, patch_get_config=None):
    from scripts import pre_call_checklist as pc
    db = _db_with({"ai_config": _coll(ai), "tenant_policies": _coll(pol)})
    with patch("scripts.pre_call_checklist.AsyncMongoClient") as mc:
        mc.return_value = MagicMock()
        mc.return_value.__getitem__.return_value = db
        mc.return_value.close = AsyncMock()
        if patch_get_config is not None:
            with patch("app.ai.service.AiService.get_config", new=patch_get_config):
                ok = await pc.run_checklist()
        else:
            ok = await pc.run_checklist()
    return ok, capsys.readouterr()


@pytest.mark.asyncio
async def test_checklist_passes_reads_flags_through_get_config_and_prints_a_count_but_no_number(capsys):
    ai, pol = _checklist_docs()
    ok, out = await _run_checklist(ai, pol, capsys)
    assert ok is True
    assert "via get_config" in out.out and "COUNT=1" in out.out and "policy state: ENABLED" in out.out
    assert "+0000" not in out.out + out.err and "000000000000" not in out.out + out.err


@pytest.mark.asyncio
async def test_checklist_fails_when_get_config_drops_a_stored_flag(capsys):
    from app.ai.service import AiService
    from config.settings import Settings
    ai, pol = _checklist_docs()
    real = AiService.get_config

    async def dropping(self, tenant_id):
        cfg = await real(self, tenant_id)
        cfg.pop("strict_grounding")                         # an allowlist that forgot a flag
        return cfg
    ok, out = await _run_checklist(ai, pol, capsys, patch_get_config=dropping)
    assert ok is False and "strict_grounding" in out.err


@pytest.mark.asyncio
async def test_checklist_fails_when_a_flag_that_must_stay_off_is_on(capsys):
    ai, pol = _checklist_docs(stt_raw_pcm=True)
    ok, out = await _run_checklist(ai, pol, capsys)
    assert ok is False and "stt_raw_pcm" in out.err


@pytest.mark.asyncio
@pytest.mark.parametrize("flag,bad", [("tts_ulaw8k", False), ("devanagari_hindi", False), ("streaming_pipeline", False), ("full_duplex_input", False),
                                      ("tts_voice", "other"), ("stream_ramp", False), ("tts_adaptive_poll", False), ("trace_evidence", False),
                                      ("glossary_correction", False), ("strict_grounding", False)])
async def test_checklist_requires_each_base_flag_and_each_new_flag_to_be_on(flag, bad, capsys):
    """The base flags the call needs (streaming_pipeline, tts_ulaw8k, devanagari_hindi, full_duplex_input, tts_voice) and the five new ones: one wrong value fails."""
    ai, pol = _checklist_docs(**{flag: bad})
    ok, out = await _run_checklist(ai, pol, capsys)
    assert ok is False and flag in out.err


@pytest.mark.asyncio
async def test_checklist_fails_on_flag_dependencies(capsys):
    ai, pol = _checklist_docs(streaming_pipeline=False)                            # stream_ramp stays True
    ok, out = await _run_checklist(ai, pol, capsys)
    assert ok is False and "stream_ramp needs streaming_pipeline" in out.err
    ai, pol = _checklist_docs(runpod_mode="runsync")                               # tts_adaptive_poll stays True
    ok, out = await _run_checklist(ai, pol, capsys)
    assert ok is False and "tts_adaptive_poll needs runpod_mode run" in out.err


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,expected", [({"enabled": True}, True), ({"enabled": False}, False), (None, None)])
async def test_rollback_reports_whether_the_tenant_is_still_armed_and_never_changes_the_policy(tmp_path, policy, expected):
    bf = tmp_path / "ai_config_mme-test_20261027T000000Z.json"
    bf.write_text(json.dumps({"tenant_id": "mme-test", "config": {}}), encoding="utf-8")
    ai, pol = _coll(None), _coll(policy)
    with patch("scripts.rollback_supervised_call_tenant.AsyncMongoClient") as mc:
        mc.return_value = MagicMock()
        mc.return_value.__getitem__.return_value = _db_with({"ai_config": ai, "tenant_policies": pol})
        mc.return_value.close = AsyncMock()
        res = await rollback_supervised_call(backup_file=bf)
    assert res["policy_enabled"] is expected
    pol.update_one.assert_not_called()                                              # the rollback only READS the policy


@pytest.mark.asyncio
async def test_disarm_then_rollback_on_a_throwaway_database_leaves_the_tenant_disarmed_and_the_flags_gone(tmp_path):
    """The S11 order: disarm (plivo_testcall) -> flag rollback -> verify. Real pymongo, throwaway database (dropped)."""
    import uuid
    from scripts.plivo_testcall import disarm
    from scripts.prepare_supervised_call_tenant import prepare_supervised_call
    uri = "mongodb://127.0.0.1:27117"
    c = await _mongo_or_skip(uri)
    name = f"mme_throwaway_{uuid.uuid4().hex[:10]}"
    try:
        db = c[name]
        await db["ai_config"].insert_one({"tenant_id": "mme-test", "enabled": True, "tts_voice": "default"})
        await db["tenant_policies"].insert_one({"tenant_id": "mme-test", "enabled": True, "allowed_callers": ["+000000000000"], "max_duration_s": 180})
        with patch("scripts.backup_ai_config.get_default_backup_dir", return_value=tmp_path):
            res = await prepare_supervised_call(apply=True, mongo_uri=uri, db_name=name)

        async def hangup(call):
            return None
        # rollback WITHOUT disarming first: flags are restored, but the script says the tenant is still armed
        early = await rollback_supervised_call(backup_file=Path(res["backup"]), mongo_uri=uri, db_name=name)
        assert early["policy_enabled"] is True
        with patch("scripts.backup_ai_config.get_default_backup_dir", return_value=tmp_path):
            await prepare_supervised_call(apply=True, mongo_uri=uri, db_name=name)    # flags on again, then the real order (backup stays in tmp_path)
        await disarm(db, "mme-test", hangup)
        done = await rollback_supervised_call(backup_file=Path(res["backup"]), mongo_uri=uri, db_name=name)
        assert done["policy_enabled"] is False
        pol = await db["tenant_policies"].find_one({"tenant_id": "mme-test"})
        assert pol["enabled"] is False
        cfg = await db["ai_config"].find_one({"tenant_id": "mme-test"})
        for f in ("stream_ramp", "tts_adaptive_poll", "trace_evidence", "glossary_correction", "strict_grounding", "streaming_pipeline", "tts_ulaw8k"):
            assert f not in cfg, f
        assert cfg["tts_voice"] == "default" and cfg["enabled"] is True
    finally:
        await c.drop_database(name)
        await c.close()
