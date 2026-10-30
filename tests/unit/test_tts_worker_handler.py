"""The RunPod TTS worker source kept in deploy/tts_worker_runpod/: it must always compile, and, where numpy/scipy/soundfile are installed, its own
no-GPU self-test (fake model) must pass. The worker's real dependencies (torch, chatterbox, runpod) are NOT needed and are not used here."""
import importlib.util
import py_compile
import subprocess
import sys
from pathlib import Path

import pytest

HANDLER = Path(__file__).resolve().parents[2] / "deploy" / "tts_worker_runpod" / "handler.py"


def test_the_worker_source_compiles(tmp_path):
    py_compile.compile(str(HANDLER), cfile=str(tmp_path / "h.pyc"), doraise=True)


@pytest.mark.skipif(any(importlib.util.find_spec(m) is None for m in ("numpy", "scipy", "soundfile")),
                    reason="numpy/scipy/soundfile are not installed in this environment (the service itself does not need them)")
def test_the_worker_self_test_passes_without_a_gpu():
    r = subprocess.run([sys.executable, str(HANDLER), "--self-test"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-500:]
    assert "checks passed" in r.stdout and "FAIL" not in r.stdout


def test_the_handler_never_deep_copies_the_prepared_voice():
    """The first v3 image crashed every worker at start-up: Chatterbox's prepared voice holds graph tensors that PyTorch refuses to deep-copy. Guard: no deepcopy call
    in the handler (a shallow copy of the object and its dict is used), and the self-test carries a fake voice that raises on deepcopy."""
    import re
    src = HANDLER.read_text(encoding="utf-8")
    assert re.search(r"copy\.deepcopy\(|\bdeepcopy\(", src) is None
    assert "def __deepcopy__" in src and "graph leaves" in src                         # the fake that models the real failure is part of the self-test
    assert "copy.copy(conds)" in src


def test_the_readme_documents_the_exact_start_up_error_of_the_v3_image():
    readme = (HANDLER.parent / "README.md").read_text(encoding="utf-8")
    assert "Only Tensors created explicitly by the user (graph leaves) support the deepcopy protocol at the moment" in readme
    assert "Never use the `v3` tag" in readme and "NEW test endpoint" in readme and "30/30 checks passed" in readme


def test_the_workflow_can_only_push_the_v4_tag_and_never_runs_for_main():
    import yaml
    d = yaml.safe_load((HANDLER.parent / "build-image-v3-test.yml").read_text(encoding="utf-8"))
    triggers = d.get("on", d.get(True))                      # PyYAML reads the key `on` as the boolean True
    assert triggers["push"]["branches"] == ["tts-v3"] and "workflow_dispatch" in triggers
    steps = d["jobs"]["build"]["steps"]
    pushed = [s for s in steps if str(s.get("uses", "")).startswith("docker/build-push-action")]
    assert len(pushed) == 1 and pushed[0]["with"]["push"] is True
    assert pushed[0]["with"]["tags"].split() == ["ghcr.io/aitechteammme-dev/mme-tts-runpod:v4"]   # :latest, :v2 and the broken :v3 can never be overwritten


def test_the_readme_warns_against_pushing_the_test_to_main():
    assert "never push this" in (HANDLER.parent / "README.md").read_text(encoding="utf-8").lower()


def test_the_v3_dockerfile_bakes_the_voice_at_the_path_the_handler_reads():
    import re
    src = HANDLER.read_text(encoding="utf-8")
    default = re.search(r'DEFAULT_REF_WAV = os\.getenv\("DEFAULT_REF_WAV", "([^"]+)"\)', src).group(1)
    df = (HANDLER.parent / "Dockerfile").read_text(encoding="utf-8")
    assert f"COPY ref_voice.wav {default}" in df and f"ENV DEFAULT_REF_WAV={default}" in df     # a mismatch would leave the worker with no voice
    assert "COPY handler.py ." in df and 'CMD ["python", "-u", "handler.py"]' in df               # the entry point is unchanged
    assert "snapshot_download('ResembleAI/chatterbox')" in df and df.index("snapshot_download") < df.index("HF_HUB_OFFLINE=1")   # weights first, then offline