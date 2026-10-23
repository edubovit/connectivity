import argparse
import asyncio
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from playwright.async_api import async_playwright, expect


ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "test-only-password"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def fixture(python, directory, name, public_url=None):
    port, http, tls = free_port(), free_port(), free_port()
    url = f"http://127.0.0.1:{port}/"
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"version": 1, "server": {"host": "127.0.0.1", "port": port, **({"public_url": public_url} if public_url else {})},
                               "auth": {"password": PASSWORD}, "ui": {"refresh_interval_seconds": 1}, "resources": []}), encoding="utf-8")
    options = {}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options = {"startupinfo": startup, "creationflags": subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP}
    with (directory / f"{name}.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([str(python), "tools/devserver.py", "--config", str(path), "--port", str(port),
                                    "--http-port", str(http), "--tls-port", str(tls)], cwd=ROOT, stdout=log, stderr=log, **options)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise AssertionError(f"Fixture exited with {process.returncode}")
                try:
                    probe = Request(url + (urlsplit(public_url).path.lstrip("/") if public_url else ""), headers={"Host": urlsplit(public_url).netloc} if public_url else {})
                    with urlopen(probe, timeout=.2) as response:
                        assert response.status == 200
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise AssertionError("Fixture did not start")
            yield {"url": url, "http": http, "tls": tls}
        finally:
            if process.poll() is None:
                try:
                    if os.name == "nt":
                        api = ctypes.WinDLL("kernel32", use_last_error=True)
                        api.FreeConsole()
                        try:
                            assert api.AttachConsole(process.pid), ctypes.get_last_error()
                            assert api.GenerateConsoleCtrlEvent(signal.CTRL_BREAK_EVENT, process.pid), ctypes.get_last_error()
                            process.wait(timeout=10)
                        finally:
                            api.FreeConsole()
                    else:
                        process.terminate()
                        process.wait(timeout=10)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
            assert process.returncode == 0, f"Fixture shutdown: {process.returncode}"
            for listener in (port, http, tls):
                with socket.socket() as sock:
                    assert sock.connect_ex(("127.0.0.1", listener)) != 0, f"Listener {listener} leaked"


async def api(page, url, path, method="GET", data=None, revision=None):
    headers = {"Origin": url.rstrip("/"), "Content-Type": "application/json"}
    if method != "GET":
        session = await (await page.request.get(url + "api/v1/session")).json()
        headers["X-CSRF-Token"] = session["csrf_token"]
    if revision:
        headers["If-Match"] = revision
    response = await page.request.fetch(url + "api/v1" + path, method=method, headers=headers, data=data)
    assert response.ok, f"{method} {path}: {response.status}"
    return await response.json()


async def sign_in(page, url):
    await page.goto(url)
    await page.locator("#password").fill(PASSWORD)
    await page.locator("#login-form button").click()
    await expect(page.locator("#dashboard")).to_be_visible()
    await expect(page.locator("#live")).to_have_text("Live")


async def layout(page):
    issues = await page.evaluate("""() => {
      const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
      const overflow = document.documentElement.scrollWidth > innerWidth + 1 ||
        [...document.querySelectorAll('dialog[open]')].some(e=>e.scrollWidth>e.clientWidth+1);
      const unnamed = [...document.querySelectorAll('button,input,select')].filter(visible).filter(e =>
        !e.getAttribute('aria-label') && !e.getAttribute('aria-labelledby') &&
        !(e.labels?.length) && !(e.textContent.trim()) && e.type !== 'hidden').map(e=>e.outerHTML);
      const tiny = [...document.querySelectorAll('p,label,button,a,summary,.stat-label')].filter(visible)
        .filter(e => parseFloat(getComputedStyle(e).fontSize) < 13).map(e=>e.className);
      const wrappedMetrics = [...document.querySelectorAll('.speed-metric strong')].filter(visible)
        .filter(e=>e.getBoundingClientRect().height>parseFloat(getComputedStyle(e).lineHeight)+1).map(e=>e.textContent);
      return {overflow,unnamed,tiny,wrappedMetrics};
    }""")
    assert issues == {"overflow": False, "unnamed": [], "tiny": [], "wrappedMetrics": []}, issues
    assert await page.evaluate("getComputedStyle(document.documentElement).colorScheme") == "dark"
    timelines = await page.locator(".timeline:visible").evaluate_all("""elements => elements.map(element => {
      const card = element.closest('article'), box = card.getBoundingClientRect(), style = getComputedStyle(card);
      const bar = element.querySelector('svg').getBoundingClientRect();
      const ticks = [...element.querySelectorAll('.timeline-labels time')];
      const labels = ticks.map(e=>e.getBoundingClientRect());
      const times = ticks.map(e=>Date.parse(e.dateTime));
      return {
        thickness: Math.abs(bar.height-12)<1,
        fullWidth: Math.abs(bar.left-box.left-parseFloat(style.paddingLeft)-parseFloat(style.borderLeftWidth))<1 &&
          Math.abs(box.right-bar.right-parseFloat(style.paddingRight)-parseFloat(style.borderRightWidth))<1,
        marks: ticks.length===3 && times[0]<times[1] && times[1]<times[2] && Math.abs(times[1]-(times[0]+times[2])/2)<=1,
        aligned: labels.length===3 && Math.abs(labels[0].left-bar.left)<1 && Math.abs(labels[2].right-bar.right)<1 &&
          Math.abs((labels[1].left+labels[1].right-bar.left-bar.right)/2)<1,
        accessible: ticks.every(e=>e.title && e.getAttribute('aria-label')===e.title)
      };
    })""")
    assert all(all(check.values()) for check in timelines), timelines


async def select_value(page, select, value):
    text = await select.evaluate("(e,value)=>[...e.options].find(option=>option.value===value).text", value)
    trigger = select.locator("xpath=following-sibling::div[1]").get_by_role("combobox")
    await trigger.click()
    await page.get_by_role("option", name=text, exact=True).click()
    await expect(select).to_have_value(value)
    await expect(trigger).to_contain_text(text)


async def fill_check(page, index, name, kind, fixture):
    check = page.locator(".check-fields").nth(index)
    await check.locator('[name="name"]').fill(name)
    await select_value(page, check.locator('[name="type"]'), kind)
    if kind == "http":
        await check.locator('[name="url"]').fill(f"http://127.0.0.1:{fixture['http']}/ok")
        await select_value(page, check.locator('[name="method"]'), "POST")
        await expect(check.locator(".warning")).to_be_visible()
        await select_value(page, check.locator('[name="method"]'), "GET")
        await expect(check.locator(".warning")).not_to_be_visible()
    else:
        await check.locator('[name="host"]').fill("127.0.0.1")
        if kind != "ping":
            await check.locator('[name="port"]').fill(str(fixture["tls" if kind == "tls" else "http"]))
    await check.locator('[name="interval_seconds"]').fill("1")
    await check.locator('[name="timeout_seconds"]').fill("2")


async def run_browser(browser_type, args, primary, peer, report):
    browser = await browser_type.launch()
    context = await browser.new_context(viewport={"width": 1440, "height": 1000}, timezone_id="Europe/Berlin", has_touch=True)
    page = await context.new_page()
    page.set_default_timeout(10000)
    errors, external = [], []
    page.on("pageerror", lambda error: errors.append(error.stack or str(error)))
    page.on("request", lambda request: external.append(request.url) if not request.url.startswith("http://127.0.0.1:") else None)
    page.on("dialog", lambda dialog: dialog.accept())
    url = primary["url"]
    screenshots = Path(args.report).parent / browser_type.name
    screenshots.mkdir(parents=True, exist_ok=True)
    (screenshots / "failure.png").unlink(missing_ok=True)
    checks = []

    async def passed(name):
        checks.append(name)
        print(f"{browser_type.name}: {name}", flush=True)

    async def snapshot(name):
        await layout(page)
        await page.screenshot(path=str(screenshots / f"{name}.png"), full_page=not await page.locator("dialog[open]").count())

    try:
        await page.goto(url)
        await expect(page.locator("#login")).to_be_visible()
        await page.emulate_media(color_scheme="light", reduced_motion="reduce")
        await snapshot("login")
        await page.set_viewport_size({"width": 320, "height": 844})
        await snapshot("login-mobile")
        await page.set_viewport_size({"width": 1440, "height": 1000})
        await page.locator("#password").fill("wrong-password")
        await page.locator("#login-form button").click()
        await expect(page.locator("#login-error")).to_contain_text("Invalid password")
        await page.locator("#password").fill(PASSWORD)
        await page.locator("#login-form button").click()
        await expect(page.locator("#content")).to_contain_text("No resources yet")
        await page.locator(".skip-link").focus()
        await expect(page.locator(".skip-link")).to_have_css("clip-path", "none")
        await page.locator(".skip-link").press("Enter")
        await expect(page.locator("#dashboard")).to_be_focused()
        await expect(page.locator(".skip-link")).to_have_css("clip-path", "inset(50%)")
        assert await page.evaluate("[percent(null),percent(0),percent(1),percent(.9999999),percent(.0000001)]") == ["N/A", "0%", "100%", "<100%", "<0.01%"]
        await snapshot("empty")
        await passed("login rejection, authentication, empty state")
        assert await page.locator(".login").evaluate("e=>getComputedStyle(e).fontFamily") == "Arial, Helvetica, sans-serif"

        await page.locator("#add-resource").click()
        await expect(page.locator("#resource-name")).to_be_focused()
        await page.locator("#resource-name").fill("Office network")
        dropdown = page.get_by_role("combobox", name="Type", exact=True)
        await dropdown.click()
        await expect(page.locator(".select-menu:popover-open")).to_be_visible()
        await dropdown.press("End")
        await dropdown.press("Enter")
        await expect(page.locator('.check-fields [name="type"]')).to_have_value("tls")
        await dropdown.click()
        await dropdown.press("Escape")
        await expect(page.locator("#editor")).to_be_visible()
        await expect(page.locator(".select-menu:popover-open")).to_have_count(0)
        for index, kind in enumerate(("http", "ping", "tcp", "tls")):
            if index:
                await page.locator("#add-check").click()
            await fill_check(page, index, {"http": "Web service", "ping": "Gateway", "tcp": "Port", "tls": "Certificate"}[kind], kind, primary)
        await snapshot("editor")
        gap = await page.locator("#add-check").evaluate("e=>e.getBoundingClientRect().top-document.querySelector('.check-fields:last-child').getBoundingClientRect().bottom")
        assert abs(gap - 16) < 1, gap
        await page.locator("#save-resource").click()
        await expect(page.locator("#editor")).not_to_be_visible()
        await expect(page.locator(".resource-card .badge")).to_have_text("online")
        before = await api(page, url, "/status")
        first_id = before["resources"][0]["id"]
        generations = {c["id"]: c["generation"] for c in before["resources"][0]["checks"]}
        await passed("create all four check types, real HTTP/TCP/TLS/ICMP results")

        await page.get_by_role("button", name="Edit Office network", exact=True).click()
        await expect(page.locator('.check-editor[open]')).to_have_count(1)
        await expect(page.get_by_role("combobox", name="Type", exact=True).first).to_be_disabled()
        await page.locator('.check-fields [name="name"]').first.fill("")
        await page.locator('.check-editor>summary').first.click()
        await page.locator("#save-resource").click()
        await expect(page.locator('.check-editor').first).to_have_attribute("open", "")
        await expect(page.locator('.check-fields [name="name"]').first).to_be_focused()
        await page.locator("#cancel-editor").click()
        await passed("compact check editor, invalid collapsed fields reopen and receive focus")

        await page.locator("#add-resource").click()
        await page.locator("#resource-name").fill("Unavailable service")
        await fill_check(page, 0, "HTTP failure", "http", primary)
        await page.locator('.check-fields [name="url"]').fill(f"http://127.0.0.1:{primary['http']}/unhealthy")
        await page.locator("#save-resource").click()
        await expect(page.locator(".resource-card")).to_have_count(2)
        await expect(page.locator(".resource-card .badge.offline")).to_have_count(1)
        await snapshot("overview")
        await passed("dark theme regardless of OS preference, full-width timelines and three aligned time marks")
        geometry = "() => [...document.querySelectorAll('.resource-tile')].map(e=>{const r=e.getBoundingClientRect();return [e.dataset.id,r.x+scrollX,r.y+scrollY,r.width,r.height]})"
        await page.mouse.move(0, 0)
        original_geometry = await page.evaluate(geometry)
        cfg = await api(page, url, "/config")
        failed = next(r for r in cfg["resources"] if r["name"] == "Unavailable service")
        failed["checks"][0]["expected_status"] = 503
        await api(page, url, "/config", "PUT", {"resources": cfg["resources"], "ui": cfg["ui"]}, cfg["revision"])
        failed_tile = page.locator(f'[data-id="{failed["id"]}"]')
        await expect(failed_tile).to_have_attribute("data-state", "online")
        assert await page.evaluate(geometry) == original_geometry
        cfg = await api(page, url, "/config")
        next(r for r in cfg["resources"] if r["id"] == failed["id"])["checks"][0]["expected_status"] = 200
        await api(page, url, "/config", "PUT", {"resources": cfg["resources"], "ui": cfg["ui"]}, cfg["revision"])
        await expect(failed_tile).to_have_attribute("data-state", "offline")
        assert await page.evaluate(geometry) == original_geometry
        desktop = await browser.new_context(viewport={"width": 1440, "height": 1000}, storage_state=await context.storage_state())
        desktop_page = await desktop.new_page()
        desktop_page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await desktop_page.goto(url)
            desktop_tile = desktop_page.locator(f'[data-id="{failed["id"]}"]')
            await expect(desktop_tile).to_have_attribute("data-state", "offline")
            before_hover = await desktop_page.evaluate(geometry)
            await desktop_tile.hover(position={"x": 10, "y": 10})
            await expect(desktop_tile).to_have_css("background-color", "rgb(53, 35, 31)")
            await expect(desktop_tile.locator(".resource-name")).to_have_css("text-decoration-line", "none")
            assert await desktop_page.evaluate(geometry) == before_hover
            await desktop_page.screenshot(path=str(screenshots / "failure-hover.png"), full_page=True)
        finally:
            await desktop.close()
        await failed_tile.tap(position={"x": 10, "y": 10})
        await expect(page.locator("#page-title")).to_have_text("Unavailable service")
        await expect(page.locator(".timeline svg[role=slider]").first).to_be_visible()
        await page.locator("#back-resources").click()
        await expect(page.locator("#content.resource-grid")).to_be_visible()
        await page.get_by_role("link", name="Office network", exact=True).focus()
        await page.get_by_role("link", name="Office network", exact=True).press("Enter")
        await expect(page.locator("#page-title")).to_have_text("Office network")
        await page.locator("#back-resources").click()
        await passed("stable failure geometry and order, whole-tile touch/keyboard navigation, failure hover and detail inspection")
        await page.locator("#name-filter").fill("office")
        await expect(page.locator(".resource-card")).to_have_count(1)
        await page.locator("#name-filter").fill("not here")
        await expect(page.locator("#content")).to_contain_text("No matching resources")
        await page.locator("#name-filter").fill("")
        await select_value(page, page.locator("#state-filter"), "offline")
        await expect(page.locator(".resource-card")).to_have_count(1)
        await select_value(page, page.locator("#state-filter"), "")
        await passed("failure state, search, state filtering, no-match state")

        await page.locator("#reorder-resources").click()
        handle = page.get_by_role("button", name=re.compile(r"^Move resource Office network,"))
        await expect(page.get_by_role("combobox", name="Current state", exact=True)).to_be_disabled()
        await handle.focus()
        await handle.press("Space")
        await handle.press("ArrowDown")
        await handle.press("Escape")
        await expect(page.locator(".resource-name").first).to_have_text("Office network")
        await handle.press("ArrowDown")
        await page.locator("#save-resource-order").click()
        await expect(page.locator("#resource-order-panel")).not_to_be_visible()
        await expect(page.locator(".resource-name").first).to_have_text("Unavailable service")
        assert await page.locator(".resource-index").all_text_contents() == ["01", "02"]
        await page.locator("#reorder-resources").click()
        handle = page.locator(".resource-card .drag-handle").first
        box = await handle.bounding_box()
        target = await page.locator(".resource-card").last.bounding_box()
        await page.mouse.move(box["x"] + 22, box["y"] + 22)
        await page.mouse.down()
        await page.mouse.move(target["x"] + target["width"] - 10, target["y"] + target["height"] - 5, steps=12)
        await page.mouse.up()
        await expect(page.locator(".resource-name").first).to_have_text("Office network")
        await page.locator("#save-resource-order").click()
        await expect(page.locator("#resource-order-panel")).not_to_be_visible()
        await passed("keyboard and pointer reorder, Escape rollback, persistence")

        await page.get_by_role("button", name="Edit Office network", exact=True).click()
        await page.locator("#resource-name").fill("Office <network>")
        await page.locator("#reorder-checks").click()
        await page.locator(".check-fields .drag-handle").first.press("End")
        await page.locator("#done-check-order").click()
        await page.locator("#save-resource").click()
        await expect(page.locator("#editor")).not_to_be_visible()
        after = await api(page, url, "/status")
        resource = next(r for r in after["resources"] if r["id"] == first_id)
        assert {c["id"]: c["generation"] for c in resource["checks"]} == generations
        assert resource["checks"][-1]["name"] == "Web service"
        await page.get_by_role("link", name="Office <network>", exact=True).click()
        await expect(page.locator(".check-card")).to_have_count(4)
        await expect(page.locator("#page-title")).to_have_text("Office <network>")
        await snapshot("details")
        card = page.locator(".check-card").first
        await card.locator(".diagnostics summary").click()
        await card.locator(".execution-details summary").click()
        await card.get_by_role("button", name="Load recent executions").click()
        await expect(card.locator("tbody tr").first).to_be_visible()
        await expect(card.locator(".diagnostics")).to_have_attribute("open", "")
        plot = card.locator(".latency-plot")
        await plot.focus()
        await expect(card.locator(".latency-tooltip-mean")).to_be_visible()
        assert (await card.locator(".latency-tooltip").bounding_box())["height"] < 180
        await snapshot("tooltip-sample")
        await plot.press("Home")
        await expect(card.locator(".latency-tooltip-samples")).to_have_text("No observations")
        await expect(card.locator(".latency-tooltip-stats")).not_to_be_visible()
        await expect(card.locator(".latency-tooltip-counts")).not_to_be_visible()
        assert (await card.locator(".latency-tooltip").bounding_box())["height"] < 110
        await snapshot("tooltip-empty")
        await plot.press("End")
        await expect(card.locator(".latency-tooltip")).to_be_visible()
        await expect(plot).to_be_focused()
        await page.wait_for_timeout(1300)
        await expect(plot).to_be_focused()
        await expect(card.locator(".diagnostics")).to_have_attribute("open", "")
        await plot.press("Escape")
        await expect(card.locator(".latency-tooltip")).not_to_be_visible()
        timeline = card.locator(".timeline svg")
        await timeline.focus()
        await timeline.press("End")
        await expect(card.locator(".timeline-inspection")).to_be_visible()
        await timeline.press("Escape")
        await expect(card.locator(".timeline-inspection")).not_to_be_visible()
        await passed("rename escaping, check reorder preserves generations, raw results, chart keyboard/focus and disclosure persistence")

        async def short_pages(route):
            await route.continue_(url=route.request.url.replace("limit=100", "limit=2"))

        await page.route("**/checks/*/results?*", short_pages)
        await card.locator(".execution-details button").click()
        await expect(card.locator(".execution-details button")).to_have_text("Load more")
        count = await card.locator("tbody tr").count()
        await card.locator(".execution-details button").click()
        await expect(card.locator("tbody tr")).to_have_count(count + 2)
        await page.unroute("**/checks/*/results?*", short_pages)
        await passed("raw-result cursor pagination")

        for seconds in (86400, 1209600, 3600):
            await page.locator(f'[data-range="{seconds}"]').click()
            await expect(page.locator(f'[data-range="{seconds}"]')).to_have_attribute("aria-pressed", "true")
            await snapshot(f"details-{seconds}")
        await page.locator('[data-range="custom"]').click()
        await page.locator('#custom-range button').click()
        await expect(page.locator('[data-range="custom"]')).to_have_attribute("aria-pressed", "true")
        await page.reload()
        await expect(page.locator('[data-range="custom"]')).to_have_attribute("aria-pressed", "true")
        await page.locator('[data-range="custom"]').click()
        await page.locator("#range-start").evaluate("input => {input.value='2026-03-29T02:30:00';input.dispatchEvent(new Event('input',{bubbles:true}));}")
        await page.locator("#offset-start").fill("+01:00")
        await page.locator('#custom-range button').click()
        await expect(page.locator("#range-error")).to_contain_text("do not exist")
        await page.locator('[data-range="3600"]').click()
        await passed("hour/day/fortnight/custom ranges, reload persistence, DST gap validation")

        await page.locator("#back-resources").click()
        await page.get_by_role("button", name="Edit Office <network>", exact=True).click()
        current = await api(page, url, "/config")
        await api(page, url, "/config", "PUT", {"resources": current["resources"], "ui": current["ui"]}, current["revision"])
        await page.locator("#resource-name").fill("Unsaved rename")
        await page.locator("#save-resource").click()
        await expect(page.locator("#editor-error")).to_contain_text("another tab")
        await expect(page.locator("#reload-config")).to_be_visible()
        await page.locator("#reload-config").click()
        await expect(page.locator("#resource-name")).to_have_value("Office <network>")

        async def fail_save(route):
            if route.request.method == "PUT":
                await route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": {"code": "persistence", "message": "Fixture disk failure"}}))
            else:
                await route.continue_()

        await page.route("**/api/v1/config", fail_save)
        await page.locator("#resource-name").fill("Not saved")
        await page.locator("#save-resource").click()
        await expect(page.locator("#editor-error")).to_contain_text("Fixture disk failure")
        assert (await api(page, url, "/config"))["resources"][0]["name"] == "Office <network>"
        await page.unroute("**/api/v1/config", fail_save)
        await page.locator("#cancel-editor").click()
        await passed("stale revision, explicit reload, failed-save draft and server preservation")

        await page.get_by_role("button", name="Edit Office <network>", exact=True).click()
        changed_id = await page.locator(".check-fields").first.get_attribute("data-id")
        await page.locator('.check-fields [name="interval_seconds"]').first.fill("2")
        await page.locator("#save-resource").click()
        await expect(page.locator("#editor")).not_to_be_visible()
        resource = next(r for r in (await api(page, url, "/status"))["resources"] if r["id"] == first_id)
        assert all((c["generation"] != generations[c["id"]]) == (c["id"] == changed_id) for c in resource["checks"])
        await passed("measurement-change confirmation and scoped history reset")

        await page.locator("#settings").click()
        await page.locator("#refresh-seconds").fill("2")
        await page.get_by_role("button", name="Save settings", exact=True).click()
        await expect(page.locator("#settings-dialog")).not_to_be_visible()
        assert (await api(page, url, "/config"))["ui"]["refresh_interval_seconds"] == 2
        await context.set_offline(True)
        await expect(page.locator("#banner")).to_be_visible(timeout=15000)
        await context.set_offline(False)
        await expect(page.locator("#banner")).not_to_be_visible(timeout=15000)
        await passed("settings persistence, offline state and automatic recovery")

        for width in (390, 320, 768):
            await page.set_viewport_size({"width": width, "height": 844})
            await snapshot(f"overview-{width}")
        await page.set_viewport_size({"width": 320, "height": 844})
        await page.locator("#reorder-resources").click()
        await snapshot("mobile-reorder")
        await page.locator("#cancel-resource-order").click()
        await page.locator('[data-range="1209600"]').click()
        await expect(page.locator('[data-range="1209600"]')).to_have_attribute("aria-pressed", "true")
        await snapshot("overview-320-fortnight")
        await page.locator('[data-range="3600"]').click()
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.get_by_role("link", name="Office <network>", exact=True).tap()
        await expect(page.locator(".check-card")).to_have_count(4)
        await page.locator(".check-card .latency-plot").first.tap()
        await expect(page.locator(".latency-tooltip").first).to_be_visible()
        await snapshot("mobile-details")
        await page.set_viewport_size({"width": 320, "height": 844})
        await snapshot("details-320")
        await page.get_by_role("button", name="Edit Office <network>", exact=True).tap()
        await expect(page.locator("#editor")).to_be_visible()
        await snapshot("mobile-editor")
        await page.locator("#close-editor").tap()
        await page.locator("#settings").tap()
        await expect(page.locator("#settings")).to_have_attribute("aria-expanded", "true")
        await snapshot("mobile-settings")
        await page.locator("#close-settings").tap()
        await expect(page.locator("#settings")).to_have_attribute("aria-expanded", "false")
        await page.locator("#nav-resources").tap()
        await expect(page.locator("#page-title")).to_have_text("Resources")
        await page.locator("#add-resource").tap()
        mobile_type = page.get_by_role("combobox", name="Type", exact=True)
        await mobile_type.tap()
        await expect(page.locator(".select-menu:popover-open")).to_be_visible()
        assert await page.locator(".select-menu:popover-open").evaluate("e=>{const r=e.getBoundingClientRect();return r.left>=0&&r.top>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1&&e.contains(document.elementFromPoint(r.left+15,r.top+15));}")
        await snapshot("mobile-dropdown")
        await mobile_type.press("p")
        await mobile_type.press("Enter")
        await expect(page.locator('.check-fields [name="type"]')).to_have_value("ping")
        await mobile_type.tap()
        await mobile_type.press("Tab")
        await expect(page.locator(".select-menu:popover-open")).to_have_count(0)
        await expect(page.locator('.check-fields [name="host"]')).to_be_focused()
        await page.locator("#cancel-editor").tap()
        await passed("320/390/768px layouts, mobile dialogs, touch chart inspection")

        await page.locator("#nav-speedtest").tap()
        await expect(page.locator("#speedtest")).to_be_visible()
        await snapshot("mobile-speedtest")
        await page.set_viewport_size({"width": 1440, "height": 1000})
        await page.locator("#speed-configure").click()
        await page.locator("#peer-add").click()
        await page.locator('.peer-fields [name="name"]').fill("Lab peer")
        await page.locator('.peer-fields [name="url"]').fill(peer["url"])
        await page.locator('.peer-fields [name="password"]').fill(PASSWORD)
        await page.get_by_role("button", name="Save peers", exact=True).click()
        await expect(page.locator("#peers-dialog")).not_to_be_visible()
        await page.locator("#speed-configure").click()
        await expect(page.locator('.peer-fields [name="password"]')).to_have_value("")
        await page.locator('.peer-fields [name="name"]').fill("Lab peer renamed")
        await page.get_by_role("button", name="Save peers", exact=True).click()
        await expect(page.locator("#peers-dialog")).not_to_be_visible()
        peers = await api(page, url, "/speedtest/peers")
        assert peers["peers"][0]["has_password"] and "password" not in peers["peers"][0]
        await passed("peer CRUD, retained password and read redaction")
        await page.locator('[data-speed-mode="node"]').click()
        peer_control = page.get_by_role("combobox", name="Peer", exact=True)
        await expect(peer_control).to_contain_text("Lab peer renamed")
        await peer_control.click()
        await expect(page.get_by_role("option", name="Lab peer renamed", exact=True)).to_be_visible()
        await peer_control.press("Escape")
        await expect(peer_control).to_be_focused()
        await page.locator('[data-speed-mode="browser"]').click()
        await passed("styled selects: keyboard choice, Escape within dialogs, immutable types, disabled filters and dynamic peer options")

        await page.locator("#speed-start").click()
        await expect(page.locator("#speed-cancel")).to_be_visible()
        await page.locator("#speed-cancel").click()
        await expect(page.locator("#speed-result-heading")).to_contain_text("cancelled", timeout=10000)
        if not args.skip_transfers:
            monitor_before = await api(page, url, "/status")
            await expect(page.locator("#speed-start")).to_be_enabled()
            await page.locator("#speed-start").click()
            await expect(page.locator("#speed-result-heading")).to_contain_text("Latest attempt: completed", timeout=65000)
            await page.locator("#speed-details>summary").click()
            await expect(page.locator(".speed-chart circle")).to_have_count(20)
            await page.locator(".speed-chart circle").first.focus()
            await page.locator(".speed-chart circle").first.press("ArrowRight")
            await expect(page.locator(".speed-chart circle").nth(1)).to_be_focused()
            await snapshot("speedtest-completed")
            await page.set_viewport_size({"width": 320, "height": 844})
            await snapshot("mobile-speedtest-completed")
            await page.set_viewport_size({"width": 1440, "height": 1000})
            await page.locator('[data-speed-mode="node"]').click()
            await expect(page.locator("#speed-start")).to_be_enabled()
            await page.locator("#speed-start").click()
            await expect(page.locator("#speed-cancel")).to_be_visible()
            await page.reload()
            await expect(page.locator("#speedtest")).to_be_visible()
            await page.locator('[data-speed-mode="node"]').click()
            await expect(page.locator("#speed-result-heading")).to_contain_text("Latest attempt: completed", timeout=65000)
            monitor_after = await api(page, url, "/status")
            assert [[c["generation"] for c in r["checks"]] for r in monitor_before["resources"]] == [[c["generation"] for c in r["checks"]] for r in monitor_after["resources"]]
            await passed("full browser and node transfers, receiver charts, node survives reload, monitoring generations preserved")
            await page.locator('[data-speed-mode="browser"]').click()
            await expect(page.locator("#speed-start")).to_be_enabled()
            await page.evaluate("""() => {
              window.uiCancelPhase = null;
              const heading = document.getElementById('speed-result-heading');
              const observer = new MutationObserver(() => {
                if (heading.textContent.startsWith('Measuring upload')) {
                  observer.disconnect();
                  window.uiCancelPhase = heading.textContent;
                  document.getElementById('speed-cancel').click();
                }
              });
              observer.observe(heading, {childList:true});
            }""")
            await page.locator("#speed-start").click()
            await expect(page.locator("#speed-result-heading")).to_contain_text("cancelled", timeout=45000)
            assert (await page.evaluate("window.uiCancelPhase")).startswith("Measuring upload")
            await expect(page.locator(".speed-metric.rate strong").first).not_to_have_text("N/A")
            await expect(page.locator(".speed-metric.rate strong").nth(1)).to_have_text("N/A")
            await passed("mid-transfer cancellation preserves download and leaves incomplete upload unavailable")
        await page.locator('[data-speed-mode="browser"]').click()
        await page.reload()
        await expect(page.locator("#speed-result-heading")).to_have_text("No result yet")
        await passed("speedtest cancellation and browser result expiry on reload")

        await page.locator("#speed-configure").click()
        await page.get_by_role("button", name="Remove peer", exact=True).click()
        await page.get_by_role("button", name="Save peers", exact=True).click()
        await expect(page.locator("#speed-peer-results")).to_contain_text("No peers configured")
        await page.locator("#nav-resources").click()
        await expect(page.locator("#back-resources")).not_to_be_visible()
        await page.get_by_role("button", name="Edit Unavailable service", exact=True).click()
        await page.locator("#delete-resource").click()
        await expect(page.locator(".resource-card")).to_have_count(1)
        await page.get_by_role("button", name="Edit Office <network>", exact=True).click()
        await page.locator(".check-fields .remove-check").first.click()
        await page.locator("#save-resource").click()
        await expect(page.locator("#editor")).not_to_be_visible()
        remaining = (await api(page, url, "/config"))["resources"][0]["checks"]
        assert len(remaining) == 3
        await passed("peer, resource and check deletion")

        await api(page, url, "/session", "DELETE", {})
        await expect(page.locator("#login")).to_be_visible(timeout=10000)
        await sign_in(page, url)
        await page.locator("#logout").click()
        await expect(page.locator("#login")).to_be_visible()
        await expect(page.locator("#dashboard")).not_to_be_visible()
        await expect(page.locator("#speedtest")).not_to_be_visible()
        await passed("revoked session recovery and logout clears private views")
        assert not errors, errors
        assert not external, external
        report.append({"browser": browser_type.name, "version": browser.version, "checks": checks,
                       "full_transfers": not args.skip_transfers, "page_errors": errors, "external_requests": external,
                       "asset_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((ROOT / "connectivity/static").iterdir())}})
    except BaseException:
        await page.screenshot(path=str(screenshots / "failure.png"), full_page=True)
        raise
    finally:
        await context.close()
        await browser.close()


async def prefix_browser(browser_type, args, backend, report):
    browser = await browser_type.launch()
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    errors, paths = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    public = "https://monitor.example:8443/monitor/"

    async def proxy(route):
        request = route.request
        parsed = urlsplit(request.url)
        assert parsed.netloc == "monitor.example:8443", request.url
        assert parsed.path.startswith("/monitor"), request.url
        paths.append(parsed.path)
        headers = {**await request.all_headers(), "host": parsed.netloc, "x-real-ip": "127.0.0.1"}
        cookies = await context.cookies(request.url)
        if cookies:
            headers["cookie"] = "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies)
        response = await context.request.fetch(backend["url"].rstrip("/") + parsed.path + ("?" + parsed.query if parsed.query else ""),
                                               method=request.method, headers=headers, data=request.post_data_buffer, max_redirects=0)
        if "set-cookie" in response.headers:
            assert all(flag in response.headers["set-cookie"] for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/monitor/"))
        await route.fulfill(response=response)

    await context.route("**/*", proxy)
    try:
        redirect = await context.request.get(backend["url"] + "monitor", headers={"Host": urlsplit(public).netloc}, max_redirects=0)
        assert redirect.status == 308 and redirect.headers["location"] == "/monitor/"
        await page.goto(public)
        await expect(page).to_have_url(public)
        await expect(page.locator(".security-note")).to_contain_text("Encrypted connection")
        await page.locator("#password").fill(PASSWORD)
        await page.locator("#login-form button").click()
        await expect(page.locator("#content")).to_contain_text("No resources yet")
        cookies = await context.cookies(public)
        session = next(c for c in cookies if c["name"] == "connectivity_session")
        assert session["secure"] and session["httpOnly"] and session["path"] == "/monitor/"
        await page.locator("#add-resource").click()
        await page.locator("#resource-name").fill("Prefixed resource")
        await fill_check(page, 0, "Loopback", "ping", backend)
        await page.locator("#save-resource").click()
        await expect(page.locator(".resource-card .badge")).to_have_text("online")
        await page.get_by_role("link", name="Prefixed resource", exact=True).click()
        await expect(page.locator(".check-card")).to_have_count(1)
        await page.locator("#nav-speedtest").click()
        await expect(page.locator("#speed-activity")).to_have_text("Ready")
        await page.locator("#settings").click()
        await page.locator("#settings-dialog summary").click()
        await expect(page.locator("#settings-dialog .warning")).to_contain_text("HTTPS encrypts")
        await page.locator("#close-settings").click()
        await page.locator("#logout").click()
        await expect(page.locator("#login")).to_be_visible()
        assert not errors, errors
        assert "/monitor/static/app.js" in paths and "/monitor/api/v1/speedtest/state" in paths
        report[-1]["prefix_proxy_ui"] = "passed: intercepted HTTPS origin forwarded to real loopback backend; not a TLS/proxy deployment test"
        print(f"{browser_type.name}: HTTPS prefix UI, non-default authority, secure scoped cookie, CRUD, navigation and logout", flush=True)
    finally:
        await context.close()
        await browser.close()


async def main(args):
    output = Path(args.report).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    results = []
    output.write_text("[]\n", encoding="utf-8")
    async with async_playwright() as playwright:
        for name in args.browsers:
            with tempfile.TemporaryDirectory(prefix="ui-fixture-", dir=output.parent) as temp:
                with fixture(args.python.resolve(), Path(temp), "primary") as primary, fixture(args.python.resolve(), Path(temp), "peer") as peer:
                    await run_browser(getattr(playwright, name), args, primary, peer, results)
                with fixture(args.python.resolve(), Path(temp), "prefix", "https://monitor.example:8443/monitor/") as backend:
                    await prefix_browser(getattr(playwright, name), args, backend, results)
            output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Passed {sum(len(result['checks']) for result in results)} browser scenarios. Report: {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Isolated UI regression checks. Full transfers use loopback only and can move gigabytes.")
    parser.add_argument("--python", type=Path, default=ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"))
    parser.add_argument("--browsers", nargs="+", choices=["chromium", "firefox", "webkit"], default=["chromium", "firefox", "webkit"])
    parser.add_argument("--report", default=".local/ui-browser/results.json")
    parser.add_argument("--skip-transfers", action="store_true")
    asyncio.run(main(parser.parse_args()))
