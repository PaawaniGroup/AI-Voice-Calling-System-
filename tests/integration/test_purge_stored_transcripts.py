"""scripts/purge_stored_transcripts.py: dry run prints counts only and changes nothing; the backup is written first (outside the repo,
mode 600, never overwritten); the purge replaces text by lengths, keeps timings, is idempotent and tenant-scoped.
Runs against the TEST database only."""
import importlib.util
import json
import stat
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("purge", Path(__file__).resolve().parents[2] / "scripts" / "purge_stored_transcripts.py")
pg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pg)

W1, W2, W3, W4 = "CALLER-ALPHA-1", "ASSIST-BRAVO-1", "CALLER-ALPHA-2", "ASSIST-BRAVO-2"


async def _seed(db):
    old = {"session_id": "s_old", "tenant_id": "t1", "turns": 2, "timings_ms": {"turn_ms": 1.0},
           "transcript": [{"turn": 1, "heard": W1, "reply": W2, "timings_ms": {"stt": 100.0}, "ts": "x"},
                          {"turn": 2, "heard": W3, "reply": W4, "timings_ms": {"stt": 110.0}, "ts": "y"}],
           "last_exchange": {"turn": 2, "heard": W3, "reply": W4, "timings_ms": {"stt": 110.0}}}
    clean = {"session_id": "s_clean", "tenant_id": "t1", "turns": 1,
             "transcript": [{"turn": 1, "heard_chars": 5, "reply_chars": 7, "timings_ms": {"stt": 90.0}}], "last_exchange": {"turn": 1, "heard_chars": 5}}
    other = {"session_id": "s_other", "tenant_id": "t2", "turns": 1, "transcript": [{"turn": 1, "heard": W1, "reply": W2, "timings_ms": {}}]}
    await db["ai_sessions"].insert_many([old, clean, other])


async def test_dry_run_scan_gives_counts_only_and_changes_nothing(db, tmp_path):
    await _seed(db)
    before = [d async for d in db["ai_sessions"].find({}, {"_id": 0}).sort("session_id")]
    rep = await pg.scan(db, None)
    assert rep == {"ai_sessions_total": 3, "sessions_with_stored_words": 2, "turn_entries_with_words": 3}
    assert (await pg.scan(db, "t1"))["sessions_with_stored_words"] == 1
    assert not any(w in json.dumps(rep) for w in (W1, W2, W3, W4))
    assert [d async for d in db["ai_sessions"].find({}, {"_id": 0}).sort("session_id")] == before   # nothing changed
    assert list(tmp_path.iterdir()) == []                                                          # nothing written


def test_backup_path_rules(tmp_path):
    with pytest.raises(SystemExit):
        pg.check_backup_path(str(pg.REPO / "transcripts.jsonl"))              # inside the repo: refused
    with pytest.raises(SystemExit):
        pg.check_backup_path(str(tmp_path / "missing_dir" / "b.jsonl"))       # directory must exist
    existing = tmp_path / "b.jsonl"
    existing.write_text("x")
    with pytest.raises(SystemExit):
        pg.check_backup_path(str(existing))                                   # never overwrite a backup
    assert pg.check_backup_path(str(tmp_path / "new.jsonl")) == (tmp_path / "new.jsonl").resolve()


async def test_backup_is_written_first_private_complete_and_checksummed(db, tmp_path):
    await _seed(db)
    p = tmp_path / "bk.jsonl"
    res = await pg.backup(db, None, p)
    lines = p.read_text(encoding="utf-8").splitlines()
    assert res["documents_written"] == 2 == len(lines) and len(res["sha256"]) == 64
    assert stat.S_IMODE(p.stat().st_mode) == 0o600                            # owner only
    body = "\n".join(lines)
    assert W1 in body and W4 in body and "s_clean" not in body                # the backup has the ORIGINAL words, only for affected sessions


async def test_purge_replaces_text_by_lengths_keeps_timings_and_is_idempotent_and_tenant_scoped(db):
    await _seed(db)
    done = await pg.purge(db, "t1")
    assert done == {"documents_updated": 1, "entries_cleaned": 2}
    old = await db["ai_sessions"].find_one({"session_id": "s_old"})
    assert not any(w in json.dumps(old, default=str) for w in (W1, W2, W3, W4))
    e = old["transcript"][0]
    assert e["heard_chars"] == len(W1) and e["reply_chars"] == len(W2) and e["timings_ms"] == {"stt": 100.0} and e["turn"] == 1
    assert old["last_exchange"]["reply_chars"] == len(W4) and old["last_exchange"]["timings_ms"] == {"stt": 110.0} and old["timings_ms"] == {"turn_ms": 1.0}
    assert (await db["ai_sessions"].find_one({"session_id": "s_clean"}))["transcript"][0]["heard_chars"] == 5     # untouched
    assert W1 in json.dumps(await db["ai_sessions"].find_one({"session_id": "s_other"}), default=str)           # other tenant untouched
    assert await pg.purge(db, "t1") == {"documents_updated": 0, "entries_cleaned": 0}                          # idempotent
    assert (await pg.scan(db, "t1"))["sessions_with_stored_words"] == 0
    assert (await pg.scan(db, None))["sessions_with_stored_words"] == 1                                         # only t2 remains


def test_cli_refuses_to_execute_without_yes_and_a_backup(capsys):
    assert pg.main(["--execute"]) == 2
    assert pg.main(["--execute", "--yes"]) == 2
    assert pg.main(["--execute", "--backup", "/tmp/x.jsonl"]) == 2
    assert "refusing" in capsys.readouterr().out
