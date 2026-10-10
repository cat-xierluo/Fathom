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
  const roots=override?[explicit]:mode==='release'?[path.join(tmp,'system'),path.join(tmp,'data')]:[old];
  record(`${kind}-effective-object`,override?current.scan_override.root===explicit:JSON.stringify(current.effective_selection.roots)===JSON.stringify(roots),current.plan_source);
  record(`${kind}-saved-preserved-read`,(fs.existsSync(settings)?fs.readFileSync(settings,'utf8'):null)===before);
  page=await browser.newPage({viewport:{width:1220,height:820}});
let writes=0,scans=0;
const errors=[];
  page.on('request',r=>{if(r.method()==='PUT')writes++;
if(r.method()==='POST'&&new URL(r.url()).pathname==='/api/scan')scans++;
});
page.on('pageerror',e=>errors.push(e.message));
  await page.goto(`http://127.0.0.1:${port}/#settings/monitoring`);
await page.waitForFunction(()=>document.querySelector('[data-test="scope-current"]')?.textContent.includes('当前扫描对象：'));
  record(`${kind}-no-choices-all-dom`,await page.locator('input[name="scope-mode"],#scope-roots,#cfg-scan-root,[data-test="scope-preview-btn"],[data-test="scope-save-btn"]').count()===0);
  const text=await page.locator('[data-test="scope-current"]').innerText();
record(`${kind}-object-visible`,roots.every(r=>text.includes(r))&&(mode!=='release'||override||text.includes('内置启动盘整体'))&&await page.locator('[data-test="scope-device"]').innerText().then(t=>t.includes('已挂载')&&!t.includes('可选')));
  if(kind!=='none'&&mode==='release'&&!override)record(`${kind}-old-value-not-active`,await page.locator('[data-test="scope-saved-compatibility"]').innerText().then(t=>t.includes('不作为当前扫描决定')));
  if(override)record(`${kind}-override-honest`,await page.locator('[data-test="scope-scan-override"]').innerText().then(t=>t.includes('不使用已保存范围')&&t.includes(kind==='cli'?'--scan-root':'FATHOM_SCAN_ROOT')));
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
