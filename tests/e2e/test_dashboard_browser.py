"""Real-browser test of the dashboard (headless Chromium via Playwright) against a live uvicorn server.
Provider is the test MOCK (no calls are placed). Skipped when Playwright/Chromium are not installed
(see docs/TEST_REPORT.md for the install commands)."""
import asyncio
import os
import time

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
    # jwt_leeway_s=60: this WSL2 host's wall clock steps by ~52 s, so a token issued right after a step is "from the future" a moment
    # later and fails with 401 (measured: 4 of 4 in a deterministic probe; ~15 % of e2e runs). The tolerance is scoped to this test
    # stack; the authentication tests elsewhere run with the strict default of 0.
    s = make_settings(mongo_db=db.s.mongo_db, jwt_leeway_s=60)
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


async def wait_until(check, timeout=10.0, what="condition"):
    """Poll an async predicate from Python. The dashboard's CSP forbids 'unsafe-eval', and Playwright's wait_for_function("...")
    evaluates a string inside the page, which that CSP can block (it made tests fail intermittently); polling over the
    protocol does not depend on the page's CSP, and the CSP stays strict."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if await check():
                return
        except Exception:
            pass
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


async def must_see(page, selector, errors=None, timeout=15.0):
    """wait_for_selector that, on failure, says what the page actually showed (route, main text, console errors) so an
    intermittent failure can be told apart: slow environment, app bug, or CSP violation."""
    try:
        await page.wait_for_selector(selector, timeout=timeout * 1000)
    except Exception as e:
        try:
            main = (await page.inner_text("#main", timeout=1000))[:500]
        except Exception:
            main = "<no #main>"
        raise AssertionError(f"never saw {selector!r}: hash={await page.evaluate('location.hash')!r} main={main!r} console_errors={errors} http_401s={getattr(page, '_net401', None)} ({type(e).__name__})") from None


def text_of(page, selector):
    async def get():
        return await page.text_content(selector, timeout=500) or ""
    return get


async def _eq(getter, want):
    return (await getter()) == want


def record_401s(page):
    """Diagnostics only: remember the URL and server error of every 401 so an intermittent 'Session expired' can be explained."""
    page._net401 = []

    async def on_resp(r):
        if r.status == 401:
            try:
                body = (await r.text())[:200]
            except Exception:
                body = "?"
            page._net401.append(f"{r.request.method} {r.url.split('://', 1)[-1].split('/', 1)[-1][:50]} -> {body}")
    page.on("response", lambda r: asyncio.ensure_future(on_resp(r)))


async def login(page, url, user):
    record_401s(page)
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
    await must_see(page, "text=Accepted:", errors)
    call = await stack.db["calls"].find_one({})
    assert call and call["to_number"] == "+919876543210" and call["state"] in ("queued", "initiating")
    # live table receives it via SSE
    await page.click("nav a[href='#/live']")
    await page.wait_for_selector(f"text={call['_id']}")
    assert (await page.inner_text("#main .pill >> nth=0")) in ("live", "polling", "connecting")
    await wait_until(lambda: _eq(text_of(page, "#main .pill"), "live"), what="live badge")
    # detail page, timeline, hangup with confirmation
    await page.click(f"text={call['_id']}")
    await page.wait_for_selector("text=Lifecycle timeline")
    assert "—" in await page.inner_text("#main")  # missing data is a dash, never invented
    assert "No message exchange recorded for this call." in await page.inner_text("#main")  # transcript honesty: says nothing was recorded, invents no words
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
    await must_see(page, "text=call_v1", errors)
    txt = await page.inner_text("#main")
    assert "9876543210" not in txt and "*" in txt
    await page.goto(stack.url + "/dashboard/#/controls")
    await page.wait_for_selector("text=Operator role required")
    await page.goto(stack.url + "/dashboard/#/users")
    await page.wait_for_selector("text=Administrator role required")
    assert not [e for e in errors if "favicon" not in e and "403" not in e], errors


async def seed_ops_call(stack):
    await stack.repos.insert_call({"_id": "call_ops1", "tenant_id": "t1", "direction": "inbound", "provider": "mock", "from_number": "+919876543210",
                                   "to_number": "+911140000000", "state": "answered", "state_version": 2, "ai_enabled": True,
                                   "answered_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)})
    return await stack.repos.get_call("call_ops1")


async def test_live_operations_page_goes_live_and_shows_every_flow(stack):
    call = await seed_ops_call(stack)
    page = await stack.browser.new_page()
    errors = await login(page, stack.url, "admin1")
    await page.click("nav a[href='#/ops']")
    await page.wait_for_selector("text=System flow")
    await wait_until(lambda: _eq(text_of(page, "#main .pill"), "live"), what="live badge")
    # topology: every component has a tile with a real status; the DB is really up, AI is really off
    for key in ("plivo", "fs_events", "media_bridge", "controller", "database", "webhooks", "stt", "brain", "tts", "rag"):
        await page.wait_for_selector(f".tile[data-key='{key}']")
    assert "up" in await page.inner_text(".tile[data-key='database']")
    assert "disabled" in await page.inner_text(".tile[data-key='stt']")
    # KPI gauges and process-wide numbers (admin only)
    body = await page.inner_text("#main")
    assert "Active calls" in body and "AI turn p95" in body and "Active calls (last ~5 min)" in body
    # the call's pipeline: answered is done, the AI conversation is waiting for evidence (no AI session exists), not faked
    await must_see(page, ".flow >> text=call_ops1", errors)
    flow = page.locator(".flow", has_text="call_ops1")
    assert "Answered" in await flow.inner_text() and "AI conversation" in await flow.inner_text()
    assert await flow.locator(".chip.done", has_text="Answered").count() == 1
    assert await flow.locator(".chip.waiting", has_text="AI conversation").count() == 1
    # a new event is pushed to the open page without reload
    await stack.repos.add_event(call, "state.bridged", "esl", "e2e-live-1", {"note": "pushed"})
    await page.wait_for_selector("table >> text=state.bridged", timeout=5000)
    # filtering the feed
    await page.fill("input[placeholder^='Filter feed']", "no-such-text-xyz")
    async def feed_empty():
        return "state.bridged" not in (await page.inner_text("#main"))
    await wait_until(feed_empty, what="feed filtered")
    await page.fill("input[placeholder^='Filter feed']", "bridged")
    await page.wait_for_selector("table >> text=state.bridged")
    # pause holds new events, resume releases them
    await page.click("text=Pause feed")  # the button keeps its label and toggles
    await stack.repos.add_event(call, "state.transferring", "esl", "e2e-live-2", {})
    await page.fill("input[placeholder^='Filter feed']", "")
    await page.wait_for_timeout(1500)    # long enough for the push to have arrived (it is held, not drawn)
    assert "state.transferring" not in await page.inner_text("#main")
    await page.click("text=Pause feed")  # second click resumes and releases the held event
    await page.wait_for_selector("table >> text=state.transferring")
    # drill down: the flow card opens the call, which shows the same flow
    await page.click(".flow >> text=call_ops1")
    await page.wait_for_selector("h2:text-is('Flow')")
    await page.wait_for_selector(".chips .chip")
    assert not [e for e in errors if "favicon" not in e], errors  # no console errors, no CSP violations (no inline styles/scripts)


async def test_live_operations_for_a_viewer_is_masked_and_hides_admin_data(stack):
    await seed_ops_call(stack)
    page = await stack.browser.new_page()
    errors = await login(page, stack.url, "view1")
    await page.click("nav a[href='#/ops']")
    await must_see(page, ".flow >> text=call_ops1", errors)
    txt = await page.inner_text("#main")
    assert "9876543210" not in txt and "*" in txt
    assert "AI turn p95" not in txt and "Recent warnings and errors" not in txt  # process-wide numbers and logs are admin-only
    assert not [e for e in errors if "favicon" not in e], errors


async def test_login_failure_and_logout(stack):
    page = await stack.browser.new_page()
    await page.goto(stack.url + "/dashboard/")
    await page.fill("#lu", "admin1")
    await page.fill("#lp", "wrong-password-x")
    await page.click("#login-form button")
    async def has_error():
        return len((await page.text_content("#login-err", timeout=500)) or "") > 0
    await wait_until(has_error, what="login error text")
    assert "invalid" in (await page.inner_text("#login-err")).lower()
    assert await page.evaluate("sessionStorage.getItem('mme_token')") is None
    await page.fill("#lp", PW)
    await page.click("#login-form button")
    await page.wait_for_selector("#app:not(.hidden)")
    await page.click("#logout")
    await page.wait_for_selector("#login:not(.hidden)")
    assert await page.evaluate("sessionStorage.getItem('mme_token')") is None
