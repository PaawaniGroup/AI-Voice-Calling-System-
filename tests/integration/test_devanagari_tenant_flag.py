"""The Devanagari flow is a per-tenant switch: default OFF; settable through the config API; read from the stored tenant document; it reaches
the live conversation and the Brain request. Models are MOCKS; the service, the media WebSocket and the conversation loop are real."""
import json

from app.ai.pipeline import OLD_SYSTEM_PROMPT, SYSTEM_PROMPT_DEVANAGARI
from tests.conftest import auth
from tests.integration.test_ai import collect, connect, hello, media, speak  # noqa: F401  (`media` is a fixture)

ADMIN = lambda: auth("t1", ("telephony:admin",))  # noqa: E731


async def _restart_ai(e):
    await e.c.post(f"/v1/ai/sessions/{e.sid}/stop", headers=auth())
    assert (await e.c.post(f"/v1/ai/sessions/{e.sid}/start", json={}, headers=auth())).status_code == 202


async def test_default_tenant_has_the_flag_off_and_the_legacy_prompt(media):
    e = media
    cfg = await e.ai.get_config("t1")
    assert cfg["devanagari_hindi"] is False and cfg["runpod_mode"] == "run"
    conv = e.ai.runtime[e.sid].conversation
    assert conv.devanagari_hindi is False and conv.messages[0]["content"] == OLD_SYSTEM_PROMPT


async def test_the_flag_is_settable_through_the_config_api_and_reaches_the_conversation_and_the_brain(media):
    e = media
    r = await e.c.put("/v1/ai/config", json={"devanagari_hindi": True}, headers=ADMIN())
    assert r.status_code == 200 and r.json()["devanagari_hindi"] is True
    assert (await e.c.get("/v1/ai/config", headers=ADMIN())).json()["devanagari_hindi"] is True
    await _restart_ai(e)
    conv = e.ai.runtime[e.sid].conversation
    assert conv.devanagari_hindi and conv.messages[0]["content"] == SYSTEM_PROMPT_DEVANAGARI
    ws = await connect(e)
    await hello(ws)
    await speak(ws)
    await collect(ws, "playback_end")
    brain = next(b for k, b in e.models.calls if k == "brain")
    assert brain["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT_DEVANAGARI}
    assert brain["messages"][-1]["content"].endswith("(reply_mode: hi)")                     # the tag reached the Brain request
    await ws.close()


async def test_the_flag_can_be_switched_off_again_and_a_stored_document_value_is_honoured(media):
    e = media
    await e.db["ai_config"].update_one({"tenant_id": "t1"}, {"$set": {"devanagari_hindi": True}})   # as set directly on the stored document
    assert (await e.ai.get_config("t1"))["devanagari_hindi"] is True
    await e.c.put("/v1/ai/config", json={"devanagari_hindi": False}, headers=ADMIN())
    await _restart_ai(e)
    assert e.ai.runtime[e.sid].conversation.messages[0]["content"] == OLD_SYSTEM_PROMPT


async def test_runpod_mode_is_validated_and_survives_other_config_updates(media):
    e = media
    assert (await e.c.put("/v1/ai/config", json={"runpod_mode": "sideways"}, headers=ADMIN())).status_code == 422
    assert (await e.c.put("/v1/ai/config", json={"runpod_mode": "runsync"}, headers=ADMIN())).status_code == 200
    await e.c.put("/v1/ai/config", json={"default_language": "hi"}, headers=ADMIN())
    assert (await e.ai.get_config("t1"))["runpod_mode"] == "runsync"
