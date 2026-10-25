#!/usr/bin/env node
/* ISS-189：真实 Chromium 加载生产六页路由/共享解读组件；纯合成 HTTP，零模型调用。 */
'use strict';
const { createFixture, analysisRecord, SNAPSHOTS, json } = require('./verify_analysis_frontend.cjs');
const { chromium } = require('playwright');
const { once } = require('events');
const fs=require('fs'),os=require('os'),path=require('path');
const evidence=fs.mkdtempSync(path.join(os.tmpdir(),'fathom-iss189-'));
const checks=[];const record=(name,ok,detail='')=>{checks.push({name,ok:!!ok,detail});console.error(`${ok?'PASS':'FAIL'} ${name} ${detail}`);};
// ISS-201：本切片新增的失败原因码必须在生产组件里有中文说明，
// 否则用户会直接看到机器码。断言直接读生产源文件，不复制文案。
function verifyNewReasonTexts(){
 const src=fs.readFileSync(path.join(__dirname,'..','frontend','modules','components','analysis-panel.js'),'utf8');
 const block=(src.match(/const ANALYSIS_JOB_REASON = \{([\s\S]*?)\n\};/)||[])[1]||'';
 const codes=['profile_required_flags_missing','profile_protocol_mismatch','profile_payload_channel_violation','protocol_tool_rejected','protocol_terminal_error','protocol_terminal_mismatch','protocol_ambiguous_terminal','protocol_ambiguous_final','protocol_unknown_permission_request','protocol_missing_terminal','protocol_missing_final','protocol_duplicate_conflict','protocol_unsupported_fragment','protocol_single_json_required','protocol_no_parsable_events'];
 const missing=codes.filter(c=>!new RegExp(c+'\\s*:\\s*"[^"]{4,}"').test(block));
 record('agent.new-reason-codes-have-user-text',missing.length===0,'missing='+JSON.stringify(missing));
 const forbidden=/(可回收|回收空间|垃圾|删除|清理|自动重试|会自动重试)/;
 const offenders=codes.filter(c=>{const m=block.match(new RegExp(c+'\\s*:\\s*"([^"]*)"'));return m&&forbidden.test(m[1]);});
 record('agent.new-reason-codes-avoid-misleading-words',offenders.length===0,'offenders='+JSON.stringify(offenders));
}
// ISS-203 切片 A：引擎状态相关的 reason_code 必须有普通中文文案与下一动作，
// 且机器码不得出现在普通面。断言读生产源文件，不复制文案。
const ISS203_RUNTIME_CODES=['runtime_not_configured','runtime_unknown','runtime_unsupported','runtime_unavailable','runtime_identity_incomplete','runtime_identity_changed','runtime_invocation_mismatch','upgrade_in_progress'];
// 误导词表：不得暗示「可回收/自动重试」等结论（与既有禁用词表同口径）。
const ISS203_FORBIDDEN=/(可回收|回收空间|垃圾|删除|清理|自动重试|会自动重试)/;
function verifyRuntimeStatusCopy(){
 const src=fs.readFileSync(path.join(__dirname,'..','frontend','modules','runtime-status.js'),'utf8');
 const missingText=ISS203_RUNTIME_CODES.filter(c=>!new RegExp(c+'\\s*:\\s*\\{[\\s\\S]{0,200}?text\\s*:\\s*"[^"]{4,}"').test(src));
 record('agent.runtime-codes-have-user-text',missingText.length===0,'missing='+JSON.stringify(missingText));
 const missingAction=ISS203_RUNTIME_CODES.filter(c=>{
  const m=src.match(new RegExp(c+'\\s*:\\s*\\{([\\s\\S]*?)\\n\\s*\\},'));
  return !m||!/action\s*:\s*"[^"]{2,}"/.test(m[1]);
 });
 record('agent.runtime-codes-have-next-action',missingAction.length===0,'missing='+JSON.stringify(missingAction));
 // 普通面文案不得含机器码自身或误导词（可回收/自动重试等）。
 const offenders=ISS203_RUNTIME_CODES.filter(c=>{
  const m=src.match(new RegExp(c+'\\s*:\\s*\\{([\\s\\S]*?)\\n\\s*\\},'));
  if(!m)return false;
  return /text\s*:\s*"[^"]*(runner_|profile_|protocol_)/.test(m[1])||ISS203_FORBIDDEN.test(m[1]);
 });
 record('agent.runtime-codes-avoid-machine-codes',offenders.length===0,'offenders='+JSON.stringify(offenders));
 // 普通面文案不得含本机路径形态（路径属于技术详情）。
 const pathOffenders=ISS203_RUNTIME_CODES.filter(c=>{
  const m=src.match(new RegExp(c+'\\s*:\\s*\\{([\\s\\S]*?)\\n\\s*\\},'));
  return m&&/text\s*:\s*"[^"]*(\/[a-z]+\/|[A-Za-z]:\\\\)/.test(m[1]);
 });
 record('agent.runtime-codes-no-paths-in-plain-text',pathOffenders.length===0,'offenders='+JSON.stringify(pathOffenders));
}
// ISS-203：普通面分层——技术详情默认折叠、机器码不裸出。
async function verifyLayeredStatus(browser){
 const fixture=createFixture({configRuntimeObject:true,intercept(req,res,url){
  if(url.pathname==='/api/snapshots'){json(res,200,SNAPSHOTS);return true;}
  if(url.pathname==='/api/analysis/history'){
   json(res,200,{jobs:[{job_id:'machine-code-job',a_snapshot_id:1,b_snapshot_id:2,status:'failed',reason_code:'runner_nonzero_exit',terminal:true,created_at:'2026-09-10T10:00:00',runtime:{id:'claude-code',version:'2.1.237'},revoked:false,analysis_id:null}],total:1,next_offset:null});return true;
  }
  return false;
 }});
 fixture.state.analysisEnabled=true;fixture.state.savedRuntime='claude-code';fixture.state.scenario='enabled';
 fixture.server.listen(0,'127.0.0.1');await once(fixture.server,'listening');const page=await browser.newPage({viewport:{width:1220,height:820}});
 try {
  await page.goto(`http://127.0.0.1:${fixture.server.address().port}/#/agent`);
  await page.waitForSelector('[data-agent-job="machine-code-job"]');
  // 普通面（历史表可见文本）不得出现机器码。
  const plainText=await page.locator('#agent-history-body').innerText();
  record('agent.reason-code-not-in-plain-surface',!plainText.includes('runner_nonzero_exit'),plainText.replace(/\s+/g,' ').slice(0,90));
  // 机器码必须仍可在技术详情里查到（折叠内），不得被丢弃。
  const details=page.locator('#agent-history-body details.cfg-details');
  record('agent.tech-detail-container-exists',await details.count()>=1,'details='+await details.count());
  record('agent.tech-detail-collapsed-by-default',await details.count()>=1&&await details.first().evaluate(n=>!n.hasAttribute('open')));
  // 折叠内容不在 innerText 里（collapsed），用 textContent 读折叠区文字。
  const detailText=await details.first().textContent().catch(()=>'');
  record('agent.machine-code-kept-in-tech-detail',detailText.includes('runner_nonzero_exit'),detailText.replace(/\s+/g,' ').slice(0,90));
  // 普通面须给短状态 + 下一动作；技术细节折叠默认收起。
  const summary=await page.locator('#agent-engine-summary').innerText();
  record('agent.engine-summary-has-short-status',summary.trim().length>0&&summary.trim().length<=60,JSON.stringify(summary.trim()));
  record('agent.engine-summary-has-next-action',summary.includes('下一动作'),JSON.stringify(summary.trim()));
  const engineTech=page.locator('#agent-engine-summary [data-test="agent-tech-details"]');
  record('agent.engine-tech-detail-collapsed-by-default',await engineTech.count()===1&&await engineTech.evaluate(n=>!n.hasAttribute('open')));
  const plainDetail=await page.locator('#agent-runtime-status').innerText();
  record('agent.plain-surface-no-machine-code',!plainDetail.includes('runner_nonzero_exit')&&!plainDetail.includes('verified_version'),JSON.stringify(plainDetail.trim().slice(0,90)));
 } finally {await page.close();fixture.server.close();await once(fixture.server,'close');}
}

// ISS-203：检测结果分三层——普通面短状态+下一动作；原因码/路径进折叠区。
async function verifyDetectLayering(browser){
 const fixture=createFixture({configRuntimeObject:true,intercept(req,res,url){
  if(url.pathname==='/api/snapshots'){json(res,200,SNAPSHOTS);return true;}
  if(url.pathname==='/api/analysis/history'){json(res,200,{jobs:[],total:0,next_offset:null});return true;}
  return false;
 }});
 fixture.state.analysisEnabled=true;fixture.state.savedRuntime='claude-code';fixture.state.scenario='enabled';
 fixture.server.listen(0,'127.0.0.1');await once(fixture.server,'listening');const page=await browser.newPage({viewport:{width:1220,height:820}});
 try {
  await page.goto(`http://127.0.0.1:${fixture.server.address().port}/#/agent`);
  await page.waitForSelector('[data-test="agent-engine-short-status"]');
  await page.click('#agent-detect');
  await page.waitForSelector('[data-test="agent-detect-short-status"]');
  const plain=await page.locator('#agent-runtime-status').innerText();
  record('agent.detect-plain-has-short-status',plain.includes('可用'),JSON.stringify(plain.replace(/\s+/g,' ').trim().slice(0,60)));
  record('agent.detect-plain-no-path',!plain.includes('/fixture/bin'),JSON.stringify(plain.replace(/\s+/g,' ').trim().slice(0,60)));
  const tech=page.locator('#agent-runtime-status [data-test="agent-tech-details"]');
  record('agent.detect-tech-detail-collapsed-by-default',await tech.count()===1&&await tech.evaluate(n=>!n.hasAttribute('open')));
  const techText=await tech.textContent();
  record('agent.detect-tech-keeps-reason-code',techText.includes('verified_version')&&techText.includes('/fixture/bin'),techText.replace(/\s+/g,' ').trim().slice(0,90));
 } finally {await page.close();fixture.server.close();await once(fixture.server,'close');}
}
// ISS-204：真实生产组件消费身份等级及调用配置失败，普通面保持简短。
async function verifyRuntimeIdentityAndMismatch(browser){
 let tier='probed';
 const failedJob={job_id:'invocation-mismatch',a_snapshot_id:1,b_snapshot_id:2,
  status:'failed',reason_code:'runtime_invocation_mismatch',terminal:true,
  created_at:'2026-10-25T10:00:00',runtime:{id:'claude-code'},revoked:false,analysis_id:null};
 const fixture=createFixture({configRuntimeObject:true,intercept(req,res,url){
  if(url.pathname==='/api/snapshots'){json(res,200,SNAPSHOTS);return true;}
  if(url.pathname==='/api/analysis/history'){json(res,200,{jobs:[failedJob],total:1,next_offset:null});return true;}
  if(url.pathname==='/api/analysis/jobs/invocation-mismatch'){json(res,200,{job:failedJob});return true;}
  if(url.pathname==='/api/analysis/runtimes/detect'){
   json(res,200,{runtimes:{'claude-code':{id:'claude-code',availability:'ready',
    reason_code:'compatible_capabilities',executable:'/fixture/bin/claude',version:'2.1.237',
    capability_snapshot:{identity:{assurance:tier,identity_complete:tier==='complete',
     execution_chain_resolved:tier!=='unresolved'}}}}});return true;
  }
  return false;
 }});
 fixture.state.analysisEnabled=true;fixture.state.savedRuntime='claude-code';fixture.state.scenario='enabled';
 fixture.server.listen(0,'127.0.0.1');await once(fixture.server,'listening');
 const page=await browser.newPage({viewport:{width:1220,height:820}});
 try {
  await page.goto(`http://127.0.0.1:${fixture.server.address().port}/#/agent`);
  await page.waitForSelector('[data-agent-job="invocation-mismatch"]');
  await page.click('[data-agent-job="invocation-mismatch"]');
  await page.waitForSelector('#agent-analysis-body [data-test="analysis-state-failure"]');
  const panel=page.locator('#agent-analysis-body');
  const plain=await panel.innerText(), tech=panel.locator('details.cfg-details');
  record('agent.invocation-mismatch-user-copy',plain.includes('调用配置不一致')&&plain.includes('重新生成预览')&&!plain.includes('runtime_invocation_mismatch'),plain.replace(/\s+/g,' ').slice(0,120));
  record('agent.invocation-mismatch-detail-retained',await tech.count()===1&&!(await tech.getAttribute('open'))&&(await tech.textContent()).includes('runtime_invocation_mismatch'));
  record('agent.invocation-mismatch-read-zero-send',fixture.state.jobsPosted===0);
  for(tier of ['probed','complete','unresolved']){
   const response=page.waitForResponse(r=>r.url().includes('/api/analysis/runtimes/detect'));
   await page.click('#agent-detect');await response;
   await page.waitForFunction(()=>!document.querySelector('#agent-detect').disabled);
   const label={probed:'身份已探测',complete:'身份链完整',unresolved:'身份待确认'}[tier];
   const detect=page.locator('#agent-runtime-status'), visible=await detect.innerText();
   record('agent.identity-'+tier+'-compact',visible.includes(label)&&visible.length<100&&!/PROBED|COMPLETE|UNRESOLVED|\/fixture\//.test(visible),visible.replace(/\s+/g,' '));
   const details=detect.locator('details.cfg-details'), detail=await details.textContent();
   const sendable=tier==='unresolved'?'暂不可发起解读':'可发起解读';
   record('agent.identity-'+tier+'-details',!(await details.getAttribute('open'))&&detail.includes(tier.toUpperCase())&&detail.includes(sendable),detail.replace(/\s+/g,' ').slice(0,150));
   const summary=await page.locator('#agent-engine-summary').innerText();
   record('agent.identity-'+tier+'-summary-current',summary.includes(label)&&(tier==='unresolved'?summary.includes('重新检测'):summary.includes('可用')),summary.replace(/\s+/g,' '));
   const layouts=[];
   for(const [width,height] of [[980,640],[1220,820]]){
    await page.setViewportSize({width,height});
    layouts.push(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth));
    await page.screenshot({path:path.join(evidence,`agent-identity-${tier}-${width}.png`),fullPage:true});
   }
   await details.evaluate(n=>{n.open=true;});
   layouts.push(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth));
   await page.screenshot({path:path.join(evidence,`agent-identity-${tier}-details.png`),fullPage:true});
   await details.evaluate(n=>{n.open=false;});
   record('agent.identity-'+tier+'-layout',layouts.every(Boolean),'980/1220及展开详情无横向溢出');
  }
 } finally {await page.close();fixture.server.close();await once(fixture.server,'close');}
}

// ISS-205：真实设置授权、Agent 重入/刷新与迟到检测，合成 HTTP 零模型调用。
async function verifyDetectionLifetime(browser) {
 let selected={id:'claude-code',executable:'/fixture/bin/claude',version:'2.1.237'};
 let settingsRevision=2,consentRevision=1,releaseDetect=null,delayDetect=false;
 const fixture=createFixture({configRuntimeObject:true,async intercept(req,res,url,body){
  if(url.pathname==='/api/snapshots'){json(res,200,SNAPSHOTS);return true;}
  if(url.pathname==='/api/analysis/history'){json(res,200,{jobs:[],total:0,next_offset:null});return true;}
  if(url.pathname==='/api/config'&&req.method==='PUT'){
   selected=JSON.parse(body).analysis.runtime;settingsRevision++;consentRevision++;
   json(res,200,{applied:true});return true;
  }
  if(url.pathname==='/api/analysis/runtimes/detect'&&delayDetect){
   await new Promise(r=>releaseDetect=r);
   json(res,200,{runtimes:{[selected.id]:{...selected,availability:'broken',reason_code:'runtime_unavailable',detail:'late detection'}}});return true;
  }
  return false;
 }});
 fixture.state.analysisEnabled=true;fixture.state.savedRuntime='claude-code';fixture.state.scenario='enabled';
 fixture.server.listen(0,'127.0.0.1');await once(fixture.server,'listening');const page=await browser.newPage();
 await page.route('**/api/config',async route=>{
  if(route.request().method()!=='GET'){await route.continue();return;}
  const response=await route.fetch(),cfg=await response.json();
  cfg.analysis={...cfg.analysis,runtime:selected,settings_revision:settingsRevision,consent_revision:consentRevision};
  await route.fulfill({response,json:cfg});
 });
 const short=()=>page.locator('[data-test="agent-engine-short-status"]').textContent();
 const detect=async()=>{await page.click('#agent-detect');await page.waitForSelector('[data-test="agent-detect-short-status"]');};
 const saveInSettings=async id=>{
  await page.click('#agent-settings');await page.waitForSelector('#btn-analysis-detect');
  await page.click('#btn-analysis-detect');
  await page.click(`[data-test="analysis-pick-btn"][data-runtime-id="${id}"]`);
  await page.click('[data-test="analysis-confirm-yes"]');await page.waitForSelector('[data-test="analysis-note"]');
  await page.click('.nav-item[data-page="agent"]');await page.waitForSelector('[data-test="agent-engine-short-status"]');
 };
 try{
  await page.goto(`http://127.0.0.1:${fixture.server.address().port}/#/agent`);
  await page.waitForSelector('[data-test="agent-engine-short-status"]');await detect();
  record('agent.detect-updates-engine-summary',await short()==='可用',await short());
  await saveInSettings('codex-cli');
  record('agent.changed-engine-requires-new-detection',(await short()).includes('未检测')&&!(await short()).includes('入口已变更'),await short());
  await detect();await saveInSettings('codex-cli');
  record('agent.same-entry-new-authorization-requires-detection',selected.id==='codex-cli'&&settingsRevision===4&&consentRevision===3&&(await short()).includes('未检测'),JSON.stringify({selected,settingsRevision,consentRevision,status:await short()}));
  await detect();await page.click('#agent-refresh');await page.waitForTimeout(150);
  record('agent.refresh-invalidates-detection',(await short()).includes('未检测'),await short());
  const mismatched=await page.evaluate(async()=>{
   const {runtimeCombinedStatus}=await import('/modules/runtime-status.js');
   return runtimeCombinedStatus({enabled:true,saved:{id:'codex-cli',executable:'/fixture/bin/codex'},info:{id:'claude-code',executable:'/fixture/bin/claude',availability:'broken'}});
  });
  record('agent.other-runtime-result-is-undetected',mismatched.text==='已授权 · 未检测',JSON.stringify(mismatched));
  delayDetect=true;await page.click('#agent-detect');
  await page.waitForFunction(()=>document.querySelector('#agent-detect').disabled);
  const deadline=Date.now()+10000;
  while(!releaseDetect&&Date.now()<deadline)await page.waitForTimeout(20);
  if(!releaseDetect)throw new Error('延迟检测请求未到达夹具');
  await page.click('#agent-refresh');releaseDetect();releaseDetect=null;await page.waitForTimeout(200);
  record('agent.refresh-discards-late-detection',(await short()).includes('未检测')&&(await page.locator('#agent-runtime-status').textContent())===''&&!await page.locator('#agent-detect').isDisabled(),await short());
  await page.screenshot({path:path.join(evidence,'iss205-detection-lifetime.png')});
 }finally{if(releaseDetect)releaseDetect();await page.close();fixture.server.close();await once(fixture.server,'close');}
}
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
  // ISS-203：普通面只给短状态；引擎名/版本/路径进默认折叠的技术详情区。
  // innerText 不含折叠内容，故普通面判定用 innerText、技术详情用 textContent。
  const enginePlain=await page.locator('#agent-engine-summary').innerText();
  const engineTech=await page.locator('#agent-engine-summary [data-test="agent-tech-details"]').textContent().catch(()=> '');
  record('agent.engine-object-summary', !enginePlain.includes('[object Object]') && !enginePlain.includes('/fixture/bin') && !enginePlain.includes('2.1.237') && engineTech.includes('claude-code') && engineTech.includes('2.1.237'), JSON.stringify({plain:enginePlain.replace(/\s+/g,' ').trim().slice(0,60),tech:engineTech.replace(/\s+/g,' ').trim().slice(0,80)}));
  const detectsBefore = fixture.state.detects;
  await page.click('#agent-detect');
  // ISS-203：检测结果的普通面是短状态（可用）+ 下一动作；
  // 版本/路径/原因码在默认折叠的技术详情区。
  await page.waitForSelector('[data-test="agent-detect-short-status"]');
  const detectPlain=await page.locator('#agent-runtime-status').innerText();
  const detectTech=await page.locator('#agent-runtime-status [data-test="agent-tech-details"]').textContent().catch(()=> '');
  record('agent.recheck-selected-runtime', fixture.state.detects === detectsBefore + 1 && detectPlain.includes('可用') && detectTech.includes('2.1.237'), JSON.stringify({plain:detectPlain.replace(/\s+/g,' ').trim().slice(0,50),tech:detectTech.replace(/\s+/g,' ').trim().slice(0,70)}));
  record('agent.recheck-keeps-range', await page.inputValue('#agent-a') === '1' && await page.inputValue('#agent-b') === '3');
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
  // 扩展快照须进 SNAPSHOTS 原数组（同一引用）：预览 mock 的 previewFor 按
  // SNAPSHOTS 查 created_at，只加进下拉数组会让同 plan 预览查 undefined。
  for (const extra of [
    {...SNAPSHOTS[0],id:4,plan_id:'p',created_at:'2026-09-15T10:00:00'},
    {...SNAPSHOTS[0],id:5,plan_id:'p',created_at:'2026-09-16T10:00:00'},
    {...SNAPSHOTS[0],id:6,root:'/fixture/other',created_at:'2026-09-17T10:00:00'},
    {...SNAPSHOTS[0],id:7,plan_id:'q',created_at:'2026-09-18T10:00:00'},
  ]) SNAPSHOTS.push(extra);
  snaps=SNAPSHOTS.slice();
  await page.click('#agent-refresh');await wait('#agent-b option[value="5"]');await choose('agent-b',5);
  record('agent.plan-dataset-converges',await page.locator('#agent-a option').count()===1&&await page.inputValue('#agent-a')==='4');
  record('agent.same-plan-range-selectable',!await visible('[data-test="agent-no-range"]')&&(await page.locator('#agent-range-status').textContent()).includes('#4 → #5'), 'status='+await page.locator('#agent-range-status').textContent());
  await page.click('#agent-analysis-body [data-test="analysis-preview-btn"]');await wait('#agent-analysis-body [data-test="analysis-confirm-btn"]');
  record('agent.same-plan-preview-constructible',await visible('#agent-analysis-body [data-test="analysis-confirm-btn"]'));
  // 跨 plan/跨档组合在 UI 上由 b 侧收敛保护隔离（正常操作构造不出跨档区间，
  // 这正是闸门前置形态）；跨 plan 的后端拒绝由 pytest 合同测试钉住。此处断言：
  // 选 q 档快照后 a 侧不再提供 p 档选项，且提示如实（不足两个或不同计划）。
  await page.evaluate(()=>{const b=document.getElementById('agent-b');b.value='7';b.dispatchEvent(new Event('change'));});
  await page.waitForTimeout(300);
  record('agent.cross-plan-unselectable-in-ui',await visible('[data-test="agent-no-range"]')&&!(await page.locator('#agent-a option[value="4"]').count()>0)&&((await page.locator('#agent-analysis-body').textContent()).includes('不足两个')||(await page.locator('#agent-analysis-body').textContent()).includes('不同计划')), 'body='+await page.locator('#agent-analysis-body').textContent());
  // 跨 plan 断言后恢复同 plan 干净态（a=4/b=5）：残留混搭选择会让后续
  // single-snapshot/restore 类断言的 a 侧下拉处于禁用态。
  await page.evaluate(()=>{const b=document.getElementById('agent-b');b.value='5';b.dispatchEvent(new Event('change'));const a=document.getElementById('agent-a');a.value='4';a.dispatchEvent(new Event('change'));});
  await page.waitForTimeout(200);
  await choose('agent-b',6);record('agent.single-snapshot-has-dataset-exit',await page.locator('#agent-a').isDisabled()&&!await page.locator('#agent-b').isDisabled()&&(await page.locator('#agent-range-status').textContent()).includes('不足两个'));
  historyFailure=true;await page.click('#agent-history-refresh');await page.waitForTimeout(150);record('agent.history-failure-visible',(await page.locator('#agent-history-error').textContent()).includes('合成历史故障'));
  historyFailure=false;await page.click('#agent-history-refresh');await page.waitForTimeout(150);record('agent.history-failure-retry',await page.locator('#agent-history-error').textContent()==='');
  for(let i=0;i<25;i++)extra.push({job_id:'synthetic-'+i,a_snapshot_id:100+i,b_snapshot_id:200+i,status:'failed',reason_code:'runner_nonzero_exit',terminal:true,created_at:'2026-09-01T12:00:00',runtime:{id:'codex-cli'}});
  await page.click('#agent-history-refresh');await wait('#agent-history-more:visible');record('agent.history-page-bounded',await page.locator('#agent-history-body tr').count()===20, 'rows='+await page.locator('#agent-history-body tr').count()+' count='+await page.locator('#agent-history-count').textContent());
  await page.click('#agent-history-more');await page.waitForTimeout(200);record('agent.history-more-complete',await page.locator('#agent-history-body tr').count()===fixture.state.jobs.size+extra.length&&!await visible('#agent-history-more'), 'rows='+await page.locator('#agent-history-body tr').count()+' expected='+(fixture.state.jobs.size+extra.length)+' hidden='+await page.locator('#agent-history-more').getAttribute('hidden'));
  for(const [width,height] of [[980,640],[1220,820],[1440,900]]){await page.setViewportSize({width,height});await page.evaluate(()=>{document.querySelector('.page-container').scrollTop=0;});await page.screenshot({path:path.join(evidence,`agent-${width}.png`),fullPage:true});record(`agent.viewport-${width}`,await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth && [...document.querySelectorAll('[data-agent-job]')].every(btn=>getComputedStyle(btn).opacity==='1' && btn.getBoundingClientRect().height<45)));}
  await page.click('.nav-item[data-page="changes"]');await wait('#analysis-panel');record('agent.changes-local-entry-retained',await visible('#analysis-panel'));
  // 尾部 verify 函数各自 createFixture 独立场景，/api/snapshots 直接返回
  // SNAPSHOTS——移除 plan 扩展快照（id 4-7），恢复它们对 1-3 数据集的假设。
  SNAPSHOTS.splice(3, 5);
  await verifyLateRestore(browser);
  await verifyHistoricalRerunRestore(browser);
  await verifyEvidenceControls(browser);
  await verifyLayeredStatus(browser);
  await verifyDetectLayering(browser);
  await verifyDetectionLifetime(browser);
  await verifyRuntimeIdentityAndMismatch(browser);
  verifyNewReasonTexts();
  verifyRuntimeStatusCopy();
  record('agent.no-unhandled-page-errors',errors.length===0,errors.join(';'));
 }finally{await browser.close();fixture.server.close();await once(fixture.server,'close');}
 const failed=checks.filter(c=>!c.ok).length;console.log(JSON.stringify({ok:!failed,passed:checks.length-failed,failed,evidence,checks},null,2));if(failed)process.exitCode=1;
}
main().catch(e=>{console.error(e);console.log(JSON.stringify({ok:false,passed:checks.filter(c=>c.ok).length,failed:checks.filter(c=>!c.ok).length+1,evidence,checks,error:String(e)},null,2));process.exitCode=1});
