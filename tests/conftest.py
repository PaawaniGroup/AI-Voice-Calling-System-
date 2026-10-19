import os
import sys
import time
import uuid
from pathlib import Path

import httpx
import jwt
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import create_app  # noqa: E402
from app.persistence.db import Database  # noqa: E402
from app.providers.base import MockProvider  # noqa: E402
from config.settings import Settings  # noqa: E402

MONGO = os.environ.get("MME_TEL_TEST_MONGO_URI", "mongodb://127.0.0.1:27117")
JWT_SECRET = "test-secret-" + "x" * 32
MASTER = "test-master-" + "y" * 32


def make_settings(**kw) -> Settings:
    base = dict(env="test", provider="mock", fs_enabled=False, mongo_uri=MONGO, jwt_secret=JWT_SECRET,
                webhook_signing_master=MASTER, log_level="WARNING", webhook_allow_private=True, webhook_allow_http=True,
                dispatch_poll_s=0.05, max_initiation_rate=1000.0, provider_max_cps=1000.0, reconcile_grace_s=0.0,
                mongo_server_selection_timeout_ms=1500, default_allowed_countries="IN,US")
    base.update(kw)
    return Settings(_env_file=None, **base)


def token(tenant="t1", sub="u1", roles=("telephony:user",), secret=JWT_SECRET, exp=3600, **extra):
    return jwt.encode({"sub": sub, "tenant_id": tenant, "roles": list(roles), "exp": int(time.time()) + exp, **extra},
                      secret, algorithm="HS256")


def auth(tenant="t1", roles=("telephony:user",), sub="u1"):
    return {"Authorization": f"Bearer {token(tenant, sub, roles)}"}


@pytest.fixture
def settings_factory():
    return make_settings


@pytest_asyncio.fixture
async def db():
    s = make_settings(mongo_db=f"mmetel_test_{uuid.uuid4().hex[:10]}")
    d = Database(s)
    try:
        await d.connect()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"test MongoDB not reachable at {MONGO}: {type(e).__name__} (run scripts/dev_mongo.sh)")
    yield d
    await d.client.drop_database(s.mongo_db)
    await d.close()


@pytest_asyncio.fixture
async def env(db):
    """App + mock provider + HTTP client with lifespan running. Controller loops are live."""
    prov = MockProvider()
    app = create_app(db.s, db=db, provider=prov)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            e = type("Env", (), {})()
            e.app, e.client, e.prov, e.db, e.s = app, client, prov, db, db.s
            e.repos, e.ctrl = app.state.repos, app.state.controller
            yield e


async def enable_tenant(env, tenant="t1", **kw):
    pol = {"enabled": True, "caller_ids": ["+911140000000"], "transfer_destinations": ["+919999999999", "+9188*"], **kw}
    await env.repos.put_policy(tenant, pol)


def call_body(**kw):
    return {"to": "+919876543210", "from_number": "+911140000000", **kw}


def idem():
    return {"Idempotency-Key": uuid.uuid4().hex}
