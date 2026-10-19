"""The existing prototype listener (mme_telephony/esl_events.py) POSTs a session keyed by the FreeSWITCH UUID,
then /answer and /end with the session id. These tests replay exactly that call sequence against the new API."""
import uuid

from tests.conftest import auth, enable_tenant
from tests.integration.test_api_calls import esl, make_call, wait_state


async def test_prototype_sequence_works_unchanged_apart_from_auth(env):
    h = auth("mme-test")
    fs = str(uuid.uuid4())
    r = await env.client.post("/v1/sessions", headers=h, json={"tenant_id": "mme-test", "caller_id": "+919876543210", "call_id": fs, "language": "auto",
                                                                  "metadata": {"source": "freeswitch-esl", "destination": "918065530418"}})
    assert r.status_code == 201
    sid = r.json()["session_id"]
    assert r.json()["status"] == "created"  # prototype said "active"; documented difference
    assert (await env.client.get(f"/v1/sessions/{sid}", headers=h)).json()["call_id"] is None  # unknown call: stored as an external reference
    assert (await env.client.post(f"/v1/sessions/{sid}/answer", headers=h)).json()["status"] == "answered"
    e = await env.client.post(f"/v1/sessions/{sid}/end", headers=h, json={"reason": "NORMAL_CLEARING"})
    assert e.json()["status"] == "ended" and e.json()["end_reason"] == "NORMAL_CLEARING"
    # duplicate CHANNEL_CREATE delivery from the listener must not create a second session
    r2 = await env.client.post("/v1/sessions", headers=h, json={"tenant_id": "mme-test", "call_id": fs})
    assert r2.json()["session_id"] == sid


async def test_listener_session_for_a_call_we_already_track_is_the_same_session(env):
    await enable_tenant(env)
    c = await make_call(env)
    c2 = await wait_state(env, c["id"], "initiating")
    # old listener (still running) reports the same call by FreeSWITCH UUID -> must return OUR session, not create another
    r = await env.client.post("/v1/sessions", headers=auth(), json={"tenant_id": "t1", "call_id": c2["fs_uuid"]})
    assert r.json()["session_id"] == c["session_id"]
    assert await env.db["sessions"].count_documents({}) == 1
    assert (await env.client.get(f"/v1/sessions?q={c2['fs_uuid']}", headers=auth())).json()["items"][0]["session_id"] == c["session_id"]
    await env.ctrl.on_esl_event(esl("CHANNEL_ANSWER", c2["fs_uuid"]))
    assert (await env.client.post(f"/v1/sessions/{c['session_id']}/end", headers=auth(), json={"reason": "x"})).json()["status"] == "ended"
