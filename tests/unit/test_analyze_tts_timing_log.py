"""tools/analyze_tts_timing_log.py on synthetic worker log lines (no network, no files of the real worker)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))
import analyze_tts_timing_log as a  # noqa: E402


def lines(n=12, gen_a=0.9, gen_b=0.5, ref=0.0, noise=0.0, extra=""):
    out = []
    for i in range(n):
        audio = 1.5 + 0.4 * i
        gen = gen_a + gen_b * audio + (noise if i % 2 else -noise)
        out.append(f"2026-10-24T21:58:{i:02d}Z [TTS_TIMING] chars={20 + i} audio_s={audio:.2f} ref_s={ref:.3f} generate_s={gen:.3f} encode_s=0.004 total_s={gen + 0.01:.3f} rtf={gen / audio:.2f}{extra}")
    return "\n".join(["some other log line", "[TTS] warmup done in 3.00s"] + out + ["[TTS_ERROR] boom"])


PHASE0 = {"tts": [{"mode": "run", "audio_s": x, "exec_ms": (1.43 + 0.58 * x) * 1000, "overhead_ms": (1.71 + 0.17 * x) * 1000} for x in (2.0, 3.0, 4.0, 5.0, 6.0)]
          + [{"mode": "runsync", "audio_s": 3.0, "exec_ms": 99999.0, "overhead_ms": 99999.0}]}


def test_it_parses_only_timing_lines_and_ignores_the_rest():
    rows = a.parse(lines())
    assert len(rows) == 12 and rows[0]["audio_s"] == 1.5 and rows[0]["voice"] is None and rows[0]["fmt"] is None
    with_extra = a.parse(lines(extra=" voice=utkarsh fmt=ulaw8k"))
    assert with_extra[0]["voice"] == "utkarsh" and with_extra[0]["fmt"] == "ulaw8k"
    assert a.parse("[TTS_TIMING] chars=3 audio_s=oops") == [] and a.parse("nothing here") == []


def test_the_fit_recovers_the_handlers_intercept_and_slope_and_counts_reference_preparations():
    res = a.analyze(a.parse(lines(gen_a=0.9, gen_b=0.5)), None)
    assert res["generate_fit"] == pytest.approx((0.9, 0.5), abs=0.01) and res["ref_s"]["requests_with_reference_prepared"] == 0
    prepared = a.analyze(a.parse(lines(ref=1.2)), None)
    assert prepared["ref_s"]["requests_with_reference_prepared"] == 12 and prepared["ref_s"]["mean_when_prepared_s"] == 1.2      # the old per-request cost would show here


def test_the_comparison_separates_handler_runpod_and_dispatch_layers():
    res = a.analyze(a.parse(lines(gen_a=0.9, gen_b=0.5)), PHASE0)
    assert res["runpod_exec_fit"] == pytest.approx((1.43, 0.58), abs=0.01) and res["dispatch_fit"] == pytest.approx((1.71, 0.17), abs=0.01)   # only the /run rows
    row = next(x for x in res["table"] if x["audio_s"] == 4.0)
    assert row["handler_total_s"] == pytest.approx(0.9 + 0.5 * 4 + 0.01, abs=0.03)
    assert row["runpod_exec_s"] == pytest.approx(1.43 + 0.58 * 4, abs=0.02)
    assert row["outside_handler_s"] == pytest.approx(row["runpod_exec_s"] - row["handler_total_s"], abs=0.01)
    assert row["wall_s"] == pytest.approx(row["runpod_exec_s"] + row["dispatch_s"], abs=0.01)


def test_too_few_lines_do_not_crash(capsys):
    res = a.analyze(a.parse("[TTS_TIMING] chars=3 audio_s=1.0 ref_s=0 generate_s=1 encode_s=0 total_s=1 rtf=1"), None)
    a.report(res)
    assert "not enough lines" in capsys.readouterr().out


def test_report_prints_the_table(capsys):
    a.report(a.analyze(a.parse(lines()), PHASE0))
    out = capsys.readouterr().out
    assert "outside the handler" in out and "dispatch around the job" in out and "handler generate_s =" in out
