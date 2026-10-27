"""Cached acknowledgement: the phrase set, its neutrality check, loading from a directory outside the repository, and rotation.
SIMULATED: the clips here are synthetic tones written to a temporary directory; no voice model, no network, nothing real was generated."""
import json
import random

import pytest

from app.ai import codec
from app.ai.streaming.ack import DENY, PROPOSED, AckSet, offending_words
from app.ai.streaming.config import PROVISIONAL, STREAM_KEYS, stream_config_from
from tests.helpers import pcm_tone

GOOD = [("hi_1.ulaw", "hi", PROPOSED["hi"][0]), ("hi_2.ulaw", "hi", PROPOSED["hi"][1]), ("hi_3.ulaw", "hi", PROPOSED["hi"][2]),
        ("en_1.ulaw", "en", PROPOSED["en"][0]), ("en_2.ulaw", "en", PROPOSED["en"][1])]


def ulaw(ms=1000):
    return codec.pcm16_to_mulaw(pcm_tone(ms, 8000))


def make(tmp_path, clips=GOOD, ms=1000, manifest=True):
    for name, _, _ in clips:
        (tmp_path / name).write_bytes(ulaw(ms))
    if manifest:
        (tmp_path / "manifest.json").write_text(json.dumps({"clips": [{"file": n, "lang": lg, "text": t} for n, lg, t in clips]}, ensure_ascii=False), encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------------ neutrality
def test_the_proposed_phrases_are_neutral():
    for lang, phrases in PROPOSED.items():
        assert len(phrases) >= 2
        for p in phrases:
            assert offending_words(p) == [], (lang, "a proposed phrase agrees or disagrees")


@pytest.mark.parametrize("phrase", ["Yes, one moment.", "No problem.", "Okay.", "OK sir", "Sure.", "Of course.", "Right away.", "haan ji",
                                    "हाँ, एक सेकंड।", "हां जी।", "जी।", "नहीं।", "ठीक है।", "बिल्कुल।", "सही बात।", "अच्छा।", "ज़रूर।", "जरूर देखता हूँ।"])
def test_phrases_that_could_claim_agreement_are_caught(phrase):
    assert offending_words(phrase), phrase


def test_the_deny_list_covers_the_plain_yes_no_ok_family():
    assert {"yes", "no", "ok", "okay", "haan", "नहीं", "जी", "ठीक"} <= DENY


# ------------------------------------------------------------------ loading
def test_a_good_directory_loads_and_lists_both_languages(tmp_path):
    s, why = AckSet.load(make(tmp_path))
    assert why == "ok" and s is not None and s.langs == ["en", "hi"]
    c = s.pick("hi")
    assert c.lang == "hi" and c.ms == 1000 and len(c.pcm8) == 16000                 # 1 s of PCM16 at 8 kHz


@pytest.mark.parametrize("directory", [None, "", "/nonexistent/ack/dir"])
def test_missing_directory_is_silently_unavailable(directory):
    s, why = AckSet.load(directory)
    assert s is None and why in ("no_dir", "no_manifest")


def test_missing_manifest_missing_file_and_bad_files_never_raise(tmp_path):
    assert AckSet.load(make(tmp_path, manifest=False)) == (None, "no_manifest")
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")
    assert AckSet.load(tmp_path) == (None, "bad_manifest")
    (tmp_path / "manifest.json").write_text(json.dumps({"clips": []}), encoding="utf-8")
    assert AckSet.load(tmp_path) == (None, "bad_manifest")
    d2 = tmp_path / "gone"
    d2.mkdir()
    make(d2)
    (d2 / "hi_2.ulaw").unlink()
    assert AckSet.load(d2) == (None, "bad_clip")


@pytest.mark.parametrize("ms,why", [(100, "bad_clip"), (5000, "bad_clip")])
def test_clips_must_be_about_a_second(tmp_path, ms, why):
    assert AckSet.load(make(tmp_path, ms=ms)) == (None, why)


def test_a_set_with_one_non_neutral_phrase_is_refused_as_a_whole(tmp_path):
    bad = GOOD[:2] + [("hi_3.ulaw", "hi", "हाँ, एक सेकंड।")] + GOOD[3:]
    assert AckSet.load(make(tmp_path, bad)) == (None, "not_neutral")


def test_a_language_with_a_single_clip_is_switched_off(tmp_path):
    s, why = AckSet.load(make(tmp_path, GOOD[:2] + GOOD[3:4]))                      # two hi, one en
    assert why == "ok" and s.langs == ["hi"] and s.pick("en") is None
    one = tmp_path / "one"
    one.mkdir()
    assert AckSet.load(make(one, GOOD[:1] + GOOD[3:4])) == (None, "too_few_clips")  # one of each: nothing can rotate


@pytest.mark.parametrize("name", ["../evil.ulaw", "a/b.ulaw", "/etc/passwd", ".hidden", ""])
def test_file_names_cannot_leave_the_directory(tmp_path, name):
    (tmp_path / "hi_1.ulaw").write_bytes(ulaw())
    clips = [{"file": "hi_1.ulaw", "lang": "hi", "text": "एक सेकंड।"}, {"file": name, "lang": "hi", "text": "एक पल।"}]
    (tmp_path / "manifest.json").write_text(json.dumps({"clips": clips}, ensure_ascii=False), encoding="utf-8")
    assert AckSet.load(tmp_path)[0] is None


# ------------------------------------------------------------------ rotation
def test_the_same_clip_is_never_played_twice_in_a_row_and_all_are_used(tmp_path):
    s, _ = AckSet.load(make(tmp_path), rng=random.Random(7))
    seq = [s.pick("hi").name for _ in range(600)]
    assert all(a != b for a, b in zip(seq, seq[1:]))
    assert set(seq) == {"hi_1.ulaw", "hi_2.ulaw", "hi_3.ulaw"}
    en = [s.pick("en").name for _ in range(50)]
    assert all(a != b for a, b in zip(en, en[1:])) and set(en) == {"en_1.ulaw", "en_2.ulaw"}


def test_two_clips_alternate_strictly(tmp_path):
    s, _ = AckSet.load(make(tmp_path, GOOD[3:]), rng=random.Random(1))
    seq = [s.pick("en").name for _ in range(10)]
    assert seq[0::2] == [seq[0]] * 5 and seq[1::2] == [seq[1]] * 5 and seq[0] != seq[1]


# ------------------------------------------------------------------ the settings
def test_the_api_model_and_the_settings_accept_the_new_switches():
    """Guard: the per-tenant switches must be settable through the API model, and the directory through the settings (a concurrent edit of either file once dropped them)."""
    from app.api.routes_platform import AiConfigIn
    from config.settings import Settings
    assert {"ack_enabled", "ack_after_ms", "keepalive_enabled", "keepalive_interval_s", "keepalive_max_s"} <= set(AiConfigIn.model_fields)
    assert "ai_ack_dir" in Settings.model_fields and Settings.model_fields["ai_ack_dir"].default == ""



def test_the_wait_has_a_default_inside_the_accepted_range_and_reaches_the_stream_config():
    lo, hi = STREAM_KEYS["ack_after_ms"]
    assert lo <= PROVISIONAL["ack_after_ms"] <= hi
    assert stream_config_from({}).ack_after_s == pytest.approx(0.3)
    assert stream_config_from({"ack_after_ms": 800}).ack_after_s == pytest.approx(0.8)
