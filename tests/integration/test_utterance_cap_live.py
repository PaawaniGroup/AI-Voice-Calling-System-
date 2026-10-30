"""The utterance cap on the real stack (Plivo stream handler, serve_loop, MongoDB) with MOCK Plivo and MOCK models. SIMULATED; NOT RUN.
Flag OFF (default): speech past 6 s is split at the cap. Flag ON with utterance_max_s: one utterance up to that maximum, labelled `silence`, never `cap`."""
import json

from tests.helpers import pcm_silence, pcm_tone
from tests.integration.test_evidence_reasons import _lines, _setup
from tests.integration.test_plivo_voice import RATE, pv, recv_until, send_media, until  # noqa: F401
from tests.integration.test_streaming_live import ADMIN, _call


async def _speak_7_seconds(pv, name):
    a, ws = await _call(pv, name)
    await send_media(ws, pcm_tone(7000, RATE, amp=6000) + pcm_silence(900, RATE))
    return ws


async def test_flag_off_speech_past_six_seconds_is_split_at_the_cap(pv, monkeypatch, tmp_path):
    f = await _setup(pv, monkeypatch, tmp_path, tts_ms=300)
    cfg = (await pv.c.get("/v1/ai/config", headers=ADMIN())).json()
    assert cfg["utterance_cap_raise"] is False
    ws = await _speak_7_seconds(pv, "CAP-OFF")

    async def got():
        return [x for x in _lines(f) if x["turn"].get("endpoint_reason") == "cap"] or None
    assert await until(got, timeout=15)
    await ws.close()


async def test_flag_on_with_a_nine_second_maximum_keeps_seven_seconds_as_one_utterance(pv, monkeypatch, tmp_path):
    f = await _setup(pv, monkeypatch, tmp_path, tts_ms=300)
    r = await pv.c.put("/v1/ai/config", json={"utterance_cap_raise": True, "utterance_max_s": 9.0}, headers=ADMIN())
    assert r.status_code == 200, r.text
    ws = await _speak_7_seconds(pv, "CAP-ON")

    async def got():
        ls = _lines(f)
        return ls if any(x["marks_ms"].get("t_first_frame_to_plivo") is not None for x in ls) else None
    ls = await until(got, timeout=20)
    assert ls is not None
    assert not any(x["turn"].get("endpoint_reason") == "cap" for x in ls)                   # no cap split
    final = next(x for x in ls if x["marks_ms"].get("t_first_frame_to_plivo") is not None)
    assert final["turn"]["endpoint_reason"] == "silence" and final["turn"]["pieces_merged"] == 1 and final["turn"]["utterance_ms"] >= 6900
    await ws.close()


async def test_a_value_outside_6_to_12_is_refused_by_the_config_api(pv):
    for bad in (5.0, 12.5):
        r = await pv.c.put("/v1/ai/config", json={"utterance_max_s": bad}, headers=ADMIN())
        assert r.status_code == 422
