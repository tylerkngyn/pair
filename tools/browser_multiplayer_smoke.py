"""Two isolated accounts exercise lobbies and ranked mode through the UI."""
import os
import secrets

from playwright.sync_api import sync_playwright, expect

BASE = os.environ.get('PAIR_TEST_URL', 'http://127.0.0.1:8765')


def register(page, name):
    page.goto(BASE)
    page.locator('#register-tab').click()
    page.locator('#account-name').fill(name)
    page.locator('#password').fill('test-password-12345')
    page.locator('#account-submit').click()
    expect(page.locator('#setup')).to_be_visible()
    page.locator('#multiplayer-mode').click()


def footer_at_bottom(page):
    assert page.evaluate('() => document.querySelector("footer").getBoundingClientRect().bottom >= innerHeight - 2')
    assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth')


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **({'executable_path':os.environ['PAIR_CHROME']} if os.environ.get('PAIR_CHROME') else {}))
        a = browser.new_context(viewport={'width':1440,'height':1000}, reduced_motion='reduce')
        b = browser.new_context(viewport={'width':1440,'height':1000}, reduced_motion='reduce')
        host, guest = a.new_page(), b.new_page()
        for page in (host, guest):
            page.on('pageerror', lambda error: print('PAGE ERROR:', error, flush=True))
            page.on('dialog', lambda dialog: dialog.accept())
        suffix = secrets.token_hex(3)
        register(host, 'host_' + suffix); register(guest, 'guest_' + suffix)
        expect(host.locator('#online-count')).to_have_text('2', timeout=10000)
        extra_tab = a.new_page(); extra_tab.goto(BASE)
        expect(extra_tab.locator('#online-count')).to_have_text('2', timeout=10000)
        extra_tab.close()
        footer_at_bottom(host)
        host.screenshot(path='/private/tmp/pair-lobbies-setup.png', full_page=True)
        host.locator('#category').select_option('Arrays & Hashing'); host.locator('#difficulty').select_option('easy')
        host.locator('#private-lobby').check()
        host.locator('#create-lobby').click()
        expect(host.locator('#lobby-view')).to_be_visible()
        expect(host.locator('#lobby-visibility')).to_contain_text('Private')
        code = host.locator('#room-code-display').inner_text()
        host.reload(); expect(host.locator('#lobby-view')).to_be_visible()
        expect(host.locator('#room-code-display')).to_have_text(code)
        expect(host.locator('#lobby-visibility')).to_contain_text('Private')
        expect(host.locator('#lobby-start')).to_be_disabled()
        footer_at_bottom(host)
        guest.locator('#refresh-lobbies').click()
        expect(guest.locator('#lobby-list')).not_to_contain_text('host_' + suffix)
        guest.locator('#join-code').fill(code.lower()); guest.locator('#join-lobby').click()
        expect(guest.locator('#lobby-view')).to_be_visible()
        guest.locator('#lobby-ready').click()
        expect(host.locator('#lobby-start')).to_be_enabled(timeout=10000)
        host.screenshot(path='/private/tmp/pair-lobby-ready.png', full_page=True)
        host.locator('#lobby-start').click()
        expect(host.locator('#match-intro')).to_be_visible(timeout=10000)
        expect(guest.locator('#match-intro')).to_be_visible(timeout=10000)
        expect(host.locator('#intro-mode')).to_contain_text('Unranked')
        expect(host.locator('#workspace')).to_be_visible(timeout=15000)
        expect(guest.locator('#workspace')).to_be_visible(timeout=15000)
        assert host.locator('#problem-title').inner_text() == guest.locator('#problem-title').inner_text()
        pid = host.evaluate('() => session.problems[0].id')
        reference = host.request.get(BASE + '/api/problems/' + pid).json()['reference']
        host.locator('#editor').fill(reference); host.locator('#submit').click()
        expect(host.locator('#submit')).to_be_enabled(timeout=90000)
        expect(host.locator('#execution-state')).to_have_text('Submission saved.')
        expect(guest.locator('#opponent-progress')).to_contain_text('100%', timeout=10000)
        assert guest.locator('#editor').input_value() != reference
        # Reload recovers the exact match and the same private draft.
        host.reload(); expect(host.locator('#workspace')).to_be_visible(timeout=15000)
        assert host.locator('#editor').input_value() == reference
        host.screenshot(path='/private/tmp/pair-match-workspace.png', full_page=True)
        host.locator('#finish').click()
        expect(host.locator('#match-waiting')).to_be_visible()
        guest.locator('#finish').click()
        expect(host.locator('#summary')).to_be_visible(timeout=15000)
        expect(guest.locator('#summary')).to_be_visible(timeout=15000)
        expect(host.locator('#summary-outcome')).to_have_text('You won')
        expect(host.locator('#summary-time')).to_contain_text('no rating change')
        footer_at_bottom(host)
        host.screenshot(path='/private/tmp/pair-match-summary.png', full_page=True)
        for page in (host, guest):
            page.locator('#again').click(); expect(page.locator('#setup')).to_be_visible()
        # The public-list join button and leaving a lobby work without a code.
        host.locator('#private-lobby').uncheck()
        host.locator('#create-lobby').click(); expect(host.locator('#lobby-view')).to_be_visible()
        expect(host.locator('#lobby-visibility')).to_contain_text('Public')
        guest.locator('#refresh-lobbies').click()
        guest.locator('.lobby-card').filter(has_text='host_' + suffix).get_by_role('button', name='Join', exact=True).click()
        expect(guest.locator('#lobby-view')).to_be_visible()
        guest.locator('#leave-lobby').click(); expect(guest.locator('#setup')).to_be_visible()
        expect(host.locator('#lobby-status')).to_have_text('1 / 2 players', timeout=10000)
        host.locator('#leave-lobby').click(); expect(host.locator('#setup')).to_be_visible()
        # No one else uses these unusual settings, so both accounts match.
        for page in (host, guest):
            page.locator('#category').select_option('Graphs'); page.locator('#difficulty').select_option('hard')
            page.locator('#duration').fill('17')
        host.locator('#find-ranked').click()
        expect(host.locator('#queue-view')).to_be_visible()
        host.evaluate('''() => {
          window.searchTicks = [{text:document.querySelector('#queue-elapsed').textContent, at:performance.now()}];
          window.tickObserver = new MutationObserver(() => searchTicks.push({text:document.querySelector('#queue-elapsed').textContent, at:performance.now()}));
          tickObserver.observe(document.querySelector('#queue-elapsed'), {childList:true});
          window.originalFetch = window.fetch;
          window.fetch = (url, options) => String(url).endsWith('/api/ranked/queue') && !options?.method
            ? new Promise((resolve, reject) => setTimeout(() => originalFetch(url, options).then(resolve, reject), 4000))
            : originalFetch(url, options);
        }''')
        host.wait_for_function('() => window.searchTicks.length >= 4')
        ticks = host.evaluate('searchTicks')
        def seconds(text):
            minutes, seconds = map(int, text.split(':')); return minutes * 60 + seconds
        for previous, current in zip(ticks, ticks[1:]):
            assert seconds(current['text']) == seconds(previous['text']) + 1, ticks
        for previous, current in zip(ticks[1:], ticks[2:]):
            assert 700 < current['at'] - previous['at'] < 1300, ticks
        host.evaluate('window.fetch = originalFetch; tickObserver.disconnect()')
        footer_at_bottom(host)
        host.screenshot(path='/private/tmp/pair-ranked-search.png', full_page=True)
        host.reload(); expect(host.locator('#queue-view')).to_be_visible(timeout=10000)
        expect(host.locator('#queue-settings')).to_contain_text('Graphs')
        expect(host.locator('#queue-settings')).to_contain_text('17 min')
        host.locator('#cancel-queue').click(); expect(host.locator('#setup')).to_be_visible()
        host.locator('#find-ranked').click(); guest.locator('#find-ranked').click()
        expect(host.locator('#loading-view')).to_be_visible()
        expect(host.locator('#loading-title')).to_have_text('Connecting…')
        expect(host.locator('#workspace')).not_to_be_visible()
        expect(host.locator('#match-intro')).to_be_visible(timeout=10000)
        expect(guest.locator('#match-intro')).to_be_visible(timeout=10000)
        expect(host.locator('#intro-self-name')).to_have_text('host_' + suffix)
        expect(host.locator('#intro-opponent-name')).to_have_text('guest_' + suffix)
        expect(host.locator('#intro-self-rating')).to_contain_text('1000 Elo')
        expect(host.locator('#intro-self-games')).to_have_text('0 ranked matches')
        host.screenshot(path='/private/tmp/pair-ranked-versus.png', full_page=True)
        host.wait_for_function("() => document.querySelector('#intro-countdown').textContent === '3'")
        deadline = host.evaluate('session.deadline')
        host.screenshot(path='/private/tmp/pair-ranked-countdown.png', full_page=True)
        host.reload()
        expect(host.locator('#match-intro')).to_be_visible(timeout=10000)
        expect(host.locator('#workspace')).to_be_visible(timeout=15000)
        expect(guest.locator('#workspace')).to_be_visible(timeout=15000)
        assert host.evaluate('session.deadline') == deadline
        assert host.evaluate('Math.abs(clockOffset + Date.now() - session.started_at * 1000) < 2500')
        # Once running, refresh resumes directly instead of replaying the intro.
        host.reload(); expect(host.locator('#workspace')).to_be_visible(timeout=10000)
        expect(host.locator('#match-intro')).not_to_be_visible()
        expect(host.locator('#match-mode')).to_contain_text('Ranked')
        host.locator('#leave-match').click()
        expect(host.locator('#summary')).to_be_visible()
        expect(guest.locator('#summary')).to_be_visible(timeout=15000)
        expect(host.locator('#summary-time')).to_contain_text('984 Elo')
        for page in (host, guest):
            page.locator('#again').click(); expect(page.locator('#setup')).to_be_visible()
        host.set_viewport_size({'width':390,'height':844}); footer_at_bottom(host)
        host.screenshot(path='/private/tmp/pair-lobbies-mobile.png', full_page=True)
        host.locator('#theme').click(); host.screenshot(path='/private/tmp/pair-lobbies-light.png', full_page=True)
        b.close()
        expect(host.locator('#online-count')).to_have_text('1', timeout=10000)
        browser.close()
        print('Two-player UI passed: unique online count, private-code/public-list joins, ready/start, Python submission, private drafts, reload, progress, unranked results, per-second clock under delayed polling, connecting/versus/countdown, countdown refresh, ranked results, footer and mobile', flush=True)


if __name__ == '__main__':
    main()
