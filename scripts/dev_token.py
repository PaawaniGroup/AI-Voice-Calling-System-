"""Mint a DEVELOPMENT JWT for manual testing. Reads MME_TEL_JWT_SECRET from the environment.
Usage: python scripts/dev_token.py <tenant_id> <role[,role...]> [ttl_seconds]
Refuses to run when MME_TEL_ENV is production/staging."""
import os
import sys
import time

import jwt

if os.environ.get("MME_TEL_ENV", "development") in ("production", "staging"):
    sys.exit("refusing to mint tokens in a production-like environment")
secret = os.environ.get("MME_TEL_JWT_SECRET", "")
if not secret:
    sys.exit("MME_TEL_JWT_SECRET is not set")
tenant, roles = sys.argv[1], sys.argv[2].split(",")
ttl = int(sys.argv[3]) if len(sys.argv) > 3 else 3600
print(jwt.encode({"sub": "dev-user", "tenant_id": tenant, "roles": roles, "exp": int(time.time()) + ttl}, secret, "HS256"))
