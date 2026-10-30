"""Worker-side progressive generator with a SYNTHETIC decoder and a FAKE token source (no GPU, no torch, no real T3/S3Gen)."""
import numpy as np
import pytest

from app.ai import codec
from app.ai.clients import ModelClients
from tests.unit.test_tts_stream_core import SPT, make_decoder
from tools.tts_stream_worker.worker_stream import StreamingDecimator, pcm16_to_ulaw, stream_synth


def collect(**kw):
    toks = kw.pop("tokens")
    return list(stream_synth(iter(toks), lambda: make_decoder()[0], **kw))


def test_decimator_gives_the_same_samples_for_any_chunking():
    rng = np.random.default_rng(1)
    x = rng.standard_normal(5000).astype(np.float32)
    whole = StreamingDecimator().process(x)
    assert len(whole) == -(-len(x) // 3)
    for sizes in ([1] * 50 + [4950], [2, 3, 5, 7, 11, 13, 4959], [5000]):
        assert sum(sizes) == len(x)
        d, out, i = StreamingDecimator(), [], 0
        for n in sizes:
            out.append(d.process(x[i:i + n]))
            i += n
        assert np.allclose(np.concatenate(out), whole, atol=1e-6)


def test_decimator_keeps_speech_band_and_removes_aliasing_band():
    t = np.arange(24000) / 24000
    keep = StreamingDecimator().process(np.sin(2 * np.pi * 300 * t).astype(np.float32))[100:]
    kill = StreamingDecimator().process(np.sin(2 * np.pi * 7000 * t).astype(np.float32))[100:]   # above the new 4 kHz Nyquist
    assert np.sqrt(np.mean(keep ** 2)) > 0.6
    assert np.sqrt(np.mean(kill ** 2)) < 0.05


def test_ulaw_matches_the_apps_codec_on_every_int16_value():
    allv = np.arange(-32768, 32768, dtype=np.int16)
    assert pcm16_to_ulaw(allv) == codec.pcm16_to_mulaw(allv.tobytes())


def test_first_item_is_emitted_long_before_the_token_stream_ends():
    consumed = []

    def tokens():
        for t in range(200):
            consumed.append(t)
            yield t
    gen = stream_synth(tokens(), lambda: make_decoder()[0])
    first = next(gen)
    assert first["seq"] == 0 and first["final"] is False and first["audio_base64"]
    assert len(consumed) <= 4 + 3 + 1                          # first hop + look-ahead: the other ~190 tokens were not even generated yet
    gen.close()


def test_items_follow_the_contract_and_the_client_parses_them_all():
    items = collect(tokens=list(range(200)))
    assert [i["seq"] for i in items] == list(range(len(items)))
    assert [i["final"] for i in items] == [False] * (len(items) - 1) + [True]
    assert all(i["format"] == "ulaw8k" and i["sample_rate"] == 8000 for i in items)
    chunks = [ModelClients._parse_tts_chunk({"output": i}) for i in items]   # the REAL client parser accepts what the worker produces
    assert sum(len(c.pcm) for c in chunks) == 2 * (200 * SPT // 3)           # exact: 200 tokens -> 64000 samples at 8 kHz, none missing


def test_short_and_empty_utterances_still_end_with_exactly_one_final_item():
    one = collect(tokens=[5])
    assert len(one) == 1 and one[0]["final"] and one[0]["audio_base64"]
    empty = collect(tokens=[])
    assert len(empty) == 1 and empty[0]["final"] and empty[0]["audio_base64"] == ""


def test_cancel_flag_stops_generation_closes_the_source_and_sends_no_final_item():
    state = {"closed": False, "made": 0}

    def tokens():
        try:
            for t in range(1000):
                state["made"] += 1
                yield t
        finally:
            state["closed"] = True
    cancel_after = {"n": 0}

    def should_cancel():
        cancel_after["n"] += 1
        return cancel_after["n"] > 20
    out = list(stream_synth(tokens(), lambda: make_decoder()[0], should_cancel=should_cancel))
    assert state["closed"] and state["made"] <= 22
    assert not any(i["final"] for i in out)                    # a cancelled stream is not a completed one


def test_closing_the_generator_releases_the_token_source():
    state = {"closed": False}

    def tokens():
        try:
            yield from range(1000)
        finally:
            state["closed"] = True
    gen = stream_synth(tokens(), lambda: make_decoder()[0])
    next(gen)
    gen.close()                                                # what the runtime does when the job is cancelled or the client is gone
    assert state["closed"]


def test_a_failing_token_source_propagates_and_is_cleaned_up():
    state = {"closed": False}

    def tokens():
        try:
            yield 1
            yield 2
            raise RuntimeError("CUDA error")
        finally:
            state["closed"] = True
    with pytest.raises(RuntimeError):
        list(stream_synth(tokens(), lambda: make_decoder()[0]))
    assert state["closed"]


def test_two_requests_interleaved_do_not_share_state():
    a, b = list(range(120)), list(range(300, 440))
    ga = stream_synth(iter(a), lambda: make_decoder()[0])
    gb = stream_synth(iter(b), lambda: make_decoder()[0])
    ia, ib = [], []
    for _ in range(100):
        for g, out in ((ga, ia), (gb, ib)):
            try:
                out.append(next(g))
            except StopIteration:
                pass
    assert ia == collect(tokens=a) and ib == collect(tokens=b)


def test_a_model_rate_that_is_not_a_multiple_of_8k_is_refused():
    with pytest.raises(ValueError):
        list(stream_synth(iter([1]), lambda: make_decoder()[0], model_rate=22050))
