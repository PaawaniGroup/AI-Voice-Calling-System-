"""tools/voice_queue_test.py: the pure helpers and the refusals. SIMULATED: no network, no RunPod; the tool itself is never run by a test."""
import asyncio
import pathlib
import sys

import httpx
import pytest

from app.ai.clients import ModelClients
from config.settings import Settings
from tools import voice_queue_test as t

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_job_class_comes_only_from_the_jobs_own_queue_time():
    assert t.classify_queue(45207.0) == "cold_start" and t.classify_queue(20000) == "cold_start"
    assert t.classify_queue(19999) == "in_between" and t.classify_queue(5000) == "in_between"
    assert t.classify_queue(4999) == "already_warm" and t.classify_queue(0) == "already_warm"
    assert t.classify_queue(None) is None and t.classify_queue("x") is None


def test_projected_cost_counts_the_idle_tail_and_both_prices():
    c = t.projected_cost(elapsed_s=100, next_gap_s=20, idle_tail_s=300, cold_s=0, price_per_s=0.0003)
    assert c == pytest.approx((100 + 20 + 5 + 300) * 0.0003)
    assert t.projected_cost(100, 20, 300, 0, 0.00019) < c


async def test_the_completed_counter_reads_only_the_jobs_completed_field():
    def handler(req: httpx.Request):
        return httpx.Response(200, json={"workers": {"idle": 2, "ready": 2, "running": 0, "initializing": 0}, "jobs": {"completed": 102, "inQueue": 0}})

    cl = ModelClients(Settings(_env_file=None), {"tts_url": "https://api.example.invalid/v2/ep/runsync", "keys": {"tts": "k"}}, transport=httpx.MockTransport(handler))
    try:
        assert await t.completed_counter(cl) == 102
    finally:
        await cl.aclose()
    bad = ModelClients(Settings(_env_file=None), {"tts_url": "https://api.example.invalid/v2/ep/runsync", "keys": {"tts": "k"}},
                       transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    try:
        assert await t.completed_counter(bad) is None
    finally:
        await bad.aclose()


def _run(monkeypatch, argv, capsys):
    monkeypatch.setattr(sys, "argv", ["voice_queue_test.py", *argv])
    rc = asyncio.run(t.main())
    return rc, capsys.readouterr().out


def test_the_tool_refuses_without_an_approval_with_too_many_jobs_or_a_result_file_in_the_repo(monkeypatch, capsys):
    rc, out = _run(monkeypatch, ["--label", "x"], capsys)
    assert rc == 2 and "no --approved" in out
    rc, out = _run(monkeypatch, ["--label", "x", "--approved", "id", "--jobs", "7"], capsys)
    assert rc == 2 and "at most 6" in out
    rc, out = _run(monkeypatch, ["--label", "x", "--approved", "id", "--result-file", str(ROOT / "result.json")], capsys)
    assert rc == 2 and "OUTSIDE the repository" in out


def test_the_tool_does_not_read_worker_counts():
    src = (ROOT / "tools" / "voice_queue_test.py").read_text(encoding="utf-8")
    for bad in ('"idle"', '"ready"', '"running"', '"initializing"'):               # the worker-slot fields of /health are never read
        assert bad not in src, bad
    assert '"jobs"' in src and '"completed"' in src                                   # only the completed-jobs counter is
