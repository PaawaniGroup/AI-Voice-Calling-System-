"""Real-browser test of the dashboard (headless Chromium via Playwright) against a live uvicorn server.
Provider is the test MOCK (no calls are placed). Skipped when Playwright/Chromium are not installed
(see docs/TEST_REPORT.md for the install commands)."""
import os

import pytest
import pytest_asyncio

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.expanduser("~/pw-browsers"))
pw = pytest.importorskip("playwright.async_api")

from app.main import create_app  # noqa: E402
from app.providers.base import MockProvider  # noqa: E402
from app.security import users as userlib  # noqa: E402
from tests.conftest import enable_tenant, make_settings  # noqa: E402
from tests.helpers import live_server  # noqa: E402

pytestmark = pytest.mark.browser
PW = "correct horse battery"


@pytest_asyncio.fixture
async def stack(db):
    s = make_settings(mongo_db=db.s.mongo_db)
    prov = MockProvider()
    app = create_app(s, db=db, provider=prov)
    async with live_server(app) as host:
        repos = app.state.repos
        await enable_tenant(type("E", (), {"repos": repos}))
        for n, r in [("admin1", "admin"), ("view1", "viewer")]:
            await userlib.create_user(db, n, PW, "t1", r)
        try:
            async with pw.async_playwright() as p:
                browser = await p.chromium.launch()
                yield type("S", (), {"url": f"http://{host}", "browser": browser, "app": app, "prov": prov, "db": db, "repos": repos})()
                await browser.close()
        except Exception as e:  # browser binaries missing
            if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
                pytest.skip("Chromium not installed")
            raise


async def login(page, url, user):
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto(url + "/dashboard/")
    await page.fill("#lu", user)
    await page.fill("#lp", PW)
    await page.click("#login-form button")
    await page.wait_for_selector("#app:not(.hidden)")
    return errors


async def test_admin_flow_overview_live_detail_hangup_and_ai_banner(stack):
    page = await stack.browser.new_page()
    errors = await login(page, stack.url, "admin1")
    assert "admin1 @ t1 (admin)" in await page.inner_text("#who")
    # overview from real data
    await page.wait_for_selector("text=Active calls")
    body = await page.inner_text("#main")
    assert "Database" in body and "FreeSWITCH event listener" in body and "up" in body
    assert "NOT ready" in body or "media bridge" in body.lower()
    # place a call through the UI form
    await page.click("nav a[href='#/controls']")
    await page.fill("input[placeholder='+91…']", "+919876543210")
    await page.fill("input[placeholder='approved caller id']", "+911140000000")
    page.once("dialog", lambda d: d.accept())
    await page.click("text=Place call")
    await page.wait_for_selector("text=Accepted:")
    call = await stack.db["calls"].find_one({})
    assert call and call["to_number"] == "+919876543210" and call["state"] in ("queued", "initiating")
    # live table receives it via SSE
    await page.click("nav a[href='#/live']")
    await page.wait_for_selector(f"text={call['_id']}")
    assert (await page.inner_text("#main .pill >> nth=0")) in ("live", "polling", "connecting")
    await page.wait_for_function("document.querySelector('#main .pill').textContent === 'live'", timeout=10000)
    # detail page, timeline, hangup with confirmation
    await page.click(f"text={call['_id']}")
    await page.wait_for_selector("text=Lifecycle timeline")
    assert "—" in await page.inner_text("#main")  # missing data is a dash, never invented
    assert "Not available" in await page.inner_text("#main")  # transcript honesty
    page.once("dialog", lambda d: d.accept())
    await page.click("text=Hang up")
    for _ in range(40):
        if stack.prov.hangups:
            break
        await page.wait_for_timeout(100)
    assert stack.prov.hangups and stack.prov.hangups[0][0] == call["_id"]
    # sessions + AI pages render real data
    await page.click("nav a[href='#/sessions']")
    try:
        await page.wait_for_selector("text=Counselling sessions", timeout=5000)
    except Exception:
        print("DEBUG", repr([await page.evaluate("location.hash"), (await page.inner_text("#main"))[:300], errors]))
        raise
    assert call["session_id"][:8] in await page.inner_text("#main") if call.get("session_id") else True
    await page.click("nav a[href='#/ai']")
    await page.wait_for_selector(".banner")
    assert "Media bridge not ready" in await page.inner_text(".banner")
    await page.click("nav a[href='#/logs']")
    await page.wait_for_selector("text=Retry & queue status")
    assert not [e for e in errors if "favicon" not in e], errors  # no console errors, no CSP violations


async def test_viewer_sees_masked_numbers_and_no_controls(stack):
    # seed a call so there is something to mask
    await stack.repos.insert_call({"_id": "call_v1", "tenant_id": "t1", "direction": "outbound", "provider": "mock", "from_number": "+911140000000",
                                   "to_number": "+919876543210", "state": "completed", "state_version": 3, "connected_duration_s": 61.0})
    page = await stack.browser.new_page()
    errors = await login(page, stack.url, "view1")
    assert await page.is_hidden("nav a[href='#/controls']") and await page.is_hidden("nav a[href='#/users']")
    await page.click("nav a[href='#/live']")
    await page.wait_for_selector("text=call_v1")
    txt = await page.inner_text("#main")
    assert "9876543210" not in txt and "*" in txt
    await page.goto(stack.url + "/dashboard/#/controls")
    await page.wait_for_selector("text=Operator role required")
    await page.goto(stack.url + "/dashboard/#/users")
    await page.wait_for_selector("text=Administrator role required")
    assert not [e for e in errors if "favicon" not in e and "403" not in e], errors


async def test_login_failure_and_logout(stack):
    page = await stack.browser.new_page()
    await page.goto(stack.url + "/dashboard/")
    await page.fill("#lu", "admin1")
    await page.fill("#lp", "wrong-password-x")
    await page.click("#login-form button")
    await page.wait_for_function("document.querySelector('#login-err').textContent.length > 0")
    assert "invalid" in (await page.inner_text("#login-err")).lower()
    assert await page.evaluate("sessionStorage.getItem('mme_token')") is None
    await page.fill("#lp", PW)
    await page.click("#login-form button")
    await page.wait_for_selector("#app:not(.hidden)")
    await page.click("#logout")
    await page.wait_for_selector("#login:not(.hidden)")
    assert await page.evaluate("sessionStorage.getItem('mme_token')") is None
