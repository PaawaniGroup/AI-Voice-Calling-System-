"""Real-time latency harness (tools/realtime). SIMULATED: the real streaming code on a VIRTUAL clock with mock models; nothing here says anything about the models' real speed."""
from __future__ import annotations

import json
import sys

import pytest

from tools.realtime import harness as H
from tools.realtime import stages as S

ALLOWED_STRINGS = {"SIMULATED", "MEASURED", "today", "ramp", "punct", "max_words", "hard_max", "flush", "ok"}


def _summ(ev, k):
    return ev["summary"][k]["median"]


def test_the_reply_is_synthetic_with_the_configured_length_and_comma_positions():
    t = H.reply_text(H.Scenario(reply_words=23, first_punct_at=9, later_punct_every=8))
    toks = t.split()
    assert len(toks) == 23 and toks[8].endswith(",") and toks[16].endswith(",") and toks[-1].endswith(".")
    assert all(w.rstrip(",.").startswith("w") for w in toks)                       # placeholder words only: never a real word
    assert "," not in H.reply_text(H.Scenario(first_punct_at=None)).replace(".", "")


def test_the_simulated_run_is_deterministic_and_has_every_stage():
    a = H.run_simulated(H.scenario("today", jitter_pct=10.0, seed=3), 4)
    b = H.run_simulated(H.scenario("today", jitter_pct=10.0, seed=3), 4)
    assert a == b and a["tag"] == "SIMULATED" and a["n_errors"] == 0 and a["n_utterances"] == 4
    for k in S.STAGE_KEYS:
        assert a["summary"][k]["n"] == 4, k
    assert _summ(a, "reply_words") == 23 and _summ(a, "first_clause_words") == 9      # MEASURED median first clause today: 9 words


def test_the_todays_baseline_is_reproduced_within_ten_percent_with_the_measured_inputs():
    """A calibration check of the SIMULATED harness against the 7.51 s baseline (without the 0.2 s path to the ear): 7.31 s MEASURED."""
    ev = H.run_simulated(H.scenario("today", voice_workers=1), 3)
    v = _summ(ev, "to_first_audio_ms")
    base = S.BASELINE_MS["total"] - S.BASELINE_MS["ear"]
    assert abs(v - base) / base < 0.10, (v, base)


def test_the_ramp_starts_audio_earlier_and_the_first_clause_is_three_words():
    today, ramp = H.run_simulated(H.scenario("today"), 3), H.run_simulated(H.scenario("ramp"), 3)
    assert _summ(ramp, "first_clause_words") == 3 and ramp["utterances"][0]["first_clause_stop_reason"] == "punct"
    assert _summ(today, "to_first_audio_ms") - _summ(ramp, "to_first_audio_ms") > 1000.0           # the ramp saves more than a second of first audio


def test_a_second_warm_worker_removes_the_silence_a_cold_one_does_not():
    one = H.run_simulated(H.scenario("ramp", voice_workers=1), 2)
    two = H.run_simulated(H.scenario("ramp", voice_workers=2), 2)
    cold = H.run_simulated(H.scenario("ramp", voice_workers=2, cold_second_worker_s=49.0), 2)
    assert _summ(two, "silence_ms") < _summ(one, "silence_ms") / 2
    assert _summ(cold, "silence_ms") == _summ(one, "silence_ms")                                  # a worker that needs 49 s does not help a 15 s reply


def test_a_shorter_queue_moves_the_first_audio_one_for_one():
    q12 = _summ(H.run_simulated(H.scenario("ramp", voice_queue_s=1.2), 2), "to_first_audio_ms")
    q15 = _summ(H.run_simulated(H.scenario("ramp", voice_queue_s=0.15), 2), "to_first_audio_ms")
    assert round(q12 - q15, -1) == 1050.0


def test_the_evidence_holds_numbers_and_a_few_enumerations_only():
    ev = H.run_simulated(H.scenario("ramp", jitter_pct=5.0), 3)
    json.dumps(ev)

    def walk(o, key=""):
        if isinstance(o, dict):
            for k, v in o.items():
                assert k not in ("text", "words_text", "reply", "heard", "transcript", "audio"), k
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, str):
            assert o in ALLOWED_STRINGS, (key, o)
    walk(ev)


def test_the_stage_numbers_add_up_to_the_first_audio_within_a_millisecond():
    u = H.run_simulated(H.scenario("today"), 1)["utterances"][0]
    parts = u["end_of_turn_ms"] + u["stt_ms"] + u["brain_first_clause_ms"] + u["voice_first_audio_ms"] + u["playout_start_ms"]
    assert abs(parts - u["to_first_audio_ms"]) < 5.0


def test_real_mode_refuses_without_an_approval_without_recordings_and_inside_the_repository(capsys):
    import argparse
    base = dict(approved="", recordings="", cap_usd=0.3)
    assert "no --approved" in H.refuse_reason(argparse.Namespace(**base))
    assert "--recordings" in H.refuse_reason(argparse.Namespace(**{**base, "approved": "id"}))
    assert "OUTSIDE" in H.refuse_reason(argparse.Namespace(**{**base, "approved": "id", "recordings": str(H.ROOT / "recs")}))
    assert H.refuse_reason(argparse.Namespace(**{**base, "approved": "id", "recordings": "/tmp/somewhere_else"})) is None
    assert H.main(["--real"]) == 2 and "no --approved" in capsys.readouterr().out


def test_the_same_code_runs_on_the_real_clock_with_fast_mocks_and_tags_nothing_as_measured_by_itself():
    """The REAL-mode path (RealClock, wrapped clients) exercised with millisecond mocks: a smoke test of the plumbing only (SIMULATED)."""
    import asyncio
    from app.ai.streaming.clock import RealClock

    class Fast:
        cfg = {}

        async def transcribe(self, pcm, rate, language):
            await asyncio.sleep(0.01)
            return {"text": "placeholder", "language": "hi"}

        async def chat_stream(self, messages, tools=None):
            await asyncio.sleep(0.01)
            for part in ("w1 w2 w3, ", "w4 w5 w6 w7 w8."):
                yield {"delta": part}
            yield {"done": True}

        async def synthesize(self, text, language):
            await asyncio.sleep(0.02)
            return b"\x00\x00" * 800, 8000

    u = asyncio.run(H.run_one(RealClock(), Fast(), None, 700.0))
    assert u["stt_ms"] >= 10 and u["to_first_audio_ms"] > 700.0 and u["first_clause_words"] >= 1 and u["voice_jobs"]
