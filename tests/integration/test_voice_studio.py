"""Voice Studio on the REAL service stack (routes, MongoDB + GridFS, AiService config, background jobs) with a MOCK RunPod-style worker (FakeWorker below).
Mocks never count as live verification: nothing here talks to a GPU, RunPod, or any paid service. Fixtures are generated tones (tests/voice_fixtures.py)."""
import asyncio
import base64
import hashlib
import io
import json
import logging
import wave

import httpx
import pytest
import pytest_asyncio

from app.main import create_app
from app.providers.base import MockProvider
from tests.conftest import auth, make_settings
from tests.helpers import pcm_tone, tone_wav
from tests.voice_fixtures import good_clip, pack_wav, wav_from_segments, wav_info

TTS_URL = "http://tts.test/v2/ep/runsync"
ADMIN = lambda t="t1": auth(t, ("telephony:admin",))      # noqa: E731
OPERATOR = lambda t="t1": auth(t, ("telephony:user",))    # noqa: E731
VIEWER = lambda t="t1": auth(t, ("telephony:viewer",))    # noqa: E731
SENTINEL = "ZQXKSENTINELTEXTWQ"                           # typed text that must never reach a log, the audit trail or the database


class FakeWorker:
    """MOCK of the RunPod TTS worker with the Voice Studio tasks. `hold` freezes a job in "queue" (IN_QUEUE) or "run" (IN_PROGRESS) so tests can look at each state."""

    def __init__(self):
        self.voices = {"utkarsh"}
        self.log = []                  # one entry per job: kind (synth/register/voices) + what mattered, never audio
        self.jobs, self.cancelled = {}, []
        self.hold = None               # None = finish at once; "queue" / "run" = stay in that state
        self.run_status = 200
        self.always_needs_reference = False
        self.legacy = False            # an OLD worker (no Voice Studio tasks, error without code)
        self.report_time = True
        self.delay_ms, self.exec_ms = 1000, 500
        self.tone_ms = 400

    def kinds(self):
        return [e["kind"] for e in self.log]

    def output(self, inp):
        task = inp.get("task")
        if task == "register_voice":
            raw = base64.b64decode(inp["reference_audio_base64"])
            self.log.append({"kind": "register", "name": inp["name"], "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "info": wav_info(raw)})
            self.voices.add(inp["name"])
            return {"ok": True, "name": inp["name"], "cached": False, "prepare_s": 1.2}
        if task == "voices":
            self.log.append({"kind": "voices"})
            return {"ok": True, "voices": sorted(self.voices)}
        self.log.append({"kind": "synth", "input": {k: v for k, v in inp.items()}})
        if self.legacy:
            return {"error": f"Unknown voice: {inp.get('voice')!r}", "voices": sorted(self.voices)} if inp.get("voice") not in self.voices else self._audio(inp)
        if inp.get("voice") not in self.voices or self.always_needs_reference:
            return {"error": "needs_reference", "code": "needs_reference", "voice": inp.get("voice"), "voices": sorted(self.voices)}
        return self._audio(inp)

    def _audio(self, inp):
        if inp.get("output_format") == "ulaw8k":
            import audioop
            return {"audio_base64": base64.b64encode(audioop.lin2ulaw(pcm_tone(self.tone_ms, 8000), 2)).decode(), "format": "ulaw8k", "sample_rate": 8000}
        return {"audio_base64": base64.b64encode(tone_wav(self.tone_ms, rate=24000)).decode(), "format": "wav", "sample_rate": 24000}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/run") and request.method == "POST":
            if self.run_status >= 400:
                return httpx.Response(self.run_status)
            inp = json.loads(request.content)["input"]
            jid = f"job-{len(self.jobs) + 1}"
            self.jobs[jid] = {"input": inp, "out": None}
            return httpx.Response(200, json={"id": jid, "status": "IN_QUEUE"})
        jid = path.rsplit("/", 1)[-1]
        if "/cancel/" in path:
            self.cancelled.append(jid)
            return httpx.Response(200, json={"id": jid, "status": "CANCELLED"})
        job = self.jobs.get(jid)
        if job is None:
            return httpx.Response(404)
        if self.hold == "queue":
            return httpx.Response(200, json={"id": jid, "status": "IN_QUEUE"})
        if self.hold == "run":
            return httpx.Response(200, json={"id": jid, "status": "IN_PROGRESS"})
        if job["out"] is None:
            job["out"] = self.output(job["input"])
        body = {"id": jid, "status": "COMPLETED", "output": job["out"]}
        if self.report_time:
            body.update(delayTime=self.delay_ms, executionTime=self.exec_ms)
        return httpx.Response(200, json=body)


class Env:
    pass


@pytest_asyncio.fixture
async def make_env(db):
    """`await make_env(**settings)` -> app + client + FakeWorker, sharing the test database. Tenants t1 and t2 both have the flag OFF until `enable()`."""
    stack = []

    async def make(**kw):
        fake = FakeWorker()
        s = make_settings(mongo_db=db.s.mongo_db, ai_allowed_hosts="tts.test", voice_studio_poll_s=0.01, **kw)
        app = create_app(s, db=db, provider=MockProvider(), ai_transport=httpx.MockTransport(fake.handler))
        cm = app.router.lifespan_context(app)
        await cm.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        stack.append((cm, client))
        e = Env()
        e.app, e.c, e.fake, e.s, e.db = app, client, fake, s, db
        return e

    yield make
    for cm, client in reversed(stack):
        await client.aclose()
        await cm.__aexit__(None, None, None)


async def enable(e, tenant="t1", tts=TTS_URL, flag=True):
    body = {"voice_studio": flag}
    if tts:
        body["tts_url"] = tts
    r = await e.c.put("/v1/ai/config", json=body, headers=ADMIN(tenant))
    assert r.status_code == 200, r.text


async def upload(e, wav=None, *, name="asha", speaker="Asha Verma", consent="true", scope="preview_only", tenant="t1", headers=None, extra=None):
    data = {"name": name, "speaker_name": speaker, "consent": consent, "consent_scope": scope, **(extra or {})}
    return await e.c.post("/v1/voices", data={k: v for k, v in data.items() if v is not None},
                          files={"file": ("clip.wav", wav if wav is not None else good_clip(), "audio/wav")}, headers=headers or ADMIN(tenant))


async def make_voice(e, tenant="t1", **kw):
    r = await upload(e, tenant=tenant, **kw)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def preview(e, vid, text="namaste, aapki fees 45000 rupees hai", language="auto", phone_quality=False, tenant="t1"):
    return await e.c.post(f"/v1/voices/{vid}/preview", json={"text": text, "language": language, "phone_quality": phone_quality}, headers=ADMIN(tenant))


async def status(e, job_id, tenant="t1"):
    r = await e.c.get(f"/v1/voices/previews/{job_id}", headers=ADMIN(tenant))
    assert r.status_code == 200, r.text
    return r.json()


async def until_status(e, job_id, wanted, tenant="t1"):
    wanted = (wanted,) if isinstance(wanted, str) else wanted
    for _ in range(600):
        st = await status(e, job_id, tenant)
        if st["status"] in wanted:
            return st
        await asyncio.sleep(0.01)
    raise AssertionError(f"job stayed {st['status']}, wanted {wanted}")


async def run_preview(e, vid, **kw):
    r = await preview(e, vid, **kw)
    assert r.status_code == 202, r.text
    return await until_status(e, r.json()["job_id"], ("done", "error"), kw.get("tenant", "t1"))


# ================================================================== the flag, roles, tenants
async def test_the_flag_is_off_by_default_and_every_endpoint_is_a_404_until_it_is_turned_on(make_env):
    e = await make_env()
    cfg = (await e.c.get("/v1/ai/config", headers=ADMIN())).json()
    assert cfg["voice_studio"] is False
    for method, url, kw in [("GET", "/v1/voices", {}), ("POST", "/v1/voices/x/preview", {"json": {"text": "a"}}), ("POST", "/v1/voices/x/disable", {}), ("DELETE", "/v1/voices/x", {}),
                            ("GET", "/v1/voices/previews/x", {}), ("GET", "/v1/voices/previews/x/audio", {})]:
        r = await e.c.request(method, url, headers=ADMIN(), **kw)
        assert r.status_code == 404 and r.json()["error"]["code"] == "voice_studio_disabled", (method, url, r.text)
    assert (await upload(e)).status_code == 404
    await enable(e, "t1")
    assert (await e.c.get("/v1/voices", headers=ADMIN())).status_code == 200
    assert (await e.c.get("/v1/voices", headers=ADMIN("t2"))).status_code == 404         # the flag is per tenant
    stored = await e.db["ai_config"].find_one({"tenant_id": "t1"})
    assert stored["voice_studio"] is True
    await enable(e, "t1", tts=None, flag=False)
    assert (await e.c.get("/v1/voices", headers=ADMIN())).status_code == 404


async def test_only_admins_may_use_voice_studio_and_a_token_is_required(make_env):
    e = await make_env()
    await enable(e)
    vid = await make_voice(e)
    for who in (VIEWER, OPERATOR):
        h = who()
        assert (await e.c.get("/v1/voices", headers=h)).status_code == 403
        assert (await upload(e, headers=h)).status_code == 403
        assert (await e.c.post(f"/v1/voices/{vid}/preview", json={"text": "a"}, headers=h)).status_code == 403
        assert (await e.c.post(f"/v1/voices/{vid}/disable", headers=h)).status_code == 403
        assert (await e.c.delete(f"/v1/voices/{vid}", headers=h)).status_code == 403
        assert (await e.c.get("/v1/voices/previews/x", headers=h)).status_code == 403
    assert (await e.c.get("/v1/voices")).status_code == 401
    assert (await e.c.post("/v1/voices", files={"file": ("c.wav", b"x")})).status_code == 401
    assert (await e.c.get("/v1/voices", headers=auth("t1", ("platform:admin",)))).status_code == 200      # the platform role counts as admin


async def test_another_tenant_cannot_see_use_disable_or_delete_a_voice_and_gets_a_404(make_env):
    e = await make_env()
    await enable(e, "t1")
    await enable(e, "t2")
    vid = await make_voice(e, "t1")
    assert (await e.c.get("/v1/voices", headers=ADMIN("t2"))).json()["items"] == []
    for method, url, kw in [("POST", f"/v1/voices/{vid}/preview", {"json": {"text": "namaste"}}), ("POST", f"/v1/voices/{vid}/disable", {}), ("DELETE", f"/v1/voices/{vid}", {})]:
        r = await e.c.request(method, url, headers=ADMIN("t2"), **kw)
        assert r.status_code == 404 and r.json()["error"]["code"] == "voice_not_found", (method, r.text)
    assert (await e.c.get("/v1/voices", headers=ADMIN("t1"))).json()["items"][0]["status"] == "active"      # nothing happened to it
    job = (await preview(e, vid, tenant="t1")).json()["job_id"]
    await until_status(e, job, "done")
    assert (await e.c.get(f"/v1/voices/previews/{job}", headers=ADMIN("t2"))).status_code == 404
    assert (await e.c.get(f"/v1/voices/previews/{job}/audio", headers=ADMIN("t2"))).status_code == 404
    assert (await e.c.get(f"/v1/voices/previews/{job}/audio", headers=ADMIN("t1"))).status_code == 200
    same_name = await upload(e, name="asha", tenant="t2")                                                    # names are per tenant
    assert same_name.status_code == 201 and same_name.json()["id"] != vid


# ================================================================== upload: consent, names, clip checks, size
async def test_an_upload_stores_the_prepared_clip_with_its_consent_record_and_reports_its_quality(make_env):
    e = await make_env()
    await enable(e)
    r = await upload(e, name="Asha_1", speaker="Asha Verma", scope="preview_and_calls")
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["status"] == "active" and v["name"] == "Asha_1" and v["worker_name"] == f"v_{v['id']}" and len(v["worker_name"]) <= 40
    assert v["consent_ref"]["speaker_name"] == "Asha Verma" and v["consent_ref"]["scope"] == "preview_and_calls" and len(v["consent_ref"]["date"]) == 10
    rep = v["report"]
    assert {"duration_s", "peak_dbfs", "noise_floor_dbfs", "speech_s_first_6s", "speech_s_first_10s", "warnings"} <= set(rep) and 10 <= rep["duration_s"] <= 10.8
    doc = await e.db["voices"].find_one({"_id": v["id"]})
    assert doc["tenant_id"] == "t1" and doc["created_by"] == "u1" and doc["checksum"] == v["checksum"] and doc["name_lower"] == "asha_1"
    assert "gridfs_id" not in json.dumps(v) and "gridfs" not in str(r.json())                           # no storage detail in the API
    stream = await e.app.state.voices.store.bucket.open_download_stream(doc["gridfs_id"])
    clip = await stream.read()
    assert hashlib.sha256(clip).hexdigest() == doc["checksum"] and wav_info(clip)["rate"] == 24000     # the PREPARED clip is what is stored
    listing = (await e.c.get("/v1/voices", headers=ADMIN())).json()
    assert [i["id"] for i in listing["items"]] == [v["id"]] and listing["decoder"]["wav_pcm"] is True and listing["limits"]["text_max_chars"] == 300
    assert listing["usage"]["spend_is_an_estimate"] is True and listing["languages"] == ["auto", "hi", "en", "hinglish"]


@pytest.mark.parametrize("kw", [{"consent": None}, {"consent": "false"}, {"consent": ""}, {"consent": "maybe"}, {"speaker": ""}, {"speaker": "   "}, {"speaker": "x" * 81}])
async def test_consent_and_a_speaker_name_are_required(make_env, kw):
    e = await make_env()
    await enable(e)
    r = await upload(e, **kw)
    assert r.status_code == 422 and r.json()["error"]["code"] == "consent_required", r.text
    assert await e.db["voices"].count_documents({}) == 0 and await e.db["voice_clips.files"].count_documents({}) == 0


async def test_an_unknown_consent_scope_is_refused(make_env):
    e = await make_env()
    await enable(e)
    r = await upload(e, scope="everything")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_scope"


@pytest.mark.parametrize("name", ["", "a b", "x" * 41, "../etc", "naam!", "नाम", "a/b", "a.b", "   "])
async def test_voice_names_follow_the_rule_letters_digits_underscore_dash_up_to_40(make_env, name):
    e = await make_env()
    await enable(e)
    r = await upload(e, name=name)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_name", r.text


async def test_good_names_work_and_a_name_is_unique_per_tenant_ignoring_case(make_env):
    e = await make_env()
    await enable(e)
    assert (await upload(e, name="a" * 36 + "_-9Z")).status_code == 201
    assert (await upload(e, name="Asha")).status_code == 201
    r = await upload(e, name="ASHA")
    assert r.status_code == 409 and r.json()["error"]["code"] == "voice_name_taken"
    assert await e.db["voice_clips.files"].count_documents({}) == 2                                    # the refused duplicate left no orphan clip


@pytest.mark.parametrize("segments,kw,code", [
    ([("silence", 6.0)], {}, "clip_silent"),
    ([("silence", 1.0), ("speech", 2.5), ("silence", 1.0)], {}, "clip_too_short"),
    ([("silence", 1.0), ("speech", 8.0), ("silence", 1.0)], {"amp": 0.9, "hard_clip": True}, "clip_clipped"),
])
async def test_a_bad_clip_is_refused_with_the_reason_and_the_report_and_nothing_is_stored(make_env, segments, kw, code):
    e = await make_env()
    await enable(e)
    r = await upload(e, wav_from_segments(segments, **kw))
    err = r.json()["error"]
    assert r.status_code == 422 and err["code"] == code and err["message"] and "peak_dbfs" in err["details"]["report"]
    assert await e.db["voices"].count_documents({}) == 0 and await e.db["voice_clips.files"].count_documents({}) == 0
    audit = await e.db["audit"].find({"action": "voice.upload"}).to_list(10)
    assert audit[0]["details"] == {"ok": False, "code": code}


async def test_a_file_that_is_not_a_wav_says_wav_only(make_env):
    e = await make_env()
    await enable(e)
    r = await upload(e, b"ID3\x03\x00" + b"\x01" * 4000)
    assert r.status_code == 422 and r.json()["error"]["code"] == "clip_unsupported_format" and "WAV" in r.json()["error"]["message"]


async def test_the_upload_size_limit_is_enforced_before_and_after_reading(make_env):
    e = await make_env(voice_studio_max_upload_bytes=100_000)
    await enable(e)
    r = await upload(e)                                                                                  # a 12 s clip is about 576 KB: refused by the body limit
    assert r.status_code == 413
    slightly_over = pack_wav([0.0] * 60000, rate=24000)                                                  # about 120 KB: under the framing slack, over the file limit
    r = await upload(e, slightly_over)
    assert r.status_code == 422 and r.json()["error"]["code"] == "clip_too_large"


async def test_an_upload_without_a_content_length_is_refused_and_other_routes_keep_the_small_body_limit(make_env):
    e = await make_env()
    await enable(e)

    async def chunks():
        yield b"--x\r\n"

    r = await e.c.post("/v1/voices", content=chunks(), headers={**ADMIN(), "content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 411
    big = await e.c.post("/v1/ai/config", content=b"{" + b" " * 70000 + b"}", headers={**ADMIN(), "content-type": "application/json"})
    assert big.status_code == 413                                                                         # the global 64 KB limit still applies elsewhere
    assert (await e.c.post("/v1/voices", json={"a": 1}, headers=ADMIN())).status_code == 422             # not multipart


# ================================================================== limits
async def test_uploads_per_day_voice_count_and_other_tenants(make_env):
    e = await make_env(voice_studio_uploads_per_day=3, voice_studio_max_voices=2)
    await enable(e, "t1")
    await enable(e, "t2")
    assert (await upload(e, name="a1")).status_code == 201
    assert (await upload(e, wav_from_segments([("silence", 5.0)]), name="a2")).status_code == 422       # a refused clip still counts as an attempt
    assert (await upload(e, name="a3")).status_code == 201
    r = await upload(e, name="a4")
    assert r.status_code == 429 and r.json()["error"]["code"] == "voice_limit" and r.json()["error"]["details"]["limit"] == "max_voices"      # 2 voices held
    await e.c.delete(f"/v1/voices/{(await e.c.get('/v1/voices', headers=ADMIN())).json()['items'][0]['id']}", headers=ADMIN())
    r = await upload(e, name="a5")
    assert r.status_code == 429 and r.json()["error"]["details"]["limit"] == "uploads_per_day"           # the third attempt of the day was the last
    assert (await upload(e, name="b1", tenant="t2")).status_code == 201                                  # tenant t2 has its own allowance


async def test_a_deleted_voice_frees_its_slot_and_its_name(make_env):
    e = await make_env(voice_studio_max_voices=1)
    await enable(e)
    vid = await make_voice(e, name="asha")
    assert (await upload(e, name="other")).status_code == 429
    assert (await e.c.delete(f"/v1/voices/{vid}", headers=ADMIN())).status_code == 200
    assert (await upload(e, name="asha")).status_code == 201


async def test_previews_per_hour_and_text_length_and_language(make_env):
    e = await make_env(voice_studio_previews_per_hour=2)
    await enable(e)
    await enable(e, "t2")
    vid = await make_voice(e)
    vid2 = await make_voice(e, "t2")
    assert (await run_preview(e, vid))["status"] == "done" and (await run_preview(e, vid))["status"] == "done"
    r = await preview(e, vid)
    assert r.status_code == 429 and r.json()["error"]["code"] == "voice_limit" and r.json()["error"]["details"]["limit"] == "previews_per_hour"
    assert (await run_preview(e, vid2, tenant="t2"))["status"] == "done"                                 # another tenant is not affected
    n = len(e.fake.log)
    assert (await preview(e, vid)).status_code == 429 and len(e.fake.log) == n                           # a refused preview sends nothing to the worker


async def test_text_rules(make_env):
    e = await make_env()
    await enable(e)
    vid = await make_voice(e)
    assert (await preview(e, vid, text="   ")).status_code == 422
    r = await preview(e, vid, text="a" * 301)
    assert r.status_code == 422 and r.json()["error"]["code"] == "text_too_long" and r.json()["error"]["details"]["max_chars"] == 300
    assert (await preview(e, vid, text="a" * 300)).status_code == 202
    r = await preview(e, vid, language="klingon")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_language"
    assert (await e.c.post(f"/v1/voices/{vid}/preview", json={"text": "x", "voice": "other"}, headers=ADMIN())).status_code == 422      # no extra fields
    assert (await e.c.post(f"/v1/voices/{vid}/preview", json={"text": "x" * 5000}, headers=ADMIN())).status_code == 422


async def test_only_so_many_previews_run_at_once_per_tenant(make_env):
    e = await make_env(voice_studio_max_running=1)
    await enable(e)
    vid = await make_voice(e)
    e.fake.hold = "queue"
    first = (await preview(e, vid)).json()["job_id"]
    r = await preview(e, vid)
    assert r.status_code == 429 and r.json()["error"]["details"]["limit"] == "max_running"
    e.fake.hold = None
    await until_status(e, first, "done")
    assert (await preview(e, vid)).status_code == 202


# ================================================================== the job lifecycle
async def test_a_preview_answers_at_once_then_moves_through_queued_warming_speaking_done(make_env):
    e = await make_env()
    await enable(e)
    vid = await make_voice(e)
    e.fake.voices.add(f"v_{vid}")                                    # the worker already has this voice: a plain synth
    e.fake.hold = "queue"
    r = await preview(e, vid)
    assert r.status_code == 202
    j = r.json()
    assert j["status"] == "queued" and j["job_id"] and j["audio"] is None and j["error"] is None
    assert (await until_status(e, j["job_id"], "warming"))["detail"].startswith("Warming up the voice server") and "minute" in (await status(e, j["job_id"]))["detail"]
    assert (await e.c.get(f"/v1/voices/previews/{j['job_id']}/audio", headers=ADMIN())).status_code == 409         # not ready yet
    e.fake.hold = "run"
    assert (await until_status(e, j["job_id"], "speaking"))["detail"] == "The voice is speaking."
    e.fake.hold = None
    done = await until_status(e, j["job_id"], "done")
    assert done["audio"]["url"] == f"/v1/voices/previews/{j['job_id']}/audio" and done["audio"]["duration_s"] == pytest.approx(0.4, abs=0.05) and done["elapsed_s"] >= 0
    a = await e.c.get(done["audio"]["url"], headers=ADMIN())
    assert a.status_code == 200 and a.headers["content-type"] == "audio/wav" and a.content[:4] == b"RIFF" and a.headers["cache-control"] == "no-store"
    assert wav_info(a.content)["rate"] == 24000
    assert e.fake.kinds() == ["synth"]                               # no registration was needed


async def test_the_first_preview_registers_the_clip_once_after_needs_reference_and_the_next_does_not(make_env):
    e = await make_env()
    await enable(e)
    vid = await make_voice(e)
    stored = (await e.db["voices"].find_one({"_id": vid}))
    st = await run_preview(e, vid)
    assert st["status"] == "done"
    assert e.fake.kinds() == ["synth", "register", "synth"]          # asked, got needs_reference, sent the clip ONCE, asked again
    reg = e.fake.log[1]
    assert reg["name"] == f"v_{vid}" and reg["sha256"] == stored["checksum"] and reg["info"]["rate"] == 24000 and reg["info"]["channels"] == 1
    assert (await run_preview(e, vid))["status"] == "done"
    assert e.fake.kinds() == ["synth", "register", "synth", "synth"]  # the worker has the voice now: no second registration
    # a worker restart forgets the voice: one more registration, then it works again
    e.fake.voices = {"utkarsh"}
    assert (await run_preview(e, vid))["status"] == "done" and e.fake.kinds().count("register") == 2


async def test_needs_reference_twice_ends_the_job_and_the_clip_is_not_sent_a_second_time(make_env):
    e = await make_env()
    await enable(e)
    vid = await make_voice(e)
    e.fake.always_needs_reference = True
    st = await run_preview(e, vid)
    assert st["status"] == "error" and st["error"]["code"] == "voice_unavailable" and st["audio"] is None
    assert e.fake.kinds() == ["synth", "register", "synth"]          # exactly one re-send of the clip


async def test_an_old_worker_without_voice_studio_support_is_named_as_such(make_env):
    e = await make_env()
    await enable(e)
    vid = await make_voice(e)
    e.fake.legacy = True
    st = await run_preview(e, vid)
    assert st["status"] == "error" and st["error"]["code"] == "worker_outdated"
    assert "register" not in e.fake.kinds()


async def test_phone_quality_goes_through_mulaw_8khz_like_calls_and_the_text_is_normalised_like_calls(make_env):
    e = await make_env()
    await enable(e)
    vid = await make_voice(e)
    e.fake.voices.add(f"v_{vid}")
    plain = await run_preview(e, vid, text="aapki fees 45000 rupees hai", phone_quality=False)
    phone = await run_preview(e, vid, text="aapki fees 45000 rupees hai", phone_quality=True)
    sent_plain, sent_phone = e.fake.log[0]["input"], e.fake.log[1]["input"]
    assert sent_plain["output_format"] == "wav" and sent_phone["output_format"] == "ulaw8k"
    assert sent_plain["text"] == sent_phone["text"] and "45000" not in sent_plain["text"] and sent_plain["language"] == "hi" and sent_plain["voice"] == f"v_{vid}"
    assert set(sent_plain) == {"text", "language", "voice", "output_format"}
    a_plain = await e.c.get(plain["audio"]["url"], headers=ADMIN())
    a_phone = await e.c.get(phone["audio"]["url"], headers=ADMIN())
    assert wav_info(a_plain.content)["rate"] == 24000 and wav_info(a_phone.content)["rate"] == 8000
    assert phone["phone_quality"] is True and phone["audio"]["duration_s"] == pytest.approx(0.4, abs=0.05)


async def test_a_preview_cannot_start_without_a_consent_record_or_for_a_disabled_voice(make_env):
    e = await make_env()
    await enable(e)
    vid = await make_voice(e)
    await e.db["voices"].update_one({"_id": vid}, {"$unset": {"consent_ref": ""}})              # a record that got there some other way
    r = await preview(e, vid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "consent_required" and e.fake.log == []
    await e.db["voices"].update_one({"_id": vid}, {"$set": {"consent_ref": {"speaker_name": "", "date": "2026-01-01", "scope": "preview_only"}}})
    assert (await preview(e, vid)).status_code == 409                                              # an empty speaker name is not consent either
    await e.db["voices"].update_one({"_id": vid}, {"$set": {"consent_ref": {"speaker_name": "Asha", "date": "2026-01-01", "scope": "preview_only"}}})
    assert (await run_preview(e, vid))["status"] == "done"
    r = await e.c.post(f"/v1/voices/{vid}/disable", headers=ADMIN())
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    r = await preview(e, vid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "voice_not_active"


async def test_delete_erases_the_clip_keeps_a_tombstone_and_the_voice_is_gone(make_env):
    e = await make_env()
    await enable(e)
    vid = await make_voice(e)
    assert await e.db["voice_clips.files"].count_documents({}) == 1
    r = await e.c.delete(f"/v1/voices/{vid}", headers=ADMIN())
    assert r.status_code == 200 and r.json() == {"id": vid, "status": "deleted"}
    assert await e.db["voice_clips.files"].count_documents({}) == 0 and await e.db["voice_clips.chunks"].count_documents({}) == 0
    tomb = await e.db["voices"].find_one({"_id": vid})
    assert tomb["status"] == "deleted" and tomb["gridfs_id"] is None and tomb["consent_ref"]["speaker_name"] == "Asha Verma"
    assert (await e.c.get("/v1/voices", headers=ADMIN())).json()["items"] == []
    assert (await preview(e, vid)).status_code == 404 and (await e.c.delete(f"/v1/voices/{vid}", headers=ADMIN())).status_code == 404


async def test_a_worker_failure_ends_the_job_with_a_clear_code(make_env):
    e = await make_env()
    await enable(e)
    vid = await make_voice(e)
    e.fake.run_status = 500
    st = await run_preview(e, vid)
    assert st["status"] == "error" and st["error"]["code"] == "worker_http_error" and st["error"]["message"]
    assert "Traceback" not in json.dumps(st)


async def test_the_deadline_ends_the_job_and_cancels_the_worker_job(make_env):
    e = await make_env(voice_studio_job_deadline_s=0.4)
    await enable(e)
    vid = await make_voice(e)
    e.fake.hold = "queue"                                             # a cold start that never ends
    st = await run_preview(e, vid)
    assert st["status"] == "error" and st["error"]["code"] == "deadline_exceeded"
    for _ in range(100):
        if e.fake.cancelled:
            break
        await asyncio.sleep(0.01)
    assert e.fake.cancelled == ["job-1"]                              # the RunPod job was cancelled so it stops billing


async def test_the_audio_is_dropped_after_its_time_and_the_status_stays(make_env):
    e = await make_env(voice_studio_result_ttl_s=0.1)
    await enable(e)
    vid = await make_voice(e)
    e.fake.voices.add(f"v_{vid}")
    st = await run_preview(e, vid)
    await asyncio.sleep(0.25)
    r = await e.c.get(st["audio"]["url"], headers=ADMIN())
    assert r.status_code == 410 and r.json()["error"]["code"] == "preview_expired"
    assert (await status(e, st["job_id"]))["status"] == "done"


async def test_the_tts_endpoint_must_be_configured_and_be_a_runpod_runsync_url(make_env):
    e = await make_env()
    await enable(e, tts=None)
    vid = await make_voice(e)
    r = await preview(e, vid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "tts_not_configured"
    await e.c.put("/v1/ai/config", json={"tts_url": "http://tts.test/v1/audio/speech"}, headers=ADMIN())
    r = await preview(e, vid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "tts_not_runpod"
    assert e.fake.log == [] and await e.app.state.voices.store.spend_get("t1") == 0       # nothing was sent and nothing was reserved


# ================================================================== spend counter and hard cap
async def test_the_spend_counter_replaces_the_estimate_by_the_measured_cost(make_env):
    e = await make_env(voice_studio_spend_cap_usd=1.0)
    await enable(e)
    vid = await make_voice(e)
    e.fake.voices.add(f"v_{vid}")
    await run_preview(e, vid, text="a" * 20)
    assert await e.app.state.voices.store.spend_get("t1") == 750                              # (1000 + 500) ms x 0.0005 USD/s = 750 millionths
    u = (await e.c.get("/v1/voices", headers=ADMIN())).json()["usage"]
    assert u["spend_usd_this_month"] == pytest.approx(0.00075, abs=1e-4) and u["spend_cap_usd"] == 1.0 and u["previews_last_hour"] == 1


async def test_the_hard_cap_refuses_a_preview_before_anything_is_sent(make_env):
    e = await make_env(voice_studio_spend_cap_usd=0.05)               # one 20-character preview is reserved at 0.0335
    await enable(e)
    vid = await make_voice(e)
    e.fake.voices.add(f"v_{vid}")
    e.fake.report_time = False                                        # the worker reports no time: the estimate stands
    assert (await run_preview(e, vid, text="a" * 20))["status"] == "done"
    assert await e.app.state.voices.store.spend_get("t1") == 33500
    sent = len(e.fake.log)
    r = await preview(e, vid, text="a" * 20)
    assert r.status_code == 429 and r.json()["error"]["code"] == "spend_cap_reached" and r.json()["error"]["details"]["cap_usd"] == 0.05
    assert len(e.fake.log) == sent and await e.app.state.voices.store.spend_get("t1") == 33500


async def test_a_cap_of_zero_blocks_every_preview_and_the_cap_is_per_tenant(make_env):
    e = await make_env(voice_studio_spend_cap_usd=0)
    await enable(e)
    vid = await make_voice(e)
    r = await preview(e, vid)
    assert r.status_code == 429 and r.json()["error"]["code"] == "spend_cap_reached" and e.fake.log == []
    e2 = await make_env(voice_studio_spend_cap_usd=1.0)
    await enable(e2, "t2")
    vid2 = await make_voice(e2, "t2")
    e2.fake.voices.add(f"v_{vid2}")
    assert (await run_preview(e2, vid2, tenant="t2"))["status"] == "done"


async def test_concurrent_previews_cannot_slip_past_the_cap(make_env):
    e = await make_env(voice_studio_spend_cap_usd=0.05, voice_studio_max_running=5)       # room for exactly one reservation of 0.0335
    await enable(e)
    vid = await make_voice(e)
    e.fake.hold = "queue"
    rs = await asyncio.gather(*(preview(e, vid, text="a" * 20) for _ in range(4)))
    assert sorted(r.status_code for r in rs) == [202, 429, 429, 429]
    e.fake.hold = None


async def test_a_job_that_never_reached_the_gpu_gives_its_reservation_back(make_env):
    e = await make_env(voice_studio_spend_cap_usd=1.0)
    await enable(e)
    vid = await make_voice(e)
    e.fake.run_status = 500
    assert (await run_preview(e, vid))["status"] == "error"
    assert await e.app.state.voices.store.spend_get("t1") == 0


# ================================================================== audit and logs: no audio, no text
async def test_logs_audit_and_database_hold_no_typed_text_and_no_audio(make_env):
    e = await make_env()
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage() + " " + str(record.args) + " " + (record.exc_text or ""))

    root, ours = logging.getLogger(), logging.getLogger("mme_telephony")
    keep, old_level = Keep(), ours.level
    root.addHandler(keep)
    ours.setLevel(logging.DEBUG)                                  # everything the service logs (not the database driver's own debug output)
    try:
        await enable(e)
        clip = good_clip()
        vid = await make_voice(e, wav=clip, speaker="Zq Speakername")
        done = await run_preview(e, vid, text=f"namaste {SENTINEL} fees 45000", phone_quality=True)
        assert done["status"] == "done"
        audio = (await e.c.get(done["audio"]["url"], headers=ADMIN())).content
        e.fake.run_status = 500
        await run_preview(e, vid, text=f"phir se {SENTINEL}")
        await e.c.post(f"/v1/voices/{vid}/disable", headers=ADMIN())
    finally:
        root.removeHandler(keep)
        ours.setLevel(old_level)
    logs = "\n".join(records)
    audit = json.dumps([{k: v for k, v in d.items() if k != "_id"} for d in await e.db["audit"].find({}).to_list(100)], default=str)
    ring = json.dumps(e.app.state.errlog.snapshot(500), default=str)
    secrets = [SENTINEL, "45000", "namaste", base64.b64encode(clip)[:60].decode(), base64.b64encode(audio)[:60].decode(), clip[100:140].hex(), audio[100:140].hex()]
    for place, blob in (("log", logs), ("audit", audit), ("error ring", ring)):
        for s in secrets:
            assert s not in blob, f"{s[:12]!r} found in the {place}"
    assert "Zq Speakername" not in logs and "Zq Speakername" not in audit                       # the speaker's name lives in the voice record only
    assert "voice preview job=" in logs and "voice uploaded" in logs                            # the service does log: ids, codes, counts
    actions = [d["action"] for d in await e.db["audit"].find({"action": {"$regex": "^voice\\."}}).sort("ts", 1).to_list(20)]
    assert actions == ["voice.upload", "voice.preview", "voice.preview", "voice.disable"]
    pv = await e.db["audit"].find_one({"action": "voice.preview"})
    assert set(pv["details"]) == {"job_id", "chars", "language", "phone_quality"} and pv["target"] == vid
    up = await e.db["audit"].find_one({"action": "voice.upload"})
    assert set(up["details"]) == {"ok", "duration_s", "scope"} and up["principal"] == "u1" and up["tenant_id"] == "t1"
    assert SENTINEL not in json.dumps([d for c in ("voices", "voice_usage", "voice_spend") for d in await e.db[c].find({}).to_list(100)], default=str)
    assert not [x for x in e.fake.log if x["kind"] == "register" and x["name"] != f"v_{vid}"]   # the worker only ever saw the opaque voice name


async def test_a_deleted_voice_leaves_an_audit_trail_without_audio(make_env):
    e = await make_env()
    await enable(e)
    vid = await make_voice(e)
    await e.c.delete(f"/v1/voices/{vid}", headers=ADMIN())
    d = await e.db["audit"].find_one({"action": "voice.delete"})
    assert d["target"] == vid and d["details"] == {}
