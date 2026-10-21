"""Repo A -> Repo B integration boundary: a FreeSWITCH-delivered inbound call whose route action is 'ai' starts the AI
conversation through B's durable lifecycle (the role the prototype's MediaService.maybe_start played).

FreeSWITCH and the media module are FAKES here (a recording bridge and ESL command stub); model servers are mocks. This proves
the controller's decisions (who starts, when, how often, what is refused), NOT audio, NOT real models, NOT a real call. The real
module path is covered by tests/integration/test_ai_freeswitch.py.
"""
import uuid

import pytest
import pytest_asyncio

from app.main import create_app
from app.providers.base import MockProvider
from tests.conftest import auth, make_settings
from tests.helpers import MockModels

ADMIN = lambda: auth("t1", ("telephony:admin",))  # noqa: E731
CFG = {"stt_url": "http://stt.test:9000", "brain_url": "http://brain.test:9000", "tts_url": "http://tts.test:9000", "enabled": True}
DID = "+918065530418"


class FakeBridge:
    name = "fake-ws-fork"

    def __init__(self):
        self.begun, self.ended = [], []

    async def ready(self):
        return True, "fake"

    async def begin(self, fs_uuid, tenant_id, session_id, token):
        self.begun.append((fs_uuid, tenant_id, session_id))

    async def end(self, fs_uuid):
        self.ended.append(fs_uuid)


class FakeEsl:
    """Answers uuid_getvar like FreeSWITCH: '_undef_' when unset, the value when set."""

    def __init__(self):
        self.cmds, self.getvar_reply = [], "_undef_"

    async def api(self, cmd):
        self.cmds.append(cmd)
        return self.getvar_reply if cmd.startswith("uuid_getvar") else "+OK"


async def make_env(db, *, autostart=True, action="ai", ai_config=True, context="public"):
    s = make_settings(mongo_db=db.s.mongo_db, ai_enabled=True, ai_media_bridge="ws_fork", ai_fs_autostart=autostart,
                      observe_contexts="public", ai_allowed_hosts="stt.test,brain.test,tts.test")
    app = create_app(s, db=db, provider=MockProvider(), ai_transport=MockModels().transport())
    cm = app.router.lifespan_context(app)
    await cm.__aenter__()
    e = type("E", (), {})()
    e.cm, e.app, e.db, e.ctrl, e.ai, e.repos = cm, app, db, app.state.controller, app.state.ai, app.state.repos
    e.bridge, e.esl = FakeBridge(), FakeEsl()
    e.ai.bridge, e.ctrl.esl_cmd = e.bridge, e.esl
    await e.repos.put_policy("t1", {"enabled": True, "caller_ids": [], "transfer_destinations": []})
    await db["inbound_routes"].insert_one({"number": DID, "tenant_id": "t1", "action": action, "enabled": True})
    if ai_config:
        await e.ai.put_config("t1", CFG, "test")
    e.uuid = str(uuid.uuid4())
    e.base = {"Unique-ID": e.uuid, "Call-Direction": "inbound", "Caller-Context": context, "Caller-Destination-Number": "918065530418",
              "Caller-Caller-ID-Number": "+919876543210", "variable_mme_leg": "x"}
    return e


async def ring_and_answer(e, answers=1):
    await e.ctrl.on_esl_event({"Event-Name": "CHANNEL_CREATE", "Event-Sequence": "1", **e.base})
    for i in range(answers):
        await e.ctrl.on_esl_event({"Event-Name": "CHANNEL_ANSWER", "Event-Sequence": str(10 + i), **e.base})
    return await e.db["calls"].find_one({"fs_uuid": e.uuid})


@pytest_asyncio.fixture
async def mk(db):
    made = []

    async def _mk(**kw):
        e = await make_env(db, **kw)
        made.append(e)
        return e
    yield _mk
    for e in made:
        await e.cm.__aexit__(None, None, None)


async def test_answered_ai_route_starts_exactly_one_conversation(mk):
    e = await mk()
    call = await ring_and_answer(e)
    assert call["state"] == "answered" and call["ai_enabled"] is True
    ai = await e.db["ai_sessions"].find_one({"session_id": call["session_id"]})
    assert ai["state"] == "starting"  # becomes 'active' only when the media client really connects
    assert e.bridge.begun == [(e.uuid, "t1", call["session_id"])]
    assert any(ev["type"] == "ai.autostart" for ev in await e.db["call_events"].find({"call_id": call["_id"]}).to_list(50))


async def test_repeated_answer_events_do_not_start_a_second_stream(mk):
    e = await mk()
    call = await ring_and_answer(e, answers=3)
    assert len(e.bridge.begun) == 1
    assert await e.db["ai_sessions"].count_documents({"session_id": call["session_id"]}) == 1


async def test_flag_off_leaves_behaviour_unchanged(mk):
    e = await mk(autostart=False)
    call = await ring_and_answer(e)
    assert call["state"] == "answered" and call["ai_enabled"] is False
    assert e.bridge.begun == [] and await e.db["ai_sessions"].count_documents({}) == 0


async def test_non_ai_route_is_never_touched(mk):
    e = await mk(action="park")
    call = await ring_and_answer(e)
    assert call["state"] == "answered" and e.bridge.begun == [] and await e.db["ai_sessions"].count_documents({}) == 0
    assert not any(c.startswith("uuid_getvar") for c in e.esl.cmds)  # no FreeSWITCH command at all


async def test_unconfigured_ai_skips_with_reason_and_call_continues(mk):
    e = await mk(ai_config=False)
    call = await ring_and_answer(e)
    assert call["state"] == "answered" and e.bridge.begun == []
    evs = await e.db["call_events"].find({"call_id": call["_id"], "type": "ai.autostart_skipped"}).to_list(5)
    assert evs and evs[0]["payload"]["reason"] in ("ai_disabled_for_tenant", "ai_not_configured")


async def test_media_already_attached_by_another_owner_is_not_duplicated(mk):
    e = await mk()
    e.esl.getvar_reply = '{"Authorization":"Bearer x"}'  # the prototype media service (or an operator) attached first
    call = await ring_and_answer(e)
    assert e.bridge.begun == [] and await e.db["ai_sessions"].count_documents({}) == 0
    ev = await e.db["call_events"].find_one({"call_id": call["_id"], "type": "ai.autostart_skipped"})
    assert ev["payload"]["reason"] == "media_already_attached"


async def test_bridge_failure_never_breaks_the_call(mk):
    e = await mk()

    async def boom(*a):
        raise RuntimeError("module gone")
    e.bridge.begin = boom
    call = await ring_and_answer(e)
    assert call["state"] == "answered"
    ai = await e.db["ai_sessions"].find_one({"session_id": call["session_id"]})
    assert ai["state"] == "failed" and ai["stop_reason"] == "bridge_start_failed"


async def test_hangup_after_autostart_stops_the_ai_session(mk):
    e = await mk()
    call = await ring_and_answer(e)
    await e.ctrl.on_esl_event({"Event-Name": "CHANNEL_HANGUP_COMPLETE", "Event-Sequence": "99", "Hangup-Cause": "NORMAL_CLEARING", **e.base})
    ai = await e.db["ai_sessions"].find_one({"session_id": call["session_id"]})
    assert ai["state"] == "stopped" and ai["stop_reason"] == "call_ended" and e.bridge.ended == [e.uuid]


def test_autostart_requires_ai_and_ws_fork_bridge():
    with pytest.raises(ValueError, match="AI_FS_AUTOSTART"):
        make_settings(ai_fs_autostart=True)
    with pytest.raises(ValueError, match="AI_FS_AUTOSTART"):
        make_settings(ai_fs_autostart=True, ai_enabled=True, ai_media_bridge="none")
    assert make_settings(ai_fs_autostart=True, ai_enabled=True, ai_media_bridge="ws_fork").ai_fs_autostart
