#!/usr/bin/env node
/* ISS-189：真实 Chromium 加载生产六页路由/共享解读组件；纯合成 HTTP，零模型调用。 */
'use strict';
const { createFixture, analysisRecord, SNAPSHOTS, json } = require('./verify_analysis_frontend.cjs');
const { chromium } = require('playwright');
const { once } = require('events');
const fs=require('fs'),os=require('os'),path=require('path');
const evidence=fs.mkdtempSync(path.join(os.tmpdir(),'fathom-iss189-'));
const checks=[];const record=(name,ok,detail='')=>{checks.push({name,ok:!!ok,detail});console.error(`${ok?'PASS':'FAIL'} ${name} ${detail}`);};
async function verifyLateRestore(browser) {
 for (const intent of ['selection','history']) {
  let release, entered;
  const enteredPromise=new Promise(r=>entered=r), releasePromise=new Promise(r=>release=r);
  const original={job_id:'original-delayed',status:'failed',reason_code:'runner_nonzero_exit',terminal:true,a_snapshot_id:1,b_snapshot_id:2,created_at:'2026-09-10T10:00:00',runtime:{id:'claude-code'},revoked:false,analysis_id:null};
  const manual={...original,job_id:'manual-latest',a_snapshot_id:2,b_snapshot_id:3};
  const fixture=createFixture({configRuntimeObject:true,async intercept(req,res,url){
   if(url.pathname==='/api/snapshots'){json(res,200,SNAPSHOTS);return true;}
   if(url.pathname==='/api/analysis/history'){json(res,200,{jobs:[original,manual],total:2,next_offset:null});return true;}
   if(url.pathname==='/api/analysis/jobs/original-delayed'){entered();await releasePromise;json(res,200,{job:original});return true;}
   return false;
  }});
  fixture.state.analysisEnabled=true;fixture.state.savedRuntime='claude-code';fixture.state.scenario='enabled';
  fixture.server.listen(0,'127.0.0.1');await once(fixture.server,'listening');
  const page=await browser.newPage();const base=`http://127.0.0.1:${fixture.server.address().port}`;
  try {
   // 先真实进入并留下可点击的历史，再重入构造保存任务恢复 GET 在途。
   await page.goto(base+'/#/agent');await page.waitForSelector('[data-agent-job="manual-latest"]');
   await page.click('.nav-item[data-page="settings"]');
   await page.evaluate(()=>sessionStorage.setItem('fathom-agent-job','original-delayed'));
   await page.click('.nav-item[data-page="agent"]');await enteredPromise;
   if(intent==='selection') {
    await page.selectOption('#agent-a','1');
    await page.waitForSelector('#agent-analysis-body [data-test="analysis-state-idle"]');
   } else {
    await page.click('[data-agent-job="manual-latest"]');
    await page.waitForSelector('#agent-analysis-body [data-test="analysis-state-failure"]');
   }
   release();await page.waitForTimeout(350);
   const view=await page.evaluate(()=>({a:document.querySelector('#agent-a').value,b:document.querySelector('#agent-b').value,range:document.querySelector('#agent-range-status').textContent,opened:document.querySelector('#agent-opened-task').textContent,body:document.querySelector('#agent-analysis-body').textContent}));
   if(intent==='selection') {
    record('agent.late-restore-selection-wins',view.a==='1'&&view.b==='3'&&view.range.includes('#1 → #3')&&view.opened===''&&view.body.includes('区间 #1 → #3 还没有')&&fixture.state.jobsPosted===0,JSON.stringify(view));
    await page.click('[data-agent-job="original-delayed"]');
    record('agent.late-restore-keeps-history-readable',(await page.locator('#agent-opened-task').textContent()).includes('original-delayed')&&(await page.locator('#agent-analysis-body').textContent()).includes('#1 → #2')&&fixture.state.jobsPosted===0);
   } else {
    record('agent.late-restore-explicit-history-wins',view.opened.includes('manual-latest')&&view.body.includes('#2 → #3')&&!view.opened.includes('original-delayed')&&fixture.state.jobsPosted===0,JSON.stringify(view));
    await page.click('#agent-history-refresh');await page.waitForTimeout(150);
    record('agent.late-restore-history-refresh-normal',await page.locator('#agent-history-body tr').count()===2&&(await page.locator('#agent-opened-task').textContent()).includes('manual-latest')&&fixture.state.jobsPosted===0);
   }
  } finally {release();await page.close();fixture.server.close();await once(fixture.server,'close');}
 }
}
async function verifyHistoricalRerunRestore(browser) {
 const original={job_id:'original-failed',status:'failed',reason_code:'runner_nonzero_exit',terminal:true,a_snapshot_id:1,b_snapshot_id:2,created_at:'2026-09-10T10:00:00',runtime:{id:'claude-code'},revoked:false,analysis_id:null};
 const fixture=createFixture({configRuntimeObject:true,intercept(req,res,url,body,state){
  if(url.pathname==='/api/snapshots'){json(res,200,SNAPSHOTS);return true;}
  if(url.pathname==='/api/analysis/history'){const jobs=[...state.jobs.values()].map(j=>({...j,terminal:!['starting','running','cancelling'].includes(j.status)}));jobs.push(original);json(res,200,{jobs,total:jobs.length,next_offset:null});return true;}
  if(url.pathname==='/api/analysis/jobs/original-failed'){json(res,200,{job:original});return true;}
  return false;
 }});
 fixture.state.analysisEnabled=true;fixture.state.savedRuntime='claude-code';fixture.state.scenario='enabled';fixture.state.holdRunning=true;
 fixture.server.listen(0,'127.0.0.1');await once(fixture.server,'listening');const page=await browser.newPage();
 try {
  await page.goto(`http://127.0.0.1:${fixture.server.address().port}/#/agent`);
  await page.waitForSelector('[data-agent-job="original-failed"]');await page.click('[data-agent-job="original-failed"]');
  await page.click('#agent-analysis-body [data-test="analysis-retry-btn"]');await page.waitForSelector('#agent-analysis-body [data-test="analysis-state-preview"]');await page.click('#agent-analysis-body [data-test="analysis-confirm-btn"]');
  await page.waitForSelector('#agent-analysis-body [data-test="analysis-state-running"]');
  const newJob=[...fixture.state.jobs.values()][0];
  const before=await page.evaluate(()=>({opened:document.querySelector('#agent-opened-task').textContent,saved:sessionStorage.getItem('fathom-agent-job')}));
  record('agent.historical-rerun-tracks-new-attempt',before.opened.includes(newJob.job_id)&&before.saved===newJob.job_id&&!before.opened.includes('original-failed'),JSON.stringify(before));
  await page.reload();await page.waitForTimeout(400);
  const cancel=page.locator('#agent-analysis-body [data-test="analysis-cancel-btn"]');
  record('agent.historical-rerun-reload-active-not-old',await cancel.isVisible()&&(await page.locator('#agent-opened-task').textContent()).includes(newJob.job_id)&&fixture.state.jobsPosted===1);
  if(await cancel.isVisible()){await cancel.click();await page.waitForSelector('#agent-analysis-body [data-test="analysis-state-failure"]');}
  record('agent.historical-rerun-restored-cancellable',newJob.status==='cancelled'&&original.status==='failed'&&fixture.state.jobsPosted===1);
  await page.click('#agent-history-refresh');await page.waitForSelector('[data-agent-job="original-failed"]');await page.click('[data-agent-job="original-failed"]');
  record('agent.historical-rerun-original-task-preserved',(await page.locator('#agent-opened-task').textContent()).includes('original-failed')&&(await page.locator('#agent-analysis-body').textContent()).includes('引擎命令行异常退出')&&fixture.state.jobsPosted===1);
 } finally {await page.close();fixture.server.close();await once(fixture.server,'close');}
}
async function verifyEvidenceControls(browser) {
 const fixture=createFixture({configRuntimeObject:true,intercept(req,res,url){
  if(url.pathname==='/api/analysis/history'){json(res,200,{jobs:[],total:0,next_offset:null});return true;}
  return false;
 }});
 fixture.state.analysisEnabled=true;fixture.state.savedRuntime='claude-code';fixture.state.scenario='expired';
 fixture.server.listen(0,'127.0.0.1');await once(fixture.server,'listening');const page=await browser.newPage();
 try {
  await page.goto(`http://127.0.0.1:${fixture.server.address().port}/#/agent`);
  await page.waitForSelector('#agent-analysis-body [data-test="analysis-state-expired"]');
  const buttons=page.locator('#agent-analysis-body [data-test="analysis-evidence-btn"]');
  const measure=()=>buttons.evaluateAll(nodes=>nodes.map(n=>{const s=getComputedStyle(n),r=n.getBoundingClientRect();return {text:n.textContent,opacity:s.opacity,visibility:s.visibility,width:r.width,height:r.height,fits:n.scrollWidth<=n.clientWidth};}));
  const close=page.locator('#agent-analysis-body [data-test="analysis-evidence-close"]');
  const visibleClose=()=>close.evaluate(n=>getComputedStyle(n).opacity==='1'&&getComputedStyle(n).visibility==='visible'&&n.getBoundingClientRect().width>=24);
  const closeChecks=[];
  for(const [width,height] of [[980,640],[1220,820],[1440,900]]) {
   await page.setViewportSize({width,height});const before=await measure();await buttons.first().hover();const after=await measure();
   const usable=list=>list.length>0&&list.every(b=>b.opacity==='1'&&b.visibility==='visible'&&b.width>=44&&b.height>=24&&b.fits);
   record(`agent.evidence-controls-visible-${width}`,usable(before)&&usable(after),JSON.stringify({before,after}));
   await buttons.first().click();await page.waitForSelector('#agent-analysis-body [data-test="analysis-evidence-card"]');
   const beforeClose=await visibleClose();await close.hover();closeChecks.push({width,before:beforeClose,after:await visibleClose()});
   await page.screenshot({path:path.join(evidence,`agent-evidence-${width}.png`),fullPage:true});
   await close.click();await page.waitForSelector('#agent-analysis-body [data-test="analysis-evidence-card"]',{state:'detached'});
  }
  await buttons.first().click();await page.waitForSelector('#agent-analysis-body [data-test="analysis-evidence-card"]');
  record('agent.evidence-close-visible',closeChecks.length===3&&closeChecks.every(c=>c.before&&c.after),JSON.stringify(closeChecks));
  record('agent.evidence-no-unavailable-locate',await page.locator('#agent-analysis-body [data-test="analysis-evidence-locate"]').count()===0&&fixture.state.jobsPosted===0);
  await close.click();await page.waitForSelector('#agent-analysis-body [data-test="analysis-evidence-card"]',{state:'detached'});
  await page.click('.nav-item[data-page="changes"]');await page.waitForSelector('#analysis-body [data-test="analysis-state-expired"]');
  await page.locator('#analysis-body [data-test="analysis-evidence-btn"]').first().click();
  await page.click('#analysis-body [data-test="analysis-evidence-locate"]');await page.waitForSelector('#changes-body tr.analysis-locate-flash');
  record('agent.shared-changes-locate-still-works',await page.locator('#changes-body tr.analysis-locate-flash').getAttribute('data-path')==='/fixture/root/Library/Caches'&&fixture.state.jobsPosted===0);
 }finally{await page.close();fixture.server.close();await once(fixture.server,'close');}
}
async function main(){
 let snaps=[], historyFailure=false;
 const original=analysisRecord(1,2,{expired:true,expiredReason:'snapshot_pruned'});original.id=88;original.job_id='old-result';
 const saved=new Map([[88,original]]);
 const extra=[{job_id:'old-result',a_snapshot_id:1,b_snapshot_id:2,status:'succeeded',terminal:true,analysis_id:88,created_at:'2026-09-14T12:00:00',runtime:{id:'claude-code',version:'2.1'},revoked:false},
 {job_id:'old-revoked',a_snapshot_id:90,b_snapshot_id:91,status:'succeeded',terminal:true,analysis_id:null,created_at:'2026-09-12T12:00:00',runtime:{id:'codex-cli'},revoked:true,revoked_at:'2026-09-13T12:00:00'}];
 const fixture=createFixture({configRuntimeObject:true,intercept(req,res,url,body,state){
  if(url.pathname==='/api/snapshots'){json(res,200,snaps);return true;}
  if(url.pathname==='/api/analysis/history'){
   if(historyFailure){json(res,500,{detail:'合成历史故障'});return true;}
   const all=[...state.jobs.values(),...extra].map(j=>{
    const out={...j,terminal:!['starting','running','cancelling'].includes(j.status)};
    const r=saved.get(j.analysis_id);
    if(r)out.analysis={id:r.id,expired:r.expired,a_created_at:r.a.created_at,b_created_at:r.b.created_at};
    else if(j.analysis_id===77)out.analysis={id:77,expired:false,a_created_at:SNAPSHOTS.find(s=>s.id===j.a_snapshot_id)?.created_at,b_created_at:SNAPSHOTS.find(s=>s.id===j.b_snapshot_id)?.created_at};else out.analysis=null;
    return out;
   }).sort((a,b)=>Number(a.terminal)-Number(b.terminal)||b.created_at.localeCompare(a.created_at)||b.job_id.localeCompare(a.job_id));
   const limit=Number(url.searchParams.get('limit')||20),offset=Number(url.searchParams.get('offset')||0);const jobs=all.slice(offset,offset+limit),more=offset+jobs.length<all.length;
   json(res,200,{jobs,total:all.length,has_more:more,next_offset:more?offset+jobs.length:null});return true;
  }
  const m=url.pathname.match(/^\/api\/analyses\/(\d+)$/);
  if(m&&req.method==='GET'){
   let r=saved.get(Number(m[1]));
   if(Number(m[1])===77){const j=[...state.jobs.values()].find(j=>j.analysis_id===77);if(j)r=analysisRecord(j.a_snapshot_id,j.b_snapshot_id);}
   json(res,r?200:404,r?{analysis:r}:{detail:'原记录已撤销'});return true;
  }
  if(m&&req.method==='DELETE'&&saved.has(Number(m[1]))){saved.delete(Number(m[1]));const j=extra.find(j=>j.analysis_id===Number(m[1]));j.analysis_id=null;j.revoked=true;j.revoked_at='2026-09-15T12:00:00';json(res,200,{ok:true,job_id:j.job_id});return true;}
  return false;
 }});
 fixture.server.listen(0,'127.0.0.1');await once(fixture.server,'listening');const base=`http://127.0.0.1:${fixture.server.address().port}`;
 const browser=await chromium.launch({headless:true});const errors=[];let page;
 const visible=async sel=>page.locator(sel).isVisible();
 const wait=async sel=>page.waitForSelector(sel,{timeout:10000,state:sel.includes(' option')?'attached':'visible'});
 const choose=async(id,value)=>{await page.selectOption('#'+id,String(value));};
 try{
  page=await browser.newPage({viewport:{width:980,height:640}});page.on('pageerror',e=>errors.push(String(e)));
  await page.goto(base+'/#/agent');await wait('[data-test="agent-no-range"]');
  record('agent.sidebar-six-pages',await page.locator('.nav-item').count()===6&&await visible('.nav-item[data-page="agent"]'));
  record('agent.direct-route',await page.locator('#page-title').textContent()==='Agent');
  record('agent.no-data-disabled-reachable',(await page.locator('#agent-range-status').textContent()).includes('尚无快照')&&(await page.locator('#agent-engine-summary').textContent()).includes('未启用'));
  await wait('[data-agent-job="old-result"]');record('agent.history-visible-without-snapshots',await page.locator('#agent-history-body tr').count()===2);
  await page.reload();await wait('[data-agent-job="old-result"]');record('agent.direct-reload',await visible('#page-agent')&&await page.locator('#page-title').textContent()==='Agent');
  await page.click('[data-agent-job="old-result"]');await wait('#agent-analysis-body [data-test="analysis-state-expired"]');
  record('agent.disabled-expired-original-readable',(await page.locator('#agent-analysis-body [data-test="analysis-meta"]').textContent()).includes('#1 → #2'));
  await page.locator('#agent-analysis-body [data-test="analysis-evidence-btn"]').first().click();await wait('#agent-analysis-body [data-test="analysis-evidence-card"]');
  record('agent.expired-saved-evidence',await visible('#agent-analysis-body [data-test="analysis-evidence-path"]'));
  record('agent.result-html-escaped',await page.evaluate(()=>!window.__pwned)&&await page.locator('#agent-analysis-body .analysis-summary img').count()===0);
  await page.click('#agent-analysis-body [data-test="analysis-rerun-btn"]');await wait('#agent-analysis-body [data-test="analysis-preview-error"]');
  record('agent.pruned-rerun-refuses-send',(await page.locator('#agent-analysis-body').textContent()).includes('原区间快照已不可用')&&fixture.state.jobsPosted===0);
  await page.click('[data-agent-job="old-result"]');await wait('#agent-analysis-body [data-test="analysis-state-expired"]');
  await page.click('#agent-analysis-body [data-test="analysis-revoke-expired-btn"]');await page.click('#agent-analysis-body [data-test="analysis-revoke-no"]');
  record('agent.revoke-cancel-preserves-record',saved.has(88));
  await page.click('#agent-analysis-body [data-test="analysis-revoke-expired-btn"]');await page.click('#agent-analysis-body [data-test="analysis-revoke-yes"]');await page.waitForTimeout(200);
  record('agent.revoke-deletes-body-retains-lifecycle',!saved.has(88)&&(await page.locator('#agent-history-body').textContent()).includes('已撤销'));
  await page.click('[data-agent-job="old-revoked"]');await wait('#agent-analysis-body [data-test="analysis-state-failure"]');record('agent.revoked-honest-no-original',(await page.locator('#agent-analysis-body').textContent()).includes('正文与事实包不再保留'));
  await page.click('#agent-settings');await wait('#settings-analysis-extra');record('agent.configuration-stays-settings',await visible('#page-settings')&&await visible('#settings-analysis-extra'));
  snaps=[...SNAPSHOTS];fixture.state.analysisEnabled=true;fixture.state.savedRuntime='claude-code';fixture.state.scenario='enabled';
  await page.click('.nav-item[data-page="agent"]');await wait('#agent-b option');await choose('agent-a',1);await wait('#agent-analysis-body [data-test="analysis-state-idle"]');
  record('agent.engine-object-summary', (await page.locator('#agent-engine-summary').textContent()).includes('claude-code v2.1.237') && !(await page.locator('#agent-engine-summary').textContent()).includes('[object Object]') && !(await page.locator('#agent-engine-summary').textContent()).includes('/fixture/bin'));
  record('agent.valid-range-ready',(await page.locator('#agent-range-status').textContent()).includes('#1 → #3'));
  await page.reload();await wait('#agent-analysis-body [data-test="analysis-state-idle"]');record('agent.selected-range-restored',await page.inputValue('#agent-a')==='1'&&await page.inputValue('#agent-b')==='3');
  await page.click('#agent-analysis-body [data-test="analysis-preview-btn"]');await wait('#agent-analysis-body [data-test="analysis-state-preview"]');
  record('agent.shared-send-preview',(await page.locator('#agent-analysis-body [data-test="analysis-preview-range"]').textContent()).includes('#1')&&await visible('#agent-analysis-body [data-test="analysis-preview-prompt-details"]'));
  await page.click('#agent-analysis-body [data-test="analysis-preview-cancel"]');record('agent.preview-cancel-zero-send',fixture.state.jobsPosted===0);
  fixture.state.slowPreviewMs=500;await page.click('#agent-analysis-body [data-test="analysis-preview-btn"]');await choose('agent-b',2);await page.waitForTimeout(750);
  record('agent.selection-discards-late-preview',!await visible('#agent-analysis-body [data-test="analysis-state-preview"]')&&(await page.locator('#agent-range-status').textContent()).includes('#1 → #2'));
  fixture.state.slowPreviewMs=0;await choose('agent-b',3);await wait('#agent-analysis-body [data-test="analysis-state-idle"]');fixture.state.holdRunning=true;
  await page.click('#agent-analysis-body [data-test="analysis-preview-btn"]');await wait('#agent-analysis-body [data-test="analysis-confirm-btn"]');await page.click('#agent-analysis-body [data-test="analysis-confirm-btn"]');await wait('#agent-analysis-body [data-test="analysis-state-running"]');
  record('agent.confirm-dispatches-once',fixture.state.jobsPosted===1&&fixture.state.jobs.size===1);
  await page.click('.nav-item[data-page="settings"]');await page.click('.nav-item[data-page="agent"]');await wait('#agent-analysis-body [data-test="analysis-state-running"]');record('agent.leave-reentry-does-not-redispatch',fixture.state.jobsPosted===1);
  await page.reload();await wait('#agent-analysis-body [data-test="analysis-state-running"]');record('agent.reload-finds-global-inflight',fixture.state.jobsPosted===1);
  await page.click('#agent-analysis-body [data-test="analysis-cancel-btn"]');await wait('#agent-analysis-body [data-test="analysis-state-failure"]');record('agent.cancel-shared-lifecycle',(await page.locator('#agent-analysis-body').textContent()).includes('分析已取消'));
  await page.click('#agent-analysis-body [data-test="analysis-retry-btn"]');await wait('#agent-analysis-body [data-test="analysis-confirm-btn"]');await page.click('#agent-analysis-body [data-test="analysis-confirm-btn"]');await wait('#agent-analysis-body [data-test="analysis-state-running"]');
  record('agent.terminal-rerun-new-key',fixture.state.jobs.size===2&&new Set(fixture.state.postedKeys).size===2);
  fixture.state.holdRunning=false;await wait('#agent-analysis-body [data-test="analysis-state-done"]');record('agent.success-result-rendered',await visible('#agent-analysis-body [data-test="analysis-summary"]'));
  const foreign={job_id:'foreign-job',status:'running',a_snapshot_id:1,b_snapshot_id:2,created_at:'2026-09-10T10:00:00',runtime:{id:'claude-code'},terminal:false};fixture.state.jobs.set('foreign-key',foreign);fixture.state.holdRunning=true;
  await page.click('#agent-history-refresh');await wait('[data-agent-job="foreign-job"]');record('agent.cross-interval-active-first',await page.locator('#agent-history-body [data-agent-job]').first().getAttribute('data-agent-job')==='foreign-job');
  await page.click('[data-agent-job="foreign-job"]');await wait('#agent-analysis-body [data-test="analysis-state-running"]');record('agent.foreign-task-original-range',(await page.locator('#agent-opened-task').textContent()).includes('#1 → #2')&&(await page.locator('#agent-analysis-body').textContent()).includes('#1 → #2'));
  await choose('agent-a',1);await page.waitForTimeout(2200);record('agent.old-task-poll-does-not-overwrite-new-range',!(await page.locator('#agent-opened-task').textContent()).includes('foreign-job')&&!(await page.locator('#agent-analysis-body').textContent()).includes('foreign-'));
  snaps=[...SNAPSHOTS,{...SNAPSHOTS[0],id:4,plan_id:'p',created_at:'2026-09-15T10:00:00'},{...SNAPSHOTS[0],id:5,plan_id:'p',created_at:'2026-09-16T10:00:00'},{...SNAPSHOTS[0],id:6,root:'/fixture/other',created_at:'2026-09-17T10:00:00'}];
  await page.click('#agent-refresh');await wait('#agent-b option[value="5"]');await choose('agent-b',5);
  record('agent.plan-dataset-converges',await page.locator('#agent-a option').count()===1&&await page.inputValue('#agent-a')==='4');
  record('agent.plan-ai-refusal',await visible('[data-test="agent-no-range"]')&&(await page.locator('#agent-analysis-body').textContent()).includes('新范围身份暂不支持'));
  await choose('agent-b',6);record('agent.single-snapshot-has-dataset-exit',await page.locator('#agent-a').isDisabled()&&!await page.locator('#agent-b').isDisabled()&&(await page.locator('#agent-range-status').textContent()).includes('不足两个'));
  historyFailure=true;await page.click('#agent-history-refresh');await page.waitForTimeout(150);record('agent.history-failure-visible',(await page.locator('#agent-history-error').textContent()).includes('合成历史故障'));
  historyFailure=false;await page.click('#agent-history-refresh');await page.waitForTimeout(150);record('agent.history-failure-retry',await page.locator('#agent-history-error').textContent()==='');
  for(let i=0;i<25;i++)extra.push({job_id:'synthetic-'+i,a_snapshot_id:100+i,b_snapshot_id:200+i,status:'failed',reason_code:'runner_nonzero_exit',terminal:true,created_at:'2026-09-01T12:00:00',runtime:{id:'codex-cli'}});
  await page.click('#agent-history-refresh');await wait('#agent-history-more:visible');record('agent.history-page-bounded',await page.locator('#agent-history-body tr').count()===20, 'rows='+await page.locator('#agent-history-body tr').count()+' count='+await page.locator('#agent-history-count').textContent());
  await page.click('#agent-history-more');await page.waitForTimeout(200);record('agent.history-more-complete',await page.locator('#agent-history-body tr').count()===fixture.state.jobs.size+extra.length&&!await visible('#agent-history-more'), 'rows='+await page.locator('#agent-history-body tr').count()+' expected='+(fixture.state.jobs.size+extra.length)+' hidden='+await page.locator('#agent-history-more').getAttribute('hidden'));
  for(const [width,height] of [[980,640],[1220,820],[1440,900]]){await page.setViewportSize({width,height});await page.evaluate(()=>{document.querySelector('.page-container').scrollTop=0;});await page.screenshot({path:path.join(evidence,`agent-${width}.png`),fullPage:true});record(`agent.viewport-${width}`,await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth && [...document.querySelectorAll('[data-agent-job]')].every(btn=>getComputedStyle(btn).opacity==='1' && btn.getBoundingClientRect().height<45)));}
  await page.click('.nav-item[data-page="changes"]');await wait('#analysis-panel');record('agent.changes-local-entry-retained',await visible('#analysis-panel'));
  await verifyLateRestore(browser);
  await verifyHistoricalRerunRestore(browser);
  await verifyEvidenceControls(browser);
  record('agent.no-unhandled-page-errors',errors.length===0,errors.join(';'));
 }finally{await browser.close();fixture.server.close();await once(fixture.server,'close');}
 const failed=checks.filter(c=>!c.ok).length;console.log(JSON.stringify({ok:!failed,passed:checks.length-failed,failed,evidence,checks},null,2));if(failed)process.exitCode=1;
}
main().catch(e=>{console.error(e);console.log(JSON.stringify({ok:false,passed:checks.filter(c=>c.ok).length,failed:checks.filter(c=>!c.ok).length+1,evidence,checks,error:String(e)},null,2));process.exitCode=1});
