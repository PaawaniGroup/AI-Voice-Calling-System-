"""Voice Studio guard rails that need no database: defaults (flag OFF, limits), the dashboard code, the call path left alone, and no audio files in the code and test folders."""
import re
from pathlib import Path

import pytest

from app.ai.service import AiService
from config.settings import Settings
from tests.conftest import make_settings

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "app" / "static" / "dashboard" / "app.js").read_text(encoding="utf-8")
SECTION = JS.split("// ---------------------------------------------------------------- Voice Studio", 1)[1].split("// ---------------------------------------------------------------- logs & system", 1)[0]


def test_the_flag_is_off_in_the_default_tenant_configuration():
    svc = AiService.__new__(AiService)
    svc.s = make_settings()
    assert AiService.default_config(svc, "t1")["voice_studio"] is False


def test_the_shipped_limits_match_the_design():
    s = Settings(_env_file=None)
    assert s.voice_studio_text_max_chars == 300 and s.voice_studio_previews_per_hour == 20 and s.voice_studio_uploads_per_day == 20
    assert s.voice_studio_spend_cap_usd == 5.0 and s.voice_studio_max_upload_bytes == 8_000_000 and s.voice_studio_job_deadline_s == 180.0
    with pytest.raises(ValueError):
        Settings(_env_file=None, voice_studio_text_max_chars=301)             # the 300-character limit cannot be raised by configuration


def test_the_page_builds_dom_nodes_only_like_the_rest_of_the_dashboard():
    assert SECTION.count("function voices") == 1
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "<script"):
        assert banned not in SECTION, banned
    assert "isRole('admin')" in SECTION and "stopLive" in SECTION                     # admin only; polling stops when the page is left
    assert "Warming up the voice server. This can take about a minute" in SECTION and "consent" in SECTION.lower()
    assert re.search(r"voices[,\s]", JS.split("const pages = {", 1)[1].split("}", 1)[0])  # registered as a page


def test_the_navigation_link_is_for_admins_only():
    html = (ROOT / "app" / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert '<a href="#/voices" data-role="admin">Voice Studio</a>' in html and "<script>" not in html


def test_only_the_upload_route_has_a_larger_body_limit_and_the_csp_allows_blob_media_only_for_the_player():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert main.count("voice_studio_max_upload_bytes") == 1 and 'request.url.path.rstrip("/") == "/v1/voices"' in main
    assert "media-src 'self' blob:" in main and "script-src 'self'" in main and "'unsafe-inline'" not in main


def test_the_call_path_does_not_know_about_voice_studio():
    for rel in ("app/ai/clients.py", "app/ai/pipeline.py", "app/ai/ws.py", "app/ai/streaming/live.py", "app/ai/streaming/turn.py", "app/ai/streaming/playout.py", "app/ai/media.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "voice_studio" not in src and "app.voices" not in src, rel


def test_no_audio_files_were_added_to_the_code_documentation_or_test_folders():
    for folder in ("app", "tests", "docs", "deploy", "tools", "scripts", "config"):
        found = [p for p in (ROOT / folder).rglob("*") if p.suffix.lower() in (".wav", ".mp3", ".ogg", ".m4a", ".flac", ".opus", ".aac")]
        assert not found, found


def test_the_worker_default_for_request_references_is_off_in_the_dockerfile_too():
    df = (ROOT / "deploy" / "tts_worker_runpod" / "Dockerfile").read_text(encoding="utf-8")
    assert "ALLOW_REQUEST_REF=1" not in df
