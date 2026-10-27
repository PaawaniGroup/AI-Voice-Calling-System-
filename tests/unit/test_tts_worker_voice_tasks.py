"""Voice Studio tasks of the RunPod TTS worker (deploy/tts_worker_runpod/handler.py): register_voice, voices, needs_reference, the LRU cap.

The worker's own self-test needs numpy, scipy and soundfile, which are not installed in the service's venv. The voice-task checks are therefore one function
(`selftest_voice_tasks`) that needs the standard library only; here the handler module is loaded with those three imports STUBBED and that function is run,
plus a few direct checks. The model is a FAKE (no GPU, no torch, no chatterbox): what happens on a real GPU is UNVERIFIED and is covered by the measuring plan
in docs/VOICE_STUDIO_DESIGN.md, not by this file."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

HANDLER = Path(__file__).resolve().parents[2] / "deploy" / "tts_worker_runpod" / "handler.py"


def load_handler(monkeypatch, **env):
    for k in ("ALLOW_REQUEST_REF", "MAX_REGISTERED_VOICES", "MAX_REGISTER_BYTES"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    stubs = {}
    for name in ("numpy", "soundfile", "scipy", "scipy.signal"):
        stubs[name] = types.ModuleType(name)
    stubs["numpy"].ndarray = object                       # used in annotations at definition time only
    stubs["scipy.signal"].resample_poly = lambda *a, **k: None
    stubs["scipy"].signal = stubs["scipy.signal"]
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)       # restored after the test: the stubs never leak into other tests
    spec = importlib.util.spec_from_file_location("tts_handler_under_test", HANDLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_checks(mod):
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))

    mod.selftest_voice_tasks(check)
    return results


def test_the_voice_task_checks_all_pass_with_a_fake_model(monkeypatch):
    results = run_checks(load_handler(monkeypatch))
    failed = [(n, d) for n, ok, d in results if not ok]
    assert not failed, failed
    assert len(results) >= 25                                  # the function really ran its checks (not an empty pass)


def test_the_checks_leave_the_module_as_they_found_it(monkeypatch):
    mod = load_handler(monkeypatch)
    before = (dict(mod.VOICES), set(mod.BAKED), dict(mod.REGISTERED), mod.MAX_REGISTERED_VOICES, mod.ALLOW_REQUEST_REF, mod.MODEL)
    run_checks(mod)
    assert (dict(mod.VOICES), set(mod.BAKED), dict(mod.REGISTERED), mod.MAX_REGISTERED_VOICES, mod.ALLOW_REQUEST_REF, mod.MODEL) == before


def test_the_real_self_test_runs_the_voice_task_checks():
    src = HANDLER.read_text(encoding="utf-8")
    assert "selftest_voice_tasks(check)" in src.split("def run_self_test")[1]


def test_request_reference_is_off_by_default_and_the_cap_comes_from_the_environment(monkeypatch):
    mod = load_handler(monkeypatch)
    assert mod.ALLOW_REQUEST_REF is False and mod.MAX_REGISTERED_VOICES == 8
    mod = load_handler(monkeypatch, MAX_REGISTERED_VOICES="3")
    assert mod.MAX_REGISTERED_VOICES == 3
    mod = load_handler(monkeypatch, MAX_REGISTERED_VOICES="0")
    assert mod.MAX_REGISTERED_VOICES == 1                       # at least one registered voice can exist
    mod = load_handler(monkeypatch, ALLOW_REQUEST_REF="1")
    assert mod.ALLOW_REQUEST_REF is True                        # only an explicit environment setting turns it on


def test_the_stt_model_id_is_not_part_of_the_tts_worker():
    assert "mme_stt_model" not in HANDLER.read_text(encoding="utf-8")


def test_a_registration_never_replaces_a_baked_voice(monkeypatch):
    mod = load_handler(monkeypatch)
    mod.VOICES.clear()
    mod.BAKED.clear()
    mod.REGISTERED.clear()
    baked = object()
    mod.VOICES["utkarsh"] = baked
    mod.BAKED.add("utkarsh")
    r = mod.handler({"input": {"task": "register_voice", "name": "utkarsh", "reference_audio_base64": "AAAA"}})
    assert r["code"] == "name_reserved" and mod.VOICES["utkarsh"] is baked and not mod.REGISTERED


@pytest.mark.parametrize("task", ["voices"])
def test_voices_task_needs_no_model(monkeypatch, task):
    mod = load_handler(monkeypatch)
    mod.VOICES.clear()
    mod.BAKED.clear()
    mod.REGISTERED.clear()
    r = mod.handler({"input": {"task": task}})
    assert r["ok"] is True and r["voices"] == [] and r["max_registered"] == 8
