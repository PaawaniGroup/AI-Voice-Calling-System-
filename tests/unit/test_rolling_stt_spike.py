"""tools/rolling_stt_spike.py with a FAKE speech recogniser (no network, no spend, no recordings)."""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import rolling_stt_spike as r  # noqa: E402

RATE = 8000
SENTENCE = "mujhe admission ke liye kaun se documents lagte hain aur fees kitni hai".split()


def test_words_drop_punctuation_but_keep_devanagari_vowel_signs():
    assert r.words("Mujhe, B.Tech ki fees?") == ["mujhe", "b", "tech", "ki", "fees"]
    assert r.words("इस कोर्स की फीस कितनी है?") == ["इस", "कोर्स", "की", "फीस", "कितनी", "है"]


def test_wer_counts_substitutions_insertions_deletions():
    ref = "a b c d".split()
    assert r.wer(ref, ref) == 0.0 and r.wer(ref, "a x c d".split()) == 0.25 and r.wer(ref, "a b c".split()) == 0.25 and r.wer(ref, "a b c d e".split()) == 0.25
    assert r.wer([], []) == 0.0 and r.wer([], ["x"]) == 1.0 and r.wer(ref, []) == 1.0


def test_stitch_counts_the_overlap_once_and_concatenates_when_there_is_none():
    assert r.stitch("a b c d".split(), "c d e f".split()) == "a b c d e f".split()
    assert r.stitch("a b".split(), "c d".split()) == "a b c d".split()
    assert r.stitch([], "c".split()) == ["c"] and r.stitch(["a"], []) == ["a"]


def test_split_audio_needs_enough_audio():
    assert r.split_audio(b"\x00\x00" * RATE * 2, 1.5, 0.7) is None                      # 2 s: too short
    pre, tail = r.split_audio(b"\x00\x00" * RATE * 6, 1.5, 0.7)
    assert len(pre) / 2 / RATE == pytest.approx(6 - 1.5 + 0.7) and len(tail) / 2 / RATE == pytest.approx(1.5 + 0.7)


class FakeStt:
    """Says one word per 0.5 s of audio; a decode takes a fixed time plus a share of the audio length (like the model does), without real sleeping."""
    def __init__(self, fixed=0.3, per_s=0.1, garble_tail=False):
        self.fixed, self.per_s, self.garble_tail, self.calls = fixed, per_s, garble_tail, 0

    async def transcribe(self, pcm, rate, lang):
        self.calls += 1
        dur = len(pcm) / 2 / rate
        n = int(dur / 0.5)
        return {"text": " ".join((SENTENCE * 3)[:n])}


def test_measure_end_to_end_with_a_fake_recogniser(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(r.time, "perf_counter", lambda: clock["t"])
    stt = FakeStt()
    orig = stt.transcribe

    async def slow(pcm, rate, lang):
        clock["t"] += 0.3 + 0.1 * len(pcm) / 2 / rate             # fixed 0.3 s + 0.1 s per second of audio
        return await orig(pcm, rate, lang)
    stt.transcribe = slow
    files = {"u01": b"\x01\x00" * RATE * 6, "u02": b"\x01\x00" * RATE * 3, "u03": b"\x01\x00" * RATE * 8}
    res = asyncio.run(r.measure(stt, files, r.Spend(0.20)))
    s = res["summary"]
    assert s["n_measured"] == 2 and s["n_skipped_too_short"] == 1 and s["n_errors"] == 0
    assert s["tail_wall_s"]["p50"] < s["whole_wall_s"]["p50"]                              # a shorter audio decodes faster
    assert res["estimated_cost_usd"] < 0.20 and "NOT an invoice" in res["basis"]
    row = next(x for x in res["rows"] if x["file"] == "u01")
    assert row["final_delay_s"] == pytest.approx(row["tail_wall_s"], abs=0.5) and 0.0 <= row["wer"] <= 1.0
    # the output holds numbers and file ids only: no transcript word
    assert not any(isinstance(v, str) and v not in ("u01", "u02", "u03") and "mujhe" in v for row in res["rows"] for v in row.values())
    assert "mujhe" not in str(res["summary"])


def test_the_budget_stops_the_run_before_it_is_exceeded():
    stt = FakeStt()
    spend = r.Spend(0.0111)                                                  # the idle tail alone is 0.01152: not even one file fits
    res = asyncio.run(r.measure(stt, {"u01": b"\x01\x00" * RATE * 6}, spend, warmup=False))
    assert stt.calls == 0 and res["summary"]["n_measured"] == 0 and res["verdict"] == "NO MEASUREMENT"


def test_both_versions_of_every_recording_share_one_budget_and_are_reported_separately(tmp_path):
    import wave
    for name in ("u01_hi_S1", "u02_en_S1", "roomtone_S1"):
        with wave.open(str(tmp_path / f"{name}.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(b"\x10\x00" * RATE * 6)
    by_variant = r.load_dir_variants(tmp_path)
    assert set(by_variant) == {"clean8k", "phone"} and all(set(v) == {"u01_hi_S1", "u02_en_S1"} for v in by_variant.values())
    stt, spend, out = FakeStt(), r.Spend(0.20), {}
    for i, (name, files) in enumerate(by_variant.items()):
        out[name] = asyncio.run(r.measure(stt, files, spend, warmup=(i == 0)))
    assert set(out) == {"clean8k", "phone"} and stt.calls == 1 + 2 * 3 * 2          # one warm-up, then three decodes per file per version
    assert all(o["summary"]["n_measured"] == 2 for o in out.values())


def test_verdict_rules():
    s = {"rolling_final_delay_s": {"p50": 0.2}, "wer_vs_whole": {"mean": 0.01}}
    assert r.verdict(s) == "GO"
    assert r.verdict({"rolling_final_delay_s": {"p50": 0.4}, "wer_vs_whole": {"mean": 0.01}}) == "NO-GO"
    assert r.verdict({"rolling_final_delay_s": {"p50": 0.2}, "wer_vs_whole": {"mean": 0.05}}) == "NO-GO"
    assert r.verdict({"rolling_final_delay_s": {"p50": None}, "wer_vs_whole": {"mean": None}}) == "NO MEASUREMENT"
