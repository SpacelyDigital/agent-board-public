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
  let sampleMode = false;
  const state = () => ({now: at, csrf_token: 'disposable-csrf', sessions: [], history: [],
    background: [], sources: [], counts: {cooking: 0, needs_you: 0, ready: 0, disconnected: 0}, usage, sample_mode: sampleMode});
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
    const context = await browser.newContext({reducedMotion: 'reduce', locale: 'en-US'});
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
    const provider = (id, windows, options = {}) => ({id, source: id === 'claude' ? 'local_export' : 'local_record',
      updated_at: at, available: !!windows.length, stale: false, windows, loading: false, ...options});
    const weekly = page.locator('#usage-claude-weekly');
    const claudeCredits = page.locator('#usage-claude-credits');
    const codexCredits = page.locator('#usage-codex-credits');
    const codex = (credits) => provider('codex', [], {credits});
    const phase = async next => {
      usage = next;
      const response = page.waitForResponse(response => response.url() === base + '/api/state');
      await page.evaluate(() => window.dispatchEvent(new Event('online')));
      await response;
    };
    const claude = provider('claude', [{id: 'five_hour', remaining_percent: 4, reset_at: null},
      {id: 'weekly', remaining_percent: 32, reset_at: at + 4 * 3600}]);
    usage = [claude, codex({amount: 0, unit: 'credits', observed_at: at})];
    await page.goto(base + '/');
    await weekly.locator('.usage-value').filter({hasText: '32% remaining'}).waitFor();
    assert.equal(await page.locator('#usage-claude-five_hour').count(), 0);
    assert.match(await weekly.locator('.usage-freshness').textContent(), /Observed locally/);
    assert.match(await weekly.locator('.usage-reset').textContent(), /Resets in 4h/);
    assert.equal(await claudeCredits.locator('.usage-value').textContent(), 'Unavailable');
    assert.equal(await claudeCredits.getByRole('link').getAttribute('href'), 'https://claude.ai/settings/usage');
    assert.equal(await codexCredits.locator('.usage-value').textContent(), '0');
    assert.equal(await codexCredits.getByRole('link').count(), 0);

    await phase([claude, codex({amount: -12.5, unit: 'credits', observed_at: at - 180, stale: true})]);
    await codexCredits.locator('.usage-value').filter({hasText: '-12.5'}).waitFor();
    assert.match(await codexCredits.locator('.usage-freshness').textContent(), /Stale.*3m/);

    await phase([provider('claude', []), codex({amount: '0', unit: 'credits'})]);
    await codexCredits.locator('.usage-value').filter({hasText: 'Unavailable'}).waitFor();
    assert.equal(await codexCredits.getByRole('link').getAttribute('href'), 'https://chatgpt.com/codex/settings/usage');
    assert.equal(await codexCredits.locator('.usage-freshness').isVisible(), false);
    assert.equal(await weekly.locator('.usage-freshness').isVisible(), false);

    sampleMode = true;
    await phase([provider('claude', [], {credits: {amount: 25, unit: 'USD'}}),
      codex({amount: 240, unit: 'credits'})]);
    await claudeCredits.locator('.usage-value').filter({hasText: '$25.00'}).waitFor();
    assert.equal(await claudeCredits.locator('.usage-freshness').textContent(), 'Sample balance');
    assert.equal(await codexCredits.locator('.usage-value').textContent(), '240');
    assert.equal(await page.getByRole('link', {name: /credit balance/}).count(), 0);
    for (const width of [270, 390, 1080]) {
      await page.setViewportSize({width, height: 800});
      assert(await page.locator('.usage-credits').evaluateAll(rows => rows.every(row => {
        const box = row.getBoundingClientRect();
        return box.left >= 0 && box.right <= innerWidth && row.scrollWidth <= row.clientWidth + 1;
      })), `Credit row clips at ${width}px`);
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(external, []);
    const result = {passed: true, phases: 4, engine: 'WebKit', provider_calls: 0,
      five_hour_hidden: true, weekly_reset_retained: true, zero_and_negative_balances: true,
      missing_balances_link_to_provider: true, stale_balance_retains_age: true, samples_labelled: true};
    if (process.argv[2]) fs.writeFileSync(process.argv[2], JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(home, {recursive: true, force: true});
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
