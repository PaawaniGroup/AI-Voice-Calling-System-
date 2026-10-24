"""tools/phase0b_tts_remeasure.py, offline: the analysis functions and the measuring class against a FAKE RunPod TTS worker (httpx mock). Spends nothing."""
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "tools", ROOT / "scripts"):
    sys.path.insert(0, str(p))

import phase0b_tts_remeasure as t  # noqa: E402
from app.ai.clients import ModelClients  # noqa: E402
from e2e_latency_bench import Ledger  # noqa: E402
from tests.conftest import make_settings  # noqa: E402
from tests.helpers import MockModels  # noqa: E402

URL = "https://api.runpod.ai/v2/testep"


def fake_runpod(worker: MockModels, seen: list):
    """POST /run -> a job; GET /status -> COMPLETED with delay/execution times and the mock worker's output for that input."""
    jobs = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/run"):
            body = json.loads(req.content)
            jid = f"j{len(jobs) + 1}"
            jobs[jid] = body["input"]
            seen.append(body["input"])
            return httpx.Response(200, json={"id": jid, "status": "IN_QUEUE"})
        jid = req.url.path.rsplit("/", 1)[-1]
        out = {"ok": True} if jobs[jid].get("warmup") else worker._tts_output(jobs[jid])
        return httpx.Response(200, json={"id": jid, "status": "COMPLETED", "delayTime": 20, "executionTime": 1500, "output": out})
    return httpx.MockTransport(handler)


def spike(tmp_path, worker=None, ref=True):
    seen = []
    worker = worker or MockModels()
    kw = {"ai_allowed_hosts": "api.runpod.ai"}
    if ref:
        r = tmp_path / "ref.wav"
        r.write_bytes(b"RIFF" + b"\x00" * 576000)
        kw["ai_tts_reference_wav"] = str(r)
    cl = ModelClients(make_settings(**kw), {"tts_url": URL + "/runsync", "keys": {"tts": "k"}}, fake_runpod(worker, seen))
    ledger = Ledger(t.PRICE, {"stt": 0.0, "brain": 0.0, "tts": 60.0}, 5.0, 0.0)
    return t.V3Spike(cl, ledger, URL, "utkarsh"), cl, seen, worker


# ------------------------------------------------------------------ pure analysis
def test_linfit_recovers_a_known_line():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert t.linfit(xs, [1.4 + 0.6 * x for x in xs]) == (1.4, 0.6)
    assert t.linfit([1.0], [2.0]) is None and t.linfit([2.0, 2.0], [1.0, 3.0]) is None


def test_summarize_rows_gives_fits_and_per_length_ratios():
    rows = [{"words": w, "audio_s": a, "exec_ms": (1.0 + 0.5 * a) * 1000, "wall_ms": (3.0 + 0.5 * a) * 1000, "overhead_ms": 2000.0, "delay_ms": 20.0}
            for w, a in ((4, 2.0), (4, 2.2), (8, 3.0), (8, 3.2), (16, 6.0), (16, 6.4))]
    s = t.summarize_rows(rows)
    assert s["exec_fit_s_vs_audio_s"] == (1.0, 0.5) and s["overhead_fit_s_vs_audio_s"][1] == 0.0
    assert set(s["by_words"]) == {4, 8, 16} and s["by_words"][16]["wall_over_audio"] == pytest.approx((3 + 0.5 * 6.2) / 6.2, abs=0.02)


def test_concurrency_verdicts():
    assert t.concurrency_verdict(4.0, [4.0, 4.2], [20, 30])["verdict"] == "parallel"
    assert t.concurrency_verdict(4.0, [4.0, 8.1, 12.0], [20, 4000, 8000])["verdict"] == "serialised"
    assert t.concurrency_verdict(4.0, [4.0, 5.8], [20, 1500])["verdict"] == "partly parallel"           # 1.45x one job
    assert t.concurrency_verdict(4.0, [4.0, 6.5], [20, 2000])["verdict"] == "serialised"                 # 1.6x for two jobs is mostly one after the other
    assert t.concurrency_verdict(4.0, [4.0, 4.1], [20, 3500])["max_delay_ms"] == 3500           # a queue delay is reported separately from the verdict


# ------------------------------------------------------------------ the three request variants, against a fake worker
async def test_variants_send_what_they_claim_and_the_sizes_show_the_difference(tmp_path):
    sp, cl, seen, _ = spike(tmp_path)
    text = "हॉस्टल में रहने और खाने की सुविधा है।"
    a, pcm_a = await sp.clause(text, "A")
    b, pcm_b = await sp.clause(text, "B")
    c, pcm_c = await sp.clause(text, "C")
    await cl.aclose()
    ia, ib, ic = seen
    assert ia["voice"] == "utkarsh" and ia["output_format"] == "ulaw8k" and "reference_audio_base64" not in ia
    assert ib["voice"] == "utkarsh" and "output_format" not in ib and "reference_audio_base64" not in ib
    assert "reference_audio_base64" in ic and "voice" not in ic and "output_format" not in ic
    assert a["format"] == "ulaw8k" and b["format"] == "wav" and c["format"] == "wav"
    assert a["request_bytes"] < 1500 and c["request_bytes"] > 700_000                      # the upload the new request avoids (about 0.5 KB against 0.77 MB)
    assert a["result_b64_bytes"] * 3 < b["result_b64_bytes"]                              # an 8 kHz mu-law answer is several times smaller than the 24 kHz... WAV
    assert a["audio_s"] == pytest.approx(0.3, abs=0.01) and len(pcm_a) == 2 * 8000 * 300 // 1000          # the mock's 300 ms; decoded to PCM16 at 8 kHz
    assert a["wall_ms"] > 0 and a["overhead_ms"] is not None and a["delay_ms"] == 20 and a["exec_ms"] == 1500


async def test_a_worker_error_stops_the_measurement_with_its_message(tmp_path):
    w = MockModels()
    w.tts_worker = "v2"                                                                     # an old worker: refuses ulaw8k
    sp, cl, _, _ = spike(tmp_path, w)
    with pytest.raises(RuntimeError, match="output_format"):
        await sp.clause("हाँ", "A")
    await cl.aclose()


async def test_report_prints_the_three_variants_and_the_phase0_comparison(tmp_path, capsys):
    sp, cl, seen, _ = spike(tmp_path)
    rows = []
    for text in ("जी, ज़रूर बताइए ना।", "हॉस्टल में रहने और खाने की सुविधा है।"):
        for v in ("A", "B", "C"):
            rows.append((await sp.clause(text, v))[0])
    await cl.aclose()
    p0 = tmp_path / "p0.json"
    p0.write_text(json.dumps({"tts": [{"mode": "run", "words": 4, "audio_s": 2.0, "exec_ms": 2300.0, "wall_ms": 4700.0, "overhead_ms": 2400.0, "delay_ms": 100.0},
                                      {"mode": "run", "words": 8, "audio_s": 3.0, "exec_ms": 3200.0, "wall_ms": 5600.0, "overhead_ms": 2300.0, "delay_ms": 100.0}]}))
    t.report({"endpoint": "testep", "poll_cap_s": 0.15, "rows": rows, "cost": {"usd_estimated_total": 0.01, "budget_usd": 0.15}}, str(p0))
    out = capsys.readouterr().out
    for must in ("A ulaw8k + voice, no reference", "B wav 24k + voice, no reference", "C wav 24k + 768 KB reference", "Phase 0, OLD worker", "A vs Phase 0", "Where the time goes"):
        assert must in out, must
    assert "key" not in out.lower() and "rpa_" not in out


def test_the_listening_files_cannot_be_written_inside_the_repository(tmp_path):
    inside = ROOT / "docs" / "evidence" / "should_not_be_written"
    assert inside.resolve().parent.name == "evidence" and (ROOT in inside.resolve().parents)        # the guard in run_measure rejects exactly this kind of path
    out = tmp_path / "listen"
    out.mkdir()
    t.write_wav(out / "x.wav", b"\x00\x00" * 8000)
    import wave
    w = wave.open(str(out / "x.wav"))
    assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (8000, 1, 2, 8000)
