#!/usr/bin/env node
/* ISS-206：生产三页真实 Chromium；合成 HTTP，不读生产库/卷，不扫描/调用模型。 */
'use strict';
const {createFixture,json}=require('./verify_analysis_frontend.cjs');
const {chromium}=require('playwright');
const {once}=require('events');
const checks=[];
const record=(name,ok,detail)=>{checks.push({name,ok:!!ok,detail});console.error(`${ok?'PASS':'FAIL'} ${name} ${JSON.stringify(detail)}`);};
const snap=(id,root,dir_count=1,plan_id=null)=>({id,root,dir_count,plan_id,created_at:`2026-10-01T00:00:${String(id).padStart(2,'0')}`,min_kb:1024,exclude_names:'',total_kb:1000,collection_status:'full'});
const system=[snap(29,'/System/Volumes/VM'),snap(28,'/System/Volumes/Preboot'),snap(27,'/System/Volumes/Update')];
const scenarios=[
 {name:'root',snaps:[...system,snap(26,'/',99999,'single-root-plan'),snap(24,'/',100),snap(23,'/',200),snap(22,'/System/Volumes/Data',10000),snap(21,'/System/Volumes/Data',11000)],want:24},
 {name:'data',snaps:[...system,snap(26,'/System/Volumes/Data',99999,'single-data-plan'),snap(22,'/System/Volumes/Data',10000),snap(21,'/System/Volumes/Data',11000)],want:22},
 {name:'content',snaps:[...system,snap(18,'/Users/synthetic',50),snap(17,'/Users/synthetic',60)],want:18},
 {name:'system-only',snaps:system,want:29},
];
async function main(){
 const browser=await chromium.launch({headless:true});
 try{for(const scenario of scenarios){
  const snaps=scenario.snaps;
  const measured=[system[2],system[1],system[0]].map(s=>({root:s.root,snapshot_id:s.id,snapshot_created_at:s.created_at,member_status:'done',quality:'full'}));
  const summary={scope:{mode:'startup_disk',roots:[...new Set(snaps.map(s=>s.root))]},capacity:{free_bytes:10000,total_bytes:100000},round:{status:'partial',started_at:'2026-10-01',finished_at:'2026-10-02'},previous_round:{started_at:'2026-09-30'},attribution:{measured_members:measured,stale_members:snaps.filter(s=>!system.some(x=>x.root===s.root)).map(s=>({root:s.root,snapshot_id:null,stale:true})),comparable_to_previous:false},unexplained:{bytes:null,comparable:false}};
  const fixture=createFixture({intercept(req,res,url){
   const p=url.pathname,q=url.searchParams;
   if(p==='/api/snapshots'){json(res,200,snaps);return true;}
   if(p==='/api/storage/summary'){json(res,200,summary);return true;}
   if(p==='/api/volume-trend'){json(res,200,[]);return true;}
   if(p==='/api/trees'||p==='/api/browse'){
    const s=snaps.find(s=>s.id===Number(q.get('snapshot_id')))||snaps[0];
    json(res,200,{snapshot_id:s.id,snapshot:s,snapshot_at:s.created_at,root:s.root,path:s.root,crumbs:[{name:s.root,path:s.root}],children:[],trend:[],size_kb:1000,pagination:{offset:0,returned:0,total:0,has_more:false}});return true;
   }
   if(p==='/api/diff'||p==='/api/diff/children'){
    const a=snaps.find(s=>s.id===Number(q.get('a')))||snaps[snaps.length-1],b=snaps.find(s=>s.id===Number(q.get('b')))||snaps[0];
    json(res,200,{a,b,dataset:{root:b?.root},path:b?.root,ancestors:[],parent:{path:b?.root,name:b?.root},children:[],pagination:{total:0,has_more:false},counts:{},grown:[],shrunk:[],added:[],removed:[]});return true;
   }
   return false;
  }});
  fixture.server.listen(0,'127.0.0.1');await once(fixture.server,'listening');
  const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(String(e)));
  const base=`http://127.0.0.1:${fixture.server.address().port}`;
  const ready=sel=>page.waitForFunction(({sel,n})=>document.querySelector(sel)?.options.length===n,{sel,n:snaps.length});
  // 重入时旧目录仍留在 DOM；等旧 option 被新响应替换，不能只等相同数量。
  const reenterAfterCatalog=async(pageName,sel)=>{
   const oldOption=await page.$(`${sel} option`);
   try{
    await page.click('.nav-item[data-page="settings"]');
    await page.click(`.nav-item[data-page="${pageName}"]`);
    await page.waitForFunction(old=>!old.isConnected,oldOption,{timeout:15000});
    await ready(sel);
    return await oldOption.evaluate(old=>!old.isConnected);
   }finally{await oldOption.dispose();}
  };
  try{
   await page.goto(base+'/#/changes');await ready('#sel-b');
   const changes=await page.locator('#sel-b').inputValue();
   record(scenario.name+'.changes-default',changes===String(scenario.want),{selected:changes,want:scenario.want});
   const catalog=await page.locator('#sel-b option').evaluateAll(nodes=>nodes.map(n=>Number(n.value)));
   record(scenario.name+'.all-snapshots-original-order',JSON.stringify(catalog)===JSON.stringify(snaps.map(s=>s.id)),catalog);
   await page.selectOption('#sel-b','29');
   const changesReplaced=await reenterAfterCatalog('changes','#sel-b');
   record(scenario.name+'.changes-explicit-system-kept',changesReplaced&&await page.locator('#sel-b').inputValue()==='29',{catalogReplaced:changesReplaced,selected:await page.locator('#sel-b').inputValue()});
   await page.goto(base+'/#/browse');await ready('#browse-snap');
   const browse=await page.locator('#browse-snap').inputValue();
   record(scenario.name+'.browse-default',browse===String(scenario.want),{selected:browse,want:scenario.want});
   await page.selectOption('#browse-snap','29');
   const browseReplaced=await reenterAfterCatalog('browse','#browse-snap');
   record(scenario.name+'.browse-explicit-system-kept',browseReplaced&&await page.locator('#browse-snap').inputValue()==='29',{catalogReplaced:browseReplaced,selected:await page.locator('#browse-snap').inputValue()});
   await page.goto(base+'/#/overview');await page.waitForSelector('[data-test="storage-cta"]');
   const cta=await page.locator('[data-test="storage-cta"]').getAttribute('data-b');
   record(scenario.name+'.cta-content-root',cta===String(scenario.want),{selected:cta,want:scenario.want,firstMeasured:27});
   await page.click('[data-test="storage-cta"]');await ready('#sel-b');
   await page.waitForTimeout(200);
   record(scenario.name+'.cta-handoff-content-root',await page.locator('#sel-b').inputValue()===String(scenario.want),{selected:await page.locator('#sel-b').inputValue()});
   record(scenario.name+'.read-only-no-page-errors',fixture.state.puts.length===0&&fixture.state.jobsPosted===0&&errors.length===0,errors);
  }finally{await page.close();fixture.server.close();await once(fixture.server,'close');}
 }}finally{await browser.close();}
 const failed=checks.filter(c=>!c.ok).length;
 console.log(JSON.stringify({ok:failed===0,passed:checks.length-failed,failed,checks},null,2));if(failed)process.exitCode=1;
}
main().catch(e=>{console.error(e);process.exitCode=1;});
