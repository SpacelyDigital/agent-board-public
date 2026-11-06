/* Render source loss and recovery in WebKit using only disposable local records. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const playwright = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const root = path.resolve(process.argv[3] || path.join(__dirname, '..', 'static'));
  const output = process.argv[2];
  const token = 'disposable-source-recovery-token';
  const at = Date.now() / 1000;
  const layouts = ['ledger', 'split', 'columns', 'focus', 'instrument'];
  const remembered = {ready: 'Ready', needs_you: 'Needs you', cooking: 'Cooking'};
  let revoked = false;
  let revision = 0;
  let controlRevision = 0;
  let phaseID = 0;
  const session = (key, status, unavailable) => ({key, provider: 'claude', status,
    title: key === 'live-ready' ? 'Connected control conversation' : `Disposable ${status} conversation`,
    project_name: 'Disposable validation', event_id: key + '-' + (key === 'live-ready' ? controlRevision : revision),
    updated_at: at - 60, attention_since: at - 3600, started_at: at - 120,
    can_open: true, open_kind: 'session', open_label: 'Open Claude',
    source_unavailable: unavailable,
    source_unavailable_detail: unavailable ? 'Reconnect this source to confirm new activity.' : '',
    notification_eligible: true});
  const state = () => ({now: at, csrf_token: 'disposable-csrf', history: [], background: [], usage: [],
    recovery_notice: 'Disposable source update ' + phaseID,
    sources: [{id: 'fixture', name: 'Disposable source', available: !revoked}],
    sessions: [session('lost-ready', 'ready', revoked), session('lost-question', 'needs_you', revoked),
      session('lost-cooking', 'cooking', revoked), session('live-ready', 'ready', false)]});
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, 'http://127.0.0.1').pathname;
    if (pathname === '/api/state') {
      if (request.headers['x-agent-board-access'] !== token) return response.writeHead(401).end();
      return response.writeHead(200, {'Content-Type': 'application/json'}).end(JSON.stringify(state()));
    }
    const relative = pathname === '/' ? 'index.html' : pathname.slice(1);
    const target = path.resolve(root, relative);
    if (request.method !== 'GET' || !target.startsWith(root + path.sep) ||
        !fs.existsSync(target) || !fs.statSync(target).isFile()) return response.writeHead(404).end();
    const types = {'.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.wav': 'audio/wav'};
    response.writeHead(200, {'Content-Type': types[path.extname(target)] || 'application/octet-stream'});
    response.end(fs.readFileSync(target));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-board-source-ui-'));
  let browser;
  const errors = [], external = [], checks = [];
  try {
    const base = 'http://127.0.0.1:' + server.address().port;
    browser = await playwright.webkit.launch({headless: true, env: {
      PATH: '/usr/bin:/bin:/usr/sbin:/sbin', HOME: home, TMPDIR: os.tmpdir(), LANG: 'en_US.UTF-8'
    }});
    const context = await browser.newContext({viewport: {width: 1492, height: 1000}, reducedMotion: 'reduce'});
    await context.route('**/*', route => {
      if (!route.request().url().startsWith(base + '/')) {
        external.push(route.request().url());
        return route.abort();
      }
      return route.continue();
    });
    await context.addInitScript(value => {
      window.__agentBoardAccessToken = value;
      localStorage.setItem('agent-board:sound', 'on');
      window.fixtureSounds = 0;
      const Context = window.AudioContext || window.webkitAudioContext;
      const create = Context.prototype.createBufferSource;
      Context.prototype.createBufferSource = function() {
        const source = create.call(this), start = source.start;
        source.start = function(...args) { window.fixtureSounds += 1; return start.apply(this,args); };
        return source;
      };
    }, token);
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(String(error)));
    await page.goto(base + '/');
    await page.locator('#count-ready').filter({hasText: /^2$/}).waitFor();
    assert.equal(await page.locator('#count-needs_you').innerText(), '1');
    assert.equal(await page.locator('#count-cooking').innerText(), '1');
    const phase = async (unavailable, nextRevision = revision, nextControlRevision = controlRevision) => {
      revoked = unavailable;
      revision = nextRevision;
      controlRevision = nextControlRevision;
      phaseID += 1;
      const response = page.waitForResponse(response => response.url() === base + '/api/state');
      await page.evaluate(() => window.dispatchEvent(new Event('online')));
      await response;
      await page.locator('#monitor-message').filter({hasText: 'Disposable source update ' + phaseID}).waitFor();
    };
    await phase(true);
    await page.locator('#count-disconnected').filter({hasText: /^3$/}).waitFor();
    assert.equal(await page.locator('#count-ready').innerText(), '1');
    assert.equal(await page.locator('#count-needs_you').innerText(), '0');
    assert.equal(await page.locator('#count-cooking').innerText(), '0');
    assert.equal(await page.locator('#cooking-section').isVisible(), false);

    for (const layout of layouts) {
      await page.locator('#appearance-menu > summary').click();
      await page.locator('#layout-select').selectOption(layout);
      await page.locator('#appearance-menu > summary').click();
      assert.equal(await page.locator('html').getAttribute('data-layout'), layout);
      if (layout === 'focus') assert.equal(await page.locator('#primary-card .session-title').innerText(), 'Connected control conversation');
      for (const width of [1492, 980, 760, 390]) {
        await page.setViewportSize({width, height: 1000});
        const measured = await page.evaluate(() => {
          const visible = element => element.getClientRects().length && getComputedStyle(element).visibility !== 'hidden';
          const failures = [];
          const badges = [...document.querySelectorAll('.session .status-badge, .split-item .status-badge')].filter(visible);
          for (const badge of badges) {
            const parent = badge.closest('.session, .split-item');
            const age = parent.querySelector('.session-age');
            const b = badge.getBoundingClientRect(), a = age.getBoundingClientRect();
            if (Math.min(b.right, a.right) - Math.max(b.left, a.left) > 0.5 &&
                Math.min(b.bottom, a.bottom) - Math.max(b.top, a.top) > 0.5) failures.push('badge overlaps age');
            if (/Last known/.test(badge.textContent) && /waiting|running/.test(age.textContent)) failures.push('unconfirmed age claims live activity');
            if (badge.scrollWidth > badge.clientWidth + 1) failures.push('badge overflows label');
          }
          return {failures, badgeCount: badges.length, pageOverflow: document.documentElement.scrollWidth > innerWidth + 1};
        });
        assert.deepEqual(measured.failures, [], `${layout} at ${width}px`);
        assert(measured.badgeCount >= 4, `${layout} shows every conversation`);
        assert.equal(measured.pageOverflow, false, `${layout} fits ${width}px`);
        checks.push({layout, width, ...measured});
        if (output && layout === 'ledger' && width === 390) {
          await page.screenshot({path: path.join(path.dirname(output), 'source-revocation18-ledger-narrow.png'), fullPage: true});
        }
      }
      await page.setViewportSize({width: 1492, height: 1000});
      if (output) await page.screenshot({path: path.join(path.dirname(output), `source-revocation18-${layout}.png`), fullPage: true});
      for (const [status, label] of Object.entries(remembered)) {
        const key = status === 'needs_you' ? 'lost-question' : 'lost-' + status;
        const row = layout === 'split' ? page.locator(`[data-select="${key}"]`) : page.locator(`article[data-key="${key}"]`);
        assert.equal(await row.locator('.status-badge').innerText(), 'Last known: ' + label);
        assert.equal(await row.getAttribute('data-status'), 'disconnected');
      }
    }
    // A new unavailable event must be silent while a fresh connected event still sounds.
    await phase(true, 1);
    assert.equal(await page.evaluate(() => window.fixtureSounds), 0, 'unavailable events do not make a sound');
    await phase(true, 1, 1);
    await page.waitForFunction(() => window.fixtureSounds === 1);
    assert.equal(await page.evaluate(() => window.fixtureSounds), 1, 'only the connected control may make a sound');
    await phase(false);
    await page.locator('#count-disconnected').filter({hasText: /^0$/}).waitFor();
    assert.equal(await page.locator('#count-ready').innerText(), '2');
    assert.equal(await page.locator('#count-needs_you').innerText(), '1');
    assert.equal(await page.locator('#count-cooking').innerText(), '1');
    assert.equal(await page.locator('.session[data-key="lost-cooking"]').getAttribute('data-status'), 'cooking');
    assert.equal(await page.locator('.session[data-key="lost-ready"] .status-badge').innerText(), 'Ready');
    assert.equal(await page.evaluate(() => window.fixtureSounds), 1, 'unchanged reconfirmed events stay silent');
    assert.deepEqual(errors, []);
    assert.deepEqual(external, []);
    const result = {passed: true, engine: 'WebKit', provider_calls: 0, layouts: checks,
      revocation_counts: {ready: 1, needs_you: 0, cooking: 0, disconnected: 3},
      historical_labels_retained: true, reconnect_restores_live_counts: true,
      stale_cooking_not_in_cooking_group: true, unavailable_events_silent: true,
      connected_control_audio_verified: true};
    if (output) fs.writeFileSync(output, JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify({passed: true, engine: 'WebKit', layout_width_cases: checks.length, provider_calls: 0}));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(home, {recursive: true, force: true});
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
