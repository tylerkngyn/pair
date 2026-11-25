"""Delayed-client shared starts and stable headings; --lobby tests host start."""
import argparse
import os
import secrets

from playwright.sync_api import expect, sync_playwright

BASE = os.environ.get('PAIR_TEST_URL', 'http://127.0.0.1:8765')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--lobby', action='store_true')
args = parser.parse_args()


def register(page, name):
    page.goto(BASE)
    page.locator('#register-tab').click()
    page.locator('#account-name').fill(name)
    page.locator('#password').fill('test-password-12345')
    page.locator('#account-submit').click()
    expect(page.locator('#setup')).to_be_visible()
    page.locator('#multiplayer-mode').click()


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, **({'executable_path':os.environ['PAIR_CHROME']} if os.environ.get('PAIR_CHROME') else {}))
    a, b = [browser.new_context(viewport={'width':1280, 'height':900}, reduced_motion='reduce') for _ in range(2)]
    slow, fast = a.new_page(), b.new_page()
    suffix = secrets.token_hex(3)
    for page, name in ((slow, 'slow_'), (fast, 'fast_')):
        page.on('pageerror', lambda error: print('PAGE ERROR:', error, flush=True))
        register(page, name + suffix)
        page.evaluate('''() => {
          window.introHeadings = [];
          new MutationObserver(() => introHeadings.push(document.querySelector('#intro-heading').textContent))
            .observe(document.querySelector('#intro-heading'), {childList:true, subtree:true, characterData:true});
          window.reveals = [];
          new MutationObserver(() => {
            if (document.body.dataset.view === 'workspace') reveals.push(Date.now() + session.clock_offset);
          }).observe(document.body, {attributes:true, attributeFilter:['data-view']});
        }''')
    # The queued account receives no match_found push; its first match load
    # takes longer than the former seven-second creation-time warmup.
    slow.evaluate(r'''() => {
      allowSocket = false; clearTimeout(socketRetry); multiplayerSocket?.close();
      const original = window.fetch; let delayed = false;
      window.fetch = async (url, options) => {
        if (!delayed && /\/api\/ranked\/matches\/[a-f0-9]+$/.test(String(url))) {
          delayed = true; window.matchLoadDelayed = true;
          await new Promise(resolve => setTimeout(resolve, 10000));
        }
        return original(url, options);
      };
    }''')
    if args.lobby:
        fast.locator('#private-lobby').check(); fast.locator('#create-lobby').click()
        expect(fast.locator('#lobby-view')).to_be_visible()
        slow.locator('#join-code').fill(fast.locator('#room-code-display').inner_text())
        slow.locator('#join-lobby').click(); expect(slow.locator('#lobby-view')).to_be_visible()
        slow.locator('#lobby-ready').click(); expect(fast.locator('#lobby-start')).to_be_enabled(timeout=10000)
        # An old waiting-lobby response arriving after Start must not reopen
        # the lobby view or cancel the host's synchronization sequence.
        fast.evaluate(r'''() => {
          const original = window.fetch; let delayed = false;
          window.fetch = async (url, options) => {
            const response = await original(url, options);
            if (!delayed && !options?.method && /\/api\/lobbies\/[A-Z0-9]{6}$/.test(String(url))) {
              delayed = true; window.staleLobbyPoll = true;
              await new Promise(resolve => setTimeout(resolve, 4000));
            }
            return response;
          };
        }''')
        fast.wait_for_function('() => window.staleLobbyPoll === true')
        fast.locator('#lobby-start').click()
    else:
        slow.locator('#find-ranked').click(); fast.locator('#find-ranked').click()
    expect(fast.locator('#match-intro')).to_be_visible(timeout=10000)
    slow.wait_for_function('() => window.matchLoadDelayed === true')
    # The fast side must not open the problem while its peer is still loading.
    fast.wait_for_timeout(8000)
    expect(fast.locator('#workspace')).not_to_be_visible()
    expect(fast.locator('#match-intro')).to_be_visible()
    expect(slow.locator('#match-intro')).to_be_visible(timeout=15000)
    for page in (slow, fast):
        expect(page.locator('#intro-countdown')).to_have_text('3', timeout=10000)
    for page in (slow, fast):
        expect(page.locator('#workspace')).to_be_visible(timeout=10000)
    assert slow.evaluate('session.started_at') == fast.evaluate('session.started_at')
    assert slow.evaluate('session.deadline') == fast.evaluate('session.deadline')
    assert slow.locator('#problem-title').inner_text() == fast.locator('#problem-title').inner_text()
    reveals = [page.evaluate('reveals[0]') for page in (slow, fast)]
    assert abs(reveals[0] - reveals[1]) < 500, reveals
    start = slow.evaluate('session.started_at * 1000')
    assert all(-50 <= revealed - start < 500 for revealed in reveals), (start, reveals)
    for page in (slow, fast):
        assert not any('Waiting for opponent' in text for text in page.evaluate('introHeadings'))
    print(f"{'Lobby' if args.lobby else 'Ranked'} workspace reveal skew: {abs(reveals[0] - reveals[1]):.0f} ms", flush=True)
    browser.close()
    print('Both delayed/poll-only clients saw the shared countdown without flashing waiting text.', flush=True)
