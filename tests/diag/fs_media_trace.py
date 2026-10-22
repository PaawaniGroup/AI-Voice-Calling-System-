"""Diagnostic harness for the real-FreeSWITCH AI media path (dev tool, NOT a test; not collected by pytest).

Repeats the scenario of tests/integration/test_ai_freeswitch.py (local loopback call, tone "caller", MOCK models, real FreeSWITCH
with the real mod_audio_stream) and stamps every milestone on ONE monotonic clock (`t` seconds since the iteration started):
  test steps, controller ESL events, ESL commands and replies, media WebSocket handshake/hello/first frame/loud-quiet
  transitions/frame gaps, conversation turns, playback hand-off, and (from a separate raw ESL tap) FreeSWITCH-side
  CHANNEL_EXECUTE / PLAYBACK / RECORD / mod_audio_stream events. The caller-side audio is measured independently from the
  recording of what the caller hears (RECORD_READ_ONLY), including its dominant frequency (reply tone 440 Hz vs caller tone 300 Hz).

Every iteration writes a JSON bundle (full timeline + FreeSWITCH log slice for failures). Nothing here reaches a carrier.

Usage (WSL, repo root):  python tests/diag/fs_media_trace.py --n 20 --out /tmp/fsmedia_diag/run1
The caller is the rig's: extension 9010 (answer, record, sleep 1.5 s, play a 1.2 s tone). The monotonic clock is used for every
wait; the LEGACY wall-clock waits and recording filter run as passive shadows so their failures can be correlated with clock steps.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import struct
import sys
import time
import uuid
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402
import app.ai.pipeline as pipeline  # noqa: E402
import app.ai.ws as wsmod  # noqa: E402
from app.main import create_app  # noqa: E402
from app.persistence.db import Database  # noqa: E402
from app.telephony.esl import parse_plain_event, read_message  # noqa: E402
from esl_check import esl_password  # noqa: E402
from tests.conftest import auth, call_body, idem, make_settings  # noqa: E402
from tests.helpers import MockModels, live_server, pcm_tone  # noqa: E402

FS_LOG = Path("/usr/local/freeswitch/var/log/freeswitch/freeswitch.log")
CFG = {"stt_url": "http://stt.test:9000", "brain_url": "http://brain.test:9000", "tts_url": "http://tts.test:9000", "enabled": True}
ADMIN = lambda: auth("t1", ("telephony:admin",))  # noqa: E731


class Trace:
    def __init__(self):
        self.t0 = time.monotonic()
        self.wall0 = time.time()
        self.rows: list[dict] = []

    def now(self) -> float:
        return time.monotonic() - self.t0

    def mark(self, src: str, ev: str, **kv):
        self.rows.append({"t": round(self.now(), 4), "src": src, "ev": ev, **kv})


# --------------------------------------------------------------------------- FreeSWITCH side tap (separate ESL connection)
class Tap:
    EVENTS = ("CHANNEL_CREATE CHANNEL_ANSWER CHANNEL_BRIDGE CHANNEL_HANGUP CHANNEL_EXECUTE CHANNEL_EXECUTE_COMPLETE PLAYBACK_START "
              "PLAYBACK_STOP RECORD_START RECORD_STOP CUSTOM mod_audio_stream::connect mod_audio_stream::disconnect "
              "mod_audio_stream::error mod_audio_stream::play")

    def __init__(self, tr: Trace):
        self.tr, self.task, self.w = tr, None, None
        self.record_paths: list[str] = []
        self.uuids: set[str] = set()

    async def start(self):
        r, self.w = await asyncio.open_connection("127.0.0.1", 8021)
        await read_message(r)
        self.w.write(f"auth {esl_password()}\n\n".encode())
        await self.w.drain()
        await read_message(r)
        self.w.write(f"event plain {self.EVENTS}\n\n".encode())
        await self.w.drain()
        self.task = asyncio.create_task(self._loop(r))
        await asyncio.sleep(0.2)

    async def _loop(self, r):
        try:
            while True:
                hdr, body = await read_message(r)
                if hdr.get("Content-Type") != "text/event-plain":
                    continue
                ev = parse_plain_event(body)
                name = ev.get("Event-Name", "")
                u = ev.get("Unique-ID", "")
                if name == "RECORD_START" and ev.get("Record-File-Path"):
                    self.record_paths.append(ev["Record-File-Path"])
                if u:
                    self.uuids.add(u)
                self.tr.mark("fs", name, sub=(ev.get("Event-Subclass") or "").split("::")[-1], uuid=u[:8],
                             chan=ev.get("Channel-Name", "")[:30], app=ev.get("Application", ""), data=(ev.get("Application-Data") or "")[:50],
                             fsts=ev.get("Event-Date-Timestamp", ""))
        except Exception:
            pass

    async def stop(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(BaseException):
                await self.task
        if self.w:
            self.w.close()


# --------------------------------------------------------------------------- instrumentation of the app under test
class Instr:
    """Monkeypatches (restored in undo()) that only observe: they never change behaviour or timing beyond a list append."""

    def __init__(self, app, tr: Trace):
        self.app, self.tr, self.undo_list = app, tr, []
        self.frames = 0
        self.loud_frames = 0
        self.last_frame_t = None
        self.loud = False

    def patch(self, obj, name, new):
        old = getattr(obj, name)
        setattr(obj, name, new)
        self.undo_list.append((obj, name, old))
        return old

    def undo(self):
        for obj, name, old in reversed(self.undo_list):
            setattr(obj, name, old)

    def install(self):
        tr, app = self.tr, self.app
        ctrl, ai = app.state.controller, app.state.ai

        orig_handler = ctrl.esl_events.handler

        async def on_ev(ev):
            n = ev.get("Event-Name", "")
            if n == "HEARTBEAT":
                return await orig_handler(ev)
            tr.mark("ctrl", "esl_in", name=n, sub=(ev.get("Event-Subclass") or "").split("::")[-1], uuid=ev.get("Unique-ID", "")[:8])
            await orig_handler(ev)
            tr.mark("ctrl", "esl_done", name=n, sub=(ev.get("Event-Subclass") or "").split("::")[-1])
        self.patch(ctrl.esl_events, "handler", on_ev)

        orig_api = ctrl.esl_cmd.api

        async def api(cmd):
            parts = cmd.split(" ")
            verb, u = parts[0], (parts[1][:8] if len(parts) > 1 else "")
            tr.mark("esl_cmd", "send", verb=verb, uuid=u)
            try:
                out = await orig_api(cmd)
            except Exception as e:
                tr.mark("esl_cmd", "error", verb=verb, err=type(e).__name__, msg=str(e)[:80])
                raise
            tr.mark("esl_cmd", "reply", verb=verb, out=out[:60].replace("\n", " "))  # replies never contain secrets (commands may: not logged)
            return out
        self.patch(ctrl.esl_cmd, "api", api)

        orig_rms = wsmod.rms

        def rms(frame):
            v = orig_rms(frame)
            now = tr.now()
            self.frames += 1
            if self.frames == 1:
                tr.mark("ws", "first_frame", bytes=len(frame))
            if self.last_frame_t is not None and now - self.last_frame_t > 0.06:
                tr.mark("ws", "frame_gap", gap=round(now - self.last_frame_t, 3))
            self.last_frame_t = now
            loud = v >= wsmod.VAD_RMS
            self.loud_frames += 1 if loud else 0
            if loud != self.loud:
                self.loud = loud
                tr.mark("ws", "loud" if loud else "quiet", rms=int(v), frame_no=self.frames)
            return v
        self.patch(wsmod, "rms", rms)

        orig_h = wsmod.handle_fs_media

        async def handle(ws, app_, **kw):
            tr.mark("ws", "handshake_in", has_auth=bool(ws.headers.get("authorization")))
            try:
                return await orig_h(ws, app_, **kw)
            finally:
                tr.mark("ws", "handler_returned")
        self.patch(wsmod, "handle_fs_media", handle)

        for meth in ("start", "attach_media", "media_connected", "media_disconnected", "_finish"):
            self._wrap_async(ai, meth, "ai")

        orig_turn = pipeline.Conversation.turn

        async def turn(selfc, pcm, rate):
            tr.mark("conv", "turn_start", pcm_ms=len(pcm) * 1000 // (2 * rate))
            res = await orig_turn(selfc, pcm, rate)
            tr.mark("conv", "turn_end", error=res.error, heard=bool(res.heard), audio=len(res.audio or b""))
            return res
        self.patch(pipeline.Conversation, "turn", turn)

        orig_play = wsmod.FsIO.play

        async def play(selfio, audio, src_rate, language, uid, on_start):
            tr.mark("io", "play_requested", bytes=len(audio), rate=src_rate, ms=len(audio) * 500 // src_rate)
            await orig_play(selfio, audio, src_rate, language, uid, on_start)
            tr.mark("io", "play_sent_to_module")
        self.patch(wsmod.FsIO, "play", play)

        orig_int = wsmod.FsIO.interrupt

        async def interrupt(selfio):
            tr.mark("io", "interrupt")
            await orig_int(selfio)
        self.patch(wsmod.FsIO, "interrupt", interrupt)

    def _wrap_async(self, obj, name, src):
        orig = getattr(obj, name)
        tr = self.tr

        async def w(*a, **kw):
            tr.mark(src, name + ":enter")
            try:
                r = await orig(*a, **kw)
                tr.mark(src, name + ":exit")
                return r
            except Exception as e:
                tr.mark(src, name + ":raised", err=type(e).__name__)
                raise
        self.patch(obj, name, w)


# --------------------------------------------------------------------------- caller-side audio measurement
def analyse_heard(path: str) -> dict:
    """Independent measurement from the recording of what the caller hears: audible segments (20 ms windows, RMS > 800),
    total audible seconds, peak, and dominant frequency by zero crossings (reply tone = 440 Hz, caller tone = 300 Hz)."""
    out = {"path": path, "exists": os.path.exists(path)}
    if not out["exists"]:
        return out
    try:
        with wave.open(path, "rb") as w:
            rate, n = w.getframerate(), w.getnframes()
            data = w.readframes(n)
    except Exception as e:
        out["error"] = type(e).__name__
        return out
    xs = struct.unpack("<%dh" % (len(data) // 2), data)
    win = max(1, rate // 50)
    segs, cur = [], None
    loud = 0
    for i in range(0, len(xs) - win, win):
        v = (sum(x * x for x in xs[i:i + win]) / win) ** 0.5
        if v > 800:
            loud += 1
            cur = [i / rate, (i + win) / rate] if cur is None else [cur[0], (i + win) / rate]
        elif cur is not None:
            segs.append(cur)
            cur = None
    if cur is not None:
        segs.append(cur)
    zc = freq = None
    if segs:
        a, b = int(segs[0][0] * rate), int(segs[0][1] * rate)
        seg = xs[a:b]
        zc = sum(1 for k in range(1, len(seg)) if (seg[k - 1] < 0) != (seg[k] < 0))
        dur = (b - a) / rate
        freq = round(zc / 2 / dur) if dur > 0 else None
    out.update(rate=rate, seconds=round(len(xs) / rate, 2), audible_s=round(loud * 0.02, 2), peak=max((abs(x) for x in xs), default=0),
               segments=[[round(a, 2), round(b, 2)] for a, b in segs][:8], first_segment_freq_hz=freq)
    return out


def write_caller_wav(path="/tmp/mme_ai_caller.wav", ms=1200, rate=8000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm_tone(ms, rate, amp=9000))
    os.chmod(path, 0o644)


def log_slice(offset: int, uuids: set[str]) -> list[str]:
    try:
        with open(FS_LOG, "rb") as f:
            f.seek(offset)
            text = f.read().decode("utf-8", "replace")
    except OSError as e:
        return [f"<log unreadable: {type(e).__name__}>"]
    keys = [u for u in uuids if u] + ["audio_stream", "mod_audio", "streamAudio", "STREAM_EXTRA"]
    lines = [ln for ln in text.splitlines() if any(k in ln for k in keys)]
    return lines[-400:]


def log_size() -> int:
    try:
        return FS_LOG.stat().st_size
    except OSError:
        return 0


# --------------------------------------------------------------------------- the scenario
async def run_iteration(idx: int, variant: str, outdir: Path) -> dict:
    tr = Trace()
    dbname = f"mmetel_diag_{uuid.uuid4().hex[:10]}"
    s0 = make_settings(mongo_db=dbname)
    db = Database(s0)
    await db.connect()
    models = MockModels()
    facts: dict = {"idx": idx, "variant": variant}
    facts["clock_offset_begin"] = round(time.time() - time.monotonic(), 2)  # wall minus monotonic; flips by ~51.7 s on this host
    since_legacy = time.time() - 1  # exactly what the legacy tests computed before starting the call
    legacy: dict = {}
    shadows: list[asyncio.Task] = []
    log_off = log_size()
    tap = Tap(tr)
    instr = None
    try:
        s = make_settings(mongo_db=dbname, fs_enabled=True, esl_password=esl_password(), provider="freeswitch_gateway",
                          fs_dial_template="loopback/9010/mme_test", ai_enabled=True, ai_media_bridge="ws_fork",
                          ai_allowed_hosts="stt.test,brain.test,tts.test", esl_reconnect_initial_s=0.2, esl_reconnect_max_s=1.0,
                          reconcile_interval_s=2.0, ring_timeout_s=20)
        app = create_app(s, db=db, ai_transport=models.transport())
        async with live_server(app) as host:
            ctrl, ai, repos = app.state.controller, app.state.ai, app.state.repos
            for _ in range(100):
                if ctrl.esl_events.connected:
                    break
                await asyncio.sleep(0.05)
            ready, reason = await ai.bridge.ready()
            facts["bridge_ready"] = ready
            if not ready:
                facts["outcome"] = "setup_failed:" + reason
                return facts
            s.ai_media_ws_url = f"ws://{host}/v1/ai/media/fs"
            await tap.start()
            instr = Instr(app, tr)
            instr.install()
            async with httpx.AsyncClient(base_url=f"http://{host}", timeout=15) as c:
                await repos.put_policy("t1", {"enabled": True, "caller_ids": ["+911140000000"], "transfer_destinations": ["+919999999999", "+9188*"]})
                assert (await c.put("/v1/ai/config", json=CFG, headers=ADMIN())).status_code == 200
                tr.mark("test", "begin")
                r = await c.post("/v1/calls", json=call_body(ai_enabled=True, language="hinglish"), headers={**auth(), **idem()})
                tr.mark("test", "post_calls_returned", status=r.status_code)
                call = r.json()
                got = None
                for _ in range(100):
                    got = await repos.get_call(call["id"])
                    if got["state"] == "answered":
                        break
                    await asyncio.sleep(0.1)
                tr.mark("test", "answered_seen", state=got["state"])
                facts["a_uuid"] = (got or {}).get("fs_uuid", "")
                sid = call["session_id"]
                tr.mark("test", "post_start")
                r = await c.post(f"/v1/ai/sessions/{sid}/start", json={}, headers=auth())
                tr.mark("test", "start_returned", status=r.status_code)
                shadows.append(asyncio.create_task(legacy_wait(db, sid, lambda a: a["state"] == "active", 10, legacy, "legacy_wait_active", tr)))
                active = await wait_for(db, sid, lambda a: a["state"] == "active", 10)
                tr.mark("test", "active" if active else "active_timeout")
                facts["active"] = bool(active)
                turn = await wait_for(db, sid, lambda a: a["turns"] >= 1, 10) if active else None
                facts["turn"] = bool(turn)
                play_evt = False
                if turn:
                    for _ in range(60):
                        if await db["call_events"].count_documents({"call_id": call["id"], "type": "media.play"}):
                            play_evt = True
                            break
                        await asyncio.sleep(0.1)
                facts["play_event"] = play_evt
                tr.mark("test", "play_event_seen" if play_evt else "play_event_timeout")
                await asyncio.sleep(1.2)
                with contextlib.suppress(Exception):
                    await ctrl.esl_cmd.api(f"uuid_kill {facts['a_uuid']} NORMAL_CLEARING")
                tr.mark("test", "killed")
                shadows.append(asyncio.create_task(
                    legacy_wait(db, sid, lambda a: a["state"] in ("stopped", "failed"), 10, legacy, "legacy_wait_stopped", tr)))
                await asyncio.sleep(1.0)
                await asyncio.gather(*shadows)
                with contextlib.suppress(Exception):
                    await ctrl.esl_cmd.api("hupall NORMAL_CLEARING mme_leg a")
            heard = analyse_heard(tap.record_paths[-1]) if tap.record_paths else {"exists": False, "note": "no RECORD_START seen"}
            facts["heard"] = heard
            facts.update(legacy)
            if heard.get("exists"):  # the legacy file selection: mtime >= (wall clock before the call) - 1
                facts["legacy_filter_includes_recording"] = os.path.getmtime(heard["path"]) >= since_legacy
                facts["mtime_minus_since_s"] = round(os.path.getmtime(heard["path"]) - since_legacy, 2)
            facts["clock_offset_end"] = round(time.time() - time.monotonic(), 2)
            facts["played_requested"] = any(x["ev"] == "play_requested" for x in tr.rows)
            facts["audible_ok"] = bool(heard.get("audible_s", 0) >= 0.2 and heard.get("peak", 0) > 2000)
    finally:
        if instr:
            instr.undo()
        await tap.stop()
        with contextlib.suppress(Exception):
            await db.client.drop_database(dbname)
            await db.close()
    facts["outcome"] = classify(facts, tr)
    facts["timeline"] = tr.rows
    facts["fs_log"] = log_slice(log_off, tap.uuids) if facts["outcome"] != "ok" else []
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"iter_{idx:03d}_{facts['outcome']}.json").write_text(json.dumps(facts, indent=1, default=str))
    return facts


async def wait_for(db, sid, pred, timeout):
    """Monotonic deadline (immune to wall-clock steps)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        a = await db["ai_sessions"].find_one({"session_id": sid})
        if a and pred(a):
            return a
        await asyncio.sleep(0.05)
    return None


async def legacy_wait(db, sid, pred, timeout, out: dict, key: str, tr: Trace):
    """SHADOW of tests/integration/test_ai_freeswitch.py::wait_ai as it was (wall-clock deadline). Observes only: records whether the
    legacy helper would have raised, and the wall-minus-monotonic offset when it gave up."""
    end = time.time() + timeout
    while time.time() < end:
        a = await db["ai_sessions"].find_one({"session_id": sid})
        if a and pred(a):
            out[key] = "ok"
            return
        await asyncio.sleep(0.1)
    out[key] = "LEGACY_TIMEOUT"
    tr.mark("legacy", key + "_gave_up", wall_minus_mono=round(time.time() - time.monotonic(), 2))


def first(rows, src, ev, **match):
    for r in rows:
        if r["src"] == src and r["ev"] == ev and all(r.get(k) == v for k, v in match.items()):
            return r["t"]
    return None


def classify(f: dict, tr: Trace) -> str:
    if not f.get("active"):
        return "no_connect"
    if not f.get("turn"):
        return "no_turn"
    if not f.get("play_event"):
        return "no_play_event"
    if not f.get("audible_ok"):
        return "played_but_inaudible"
    return "ok"


def summarize(results: list[dict]) -> dict:
    cats: dict[str, int] = {}
    for r in results:
        cats[r["outcome"]] = cats.get(r["outcome"], 0) + 1
    n = len(results)
    legacy_wait_fail = sum(1 for r in results if "LEGACY_TIMEOUT" in (r.get("legacy_wait_active"), r.get("legacy_wait_stopped")))
    legacy_filter_miss = sum(1 for r in results if r.get("legacy_filter_includes_recording") is False)
    return {"runs": n, "ok": cats.get("ok", 0), "failures": n - cats.get("ok", 0),
            "failure_rate": round((n - cats.get("ok", 0)) / n, 3) if n else None, "by_category": cats,
            "legacy_wait_would_have_failed": legacy_wait_fail, "legacy_recording_filter_would_have_missed": legacy_filter_miss}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--variant", default="orig")
    ap.add_argument("--out", default="/tmp/fsmedia_diag/run")
    a = ap.parse_args()
    write_caller_wav()
    out = Path(a.out)
    results = []
    for i in range(a.n):
        r = await run_iteration(i, a.variant, out)
        results.append(r)
        h = r.get("heard", {})
        print(f"iter {i:03d} {r['outcome']:22s} audible_s={h.get('audible_s')} freq={h.get('first_segment_freq_hz')}", flush=True)
    summ = summarize(results)
    (out / "summary.json").write_text(json.dumps(summ, indent=1))
    print(json.dumps(summ))


if __name__ == "__main__":
    asyncio.run(main())
