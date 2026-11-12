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
  let storeProfile = false;
  const state = () => ({now: at, csrf_token: 'disposable-csrf', sessions: [], history: [],
    background: [], sources: [], counts: {cooking: 0, needs_you: 0, ready: 0, disconnected: 0}, usage, sample_mode: sampleMode, store_profile: storeProfile});
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
    const claudeNote = page.locator('#usage-claude-note');
    const codexNote = page.locator('#usage-codex-note');
    const codex = (credits) => provider('codex', [], {credits});
    let phases = 0;
    const phase = async next => {
      phases += 1;
      usage = next;
      const response = page.waitForResponse(response => response.url() === base + '/api/state');
      await page.evaluate(() => window.dispatchEvent(new Event('online')));
      await response;
    };
    let alignmentChecks = 0;
    const checkAlignment = async label => {
      for (const width of [270, 390, 1080, 1440]) {
        await page.setViewportSize({width, height: 800});
        const rows = await page.locator('.usage-provider').evaluateAll(providers => providers.map(provider => {
          const weekly = provider.querySelector('.usage-window').getBoundingClientRect();
          const credits = provider.querySelector('.usage-credits').getBoundingClientRect();
          return {weeklyHeight: weekly.height, creditHeight: credits.height, spacing: credits.top - weekly.top,
            clipped: provider.scrollWidth > provider.clientWidth + 1};
        }));
        assert(rows.every(row => !row.clipped), `${label} clips at ${width}px`);
        assert(Math.abs(rows[0].spacing - rows[1].spacing) < 1, `${label} row spacing differs at ${width}px: ${JSON.stringify(rows)}`);
        alignmentChecks++;
      }
    };
    const claude = provider('claude', [{id: 'five_hour', remaining_percent: 4, reset_at: null},
      {id: 'weekly', remaining_percent: 31.6, reset_at: at + 4 * 3600}]);
    usage = [claude, codex({amount: 0, unit: 'credits', observed_at: at})];
    await page.goto(base + '/');
    await weekly.locator('.usage-value').filter({hasText: '32% remaining'}).waitFor();
    assert.equal(await page.locator('#usage-claude-five_hour').count(), 0);
    assert.match(await claudeNote.textContent(), /Observed locally/);
    assert.match(await weekly.locator('.usage-reset').textContent(), /Resets in 4h/);
    assert.equal(await claudeCredits.locator('.usage-value').textContent(), 'Unavailable');
    assert.equal(await claudeCredits.getByRole('link').getAttribute('href'), 'https://claude.ai/settings/usage');
    assert.equal(await codexCredits.locator('.usage-value').textContent(), '0');
    assert.equal(await codexCredits.getByRole('link').count(), 0);

    await phase([claude, codex({amount: -12.5, unit: 'credits', observed_at: at - 180, stale: true})]);
    await codexCredits.locator('.usage-value').filter({hasText: '-13'}).waitFor();
    assert.match(await codexNote.textContent(), /stale.*3m/);

    await phase([provider('claude', [], {error: 'Keychain has not allowed access to Claude Safe Storage. No permission prompt was opened.', error_code: 'claude_account_access_required'}), codex({amount: '0', unit: 'credits'})]);
    await codexCredits.locator('.usage-value').filter({hasText: 'Unavailable'}).waitFor();
    assert.equal(await codexCredits.getByRole('link').getAttribute('href'), 'https://chatgpt.com/codex/settings/usage');
    assert.equal(await codexNote.textContent(), 'No usage reading is available.');
    assert.equal(await claudeNote.textContent(), 'Allow Claude account access in Settings.');

    await phase([provider('claude', [{id: 'weekly', remaining_percent: 32.5, reset_at: at + 3600}],
      {credits: {amount: 24.5, unit: 'USD', observed_at: at}}),
      provider('codex', [{id: 'weekly', remaining_percent: 95.6, reset_at: at + 3600}],
        {credits: {amount: 57824.503123, unit: 'credits', observed_at: at}})]);
    await codexCredits.locator('.usage-value').filter({hasText: '57,825'}).waitFor();
    assert.equal(await claudeCredits.locator('.usage-value').textContent(), '$25');
    assert.equal(await weekly.locator('.usage-value').textContent(), '33% remaining');
    assert.equal(await page.locator('#usage-codex-weekly .usage-value').textContent(), '96% remaining');

    await phase([provider('claude', [{id: 'weekly', remaining_percent: 30, reset_at: at + 3600}],
      {credits: {status: 'disabled', unit: 'USD', observed_at: at}})]);
    await claudeCredits.locator('.usage-value').filter({hasText: 'Turned off'}).waitFor();
    assert.equal(await claudeCredits.getByRole('link', {name: 'Manage Claude usage credits'}).textContent(), 'Manage credits');
    assert.match(await claudeNote.textContent(), /Observed locally/);

    const healthy = provider('codex', [{id: 'weekly', remaining_percent: 96, reset_at: at + 3600}],
      {source: 'account', credits: {amount: 57825, unit: 'credits', observed_at: at}});
    const limited = provider('claude', [{id: 'weekly', remaining_percent: 30, reset_at: at + 3600}],
      {source: 'account', stale: true, updated_at: at - 300, error_code: 'rate_limited', retry_at: at + 480,
       credits: {status: 'disabled', unit: 'USD', observed_at: at - 300, stale: true}});
    await phase([limited, healthy]);
    await claudeNote.filter({hasText: 'Usage checks are temporarily rate limited.'}).waitFor();
    assert.match(await claudeNote.textContent(), /Last reading 5m ago.*Retrying in 8m/);
    assert.equal(await codexNote.isVisible(), false);
    assert.equal(await page.locator('.usage-provider-note:visible').count(), 1);
    await checkAlignment('rate-limited and healthy');
    await phase([provider('claude', [], {source: 'account', loading: true}), healthy]);
    await weekly.locator('.usage-value').filter({hasText: 'Loading'}).waitFor();
    await checkAlignment('loading and healthy');
    await phase([provider('claude', [], {source: 'account', error: 'Account usage could not be read.'}), healthy]);
    await weekly.locator('.usage-value').filter({hasText: 'Unavailable'}).waitFor();
    await checkAlignment('unavailable and healthy');
    await phase([{...limited, stale: false, error_code: null, retry_at: null, updated_at: at,
      credits: {status: 'disabled', unit: 'USD', observed_at: at}}, healthy]);
    await claudeNote.waitFor({state: 'hidden'});
    await checkAlignment('recovered and healthy');

    storeProfile = true;
    await phase([provider('claude', [], {error: 'Not available in the App Store edition.'}), healthy]);
    await weekly.locator('.usage-value').filter({hasText: 'Unavailable'}).waitFor();
    assert.equal(await weekly.getAttribute('title'), 'Not available in the App Store edition.');
    assert.equal(await page.locator('#usage').getAttribute('aria-label'), 'Local usage');

    sampleMode = true;
    await phase([provider('claude', [], {credits: {amount: 25, unit: 'USD'}}),
      codex({amount: 240, unit: 'credits'})]);
    await claudeCredits.locator('.usage-value').filter({hasText: '$25'}).waitFor();
    assert.equal(await claudeNote.textContent(), 'Sample usage and balance.');
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
    const result = {passed: true, phases, engine: 'WebKit', provider_calls: 0, alignment_checks: alignmentChecks,
      five_hour_hidden: true, weekly_reset_retained: true, zero_and_negative_balances: true,
      missing_balances_link_to_provider: true, stale_balance_retains_age: true, samples_labelled: true,
      all_usage_numbers_rounded: true, keychain_recovery_visible: true, disabled_claude_credits_explicit: true,
      rate_limit_and_retry_visible_once: true, healthy_note_hidden: true, store_local_usage_disclosure: true};
    if (process.argv[2]) fs.writeFileSync(process.argv[2], JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(home, {recursive: true, force: true});
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
