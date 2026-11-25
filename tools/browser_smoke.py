"""Optional end-to-end check: pip install playwright; run against a local server.

Uses an isolated browser profile and a disposable test account. Never sends code
to the API for execution; all submissions run through the sandboxed iframe.
"""
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            **({"executable_path": os.environ["PAIR_CHROME"]} if os.environ.get("PAIR_CHROME") else {}),
            headless=True,
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        base_url = os.environ.get("PAIR_TEST_URL", "http://127.0.0.1:8765")
        origin = urlparse(base_url).netloc
        external = []
        runtime_requests = []
        runner_requests = []
        page.on("request", lambda request: runner_requests.append(request.url) if '/assets/runner.html' in request.url else None)
        def app_only(route):
            if urlparse(route.request.url).netloc != origin:
                external.append(route.request.url)
                route.abort()
            elif urlparse(route.request.url).path == '/assets/runner.html' and 'rev=bundled-v2' not in route.request.url:
                # Emulate the stale runner observed in a real Chrome profile.
                # The current workspace must never request this cached URL.
                route.fulfill(body="<script>parent.postMessage('pair-runner-ready','*')</script>", content_type='text/html')
            else:
                route.continue_()
        page.context.route("**/*", app_only)
        page.on("request", lambda request: runtime_requests.append(request.url) if '/runtime/314.0.8/' in request.url and not request.url.endswith('manifest.json') else None)
        page.on("pageerror", lambda error: print("PAGE ERROR:", str(error)[:1000]))
        page.on("console", lambda message: print("CONSOLE:", message.text[:1000]) if message.type == "error" else None)
        page.on("requestfailed", lambda request: print("REQUEST FAILED:", request.url, request.failure))
        page.goto(base_url)
        page.locator("#register-tab").click()
        username = "smoke_" + secrets.token_hex(4)
        page.locator("#account-name").fill(username)
        page.locator("#password").fill("test-password-12345")
        page.locator("#account-submit").click()
        page.locator("#setup").wait_for(state="visible")
        expect(page.locator('#problem-list .topic-node')).to_have_count(18)
        page.emulate_media(reduced_motion='reduce')
        page.screenshot(path='/private/tmp/pair-topic-map.png', full_page=True)
        page.set_viewport_size({'width':390, 'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path='/private/tmp/pair-topic-map-mobile.png', full_page=True)
        page.set_viewport_size({'width':1440, 'height':1000})
        page.locator('.topic-node[data-category="Arrays & Hashing"]').click()
        expect(page.locator('#problem-list .graph-node')).to_have_count(9)
        page.locator('.graph-node[data-problem="contains-duplicate"]').focus()
        page.keyboard.press('Space')
        expect(page.locator('.graph-node[data-problem="contains-duplicate"]')).to_have_attribute('aria-pressed', 'true')
        page.keyboard.press('Space')
        expect(page.locator('.graph-node[data-problem="contains-duplicate"]')).to_have_attribute('aria-pressed', 'false')
        for node in page.locator('#problem-list .graph-node').all()[:5]:
            node.click()
        expect(page.locator('#problem-count')).to_have_value('5')
        page.locator('#problem-list .graph-node').nth(5).click()
        expect(page.locator('#problem-list .graph-node.selected')).to_have_count(5)
        page.locator('#randomize').click()
        expect(page.locator('#problem-list .graph-node.selected')).to_have_count(0)
        page.locator('#search').fill('duplicate')
        expect(page.locator('#problem-list .graph-node')).to_have_count(1)
        page.locator('#search').fill('')
        page.locator('.graph-node[data-problem="contains-duplicate"]').click()
        expect(page.locator('.graph-node[data-problem="contains-duplicate"]')).to_have_attribute('aria-pressed', 'true')
        # Topic navigation and difficulty filters must not discard the basket.
        page.locator('#graph-back').click()
        expect(page.locator('.topic-node[data-category="Arrays & Hashing"]')).to_contain_text('1 selected')
        page.locator('.topic-node[data-category="Stack"]').click()
        page.locator('.graph-node[data-problem="valid-parentheses"]').click()
        expect(page.locator('#selected-problems .selected-problem')).to_have_count(2)
        page.locator('#graph-back').click()
        page.locator('#difficulty').select_option('easy')
        page.locator('.topic-node[data-category="Arrays & Hashing"]').click()
        expect(page.locator('.graph-node[data-problem="contains-duplicate"]')).to_have_attribute('aria-pressed', 'true')
        expect(page.locator('#problem-count')).to_have_value('2')
        page.locator('#start').click(); expect(page.locator('#workspace')).to_be_visible()
        expect(page.locator('#problem-tabs button')).to_have_count(2)
        assert set(page.evaluate('session.problems.map(p => p.id)')) == {'contains-duplicate', 'valid-parentheses'}
        page.locator('#finish').click(); expect(page.locator('#summary')).to_be_visible()
        page.locator('#again').click(); expect(page.locator('#setup')).to_be_visible()
        page.locator('#selected-problems').get_by_role('button', name='Remove Valid Parentheses', exact=True).click()
        expect(page.locator('#problem-count')).to_have_value('1')
        page.screenshot(path='/private/tmp/pair-problem-map.png', full_page=True)
        page.locator('#theme').click()
        page.screenshot(path='/private/tmp/pair-problem-map-light.png', full_page=True)
        page.locator('#theme').click()
        page.locator("#start").click()
        page.locator("#workspace").wait_for(state="visible")
        code = "class Solution:\n    def containsDuplicate(self, nums):\n        return len(nums) != len(set(nums))\n"
        page.locator("#editor").fill(code)
        if os.environ.get("PAIR_TEST_RUNTIME_FAILURES"):
            # A missing/truncated deployment asset must release Run/Submit and
            # allow a retry. Stop must also work during the download phase.
            asset_url = "**/runtime/314.0.8/pyodide.asm.wasm"
            def fail_asset(route):
                route.fulfill(status=503, body="Unavailable")
            page.context.route(asset_url, fail_asset)
            page.locator("#run").click()
            expect(page.locator("#run")).to_be_enabled(timeout=10000)
            assert "Could not download Python" in page.locator("#execution-state").inner_text()
            page.context.unroute(asset_url, fail_asset)
            def hold_asset(route):
                pass
            page.context.route(asset_url, hold_asset)
            page.locator("#run").click()
            expect(page.locator("#execution-state")).to_contain_text("Loading Python", timeout=10000)
            page.locator("#stop").click()
            expect(page.locator("#run")).to_be_enabled(timeout=5000)
            assert "Execution stopped" in page.locator("#execution-state").inner_text()
            page.context.unroute(asset_url, hold_asset)
            runtime_requests.clear()
            # Mixed cached bootstrap versions must fail immediately, rather
            # than launching an old CDN worker and waiting for a load timeout.
            runner_url = '**/assets/runner.html?rev=bundled-v2'
            def stale_runner(route):
                route.fulfill(body="<script>parent.postMessage('pair-runner-ready','*')</script>", content_type='text/html')
            page.context.route(runner_url, stale_runner)
            page.locator('#run').click()
            expect(page.locator('#run')).to_be_enabled(timeout=10000)
            assert 'outdated Python runner' in page.locator('#execution-state').inner_text()
            page.context.unroute(runner_url, stale_runner)
        page.locator("#run").click()
        expect(page.locator("#run")).to_be_enabled(timeout=100000)
        print("RUN:", page.locator("#results").inner_text()[:1000])
        assert "2 / 2 passed" in page.locator("#results").inner_text(), page.locator("#execution-state").inner_text()
        downloads = len(runtime_requests)
        page.locator("#submit").click()
        expect(page.locator("#submit")).to_be_enabled(timeout=100000)
        assert "Submission saved" in page.locator("#execution-state").inner_text(), page.locator("#notice").inner_text()
        assert len(runtime_requests) == downloads, "Runtime files were downloaded again for Submit"
        # User code runs in an opaque-origin worker without DOM/account access.
        isolated_code = "class Solution:\n def containsDuplicate(self, nums):\n  import js\n  assert str(js.globalThis.location.origin) == 'null'\n  assert not hasattr(js.globalThis, 'document')\n  return len(nums) != len(set(nums))"
        page.locator("#editor").fill(isolated_code)
        page.locator("#run").click()
        expect(page.locator("#run")).to_be_enabled(timeout=100000)
        assert "2 / 2 passed" in page.locator("#results").inner_text()
        # Infinite Python must not block the page; Stop and the deadline recover.
        page.locator("#editor").fill("while True:\n    pass")
        page.locator("#run").click()
        expect(page.locator("#execution-state")).to_have_text("Running tests…", timeout=90000)
        page.locator("#stop").click()
        expect(page.locator("#run")).to_be_enabled(timeout=5000)
        assert "Execution stopped" in page.locator("#execution-state").inner_text()
        page.locator("#run").click()
        expect(page.locator("#execution-state")).to_have_text("Running tests…", timeout=90000)
        expect(page.locator("#run")).to_be_enabled(timeout=15000)
        assert "exceeded 10 seconds" in page.locator("#execution-state").inner_text()
        page.locator("#editor").fill(code)
        page.locator("#submit").click()
        expect(page.locator("#submit")).to_be_enabled(timeout=100000)
        assert "Submission saved" in page.locator("#execution-state").inner_text()
        page.screenshot(path="/private/tmp/pair-practice-dark.png", full_page=True)
        page.locator("#theme").click()
        page.screenshot(path="/private/tmp/pair-practice-light.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator("header").count() == 1
        assert page.locator("#runner-frame").is_hidden()
        page.screenshot(path="/private/tmp/pair-practice-mobile.png")
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.locator("#finish").click()
        page.locator("#summary").wait_for(state="visible")
        assert page.locator("#summary-score").inner_text() == "100% correctness"
        page.locator("#signout").click()
        page.locator("#account").wait_for(state="visible")
        page.locator("#login-tab").click()
        page.locator("#account-name").fill(username)
        page.locator("#password").fill("test-password-12345")
        page.locator("#account-submit").click()
        page.locator("#setup").wait_for(state="visible")
        assert "100%" in page.locator("#history").inner_text()
        page.screenshot(path="/private/tmp/pair-practice-setup.png", full_page=True)
        if os.environ.get("PAIR_TEST_ALL"):
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from app.practice.catalog import PROBLEMS
            base = Path("static/practice/harness.py").read_text()
            extra = "\n_original_evaluate = evaluate\ndef evaluate(problem, source, full=True):\n return {p['id']: _original_evaluate(p, p['reference']) for p in json.loads(source)}\n"
            rows = list(PROBLEMS.values())
            for offset in range(0, 150, 5):
                result = page.evaluate("""async ({batch, source}) => {
                    harness = source;
                    return await new PythonRunner().run({}, JSON.stringify(batch), true);
                }""", {"batch": rows[offset:offset + 5], "source": base + extra})
                assert all(r["passed"] == r["total"] for r in result.values()), result
                print("Pyodide catalog verified:", offset + 5, flush=True)
        assert not external, external
        assert runner_requests and all('rev=bundled-v2' in url for url in runner_requests), runner_requests
        browser.close()
        print("Browser smoke passed: cross-topic selection persistence, exact mixed-category session, app-only assets, runtime reuse, register, catalog, run, submit, finish, history, login, themes, mobile, isolated origin, stop, timeout")


if __name__ == "__main__":
    main()
