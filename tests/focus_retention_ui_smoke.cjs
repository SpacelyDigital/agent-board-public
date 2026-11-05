/* Keyboard focus through real WebKit renders, using only disposable local state. */
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),os=require('node:os'),assert=require('node:assert/strict');
const {webkit}=require(process.env.PLAYWRIGHT_MODULE);
const output=process.argv[2], root=path.resolve(process.argv[3]||path.join(__dirname,'..','static'));
const at=Date.now()/1000, results=[],errors=[],external=[];
let sessions,history=[],sampleMode=false,actionGate=null,phase=0;
const row=(key,status='ready',age=600)=>({key,provider:'claude',status,title:'Disposable '+key,project_name:'Validation',event_id:key+'-1',updated_at:at-age,attention_since:at-age,started_at:at-age,can_open:true,open_kind:'session',open_label:'Open Claude',preview:'Disposable reply',preview_kind:'reply'});
const snapshot=()=>({now:Date.now()/1000,csrf_token:'fixture-csrf',recovery_notice:'Focus phase '+phase,sessions,history,background:[],usage:[],sample_mode:sampleMode,sources:[{id:'fixture',name:'Disposable source',available:true}]});
const server=http.createServer(async(req,res)=>{
  const name=new URL(req.url,'http://127.0.0.1').pathname;
  if(name==='/api/state')return res.writeHead(200,{'Content-Type':'application/json'}).end(JSON.stringify(snapshot()));
  const action=name.match(/^\/api\/sessions\/([^/]+)\/(open|review|restore)$/);
  if(req.method==='POST'&&action){
    if(req.headers['x-agent-board-token']!=='fixture-csrf')return res.writeHead(403).end();
    try{
      let text='';for await(const chunk of req)text+=chunk;
      const body=JSON.parse(text),key=decodeURIComponent(action[1]),restore=action[2]==='restore';
      if(actionGate?.action===action[2]){const gate=actionGate;gate.requested();await gate.wait;if(gate.error)return res.writeHead(409,{'Content-Type':'application/json'}).end(JSON.stringify({error:'Disposable conflict'}));}
      if(action[2]==='open')return res.writeHead(200,{'Content-Type':'application/json'}).end(JSON.stringify({native_open_url:'claude://disposable-fixture',provider:'claude'}));
      const from=restore?history:sessions,to=restore?sessions:history,index=from.findIndex(item=>item.key===key);
      if(index<0||(!restore&&body.event_id!==from[index].event_id))return res.writeHead(409).end();
      to.push(from.splice(index,1)[0]);phase++;
      return res.writeHead(200,{'Content-Type':'application/json'}).end('{}');
    }catch(_){return res.writeHead(400).end();}
  }
  if(req.method!=='GET')return res.writeHead(403).end();
  const file=path.resolve(root,name==='/'?'index.html':name.slice(1));
  if(!file.startsWith(root+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile())return res.writeHead(404).end();
  res.writeHead(200,{'Content-Type':({'.html':'text/html','.js':'application/javascript','.css':'text/css'})[path.extname(file)]||'application/octet-stream'}).end(fs.readFileSync(file));
});
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const base='http://127.0.0.1:'+server.address().port,home=fs.mkdtempSync(path.join(os.tmpdir(),'agent-board-focus-'));
  let browser;
  try{
    browser=await webkit.launch({headless:true,env:{PATH:'/usr/bin:/bin:/usr/sbin:/sbin',HOME:home,TMPDIR:os.tmpdir(),LANG:'en_US.UTF-8'}});
    const context=await browser.newContext({viewport:{width:1180,height:900},reducedMotion:'reduce'});
    await context.route('**/*',r=>{if(!r.request().url().startsWith(base+'/')){external.push(r.request().url());return r.abort();}return r.continue();});
    const page=await context.newPage();page.on('pageerror',e=>errors.push(String(e)));page.setDefaultTimeout(6000);
    await context.addInitScript(()=>{
      localStorage.setItem('agent-board:sound','off');window.fixtureNativeOpens=0;
      Object.defineProperty(window,'webkit',{configurable:true,value:{messageHandlers:{agentBoard:{postMessage:message=>{if(message?.kind==='openChat')window.fixtureNativeOpens++;}}}}});
    });
    const update=async()=>{phase++;await page.evaluate(()=>dispatchEvent(new Event('online')));await page.locator('#monitor-message').filter({hasText:'Focus phase '+phase}).waitFor();};
    const open=key=>page.locator(`article[data-key="${key}"] [data-action="open"]`);
    const active=()=>page.evaluate(()=>({tag:document.activeElement?.tagName,key:document.activeElement?.closest('[data-key]')?.dataset.key||null,action:document.activeElement?.dataset.action||null,menu:document.activeElement?.dataset.menu||null,select:document.activeElement?.dataset.select||null,id:document.activeElement?.id||null,usable:!!(document.activeElement?.isConnected&&!document.activeElement.disabled&&document.activeElement.getClientRects().length)}));
    const check=async(layout,name,expected)=>{const actual=await active();results.push({layout,name,expected,actual,passed:Object.entries(expected).every(([k,v])=>actual[k]===v)});};
    // Option-Tab includes all controls in headless WebKit. The native app enables
    // ordinary Tab traversal through WKPreferences.tabFocusesLinks.
    const tabTo=async target=>{for(let n=0;n<80;n++){if(await target.evaluate(node=>node===document.activeElement))return;await page.keyboard.press('Alt+Tab');}throw new Error('Keyboard traversal did not reach '+await target.getAttribute('data-action'));};
    const load=async(layout,kind='live')=>{
      sessions=[row('a'),row('b','ready',300)];history=[];sampleMode=kind==='sample';phase++;
      if(kind!=='live')Object.assign(sessions[0],{can_open:false,open_error:kind==='sample'?'Sample conversations do not open another app.':'The native chat link is unavailable.'});
      await page.goto(base+'/');await page.locator('#count-ready').filter({hasText:/^2$/}).waitFor();
      await page.evaluate(layout=>{localStorage.setItem('agent-board:layout',layout);localStorage.setItem('agent-board:selected','a');},layout);
      await page.reload();await page.locator('#monitor-message').filter({hasText:'Focus phase '+phase}).waitFor();
      await page.locator('article[data-key="a"] [data-action="review"]').waitFor();
    };
    const toHistory=async()=>{
      await page.locator('article[data-key="a"] [data-action="review"]').press('Enter');
      await page.locator('#history-count').filter({hasText:/^1$/}).waitFor();
      await page.waitForFunction(()=>document.activeElement===document.querySelector('#history summary'));
      await page.keyboard.press('Enter');
      const restore=page.locator('article[data-key="a"] [data-action="restore"]');await restore.waitFor();
      return restore;
    };
    const restored=async()=>{await page.locator('#count-ready').filter({hasText:/^2$/}).waitFor();await page.waitForFunction(()=>![...document.querySelectorAll('button')].some(button=>button.textContent==='Working…'));};
    for(const layout of ['ledger','split','columns','focus','instrument']){
      await load(layout);
      await open('a').focus();await update();await check(layout,'unchanged poll preserves Open',{key:'a',action:'open'});
      sessions[0].status='cooking';await update();await check(layout,'automatic Ready to Cooking preserves Open',{key:'a',action:'open'});
      sessions[0].status='ready';await update();
      await page.locator('article[data-key="a"] [data-action="review"]').focus();sessions[0].status='cooking';await update();await check(layout,'disabled Reviewed falls back to same-chat Open',{key:'a',action:'open'});
      await page.locator('#sound-toggle').focus();sessions[0].status='ready';await update();await check(layout,'refresh never steals other-control focus',{id:'sound-toggle'});
      await page.locator('article[data-key="a"] [data-menu]').press('Enter');await page.keyboard.press('Enter');await page.locator('#rename-dialog[open]').waitFor();
      sessions[0].status='cooking';await update();await check(layout,'refresh leaves rename dialog input focused',{id:'rename-input'});await page.keyboard.press('Escape');
      await open('a').focus();sessions=sessions.filter(x=>x.key!=='a');await update();await check(layout,'removed row falls back to current tab',{id:'board-tab'});
      for(const kind of ['sample','unavailable','live']){
        await load(layout,kind);const restore=await toHistory();await tabTo(restore);await page.keyboard.press('Enter');await restored();
        await check(layout,kind+' keyboard Review to History to Restore returns to the same chat',layout==='split'?{select:'a',usable:true}:{key:'a',action:kind==='live'?'open':'review',usable:true});
        await page.keyboard.press('Alt+Tab');
        const next=await active();results.push({layout,name:kind+' next keyboard step stays in restored conversation controls',actual:next,passed:next.usable&&next.id!=='board-tab'&&(next.key!==null||next.select!==null)});
      }
      await load(layout,'sample');await(await toHistory()).click();await restored();
      await check(layout,'pointer Restore has an enabled same-chat fallback',layout==='split'?{select:'a',usable:true}:{key:'a',action:'review',usable:true});
      for(const kind of ['sample','live']){
        await load(layout,kind);const delayed=await toHistory();await tabTo(delayed);
        let requested;const started=new Promise(resolve=>{requested=resolve;});let release;
        actionGate={action:'restore',requested,wait:new Promise(resolve=>{release=resolve;})};
        try{
          await page.keyboard.press('Enter');await started;await tabTo(page.locator('#sound-toggle'));
          release();await restored();await check(layout,kind+' completed Restore never steals focus chosen during the request',{id:'sound-toggle',usable:true});
        }finally{release();actionGate=null;}
      }
      await load(layout);let requested;const started=new Promise(resolve=>{requested=resolve;});let release;
      actionGate={action:'review',requested,wait:new Promise(resolve=>{release=resolve;})};
      try{
        await page.locator('article[data-key="a"] [data-action="review"]').press('Enter');await started;
        await tabTo(page.locator('#sound-toggle'));release();
        await page.locator('#history-count').filter({hasText:/^1$/}).waitFor();
        await page.waitForFunction(()=>![...document.querySelectorAll('button')].some(button=>button.textContent==='Working…'));
        await check(layout,'completed Review never steals focus chosen during the request',{id:'sound-toggle',usable:true});
      }finally{release();actionGate=null;}
      for(const [action,error] of [['open',false],['open',true],['review',true],['restore',true]]){
        for(const movedFocus of [false,true]){
          await load(layout);let target=page.locator('article[data-key="a"] [data-action="'+action+'"]');
          if(action==='restore'){target=await toHistory();await tabTo(target);}
          let requested,release;const started=new Promise(resolve=>{requested=resolve;});
          actionGate={action,error,requested,wait:new Promise(resolve=>{release=resolve;})};
          const name=(error?'failed ':'successful ')+action+(movedFocus?' preserves user-selected Sound':' restores its enabled same-chat control');
          try{
            await target.press('Enter');await started;
            if(movedFocus)await tabTo(page.locator('#sound-toggle'));
            release();
            if(error)await page.locator('#action-message').filter({hasText:'Disposable conflict'}).waitFor();
            else await page.waitForFunction(()=>window.fixtureNativeOpens===1);
            await page.waitForFunction(()=>![...document.querySelectorAll('button')].some(button=>button.textContent==='Working…'));
            await check(layout,name,movedFocus?{id:'sound-toggle',usable:true}:{key:'a',action,usable:true});
            await page.keyboard.press('Alt+Tab');
            await check(layout,name+' allows the next keyboard step',movedFocus?{id:'fullscreen-toggle',usable:true}:action==='open'?{key:'a',action:'review',usable:true}:{key:'a',menu:'a',usable:true});
          }finally{release();actionGate=null;}
        }
      }
    }
    assert.deepEqual(external,[]);assert.deepEqual(errors,[]);
    const result={passed:results.every(x=>x.passed),case_count:results.length,engine:'WebKit',keyboard_traversal:'Option-Tab and Return; native ordinary Tab is tested separately with tabFocusesLinks enabled',static_root:root,provider_calls:0,native_window_or_assistive_tests:false,results,errors,external};
    if(output)fs.writeFileSync(output,JSON.stringify(result,null,2)+'\n');
    console.log(JSON.stringify({passed:result.passed,cases:results.length,failures:results.filter(x=>!x.passed)},null,2));
    if(!result.passed)process.exitCode=1;
  }finally{if(browser)await browser.close();await new Promise(r=>server.close(r));fs.rmSync(home,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1;});
