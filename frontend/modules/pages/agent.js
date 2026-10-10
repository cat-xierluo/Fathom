/* Agent 中心：选已保存快照区间、纯读跨区间任务历史；配置仍由设置页负责。 */
import { fetchJSON, beginRequest, invalidateRequest } from '../request.js';
import { escapeHtml } from '../format.js';
import { datasetKey } from '../dataset.js';
import { createAnalysisPanel } from '../components/analysis-panel.js';
let snapshots = [], jobs = [], nextOffset = null, openedJob = null;
const el = (id) => document.getElementById(id);
const selection = () => openedJob
  ? { a: String(openedJob.a_snapshot_id), b: String(openedJob.b_snapshot_id) }
  : { a: el('agent-a').value, b: el('agent-b').value };
const snapshot = (id) => snapshots.find(s => String(s.id) === String(id));
function rangeReason() {
  const {a,b} = selection(); const sa = snapshot(a), sb = snapshot(b);
  if (!sa || !sb) return '该任务的原区间快照已不可用；仍可读取保存的原文与证据，请改选有效区间后生成新预览。';
  if (a === b || sa.created_at >= sb.created_at) return '请选择时间更早的基线与更晚的对比快照。';
  if (datasetKey(sa) !== datasetKey(sb)) return '所选快照属于不同数据集，不能生成变化解读。';
  if (sa.plan_id || sb.plan_id) return '新范围身份暂不支持 AI 变化解读；不会降级为旧口径或发送数据。';
  return '';
}
const panel = createAnalysisPanel({
  panelId:'agent-analysis-panel', bodyId:'agent-analysis-body', domain:'agentAnalysis',
  selection, readWhenDisabled:true, previewBlock:rangeReason,
  root:() => openedJob ? null : snapshot(selection().a)?.root,
  changed:() => loadHistory(),
});
function option(s) {
  return `<option value="${escapeHtml(String(s.id))}">#${escapeHtml(String(s.id))} · ${escapeHtml(s.created_at.replace('T',' '))} · ${escapeHtml(s.root || '未知根')}${s.plan_id ? '（新范围）' : ''}</option>`;
}
function fillSelectors({changedB=false} = {}) {
  const a = el('agent-a'), b = el('agent-b'), oldA = a.value, oldB = b.value;
  b.innerHTML = snapshots.map(option).join('');
  if (snapshot(oldB)) b.value = oldB;
  const anchor = snapshot(b.value);
  const group = anchor ? snapshots.filter(s => datasetKey(s) === datasetKey(anchor) && s.id !== anchor.id && s.created_at < anchor.created_at) : [];
  a.innerHTML = group.map(option).join('');
  if (!changedB && group.some(s => String(s.id) === oldA)) a.value = oldA;
  a.disabled = group.length === 0; b.disabled = snapshots.length === 0;
  try { sessionStorage.setItem('fathom-agent-range', JSON.stringify({a:a.value,b:b.value})); } catch (_) {}
}
function updateRange() {
  const reason = !snapshots.length ? '尚无快照。解读需要同一数据集内两个有效历史点；采集完成后可在这里选择。'
    : !el('agent-a').value ? '该数据集内不足两个按时间排序的快照。请切换对比快照；任务历史仍可查看。' : rangeReason();
  el('agent-range-status').textContent = reason || `当前区间 #${selection().a} → #${selection().b}；预览只整理本机事实，确认后才发送。`;
  el('agent-opened-task').textContent = openedJob ? `正在查看原任务 ${openedJob.job_id} · 区间 #${openedJob.a_snapshot_id} → #${openedJob.b_snapshot_id}（与上方新任务选点独立）` : '';
  if (openedJob) { panel.showJob(openedJob); return; }
  if (reason) {
    panel.leave(); el('agent-analysis-panel').hidden = false;
    el('agent-analysis-body').innerHTML = `<p class="hint" data-test="agent-no-range">${escapeHtml(reason)}</p>`;
  } else panel.load();
}
const statusText = {starting:'启动中',running:'运行中',cancelling:'取消中',succeeded:'已完成',failed:'失败',cancelled:'已取消',timed_out:'超时',interrupted:'已中断'};
function renderHistory(total) {
  el('agent-history-count').textContent = `共 ${total} 个任务；已显示 ${jobs.length} 个，在途优先。只展示本机仍保留的生命周期记录。`;
  el('agent-history-body').innerHTML = jobs.length ? jobs.map(j => `<tr>
    <td><button type="button" class="btn-mini" data-agent-job="${escapeHtml(j.job_id)}" aria-pressed="${openedJob?.job_id === j.job_id}">查看 #${escapeHtml(String(j.a_snapshot_id))} → #${escapeHtml(String(j.b_snapshot_id))}</button></td>
    <td>${escapeHtml(j.revoked ? '已撤销（正文不再保留）' : j.analysis?.expired ? '已过期（原文可读）' : statusText[j.status] || j.status)}${j.reason_code ? `<br><span class="hint">${escapeHtml(j.reason_code)}</span>` : ''}</td>
    <td>${escapeHtml(j.runtime?.id || '未上报')}<br><span class="hint">${escapeHtml(j.runtime?.version || '')}</span></td>
    <td>${escapeHtml((j.created_at || '').replace('T',' '))}${j.finished_at ? `<br><span class="hint">结束 ${escapeHtml(j.finished_at.replace('T',' '))}</span>` : ''}${j.revoked_at ? `<br><span class="hint">撤销 ${escapeHtml(j.revoked_at.replace('T',' '))}</span>` : ''}${j.analysis ? `<br><span class="hint">原快照 ${escapeHtml(j.analysis.a_created_at || '')} → ${escapeHtml(j.analysis.b_created_at || '')}</span>` : ''}</td>
  </tr>`).join('') : '<tr><td colspan="4" class="hint" data-test="agent-history-empty">尚无解读任务；打开页面不会自动派发。</td></tr>';
  el('agent-history-more').hidden = nextOffset == null;
}
async function loadHistory({more=false, restore=false} = {}) {
  const request = beginRequest('agentHistory');
  el('agent-history-error').textContent = '';
  try {
    const data = await fetchJSON(`/api/analysis/history?limit=20&offset=${more ? nextOffset || 0 : 0}`);
    if (!request.current()) return;
    jobs = more ? [...new Map([...jobs,...data.jobs].map(j=>[j.job_id,j])).values()] : data.jobs;
    nextOffset = data.next_offset;
    if (restore && !openedJob) {
      let saved; try { saved = sessionStorage.getItem('fathom-agent-job'); } catch (_) {}
      const active = jobs.find(j=>!j.terminal);
      if (saved) {
        try { const original = await fetchJSON(`/api/analysis/jobs/${encodeURIComponent(saved)}`); if (!request.current()) return; openedJob = original.job; } catch (_) { /* 清理后不猜旧任务状态 */ }
      } else if (active) openedJob = active;
    }
    renderHistory(data.total);
    if (restore) updateRange();
  } catch (e) {
    if (request.current()) el('agent-history-error').textContent = `任务历史读取失败：${e.message}；可点击刷新任务历史重试。`;
  }
}
async function load() {
  const request = beginRequest('agentPage');
  panel.leave();
  try {
    const [cfg, data] = await Promise.all([fetchJSON('/api/config'),fetchJSON('/api/snapshots')]);
    if (!request.current()) return;
    const runtime = cfg.analysis?.runtime;
    const runtimeLabel = typeof runtime === 'string' ? runtime : runtime?.display_name || runtime?.id || '尚未选择';
    el('agent-engine-summary').textContent = `${cfg.analysis?.enabled ? '变化解读已启用' : '变化解读未启用'} · 引擎 ${runtimeLabel}${runtime?.version ? ` v${runtime.version}` : ''}。启用与配置在设置中管理；关闭引擎仍可查看保留的任务和原文。`;
    snapshots = [...data].sort((a,b)=>b.created_at.localeCompare(a.created_at) || b.id-a.id);
    let persisted; try { persisted = JSON.parse(sessionStorage.getItem('fathom-agent-range') || 'null'); } catch (_) {}
    fillSelectors();
    // 直达刷新恢复用户选点；仍须校验实际目录口径与时间。
    if (!el('agent-a').dataset.restored) {
      const saved = persisted;
      if (saved && snapshot(saved.b)) { el('agent-b').value = saved.b; fillSelectors(); if (snapshot(saved.a) && [...el('agent-a').options].some(o=>o.value===saved.a)) el('agent-a').value=saved.a; }
      el('agent-a').dataset.restored='1';
    }
    updateRange();
    await loadHistory({restore:true});
  } catch (e) { if (request.current()) el('agent-range-status').textContent = `Agent 页面读取失败：${e.message}；可点击刷新重试。`; }
}
export const agentPage = {
  id:'agent',load,
  init() {
    el('agent-settings').addEventListener('click',()=>{window.__fathomSettingsSectionPending='analysis';location.hash='#/settings';});
    el('agent-refresh').addEventListener('click',()=>load());
    el('agent-history-refresh').addEventListener('click',()=>loadHistory());
    el('agent-history-more').addEventListener('click',()=>loadHistory({more:true}));
    ['agent-a','agent-b'].forEach(id=>el(id).addEventListener('change',()=>{
      openedJob=null; try {sessionStorage.removeItem('fathom-agent-job');} catch (_) {}
      panel.leave(); if(id==='agent-b') fillSelectors({changedB:true}); else fillSelectors(); updateRange();
    }));
    el('agent-history-body').addEventListener('click',event=>{
      const btn=event.target.closest('[data-agent-job]'); if(!btn)return;
      openedJob=jobs.find(j=>j.job_id===btn.dataset.agentJob); if(!openedJob)return;
      try {sessionStorage.setItem('fathom-agent-job',openedJob.job_id);} catch (_) {}
      updateRange(); renderHistory(Number(el('agent-history-count').textContent.match(/\d+/)?.[0] || jobs.length));
    });
  },
  leave(){ ['agentPage','agentHistory'].forEach(invalidateRequest);panel.leave(); },
};
