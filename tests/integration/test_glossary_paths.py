"""The legacy path (Conversation.stream_turn) and the streaming path (run_streaming_turn) correct the caller's text identically, on the real service stack (Plivo stream handler,
MongoDB, AiService) with MOCK Plivo and MOCK models. The Brain request carries the corrected text; the same inputs go through both paths, including inputs that must NOT change.
SIMULATED; NOT RUN. Generic course sentences only."""
import re

import pytest

from tests.integration.test_plivo_voice import pv, recv_until, send_media, until, utterance  # noqa: F401
from tests.integration.test_streaming_live import ADMIN, _call, _enable

CASES = [("edm ke liye fees batao", "B.Tech ke liye fees batao"),
         ("edm kya hai", "B.Tech kya hai"),
         ("i love edm music festivals", "i love edm music festivals"),            # no cue: must NOT change
         ("namaste aap kaise hain", "namaste aap kaise hain"),                      # must NOT change
         ("mujhe beetaik karna hai", "mujhe B.Tech karna hai")]                     # the always-on mapping (no cue needed)


def _brain_user_text(pv):
    brains = [b for k, b in pv.models.calls if k == "brain"]
    assert brains, "no Brain request was made"
    users = [m["content"] for m in brains[0]["messages"] if m.get("role") == "user"]
    return users[-1]


@pytest.mark.parametrize("streaming", [False, True], ids=["legacy_path", "streaming_path"])
@pytest.mark.parametrize("raw,expected", CASES)
async def test_both_paths_send_the_same_corrected_text_to_the_brain(pv, streaming, raw, expected):
    pv.models.stt_text = raw
    if streaming:
        pv.models.brain_sse = True
        await _enable(pv, glossary_correction=True)
    else:
        r = await pv.c.put("/v1/ai/config", json={"glossary_correction": True}, headers=ADMIN())
        assert r.status_code == 200, r.text
    a, ws = await _call(pv, f"GL-{int(streaming)}-{abs(hash(raw)) % 10000}")
    await send_media(ws, utterance())
    await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=20)
    sent = _brain_user_text(pv)
    assert expected in sent                                                      # the Brain got the corrected (or unchanged) text
    if expected != raw:
        assert not re.search(r"\bedm\b|\bbeetaik\b", sent)                       # and not the raw sound-alike
    await ws.close()


@pytest.mark.parametrize("streaming", [False, True], ids=["legacy_path", "streaming_path"])
async def test_with_the_flag_off_neither_path_corrects_anything(pv, streaming):
    pv.models.stt_text = "edm ke liye fees batao"
    if streaming:
        pv.models.brain_sse = True
        await _enable(pv)
    a, ws = await _call(pv, f"GL-OFF-{int(streaming)}")
    await send_media(ws, utterance())
    await recv_until(ws, lambda m: m.get("event") == "checkpoint", timeout=20)
    assert "edm ke liye fees batao" in _brain_user_text(pv)
    await ws.close()
