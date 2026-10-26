"""The torch-free self-test of the STT worker patch (docs/worker_stt) runs and passes. SIMULATED: pure functions, no model, no RunPod."""
import pathlib
import subprocess
import sys

SELFTEST = pathlib.Path(__file__).resolve().parents[2] / "docs" / "worker_stt" / "selftest.py"


def test_worker_stt_selftest_passes_without_torch():
    r = subprocess.run([sys.executable, "-I", str(SELFTEST)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "checks passed" in r.stdout


def test_worker_stt_logic_does_not_import_torch_numpy_or_transformers():
    import re
    src = (SELFTEST.parent / "stt_logic.py").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:import|from)\s+([A-Za-z0-9_.]+)", src, flags=re.M)      # import statements only (the docstring may name the packages)
    assert set(imports) <= {"__future__", "base64"}, imports
