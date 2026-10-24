"""Per-tenant flag `tts_ulaw8k` (default OFF) of the RunPod TTS adapter: the voice is named, the worker answers 8 kHz mu-law, no reference audio is read or
sent, and the answer is decoded with ulaw2lin. The TTS worker is a MOCK (tests/helpers.MockModels): v3 = voices by name + ulaw8k, v2 = WAV only."""
import audioop

import pytest

from app.ai.clients import AiError, ModelClients
from tests.conftest import make_settings
from tests.helpers import MockModels, pcm_tone

TTS = "http://tts.test:9000/v2/ep/runsync"


def clients(models, tmp_path=None, ref="missing", **cfg_extra):
    kw = {"ai_allowed_hosts": "tts.test"}
    if ref == "missing":
        kw["ai_tts_reference_wav"] = str(tmp_path / "no_such_reference.wav") if tmp_path is not None else "/nonexistent/ref.wav"
    elif ref:
        kw["ai_tts_reference_wav"] = ref
    return ModelClients(make_settings(**kw), {"tts_url": TTS, "keys": {"tts": "k"}, **cfg_extra}, models.transport())


def tts_requests(models):
    return [b["input"] for k, b in models.calls if k == "tts"]


async def test_default_is_unchanged_the_reference_is_sent_and_a_wav_comes_back(tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF" + b"\x00" * 100)
    m = MockModels()
    c = clients(m, ref=str(ref))
    pcm, rate = await c.synthesize("haan", "hi")
    await c.aclose()
    inp = tts_requests(m)[0]
    assert "reference_audio_base64" in inp and "voice" not in inp and "output_format" not in inp
    assert rate == 16000 and len(pcm) > 1000                                           # the mock's WAV, decoded as before


async def test_flag_on_names_the_voice_asks_for_ulaw8k_and_never_touches_the_reference(tmp_path):
    m = MockModels()
    c = clients(m, tmp_path, tts_ulaw8k=True)          # the configured reference file does not exist: reading it would raise reference_unreadable
    pcm, rate = await c.synthesize("haan", "hi")
    await c.aclose()
    inp = tts_requests(m)[0]
    assert inp["voice"] == "utkarsh" and inp["output_format"] == "ulaw8k" and "reference_audio_base64" not in inp
    assert set(inp) == {"text", "prompt", "language", "model", "voice", "output_format"}


async def test_flag_on_decodes_the_mulaw_answer_with_ulaw2lin_at_8khz(tmp_path):
    m = MockModels()
    m.tts_ms = 300
    c = clients(m, tmp_path, tts_ulaw8k=True)
    pcm, rate = await c.synthesize("haan", "hi")
    await c.aclose()
    expected = audioop.ulaw2lin(audioop.lin2ulaw(pcm_tone(300, 8000), 2), 2)
    assert rate == 8000 and pcm == expected and len(pcm) == 2 * 8000 * 300 // 1000


async def test_a_tenant_can_name_another_voice(tmp_path):
    m = MockModels()
    c = clients(m, tmp_path, tts_ulaw8k=True, tts_voice="priya")
    await c.synthesize("haan", "hi")
    await c.aclose()
    assert tts_requests(m)[0]["voice"] == "priya"


async def test_an_unknown_voice_is_a_clear_worker_error_not_silence(tmp_path):
    m = MockModels()
    c = clients(m, tmp_path, tts_ulaw8k=True, tts_voice="nobody")
    with pytest.raises(AiError) as e:
        await c.synthesize("haan", "hi")
    await c.aclose()
    assert e.value.service == "tts" and e.value.kind == "worker_error" and "Unknown voice" in e.value.detail and len(e.value.detail) <= 80


async def test_an_old_worker_that_does_not_know_ulaw8k_is_reported_as_such(tmp_path):
    m = MockModels()
    m.tts_worker = "v2"
    c = clients(m, tmp_path, tts_ulaw8k=True)
    with pytest.raises(AiError) as e:
        await c.synthesize("haan", "hi")
    await c.aclose()
    assert e.value.kind == "worker_error" and "output_format" in e.value.detail           # the operator sees WHY: the endpoint still runs the old image


async def test_a_worker_that_answers_a_wav_anyway_is_still_decoded(tmp_path):
    class WavAnyway(MockModels):
        def _tts_output(self, inp):
            return super()._tts_output({k: v for k, v in inp.items() if k != "output_format"})

    m = WavAnyway()
    c = clients(m, tmp_path, tts_ulaw8k=True)
    pcm, rate = await c.synthesize("haan", "hi")
    await c.aclose()
    assert rate == 16000 and len(pcm) > 1000


async def test_an_empty_answer_is_a_bad_response(tmp_path):
    class Empty(MockModels):
        def _tts_output(self, inp):
            return {"audio_base64": "", "format": "ulaw8k"}

    m = Empty()
    c = clients(m, tmp_path, tts_ulaw8k=True)
    with pytest.raises(AiError) as e:
        await c.synthesize("haan", "hi")
    await c.aclose()
    assert e.value.kind == "bad_response"


async def test_without_the_flag_a_worker_error_keeps_its_old_behaviour(tmp_path):
    m = MockModels()
    m.tts_error = "boom"
    c = clients(m, ref=None)
    with pytest.raises(AiError) as e:
        await c.synthesize("haan", "hi")
    await c.aclose()
    assert e.value.kind == "bad_response"                                                  # unchanged: only the flag path reports worker_error


async def test_text_normalisation_still_applies_with_the_flag(tmp_path):
    m = MockModels()
    c = clients(m, tmp_path, tts_ulaw8k=True)
    await c.synthesize("फ़ीस 1,25,000 रुपये है", "hi")
    await c.aclose()
    assert "1,25,000" not in tts_requests(m)[0]["text"]                                    # numbers are still written as spoken words before the worker
