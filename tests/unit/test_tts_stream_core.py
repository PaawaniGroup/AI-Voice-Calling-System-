"""StreamDecoder against a SYNTHETIC decoder with known ground truth (no torch, no GPU, not the real S3Gen).
These tests prove the windowing / look-ahead / cross-fade / flush / cancel LOGIC. Whether the real S3Gen keeps its quality under a bounded window
is NOT established here (it needs a GPU host; see docs/STREAMING_TTS_STATUS.md)."""
import numpy as np
import pytest

from tools.tts_stream_worker.stream_core import StreamClosed, StreamDecoder

SPT, LOOK, FADE = 960, 3, 480          # samples per token (24 kHz / 25 Hz), look-ahead tokens, per-decode fade-in like S3Gen's trim_fade


def make_decoder(offset_per_decode: float = 0.0):
    """token t -> SPT samples of a value that depends on the token only (so every window agrees), like an ideal decoder.
    Imperfections copied from S3Gen: a fade-in over the first FADE samples of EVERY decode, and (optionally) a constant offset on every
    decode after the first = a window that disagrees with the one before."""
    calls = {"n": 0}

    def decode(window, final):
        calls["n"] += 1
        n = len(window) if final else len(window) - LOOK
        n = max(n, 0)
        audio = np.concatenate([np.full(SPT, 0.001 * (t + 1), dtype=np.float32) for t in window[:n]]) if n else np.zeros(0, np.float32)
        if len(audio):
            k = min(FADE, len(audio))
            audio[:k] *= np.linspace(0.0, 1.0, k, dtype=np.float32)
        if calls["n"] % 2 == 0 and len(audio):      # every other decode disagrees with its neighbours: a click at every boundary
            audio = audio + np.float32(offset_per_decode)
        return audio
    return decode, calls


def full(tokens):
    d, _ = make_decoder()
    return d(list(tokens), True)


def run(tokens, push_sizes, **kw):
    d, _ = make_decoder(kw.pop("offset", 0.0))
    sd = StreamDecoder(d, SPT, lookahead_tokens=LOOK, **kw)
    chunks, i = [], 0
    for n in push_sizes:
        chunks += sd.push(tokens[i:i + n])
        i += n
    chunks += sd.push(tokens[i:])
    chunks += sd.flush()
    return sd, chunks


TOKENS = list(range(200))


@pytest.mark.parametrize("kw", [dict(context_tokens=25), dict(context_tokens=25, crossfade_samples=240), dict(context_tokens=None),
                                dict(context_tokens=8, hop_tokens=5, first_hop_tokens=2, crossfade_samples=480)])
@pytest.mark.parametrize("pushes", [[1] * 200, [7, 13, 40, 1, 99], [200]])
def test_stream_equals_the_full_decode_no_gap_no_duplicate(kw, pushes):
    _, chunks = run(TOKENS, pushes, **kw)
    got = np.concatenate(chunks)
    ref = full(TOKENS)
    assert len(got) == len(TOKENS) * SPT                      # exact output length: nothing missing, nothing repeated
    assert np.allclose(got, ref, atol=1e-6)                   # same samples in the same order as one full decode


def test_first_audio_comes_before_the_utterance_is_complete():
    d, _ = make_decoder()
    sd = StreamDecoder(d, SPT, lookahead_tokens=LOOK, hop_tokens=12, first_hop_tokens=4, context_tokens=25)
    assert sd.push(TOKENS[:6]) == []                          # 6 - look(3) = 3 stable tokens < first hop (4): nothing yet
    first = sd.push(TOKENS[6:7])                              # 7 - 3 = 4 stable tokens -> first chunk, 193 tokens early
    assert len(first) == 1 and len(first[0]) == 4 * SPT


def test_bounded_window_cost_is_constant_per_push_and_full_prefix_is_quadratic():
    cost = {}
    for n in (400, 800):
        for mode, ctx in (("bounded", 25), ("full", None)):
            sd, _ = run(list(range(n)), [1] * n, context_tokens=ctx, hop_tokens=12)
            cost[mode, n] = sd.stats
    assert cost["bounded", 800]["max_window"] <= 25 + 12 + LOOK + 1   # context + hop + look-ahead (+1 for the push that triggered it)
    assert cost["full", 800]["max_window"] >= 780                      # the whole history is re-decoded
    # doubling the utterance doubles the work when bounded, quadruples it for the full prefix
    assert cost["bounded", 800]["decoded_tokens"] / cost["bounded", 400]["decoded_tokens"] < 2.2
    assert cost["full", 800]["decoded_tokens"] / cost["full", 400]["decoded_tokens"] > 3.5


def test_buffered_tokens_stay_bounded():
    d, _ = make_decoder()
    sd = StreamDecoder(d, SPT, lookahead_tokens=LOOK, hop_tokens=12, context_tokens=25)
    for i in range(0, 2000, 4):
        sd.push(range(i, i + 4))
        assert len(sd._tokens) <= 25 + 12 + LOOK + 4


def test_short_utterance_only_flushes():
    for n in (1, 2, 3, 4, 11):                                 # shorter than look-ahead + hop: no push output at all
        d, _ = make_decoder()
        sd = StreamDecoder(d, SPT, lookahead_tokens=LOOK, hop_tokens=12, context_tokens=25)
        assert sd.push(range(n)) == []
        out = np.concatenate(sd.flush())
        assert len(out) == n * SPT and np.allclose(out, full(range(n)), atol=1e-6)


def test_empty_stream_flushes_to_nothing():
    d, _ = make_decoder()
    sd = StreamDecoder(d, SPT, lookahead_tokens=LOOK)
    assert sd.flush() == []


def test_cancel_drops_everything_and_refuses_more():
    d, calls = make_decoder()
    sd = StreamDecoder(d, SPT, lookahead_tokens=LOOK, hop_tokens=4, context_tokens=25, crossfade_samples=240)
    sd.push(range(30))
    before = calls["n"]
    sd.close()
    assert sd._tokens == [] and len(sd._tail) == 0
    with pytest.raises(StreamClosed):
        sd.push([1])
    with pytest.raises(StreamClosed):
        sd.flush()
    assert calls["n"] == before                                # no decoding after cancel


def test_flush_twice_is_refused():
    d, _ = make_decoder()
    sd = StreamDecoder(d, SPT, lookahead_tokens=LOOK)
    sd.push(range(5))
    sd.flush()
    with pytest.raises(StreamClosed):
        sd.flush()


def test_a_decoder_that_returns_too_little_audio_is_an_error_not_silence():
    sd = StreamDecoder(lambda w, f: np.zeros(10, np.float32), SPT, lookahead_tokens=LOOK, hop_tokens=2, first_hop_tokens=2)
    with pytest.raises(ValueError):
        sd.push(range(10))


def test_crossfade_smooths_a_window_that_disagrees_with_the_previous_one():
    off = 0.02
    def worst_step(xf):
        _, chunks = run(TOKENS, [200], context_tokens=25, hop_tokens=12, crossfade_samples=xf, offset=off)
        a = np.concatenate(chunks)
        return float(np.max(np.abs(np.diff(a[SPT:]))))        # past the fade-in at the start of the utterance
    assert worst_step(0) > 0.015                               # hard cut: a click as large as the disagreement
    assert worst_step(480) < 0.005                             # cross-fade: the step is spread over 480 samples


def test_two_streams_do_not_share_state():
    da, _ = make_decoder()
    db, _ = make_decoder()
    a = StreamDecoder(da, SPT, lookahead_tokens=LOOK, hop_tokens=6, context_tokens=25, crossfade_samples=240)
    b = StreamDecoder(db, SPT, lookahead_tokens=LOOK, hop_tokens=6, context_tokens=25, crossfade_samples=240)
    ta, tb = list(range(100)), list(range(500, 640))
    ca, cb = [], []
    for i in range(0, 140, 5):
        if i < 100:
            ca += a.push(ta[i:i + 5])
        cb += b.push(tb[i:i + 5])
    ca += a.flush()
    cb += b.flush()
    assert np.allclose(np.concatenate(ca), full(ta), atol=1e-6)
    assert np.allclose(np.concatenate(cb), full(tb), atol=1e-6)


def test_crossfade_longer_than_the_context_is_rejected():
    with pytest.raises(ValueError):
        StreamDecoder(lambda w, f: np.zeros(0, np.float32), SPT, context_tokens=1, crossfade_samples=SPT + 1)
