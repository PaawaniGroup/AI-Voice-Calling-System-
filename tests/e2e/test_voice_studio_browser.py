"""Real-browser test of the Voice Studio dashboard page (headless Chromium via Playwright, local only) against a live uvicorn server, MongoDB test database and a MOCK
RunPod worker (tests/integration/test_voice_studio.FakeWorker). Nothing leaves this machine; no GPU, no paid service. Skipped when Playwright/Chromium are missing."""
import os

import httpx
import pytest
import pytest_asyncio

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.expanduser("~/pw-browsers"))
pw = pytest.importorskip("playwright.async_api")

from app.main import create_app  # noqa: E402
from app.providers.base import MockProvider  # noqa: E402
from app.security import users as userlib  # noqa: E402
from tests.conftest import make_settings  # noqa: E402
from tests.e2e.test_dashboard_browser import PW, login, must_see, wait_until  # noqa: E402
from tests.helpers import live_server  # noqa: E402
from tests.integration.test_voice_studio import TTS_URL, FakeWorker  # noqa: E402
from tests.voice_fixtures import good_clip  # noqa: E402

pytestmark = pytest.mark.browser


@pytest_asyncio.fixture
async def stack(db):
    fake = FakeWorker()
    s = make_settings(mongo_db=db.s.mongo_db, jwt_leeway_s=60, ai_allowed_hosts="tts.test", voice_studio_poll_s=0.01)       # leeway: see test_dashboard_browser.py
    app = create_app(s, db=db, provider=MockProvider(), ai_transport=httpx.MockTransport(fake.handler))
    async with live_server(app) as host:
        for n, r in [("admin1", "admin"), ("view1", "viewer")]:
            await userlib.create_user(db, n, PW, "t1", r)
        try:
            async with pw.async_playwright() as p:
                browser = await p.chromium.launch()
                yield type("S", (), {"url": f"http://{host}", "browser": browser, "app": app, "db": db, "fake": fake})()
                await browser.close()
        except Exception as e:  # browser binaries missing
            if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
                pytest.skip("Chromium not installed")
            raise


async def test_voice_studio_page_upload_report_speak_and_play(stack, tmp_path):
    # --- a viewer never sees the page's link; an admin sees it, and with the flag OFF the page says it is switched off
    viewer = await stack.browser.new_page()
    await login(viewer, stack.url, "view1")
    assert not await viewer.is_visible("nav a[href='#/voices']")
    page = await stack.browser.new_page()
    errors = await login(page, stack.url, "admin1")
    await page.click("nav a[href='#/voices']")
    await must_see(page, "text=switched off for this tenant", errors)
    assert any("404" in e for e in errors)                  # the browser logs the deliberate 404 of a switched-off feature; nothing else may be logged from here on
    errors.clear()

    # --- turn the tenant flag on (the API an administrator would use) and open the page again
    await stack.app.state.ai.put_config("t1", {"voice_studio": True, "tts_url": TTS_URL}, "test")
    await page.click("nav a[href='#/ai']")
    await page.click("nav a[href='#/voices']")
    await must_see(page, "text=Add a voice", errors)
    assert "Previews only" in await page.inner_text("#main")

    # --- upload: consent is required in the page, then the quality report is shown
    clip = tmp_path / "clip.wav"
    clip.write_bytes(good_clip())
    await page.set_input_files("input[type=file]", str(clip))
    await page.fill("input[placeholder='e.g. asha_1']", "asha_1")
    await page.fill("input[placeholder='speaker full name']", "Asha Verma")
    await page.click("text=Upload and check")
    await must_see(page, "text=The consent box must be ticked.", errors)
    await page.check("form input[type=checkbox]")
    await page.click("text=Upload and check")
    await must_see(page, "text=Quality report:", errors)
    body = await page.inner_text("#main")
    assert "Length after trimming" in body and "Speech in the first 6 s" in body and "Speech in the first 10 s" in body and "Background noise" in body
    assert await stack.db["voices"].count_documents({"name": "asha_1"}) == 1

    # --- the list shows the voice after the page reloads itself; speak with a cold worker first
    await must_see(page, "td:has-text('Asha Verma')", errors, timeout=20)
    stack.fake.hold = "queue"
    await page.fill("textarea", "namaste, aapki fees 45000 rupees hai")
    assert (await page.inner_text("span.muted >> text=/ 300")).startswith("36")           # the counter follows the typing
    await page.click("button:text-is('Speak')")
    await must_see(page, "text=Warming up the voice server. This can take about a minute", errors)
    assert await page.is_disabled("button:text-is('Speak')")                                # one preview at a time from this page
    stack.fake.hold = None
    await must_see(page, "text=Ready. Press play.", errors, timeout=30)
    await wait_until(lambda: _src_is_blob(page), what="an audio player with a blob URL")      # the CSP allows media from blob:
    assert not errors, errors
    assert stack.fake.kinds() == ["synth", "register", "synth"]
    assert not await page.is_disabled("button:text-is('Speak')")

    # --- a refusal is explained in plain words
    await page.fill("textarea", "x")
    stack.fake.run_status = 500
    await page.click("button:text-is('Speak')")
    await must_see(page, "text=worker_http_error", None, timeout=30)


async def _src_is_blob(page):
    return ((await page.get_attribute("audio.vs-player", "src")) or "").startswith("blob:")
