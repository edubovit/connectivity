import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.ui_browser_check import fixture, sign_in


INIT = """(() => {
  const probe = window.pollProbe = {id:Math.random().toString(36), hold:null, pending:false, reads:0};
  const log = (kind, data={}) => console.log('POLL-PROBE ' + JSON.stringify({id:probe.id,kind,...data}));
  const fetch = window.fetch;
  window.fetch = function(input, options) {
    if (String(input).includes('/speedtest/')) log('fetch', {path:String(input)});
    return fetch.call(this, input, options);
  };
  const json = Response.prototype.json;
  Response.prototype.json = async function() {
    const data = await json.call(this);
    if (this.url.endsWith('/api/v1/speedtest/state')) probe.reads++;
    if (probe.hold && this.url.endsWith('/api/v1' + probe.hold)) {
      probe.hold = null;
      probe.pending = true;
      log('paused');
      await new Promise(resolve => probe.release = resolve);
      probe.pending = false;
      log('released');
    }
    return data;
  };
  for (const type of ['beforeunload','pagehide','pageshow']) addEventListener(type, event => {
    log(type, {persisted:event.persisted});
    if (type === 'beforeunload' && probe.pending) setTimeout(() => probe.release(), 10);
  });
  addEventListener('unhandledrejection', event => log('unhandledrejection', {message:String(event.reason)}));
})();"""


async def check(browser_type, url, report):
    browser = await browser_type.launch()
    context = await browser.new_context()
    await context.add_init_script(INIT)
    page = await context.new_page()
    page.set_default_timeout(10000)
    events, errors, checks = [], [], []
    page.on("console", lambda message: events.append(json.loads(message.text[11:])) if message.text.startswith("POLL-PROBE ") else None)
    page.on("pageerror", lambda error: errors.append(error.stack or str(error)))
    peer = {"id": "fixture", "name": "Lifecycle fixture", "url": url, "has_password": True}
    run = {"id": "fixture-run", "mode": "node", "peer": peer, "outcome": "cancelled", "phase": "finished",
           "completed_at": "2026-01-01T00:00:00Z", "metrics": {}}

    async def speed_reads(route):
        path = route.request.url.split("/api/v1/speedtest/", 1)[1]
        responses = {"state": {"config_revision": "fixture", "active": None, "latest": [run]},
                     "peers": {"revision": "fixture", "peers": [peer]}, "runs/fixture-run": run}
        assert route.request.method == "GET", route.request.method
        await route.fulfill(json=responses[path])

    async def pause(path):
        await page.evaluate("path => {pollProbe.hold=path;}", path)
        await page.locator('[data-speed-mode="node"]').click()
        await page.wait_for_function("() => pollProbe.pending")
        return await page.evaluate("pollProbe.id")

    async def resumed(previous):
        await page.wait_for_function("previous => pollProbe.reads > previous && !pollProbe.pending", arg=previous)
        await expect(page.locator("#speed-error")).to_have_text("")

    result = {"browser": browser_type.name, "version": browser.version, "checks": checks,
              "page_errors": errors, "events": events, "passed": False}
    report.append(result)
    try:
        await page.route("**/api/v1/speedtest/**", speed_reads)
        await sign_in(page, url)
        await page.locator("#nav-speedtest").click()
        await page.locator('[data-speed-mode="node"]').click()
        await expect(page.locator("#speed-result-heading")).to_contain_text("Latest attempt: cancelled")

        for path in ("/speedtest/state", "/speedtest/peers", "/speedtest/runs/fixture-run"):
            old_document = await pause(path)

            async def delayed_document(route):
                await asyncio.sleep(1.5)
                await route.continue_()

            await page.route(url, delayed_document)
            await page.reload()
            await page.unroute(url, delayed_document)
            await expect(page.locator("#speedtest")).to_be_visible()
            await resumed(0)
            outgoing = [event for event in events if event["id"] == old_document]
            departing = next(index for index, event in enumerate(outgoing) if event["kind"] == "beforeunload")
            assert any(event["kind"] == "released" for event in outgoing[departing:]), outgoing
            if browser_type.name == "webkit":
                assert not any(event["kind"] == "fetch" for event in outgoing[departing:]), outgoing
            checks.append(f"reload during paused {path}; outgoing document delayed 1.5 seconds")

        await pause("/speedtest/state")
        before = await page.evaluate("pollProbe.reads")
        await page.evaluate("addEventListener('beforeunload', event => {event.preventDefault();event.returnValue='';}, {once:true})")
        page.once("dialog", lambda dialog: dialog.dismiss())
        async with page.expect_event("dialog") as dialog_info:
            await page.evaluate("url => {location.assign(url);}", url + "cancelled-navigation")
        dialog = await dialog_info.value
        assert dialog.type == "beforeunload"
        await resumed(before)
        assert page.url == url + "#speedtest"
        checks.append("dismissed beforeunload confirmation resumes polling in the same document")

        for outcome in ("aborted", "no-content"):
            await pause("/speedtest/peers")
            before = await page.evaluate("pollProbe.reads")
            navigation_done = asyncio.Event()

            async def failed_navigation(route):
                await asyncio.sleep(.2)
                if outcome == "aborted":
                    await route.abort("aborted")
                else:
                    await route.fulfill(status=204)
                navigation_done.set()

            target = url + outcome
            await page.route(target, failed_navigation)
            await page.evaluate("url => {location.assign(url);}", target)
            await asyncio.wait_for(navigation_done.wait(), 10)
            await resumed(before)
            assert page.url == url + "#speedtest"
            await page.unroute(target, failed_navigation)
            checks.append(f"{outcome} navigation resumes polling without pageshow or focus")

        await pause("/speedtest/state")
        before = await page.evaluate("pollProbe.reads")
        await page.evaluate("""() => {
          dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}));
          pollProbe.release();
        }""")
        await page.wait_for_function("() => !pollProbe.pending")
        await page.evaluate("dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
        await resumed(before)
        checks.append("synthetic persisted pagehide/pageshow restores polling (not a BFCache eligibility test)")
        assert not errors, errors
        assert not any(event["kind"] == "unhandledrejection" for event in events), events
        result["passed"] = True
        print(f"{browser_type.name} {browser.version}: {len(checks)} lifecycle checks passed", flush=True)
    finally:
        await context.close()
        await browser.close()


async def main(args):
    report = {"speed_ui_sha256": hashlib.sha256((ROOT / "connectivity/static/speed-ui.js").read_bytes()).hexdigest(),
              "scope": "Real local login and browser lifecycle; deterministic mocked speedtest GET responses; no transfers.",
              "results": []}
    output = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="lifecycle-", dir=output.parent) as directory:
            with fixture(args.python, Path(directory), "primary") as primary:
                async with async_playwright() as playwright:
                    for name in args.browsers:
                        await check(getattr(playwright, name), primary["url"], report["results"])
    finally:
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Isolated speedtest poll/navigation regression; no bandwidth transfers.")
    parser.add_argument("--python", type=Path, default=ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"))
    parser.add_argument("--browsers", nargs="+", choices=("chromium", "firefox", "webkit"), default=["webkit"])
    parser.add_argument("--report", default=str(ROOT / ".local/speed-ui-lifecycle/report.json"))
    asyncio.run(main(parser.parse_args()))
