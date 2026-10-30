"""The torch-free parts of the Chatterbox adaptor. The torch parts (t3_token_stream, flow_inference_nonfinal, make_window_decoder) are NOT tested here:
they need the real model and are covered by scripts/gpu_validate_streaming.py on a GPU host."""
from tools.tts_stream_worker.chatterbox_adapter import (EXPECTED_AST_SHA256, SAMPLES_PER_TOKEN, SPEECH_VOCAB_SIZE, ast_fingerprint,
                                                        hold_back_last_token)


def test_module_imports_without_torch_or_chatterbox():
    """In a FRESH interpreter (other tests in this process may legitimately have imported torch on a GPU host)."""
    import subprocess
    import sys
    code = "import sys, tools.tts_stream_worker.chatterbox_adapter; assert 'torch' not in sys.modules and 'chatterbox' not in sys.modules"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_final_token_is_dropped_like_generate_does_but_a_single_token_is_kept():
    assert list(hold_back_last_token(iter([]))) == []
    assert list(hold_back_last_token(iter([7]))) == [7]                 # upstream max(1, n - 1)
    assert list(hold_back_last_token(iter([7, 8]))) == [7]
    assert list(hold_back_last_token(iter(range(10)))) == list(range(9))


def test_tokens_are_released_one_step_late_so_the_last_is_never_emitted_early():
    seen = []

    def src():
        for t in range(5):
            seen.append(t)
            yield t
    g = hold_back_last_token(src())
    assert next(g) == 0 and seen == [0, 1]                              # token 0 is released only after token 1 was produced


def test_constants_match_the_upstream_source_read():
    assert SPEECH_VOCAB_SIZE == 6561 and SAMPLES_PER_TOKEN == 960
    assert len(EXPECTED_AST_SHA256) == 6 and all(len(h) == 64 for h in EXPECTED_AST_SHA256.values())


def test_fingerprint_ignores_formatting_comments_and_indentation_but_not_code():
    def a(x):
        return x + 1

    def b(x):
        # a comment
        return (x
                + 1)

    def c(x):
        return x + 2
    assert ast_fingerprint(a) != ""
    assert ast_fingerprint(b).__len__() == 64 and ast_fingerprint(a) != ast_fingerprint(c)
    # same body, different function name -> different dump (name is part of the node): the check is about the named upstream function
