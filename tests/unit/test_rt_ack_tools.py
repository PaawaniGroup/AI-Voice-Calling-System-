"""The one-off acknowledgement-clip generator and its free checker. SIMULATED: a fake voice client returns synthetic tones; no network, no RunPod, nothing real is generated or billed."""
from __future__ import annotations

import json

import pytest

from app.ai import codec
from app.ai.streaming.ack import AckSet
from tests.helpers import pcm_tone
from tools.realtime import check_ack_clips as C
from tools.realtime import gen_ack_clips as G


class FakeVoice:
    def __init__(self, ms=1000, amp=6000, rate=8000, fail_from=None, status=None):
        self.ms, self.amp, self.rate, self.fail_from, self.status = ms, amp, rate, fail_from, status
        self.calls = 0
        self.closed = False

    async def synthesize(self, text, lang):
        self.calls += 1
        if self.fail_from is not None and self.calls >= self.fail_from:
            e = RuntimeError("voice down")
            e.status_code = self.status
            raise e
        return pcm_tone(self.ms, 8000, self.amp), self.rate

    async def aclose(self):
        self.closed = True


def args(out, **kw):
    a = ["--out-dir", str(out), "--approved", "id-1"]
    for k, v in kw.items():
        a += [f"--{k.replace('_', '-')}", str(v)]
    return a


def test_the_plan_is_the_five_approved_neutral_phrases_with_the_loader_file_names():
    p = G.plan()
    assert [n for n, _, _ in p] == ["hi_1.ulaw", "hi_2.ulaw", "hi_3.ulaw", "en_1.ulaw", "en_2.ulaw"] and len(p) == 5


def test_refusals_without_approval_inside_the_repository_or_with_a_cap_above_the_limit(tmp_path):
    assert "approved" in G.refuse_reason("", str(tmp_path), 0.15)
    assert "OUTSIDE" in G.refuse_reason("id", str(G.ROOT / "docs" / "clips"), 0.15) and "OUTSIDE" in G.refuse_reason("id", str(G.ROOT), 0.15)
    assert "cap" in G.refuse_reason("id", str(tmp_path), 0.5) and "cap" in G.refuse_reason("id", str(tmp_path), 0)
    assert G.refuse_reason("id", str(tmp_path), 0.15) is None


async def test_main_refuses_before_creating_any_job(tmp_path, capsys):
    v = FakeVoice()
    assert await G.main(["--out-dir", str(tmp_path)], clients=v) == 2 and v.calls == 0 and "refused" in capsys.readouterr().out


async def test_one_warmup_then_five_jobs_write_a_set_the_loader_and_the_checker_accept(tmp_path, capsys):
    v = FakeVoice()
    assert await G.main(args(tmp_path / "ack"), clients=v) == 0
    assert v.calls == 6 and v.closed                                               # 1 warm-up + 5 phrases; Active workers are not touched by this tool
    out = capsys.readouterr().out
    assert "hi_" not in out and "text" not in out                                  # numbers only: no file text, no phrase
    s, why = AckSet.load(tmp_path / "ack")
    assert why == "ok" and s.langs == ["en", "hi"]
    assert not list((tmp_path / "ack").glob("*warm*"))                             # the warm-up audio is discarded
    r = C.check(tmp_path / "ack")
    assert r["errors"] == [] and r["load"] == "ok" and len(r["clips"]) == 5
    assert C.main([str(tmp_path / "ack")]) == 0


async def test_the_cost_cap_stops_the_run_before_any_job(tmp_path):
    v = FakeVoice()
    assert await G.main(args(tmp_path / "ack", price_per_s=1.0), clients=v) == 1 and v.calls == 0


async def test_the_first_401_stops_the_run_and_three_errors_stop_it(tmp_path):
    v = FakeVoice(fail_from=1, status=401)
    assert await G.main(args(tmp_path / "a"), clients=v) == 1 and v.calls == 1
    v2 = FakeVoice(fail_from=1, status=500)
    assert await G.main(args(tmp_path / "b"), clients=v2) == 1 and v2.calls == 3


async def test_a_wrong_sample_rate_is_not_written(tmp_path):
    v = FakeVoice(rate=24000)
    assert await G.main(args(tmp_path / "ack"), clients=v) == 1 and not list((tmp_path / "ack").glob("*.ulaw"))


def _folder(tmp_path, ms=1000, amp=6000, text="एक सेकंड।"):
    clips = [("hi_1.ulaw", "hi", text), ("hi_2.ulaw", "hi", "एक पल।"), ("en_1.ulaw", "en", "One moment."), ("en_2.ulaw", "en", "Just a second.")]
    for n, _, _ in clips:
        (tmp_path / n).write_bytes(codec.pcm16_to_mulaw(pcm_tone(ms, 8000, amp)))
    (tmp_path / "manifest.json").write_text(json.dumps({"clips": [{"file": n, "lang": lg, "text": t} for n, lg, t in clips]}, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_the_checker_catches_short_clipped_silent_and_non_neutral_clips(tmp_path):
    (tmp_path / "ok").mkdir()
    assert C.check(_folder(tmp_path / "ok"))["errors"] == []
    (tmp_path / "short").mkdir()
    assert any(e.endswith(":duration") for e in C.check(_folder(tmp_path / "short", ms=100))["errors"])
    (tmp_path / "loud").mkdir()
    assert any(e.endswith(":clipping") for e in C.check(_folder(tmp_path / "loud", amp=32767))["errors"])
    (tmp_path / "quiet").mkdir()
    assert any(e.endswith(":silent") for e in C.check(_folder(tmp_path / "quiet", amp=5))["errors"])
    (tmp_path / "deny").mkdir()
    assert "load:not_neutral" in C.check(_folder(tmp_path / "deny", text="हाँ, एक सेकंड।"))["errors"]
    (tmp_path / "none").mkdir()
    assert C.check(tmp_path / "none")["errors"] == ["no_manifest"]


def test_a_clipped_tone_is_flagged_and_a_clean_speech_level_clip_is_not(tmp_path):
    (tmp_path / "clean").mkdir()
    (tmp_path / "clipped").mkdir()
    clean = C.check(_folder(tmp_path / "clean", amp=6000))
    clipped = C.check(_folder(tmp_path / "clipped", amp=32767))
    assert all(r["clipping_share"] == 0.0 for r in clean["clips"]) and not any(e.endswith(":clipping") for e in clean["errors"])
    assert all(r["clipping_share"] > C.CLIP_SHARE for r in clipped["clips"]) and sum(e.endswith(":clipping") for e in clipped["errors"]) == 4
    assert C.CLIP_LEVEL == 32000 and C.CLIP_SHARE == 0.001


async def test_a_playable_wav_copy_of_each_clip_is_written_next_to_the_mulaw_files(tmp_path, capsys):
    import wave
    assert await G.main(args(tmp_path / "ack"), clients=FakeVoice(ms=1000)) == 0
    names = sorted(p.name for p in (tmp_path / "ack").glob("*.wav"))
    assert names == ["en_1.wav", "en_2.wav", "hi_1.wav", "hi_2.wav", "hi_3.wav"]
    with wave.open(str(tmp_path / "ack" / "hi_1.wav")) as w:
        assert w.getframerate() == 8000 and w.getnchannels() == 1 and w.getsampwidth() == 2 and 0.9 < w.getnframes() / 8000 < 1.1
    assert C.check(tmp_path / "ack")["errors"] == []                      # the extra files do not disturb the loader or the checker