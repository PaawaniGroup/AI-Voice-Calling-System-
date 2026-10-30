"""tools/phase3/chunked_tts_spike.py, the parts that need no GPU: the source patch that turns T3.inference into a generator, the chunk planner, the stitching of
chunks (against a fake, context-free vocoder every way of cutting must reproduce the one-shot render sample for sample), the playback simulation and the click score."""
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "phase3"))
import chunked_tts_spike as sp  # noqa: E402

TS = sp.TOKEN_SAMPLES


def fake_vocode(tokens, finalize):
    n = len(tokens) if finalize else max(0, len(tokens) - sp.LOOKAHEAD)
    if not n:
        return np.zeros(0, np.float32)
    return np.concatenate([(np.sin(np.arange(TS) * 0.05) * (1 + (t % 7)) / 8).astype(np.float32) for t in tokens[:n]])


def one_shot(tokens):
    return fake_vocode(tokens, True)[:max(1, len(tokens) - 1) * TS]


class Tick:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.1
        return self.t


# ------------------------------------------------------------------ the source patch
FIXTURE = '''
    def inference(self, *, text_tokens, max_new_tokens=None):
        predicted = []
        for i in range(max_new_tokens):
            next_token = self.sample(i)
            predicted.append(next_token)
            if next_token == self.stop:
                break
        predicted_tokens = list(predicted)
        return predicted_tokens
'''


def test_the_patch_turns_the_loop_into_a_generator_with_one_added_line():
    out = sp.patch_t3_source(FIXTURE)
    assert out.count("yield next_token") == 1 and "def inference_stream(" in out and "def inference(" not in out
    original = sp.textwrap.dedent(FIXTURE).replace("def inference(", "def inference_stream(").splitlines()
    assert [l for l in out.splitlines() if l not in original] == ["        yield next_token"]       # exactly one line was added, nothing else changed
    ns = {}
    exec(compile(out, "<t>", "exec"), ns)

    class M:
        stop = 9

        def sample(self, i):
            return [4, 5, 6, 9, 7][i]
    g = ns["inference_stream"](M(), text_tokens=None, max_new_tokens=10)
    assert list(g) == [4, 5, 6, 9]                                              # every token as it is sampled, the stop token included, then the loop ends


def test_the_patch_refuses_code_that_is_not_the_expected_loop():
    with pytest.raises(RuntimeError, match="not found exactly once"):
        sp.patch_t3_source(FIXTURE.replace("predicted.append(next_token)", "predicted += [next_token]"))
    with pytest.raises(RuntimeError, match="not found exactly once"):
        sp.patch_t3_source(FIXTURE + FIXTURE.replace("def inference(", "def other("))        # two appends: ambiguous
    with pytest.raises(RuntimeError):
        sp.patch_t3_source("def something_else(): pass")


# ------------------------------------------------------------------ planning and stitching
def test_the_planner_fires_at_the_first_size_then_every_chunk_and_never_before_the_lookahead():
    p = sp.ChunkPlanner(10, 20)
    assert [n for n in range(1, 100) if p.due(n)] == [10, 30, 50, 70, 90]
    assert sp.ChunkPlanner(1, 5).next_at == sp.LOOKAHEAD + 1                    # the lookahead drops 3 tokens: a render before 4 tokens would be empty


@pytest.mark.parametrize("mode", ["A", "B"])
@pytest.mark.parametrize("n_tokens,first,chunk,ctx", [(130, 10, 20, 25), (130, 16, 40, 25), (131, 12, 7, 5), (60, 10, 20, 0), (12, 10, 20, 25), (6, 10, 20, 25), (1, 10, 20, 25)])
def test_every_way_of_cutting_reproduces_the_one_shot_render(mode, n_tokens, first, chunk, ctx):
    toks = list(range(1, n_tokens + 1))
    audio, events, joins, got = sp.stream_render(iter(toks), fake_vocode, mode, first, chunk, ctx, None, clock=Tick())
    ref = one_shot(toks)
    assert got == toks and len(audio) == len(ref) and np.allclose(audio, ref)
    assert [e[1] for e in events] == sorted(e[1] for e in events) and events[-1][1] == len(ref)     # cumulative samples only grow and end at the full length
    assert joins == [e[1] for e in events[:-1]]                                  # a join exactly where one piece ends and the next begins


@pytest.mark.parametrize("mode", ["A", "B"])
def test_the_first_chunk_is_available_long_before_the_end_of_generation(mode):
    toks = list(range(1, 201))
    seen = []

    def gen():
        for t in toks:
            seen.append(t)
            yield t
    audio, events, joins, _ = sp.stream_render(gen(), fake_vocode, mode, 10, 20, 25, None, clock=Tick())
    assert events[0][1] == (10 - sp.LOOKAHEAD) * TS                              # 7 tokens = 280 ms of audio from the first 10 tokens


def test_mode_b_renders_only_a_bounded_window_mode_a_the_whole_sequence():
    sizes = {"A": [], "B": []}
    for mode in ("A", "B"):
        def voc(tokens, finalize, mode=mode):
            sizes[mode].append(len(tokens))
            return fake_vocode(tokens, finalize)
        sp.stream_render(iter(range(1, 301)), voc, mode, 10, 20, 25, None, clock=Tick())
    assert max(sizes["A"]) == 300 and sizes["A"] == sorted(sizes["A"])           # A grows with the text
    assert max(sizes["B"]) <= 25 + 20 + sp.LOOKAHEAD + 20                        # B stays bounded by the context plus one chunk


def test_the_fade_is_applied_once_to_the_start_only():
    toks = list(range(1, 80))
    fade = np.linspace(0, 1, 100).astype(np.float32)
    audio, _, _, _ = sp.stream_render(iter(toks), fake_vocode, "A", 10, 20, 25, fade, clock=Tick())
    ref = one_shot(toks)
    assert np.allclose(audio[:100], ref[:100] * fade) and np.allclose(audio[100:], ref[100:])


# ------------------------------------------------------------------ metrics
def test_simulated_playback_counts_the_stalls_of_late_chunks():
    sr = sp.SR
    ev = [(1.0, sr), (1.5, 2 * sr), (2.2, 3 * sr)]                               # 1 s of audio per chunk; chunks ready at 1.0, 1.5, 2.2
    r = sp.simulate_playback(ev)
    assert r["ttfa_s"] == 1.0 and r["stall_ms_total"] == 0.0                      # playback 1.0..2.0, 2.0..3.0: the chunk at 1.5 and at 2.2 are early enough
    late = sp.simulate_playback([(1.0, sr), (3.0, 2 * sr)])                       # the second chunk is 1 s late (audio ran out at 2.0)
    assert late["stall_ms_total"] == 1000.0 and late["stall_ms_max"] == 1000.0
    assert sp.simulate_playback([])["ttfa_s"] is None


def test_the_click_score_separates_a_smooth_join_from_a_click():
    t = np.arange(24000) / sp.SR
    smooth = np.sin(2 * np.pi * 200 * t).astype(np.float32) * 0.3
    clicked = smooth.copy()
    clicked[12000:] += 0.4                                                        # a step at sample 12000
    assert sp.click_scores(smooth, [12000])[0] < 3
    assert sp.click_scores(clicked, [12000])[0] > 8


def test_self_check_passes():
    sp.self_check()
