"""The streaming pipeline's built-in clause sizes come from the 25 Oct 2026 TTS re-measurement (docs/evidence/phase0b_tts_v3.json). The pipeline stays OFF."""
import json
from pathlib import Path

from app.ai.streaming.config import PROVISIONAL, STREAM_KEYS, stream_config_from, value

ROOT = Path(__file__).resolve().parents[2]


def test_defaults_follow_the_measured_clause_sizes():
    assert (PROVISIONAL["stream_first_min_words"], PROVISIONAL["stream_min_words"], PROVISIONAL["stream_max_words"], PROVISIONAL["stream_prefetch_depth"]) == (4, 12, 14, 2)
    cfg = stream_config_from({})
    assert cfg.segmenter.first_min_words == 4 and cfg.segmenter.min_words == 12 and cfg.segmenter.max_words == 14 and cfg.prefetch_depth == 2


def test_every_default_is_inside_the_range_the_api_accepts():
    for key, (lo, hi) in STREAM_KEYS.items():
        assert lo <= PROVISIONAL[key] <= hi, key


def test_a_tenant_value_still_overrides_the_default():
    cfg = stream_config_from({"stream_first_min_words": 2, "stream_min_words": 8, "stream_max_words": 20, "stream_prefetch_depth": 3})
    assert (cfg.segmenter.first_min_words, cfg.segmenter.min_words, cfg.segmenter.max_words, cfg.prefetch_depth) == (2, 8, 20, 3)
    assert value({"stream_min_words": None}, "stream_min_words") == 12


def test_the_measurement_behind_the_numbers_is_in_the_evidence_file():
    """Wall time per second of audio (variant A): above 1 below 12 words, about 1 from 12 words on."""
    p = ROOT / "docs" / "evidence" / "phase0b_tts_v3.json"
    rows = [r for r in json.loads(p.read_text(encoding="utf-8"))["rows"] if r["variant"] == "A"]
    ratio = {}
    for w in (4, 8, 12, 16):
        xs = sorted(r["wall_ms"] / 1000 / r["audio_s"] for r in rows if r["words"] == w)
        ratio[w] = xs[len(xs) // 2]
    assert ratio[4] > 1.3 and ratio[8] > 1.1 and ratio[12] < 1.05 and ratio[16] < 1.05, ratio
