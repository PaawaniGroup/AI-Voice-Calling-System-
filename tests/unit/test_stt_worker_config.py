"""STT worker v2: request language is honoured, default is configurable, revision is pinned, bad config fails loudly, audio is truly resampled."""
import base64
import io
import struct
import sys
import types
import wave

import pytest

from tools.stt_worker_v2 import handler as H


def wav_b64(samples, rate):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(struct.pack("<%dh" % len(samples), *samples))
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def rec(monkeypatch):
    seen = {}

    def fake_transcribe(audio, prompt=None, num_beams=None, language=None):
        seen.update(language=language, num_beams=num_beams, prompt=prompt)
        return "text", -0.1

    monkeypatch.setattr(H, "transcribe", fake_transcribe)
    monkeypatch.setattr(H, "load_audio", lambda data: [0.0] * 16000)
    return seen


def run(rec, **inp):
    return H.handler({"input": {"task": "stt", "audio_b64": "x", **inp}})


def test_request_language_is_not_ignored(rec):
    run(rec, language="en"); assert rec["language"] == "en"
    run(rec, language="hi"); assert rec["language"] == "hi"
    run(rec, language="hinglish"); assert rec["language"] == "hi"          # app's name for Hindi/Hinglish


def test_default_language_is_configurable_and_not_english(rec, monkeypatch):
    run(rec); assert rec["language"] == "hi"                                # deployment default
    run(rec, language=None); assert rec["language"] == "hi"                 # the app sends None for "auto"
    monkeypatch.setattr(H, "WHISPER_LANGUAGE", "en")
    run(rec); assert rec["language"] == "en"
    monkeypatch.setattr(H, "WHISPER_LANGUAGE", "auto")
    run(rec); assert rec["language"] is None                                # automatic detection
    run(rec, language="auto"); assert rec["language"] is None


def test_invalid_language_and_beams_fail_explicitly(rec):
    with pytest.raises(H.ConfigError, match="unsupported language"):
        run(rec, language="klingon")
    with pytest.raises(H.ConfigError, match="language must be a string"):
        run(rec, language=5)
    with pytest.raises(H.ConfigError, match="num_beams"):
        run(rec, num_beams=0)
    with pytest.raises(H.ConfigError, match="num_beams"):
        run(rec, num_beams="many")


def test_response_schema_is_backward_compatible_and_does_not_fake_a_detected_language(rec):
    out = run(rec, language="en")
    assert {"text", "avg_logprob", "model", "timing"} <= set(out)
    assert out["text"] == "text" and out["decode_language"] == "en"
    assert "language" not in out        # the app reads `language` as a detected-language signal (pipeline lang for TTS/mode)


def test_gen_kwargs_carry_the_language_only_when_set():
    assert H.build_gen_kwargs("hi", 1)["language"] == "hi"
    assert "language" not in H.build_gen_kwargs(None, 1)
    assert H.build_gen_kwargs("hi", 3)["num_beams"] == 3 and H.build_gen_kwargs("hi", 1)["task"] == "transcribe"


def test_model_revision_is_passed_to_from_pretrained(monkeypatch):
    calls = []

    class Fake:
        @classmethod
        def from_pretrained(cls, model_id, **kw):
            calls.append((model_id, kw))
            o = cls(); o.to = lambda *_: o; o.eval = lambda: o
            return o

    tf = types.ModuleType("transformers"); tf.WhisperProcessor = Fake; tf.WhisperForConditionalGeneration = Fake
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.float16 = torch.float32 = object()
    monkeypatch.setitem(sys.modules, "transformers", tf)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(H, "MODEL_REVISION", "abc123")
    H.initialize_worker()
    assert len(calls) == 2 and all(kw.get("revision") == "abc123" for _, kw in calls)
    calls.clear()
    monkeypatch.setattr(H, "MODEL_REVISION", "")
    H.initialize_worker()
    assert all(kw.get("revision") is None for _, kw in calls)     # unpinned only when explicitly left empty


# ---- real sample conversion (needs numpy/scipy/soundfile: NOT skipped silently - the report states where it ran)
@pytest.fixture
def np():
    pytest.importorskip("scipy"); pytest.importorskip("soundfile")
    return pytest.importorskip("numpy")


def peak_hz(np, x, rate):
    spec = np.abs(np.fft.rfft(x))
    return float(np.argmax(spec) * rate / len(x))


def test_8k_wav_is_really_resampled_to_16k_not_just_relabelled(np):
    tone = [int(8000 * np.sin(2 * np.pi * 1000 * i / 8000)) for i in range(8000)]          # 1 s, 1 kHz at 8 kHz
    audio = H.load_audio({"audio_b64": wav_b64(tone, 8000)})
    assert audio.dtype == np.float32 and audio.ndim == 1
    assert len(audio) == 16000                                                          # 2x samples
    assert abs(peak_hz(np, audio, 16000) - 1000) < 5                                         # still 1 kHz: samples were interpolated
    rms_in = float(np.sqrt(np.mean((np.array(tone) / 32768.0) ** 2)))
    assert abs(float(np.sqrt(np.mean(audio ** 2))) / rms_in - 1) < 0.05                  # amplitude preserved (no clipping/gain)
    assert float(np.max(np.abs(audio))) <= 1.0


def test_raw_pcm16_path_and_passthrough_at_16k(np):
    tone = [int(8000 * np.sin(2 * np.pi * 500 * i / 8000)) for i in range(4000)]
    raw = base64.b64encode(struct.pack("<%dh" % len(tone), *tone)).decode()
    a = H.load_audio({"audio_pcm16_b64": raw, "sample_rate": 8000})
    assert len(a) == 8000 and abs(peak_hz(np, a, 16000) - 500) < 5
    t16 = [int(8000 * np.sin(2 * np.pi * 500 * i / 16000)) for i in range(16000)]
    b = H.load_audio({"audio_b64": wav_b64(t16, 16000)})
    assert len(b) == 16000                                                              # already 16 kHz: untouched


def test_empty_and_malformed_audio_are_rejected(np):
    with pytest.raises(ValueError):
        H.load_audio({"audio_b64": wav_b64([], 8000)})
    with pytest.raises(ValueError):
        H.load_audio({})
    with pytest.raises(Exception):
        H.load_audio({"audio_b64": "***not base64***"})
