"""CPU/mocked unit tests. These validate contracts only - they are NOT GPU or live-model validation."""
import base64
import io
import wave

import numpy as np
import pytest

import handler
from app import audio_preprocessor as ap
from app import language_detector as ld
from app import transcript_normalizer as tn
from app.asr_engine import AsrEngine
from app.response_schema import AsrError


def wav_b64(sr=8000, secs=1.0, ch=1, freq=440):
    t = np.arange(int(sr * secs)) / sr
    x = (np.sin(2 * np.pi * freq * t) * 12000).astype("<i2")
    if ch == 2:
        x = np.repeat(x, 2)
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(ch); w.setsampwidth(2); w.setframerate(sr); w.writeframes(x.tobytes())
    return base64.b64encode(b.getvalue()).decode()


class FakeModel:
    class R:
        def __init__(self, language, text):
            self.language, self.text = language, text

    def __init__(self, language="Hindi", text="मेरा नाम utkarsh है."):
        self.calls, self.language, self.text = [], language, text

    def transcribe(self, audio, language=None):
        self.calls.append((audio, language))
        return [self.R(self.language, self.text)]


def eng(model=None):
    return AsrEngine("m", "rev", backend=model or FakeModel())


# --- audio ---
def test_resample_is_real_8k_to_16k():
    x, meta = ap.preprocess(wav_b64(8000, 1.0), "wav", None)
    assert abs(len(x) - 16000) <= 2 and meta["source_sample_rate"] == 8000 and meta["model_sample_rate"] == 16000
    spec = np.abs(np.fft.rfft(x)); peak = np.argmax(spec) * 16000 / len(x)
    assert abs(peak - 440) < 5          # tone frequency preserved => genuine resample, not a header rewrite


def test_stereo_downmix_and_mulaw():
    x, _ = ap.preprocess(wav_b64(16000, 0.5, ch=2), "wav", None)
    assert x.ndim == 1 and len(x) == 8000
    assert ap.mulaw_decode(bytes([0xFF]))[0] == 0 and ap.mulaw_decode(bytes([0x00]))[0] == -32124
    b = base64.b64encode(bytes([0xFF] * 800)).decode()
    x, meta = ap.preprocess(b, "mulaw", 8000)
    assert meta["audio_duration_ms"] == 100.0 and len(x) == 1600


@pytest.mark.parametrize("args,code", [
    (("", "wav", None), "invalid_audio"), (("!!!", "wav", None), "invalid_audio"),
    ((base64.b64encode(b"notwav").decode(), "wav", None), "invalid_audio"),
    ((base64.b64encode(b"\x00\x00").decode(), "pcm16", None), "invalid_sample_rate"),
    ((base64.b64encode(b"\x00\x00\x00").decode(), "pcm16", 8000), "invalid_audio"),
    ((base64.b64encode(b"\x00\x00").decode(), "mp9", 8000), "unsupported_format"),
])
def test_invalid_audio(args, code):
    with pytest.raises(AsrError) as e:
        ap.preprocess(*args)
    assert e.value.code == code


def test_too_long():
    with pytest.raises(AsrError) as e:
        ap.preprocess(wav_b64(8000, 3.0), "wav", None, 16000, 2.0)
    assert e.value.code == "audio_too_long"


# --- language ---
def test_language_mapping():
    assert ld.request_language_to_model("auto") is None and ld.request_language_to_model(None) is None
    assert ld.request_language_to_model("hi") == "Hindi" and ld.request_language_to_model("English") == "English"
    assert ld.model_language_to_iso("Hindi") == "hi"
    with pytest.raises(ValueError):
        ld.request_language_to_model("klingon")


# --- normalization ---
def test_normalizer_never_changes_words():
    raw = "मेरा नाम  utkarsh है. maine btech kiya"
    out, rep = tn.normalize(raw, ["BTech"])
    assert out == "मेरा नाम utkarsh है। maine BTech kiya"
    assert rep["operations"]["protected_case"] == 1 and rep["operations"]["danda"] == 1
    assert [w for w in out.replace("।", "").split() if w.lower() != "btech"] == \
           [w for w in raw.replace(".", "").split() if w.lower() != "btech"]


def test_normalizer_preserves_numbers_and_unrelated_terms():
    raw = "फीस 1,50,000 रुपये है, 85% marks, 12/05/2026, BTechnology"
    out, _ = tn.normalize(raw, ["BTech"])
    assert out == raw                    # 'BTechnology' is not 'BTech'


# --- handler ---
def test_handler_success_schema_and_timings():
    m = FakeModel()
    r = handler.process({"audio_base64": wav_b64(8000, 1.0), "audio_format": "wav", "language": "auto"}, eng(m))
    assert r["status"] == "success" and r["language"] == "hi" and r["raw_text"] == "मेरा नाम utkarsh है."
    assert r["normalized_text"].endswith("।") and r["raw_text"].endswith(".")   # raw kept unchanged
    assert r["audio_duration_ms"] == 1000.0 and r["inference_time_ms"] >= 0 and r["real_time_factor"] is not None
    audio, language = m.calls[0]
    assert language is None and audio[1] == 16000 and abs(len(audio[0]) - 16000) <= 2


def test_handler_forced_language_and_flags():
    m = FakeModel("English", "hello")
    r = handler.process({"audio_base64": wav_b64(), "language": "en", "return_normalized_text": False}, eng(m))
    assert m.calls[0][1] == "English" and "normalized_text" not in r and r["language_forced"] is True


@pytest.mark.parametrize("inp,code", [
    ({}, "invalid_audio"), ({"audio_base64": "x!"}, "invalid_audio"),
    ({"audio_base64": wav_b64(), "language": "xx"}, "unsupported_language"),
])
def test_handler_errors_are_structured(inp, code):
    r = handler.process(inp, eng())
    assert r["status"] == "error" and r["error"]["code"] == code


def test_handler_model_exception_is_contained():
    class Boom:
        def transcribe(self, **kw): raise RuntimeError("cuda oom")
    r = handler.process({"audio_base64": wav_b64()}, eng(Boom()))
    assert r["error"]["code"] == "inference_failed"


def test_shutdown_rejects(monkeypatch):
    monkeypatch.setattr(handler, "_shutting_down", True)
    assert handler.process({"audio_base64": wav_b64()}, eng())["error"]["code"] == "shutting_down"


def test_engine_requires_pinned_revision():
    with pytest.raises(RuntimeError):
        AsrEngine("m", "UNPINNED", backend=FakeModel(), require_pinned=True)


def test_model_loaded_once_and_reused():
    e = eng(); m = e._model
    for _ in range(3):
        handler.process({"audio_base64": wav_b64()}, e)
    assert e._model is m and len(m.calls) == 3
