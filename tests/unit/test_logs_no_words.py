"""Logs carry lengths and counts, never the words of a conversation (what the caller said or what the assistant answered).
Covers the three lines that used to print text: clause_verified_conversion, clause_script_violation (text + the offending words)
and turn_script_log (roman_tokens). Model services are MOCKS."""
import logging

from app.ai.clients import ModelClients
from app.ai.pipeline import Conversation
from app.ai.script_enforcement import enforce_clause, problem_kinds
from tests.conftest import make_settings
from tests.helpers import MockModels, pcm_tone

SECRET = "ZZQNAMEKX"                                  # a name that must never reach a log
CALLER_WORDS = ("baare", "batao")                     # distinctive words of what MockModels' STT returns for the caller


def test_problem_kinds_hides_the_offending_words():
    assert problem_kinds(["roman_hindi_word:kaise", "roman_hindi_word:hain", "something_else"]) == {"roman_hindi_word": 2, "something_else": 1}


def test_enforce_clause_violation_log_has_counts_not_words(caplog):
    caplog.set_level(logging.DEBUG)
    clause = f"aap kaise hain {SECRET} ji, mera naam hai"
    spoken, passed, problems = enforce_clause(clause, mode="hi")
    assert passed is False and problems, "the fixture clause must really violate the script rule"
    text = caplog.text
    assert "clause_script_violation" in text and f"clause_chars={len(clause)}" in text and "roman_hindi_word" in text
    for word in ("kaise", "hain", SECRET, "naam"):
        assert word not in text, f"word {word!r} leaked into the log"


async def test_non_streamed_retry_logs_lengths_not_replies(caplog):
    from app.ai.script_enforcement import enforce_non_streamed
    caplog.set_level(logging.DEBUG)
    bad = f"Namaste {SECRET}, aap kaise hain, fees kya hai"

    async def good(_msgs):          # the corrective retry succeeds
        return {"text": f"जी, {SECRET} जी, आपकी fees ₹5,000 है।"}

    async def still_bad(_msgs):     # the corrective retry fails the checker again
        return {"text": f"phir se aap kaise hain {SECRET}"}

    async def boom(_msgs):          # the retry raises with the words in the message
        raise RuntimeError(f"upstream said: {SECRET} aap kaise hain")
    for fn in (good, still_bad, boom):
        await enforce_non_streamed(bad, "hi", fn)
    text = caplog.text
    assert "non_streamed_retry_triggered" in text and "non_streamed_retry_succeeded: chars=" in text
    assert "non_streamed_retry_failed_checker: chars=" in text and "non_streamed_retry_timeout_or_error: RuntimeError" in text
    for word in (SECRET, "kaise", "aap", "fees"):
        assert word not in text, f"word {word!r} leaked into the log"


async def test_a_full_turn_logs_no_caller_or_assistant_words(caplog):
    caplog.set_level(logging.DEBUG)
    m = MockModels()
    m.reply = f"Namaste! {SECRET} ji, BTech ke liye eligibility 12th mein 50% hai. Aap kaise hain?"
    s = make_settings(ai_allowed_hosts="stt.test,brain.test,tts.test")
    cfg = {"stt_url": "http://stt.test:9000", "brain_url": "http://brain.test:9000", "tts_url": "http://tts.test:9000"}
    clients = ModelClients(s, cfg, m.transport())
    conv = Conversation(clients, "hi", script_enforcement=True)
    res = await conv.turn(pcm_tone(400, 16000), 16000)
    await clients.aclose()
    assert res.error is None and res.reply, "the turn must complete for the log check to mean anything"
    text = caplog.text.lower()
    assert "turn_script_log" in text and "roman_token_count=" in text
    for word in (SECRET, "eligibility", "kaise", "namaste", *CALLER_WORDS):
        assert word.lower() not in text, f"word {word!r} leaked into the logs"
