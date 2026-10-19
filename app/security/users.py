"""Local dashboard users (MongoDB `users`), scrypt password hashes, token issuing.

Passwords are never logged or returned. Hash format: scrypt$N$r$p$salt_b64$hash_b64 (stdlib only).
Login failures are indistinguishable between unknown user and wrong password and always cost one hash.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import time

import jwt

from app.core.errors import Unauthorized, Unprocessable
from app.core.ids import utcnow
from config.settings import Settings

_N, _R, _P = 2**14, 8, 1
_USERNAME = re.compile(r"^[a-z0-9._@-]{3,64}$")
ROLE_NAMES = {"viewer", "operator", "admin"}


def hash_password(password: str) -> str:
    if len(password) < 12 or len(password) > 256:
        raise Unprocessable("password must be 12-256 characters", code="weak_password")
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str | None) -> bool:
    # Always perform one scrypt so timing does not reveal whether the user exists.
    if not stored or not stored.startswith("scrypt$"):
        hashlib.scrypt(password.encode(), salt=b"\0" * 16, n=_N, r=_R, p=_P, dklen=32)
        return False
    try:
        _, n, r, p, salt, dk = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def roles_for(s: Settings, role: str) -> list[str]:
    return {"viewer": [s.role_viewer], "operator": [s.role_user, s.role_viewer],
            "admin": [s.role_admin, s.role_user, s.role_viewer]}[role]


def issue_token(s: Settings, user: dict) -> tuple[str, int]:
    now = int(time.time())
    exp = now + s.jwt_ttl_s
    payload = {"sub": user["username"], "tenant_id": user["tenant_id"], "roles": roles_for(s, user["role"]),
               s.jwt_tenant_claim: user["tenant_id"], s.jwt_roles_claim: roles_for(s, user["role"]),
               "iat": now, "exp": exp, "typ": "dashboard"}
    if s.jwt_issuer:
        payload["iss"] = s.jwt_issuer
    if s.jwt_audience:
        payload["aud"] = s.jwt_audience
    return jwt.encode(payload, s.jwt_secret.get_secret_value(), algorithm=s.jwt_algorithm), exp


def valid_username(u: str) -> str:
    u = (u or "").strip().lower()
    if not _USERNAME.match(u):
        raise Unprocessable("username must be 3-64 chars of a-z 0-9 . _ @ -", code="invalid_username")
    return u


async def create_user(db, username: str, password: str, tenant_id: str, role: str) -> dict:
    from pymongo.errors import DuplicateKeyError

    from app.core.errors import Conflict
    if role not in ROLE_NAMES:
        raise Unprocessable("role must be viewer, operator or admin", code="invalid_role")
    doc = {"username": valid_username(username), "password_hash": hash_password(password), "tenant_id": tenant_id,
           "role": role, "disabled": False, "created_at": utcnow(), "last_login_at": None}
    try:
        await db["users"].insert_one(doc)
    except DuplicateKeyError:
        raise Conflict("username already exists", code="user_exists") from None
    return doc


async def authenticate(db, username: str, password: str) -> dict:
    user = await db["users"].find_one({"username": (username or "").strip().lower()})
    ok = verify_password(password or "", user.get("password_hash") if user else None)
    if not (ok and user and not user.get("disabled")):
        raise Unauthorized("invalid username or password", code="invalid_credentials")
    await db["users"].update_one({"_id": user["_id"]}, {"$set": {"last_login_at": utcnow()}})
    return user
