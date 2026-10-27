"""A Voice Studio preview must speak the SAME text a call would: `spoken_text` is compared with what the real call-path client (ModelClients, MOCK RunPod worker)
sends for the same input. The call path itself is not changed by Voice Studio."""
import pytest

from app.ai.clients import ModelClients
from app.voices.frontend import PREVIEW_LANGUAGES, spoken_text, worker_input
from tests.conftest import make_settings
from tests.helpers import MockModels

TTS = "http://tts.test:9000/v2/ep/runsync"
TEXTS = ["aapki fees 45000 rupees hai, 15/11/2026 tak jama karein", "Your JEE rank is 4521 and the fee is Rs. 1,25,000.", "नमस्ते, ₹5,000 जमा करें 20 June को",
         "Call us on +91 98765 43210 before 3:30 PM", "mujhe B.Tech ke baare mein batao", "12वीं में 75% chahiye"]


async def call_path(text, language, normalize):
    m = MockModels()
    c = ModelClients(make_settings(ai_allowed_hosts="tts.test", ai_tts_normalize=normalize), {"tts_url": TTS, "keys": {"tts": "k"}, "tts_ulaw8k": True}, m.transport())
    await c.synthesize(text, language)
    await c.aclose()
    return [b["input"] for k, b in m.calls if k == "tts"][0]


@pytest.mark.parametrize("normalize", [True, False])
@pytest.mark.parametrize("language", ["auto", "hi", "en", "hinglish"])
@pytest.mark.parametrize("text", TEXTS)
async def test_the_preview_text_equals_the_text_the_call_path_sends(text, language, normalize):
    sent = await call_path(text, language, normalize)
    spoken, lang = spoken_text(text, language, normalize)
    assert (spoken, lang) == (sent["text"], sent["language"])


async def test_phone_quality_asks_for_the_same_ulaw8k_format_as_calls_and_a_wav_otherwise():
    sent = await call_path("namaste", "hi", True)
    phone = worker_input("namaste", "hi", True, "v_abc", True)
    assert phone["output_format"] == sent["output_format"] == "ulaw8k"
    assert phone == {"text": sent["text"], "language": sent["language"], "voice": "v_abc", "output_format": "ulaw8k"}
    assert worker_input("namaste", "hi", True, "v_abc", False)["output_format"] == "wav"


def test_the_languages_are_the_ones_a_tenant_can_use_for_calls():
    from app.ai.service import LANGS
    assert list(PREVIEW_LANGUAGES) == LANGS


def test_numbers_really_are_written_as_words():
    spoken, lang = spoken_text("fees 45000 rupees hai", "auto", True)
    assert lang == "hi" and "45000" not in spoken
