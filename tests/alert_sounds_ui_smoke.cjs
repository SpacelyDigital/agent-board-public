/* Actual headless WebKit, fictional state and local assets only. No OS alerts. */
const fs = require('fs');
const path = require('path');
const http = require('http');
const assert = require('assert/strict');
const { webkit } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const recordedFartDuration = 6912 / 22050;
const choices = [['soft-ding', .34], ['chime', .9], ['bell', 1.1], ['glass', .65], ['marimba', .6], ['arcade', .45], ['radar', .7], ['space-signal', .85], ['pop', .16], ['fart', recordedFartDuration]];
const native = fs.readFileSync(path.join(root, 'MacApp.swift'), 'utf8');
const begin = native.indexOf('let script = """', native.indexOf('    func installAccessScript()'));
const end = native.indexOf('"""', begin + 16);
assert(begin > 0 && end > begin);
const bridge = native.slice(begin + 'let script = """'.length, end);
assert.equal(bridge.split('\\(arguments)').length, 2);
const row = {key:'codex:audio-fixture',id:'audio-fixture',provider:'codex',title:'Audio fixture',project:'Example',status:'cooking',event_id:'fixture-start',updated_at:1,can_open:false};
let current = {...row};
const server = http.createServer((req, res) => {
  const pathname = new URL(req.url, 'http://localhost').pathname;
  if (pathname === '/api/state') {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({sessions:[current],history:[],sources:[],usage:[],csrf_token:'owned-fixture',now:Date.now()/1000,last_scan:Date.now()/1000,poll_interval:2}));
    return;
  }
  const name = pathname === '/' ? 'index.html' : decodeURIComponent(pathname.slice(1));
  const file = path.resolve(root, 'static', name);
  if (!file.startsWith(path.join(root, 'static') + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {res.writeHead(404);res.end();return;}
  const type = {'.js':'text/javascript','.css':'text/css','.html':'text/html','.wav':'audio/wav','.svg':'image/svg+xml'}[path.extname(file)] || 'application/octet-stream';
  res.setHeader('Content-Type', type);
  res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; media-src 'self'; img-src 'self'");
  res.end(fs.readFileSync(file));
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  if (process.argv.includes('--serve-only')) {
    console.log(JSON.stringify({fixture_url:origin,source:root,fictional_data:true,account_reads:false,provider_calls:false,expires_after_seconds:7200}));
    const timer = setTimeout(() => server.close(), 7200 * 1000);
    process.once('SIGTERM', () => server.close());
    await new Promise(resolve => server.once('close', resolve));
    clearTimeout(timer);
    return;
  }
  const browser = await webkit.launch({headless:true});
  const context = await browser.newContext({viewport:{width:1080,height:800}});
  const errors = [], outside = [];
  try {
    await context.addInitScript({content: `
      window.__audioPlayed = []; window.__audioDecoded = 0; window.__preferenceMessages = []; window.__toolbarMessages = [];
      Object.defineProperty(window, 'webkit', {configurable:true,value:{messageHandlers:{agentBoard:{postMessage(message) {
        if (message.kind === 'displayPreferences') { window.__preferenceMessages.push(message); window.name = JSON.stringify(message.values); }
        if (message === 'settings' || message === 'fullscreen') window.__toolbarMessages.push(message);
      }}}}});
      const contextType = window.AudioContext || window.webkitAudioContext;
      const decode = contextType.prototype.decodeAudioData;
      contextType.prototype.decodeAudioData = function(...args) { return decode.apply(this,args).then(buffer => { window.__audioDecoded += 1; return buffer; }); };
      const original = contextType.prototype.createBufferSource;
      contextType.prototype.createBufferSource = function() {
        const source = original.call(this); const start = source.start;
        source.start = function(...args) { window.__audioPlayed.push({duration:this.buffer.duration,channels:this.buffer.numberOfChannels}); return start.apply(this,args); };
        return source;
      };
      ${bridge.replace('\\(arguments)', `[${JSON.stringify(origin)},'owned-fixture',JSON.parse(window.name || '{}'),'fixture-generation']`)}
    `});
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', req => {if (!req.url().startsWith(origin + '/')) outside.push(req.url());});
    await page.goto(origin);
    await page.waitForFunction(() => document.querySelector('#connection-text').textContent !== 'Connecting');
    const settings = page.getByRole('button', {name:'Settings', exact:true});
    const sound = page.getByRole('button', {name:'Sound', exact:true});
    const appearance = page.locator('#appearance-menu > summary');
    const fullscreen = page.getByRole('button', {name:'Full screen', exact:true});
    assert.deepEqual(await page.locator('.toolbar-icon').evaluateAll(controls => controls.map(control => control.title)), ['Settings', 'Unmute sound', 'Appearance', 'Full screen']);
    assert.deepEqual(await page.locator('.toolbar-icon').allTextContents().then(texts => texts.map(text => text.trim())), ['', '', '', '']);
    await settings.press('Enter');
    await fullscreen.click();
    assert.deepEqual(await page.evaluate(() => window.__toolbarMessages), ['settings', 'fullscreen']);
    await page.evaluate(() => window.dispatchEvent(new CustomEvent('agentboard:fullscreenchange', {detail:true})));
    assert.equal(await page.getByRole('button', {name:'Exit full screen', exact:true}).getAttribute('title'), 'Exit full screen');
    assert(await page.locator('.fullscreen-exit').isVisible());
    await page.evaluate(() => window.dispatchEvent(new CustomEvent('agentboard:fullscreenchange', {detail:false})));
    assert(await fullscreen.isVisible());
    await appearance.press('Enter');
    assert(await page.locator('#layout-select').isVisible());
    await page.keyboard.press('Escape');
    assert.equal(await appearance.evaluate(element => document.activeElement === element), true);
    await appearance.press('Space');
    assert(await page.locator('#sound-select').isVisible());
    assert.equal(await page.locator('#sound-select option').count(),10);
    assert.equal(await page.locator('#sound-select').inputValue(),'soft-ding');
    assert.equal(await page.locator('#sound-toggle').getAttribute('aria-pressed'),'false');
    for (const [id,duration] of choices) {
      const before = await page.evaluate(() => window.__audioPlayed.length);
      await page.locator('#sound-select').selectOption(id);
      await page.locator('#sound-preview').click();
      await page.waitForFunction(n => window.__audioPlayed.length === n + 1, before);
      const played = await page.evaluate(() => window.__audioPlayed.at(-1));
      assert(Math.abs(played.duration-duration)<.002, `wrong selected audio buffer: ${id}`);
      assert.equal(played.channels,1);
      assert.equal(await page.locator('#sound-toggle').getAttribute('aria-pressed'),'false','Preview enabled sounds');
      assert.equal(await page.evaluate(() => window.__preferenceMessages.at(-1).values['agent-board:sound-id']), id);
    }
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#cooking-count').textContent === '1');
    await page.locator('#appearance-menu > summary').click();
    assert.equal(await page.locator('#sound-select').inputValue(),'fart','native-persisted sound choice did not restore');
    assert.equal(await page.locator('#sound-toggle').getAttribute('aria-pressed'),'false');
    await page.locator('#sound-toggle').click();
    assert.equal(await sound.getAttribute('title'), 'Mute sound');
    assert(await page.locator('.sound-waves').isVisible());
    assert(!await page.locator('.sound-muted').isVisible());
    assert.equal(await page.evaluate(() => window.__preferenceMessages.at(-1).values['agent-board:sound']),'on');
    await page.evaluate(() => Object.defineProperty(document,'hasFocus',{configurable:true,value:()=>true}));
    current = {...row,status:'ready',event_id:'fixture-complete'};
    await page.waitForFunction(() => window.__audioPlayed.length === 1);
    assert(Math.abs((await page.evaluate(() => window.__audioPlayed[0].duration))-recordedFartDuration)<.002,'attention did not use restored Fart recording');
    await page.evaluate(() => Object.defineProperty(document,'hasFocus',{configurable:true,value:()=>false}));
    current = {...row,status:'ready',event_id:'fixture-background-complete'};
    await page.waitForTimeout(2300);
    assert.equal(await page.evaluate(() => window.__audioPlayed.length),1,'background native board also played foreground audio');
    await page.locator('#sound-toggle').click();
    assert.equal(await sound.getAttribute('title'), 'Unmute sound');
    assert(await page.locator('.sound-muted').isVisible());
    assert.equal(await page.evaluate(() => window.__preferenceMessages.at(-1).values['agent-board:sound']),'off');
    current = {...row,event_id:'fixture-race-start'};
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#cooking-count').textContent === '1');
    await page.evaluate(() => {
      Object.defineProperty(document,'hasFocus',{configurable:true,value:()=>true});
      const original = window.fetch;
      window.fetch = async (...args) => {
        if (String(args[0]).startsWith('/sounds/')) await new Promise(resolve => {window.releaseAudioFetch = resolve;});
        return original(...args);
      };
    });
    await page.locator('#sound-toggle').click();
    current = {...row,status:'ready',event_id:'fixture-complete-during-load'};
    await page.waitForFunction(() => typeof window.releaseAudioFetch === 'function');
    await page.evaluate(() => {Object.defineProperty(document,'hasFocus',{configurable:true,value:()=>false});window.releaseAudioFetch();});
    await page.waitForFunction(() => window.__audioDecoded === 1);
    assert.equal(await page.evaluate(() => window.__audioPlayed.length),0,'audio loading in foreground started after background transition');
    await page.locator('#sound-toggle').click();
    let geometryChecks = 0;
    for (const width of [270,390,1080]) for (const layout of ['ledger','split','columns','focus','instrument']) {
      await page.setViewportSize({width,height:900});
      if (!await page.locator('#appearance-menu').evaluate(node => node.open)) await page.locator('#appearance-menu > summary').click();
      await page.locator('#layout-select').selectOption(layout);
      const metrics = await page.evaluate(() => {
        const controls = ['#sound-select','#sound-preview'].map(selector=>{const r=document.querySelector(selector).getBoundingClientRect();return {left:r.left,right:r.right,width:r.width,height:r.height};});
        const toolbar = [...document.querySelectorAll('.toolbar-icon')].map(icon => icon.getBoundingClientRect().toJSON());
        return {width:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth+1,controls,toolbar,font:getComputedStyle(document.body).fontFamily};
      });
      assert(!metrics.overflow, `page overflow ${layout}/${width}`);
      assert(metrics.controls.every(r=>r.left>=-1 && r.right<=metrics.width+1 && r.width>=20 && r.height>=18), `audio controls clipped ${layout}/${width}: ${JSON.stringify(metrics)}`);
      assert(metrics.toolbar.every((r,index) => r.left>=0 && r.right<=width && r.width>=32 && Math.abs(r.top-metrics.toolbar[0].top)<1 && (!index || r.left>=metrics.toolbar[index-1].right)), `toolbar clipped or wrapped ${layout}/${width}`);
      assert.match(metrics.font, /-apple-system/);
      geometryChecks++;
    }
    assert.deepEqual(errors,[]);assert.deepEqual(outside,[]);
    console.log(JSON.stringify({passed:true,previewed_actual_decoded_WAVs:10,native_publisher_choice_and_mute_sync:true,restored_choice_used_for_new_attention:true,background_native_web_audio_suppressed:true,background_transition_during_decode_suppressed:true,toolbar_keyboard_and_tooltips:true,layout_width_checks:geometryChecks,external_requests:0,OS_notifications_or_audibility_verified:false}));
  } finally {await context.close();await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error.stack);server.close();process.exitCode=1;});
