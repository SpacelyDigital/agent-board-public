const {webkit} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
(async () => {
  const base = process.env.BOARD_QA_URL;
  const token = process.env.BOARD_QA_TOKEN;
  const out = process.env.BOARD_QA_OUT;
  const phase = process.env.BOARD_QA_PHASE || 'workflow';
  const key = 'codex:release-qa-chat';
  const browser = await webkit.launch({headless:true});
  try {
    const context = await browser.newContext({viewport:{width:1180,height:900}});
    const page = await context.newPage();
    const errors=[], external=[], apiRequests=[];
    page.on('pageerror', error => errors.push(String(error)));
    page.on('request', request => { if (new URL(request.url()).pathname.startsWith('/api/')) apiRequests.push({path:new URL(request.url()).pathname,token:request.headers()['x-agent-board-access'] || null}); });
    await context.route('**/*', route => {
      if (!route.request().url().startsWith(base + '/')) { external.push(route.request().url()); return route.abort(); }
      return route.continue();
    });
    await page.addInitScript(({base,token}) => {
      if (location.origin === base && location.pathname === '/') window.__agentBoardAccessToken = token;
    }, {base,token});
    await page.goto(base + '/');
    await page.locator('#connection-text').filter({hasText:'Monitoring locally'}).waitFor();
    assert.equal(await page.evaluate(() => '__agentBoardAccessToken' in window), false, 'access token global removed');
    assert.equal(await page.evaluate(token => [localStorage,sessionStorage].some(storage => Object.values(storage).some(value => String(value).includes(token))),token), false, 'access token not persisted in web storage');
    assert.equal(await page.evaluate(async () => (await fetch('/api/state')).status),403, 'bare in-page fetch cannot read private state');
    const card = page.locator(`article[data-key="${key}"]`);
    if (phase === 'restart') {
      await page.locator('#history > summary').click();
      await card.locator('[data-action="restore"]').waitFor();
      await card.locator('[data-action="restore"]').click();
      await card.locator('[data-action="review"]').waitFor();
      assert.equal(await page.locator('html').getAttribute('data-layout'),'ledger');
      await page.screenshot({path:out+'/after-restart-restored.png',fullPage:true});
    } else {
      await card.locator('[data-action="review"]').waitFor();
      assert.equal(await card.locator('.session-title').innerText(),'Disposable release workflow');
      await card.locator('.preview-toggle').click();
      assert.equal(await card.locator('.preview-text').innerText(),'The disposable release draft is ready.');
      await card.locator('[data-action="review"]').click();
      await page.locator('#history > summary').click();
      await card.locator('[data-action="restore"]').waitFor();
      await card.locator('[data-action="restore"]').click();
      await card.locator('[data-action="review"]').waitFor();
      await page.screenshot({path:out+'/board-authenticated.png',fullPage:true});
      await card.locator('[data-action="review"]').click();
      await card.locator('[data-action="restore"]').waitFor();
      // About is a public local page and must not depend on authenticated state.
      await page.goto(base+'/about/#privacy');
      assert.equal(await page.locator('h1').innerText(),'About Agent Board');
      assert.equal(await page.locator('#privacy').evaluate(element=>element.open),true);
      assert.equal(await page.locator('.coffee-link').getAttribute('href'),'https://ko-fi.com/spacelydigital');
      assert.equal(await page.locator('.qr-link').getAttribute('href'),'https://ko-fi.com/spacelydigital');
      assert.equal(await page.locator('.qr-link img').evaluate(element=>element.complete && element.naturalWidth>0),true);
      assert.equal(await page.locator('body').innerText().then(text=>text.includes('Disposable release workflow')),false,'About contains no private chat');
      await page.screenshot({path:out+'/about-desktop.png',fullPage:true});
      await page.locator('.qr-link img').screenshot({path:out+'/qr-rendered.png',scale:'css'});
      await page.setViewportSize({width:390,height:844});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'About fits narrow screen');
      await page.screenshot({path:out+'/about-narrow.png',fullPage:true});
      await page.locator('.back-link').click();
      await page.locator('#connection-text').filter({hasText:'Monitoring locally'}).waitFor();
      await page.locator('#history > summary').click();
      await card.locator('[data-action="restore"]').waitFor();
    }
    assert.deepEqual(errors,[],'no JavaScript errors');
    assert.deepEqual(external,[],'no external browser requests');
    const reads=apiRequests.filter(request=>request.path==='/api/state' && request.token===token);
    assert.ok(reads.length>0,'production app.js authenticated state reads');
    assert.ok(apiRequests.filter(request=>/\/(review|restore)$/.test(request.path)).every(request=>request.token===token),'production app.js authenticated mutations');
    fs.writeFileSync(out+'/browser-'+phase+'.json',JSON.stringify({phase,authenticatedReads:reads.length,mutations:apiRequests.filter(request=>/\/(review|restore)$/.test(request.path)).length,externalRequests:external,pageErrors:errors,passed:true},null,2));
    console.log(JSON.stringify({phase,passed:true,authenticatedReads:reads.length}));
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1)});
