"""JWT authentication, role checks, tenant resolution and in-process rate limiting."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

import jwt
from fastapi import Request

from app.core.errors import Forbidden, RateLimited, Unauthorized
from app.observability import metrics as m
from config.settings import Settings


@dataclass(frozen=True)
class Principal:
    sub: str
    tenant_id: str
    roles: frozenset[str] = field(default_factory=frozenset)

    def has(self, role: str) -> bool:
        return role in self.roles


def decode_token(token: str, s: Settings) -> Principal:
    secret = s.jwt_secret.get_secret_value()
    if not secret:
        raise Unauthorized("authentication not configured", code="auth_not_configured")
    opts = {"require": ["exp", "sub"], "verify_aud": bool(s.jwt_audience), "verify_iss": bool(s.jwt_issuer)}
    try:
        claims = jwt.decode(token, secret, algorithms=[s.jwt_algorithm], options=opts, leeway=s.jwt_leeway_s,
                            audience=s.jwt_audience or None, issuer=s.jwt_issuer or None)
    except jwt.PyJWTError:
        raise Unauthorized("invalid or expired token") from None
    tenant = claims.get(s.jwt_tenant_claim)
    roles = claims.get(s.jwt_roles_claim) or []
    if not isinstance(tenant, str) or not tenant or not isinstance(roles, list):
        raise Unauthorized("token missing tenant or roles")
    return Principal(sub=str(claims["sub"]), tenant_id=tenant, roles=frozenset(str(r) for r in roles))


def principal_dep(request: Request) -> Principal:
    s: Settings = request.app.state.settings
    h = request.headers.get("authorization", "")
    scheme, _, token = h.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("missing bearer token")
    p = decode_token(token.strip(), s)
    request.state.principal = p
    request.app.state.limiter.check(f"p:{p.tenant_id}:{p.sub}", s.rate_limit_per_min)
    return p


def require_viewer(request: Request) -> Principal:
    """Read-only access: viewer, operator (user), admin or platform."""
    p = principal_dep(request)
    s: Settings = request.app.state.settings
    if not any(p.has(r) for r in (s.role_viewer, s.role_user, s.role_admin, s.role_platform)):
        raise Forbidden("insufficient role")
    return p


def require_user(request: Request) -> Principal:
    p = principal_dep(request)
    s: Settings = request.app.state.settings
    if not (p.has(s.role_user) or p.has(s.role_admin) or p.has(s.role_platform)):
        raise Forbidden("insufficient role")
    return p


def require_admin(request: Request) -> Principal:
    p = principal_dep(request)
    s: Settings = request.app.state.settings
    if not (p.has(s.role_admin) or p.has(s.role_platform)):
        raise Forbidden("admin role required")
    return p


def require_platform(request: Request) -> Principal:
    p = principal_dep(request)
    if not p.has(request.app.state.settings.role_platform):
        raise Forbidden("platform admin role required")
    return p


class RateLimiter:
    """Sliding-window limiter, per process (the service runs a single worker; see docs/SECURITY.md)."""

    def __init__(self) -> None:
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window: float = 60.0) -> None:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            m.RATE_LIMITED.inc()
            raise RateLimited("rate limit exceeded", code="rate_limited")
        q.append(now)
        if len(self.hits) > 50000:  # bound memory
            for k in [k for k, v in self.hits.items() if not v][:10000]:
                self.hits.pop(k, None)
