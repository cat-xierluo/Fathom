/* Agent 中心：选已保存快照区间、纯读跨区间任务历史；配置仍由设置页负责。 */
import { fetchJSON, apiPost, beginRequest, invalidateRequest } from '../request.js';
import { escapeHtml } from '../format.js';
import { datasetKey } from '../dataset.js';
import { createAnalysisPanel } from '../components/analysis-panel.js';
import {
  runtimeCombinedStatus, runtimeDetectStatus, runtimeReasonDetail, runtimeIdentityStatus,
} from '../runtime-status.js';
let snapshots = [], jobs = [], nextOffset = null, openedJob = null;
/* 最近一次检测结果（本次会话内有效）：普通面只显示短状态与下一动作，
 * 技术细节（reason_code/detail/路径/版本）收进默认折叠的技术详情区。 */
let detectedRuntime = null;
// 自动恢复不能覆盖更晚的选点、历史点击或已确认的新尝试。
let viewRevision = 0;
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
  // plan 档闸门：datasetKey 对 plan 档按 plan_id 分档，故两侧 plan_id 非空
  // 且相等即同一计划双快照 → 放行构造预览。跨计划或 plan↔legacy 混搭继续拒绝，
  // 不降级为旧口径、不发送数据。（计量版本一致性由后端合同闸门兜底拒绝。）
  if (sa.plan_id || sb.plan_id) {
    if (sa.plan_id && sa.plan_id === sb.plan_id) return '';
    return '所选快照属于不同计划或身份，不能生成变化解读；不会降级为旧口径或发送数据。';
  }
  return '';
}
const panel = createAnalysisPanel({
  panelId:'agent-analysis-panel', bodyId:'agent-analysis-body', domain:'agentAnalysis',
  selection, readWhenDisabled:true, previewBlock:rangeReason,
  root:() => openedJob ? null : snapshot(selection().a)?.root,
  changed:() => loadHistory(),
  started(job) {
    viewRevision++; openedJob = job;
    try { sessionStorage.setItem('fathom-agent-job', job.job_id); } catch (_) {}
    renderOpenedTask();
    loadHistory();
  },
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
function renderOpenedTask() {
  el('agent-opened-task').textContent = openedJob ? `正在查看任务 ${openedJob.job_id} · 区间 #${openedJob.a_snapshot_id} → #${openedJob.b_snapshot_id}（与上方新任务选点独立）` : '';
}
function updateRange() {
  const reason = !snapshots.length ? '尚无快照。解读需要同一数据集内两个有效历史点；采集完成后可在这里选择。'
    : !el('agent-a').value ? '该数据集内不足两个按时间排序的快照。请切换对比快照；任务历史仍可查看。' : rangeReason();
  el('agent-range-status').textContent = reason || `当前区间 #${selection().a} → #${selection().b}；预览只整理本机事实，确认后才发送。`;
  renderOpenedTask();
  if (openedJob) { panel.showJob(openedJob); return; }
  if (reason) {
    panel.leave(); el('agent-analysis-panel').hidden = false;
    el('agent-analysis-body').innerHTML = `<p class="hint" data-test="agent-no-range">${escapeHtml(reason)}</p>`;
  } else panel.load();
}
const statusText = {starting:'启动中',running:'运行中',cancelling:'取消中',succeeded:'已完成',failed:'失败',cancelled:'已取消',timed_out:'超时',interrupted:'已中断'};
/* 失败/取消态的普通面说明（不含机器码、不含路径）：
 * 短状态只说「发生了什么」，具体原因码与引擎细节进折叠区。 */
const JOB_FAILURE_TEXT = {
  failed:'分析失败',
  cancelled:'分析已取消',
  timed_out:'分析超时',
  interrupted:'分析被中断',
};
/* 任务历史行的技术详情：机器码 reason_code、引擎版本与各时间戳都收在
 * 这里，默认折叠（ISS-203 普通面/技术详情分层合同）。 */
function jobTechDetails(j) {
  const lines = [];
  if (j.reason_code) lines.push(`原因码：${j.reason_code}`);
  if (j.runtime?.version) lines.push(`引擎版本：${j.runtime.version}`);
  if (j.runtime?.model) lines.push(`模型：${j.runtime.model}`);
  if (j.duration_ms != null) lines.push(`耗时：${Math.round(Number(j.duration_ms) / 1000)} 秒`);
  if (j.finished_at) lines.push(`结束：${j.finished_at.replace('T',' ')}`);
  if (j.revoked_at) lines.push(`撤销：${j.revoked_at.replace('T',' ')}`);
  if (!lines.length) return '';
  return `<details class="cfg-details" data-test="agent-job-tech-details"><summary>技术详情</summary><ul class="agent-job-tech-list">${
    lines.map(line => `<li>${escapeHtml(line)}</li>`).join('')
  }</ul></details>`;
}
function renderHistory(total) {
  el('agent-history-count').textContent = `共 ${total} 个任务；已显示 ${jobs.length} 个，在途优先。只展示本机仍保留的生命周期记录。`;
  el('agent-history-body').innerHTML = jobs.length ? jobs.map(j => `<tr>
    <td><button type="button" class="btn-mini" data-agent-job="${escapeHtml(j.job_id)}" aria-pressed="${openedJob?.job_id === j.job_id}">查看 #${escapeHtml(String(j.a_snapshot_id))} → #${escapeHtml(String(j.b_snapshot_id))}</button></td>
    <td>${escapeHtml(j.revoked ? '已撤销（正文不再保留）' : j.analysis?.expired ? '已过期（原文可读）' : statusText[j.status] || j.status)}${
      !j.revoked && !j.analysis?.expired && JOB_FAILURE_TEXT[j.status]
        ? `<br><span class="hint">${escapeHtml(JOB_FAILURE_TEXT[j.status])}</span>` : ''
    }${jobTechDetails(j)}</td>
    <td>${escapeHtml(j.runtime?.id || '未上报')}<br><span class="hint">${escapeHtml(j.runtime?.version || '')}</span></td>
    <td>${escapeHtml((j.created_at || '').replace('T',' '))}${j.analysis ? `<br><span class="hint">原快照 ${escapeHtml(j.analysis.a_created_at || '')} → ${escapeHtml(j.analysis.b_created_at || '')}</span>` : ''}</td>
  </tr>`).join('') : '<tr><td colspan="4" class="hint" data-test="agent-history-empty">尚无解读任务；打开页面不会自动派发。</td></tr>';
  el('agent-history-more').hidden = nextOffset == null;
}
async function loadHistory({more=false, restore=false} = {}) {
  const request = beginRequest('agentHistory');
  const restoreRevision = viewRevision;
  el('agent-history-error').textContent = '';
  try {
    const data = await fetchJSON(`/api/analysis/history?limit=20&offset=${more ? nextOffset || 0 : 0}`);
    if (!request.current()) return;
    jobs = more ? [...new Map([...jobs,...data.jobs].map(j=>[j.job_id,j])).values()] : data.jobs;
    nextOffset = data.next_offset;
    if (restore && restoreRevision === viewRevision && !openedJob) {
      let saved; try { saved = sessionStorage.getItem('fathom-agent-job'); } catch (_) {}
      const active = jobs.find(j=>!j.terminal);
      if (saved) {
        try { const original = await fetchJSON(`/api/analysis/jobs/${encodeURIComponent(saved)}`); if (!request.current()) return; if (restoreRevision === viewRevision) openedJob = original.job; } catch (_) { /* 清理后不猜旧任务状态 */ }
      } else if (active) openedJob = active;
    }
    renderHistory(data.total);
    if (restore && restoreRevision === viewRevision) updateRange();
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
    renderEngineSummary(cfg.analysis || null);
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

/* 普通面分层（ISS-203）：短状态 + 下一动作；技术细节默认折叠。
 * 引擎摘要只说「现在是什么状态、下一步做什么」，
 * 版本/路径/原因码等收进 <details class="cfg-details">。 */
function renderEngineSummary(cfgAnalysis) {
  const host = el('agent-engine-summary');
  if (!host) return;
  const runtime = cfgAnalysis?.runtime;
  const saved = typeof runtime === 'string' ? { id: runtime } : runtime;
  const status = runtimeCombinedStatus({
    enabled: Boolean(cfgAnalysis?.enabled),
    saved,
    info: detectedRuntime,
  });
  const identity = runtimeIdentityStatus(detectedRuntime);
  const parts = [`<span data-test="agent-engine-short-status">${escapeHtml(status.text)}</span>`];
  if (identity?.sendable) parts.push(`<span class="hint" data-test="agent-identity-status">${escapeHtml(identity.text)}</span>`);
  if (status.detail) parts.push(`<span class="hint" data-test="agent-engine-detail">${escapeHtml(status.detail)}</span>`);
  if (status.action) {
    parts.push(`<span class="hint" data-test="agent-engine-action">下一动作：${escapeHtml(status.action)}</span>`);
  }
  parts.push(agentTechDetailsHtml([
    saved?.id ? `已保存引擎：${saved.id}` : '',
    saved?.version ? `已保存版本：${saved.version}` : '',
    saved?.executable ? `已保存入口：${saved.executable}` : '',
    detectedRuntime?.version ? `检测版本：${detectedRuntime.version}` : '',
    detectedRuntime?.reason_code ? `原因码：${detectedRuntime.reason_code}` : '',
    detectedRuntime?.detail ? `检测说明：${detectedRuntime.detail}` : '',
    identity ? `身份等级：${identity.level} · ${identity.detail}` : '',
    identity ? `发送资格：${identity.sendable ? '可发起解读' : '暂不可发起解读'}` : '',
  ]));
  host.innerHTML = parts.filter(Boolean).join(' ');
}

/* 技术详情折叠容器（默认收起）。lines 为空时不渲染空折叠区。 */
function agentTechDetailsHtml(lines) {
  const items = lines.filter(Boolean);
  if (!items.length) return '';
  return `<details class="cfg-details" data-test="agent-tech-details"><summary>技术详情</summary><ul class="agent-tech-list">${
    items.map(line => `<li>${escapeHtml(line)}</li>`).join('')
  }</ul></details>`;
}

/* 检测结果（普通面只显示短状态与下一动作；原因码/路径进折叠区）。 */
function renderDetectResult(info, saved, moved) {
  const host = el('agent-runtime-status');
  if (!host) return;
  const status = moved
    ? { text: '引擎入口已变更', action: '前往设置重新选择', actionKind: 'settings',
        detail: '检测到的入口与已保存的入口不同，需要重新选择并授权。' }
    : runtimeDetectStatus(info);
  const identity = runtimeIdentityStatus(info);
  const parts = [`<span data-test="agent-detect-short-status">${escapeHtml(status.text)}</span>`];
  if (identity?.sendable) parts.push(`<span class="hint" data-test="agent-identity-status">${escapeHtml(identity.text)}</span>`);
  if (status.detail) parts.push(`<span class="hint">${escapeHtml(status.detail)}</span>`);
  if (status.action) {
    parts.push(`<span class="hint" data-test="agent-detect-action">下一动作：${escapeHtml(status.action)}</span>`);
  }
  const detailLine = runtimeReasonDetail(info?.reason_code);
  parts.push(agentTechDetailsHtml([
    info?.reason_code ? `原因码：${info.reason_code}` : '',
    detailLine ? `说明：${detailLine}` : '',
    info?.detail ? `检测说明：${info.detail}` : '',
    info?.version ? `检测版本：${info.version}` : '',
    info?.executable ? `检测入口：${info.executable}` : '',
    saved?.executable ? `已保存入口：${saved.executable}` : '',
    identity ? `身份等级：${identity.level} · ${identity.detail}` : '',
    identity ? `发送资格：${identity.sendable ? '可发起解读' : '暂不可发起解读'}` : '',
  ]));
  host.innerHTML = parts.filter(Boolean).join(' ');
}

async function detectSelectedRuntime() {
  const request = beginRequest('agentRuntime');
  const button = el('agent-detect');
  button.disabled = true;
  el('agent-runtime-status').textContent = '正在检测当前命令行版本与调用参数…';
  try {
    const cfg = await fetchJSON('/api/config');
    if (!request.current()) return;
    const selected = cfg.analysis?.runtime;
    const id = typeof selected === 'string' ? selected : selected?.id;
    if (!id) { el('agent-runtime-status').textContent = '尚未选择引擎，请前往设置配置。'; return; }
    const response = await apiPost('/api/analysis/runtimes/detect', {runtime_id:id});
    const data = await response.json();
    if (!request.current()) return;
    const info = data.runtimes?.[id];
    if (!info) throw new Error('未返回所选引擎的检测结果');
    const moved = selected?.executable && info.executable && info.executable !== selected.executable;
    detectedRuntime = info;
    renderDetectResult(info, selected, moved);
    renderEngineSummary(cfg.analysis);
  } catch (e) {
    if (request.current()) el('agent-runtime-status').textContent = `检测失败：${e.message}；可重试。`;
  } finally { if (request.current()) button.disabled = false; }
}

export const agentPage = {
  id:'agent',load,
  init() {
    el('agent-detect').addEventListener('click', detectSelectedRuntime);
    el('agent-settings').addEventListener('click',()=>{window.__fathomSettingsSectionPending='analysis';location.hash='#/settings';});
    el('agent-refresh').addEventListener('click',()=>load());
    el('agent-history-refresh').addEventListener('click',()=>loadHistory());
    el('agent-history-more').addEventListener('click',()=>loadHistory({more:true}));
    ['agent-a','agent-b'].forEach(id=>el(id).addEventListener('change',()=>{
      viewRevision++; openedJob=null; try {sessionStorage.removeItem('fathom-agent-job');} catch (_) {}
      panel.leave(); if(id==='agent-b') fillSelectors({changedB:true}); else fillSelectors(); updateRange();
    }));
    el('agent-history-body').addEventListener('click',event=>{
      const btn=event.target.closest('[data-agent-job]'); if(!btn)return;
      const chosen=jobs.find(j=>j.job_id===btn.dataset.agentJob); if(!chosen)return;
      viewRevision++; openedJob=chosen;
      try {sessionStorage.setItem('fathom-agent-job',openedJob.job_id);} catch (_) {}
      updateRange(); renderHistory(Number(el('agent-history-count').textContent.match(/\d+/)?.[0] || jobs.length));
    });
  },
  leave(){ ['agentPage','agentHistory','agentRuntime'].forEach(invalidateRequest);el('agent-detect').disabled=false;el('agent-runtime-status').textContent='';panel.leave(); },
};
