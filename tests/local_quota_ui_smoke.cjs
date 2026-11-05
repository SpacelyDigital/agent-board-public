/* Actual WebKit rendering of disposable quota observations; no provider access. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
let playwright;
try { playwright = require(process.env.PLAYWRIGHT_MODULE || 'playwright'); }
catch (error) {
  if (process.env.PLAYWRIGHT_MODULE) throw error;
  playwright = require('/private/tmp/azwk/node_modules/playwright');
}

async function main() {
  const root = path.resolve(process.argv[3] || path.join(__dirname, '..', 'static'));
  const token = 'disposable-quota-ui-token';
  const at = Date.now() / 1000;
  let usage = [];
  const state = () => ({now: at, csrf_token: 'disposable-csrf', sessions: [], history: [],
    background: [], sources: [], counts: {cooking: 0, needs_you: 0, ready: 0, disconnected: 0}, usage});
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, 'http://127.0.0.1').pathname;
    if (pathname === '/api/state') {
      if (request.headers['x-agent-board-access'] !== token) {
        response.writeHead(401).end();
        return;
      }
      response.writeHead(200, {'Content-Type': 'application/json'}).end(JSON.stringify(state()));
      return;
    }
    const relative = pathname === '/' ? 'index.html' : pathname.slice(1);
    const target = path.resolve(root, relative);
    if (request.method !== 'GET' || !target.startsWith(root + path.sep) || !fs.existsSync(target) ||
        !fs.statSync(target).isFile()) {
      response.writeHead(404).end();
      return;
    }
    const types = {'.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml'};
    response.writeHead(200, {'Content-Type': types[path.extname(target)] || 'application/octet-stream'});
    response.end(fs.readFileSync(target));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-board-quota-ui-'));
  let browser;
  const errors = [], external = [];
  try {
    const base = 'http://127.0.0.1:' + server.address().port;
    browser = await playwright.webkit.launch({headless: true, env: {
      PATH: '/usr/bin:/bin:/usr/sbin:/sbin', HOME: home,
      TMPDIR: os.tmpdir(), LANG: 'en_US.UTF-8'
    }});
    const context = await browser.newContext({reducedMotion: 'reduce'});
    await context.route('**/*', route => {
      if (!route.request().url().startsWith(base + '/')) {
        external.push(route.request().url());
        return route.abort();
      }
      return route.continue();
    });
    await context.addInitScript(value => { window.__agentBoardAccessToken = value; }, token);
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(String(error)));
    const provider = (windows, options = {}) => ({id: 'claude', name: 'Claude', source: 'local_export',
      updated_at: at, available: !!windows.length, stale: false, windows, loading: false, ...options});
    const five = page.locator('#usage-claude-five_hour');
    const weekly = page.locator('#usage-claude-weekly');
    const phase = async next => {
      usage = [next];
      const response = page.waitForResponse(response => response.url() === base + '/api/state');
      await page.evaluate(() => window.dispatchEvent(new Event('online')));
      await response;
    };
    usage = [provider([{id: 'five_hour', remaining_percent: 32, reset_at: null},
      {id: 'weekly', remaining_percent: 32, reset_at: at + 4 * 3600}])];
    await page.goto(base + '/');
    await five.locator('.usage-value').filter({hasText: '32% remaining'}).waitFor();
    assert.equal(await five.locator('.usage-reset').textContent(), 'Reset time unavailable.');
    assert.equal(await five.locator('.usage-freshness').isVisible(), true);
    assert.match(await five.locator('.usage-freshness').textContent(), /Observed locally/);
    assert.match(await weekly.locator('.usage-reset').textContent(), /Resets in 4h/);

    await phase(provider([{id: 'weekly', remaining_percent: 32, reset_at: at + 4 * 3600}]));
    await five.locator('.usage-value').filter({hasText: 'Unavailable'}).waitFor();
    assert.equal(await five.locator('.usage-freshness').isVisible(), false);
    assert.equal(await five.locator('.usage-freshness').getAttribute('title'), '');
    assert(!/Last usage reading/.test(await five.getAttribute('title')));
    assert.equal(await weekly.locator('.usage-freshness').isVisible(), true);

    await phase(provider([{id: 'five_hour', remaining_percent: 32, reset_at: null}],
      {updated_at: at - 180, stale: true}));
    await five.locator('.usage-value').filter({hasText: '32% remaining'}).waitFor();
    assert.match(await five.locator('.usage-freshness').textContent(), /Stale.*3m/);
    assert.equal(await weekly.locator('.usage-freshness').isVisible(), false);
    assert.equal(await five.locator('.usage-reset').textContent(), 'Reset time unavailable.');

    await phase(provider([]));
    await five.locator('.usage-value').filter({hasText: 'Unavailable'}).waitFor();
    assert.equal(await five.locator('.usage-freshness').isVisible(), false);
    assert.equal(await weekly.locator('.usage-freshness').isVisible(), false);
    assert.deepEqual(errors, []);
    assert.deepEqual(external, []);
    const result = {passed: true, phases: 4, engine: 'WebKit', provider_calls: 0,
      missing_reset_preserves_percentage: true, absent_windows_have_no_observation_age: true,
      weekly_near_reset_keeps_label: true, stale_observation_retains_age: true};
    if (process.argv[2]) fs.writeFileSync(process.argv[2], JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(home, {recursive: true, force: true});
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
