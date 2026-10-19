"""Create a dashboard user (bootstrap the first admin).

Usage:  MME_TEL_MONGO_URI=... MME_TEL_MONGO_DB=... python scripts/create_user.py <username> <tenant_id> <viewer|operator|admin>
The password is read from the MME_TEL_NEW_USER_PASSWORD environment variable or an interactive hidden prompt;
it is never accepted as a command-line argument (it would appear in `ps` and shell history) and never printed.
"""
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.persistence.db import Database  # noqa: E402
from app.security.users import create_user  # noqa: E402
from config.settings import Settings  # noqa: E402


async def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    username, tenant, role = sys.argv[1:4]
    pw = os.environ.get("MME_TEL_NEW_USER_PASSWORD") or getpass.getpass("Password (min 12 chars): ")
    db = Database(Settings())
    await db.connect()
    try:
        u = await create_user(db, username, pw, tenant, role)
        print(f"created user '{u['username']}' tenant={tenant} role={role}")
        return 0
    except Exception as e:
        print(f"failed: {getattr(e, 'code', type(e).__name__)}: {getattr(e, 'message', e)}")
        return 1
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
