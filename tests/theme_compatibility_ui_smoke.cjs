/* Simulate only unsupported color-mix declarations. This is not old-macOS runtime proof. */
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),os=require('node:os'),assert=require('node:assert/strict');
const {webkit}=require(process.env.PLAYWRIGHT_MODULE);
const output=process.argv[2],root=path.resolve(process.argv[3]||path.join(__dirname,'..','static'));
let removeFallback=false,strip=false;
const server=http.createServer((req,res)=>{
  const name=new URL(req.url,'http://127.0.0.1').pathname;
  if(name==='/api/state')return res.writeHead(200,{'Content-Type':'application/json'}).end(JSON.stringify({now:Date.now()/1000,csrf_token:'fixture',sessions:[],history:[],background:[],sources:[],usage:[]}));
  if(req.method!=='GET')return res.writeHead(403).end();
  let file=path.resolve(root,name==='/'?'index.html':name.slice(1));
  if(!file.startsWith(root+path.sep))return res.writeHead(404).end();
  if(fs.existsSync(file)&&fs.statSync(file).isDirectory())file=path.join(file,'index.html');
  if(!fs.existsSync(file)||!fs.statSync(file).isFile())return res.writeHead(404).end();
  let content=fs.readFileSync(file);
  if(removeFallback&&['/effects.css','/colors/colors.css'].includes(name))content=Buffer.from(content.toString().replace(/background:\s*var\(--paper\);\s*(?=background:\s*color-mix\()/g,''));
  if(strip&&file.endsWith('.css'))content=Buffer.from(content.toString().replace(/\b(?:background|box-shadow)\s*:[^;{}]*color-mix\([^;{}]*;/g,''));
  res.writeHead(200,{'Content-Type':({'.html':'text/html','.js':'application/javascript','.css':'text/css'})[path.extname(file)]||'application/octet-stream'}).end(content);
});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const base='http://127.0.0.1:'+server.address().port,home=fs.mkdtempSync(path.join(os.tmpdir(),'agent-board-css-'));let browser;
 const results=[],errors=[],external=[];
 try{
  browser=await webkit.launch({headless:true,env:{PATH:'/usr/bin:/bin:/usr/sbin:/sbin',HOME:home,TMPDIR:os.tmpdir(),LANG:'en_US.UTF-8'}});
  for(strip of [false,true])for(removeFallback of [true,false]){
   const context=await browser.newContext({viewport:{width:1180,height:900},reducedMotion:'reduce'});
   await context.route('**/*',r=>{if(!r.request().url().startsWith(base+'/')){external.push(r.request().url());return r.abort();}return r.continue();});
   await context.addInitScript(()=>{localStorage.setItem('agent-board:theme-id','matrix');localStorage.setItem('agent-board:mode','dark');localStorage.setItem('agent-board:sound','off');});
   const page=await context.newPage();page.on('pageerror',e=>errors.push(String(e)));
   for(const [pagePath,selectors]of [['/',['.header','.usage','.summary']],['/colors/#matrix-dark',['.gallery-heading','.theme-picker','.gallery-footnote','.reviewed-preview']]]){
    await page.goto(base+pagePath);await page.locator('#board-effects').waitFor({state:'attached'});
    const backgrounds=await page.evaluate(selectors=>Object.fromEntries(selectors.map(selector=>[selector,getComputedStyle(document.querySelector(selector)).backgroundColor])),selectors);
    results.push({unsupported_color_mix_simulated:strip,readability_fallback_present:!removeFallback,page:pagePath,backgrounds});
   }
   await context.close();
  }
  for(const result of results){
   if(result.unsupported_color_mix_simulated)for(const value of Object.values(result.backgrounds))assert.equal(value==='rgba(0, 0, 0, 0)',!result.readability_fallback_present);
   else if(result.readability_fallback_present){const original=results.find(r=>!r.unsupported_color_mix_simulated&&!r.readability_fallback_present&&r.page===result.page);assert.deepEqual(result.backgrounds,original.backgrounds);}
  }
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
  const report={passed:true,engine:'WebKit',page_cases:results.length,measured_backgrounds:results.reduce((sum,r)=>sum+Object.keys(r.backgrounds).length,0),modern_blends_unchanged:true,unsupported_color_mix_simulated:true,oldest_macos_exercised:false,provider_calls:0,results,errors,external};
  if(output)fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify({passed:true,page_cases:report.page_cases,measured_backgrounds:report.measured_backgrounds,provider_calls:0}));
 }finally{if(browser)await browser.close();await new Promise(r=>server.close(r));fs.rmSync(home,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1;});
