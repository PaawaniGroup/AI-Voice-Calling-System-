"""Devanagari flow (per-tenant flag `devanagari_hindi`): the Devanagari prompt, the per-turn `(reply_mode: hi|en)` tag, the reply check, ONE
bounded "rewrite in Devanagari" retry, and logs that carry kinds and counts only. Off by default: the default prompt is unchanged.
Model services are MOCKS."""
import json
import logging

from app.ai import pipeline
from app.ai.clients import ModelClients
from app.ai.pipeline import OLD_SYSTEM_PROMPT, SYSTEM_PROMPT_DEVANAGARI, Conversation
from app.ai.script_enforcement import check_reply, strip_mode_tag
from tests.conftest import make_settings
from tests.helpers import MockModels, pcm_tone

HINGLISH_Q = "mujhe BTech ke baare mein batao"
ENGLISH_Q = "what is the eligibility for the btech admission please"
ROMAN = "ZZQNAME ji aap kaise hain, main aapko batata hoon admission ke baare mein"       # Roman-letter Hindi
DEVA = "जी, आप कैसे हैं? मैं आपको admission के बारे में बताता हूँ।"
ENGLISH_REPLY = "You need at least 60% in 12th with Physics, Chemistry and Maths."


def _conv(models, **kw):
    s = make_settings(ai_allowed_hosts="stt.test,brain.test,tts.test")
    cfg = {"stt_url": "http://stt.test:9000", "brain_url": "http://brain.test:9000", "tts_url": "http://tts.test:9000"}
    clients = ModelClients(s, cfg, models.transport())
    return Conversation(clients, "hi", **kw), clients


async def _turn(conv, models):
    res = await conv.turn(pcm_tone(400, 16000), 16000)
    assert res.error is None, res.error
    return res


def _brain(models):
    return [b for k, b in models.calls if k == "brain"]


def _spoken(models):
    return [b["input"] for k, b in models.calls if k == "tts"]


# ------------------------------------------------------------------ prompt and flag
def test_default_prompt_is_unchanged_when_the_flag_is_off():
    assert Conversation(clients=None).messages[0]["content"] == OLD_SYSTEM_PROMPT
    assert Conversation(clients=None, devanagari_hindi=False).messages[0]["content"] == OLD_SYSTEM_PROMPT
    c = Conversation(clients=None)
    assert c.devanagari_hindi is False and c.enforce is False


def test_the_flag_selects_the_devanagari_prompt_with_the_operators_rules_the_earlier_examples_and_the_factual_guardrails():
    c = Conversation(clients=None, devanagari_hindi=True)
    p = c.messages[0]["content"]
    assert p == SYSTEM_PROMPT_DEVANAGARI and p.startswith(OLD_SYSTEM_PROMPT)
    for must in ("You are the voice counsellor of Make My Education", "Never write \"aap\", \"hai\", \"hoon\", \"han\", \"sakta\", \"chahiye\", \"baare\", \"main\".",
                 "EXAMPLES (caller -> you)", '"Haan, bataiye" -> "जी, ज़रूर।', "Short sentences, at most 20 words.",
                 "More examples of the script rule. Right: 'मैं आपको course के बारे में बता सकता हूँ।'", "Wrong: 'Main aapko course ke baare mein bata sakta hoon.'",
                 "(reply_mode: hi)", "NEVER say, write or mention the tag", "Never invent fees or deadlines", "provided CONTEXT"):
        assert must in p, must
    assert "override anything above about language and script" in p                  # no conflict with the legacy language sentence
    assert Conversation(clients=None, devanagari_hindi=True, script_enforcement=True).messages[0]["content"] == SYSTEM_PROMPT_DEVANAGARI
    assert c.devanagari_hindi and c.enforce and c.hint_style == "short"


# ------------------------------------------------------------------ the per-turn tag
async def test_the_tag_is_added_only_in_hi_or_en_mode_and_only_when_the_flag_is_on_and_never_stored():
    m = MockModels()
    m.reply = DEVA
    conv, clients = _conv(m, devanagari_hindi=True)
    await _turn(conv, m)                                                     # Hinglish caller -> hi
    m.stt_text, m.reply = ENGLISH_Q, ENGLISH_REPLY
    await _turn(conv, m)                                                     # English caller -> en
    await clients.aclose()
    b1, b2 = _brain(m)
    assert b1["messages"][-1]["content"].endswith("(reply_mode: hi)") and b2["messages"][-1]["content"].endswith("(reply_mode: en)")
    assert "reply_mode" not in json.dumps(conv.messages[1:], ensure_ascii=False)   # the history (after the system prompt that explains the tag) never carries it
    off = MockModels()
    conv2, clients2 = _conv(off)                                              # flag off: the legacy request, no tag
    await _turn(conv2, off)
    await clients2.aclose()
    assert "reply_mode" not in json.dumps(_brain(off), ensure_ascii=False)


async def test_a_tag_echoed_by_the_brain_is_never_spoken():
    assert strip_mode_tag("जी, बताइए। (reply_mode: hi)") == "जी, बताइए।" and strip_mode_tag("Yes. reply_mode: en") == "Yes."
    m = MockModels()
    m.reply = "जी, बताइए, आप किस कोर्स के बारे में जानना चाहते हैं? (reply_mode: hi)"
    conv, clients = _conv(m, devanagari_hindi=True)
    await _turn(conv, m)
    await clients.aclose()
    assert _spoken(m) and not any("reply_mode" in t for t in _spoken(m))


# ------------------------------------------------------------------ the check
def test_the_check_catches_roman_hindi_and_wrong_script():
    ok, problems = check_reply(ROMAN, "hi")
    assert not ok and any(p.startswith("roman_hindi_word:") for p in problems) and "no_devanagari_in_hi_mode" in problems
    assert check_reply(DEVA, "hi")[0]
    assert check_reply("जी, main आपकी help करूँगा, JEE और B.Tech के बारे में।", "hi")[0]       # homographs, acronyms, allow-listed terms are fine
    assert not check_reply("Please call us for the admission details of this course", "hi")[0]   # a Hindi caller got a Latin-only reply
    ok, problems = check_reply(DEVA, "en")
    assert not ok and problems == ["devanagari_in_en_mode"] and check_reply(ENGLISH_REPLY, "en")[0]


# ------------------------------------------------------------------ the retry
async def test_a_failing_reply_gets_one_rewrite_and_the_rewrite_is_what_is_spoken_and_remembered():
    m = MockModels()
    m.reply_queue = [ROMAN, DEVA]
    conv, clients = _conv(m, devanagari_hindi=True)
    await _turn(conv, m)
    await clients.aclose()
    brains = _brain(m)
    assert len(brains) == 2                                                                  # exactly one retry
    msgs = brains[1]["messages"]
    assert msgs[-2] == {"role": "assistant", "content": ROMAN} and "Rewrite your reply in Devanagari" in msgs[-1]["content"]
    assert _spoken(m) and all(any("ऀ" <= ch <= "ॿ" for ch in t) for t in _spoken(m)) and not any("ZZQNAME" in t for t in _spoken(m))
    assert [x["content"] for x in conv.messages if x["role"] == "assistant"] == [DEVA]       # the history keeps the corrected reply
    rep = conv.last_script_report
    assert rep["first_ok"] is False and rep["retried"] is True and rep["retry_ok"] is True and rep["kinds"].get("roman_hindi_word", 0) >= 3


async def test_a_reply_that_passes_costs_no_retry():
    m = MockModels()
    m.reply = DEVA
    conv, clients = _conv(m, devanagari_hindi=True)
    await _turn(conv, m)
    await clients.aclose()
    assert len(_brain(m)) == 1 and conv.last_script_report["first_ok"] and not conv.last_script_report["retried"]


async def test_english_mode_reply_with_devanagari_is_rewritten_in_english():
    m = MockModels()
    m.stt_text, m.reply_queue = ENGLISH_Q, ["Jee, aapko 60% chahiye. जी, बताइए।", ENGLISH_REPLY]
    conv, clients = _conv(m, devanagari_hindi=True)
    await _turn(conv, m)
    await clients.aclose()
    assert len(_brain(m)) == 2 and "fully in English" in _brain(m)[1]["messages"][-1]["content"]
    assert conv.last_script_report["retry_ok"] is True and _spoken(m) and all(ENGLISH_REPLY.split(",")[0] in t for t in _spoken(m)[:1])


async def test_a_retry_that_still_fails_is_spoken_as_it_is_and_only_counted(caplog):
    caplog.set_level(logging.DEBUG)
    m = MockModels()
    m.reply_queue = [ROMAN, "ZZQNAME ji phir se aap kaise hain, main aapko batata hoon"]
    conv, clients = _conv(m, devanagari_hindi=True)
    await _turn(conv, m)
    await clients.aclose()
    assert len(_brain(m)) == 2                                                                # one retry, never a loop
    assert _spoken(m) and "ZZQNAME" in _spoken(m)[0]                                          # the original is spoken: the caller always gets an answer
    rep = conv.last_script_report
    assert rep["retry_ok"] is False and rep["kinds_after_retry"]["roman_hindi_word"] >= 3
    logs = caplog.text
    assert "reply_script_check" in logs and "problem_kinds=" in logs
    for word in ("ZZQNAME", "kaise", "batata", "aapko", "baare"):
        assert word not in logs, f"word {word!r} leaked into the logs"


async def test_a_retry_slower_than_its_time_limit_is_abandoned_and_cancelled(monkeypatch):
    monkeypatch.setattr(pipeline, "SCRIPT_RETRY_TIMEOUT_S", 0.05)
    m = MockModels()
    m.brain_delay = 0.2                                   # slower than the 0.05 s limit: the first answer is awaited, the retry is not
    m.reply_queue = [ROMAN, DEVA]
    conv, clients = _conv(m, devanagari_hindi=True)
    monkeypatch.setattr(pipeline, "SCRIPT_RETRY_TIMEOUT_S", 0.05)
    await _turn(conv, m)
    await clients.aclose()
    assert len(_brain(m)) == 2 and m.cancelled.count("brain") == 1                            # the late rewrite was cancelled, not left running
    assert conv.last_script_report["retried"] and conv.last_script_report["retry_ok"] is False
    assert _spoken(m) and "ZZQNAME" in _spoken(m)[0]


async def test_clause_level_enforcement_also_runs_in_the_devanagari_flow():
    """The reply passes the gate with a Roman-looking clause the verified lexicon can convert: the existing clause layer is reused."""
    m = MockModels()
    m.reply = DEVA
    conv, clients = _conv(m, devanagari_hindi=True)
    res = await _turn(conv, m)
    await clients.aclose()
    assert res.mode == "hi" and res.first_gen_passed is True and conv.current_mode == "hi"


async def test_flag_off_means_no_gate_no_retry_no_tag(caplog):
    caplog.set_level(logging.DEBUG)
    m = MockModels()
    m.reply_queue = [ROMAN]
    conv, clients = _conv(m)
    await _turn(conv, m)
    await clients.aclose()
    assert len(_brain(m)) == 1 and conv.last_script_report is None and "reply_script_check" not in caplog.text
