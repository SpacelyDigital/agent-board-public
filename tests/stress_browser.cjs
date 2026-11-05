/* Fixture-only, seeded UI stress journeys. No provider requests or app opens. */
let playwright;
try { playwright = require(process.env.PLAYWRIGHT_MODULE || 'playwright'); }
catch(error) { if (process.env.PLAYWRIGHT_MODULE) throw error; playwright = require('/private/tmp/azwk/node_modules/playwright'); }
const { webkit } = playwright;
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const readline = require('node:readline');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const layouts = ['ledger', 'split', 'columns', 'focus', 'instrument'];
const themes = ['terminal', 'graphite', 'paper', 'amber', 'arctic', 'orchid', 'moss', 'rose', 'matrix', 'hearth', 'aurora', 'starfield', 'aquarium', 'lava', 'rain'];
const failures = [], results = [];
const peers = new Map();
let nextProfile = 0;
let stopRequested = false;
process.on('SIGTERM',()=>{stopRequested=true;});
process.on('SIGINT',()=>{stopRequested=true;});
function random(seed) { let value = seed >>> 0; return () => { value += 0x6D2B79F5; let t = value; t = Math.imul(t ^ t >>> 15, t | 1); t ^= t + Math.imul(t ^ t >>> 7, t | 61); return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
function fixture(directory) {
  const child = spawn(config.python, [config.fixtureScript, '--fixture', directory, '--runtime-root', config.runtimeRoot], {stdio:['pipe','pipe','pipe'],env:{PATH:'/usr/bin:/bin:/usr/sbin:/sbin',LANG:'en_US.UTF-8'}});
  const lines = readline.createInterface({input:child.stdout});
  const waiting = [];
  let stderr = '';
  child.stderr.on('data', data => stderr += data);
  lines.on('line', line => { const waiter = waiting.shift(); if (waiter) waiter.resolve(JSON.parse(line)); });
  child.on('exit', code => { for (const waiter of waiting.splice(0)) waiter.reject(new Error(`Fixture exited ${code}: ${stderr}`)); });
  const receive = () => new Promise((resolve,reject) => waiting.push({resolve,reject}));
  const ready = receive();
  return {
    ready,
    async command(operation, fields={}) {
      const response = receive();
      child.stdin.write(JSON.stringify({operation,...fields})+'\n');
      const result = await response;
      if (result.error) throw new Error(result.error);
      return result.result;
    },
    async close() {
      if (child.exitCode !== null) return;
      const exited = new Promise(resolve => child.once('exit',resolve));
      child.stdin.end(JSON.stringify({operation:'quit'})+'\n');
      await exited;
    },
  };
}
async function runProfile(browser, index) {
  const seed = (config.seed + Math.imul(index + 1, 104729)) >>> 0;
  const rng = random(seed);
  const directory = path.join(config.out, `profile-${String(index).padStart(4,'0')}`);
  fs.mkdirSync(directory,{recursive:true});
  const collector = fixture(directory);
  let context, page;
  const trace = [], pageErrors = [], external = [], mutations = [];
  const chosen = {layout:layouts[index % 5],theme:themes[Math.floor(index / 5) % 15],mode:Math.floor(index / 75) % 2 ? 'light' : 'dark'};
  let step = 'fixture startup';
  let started = Date.now();
  try {
    const {base,token} = await collector.ready;
    peers.set(index,{base,token});
    const viewport = [{width:390,height:844},{width:900,height:760},{width:1440,height:960}][index % 3];
    context = await browser.newContext({viewport,reducedMotion:index % 13 === 0 ? 'reduce' : 'no-preference'});
    await context.route('**/*', route => {
      const request = route.request();
      if (!request.url().startsWith(base+'/')) { external.push(request.url()); return route.abort(); }
      // Never hand a fake chat ID to the owner's real desktop applications.
      if (/\/api\/sessions\/.*\/open$/.test(new URL(request.url()).pathname)) { external.push('blocked desktop app open'); return route.abort(); }
      return route.continue();
    });
    await context.addInitScript(({base,token}) => {
      if (location.origin === base && location.pathname === '/') window.__agentBoardAccessToken = token;
      // Stress tests remain silent; the saved Sound control is checked separately.
      localStorage.setItem('agent-board:sound','off');
    }, {base,token});
    page = await context.newPage();
    page.setDefaultTimeout(6000);
    page.on('pageerror', error => pageErrors.push(String(error)));
    page.on('response', response => {
      if (response.request().method() === 'POST') mutations.push({path:new URL(response.url()).pathname,status:response.status()});
    });
    const connected = async () => page.locator('#connection-text').filter({hasText:'Monitoring locally'}).waitFor();
    const state = () => collector.command('state');
    const card = key => page.locator(`article[data-key="${key}"]`);
    const forcePoll = () => page.evaluate(()=>window.dispatchEvent(new Event('online')));
    async function boardCard(key='codex:ready') {
      await page.locator('#board-tab').click();
      const current = await state();
      if (current.history.some(row=>row.key === key)) await page.locator('#history').evaluate(element=>{element.open=true;});
      if (await page.locator('html').getAttribute('data-layout') === 'split' && current.sessions.some(row=>row.key === key)) await page.locator(`button[data-select="${key}"]`).click();
      await card(key).waitFor({state:'visible'});
      return card(key);
    }
    async function menu(key, action, section='board') {
      const item = section === 'board' ? await boardCard(key) : card(key);
      await item.locator('[data-menu]').click();
      await item.locator(`[data-menu-action="${action}"]`).waitFor({state:'visible'});
      return item.locator(`[data-menu-action="${action}"]`);
    }
    async function action(control, name, expected=200) {
      const response = page.waitForResponse(response => response.request().method()==='POST' && new URL(response.url()).pathname.endsWith('/'+name));
      await control.click();
      const result = await response;
      assert.equal(result.status(),expected,`${name}: ${await result.text()}`);
      await page.waitForFunction(()=>!document.querySelector('.session [data-action][disabled]') || ![...document.querySelectorAll('.session [data-action]')].some(button=>button.textContent==='Working…'));
      trace.push(name);
      return result;
    }
    step = 'initial load';
    await page.goto(base+'/');
    await connected();
    assert.equal(await page.locator('article.session').count(),5);
    assert.equal(await page.evaluate(()=>Object.hasOwn(window,'__agentBoardAccessToken')),false);
    step = 'keyboard conversation menu';
    const keyboardCard = await boardCard();
    const keyboardToggle = keyboardCard.locator('[data-menu]');
    for (const activation of ['Enter', 'Space']) {
      await keyboardToggle.press(activation);
      await keyboardCard.locator('.session-menu').waitFor({state:'visible'});
      assert.equal(await keyboardCard.locator('.session-menu').evaluate(element=>element.contains(document.activeElement)),true,'keyboard activation enters the conversation menu');
      await page.keyboard.press('End');
      await page.keyboard.press('Home');
      assert.equal(await keyboardCard.locator('.session-menu button:visible:enabled').first().evaluate(element=>element===document.activeElement),true,'Home returns to the first available action');
      await page.keyboard.press('Escape');
      assert.equal(await keyboardToggle.getAttribute('aria-expanded'),'false');
      assert.equal(await keyboardToggle.evaluate(element=>element===document.activeElement),true,'Escape returns focus to conversation options');
    }
    trace.push('keyboard Enter/Space/menu arrows/Escape focus');
    step = 'appearance controls';
    await page.locator('#appearance-menu > summary').click();
    await page.selectOption('#layout-select',chosen.layout);
    await page.selectOption('#theme-select',chosen.theme);
    await page.selectOption('#mode-select',chosen.mode);
    if (await page.locator('#motion-option').isVisible() && !await page.locator('#motion-toggle').isDisabled()) {
      await page.locator('#motion-toggle').uncheck();
      await page.locator('#motion-toggle').check();
      await page.locator('#motion-toggle').uncheck();
      trace.push('motion off/on/off');
    }
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,'horizontal overflow');
    trace.push(`appearance:${chosen.layout}/${chosen.theme}/${chosen.mode}`);
    if (index===1) {
      step = 'switch rename dialog during delayed save';
      let release, intercepted;
      const gate=new Promise(resolve=>{release=resolve;});
      const arrived=new Promise(resolve=>{intercepted=resolve;});
      const delay=async route=>{
        if(new URL(route.request().url()).pathname==='/api/sessions/codex%3Aready/rename') {intercepted();await gate;}
        await route.continue();
      };
      await page.route('**/api/sessions/*/rename',delay);
      await (await menu('codex:ready','rename')).click();
      await page.locator('#rename-input').fill('First delayed title');
      await page.locator('#rename-save').click();
      await arrived;
      await page.locator('#rename-cancel').click();
      await (await menu('codex:waiting','rename')).click();
      await page.locator('#rename-input').fill('Second unsaved title');
      release();
      await page.waitForFunction(()=>!document.getElementById('rename-save').disabled);
      assert.equal(await page.locator('#rename-dialog').evaluate(element=>element.open),true,'a completed old save must not close a new rename dialog');
      assert.equal(await page.locator('#rename-input').inputValue(),'Second unsaved title');
      await page.locator('#rename-cancel').click();
      await page.unroute('**/api/sessions/*/rename',delay);
      trace.push('delayed save preserves newer rename dialog');
    }
    step = 'rename literal text';
    await (await menu('codex:ready','rename')).click();
    const title = index % 2 ? `Profile ${index} café 👾 ${'Z'.repeat(80)}` : `<img src=x onerror="window.owned=true"> Profile ${index}`;
    await page.locator('#rename-input').fill(title);
    await action(page.locator('#rename-save'),'rename');
    await page.locator('#rename-dialog').waitFor({state:'hidden'});
    await boardCard();
    assert.equal(await card('codex:ready').locator('.session-title').innerText(),title);
    assert.equal(await page.evaluate(()=>window.owned === true),false,'title injection');
    assert.equal(await card('codex:ready').locator('.session-title img').count(),0);
    if (index % 5 === 0) {
      for (const [peerIndex,peer] of peers) {
        if (peerIndex===index) continue;
        try {
          const wrong=await fetch(peer.base+'/api/state',{headers:{'X-Agent-Board-Access':token},signal:AbortSignal.timeout(1500)});
          assert.equal(wrong.status,403,'one installation token cannot read another installation');
          const payload=await wrong.text();
          assert.equal(payload.includes(title),false,'foreign installation exposes no local canary');
          const own=await fetch(base+'/api/state',{headers:{'X-Agent-Board-Access':peer.token},signal:AbortSignal.timeout(1500)});
          assert.equal(own.status,403,'cross-installation rejection is reciprocal');
          trace.push('cross-profile token/canary isolation');
        } catch(error) { if(error.cause?.code!=='ECONNREFUSED') throw error; }
        break;
      }
    }
    step = 'snooze current reply';
    await action(await menu('codex:ready','snooze'),'snooze');
    assert.ok((await state()).sessions.find(row=>row.key==='codex:ready').snoozed_until>Date.now()/1000);
    await boardCard();
    await card('codex:ready').locator('.snooze-note').waitFor({state:'visible'});
    step = 'review and restore';
    await action(card('codex:ready').locator('[data-action="review"]'),'review');
    assert.ok((await state()).history.some(row=>row.key==='codex:ready'));
    await boardCard();
    await action(card('codex:ready').locator('[data-action="restore"]'),'restore');
    assert.ok((await state()).sessions.some(row=>row.key==='codex:ready'));
    step = 'move to background and return';
    await action(await menu('codex:ready','move'),'move');
    assert.ok((await state()).background.some(row=>row.key==='codex:ready'));
    await page.locator('#background-tab').click();
    await card('codex:ready').waitFor({state:'visible'});
    await action(await menu('codex:ready','move','background'),'move');
    await boardCard();
    assert.ok((await state()).sessions.some(row=>row.key==='codex:ready'));
    step = 'automatic title and placement';
    await action(await menu('codex:ready','automatic-title'),'rename');
    await action(await menu('codex:ready','automatic-placement'),'move');
    assert.equal((await state()).sessions.find(row=>row.key==='codex:ready').title_override,undefined);
    step = 'seeded rapid controls';
    for (let operation=0;operation<8;operation++) {
      const type = Math.floor(rng()*6);
      if (type<=2) {
        await page.locator('#board-tab').click();
        if (!await page.locator('#appearance-menu').evaluate(element=>element.open)) await page.locator('#appearance-menu > summary').click();
        if (type===0) await page.selectOption('#layout-select',layouts[Math.floor(rng()*layouts.length)]);
        if (type===1) await page.selectOption('#theme-select',themes[Math.floor(rng()*themes.length)]);
        if (type===2) await page.selectOption('#mode-select',rng()<0.5?'light':'dark');
        trace.push('random appearance '+type);
      } else if (type===3) {
        await page.locator('#background-tab').click();
        await page.locator('#background-tab').press('Home');
        assert.equal(await page.locator('#board-tab').getAttribute('aria-selected'),'true');
        trace.push('keyboard tab switch');
      } else if (type===4) {
        await (await boardCard()).locator('[data-menu]').press('ArrowDown');
        await page.keyboard.press('Escape');
        assert.equal(await card('codex:ready').locator('[data-menu]').getAttribute('aria-expanded'),'false');
        trace.push('keyboard menu escape');
      } else {
        await boardCard();
        const preview=card('codex:ready').locator('.preview-toggle');
        if (await preview.isVisible()) { await preview.click(); await preview.click(); }
        trace.push('preview toggle');
      }
    }
    step = 'preferences reload';
    const saved = await page.evaluate(()=>({layout:document.documentElement.dataset.layout,theme:document.documentElement.dataset.palette,mode:document.documentElement.dataset.theme}));
    await page.reload();
    await connected();
    assert.deepEqual(await page.evaluate(()=>({layout:document.documentElement.dataset.layout,theme:document.documentElement.dataset.palette,mode:document.documentElement.dataset.theme})),saved);
    trace.push('preferences reload');
    if (index % 20 === 0) {
      step = 'multi-tab stale review conflict';
      await boardCard();
      const frozen = await state();
      await page.route('**/api/state',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(frozen)}));
      const second=await context.newPage();
      second.on('pageerror',error=>pageErrors.push(String(error)));
      await second.goto(base+'/');
      await second.locator('#connection-text').filter({hasText:'Monitoring locally'}).waitFor();
      await collector.command('advance',{ident:'ready',status:'cooking'});
      await second.evaluate(()=>window.dispatchEvent(new Event('online')));
      await second.waitForFunction(()=>[...document.querySelectorAll('article[data-key="codex:ready"]')].some(element=>element.dataset.status==='cooking'));
      await action(await menu('codex:ready','snooze'),'snooze',409);
      assert.equal((await state()).sessions.find(row=>row.key==='codex:ready').snoozed_until,0,'stale snooze must not silence a new turn');
      await action(card('codex:ready').locator('[data-action="review"]'),'review',409);
      assert.equal((await state()).history.some(row=>row.key==='codex:ready'),false,'stale review must not clear new turn');
      await page.unroute('**/api/state');
      await forcePoll();
      await page.waitForFunction(()=>document.querySelector('article[data-key="codex:ready"]')?.dataset.status==='cooking');
      await second.close();
      await collector.command('advance',{ident:'ready',status:'ready'});
      await forcePoll();
      await page.waitForFunction(()=>document.querySelector('article[data-key="codex:ready"]')?.dataset.status==='ready');
      assert.equal((await state()).sessions.find(row=>row.key==='codex:ready').snoozed_until,0,'new event clears old snooze');
      trace.push('new turn reappears and clears prior snooze');
    }
    if (index % 25 === 0) {
      step = 'collector loss and restart';
      await collector.command('stop');
      await forcePoll();
      await page.locator('#connection-text').filter({hasText:'Monitor disconnected'}).waitFor();
      assert.equal(await page.locator('.session [data-menu]:enabled').count(),0,'no mutation while disconnected');
      await collector.command('restart');
      await forcePoll();
      await connected();
      await boardCard();
      await action(card('codex:ready').locator('[data-action="review"]'),'review');
      await collector.command('restart');
      await forcePoll();
      await connected();
      assert.ok((await state()).history.some(row=>row.key==='codex:ready'),'review persists after actual process restart');
      trace.push('disconnect/reconnect/review persistence');
    }
    step = 'final state invariant';
    const final = await state();
    const rows = [...final.sessions,...final.history,...final.background];
    assert.equal(new Set(rows.map(row=>row.key)).size,rows.length,'session appears once');
    assert.equal(rows.length,5);
    assert.equal(await page.evaluate(()=>window.owned===true),false);
    assert.ok(await page.locator('#board-effects').count()<=1,'at most one effects canvas');
    assert.deepEqual(pageErrors,[],'browser JavaScript errors');
    assert.deepEqual(external,[],'external network or desktop open attempted');
    assert.equal(mutations.some(item=>item.status>=500),false,'no server failures');
    if (index===0) {
      step = 'damaged database recovery';
      const recovered=await collector.command('corrupt');
      assert.ok(recovered.recovery_notice);
      await forcePoll();
      await page.locator('#monitor-message').filter({hasText:'database was damaged'}).waitFor();
      const notice=await page.locator('#monitor-message').innerText();
      await forcePoll();
      assert.equal(await page.locator('#monitor-message').innerText(),notice,'recovery notice remains on healthy polling');
      assert.ok(await page.locator('#sources .source').count()>0,'source health remains rendered');
      await collector.command('stop');
      await forcePoll();
      await page.locator('#connection-text').filter({hasText:'Monitor disconnected'}).waitFor();
      assert.ok((await page.locator('#monitor-message').innerText()).includes(notice),'offline recovery notice retained');
      assert.ok((await page.locator('#monitor-message').innerText()).includes('The monitor is disconnected.'),'offline warning remains visible alongside recovery');
      await collector.command('restart');
      await forcePoll();
      await connected();
      assert.equal(await page.locator('#monitor-message').innerText(),notice,'recovery notice persists after actual collector restart');
      trace.push('damaged DB recovery/poll/disconnect/restart notice');
    }
    const result={index,seed,viewport,chosen,trace,mutations,elapsedMs:Date.now()-started,passed:true};
    results.push(result);
    fs.writeFileSync(path.join(directory,'journey.json'),JSON.stringify(result,null,2));
    if (index % 25 === 0) console.log(JSON.stringify({completed:results.length,profile:index,seed,passed:true}));
  } catch (error) {
    const failure={index,seed,chosen,step,error:String(error.stack||error),trace,mutations,pageErrors,external,elapsedMs:Date.now()-started};
    failures.push(failure);
    fs.writeFileSync(path.join(directory,'failure.json'),JSON.stringify(failure,null,2));
    if (page) await page.screenshot({path:path.join(directory,'failure.png'),fullPage:true}).catch(()=>{});
    console.error(JSON.stringify({profile:index,seed,step,error:String(error)}));
  } finally {
    peers.delete(index);
    try { if (context) await context.close(); }
    finally { await collector.close(); }
  }
}
(async()=>{
  const browser=await webkit.launch({headless:true,handleSIGINT:false,handleSIGTERM:false,handleSIGHUP:false});
  const started=Date.now();
  try {
    await Promise.all(Array.from({length:config.concurrency},async()=>{
      while (nextProfile<config.profiles && !stopRequested) {
        const index=nextProfile++;
        await runProfile(browser,index);
        // A concrete failure is fixed before spending time repeating it.
        if (failures.length) break;
      }
    }));
  } finally {await browser.close();}
  const report={seed:config.seed,requestedProfiles:config.profiles,completedProfiles:results.length,failedProfiles:failures.length,concurrency:config.concurrency,elapsedMs:Date.now()-started,mutations:results.reduce((sum,result)=>sum+result.mutations.length,0),uiTraceSteps:results.reduce((sum,result)=>sum+result.trace.length,0),crossProfileIsolationChecks:results.filter(result=>result.trace.includes('cross-profile token/canary isolation')).length,layoutThemeModeCombinations:new Set(results.map(result=>`${result.chosen.layout}/${result.chosen.theme}/${result.chosen.mode}`)).size,sourceManifest:'source-manifest.json',results,failures,scope:'Actual Playwright WebKit UI with disposable per-profile HOME, provider fixture files, real source adapters, collector HTTP API and SQLite store. Runtime frozen with SHA-256 manifest. Provider usage/projects disabled; account environment variables absent from collector; non-loopback browser requests and desktop app opens blocked. Multi-tab stale responses intentionally frozen at browser boundary; lifecycle updates use fixture logs. Bounded concurrency is not a claim of hundreds of simultaneous Mac installations.',unverified:['native macOS permission dialogs, signing and Gatekeeper','actual provider account reads or quota usage','multiple physical Macs and real user behavior']};
  fs.writeFileSync(path.join(config.out,'results.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify({completedProfiles:report.completedProfiles,failedProfiles:report.failedProfiles,uiTraceSteps:report.uiTraceSteps,mutations:report.mutations,elapsedMs:report.elapsedMs,report:path.join(config.out,'results.json')}));
  if(failures.length || results.length!==config.profiles) process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1});
