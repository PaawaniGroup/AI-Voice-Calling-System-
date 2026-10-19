import asyncio

import pytest

from app.core.errors import Conflict, NotFound, Unavailable
from app.services import recordings as rec
from tests.conftest import auth, make_settings
from tests.integration.test_freeswitch import _fs_up, fs_env, place, wait_for  # noqa: F401
from tests.conftest import enable_tenant


def test_recording_policy_matrix():
    on = {"recording": "on"}
    s_off = make_settings()
    assert rec.recording_allowed(s_off, on) == (False, "recording_disabled_globally")
    s = make_settings(recording_enabled=True, recording_consent_mode="announce", storage_backend="local")
    assert rec.recording_allowed(s, on) == (True, "ok")
    assert rec.recording_allowed(s, {"recording": "off"})[0] is False
    assert rec.recording_allowed(s, None)[0] is False


async def test_local_storage_rejects_path_escape_and_has_no_public_url(tmp_path):
    st = rec.LocalStorage(str(tmp_path))
    with pytest.raises(ValueError):
        st._p("../evil")
    with pytest.raises(Unavailable):
        await st.presign("a/b.wav", 60)


async def test_download_authorization_and_state(db, tmp_path):
    from app.persistence.repos import Repos
    s = make_settings(mongo_db=db.s.mongo_db)
    repos = Repos(db, s)
    await db["recordings"].insert_one({"_id": "rec_1", "tenant_id": "t1", "call_id": "c", "status": "recording", "created_at": None})
    with pytest.raises(NotFound):
        await rec.download_url(repos, rec.LocalStorage(str(tmp_path)), s, "t2", "rec_1")  # other tenant
    with pytest.raises(Conflict):
        await rec.download_url(repos, rec.LocalStorage(str(tmp_path)), s, "t1", "rec_1")  # not available yet


@pytest.mark.freeswitch
async def test_real_recording_roundtrip_on_local_freeswitch(db, tmp_path):
    if not _fs_up():
        pytest.skip("FreeSWITCH not available")
    spool = "/tmp/mme_rec_test"
    async with fs_env(db, recording_enabled=True, recording_consent_mode="announce", storage_backend="local",
                      fs_recordings_dir=spool) as env:
        await enable_tenant(env, recording="on")
        cid = await place(env)
        await wait_for(env, cid, "answered")
        await asyncio.sleep(2.5)  # let FreeSWITCH write some audio
        r = await env.client.post(f"/v1/calls/{cid}/hangup", headers=auth())
        assert r.status_code == 200
        await wait_for(env, cid, "completed")
        recs = await db["recordings"].find({"call_id": cid}).to_list(5)
        assert len(recs) == 1 and recs[0]["status"] == "recording"
        assert await rec.finalize_recordings(env.repos, rec.make_storage(env.s), env.s) == 1
        done = await db["recordings"].find_one({"call_id": cid})
        assert done["status"] == "available" and done["size_bytes"] > 1000 and done["consent_mode"] == "announce"
        listing = (await env.client.get(f"/v1/calls/{cid}/recordings", headers=auth())).json()["items"]
        assert listing[0]["status"] == "available"
        assert (await env.client.get(f"/v1/calls/{cid}/recordings", headers=auth("t2"))).status_code == 404
        # local backend cannot mint URLs (never a public link)
        assert (await env.client.get(f"/v1/recordings/{done['_id']}/download-url", headers=auth())).status_code == 503


async def test_no_recording_without_policy(db):
    if not _fs_up():
        pytest.skip("FreeSWITCH not available")
    async with fs_env(db) as env:  # recording globally disabled by default
        await enable_tenant(env, recording="on")
        cid = await place(env)
        await wait_for(env, cid, "answered")
        assert await db["recordings"].count_documents({}) == 0
