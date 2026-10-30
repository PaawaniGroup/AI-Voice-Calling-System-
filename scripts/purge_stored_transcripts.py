"""Remove conversation text that older builds stored in MongoDB (`ai_sessions.transcript[].heard/reply`, `last_exchange.heard/reply`).

    python scripts/purge_stored_transcripts.py                       # DRY RUN (default): prints counts only, changes nothing, writes nothing
    python scripts/purge_stored_transcripts.py --execute --yes --backup /path/outside/the/repo/transcripts.jsonl

What it does with --execute, in this order, stopping at the first problem:
  1. counts the affected sessions;
  2. BACKS THEM UP FIRST: every affected document, in full, to the --backup file (one JSON per line, permissions 600) and verifies the
     number of lines written equals the number of affected documents; prints its SHA-256;
  3. replaces each stored text by its length (`heard` -> `heard_chars`, `reply` -> `reply_chars`) and keeps the timings;
  4. re-counts: the affected number must be 0.
The backup file CONTAINS THE WORDS: it must live outside the repository (refused otherwise) and should be moved to offline storage and
protected. Nothing here prints any conversation text: counts, the backup path and the checksum only.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bson import json_util  # noqa: E402

from app.core.safe_output import safe_json  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
WORD_FIELDS = ("heard", "reply")


def affected_query(tenant: str | None) -> dict:
    q: dict = {"$or": [{f"transcript.{f}": {"$exists": True}} for f in WORD_FIELDS] + [{f"last_exchange.{f}": {"$exists": True}} for f in WORD_FIELDS]}
    return {"$and": [{"tenant_id": tenant}, q]} if tenant else q


def _clean_entry(e: dict) -> tuple[dict, bool]:
    """The entry without its text, keeping a character count. Returns (entry, changed)."""
    if not isinstance(e, dict) or not any(f in e for f in WORD_FIELDS):
        return e, False
    out = dict(e)
    for f in WORD_FIELDS:
        if f in out:
            text = out.pop(f)
            out.setdefault(f"{f}_chars", len(text) if isinstance(text, str) else 0)
    return out, True


async def scan(db, tenant: str | None) -> dict:
    """Counts only."""
    docs = entries = 0
    async for d in db["ai_sessions"].find(affected_query(tenant), {"transcript": 1, "last_exchange": 1}):
        docs += 1
        entries += sum(1 for e in d.get("transcript") or [] if _clean_entry(e)[1])
    return {"ai_sessions_total": await db["ai_sessions"].count_documents({"tenant_id": tenant} if tenant else {}),
            "sessions_with_stored_words": docs, "turn_entries_with_words": entries}


def check_backup_path(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    if REPO == p or REPO in p.parents:
        raise SystemExit("refusing: the backup contains the words and must be OUTSIDE the repository")
    if p.exists():
        raise SystemExit("refusing: the backup file already exists (never overwrite a backup)")
    if not p.parent.is_dir():
        raise SystemExit("refusing: the backup directory does not exist")
    return p


async def backup(db, tenant: str | None, path: Path) -> dict:
    n, h = 0, hashlib.sha256()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        async for d in db["ai_sessions"].find(affected_query(tenant)):
            line = json_util.dumps(d, ensure_ascii=False) + "\n"
            f.write(line)
            h.update(line.encode("utf-8"))
            n += 1
        f.flush()
        os.fsync(f.fileno())
    return {"documents_written": n, "sha256": h.hexdigest()}


async def purge(db, tenant: str | None) -> dict:
    docs = entries = 0
    async for d in db["ai_sessions"].find(affected_query(tenant)):
        new_tr, changed = [], 0
        for e in d.get("transcript") or []:
            ne, c = _clean_entry(e)
            new_tr.append(ne)
            changed += int(c)
        upd: dict = {}
        if changed:
            upd["transcript"] = new_tr
        le, lc = _clean_entry(d["last_exchange"]) if isinstance(d.get("last_exchange"), dict) else (None, False)
        if lc:
            upd["last_exchange"] = le
        if upd:
            await db["ai_sessions"].update_one({"_id": d["_id"]}, {"$set": upd})
            docs += 1
            entries += changed
    return {"documents_updated": docs, "entries_cleaned": entries}


async def amain(args) -> int:
    from app.persistence.db import Database
    from config.settings import Settings
    db = Database(Settings())
    await db.connect()
    try:
        before = await scan(db, args.tenant)
        if not args.execute:
            print(safe_json({"mode": "dry-run (nothing changed, nothing written)", **before,
                             "next": "to purge: --execute --yes --backup <file outside the repo>"}))
            return 0
        if not (args.yes and args.backup):
            print("refusing: --execute needs --yes and --backup <file outside the repo>")
            return 2
        path = check_backup_path(args.backup)
        if before["sessions_with_stored_words"] == 0:
            print(safe_json({"nothing_to_do": True, **before}))
            return 0
        bk = await backup(db, args.tenant, path)
        if bk["documents_written"] != before["sessions_with_stored_words"]:
            print(safe_json({"ABORTED": "backup count differs from the scan: nothing was purged", "backup": bk, "scan": before}))
            return 1
        done = await purge(db, args.tenant)
        after = await scan(db, args.tenant)
        print(safe_json({"mode": "executed", "backup": {"path_is_outside_repo": True, **bk}, "purged": done, "after": after,
                         "reminder": "the backup holds the words: move it to offline storage and protect it"}))
        return 0 if after["sessions_with_stored_words"] == 0 else 1
    finally:
        await db.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", default=None, help="limit to one tenant (default: all)")
    ap.add_argument("--execute", action="store_true", help="really purge (default is a dry run)")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--backup", help="file (outside the repo, must not exist) that receives the affected documents before anything is changed")
    args = ap.parse_args(argv)
    if args.execute and not (args.yes and args.backup):
        print("refusing: --execute needs --yes and --backup <file outside the repo>")
        return 2
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
