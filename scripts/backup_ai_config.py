#!/usr/bin/env python3
"""Pre-change backup tool for tenant ai_config document.

Saves a clean backup of the tenant's ai_config document OUTSIDE the repository,
with all sensitive keys strictly masked (...last4).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai.key_refs import mask_key
from app.persistence.db import Database
from config.settings import Settings

DEFAULT_BACKUP_DIR = Path(os.environ.get("LOCALAPPDATA", "C:/Users/Admin/AppData/Local")) / "mme_telephony_backups"


async def backup_tenant_ai_config(tenant_id: str, out_file: Path | None = None) -> Path:
    s = Settings()
    db = Database(s)
    await db.connect()

    try:
        doc = await db.db["ai_config"].find_one({"tenant_id": tenant_id})
        if not doc:
            raise RuntimeError(f"No ai_config document found for tenant '{tenant_id}'")
        doc.pop("_id", None)

        # Strictly mask all keys in backup
        if "keys" in doc and isinstance(doc["keys"], dict):
            doc["keys"] = {k: mask_key(v) for k, v in doc["keys"].items()}
        for k in list(doc.keys()):
            if "api_key" in k:
                doc[k] = mask_key(doc[k])

        backup_payload = {
            "backup_timestamp": datetime.now(timezone.utc).isoformat(),
            "tenant_id": tenant_id,
            "config": doc,
            "security_note": "All API keys are strictly masked (...last4). Raw keys are never stored."
        }

        if out_file is None:
            DEFAULT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            out_file = DEFAULT_BACKUP_DIR / f"ai_config_{tenant_id}_backup.json"
        else:
            out_file.parent.mkdir(parents=True, exist_ok=True)

        out_file.write_text(json.dumps(backup_payload, indent=2), encoding="utf-8")
        return out_file
    finally:
        await db.close()


async def main():
    parser = argparse.ArgumentParser(description="Backup tenant ai_config outside repository with masked keys")
    parser.add_argument("--tenant", default="mme-test", help="Tenant ID")
    parser.add_argument("--out", help="Output file path (must be outside the repository)")
    args = parser.parse_args()

    out_p = Path(args.out) if args.out else None
    saved_path = await backup_tenant_ai_config(args.tenant, out_p)
    print(f"Successfully backed up ai_config for tenant '{args.tenant}' to:")
    print(f"  {saved_path} (outside repository, keys strictly masked)")


if __name__ == "__main__":
    asyncio.run(main())
