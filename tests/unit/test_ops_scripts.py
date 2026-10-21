"""Harness tests for scripts/ai_bench.py and scripts/collect_call_evidence.py.

These run against MOCK model servers: they prove the scripts' logic (refusal, statistics, masking, no fabrication),
NOT model performance. Real numbers come only from running the scripts against the real endpoints.
"""
import argparse
import asyncio
import importlib.util
import json
import struct
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

from tests.helpers import MockModels

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def args(**kw):
    base = dict(stt=None, brain=None, tts=None, wav=None, repeats=3, timeout=5.0, levels=[1, 2], out=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_bench_refuses_without_endpoints(capsys):
    bench = load("ai_bench")
    assert bench.main([]) == 2
    assert "refusing" in capsys.readouterr().out


def test_bench_percentiles():
    bench = load("ai_bench")
    s = bench.summarize([10, 20, 30, 40, 50])
    assert s["n"] == 5 and s["p50_ms"] == 30 and s["max_ms"] == 50
    assert bench.summarize([])["p50_ms"] is None


def test_bench_runs_and_does_not_invent_stt_audio(tmp_path):
    bench = load("ai_bench")
    models = MockModels()
    res = asyncio.run(bench.run(args(stt="http://127.0.0.1:1", brain="http://127.0.0.1:2", tts="http://127.0.0.1:3"), models.transport()))
    assert res["stages"]["stt"]["skipped"]  # no --wav -> no synthetic audio
    assert res["stages"]["tts"]["per_language"]["en"]["n"] == 3
    assert res["stages"]["brain"]["concurrency"]["2"]["n"] >= 2
    assert not res["any_errors"]
    assert "key" not in json.dumps(res).lower().replace("keys", "")


def test_bench_stt_with_wav(tmp_path):
    bench = load("ai_bench")
    p = tmp_path / "s.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(struct.pack("<8000h", *([1000, -1000] * 4000)))
    res = asyncio.run(bench.run(args(stt="http://127.0.0.1:1", wav=str(p)), MockModels().transport()))
    assert res["stages"]["stt"]["n"] == 3 and res["stages"]["stt"]["audio_seconds"] == 0.5


def test_bench_counts_errors():
    bench = load("ai_bench")
    models = MockModels()
    models.fail = {"tts": 500}
    res = asyncio.run(bench.run(args(tts="http://127.0.0.1:3"), models.transport()))
    assert res["any_errors"] is True


def test_evidence_masks_numbers_and_drops_internal_fields():
    ev = load("collect_call_evidence")
    doc = {"to_number": "+919876543210", "purge_at": datetime.now(timezone.utc), "n": {"from_number": "+14155550100", "at": datetime(2026, 1, 1, tzinfo=timezone.utc)}}
    out = ev.clean(doc, False)
    assert "9876543210" not in json.dumps(out) and "4155550100" not in json.dumps(out)
    assert "purge_at" not in out and out["n"]["at"].startswith("2026-01-01")
    assert ev.clean(doc, True)["to_number"] == "+919876543210"


def test_evidence_reports_missing_call_honestly():
    ev = load("collect_call_evidence")

    class Coll:
        async def find_one(self, *_):
            return None

    class Db:
        def __getitem__(self, _):
            return Coll()

    assert asyncio.run(ev.collect(Db(), "nope", False)) == {"call_id": "nope", "error": "call not found"}
