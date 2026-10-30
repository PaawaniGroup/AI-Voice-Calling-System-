# -*- coding: utf-8 -*-
import io
import wave
import httpx
import pytest

from app.ai.clients import AiError, ModelClients
from app.ai.pipeline import Conversation, StreamingSentenceExtractor, TurnChunk
from app.ai import turn_trace
from app.ai.turn_trace import TurnTrace
from config.settings import Settings


def make_settings(**kw):
    base = {
        "mongo_uri": "mongodb://127.0.0.1:27017",
        "jwt_secret": "test" * 10,
        "webhook_signing_master": "test" * 10,
        "fs_enabled": False,
        "env": "development",
    }
    base.update(kw)
    return Settings(**base)


def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 800)
    return buf.getvalue()


# -----------------------------------------------------------------------------
# 1. StreamingSentenceExtractor Unit Tests
# -----------------------------------------------------------------------------
def test_extractor_emits_hindi_sentence_on_danda():
    ext = StreamingSentenceExtractor(min_words=3)
    tokens = ["जी, ", "B.Tech ", "की ", "कुल ", "फीस ", "₹1,25,000 ", "है। ", "क्या ", "मैं ", "eligibility ", "भी ", "बताऊँ?"]
    emitted = []
    for tok in tokens:
        ready = ext.feed(tok)
        emitted.extend(ready)
    emitted.extend(ext.finish())

    assert len(emitted) == 2
    assert emitted[0] == "जी, B.Tech की कुल फीस ₹1,25,000 है।"
    assert emitted[1] == "क्या मैं eligibility भी बताऊँ?"


def test_extractor_preserves_acronyms():
    ext = StreamingSentenceExtractor(min_words=3)
    # B.Tech should not be split on the period
    tokens = ["B.Tech ", "and ", "M.B.A. ", "are ", "available. ", "Admissions ", "close ", "soon."]
    emitted = []
    for tok in tokens:
        emitted.extend(ext.feed(tok))
    emitted.extend(ext.finish())

    assert len(emitted) == 2
    assert "B.Tech and M.B.A. are available." in emitted[0]
    assert "Admissions close soon." in emitted[1]


def test_extractor_groups_short_prefixes():
    ext = StreamingSentenceExtractor(min_words=3)
    tokens = ["जी! ", "आपका ", "admission ", "हो ", "जाएगा।"]
    emitted = []
    for tok in tokens:
        emitted.extend(ext.feed(tok))
    emitted.extend(ext.finish())

    assert len(emitted) == 1
    assert emitted[0] == "जी! आपका admission हो जाएगा।"


def test_extractor_empty_input():
    ext = StreamingSentenceExtractor()
    assert ext.feed("") == []
    assert ext.finish() == []


# -----------------------------------------------------------------------------
# 2. ModelClients.chat_stream Unit Tests
# -----------------------------------------------------------------------------
async def test_chat_stream_sse_tokens():
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"जी, "}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"B.Tech "}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"available है।"}}]}\n\n',
        'data: [DONE]\n\n',
    ]

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(sse_lines).encode("utf-8"),
        )

    s = make_settings(ai_allowed_hosts="brain.test")
    client = ModelClients(s, {"brain_url": "http://brain.test/v1/chat/completions"}, httpx.MockTransport(handler))

    events = []
    async for ev in client.chat_stream([{"role": "user", "content": "hi"}]):
        events.append(ev)
    await client.aclose()

    deltas = [e["delta"] for e in events if "delta" in e]
    assert deltas == ["जी, ", "B.Tech ", "available है।"]
    done = [e for e in events if e.get("done")]
    assert len(done) == 1
    assert done[0]["text"] == "जी, B.Tech available है।"


async def test_chat_stream_sse_tool_calls():
    sse_lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"check_fees","arguments":"{\\"course\\": "}}]}}]}\n\n',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"B.Tech\\"}"}}]}}]}\n\n',
        'data: [DONE]\n\n',
    ]

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(sse_lines).encode("utf-8"),
        )

    s = make_settings(ai_allowed_hosts="brain.test")
    client = ModelClients(s, {"brain_url": "http://brain.test/v1/chat/completions"}, httpx.MockTransport(handler))

    events = []
    async for ev in client.chat_stream([{"role": "user", "content": "fees"}]):
        events.append(ev)
    await client.aclose()

    tool_evs = [e for e in events if "tool_calls" in e]
    assert len(tool_evs) == 1
    tc = tool_evs[0]["tool_calls"]
    assert len(tc) == 1
    assert tc[0]["function"]["name"] == "check_fees"
    assert tc[0]["function"]["arguments"] == '{"course": "B.Tech"}'


async def test_chat_stream_json_fallback():
    # If a server returns application/json instead of text/event-stream
    json_body = {"choices": [{"message": {"role": "assistant", "content": "Admission open."}}]}

    def handler(request: httpx.Request):
        return httpx.Response(200, json=json_body, headers={"content-type": "application/json"})

    s = make_settings(ai_allowed_hosts="brain.test")
    client = ModelClients(s, {"brain_url": "http://brain.test/v1/chat/completions"}, httpx.MockTransport(handler))

    events = []
    async for ev in client.chat_stream([{"role": "user", "content": "hi"}]):
        events.append(ev)
    await client.aclose()

    assert any(e.get("delta") == "Admission open." for e in events)
    assert any(e.get("done") and e.get("text") == "Admission open." for e in events)


async def test_chat_stream_http_error():
    def handler(request: httpx.Request):
        return httpx.Response(500, content=b"server error")

    s = make_settings(ai_allowed_hosts="brain.test")
    client = ModelClients(s, {"brain_url": "http://brain.test/v1/chat/completions"}, httpx.MockTransport(handler))

    with pytest.raises(AiError) as exc_info:
        async for _ in client.chat_stream([{"role": "user", "content": "hi"}]):
            pass
    await client.aclose()
    assert exc_info.value.kind == "http_error"


# -----------------------------------------------------------------------------
# 3. Conversation.stream_turn End-to-End Streaming Tests
# -----------------------------------------------------------------------------
async def test_conversation_streams_audio_sentence_by_sentence():
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"जी, "}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"B.Tech fees ₹1,25,000 है। "}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"क्या मैं "}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"eligibility भी बताऊँ?"}}]}\n\n',
        'data: [DONE]\n\n',
    ]

    def handler(request: httpx.Request):
        if "/transcriptions" in request.url.path:
            return httpx.Response(200, json={"text": "B.Tech fees", "language": "hi"})
        if "/chat/completions" in request.url.path:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join(sse_lines).encode("utf-8"))
        if "/audio/speech" in request.url.path:
            return httpx.Response(200, headers={"content-type": "audio/wav"}, content=_wav())
        return httpx.Response(404)

    s = make_settings(ai_allowed_hosts="m.test")
    cfg = {
        "stt_url": "http://m.test/v1/audio/transcriptions",
        "brain_url": "http://m.test/v1/chat/completions",
        "tts_url": "http://m.test/v1/audio/speech",
    }
    client = ModelClients(s, cfg, httpx.MockTransport(handler))
    conv = Conversation(clients=client, script_enforcement=True)

    tr = TurnTrace(t_speech_end=10.0)
    token = turn_trace.start(tr)

    chunks: list[TurnChunk] = []
    try:
        async for chunk in conv.stream_turn(b"\x00" * 3200, 16000):
            chunks.append(chunk)
    finally:
        turn_trace._current.reset(token)
    await client.aclose()

    # Check that chunks were yielded
    audio_chunks = [c for c in chunks if c.audio]
    assert len(audio_chunks) >= 2
    assert "B.Tech fees ₹1,25,000 है।" in audio_chunks[0].sentence
    assert "eligibility भी बताऊँ?" in audio_chunks[1].sentence

    # Check timestamps: t_llm_first_token and t_llm_first_clause were marked
    assert "t_llm_first_token" in tr.marks
    assert "t_llm_first_clause" in tr.marks
    assert tr.marks["t_llm_first_clause"] >= tr.marks["t_llm_first_token"]

    # Check assistant history updated with full reply
    assert len(conv.messages) >= 3
    assert conv.messages[-1]["role"] == "assistant"
    assert "B.Tech fees ₹1,25,000 है। क्या मैं eligibility भी बताऊँ?" in conv.messages[-1]["content"]
