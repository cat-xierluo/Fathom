#!/usr/bin/env node
/* ISS-191: real isolated serve + synthetic startup discovery + production page.
 * No production roots, permissions, models, install or scope selection. */
'use strict';
const fs=require('fs'),path=require('path'),os=require('os'),net=require('net');
const {spawn}=require('child_process');
 const {chromium}=require('playwright');
const REPO=path.resolve(__dirname,'..');
const PY=process.env.FATHOM_PYTHON || (fs.existsSync(path.join(REPO,'.venv/bin/python'))?path.join(REPO,'.venv/bin/python'):path.join(REPO,'.runtime/bin/python'));
const evidence=process.env.FATHOM_SCOPE_EVIDENCE_DIR ? path.resolve(process.env.FATHOM_SCOPE_EVIDENCE_DIR) : path.join(REPO,'verify-results/iss191/browser');
fs.mkdirSync(evidence,{recursive:true});
const checks=[],services=[];
let browser;
function record(name,ok,detail=''){checks.push({name,ok:!!ok,detail});
process.stderr.write(`${ok?'PASS':'FAIL'} ${name} ${detail}\n`);
if(!ok)throw Error(name);
}
const delay=ms=>new Promise(r=>setTimeout(r,ms));
async function wait(fn){const until=Date.now()+12000;
while(Date.now()<until){try{if(await fn())return;
}catch{}await delay(100);
}throw Error('timed out');
}
async function freePort(){return new Promise((resolve,reject)=>{const s=net.createServer();
s.on('error',reject);
s.listen(0,'127.0.0.1',()=>{const p=s.address().port;
s.close(()=>resolve(p));
});
});
}
async function portClosed(port){return new Promise(resolve=>{const s=net.connect(port,'127.0.0.1');
s.on('error',()=>{s.destroy();
resolve(true);
});
s.on('connect',()=>{s.destroy();
resolve(false);
});
});
}
/* ISS-193：普通层/技术层分层断言（14 项）。
 * 普通层只讲人话（当前对象、卷状态计数、共享空间口径、隔离说明、旧范围兼容、采集动作）；
 * 容器引用、卷标识、计划身份、读取限制、耗时说明只出现在默认收起的 details 里。
 * 两层取自同一份已加载载荷：展开折叠区不得产生任何新请求。 */
const TECH_LITERALS = ['plan_id', 'scope_id', 'identity_version', 'min_kb', 'du_timeout_s', 'apfs-volume:', 'apfs-container:'];

async function scopeLayeringChecks(kind, page, reads, meta) {
  const q = (s) => page.locator(s);
  const vis = async (s) => await q(s).count() > 0 && await q(s).first().isVisible();
  const txt = async (s) => await q(s).count() ? await q(s).first().innerText() : '';

  await wait(async () => await q('[data-test="scope-tech"]').count() === 1);
  record(`${kind}-layer-tech-toggle-exists`, await q('[data-test="scope-tech"]').count() === 1);
  record(`${kind}-layer-tech-collapsed-by-default`,
    await q('[data-test="scope-tech"]').evaluate((el) => !el.hasAttribute('open')) && !(await vis('[data-test="scope-tech-body"]')));
  record(`${kind}-layer-tech-summary-accessible`,
    await vis('[data-test="scope-tech"] > summary')
    && await q('[data-test="scope-tech"] > summary').evaluate((el) => el.tagName === 'SUMMARY' && (el.textContent || '').includes('技术诊断信息')));
  record(`${kind}-layer-normal-current-object-first`, await page.evaluate(() => {
    const p = document.getElementById('scope-panel');
    const cur = p && p.querySelector('[data-test="scope-current"]');
    const after = p && p.querySelector('[data-test="scope-discovery-note"],[data-test="scope-discovery-pending"],[data-test="scope-discovery-error"],[data-test="scope-volume-summary"],[data-test="scope-shared-free"],[data-test="scope-tech"]');
    return Boolean(cur && after && (cur.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING));
  }));
  record(`${kind}-layer-normal-volume-summary`, meta.volumes === 0
    ? !(await vis('[data-test="scope-volume-summary"]'))
    : await vis('[data-test="scope-volume-summary"]')
      && await txt('[data-test="scope-volume-summary"]').then((t) => t.includes('个卷') && t.includes('可访问') && t.includes(`启动容器内 ${meta.volumes} 个卷`)));
  record(`${kind}-layer-normal-shared-free`, !meta.containerRef
    ? !(await vis('[data-test="scope-shared-free"]'))
    : await vis('[data-test="scope-shared-free"]') && await txt('[data-test="scope-shared-free"]').then((t) => t.includes('共享')));

  // 普通层文本 = 面板去掉技术折叠区后的全部文本（不依赖视觉位置）。
  const normalText = await page.evaluate(() => {
    const p = document.getElementById('scope-panel');
    const clone = p.cloneNode(true);
    clone.querySelectorAll('[data-test="scope-tech"]').forEach((n) => n.remove());
    return clone.textContent || '';
  });
  const leaked = TECH_LITERALS.filter((k) => normalText.includes(k));
  if (/\bdisk\d/.test(normalText)) leaked.push('disk 容器引用');
  if (meta.containerRef && normalText.includes(meta.containerRef)) leaked.push(`容器引用 ${meta.containerRef}`);
  record(`${kind}-layer-normal-no-tech-literals`, leaked.length === 0, leaked.join(','));

  record(`${kind}-layer-normal-legacy-compat`, meta.hasSaved
    ? await vis('[data-test="scope-saved-compatibility"]')
      && await txt('[data-test="scope-saved-compatibility"]').then((t) => t.includes('不作为当前扫描决定') && t.includes('旧历史保持独立基线'))
    : !(await vis('[data-test="scope-saved-compatibility"]')));
  record(`${kind}-layer-normal-no-editable-scope`, await page.evaluate(() => document.querySelectorAll(
    '#scope-panel input,#scope-panel select,#scope-panel textarea,#scope-panel [contenteditable="true"],'
    + '[data-test="scope-preview-btn"],[data-test="scope-save-btn"],#scope-roots,#cfg-scan-root,input[name="scope-mode"]').length) === 0);
  record(`${kind}-layer-normal-collect-affordance`, await vis('[data-test="scope-firstscan-btn"]') && await page.evaluate(() => {
    const p = document.getElementById('scope-panel');
    const c = p && p.querySelector('[data-test="scope-firstrun"]');
    return Boolean(c && (c.textContent || '').includes('无需选择或保存范围') && !c.closest('[data-test="scope-tech"]'));
  }));

  const before = reads;
  await q('[data-test="scope-tech"] > summary').click();
  await wait(async () => await q('[data-test="scope-tech"][open]').count() === 1);
  const required = [];
  if (meta.containerRef) required.push('[data-test="scope-container"]');
  if (meta.volumes > 0) required.push('[data-test="scope-volume"]');
  if (meta.others > 0) required.push('[data-test="scope-device"]');
  if (meta.hasPlan) required.push('[data-test="scope-preview"]', '[data-test="scope-preview-limits"]', '[data-test="scope-preview-uncertainty"]');
  const missing = await page.evaluate((sels) => {
    const b = document.querySelector('[data-test="scope-tech-body"]');
    return b ? sels.filter((s) => !b.querySelector(s)) : sels;
  }, required);
  record(`${kind}-layer-tech-anchors-complete`, missing.length === 0 && await vis('[data-test="scope-tech-body"]'), missing.join(','));

  const techText = await txt('[data-test="scope-tech-body"]');
  const want = meta.hasPlan ? ['min_kb', 'du', '身份版本', meta.scopeId, meta.planId].filter(Boolean) : [];
  const absent = want.filter((k) => !techText.includes(k));
  record(`${kind}-layer-tech-fields-visible`, absent.length === 0, absent.join(','));

  // 展开态承载旧实现的信息量，按规格在 980×640 下同样不得横向溢出。
  await page.setViewportSize({ width: 980, height: 640 });
  await delay(80);
  const openLayout = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }));
  record(`${kind}-layer-980-open-no-overflow`, openLayout.scroll <= openLayout.width + 1, JSON.stringify(openLayout));
  await q('[data-test="scope-tech"] > summary').click();
  await page.setViewportSize({ width: 1220, height: 820 });
  await delay(80);
  record(`${kind}-layer-no-second-data-fetch`, reads === before, `reads=${reads} before=${before}`);
}

const bootstrap=String.raw`import sys,os,runpy,pytest
from pathlib import Path
from fathom import cli,config,api,scan_coordinator,notify,storage
from dataclasses import replace
m=pytest.MonkeyPatch();cfg,system,data=runpy.run_path('tests/test_startup_default.py')['isolated'].__wrapped__(Path(sys.argv[1]),m)
discovery=storage.discover_startup('/')
external=storage.OtherDevice(storage.OTHER_DEVICE_SCHEMA,storage.DISCOVERY_VERSION,'partition:synthetic-external','Synthetic external','disk10s1',str(Path(sys.argv[1])/'legacy'),'Apple_HFS',False,1024,('synthetic',))
m.setattr(storage,'discover_startup',lambda *a,**k:replace(discovery,other_devices=(external,)))
(system/'fixture').write_bytes(b's'*8192);(data/'fixture').write_bytes(b'd'*12288)
scan_coordinator.read_capacity_readings=lambda:[]
notify.notify_scan_round=lambda *a,**k:False
if sys.argv[4]=='env':os.environ['FATHOM_SCAN_ROOT']=sys.argv[5]
config._ACTIVE=config.RuntimeConfig.from_env(home=Path(sys.argv[1])/'legacy')
argv=['--runtime-dir',str(cfg.runtime_dir),'--port',os.environ['FATHOM_PORT'],'--port-range','0','--resource-dir',sys.argv[2]]
if sys.argv[3]=='development':argv+=['--runtime-mode','development']
if sys.argv[4]=='cli':argv+=['--scan-root',sys.argv[5]]
raise SystemExit(cli.main(argv+['serve']))`;
async function scenario(kind){
 const tmp=fs.mkdtempSync(path.join(evidence,`${kind}-`)),runtime=path.join(tmp,'runtime'),old=path.join(tmp,'old-directory'),explicit=path.join(tmp,'explicit');
 for(const p of[runtime,old,explicit])fs.mkdirSync(p,{recursive:true});
 const mode=kind==='development'?'development':'release',override=['env','cli'].includes(kind)?kind:null;
 const saved={schema:'fathom.scope.config',mode:'custom_directory',roots:[kind==='missing'?path.join(tmp,'missing'):old],scope_ids:['path:old'],identity_version:1,container_id:null,revision:7,selected_at:null};
 const settings=path.join(runtime,'settings.json');
if(kind!=='none')fs.writeFileSync(settings,JSON.stringify({storage_scope:saved,min_kb:1}));
 const before=fs.existsSync(settings)?fs.readFileSync(settings,'utf8'):null,port=await freePort();
 const env={...process.env};
for(const k of Object.keys(env))if(k.startsWith('FATHOM_'))delete env[k];
 Object.assign(env,{HOME:tmp,FATHOM_RUNTIME_MODE:mode,FATHOM_RUNTIME_DIR:runtime,FATHOM_RESOURCE_DIR:REPO,FATHOM_PORT:String(port)});
if(kind==='env')env.FATHOM_SCAN_ROOT=explicit;
 const fd=fs.openSync(path.join(tmp,'service.log'),'w');
const child=spawn(PY,['-c',bootstrap,tmp,REPO,mode,override||'',explicit],{cwd:REPO,env,stdio:['ignore',fd,fd]});
fs.closeSync(fd);
let exited=false;
child.on('exit',()=>{exited=true;
});
 const service={kind,pid:child.pid,port,tmp};
services.push(service);
let page;
 const request=async(url,method='GET',body)=>{const r=await fetch(`http://127.0.0.1:${port}${url}`,{method,headers:body?{'Content-Type':'application/json','X-Fathom-Token':service.token}:{},body:body?JSON.stringify(body):undefined});
return{status:r.status,body:await r.json()};
};
 try{
  await wait(async()=>{if(exited)throw Error('child exited');
return(await request('/health')).status===200;
});
const health=(await request('/health')).body;
service.token=(await request('/api/bootstrap')).body.token;
  record(`${kind}-health-bound`,health.pid===child.pid&&health.port===port&&health.runtime_mode===mode,`pid=${child.pid} port=${port}`);
  const current=(await request('/api/storage/plan/preview')).body;
const discovery=(await request('/api/storage/discovery')).body.discovery||{};
const plan0=current.plan||null,planRows=plan0&&plan0.plans||[];
// hasSaved 与渲染条件一致：旧范围存在且未生效才出兼容结论句（不靠 kind 猜）。
const layering={volumes:(discovery.startup_volumes||[]).length,others:(discovery.other_devices||[]).length,
 containerRef:(discovery.startup_container||{}).container_reference||'',hasPlan:planRows.length>0,
 scopeId:planRows[0]?planRows[0].scope_id:'',planId:planRows[0]?planRows[0].plan_id:'',
 hasSaved:Boolean(current.selection)&&!current.saved_active};
  const roots=override?[explicit]:mode==='release'?[path.join(tmp,'system'),path.join(tmp,'data')]:[old];
  record(`${kind}-effective-object`,override?current.scan_override.root===explicit:JSON.stringify(current.effective_selection.roots)===JSON.stringify(roots),current.plan_source);
  record(`${kind}-saved-preserved-read`,(fs.existsSync(settings)?fs.readFileSync(settings,'utf8'):null)===before);
  page=await browser.newPage({viewport:{width:1220,height:820}});
let writes=0,scans=0,reads=0;
const errors=[];
  page.on('request',r=>{const pn=new URL(r.url()).pathname;
if(r.method()==='PUT')writes++;
if(r.method()==='POST'&&pn==='/api/scan')scans++;
if(r.method()==='GET'&&(pn==='/api/storage/discovery'||pn==='/api/storage/plan/preview'))reads++;
});
page.on('pageerror',e=>errors.push(e.message));
  await page.goto(`http://127.0.0.1:${port}/#settings/monitoring`);
await page.waitForFunction(()=>document.querySelector('[data-test="scope-current"]')?.textContent.includes('当前扫描对象：'));
  record(`${kind}-no-choices-all-dom`,await page.locator('input[name="scope-mode"],#scope-roots,#cfg-scan-root,[data-test="scope-preview-btn"],[data-test="scope-save-btn"]').count()===0);
  const text=await page.locator('[data-test="scope-current"]').innerText();
// scope-device 已在技术折叠区内：按锚点读 textContent，不依赖可见性。
record(`${kind}-object-visible`,roots.every(r=>text.includes(r))&&(mode!=='release'||override||text.includes('内置启动盘整体'))&&await page.locator('[data-test="scope-device"]').textContent().then(t=>t.includes('已挂载')&&!t.includes('可选')));
  if(kind!=='none'&&mode==='release'&&!override)record(`${kind}-old-value-not-active`,await page.locator('[data-test="scope-saved-compatibility"]').innerText().then(t=>t.includes('不作为当前扫描决定')));
  if(override)record(`${kind}-override-honest`,await page.locator('[data-test="scope-scan-override"]').innerText().then(t=>t.includes('不使用已保存范围')&&t.includes(kind==='cli'?'--scan-root':'FATHOM_SCAN_ROOT')));
  await scopeLayeringChecks(kind,page,reads,layering);
  await page.click('[data-test="scope-reload-btn"]');
await page.waitForFunction(()=>document.querySelector('[data-test="scope-current"]')?.textContent.includes('当前扫描对象：'));
  record(`${kind}-readonly-reload-no-write-scan`,writes===0&&scans===0&&(fs.existsSync(settings)?fs.readFileSync(settings,'utf8'):null)===before);
  for(const width of[980,1220,1440]){await page.setViewportSize({width,height:820});
await delay(80);
const layout=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,choices:document.querySelectorAll('input[name="scope-mode"],#scope-roots,#cfg-scan-root').length}));
record(`${kind}-${width}-readonly-no-overflow`,layout.choices===0&&layout.scroll<=layout.width+1,JSON.stringify(layout));
await page.screenshot({path:path.join(tmp,`settings-${width}.png`)});
}
  await page.focus('[data-test="scope-reload-btn"]');
record(`${kind}-reload-keyboard-focus`,await page.evaluate(()=>document.activeElement?.dataset.test==='scope-reload-btn'));
  await page.click('[data-section="analysis"]');
record(`${kind}-analysis-still-visible`,await page.locator('#settings-section-analysis').isVisible());
await page.click('[data-section="about"]');
record(`${kind}-about-still-visible`,await page.locator('#settings-section-about').isVisible());
await page.click('[data-section="monitoring"]');
  if(kind==='custom'){
   await page.click('[data-test="scope-tech"] > summary');
await page.waitForFunction(()=>!!document.querySelector('[data-test="scope-tech"][open]'));
   record('current-plan-identity-limits',await page.locator('[data-test="scope-preview"]').innerText().then(t=>t.includes('apfs-volume:system')&&t.includes('plan:')&&t.includes('du')&&t.includes('min_kb')));
   await page.click('[data-test="scope-firstscan-btn"]');
await wait(()=>scans===1);
await wait(async()=>!!(await request('/api/storage/summary')).body.round);
   record('scan-is-explicit-no-save',scans===1&&writes===0&&fs.readFileSync(settings,'utf8')===before);
   const summary=(await request('/api/storage/summary')).body;
record('firstscan-summary-matches-default',summary.scope.mode==='startup_storage'&&JSON.stringify(summary.scope.roots)===JSON.stringify(roots)&&!!summary.round);
   await page.route('**/api/storage/plan/preview',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'合成启动盘发现失败'})}));
await page.click('[data-test="scope-reload-btn"]');
await page.waitForSelector('[data-test="scope-feedback"]');
record('failed-current-no-home-fallback',await page.locator('[data-test="scope-feedback"]').innerText().then(t=>t.includes('失败'))&&await page.locator('[data-test="scope-firstscan-btn"]').count()===0);
   await page.unroute('**/api/storage/plan/preview');
await page.click('[data-test="scope-reload-btn"]');
await page.waitForSelector('[data-test="scope-firstscan-btn"]');
record('failure-reload-restores-current',await page.locator('[data-test="scope-current"]').innerText().then(t=>t.includes('内置启动盘整体')));
  }
  record(`${kind}-no-page-errors`,errors.length===0,errors.join(';'));
 }finally{
  if(page)await page.close();
if(!exited)child.kill('SIGTERM');
await wait(()=>exited);
record(`${kind}-port-released`,await portClosed(port));
service.closed=true;
delete service.token;
 }
}
(async()=>{try{browser=await chromium.launch({headless:true});
for(const kind of['none','custom','missing','development','env','cli'])await scenario(kind);
}catch(e){checks.push({name:'exception',ok:false,detail:e.stack});
process.stderr.write(e.stack+'\n');
}finally{if(browser)await browser.close();
const result={ok:checks.every(c=>c.ok),passed:checks.filter(c=>c.ok).length,failed:checks.filter(c=>!c.ok).length,checks,services,evidenceDir:evidence};
fs.writeFileSync(path.join(evidence,'result.json'),JSON.stringify(result,null,2));
console.log(JSON.stringify(result));
if(!result.ok)process.exitCode=1;
}})();
