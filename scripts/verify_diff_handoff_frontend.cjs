#!/usr/bin/env node
/* ISS-188: Chromium + production frontend/serve, synthetic DB only.
 * Gate genuine /api/snapshots responses at the network boundary. No extracted
 * functions, fake DOM, sleeps, retries, scans or model calls. Legal b1 prewarm
 * precedes the overview CTA, exactly as in the original overview regression.
 */
'use strict';
const fs=require('fs'),os=require('os'),path=require('path'),http=require('http'),net=require('net');
const {spawn,execFileSync}=require('child_process');
const {once}=require('events');
const {chromium}=require('playwright');
const {SEED_PY}=require('./verify_storage_overview_frontend.cjs');
const REPO=path.resolve(__dirname,'..');
const PY=process.env.FATHOM_PYTHON||path.join(REPO,'.venv/bin/python');
const idx=process.argv.indexOf('--evidence');
const evidence=path.resolve(idx>=0?process.argv[idx+1]:'verify-results/iss188/handoff');
fs.mkdirSync(evidence,{recursive:true});
const checks=[],traces=[],resources=[];let gateSequence=0;
function record(name,ok,detail=''){checks.push({name,ok:!!ok,detail});console.error(`${ok?'PASS':'FAIL'} ${name} ${detail}`);if(!ok)process.exitCode=1;}
process.on('unhandledRejection',error=>record('unhandled-rejection',false,error.stack||String(error)));
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return{promise,resolve};}
function get(port,url){return new Promise((resolve,reject)=>{const req=http.get({host:'127.0.0.1',port,path:url},res=>{let body='';res.on('data',c=>body+=c);res.on('end',()=>resolve({status:res.statusCode,json:JSON.parse(body)}));});req.setTimeout(2000,()=>req.destroy(new Error('HTTP timeout')));req.on('error',reject);});}
async function freePort(){const s=net.createServer();s.listen(0,'127.0.0.1');await once(s,'listening');const p=s.address().port;await new Promise(r=>s.close(r));return p;}
async function helper(){
 const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'fathom-iss188-')),runtime=path.join(tmp,'runtime'),scanRoot=path.join(tmp,'scanroot');fs.mkdirSync(scanRoot);
 const port=await freePort(),env={...process.env,FATHOM_RUNTIME_DIR:runtime,FATHOM_DB:path.join(runtime,'data/fathom.db'),FATHOM_SCAN_ROOT:scanRoot,FATHOM_PORT:String(port),PYTHONPATH:REPO};
 delete env.ISS158_STALE;
 const seed=spawn(PY,['-c',SEED_PY,tmp],{cwd:REPO,env});let seedOut='';seed.stdout.on('data',c=>seedOut+=c);seed.stderr.on('data',c=>seedOut+=c);const [code]=await once(seed,'exit');if(code!==0)throw Error(`seed ${code}: ${seedOut}`);
 const info=JSON.parse(seedOut.trim().split('\n').pop());
 const child=spawn(PY,['-m','fathom','serve'],{cwd:REPO,env});let log='';child.stdout.on('data',c=>log+=c);child.stderr.on('data',c=>log+=c);
 const closed=once(child,'close');let health;
 // Readiness is an actual HTTP barrier; the bounded loop is not a race-test retry.
 for(let attempt=0;attempt<300;attempt++){try{health=(await get(port,'/health')).json;if(health.pid===child.pid)break;}catch{}await new Promise(r=>setTimeout(r,100));}
 record('helper.production-identity',health?.pid===child.pid&&health.runtime_mode==='development'&&health.port===port,JSON.stringify({pid:child.pid,port,runtime,scanRoot}));
 const resource={pid:child.pid,port,runtime,scanRoot};resources.push(resource);
 return{base:`http://127.0.0.1:${port}`,info,port,env,async close(){child.kill('SIGTERM');await closed;resource.pidExited=true;resource.listenClosed=await new Promise(resolve=>{const s=net.connect({host:'127.0.0.1',port});s.once('connect',()=>{s.destroy();resolve(false);});s.once('error',e=>resolve(e.code==='ECONNREFUSED'));});fs.writeFileSync(path.join(evidence,'serve.log'),log);record('helper.cleanup',resource.pidExited&&resource.listenClosed,JSON.stringify(resource));}};
}
async function catalogGate(page,trace){
 const entered=deferred(),release=deferred(),token=String(++gateSequence);let gated=false;
 const handler=async route=>{
  if(gated){await route.continue();return;}gated=true;
  const response=await route.fetch(),json=await response.json();
  trace.push({event:'catalog-captured',url:route.request().url(),ids:json.map(s=>s.id)});entered.resolve();
  await release.promise;trace.push({event:'catalog-released',token});await route.fulfill({response,headers:{...response.headers(),'x-iss188-gate':token}});
 };
 await page.route('**/api/snapshots',handler);
 return{entered:entered.promise,release:()=>release.resolve(),async settled(){await page.waitForFunction(t=>window.__completedCatalogGates.includes(t),token);},async close(){release.resolve();await page.unrouteAll({behavior:'wait'});}};
}
async function read(page){return page.evaluate(()=>({a:document.querySelector('#sel-a').value,b:document.querySelector('#sel-b').value,aOptions:Array.from(document.querySelector('#sel-a').options,o=>o.value),hash:location.hash,mutations:window.__handoffMutations||0}));}
async function watch(page){await page.evaluate(()=>{window.__handoffMutations=0;new MutationObserver(()=>window.__handoffMutations++).observe(document.querySelector('#sel-b'),{childList:true});window.__diag=[];});}
async function overview(page){const response=page.waitForResponse(r=>r.url().endsWith('/api/storage/summary'));await page.click('.nav-item[data-page="overview"]');await response;await page.waitForFunction(()=>!!document.querySelector('[data-test="storage-cta"]'));}
async function warm(page,base){
 await page.goto(base+'/#/overview');await page.waitForSelector('[data-test="storage-cta"]');await page.click('[data-test="storage-cta"]');
 await page.waitForFunction(()=>document.querySelector('#sel-b').value==='3'&&document.querySelector('#sel-a').value==='1');
 await page.selectOption('#sel-b','1');await page.selectOption('#sel-a','3');await page.waitForFunction(()=>document.querySelector('#sel-b').value==='1'&&document.querySelector('#sel-a').value==='3');
 await overview(page);await watch(page);
}
// Observe JSON consumption at the actual browser fetch boundary. The native
// response/data are returned unchanged; the next animation frame is a barrier
// after the production await continuations, including ignored stale responses.
async function installResponseBarrier(page){
 await page.addInitScript(()=>{
  window.__completedCatalogGates=[];
  const nativeFetch=window.fetch.bind(window);
  window.fetch=async(...args)=>{
   const response=await nativeFetch(...args),token=response.headers.get('x-iss188-gate');
   if(token){const read=response.json.bind(response);response.json=async(...values)=>{
    const data=await read(...values);requestAnimationFrame(()=>window.__completedCatalogGates.push(token));return data;
   };}
   return response;
  };
 });
}
async function beginWarmCta(page,fixture,trace){
 await warm(page,fixture.base);const gate=await catalogGate(page,trace);
 await page.click('[data-test="storage-cta"]');await gate.entered;
 await page.waitForFunction(()=>document.querySelector('#sel-b').value==='3');
 return gate;
}
async function verifyNewerManual(page,fixture,width,trace){
 const gate=await beginWarmCta(page,fixture,trace);
 try{
  // #2 is not the first catalog option (#4); rebuilding a select must not reset it.
  await page.selectOption('#sel-b','2');await page.selectOption('#sel-a','4');
  const selected=await read(page);trace.push({event:'newer-manual-before-release',...selected});
  gate.release();await gate.settled();const settled=await read(page);
  record(`${width}.newer-manual-keeps-range`,settled.a==='4'&&settled.b==='2',JSON.stringify(settled));
  record(`${width}.manual-baseline-same-dataset`,settled.aOptions.every(id=>['2','4'].includes(id)),JSON.stringify(settled));
  // Explicit equal endpoints remain invalid; a catalog may not silently repair them.
  await page.selectOption('#sel-a','2');
  record(`${width}.explicit-equal-point-preserved`,(await read(page)).a==='2'&&(await read(page)).b==='2');
 }finally{await gate.close();}
}
async function secondRootSummary(page){
 const handler=async route=>{
  const response=await route.fetch(),summary=await response.json();
  // Both are real current-round members. Choose the other root by legal member
  // order; no invented snapshot or mismatched plan is inserted into the fixture.
  summary.attribution.measured_members.reverse();await route.fulfill({response,json:summary});
 };
 await page.route('**/api/storage/summary',handler);return()=>page.unroute('**/api/storage/summary',handler);
}
async function verifySecondCta(page,fixture,width,trace){
 const gate=await beginWarmCta(page,fixture,trace);let restore;
 try{
  restore=await secondRootSummary(page);await overview(page);
  await page.waitForFunction(()=>document.querySelector('[data-test="storage-cta"]').dataset.b==='4');
  await page.click('[data-test="storage-cta"]');await page.waitForFunction(()=>document.querySelector('#sel-b').value==='4');
  trace.push({event:'second-cta-applied',...await read(page)});
  gate.release();await gate.settled();const settled=await read(page);
  record(`${width}.second-cta-wins-old-catalog`,settled.b==='4'&&settled.a==='2',JSON.stringify(settled));
 }finally{await gate.close();if(restore)await restore();}
}
async function verifyRouteReturn(page,fixture,width,trace){
 const gate=await beginWarmCta(page,fixture,trace);
 try{
  await page.click('.nav-item[data-page="agent"]');await page.waitForSelector('#page-agent:not(.hidden)');
  await page.click('.nav-item[data-page="changes"]');await page.waitForFunction(()=>document.querySelector('#sel-b').options.length===4);
  await page.selectOption('#sel-b','2');await page.selectOption('#sel-a','4');
  trace.push({event:'route-return-new-range',...await read(page)});
  gate.release();await gate.settled();const settled=await read(page);
  record(`${width}.route-return-old-response-ignored`,settled.a==='4'&&settled.b==='2',JSON.stringify(settled));
 }finally{await gate.close();}
}
async function verifyWaitingHandoff(page,fixture,width,trace){
 // A legal older directory contains only the first round (#1/#2). Keep its b1
 // selection before the CTA while the newer real directory is held in flight.
 let first=true;
 const prior=async route=>{if(!first){await route.continue();return;}first=false;
  const response=await route.fetch(),snaps=await response.json();
  await route.fulfill({response,json:snaps.filter(s=>s.id<=2)});
 };
 await page.route('**/api/snapshots',prior);
 await page.goto('about:blank');await page.goto(fixture.base+'/#/changes');await page.waitForFunction(()=>document.querySelector('#sel-b').options.length===2);
 await page.unrouteAll({behavior:'wait'});await page.selectOption('#sel-b','1');await overview(page);await watch(page);
 await page.clock.install();const gate=await catalogGate(page,trace);
 try{
  await page.click('[data-test="storage-cta"]');await gate.entered;
  await page.waitForFunction(()=>window.__diag.some(e=>e.kind==='tick'&&!e.hasB));
  trace.push({event:'old-handoff-waiting',...await read(page)});
  await page.click('.nav-item[data-page="agent"]');await page.click('.nav-item[data-page="changes"]');
  await page.waitForFunction(()=>document.querySelector('#sel-b').options.length===4&&document.querySelector('#sel-a').value==='3');
  // Drive precisely one original 100ms waiter deadline. This controls the
  // existing scheduler; it does not sleep/retry or delay product responses.
  await page.clock.runFor(100);
  gate.release();await gate.settled();const settled=await read(page);trace.push({event:'waiting-handoff-after-route-return',...settled});
  record(`${width}.leave-cancels-waiting-handoff`,settled.b==='1'&&settled.a==='3',JSON.stringify(settled));
  await page.selectOption('#sel-b','2');
  record(`${width}.route-return-dataset-switch-usable`,(await read(page)).a==='4'&&(await read(page)).b==='2'&&!await page.locator('#sel-b').isDisabled());
 }finally{await gate.close();}
}
async function verifyManualDuringWait(page,fixture,width,trace){
 let first=true;
 const prior=async route=>{if(!first){await route.continue();return;}first=false;
  const response=await route.fetch(),snaps=await response.json();
  await route.fulfill({response,json:snaps.filter(s=>s.id!==3)});
 };
 await page.route('**/api/snapshots',prior);await page.goto('about:blank');await page.goto(fixture.base+'/#/changes');
 await page.waitForFunction(()=>document.querySelector('#sel-b').options.length===3);
 await page.unrouteAll({behavior:'wait'});await page.selectOption('#sel-b','1');await overview(page);await watch(page);
 await page.clock.install();const gate=await catalogGate(page,trace);
 try{
  await page.click('[data-test="storage-cta"]');await gate.entered;
  await page.waitForFunction(()=>window.__diag.some(e=>e.kind==='tick'&&!e.hasB));
  await page.selectOption('#sel-b','2');await page.selectOption('#sel-a','4');
  trace.push({event:'manual-during-handoff-wait',...await read(page)});
  gate.release();await gate.settled();await page.clock.runFor(100);const settled=await read(page);
  record(`${width}.manual-before-handoff-ready-wins`,settled.b==='2'&&settled.a==='4',JSON.stringify(settled));
  record(`${width}.manual-before-ready-keeps-group`,settled.aOptions.every(id=>['2','4'].includes(id)),JSON.stringify(settled));
 }finally{await gate.close();}
}
async function verify(browser,fixture,width,height){
 const page=await browser.newPage({viewport:{width,height}}),trace=[];traces.push({viewport:`${width}x${height}`,events:trace});let gate;
 await installResponseBarrier(page);
 const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(/\/api\/(snapshots|diff)(\?|$)/.test(r.url()))trace.push({event:'request',url:r.url()});});
 try{
  await warm(page,fixture.base);trace.push({event:'prewarm-before-cta',...await read(page)});
  gate=await catalogGate(page,trace);await page.click('[data-test="storage-cta"]');await gate.entered;
  await page.waitForFunction(()=>document.querySelector('#sel-b').value==='3');const applied=await read(page);trace.push({event:'cta-applied',...applied});
  gate.release();await gate.settled();await page.waitForFunction(n=>window.__handoffMutations>n,applied.mutations);const settled=await read(page);trace.push({event:'catalog-settled',...settled});trace.push({event:'production-diagnostics',events:await page.evaluate(()=>window.__diag)});
  record(`${width}.late-catalog-keeps-cta`,applied.b==='3'&&settled.b==='3',JSON.stringify({applied,settled}));
  record(`${width}.cta-baseline-same-dataset`,settled.a==='1'&&settled.aOptions.every(id=>['1','3'].includes(id)),JSON.stringify(settled));
  await gate.close();gate=null;
  await verifyNewerManual(page,fixture,width,trace);
  await verifySecondCta(page,fixture,width,trace);
  await verifyRouteReturn(page,fixture,width,trace);
  await verifyWaitingHandoff(page,fixture,width,trace);
  await verifyManualDuringWait(page,fixture,width,trace);
  await page.screenshot({path:path.join(evidence,`${width}x${height}.png`)});
  record(`${width}.console-clean`,errors.length===0,errors.join(';'));
 }finally{if(gate)await gate.close();await page.close();}
}
async function main() {
 let fixture, browser;
 try {
  fixture = await helper();
  const identity = await get(fixture.port, '/api/diff?a=1&b=2');
  record('http.cross-dataset-rejected',
   identity.status === 409 && identity.json.detail.code === 'plan_identity_unsupported',
   JSON.stringify(identity));
  browser = await chromium.launch({headless:true});
  for (const [width, height] of [[980,800], [1220,900], [1440,1000]]) {
   await verify(browser, fixture, width, height);
  }
  const counts = JSON.parse(execFileSync(PY, ['-c',
   "import json,os,sqlite3; c=sqlite3.connect(os.environ['FATHOM_DB']); " +
   "print(json.dumps({'scan_runs':c.execute('SELECT count(*) FROM scan_runs').fetchone()[0]}))"
  ], {cwd:REPO, env:fixture.env, encoding:'utf8'}));
  record('fixture.no-scan-runs', counts.scan_runs === 0, JSON.stringify(counts));
 } catch (error) {
  record('flow-error', false, error.stack || error.message);
 } finally {
  if (browser) await browser.close();
  if (fixture) await fixture.close();
  fs.writeFileSync(path.join(evidence,'trace.json'), JSON.stringify(traces,null,2));
  fs.writeFileSync(path.join(evidence,'resources.json'), JSON.stringify(resources,null,2));
  const failed = checks.filter(check => !check.ok);
  const result = {
   source:execFileSync('git',['rev-parse','HEAD'],{cwd:REPO,encoding:'utf8'}).trim(),
   ok:failed.length === 0, passed:checks.length - failed.length,
   failed:failed.length, evidence, checks,
  };
  fs.writeFileSync(path.join(evidence,'result.json'), JSON.stringify(result,null,2));
  console.log(JSON.stringify(result));
 }
}
if (require.main === module) main().catch(error => {
 console.error(error);
 process.exitCode = 1;
});
