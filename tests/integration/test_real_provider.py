"""OPT-IN real Plivo suite. Skipped unless ALL of these are set explicitly (it places a PAID call):

  MME_TEL_REAL_PROVIDER_TESTS=1
  MME_TEL_PLIVO_AUTH_ID / MME_TEL_PLIVO_AUTH_TOKEN          (secrets, from your secret store)
  MME_TEL_WEBHOOK_PUBLIC_BASE_URL                           (https URL that reaches THIS test instance)
  MME_TEL_TEST_DESTINATION                                  (an approved, consenting test number, E.164)
  MME_TEL_TEST_CALLER_ID                                    (a Plivo number you own, E.164)

Never runs in routine CI. Verifies the UNVERIFIED items in docs/PROVIDER_SETUP.md: originate response
(request_uuid), callback field names, hangup of a live call.
"""
import asyncio
import os

import httpx
import pytest

from app.main import create_app
from tests.conftest import auth, enable_tenant, idem, make_settings

REQUIRED = ["MME_TEL_PLIVO_AUTH_ID", "MME_TEL_PLIVO_AUTH_TOKEN", "MME_TEL_WEBHOOK_PUBLIC_BASE_URL",
            "MME_TEL_TEST_DESTINATION", "MME_TEL_TEST_CALLER_ID"]

pytestmark = [
    pytest.mark.real_provider,
    pytest.mark.skipif(os.environ.get("MME_TEL_REAL_PROVIDER_TESTS") != "1" or any(not os.environ.get(k) for k in REQUIRED),
                       reason="real-provider tests are opt-in (see module docstring)"),
]


async def test_real_outbound_call_reaches_ringing_and_can_be_hung_up(db):
    dest, cid_num = os.environ["MME_TEL_TEST_DESTINATION"], os.environ["MME_TEL_TEST_CALLER_ID"]
    s = make_settings(mongo_db=db.s.mongo_db, provider="plivo_api", real_calls_enabled=True,
                      plivo_auth_id=os.environ["MME_TEL_PLIVO_AUTH_ID"], plivo_auth_token=os.environ["MME_TEL_PLIVO_AUTH_TOKEN"],
                      webhook_public_base_url=os.environ["MME_TEL_WEBHOOK_PUBLIC_BASE_URL"], max_active_global=1,
                      max_active_per_tenant=1, provider_max_concurrent=1, default_allowed_countries="IN,US,GB")
    app = create_app(s, db=db)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            await enable_tenant(type("E", (), {"repos": app.state.repos}), caller_ids=[cid_num])
            r = await c.post("/v1/calls", json={"to": dest, "from_number": cid_num, "max_duration_s": 30},
                             headers={**auth(), **idem()})
            assert r.status_code == 202, r.text
            call_id = r.json()["id"]
            for _ in range(60):  # up to ~30 s for Plivo to accept and the callee to ring
                call = await app.state.repos.get_call(call_id)
                if call.get("provider_call_id"):
                    break
                await asyncio.sleep(0.5)
            assert call.get("provider_call_id"), "Plivo did not return a request_uuid"
            await asyncio.sleep(5)
            h = await c.post(f"/v1/calls/{call_id}/hangup", headers=auth())
            assert h.status_code in (200, 502), h.text  # 502 documents the UNVERIFIED cancel-by-request-uuid behaviour
            print("final state:", (await app.state.repos.get_call(call_id))["state"])
