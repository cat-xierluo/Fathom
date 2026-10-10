/* 变化页与 Agent 页共用的解读生命周期；只参数化 DOM、区间和证据定位。 */
import { fetchJSON, beginRequest, invalidateRequest, apiPost, apiSend } from "../request.js";
import { fmtKB, fmtDelta, escapeHtml } from "../format.js";
import { icon } from "../../icons.js";
export function createAnalysisPanel(options) {
const domain = options.domain || "analysis";
const pollDomain = domain + "Poll";
/* ---------- AI 解读（ISS-035C）：净变化之后、变化表之前的七主态区 ----------
 *
 * 方案 §7 合同：
 * - 七主态：未启用 / 未分析 / 发送预览 / 运行中 / 完成 / 失败 / 过期；
 *   取消/超时/中断以明确原因和手动重试动作呈现；
 * - 预览展示区间、范围与截断（manifest）、发送对象、完整发送文本入口
 *   （prompt_text 展开）、本地保存说明；确认仅一次（幂等键会话生成）；
 * - 世代守卫：解读区独立请求域 + 终态前比对 job 的 a/b 与当前选择——
 *   改选快照后迟到的旧响应不得覆盖新区间的视图（反例 2）；
 * - 离页不隐式重发：离开页面只作废请求域与轮询，不取消已授权任务；
 *   重入按 sessionStorage 记录的原 job 恢复显示（GET jobs/{id} 纯读），
 *   刷新页面不会重复发送（POST jobs 计数不变，反例 4）；
 * - evidence_id 跳转到该报告保存的事实条目；能对位时在变化表内定位，
 *   脱敏路径无法对位时如实说明，不猜（@user 段）；
 * - AI 不可用不影响基础事实：本区任何失败都只发生在本区内部。
 */

const ANALYSIS_PANEL_ID = options.panelId;
const ANALYSIS_BODY_ID = options.bodyId;
const ANALYSIS_POLL_MS = 2000;

/* 会话状态：改选区间即重置（preview/job 与旧区间的关联一并作废）。 */
let analysisCfg = null;        // GET /api/config 的 analysis 块
let analysisPreview = null;    // 当前预览（previews 响应体）
let analysisJob = null;        // 最近 job 视图（运行中/终态）
let analysisJobTimer = null;   // 轮询句柄

/* job 失败 reason_code 的可读文案；未登记值原样展示不吞字。 */
const ANALYSIS_JOB_REASON = {
  runner_timed_out: "分析超时：超过单次时限已被终止，可重试",
  runner_cancelled: "分析已取消（已发送部分无法撤回）",
  runner_nonzero_exit: "引擎命令行异常退出",
  runner_spawn_failed: "引擎无法启动（可能已被移动或损坏）",
  runner_output_limit: "引擎输出超出限额，结果不可信",
  runner_decode_error: "引擎输出无法按 UTF-8 解码",
  parse_failed_bad_json: "引擎返回内容不是有效 JSON",
  parse_failed_not_result: "引擎返回缺少 result 结构",
  parse_failed_missing_field: "引擎返回缺少必需字段",
  app_error: "引擎报告了应用层错误",
  cancelled_before_start: "分析在开始前被取消",
  owner_exit: "服务中断：本次分析未完成，不会自动重试",
  startup_aborted: "分析在启动阶段中断，未产生任何解读",
};

/* 事实条目 kind 的中文说明（与 facts 包 entry_legend 同口径）。 */
const ANALYSIS_ENTRY_KIND = {
  measured: "已测量（两次都被记录，增减为实测）",
  first_recorded: "首次进入统计（刚越过入库阈值；不是文件系统新建）",
  unrecorded: "未记录（可能跌破阈值、权限受限或已被移除；不是删除证据）",
};

function analysisPanelEl() { return document.getElementById(ANALYSIS_PANEL_ID); }
function analysisBodyEl() { return document.getElementById(ANALYSIS_BODY_ID); }

function analysisSel() { return options.selection(); }

function sameAnalysisRange(obj, sel) {
  return obj && String(obj.a_snapshot_id ?? obj.a) === String(sel.a) &&
    String(obj.b_snapshot_id ?? obj.b) === String(sel.b);
}

/** 读回本区间当前尝试的幂等记录（含是否已终态）。 */
function analysisIdemRecord(sel) {
  try {
    const raw = sessionStorage.getItem(`fathom-aidem:${sel.a}->${sel.b}`);
    if (!raw) return null;
    const saved = JSON.parse(raw);
    return saved && typeof saved.key === "string" ? saved : null;
  } catch (_) { return null; }  /* 隐私模式等场景不可用 */
}

/** 记下本次尝试的 job，供刷新/离页后按 job_id 纯读重入。 */
function saveAnalysisAttempt(sel, { key, digest, job, replayed }) {
  try {
    sessionStorage.setItem(`fathom-aidem:${sel.a}->${sel.b}`, JSON.stringify({
      key, digest,
      job_id: job?.job_id ?? null,
      status: job?.status ?? null,
      terminal: Boolean(job?.terminal),
    }));
  } catch (_) { /* 隐私模式等场景：仅当前会话内可恢复 */ }
}

/** 会话幂等键（确认仅一次的载体）：按区间存 sessionStorage；
 * 绑定 request_digest——digest 变了（新预览）就换新键，避免
 * idempotency_conflict。
 *
 * 键的生命周期绑定「一次用户授权的尝试」，而不是区间（ISS-128）：
 *  - 同 digest 且上次尝试**仍在途** → 复用该键。这是传输层重试与双击，
 *    它们属于同一次尝试，必须收敛到同一份派发。
 *  - 同 digest 但上次尝试**已终态** → 新的用户尝试（重跑、取消后重试、
 *    撤销后再次分析、换引擎跑同一事实）发新键。否则后端只会重放旧终态，
 *    用户点了「重新分析」却看到上一次的旧结果。
 * 刷新重入不走这里：它用 analysisSavedJobId 做纯 GET，不会重复派发。 */
function analysisIdemKey(sel, digest, { create } = {}) {
  const saved = analysisIdemRecord(sel);
  if (saved && saved.digest === digest && !saved.terminal) return saved.key;
  if (!create) return null;
  const key = (crypto.randomUUID ? crypto.randomUUID() : `idem-${Date.now()}-${Math.random()}`);
  saveAnalysisAttempt(sel, { key, digest, job: null, replayed: false });
  return key;
}

/** 改选区间/清空结果时重置会话态；hide=true 同时收起解读区。 */
function resetAnalysisSession({ hide = false } = {}) {
  clearTimeout(analysisJobTimer);
  analysisJobTimer = null;
  analysisPreview = null;
  analysisJob = null;
  if (hide) {
    const panel = analysisPanelEl();
    if (panel) panel.hidden = true;
  }
}

/** 解读区加载失败/离线的就地重试（基础事实不受影响）。 */
function showAnalysisRetry(message, testId, retryLoad = loadAnalysisPanel) {
  const el = analysisBodyEl();
  el.replaceChildren(document.createTextNode(message));
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "diff-retry";
  retry.textContent = "重试";
  retry.setAttribute("aria-label", "重新加载 AI 解读状态");
  if (testId) retry.setAttribute("data-test", testId);
  retry.addEventListener("click", retryLoad);
  el.appendChild(retry);
}

/* ----- 渲染入口：state = {state: 七态之一, ...载荷} ----- */

function renderAnalysis(state) {
  const body = analysisBodyEl();
  const panel = analysisPanelEl();
  if (!body || !panel) return;
  panel.hidden = false;
  switch (state.state) {
    case "disabled": {
      body.innerHTML = `
        <p class="hint" data-test="analysis-state-disabled">AI 解读未启用：下方变化表等基础事实照常可用，不会发送任何数据。</p>
        <div class="perm-link-row">
          <button type="button" class="btn" data-test="analysis-goto-settings">${icon("settings", 14)} 前往设置开启</button>
        </div>`;
      body.querySelector("[data-test='analysis-goto-settings']").addEventListener("click", () => {
        window.__fathomSettingsSectionPending = "analysis";
        location.hash = "#/settings";
      });
      return;
    }
    case "idle": {
      const sel = analysisSel();
      body.innerHTML = `
        <p class="hint" data-test="analysis-state-idle">区间 #${escapeHtml(sel.a)} → #${escapeHtml(sel.b)} 还没有 AI 解读。
          生成发送预览后，你可以先查看将要发送的完整内容再确认；不确认就不会发送。</p>
        <div class="perm-link-row">
          <button type="button" class="btn" data-test="analysis-preview-btn">${icon("sparkles", 14)} 生成发送预览</button>
        </div>
        <p class="hint">生成预览只在本机整理事实包（零外传）；确认后才交给所选引擎分析。</p>`;
      body.querySelector("[data-test='analysis-preview-btn']").addEventListener("click", startAnalysisPreview);
      return;
    }
    case "preview": return renderAnalysisPreview(state);
    case "running": {
      const job = state.job || {};
      const cancelling = state.cancelling;
      body.innerHTML = `
        <p class="hint" data-test="analysis-state-running">
          <span class="dr-loading" data-dr-spin aria-hidden="true"></span>
          正在分析（区间 #${escapeHtml(String(job.a_snapshot_id))} → #${escapeHtml(String(job.b_snapshot_id))}，任务 ${escapeHtml(String(job.job_id || "").slice(0, 8))}）…
        </p>
        <p class="hint">关闭页面不会取消分析；回到本页会自动恢复显示。取消只能停止后续处理，不能撤回已发送给引擎的数据。</p>
        <div class="perm-link-row">
          <button type="button" class="btn" data-test="analysis-cancel-btn"${cancelling ? " disabled" : ""}>${cancelling ? "正在取消…" : "取消分析"}</button>
        </div>`;
      body.querySelector("[data-test='analysis-cancel-btn']").addEventListener("click", cancelAnalysisJob);
      return;
    }
    case "failure": return renderAnalysisFailure(state);
    case "done":
    case "expired": return renderAnalysisResult(state);
    case "pollError": {
      body.innerHTML = `
        <p class="hint" data-test="analysis-poll-error">分析状态查询失败：${escapeHtml(state.error?.message || "")}；任务仍在后台，稍后自动恢复或手动重试。</p>
        <div class="perm-link-row">
          <button type="button" class="btn" data-test="analysis-poll-retry">重试查询</button>
        </div>`;
      body.querySelector("[data-test='analysis-poll-retry']").addEventListener("click", () => {
        if (analysisJob?.job_id) scheduleAnalysisPoll(analysisJob.job_id);
        else loadAnalysisPanel();
      });
      return;
    }
    default:
      body.innerHTML = `<p class="hint">解读状态未知，请刷新。</p>`;
  }
}

/* ----- 未启用 / 未分析 ----- */

/* ----- 发送预览（方案 §4.2：预览=将发送的不可变请求） ----- */

function renderAnalysisPreview({ preview }) {
  const body = analysisBodyEl();
  const m = preview.manifest || {};
  const sel = m.snapshots || {};
  const sampling = m.sampling || {};
  const truncation = m.truncation || {};
  const runtime = preview.runtime || {};
  const aDate = String(sel.a?.created_at || "").slice(0, 16).replace("T", " ");
  const bDate = String(sel.b?.created_at || "").slice(0, 16).replace("T", " ");
  const dataset = m.dataset || {};
  const omits = [];
  if (Number(sampling.omitted) > 0) {
    omits.push(`候选 ${sampling.total_candidates} 条，取样 ${sampling.selected} 条，另有 ${sampling.omitted} 条未进入本包`);
  }
  if (truncation.utf8_truncated) {
    omits.push(`发送文本超出字节上限，已截断 ${truncation.omitted_entries || "?"} 条`);
  }
  body.innerHTML = `
    <div data-test="analysis-state-preview">
      <p class="hint" data-test="analysis-preview-range">将分析区间：<strong>#${escapeHtml(String(sel.a?.snapshot_id))}（${escapeHtml(aDate)}）→ #${escapeHtml(String(sel.b?.snapshot_id))}（${escapeHtml(bDate)}）</strong></p>
      <p class="hint" data-test="analysis-preview-scope">范围：根 <code>${escapeHtml(dataset.root_display || "?")}</code> ·
        入库阈值 ${dataset.min_kb == null ? "未知" : escapeHtml(String(dataset.min_kb)) + " KB"} ·
        排除掩码 ${(dataset.exclude_names || []).length ? escapeHtml((dataset.exclude_names || []).join("; ")) : "无"} ·
        单位 ${escapeHtml(m.units || "KiB")}（子目录行与根累计有重叠，行值不可相加）</p>
      <p class="hint" data-test="analysis-preview-truncation">${omits.length
        ? `截断与取样：${escapeHtml(omits.join("；"))}。模型只看到取样子集。`
        : "全部过阈值候选已进入本包，无取样省略。"}</p>
      <p class="hint" data-test="analysis-preview-target">发送对象：本机命令行
        <strong>${escapeHtml(runtime.display_name || runtime.id || "?")}</strong>${runtime.version ? `（版本 ${escapeHtml(runtime.version)}）` : ""}——
        它可能把数据发送给它配置的模型服务；账号与额度由该命令行管理。</p>
      <p class="hint" data-test="analysis-preview-retain">解读结果与本次批准发送的事实包会保存在本机运行目录，可随时撤销；
        撤销不能清除该命令行或模型服务自身的留存。</p>
      <details class="cfg-details" data-test="analysis-preview-prompt-details">
        <summary>查看完整发送文本</summary>
        <pre class="analysis-prompt" data-test="analysis-prompt-text">${escapeHtml(preview.prompt_text || "")}</pre>
      </details>
      <div class="exclude-actions">
        <button type="button" class="btn primary" data-test="analysis-confirm-btn">确认并发送分析</button>
        <button type="button" class="btn" data-test="analysis-preview-cancel">取消（不发送）</button>
      </div>
      <p class="hint">预览 ${escapeHtml(String(preview.expires_in_s ?? "?"))} 秒后过期；确认只发送这一次，不会自动重发。</p>
    </div>`;
  body.querySelector("[data-test='analysis-confirm-btn']").addEventListener("click", () => confirmAnalysisSend(preview));
  body.querySelector("[data-test='analysis-preview-cancel']").addEventListener("click", () => {
    analysisPreview = null;
    renderAnalysis({ state: "idle" });
  });
}

/** 确认发送（仅一次）：按钮点击即禁用；幂等键按会话/区间取或建；
 * 同键同 digest 重入返回同一 job（后端幂等），不会重复发送。 */
async function confirmAnalysisSend(preview) {
  const body = analysisBodyEl();
  const btn = body.querySelector("[data-test='analysis-confirm-btn']");
  if (btn) { btn.disabled = true; btn.textContent = "已确认，正在提交…"; }
  const sel = analysisSel();
  if (!sameAnalysisRange({ a: preview.a?.snapshot_id, b: preview.b?.snapshot_id }, sel)) {
    // 用户在预览展示期间改选了区间：旧预览不再代表当前视图，直接作废
    resetAnalysisSession();
    loadAnalysisPanel();
    return;
  }
  const digest = preview.request_digest;
  const key = analysisIdemKey(sel, digest, { create: true });
  const request = beginRequest(domain);
  try {
    const r = await apiPost("/api/analysis/jobs", {
      preview_id: preview.preview_id,
      request_digest: digest,
      idempotency_key: key,
    });
    if (!request.current()) return;
    const data = await r.json();
    // 记录原 job 供刷新/离页后重入恢复（GET jobs/{id} 纯读）。
    saveAnalysisAttempt(sel, { key, digest, job: data.job, replayed: data.replayed });
    analysisPreview = null;
    analysisJob = data.job;
    // 重放（replayed=true）不等于「正在运行」（ISS-128）：后端可能返回的是
    // 一个**已终态**的旧 job——此前这里无条件渲染 running 且不轮询，页面会
    // 永远停在运行中。现在按 job 自身的终态标记分流。
    if (data.job?.terminal) {
      await onAnalysisJobTerminal(data.job);
      return;
    }
    renderAnalysis({ state: "running", job: data.job });
    // 重放在途说明别处已有一次同键派发（典型是本会话较早的尝试）：继续轮询
    // 把它收敛到终态，而不是当作已经结束。
    scheduleAnalysisPoll(data.job.job_id);
  } catch (e) {
    if (!request.current()) return;
    handleAnalysisSendError(e);
  }
}

function handleAnalysisSendError(e) {
  const code = e.reason_code;
  if (code === "preview_expired" || code === "preview_not_found" || code === "preview_stale") {
    analysisPreview = null;
    renderAnalysis({ state: "idle" });
    const body = analysisBodyEl();
    const note = document.createElement("p");
    note.className = "hint cfg-error";
    note.setAttribute("data-test", "analysis-preview-stale");
    note.textContent = code === "preview_stale"
      ? "设置或数据已变化，原预览失效；请重新生成预览。"
      : "预览已过期（有效期 5 分钟）；请重新生成预览。";
    body.prepend(note);
    return;
  }
  if (code === "analysis_busy") {
    renderAnalysis({ state: "idle" });
    const body = analysisBodyEl();
    const note = document.createElement("p");
    note.className = "hint cfg-error";
    note.setAttribute("data-test", "analysis-busy");
    note.textContent = "已有分析在进行中（同一时间只允许一个）；等待其结束后再试，不会排队。";
    body.prepend(note);
    return;
  }
  if (code === "analysis_disabled") {
    renderAnalysis({ state: "disabled" });
    return;
  }
  // 其余（403 引擎不可用 / 404 快照失效 / 400 口径）：可读原因 + 回未分析
  const sel = analysisSel();
  renderAnalysis({ state: "idle" });
  const body = analysisBodyEl();
  const note = document.createElement("p");
  note.className = "hint cfg-error";
  note.setAttribute("data-test", "analysis-send-error");
  note.textContent = `无法开始分析：${e.message}`;
  body.prepend(note);
}

/* ----- 运行中：轮询（离页作废；重入恢复） ----- */

function scheduleAnalysisPoll(jobId) {
  clearTimeout(analysisJobTimer);
  analysisJobTimer = setTimeout(async () => {
    const request = beginRequest(pollDomain);
    try {
      const r = await fetchJSON(`/api/analysis/jobs/${encodeURIComponent(jobId)}`);
      if (!request.current()) return;
      const job = r.job;
      // 区间世代守卫：job 不属于当前选择的区间时不写入（迟到响应）
      if (!sameAnalysisRange(job, analysisSel())) return;
      analysisJob = job;
      if (job.terminal) {
        await onAnalysisJobTerminal(job);
        return;
      }
      renderAnalysis({ state: "running", job, cancelling: job.status === "cancelling" });
      scheduleAnalysisPoll(jobId);
    } catch (e) {
      if (!request.current()) return;
      renderAnalysis({ state: "pollError", error: e });
    }
  }, ANALYSIS_POLL_MS);
}

async function onAnalysisJobTerminal(job) {
  // 无论哪种终态，都把本次尝试标记为「已结束」：下一次用户发起的尝试会因此
  // 拿到新键，不会重放这一次的旧终态（ISS-128）。此前只在部分状态下清键，
  // 且恰好漏掉 succeeded——成功保留旧键，用户点「重新分析」只会拿到上一次的
  // 结果。job_id 仍保留，刷新重入（纯 GET）不受影响。
  markAnalysisAttemptTerminal(job);
  options.changed?.();
  if (job.status === "succeeded") {
    // 终态成功：以 analyses 读取层为准（含过期评估），不自行渲染 stdout
    await loadAnalysisPanel();
    return;
  }
  renderAnalysis({ state: "failure", job });
}

/** 把指定区间（默认当前区间）的尝试标记为已终态；保留 job_id 供重入。 */
function markAnalysisAttemptTerminal(job, sel = analysisSel()) {
  const saved = analysisIdemRecord(sel);
  if (!saved) return;
  try {
    sessionStorage.setItem(`fathom-aidem:${sel.a}->${sel.b}`, JSON.stringify({
      ...saved,
      job_id: job?.job_id ?? saved.job_id ?? null,
      status: job?.status ?? null,
      terminal: true,
    }));
  } catch (_) { /* 隐私模式等场景：仅当前会话内可恢复 */ }
}

async function cancelAnalysisJob() {
  const job = analysisJob;
  if (!job?.job_id) return;
  renderAnalysis({ state: "running", job, cancelling: true });
  const request = beginRequest(domain);
  try {
    await apiPost(`/api/analysis/jobs/${encodeURIComponent(job.job_id)}/cancel`, {});
    if (!request.current()) return;
    // 取消受理后由轮询收敛终态（cancelling → cancelled）
    scheduleAnalysisPoll(job.job_id);
  } catch (e) {
    if (!request.current()) return;
    if (e.status === 409) {
      // 终态竞争：任务已结束，取消不再生效——按最终状态呈现（响应已带 job）
      const finalJob = e.body?.job;
      if (finalJob && finalJob.terminal) {
        analysisJob = finalJob;
        await onAnalysisJobTerminal(finalJob);
        return;
      }
      scheduleAnalysisPoll(job.job_id);
      return;
    }
    renderAnalysis({ state: "running", job });
    const body = analysisBodyEl();
    const note = document.createElement("p");
    note.className = "hint cfg-error";
    note.textContent = `取消请求失败：${e.message}；可再次尝试。`;
    body.appendChild(note);
  }
}

/* ----- 失败 / 过期 / 完成 ----- */

function renderAnalysisFailure({ job }) {
  const body = analysisBodyEl();
  const sel = analysisSel();
  const status = job?.status || "failed";
  const code = job?.reason_code;
  const reason = ANALYSIS_JOB_REASON[code] || code || "未知原因";
  const rangeText = job && job.a_snapshot_id != null
    ? `#${escapeHtml(String(job.a_snapshot_id))} → #${escapeHtml(String(job.b_snapshot_id))}`
    : `#${escapeHtml(sel.a)} → #${escapeHtml(sel.b)}`;
  const headline = status === "cancelled" ? "分析已取消"
    : status === "timed_out" ? "分析超时"
      : status === "interrupted" ? "分析被中断"
        : status === "cancelling" ? "正在取消" : "分析失败";
  body.innerHTML = `
    <div data-test="analysis-state-failure">
      <p class="hint" data-test="analysis-failure-headline"><strong>${escapeHtml(headline)}</strong></p>
      <p class="hint" data-test="analysis-failure-reason">原因：${escapeHtml(reason)}${code ? `（${escapeHtml(code)}）` : ""}</p>
      <p class="hint">失败不会产生或改动任何解读记录；区间 ${rangeText} 的基础事实不受影响。</p>
      <div class="perm-link-row">
        <button type="button" class="btn" data-test="analysis-retry-btn">${icon("activity", 14)} 重新生成预览</button>
      </div>
    </div>`;
  body.querySelector("[data-test='analysis-retry-btn']").addEventListener("click", startAnalysisPreview);
}

/** 完成/过期共用的正文渲染（方案 §7：重点/推断/限制/查看方向 + 元信息）。
 * expired=true 时先给原因横幅，仍渲染已保存原文与证据。 */
function renderAnalysisResult({ state, record }) {
  const body = analysisBodyEl();
  const result = record.result || {};
  const runtime = record.runtime || {};
  const facts = record.facts || {};
  const aId = record.a?.snapshot_id, bId = record.b?.snapshot_id;
  const aDate = String(record.a?.created_at || "").slice(0, 16).replace("T", " ");
  const bDate = String(record.b?.created_at || "").slice(0, 16).replace("T", " ");
  const expiredReason = {
    snapshot_replaced: "原区间快照已被同日新快照替换",
    snapshot_pruned: "原区间快照已按保留策略淘汰",
    dataset_unverifiable: "数据集口径（根/阈值/排除）已变化，无法再验证原区间",
  }[record.expired_reason] || record.expired_reason || "原因未上报";
  const findings = Array.isArray(result.findings) ? result.findings : [];
  const limitations = Array.isArray(result.limitations) ? result.limitations : [];
  const inspectNext = Array.isArray(result.inspect_next) ? result.inspect_next : [];
  const evidenceBtn = (eid, label) => `
    <button type="button" class="btn-mini" data-test="analysis-evidence-btn" data-evidence-id="${escapeHtml(eid)}"
            aria-label="查看证据 ${escapeHtml(eid)}" title="查看该编号保存的事实">${escapeHtml(label || eid)}</button>`;
  body.innerHTML = `
    <div data-test="analysis-state-${state === "expired" ? "expired" : "done"}">
      ${state === "expired" ? `
      <p class="hint analysis-expired-note" data-test="analysis-expired-note">
        <strong>该解读已过期</strong>：${escapeHtml(expiredReason)}。
        以下仍可查看原日期（${escapeHtml(aDate)} → ${escapeHtml(bDate)}）保存的原文与证据，但不代表当前数据。</p>` : ""}
      <p class="hint" data-test="analysis-meta">
        ${escapeHtml(runtime.id || "引擎")}${runtime.version ? ` v${escapeHtml(runtime.version)}` : ""} ·
        模型：${escapeHtml(runtime.model || "未上报")} ·
        生成于 ${escapeHtml(String(record.created_at || "").slice(0, 16).replace("T", " "))} ·
        区间 #${escapeHtml(String(aId))} → #${escapeHtml(String(bId))} ·
        解读版本 ${escapeHtml(record.prompt_version || "?")}
      </p>
      <p class="analysis-summary" data-test="analysis-summary">${escapeHtml(result.summary || "（引擎未给出摘要）")}</p>
      ${findings.length ? `
      <h3 class="analysis-subhead">重点与推断</h3>
      <ul class="analysis-findings" data-test="analysis-findings">
        ${findings.map((f) => `
        <li class="analysis-finding">
          <span class="quality-chip ${f.certainty === "hypothesis" ? "warn" : "ok"}" data-test="analysis-certainty">${f.certainty === "hypothesis" ? "推断" : "已观察"}</span>
          <span class="analysis-finding-text">${escapeHtml(f.text || "")}</span>
          <span class="analysis-finding-evidence">${(f.evidence_ids || []).map((eid) => evidenceBtn(eid)).join(" ")}</span>
        </li>`).join("")}
      </ul>` : `<p class="hint" data-test="analysis-no-findings">本次解读没有给出有依据的重点（引擎应说明原因，见下方限制）。</p>`}
      ${limitations.length ? `
      <h3 class="analysis-subhead">限制</h3>
      <ul class="analysis-limitations" data-test="analysis-limitations">
        ${limitations.map((t) => `<li>${escapeHtml(String(t))}</li>`).join("")}
      </ul>` : ""}
      ${inspectNext.length ? `
      <h3 class="analysis-subhead">查看方向</h3>
      <ul class="analysis-inspect" data-test="analysis-inspect-next">
        ${inspectNext.map((it) => `
        <li>${evidenceBtn(it.evidence_id)} <span>${escapeHtml(it.reason || "")}</span></li>`).join("")}
      </ul>` : ""}
      <div class="perm-link-row" data-test="analysis-result-actions">
        <button type="button" class="btn" data-test="analysis-rerun-btn">${icon("activity", 14)} 重跑</button>
        ${state === "done" ? `<button type="button" class="btn" data-test="analysis-revoke-btn">${icon("trash", 14)} 撤销此解读</button>` : ""}
      </div>
      <div data-test="analysis-evidence-card-host"></div>
      ${state === "expired" ? `<div data-test="analysis-revoke-expired-host"></div>` : ""}
    </div>`;
  body.querySelectorAll("[data-test='analysis-evidence-btn']").forEach((btn) => {
    btn.addEventListener("click", () => showAnalysisEvidence(btn.dataset.evidenceId, record));
  });
  body.querySelector("[data-test='analysis-rerun-btn']").addEventListener("click", startAnalysisPreview);
  const revokeBtn = body.querySelector("[data-test='analysis-revoke-btn']");
  if (revokeBtn) revokeBtn.addEventListener("click", () => confirmAnalysisRevoke(record));
  if (state === "expired") {
    const host = body.querySelector("[data-test='analysis-revoke-expired-host']");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn";
    btn.setAttribute("data-test", "analysis-revoke-expired-btn");
    btn.innerHTML = `${icon("trash", 14)} 撤销此历史解读`;
    btn.addEventListener("click", () => confirmAnalysisRevoke(record));
    host?.appendChild(btn);
  }
}

/** 证据条目卡：显示该编号在已保存事实包中的完整事实；能对位时提供
 * 「在变化表中定位」。脱敏路径（@user 段）无法对位时如实说明。 */
function showAnalysisEvidence(evidenceId, record) {
  const host = analysisBodyEl().querySelector("[data-test='analysis-evidence-card-host']");
  if (!host) return;
  const entry = ((record.facts && record.facts.entries) || [])
    .find((x) => x.evidence_id === evidenceId);
  if (!entry) {
    host.innerHTML = `<p class="hint cfg-error" data-test="analysis-evidence-missing">证据 ${escapeHtml(evidenceId)} 不在已保存的事实包中（可能是引擎引用了无效编号，已被验证器拒绝的历史不会出现；如反复出现请重跑）。</p>`;
    return;
  }
  const kindText = ANALYSIS_ENTRY_KIND[entry.kind] || entry.kind;
  const deltaText = entry.delta_kb == null ? "—" : fmtDelta(entry.delta_kb);
  const oldText = entry.old_kb == null ? "—" : fmtKB(entry.old_kb);
  const newText = entry.new_kb == null ? "—" : fmtKB(entry.new_kb);
  const tablePath = analysisEvidenceTablePath(entry);
  host.innerHTML = `
    <div class="analysis-evidence-card" data-test="analysis-evidence-card">
      <div class="analysis-evidence-head">
        <strong>证据 ${escapeHtml(entry.evidence_id)}</strong>
        <span class="hint">${escapeHtml(kindText)}</span>
        <button type="button" class="btn-mini" data-test="analysis-evidence-close" aria-label="关闭证据卡">${icon("x", 12)}</button>
      </div>
      <p class="analysis-evidence-path" data-test="analysis-evidence-path">${escapeHtml(entry.path)}</p>
      <p class="hint">${entry.old_kb == null ? "" : `${escapeHtml(oldText)} → `}
        ${entry.new_kb == null ? "" : escapeHtml(newText)}
        ${entry.delta_kb == null ? "" : ` · 变化 ${escapeHtml(deltaText)}`}</p>
      ${tablePath
        ? `<div class="perm-link-row"><button type="button" class="btn" data-test="analysis-evidence-locate">在变化表中定位</button></div>`
        : `<p class="hint">该路径含脱敏标记或不在当前变化表中（可能未进取样集），不以表行反推事实。</p>`}
    </div>`;
  host.querySelector("[data-test='analysis-evidence-close']").addEventListener("click", () => { host.innerHTML = ""; });
  const locateBtn = host.querySelector("[data-test='analysis-evidence-locate']");
  if (locateBtn) locateBtn.addEventListener("click", () => locateEvidenceInTable(entry));
}

/** 把 facts 的脱敏路径换算成变化表的绝对路径；无法对位（@user 段、
 * 无根路径）返回 null——不猜。 */
function analysisEvidenceTablePath(entry) {
  const root = options.root?.();
  const p = String(entry.path || "");
  if (!root) return null;
  if (p === "@root") return root;
  if (p.startsWith("@root/")) {
    const rel = p.slice("@root/".length);
    if (rel.includes("@user") || rel.includes("@root")) return null;
    return root.replace(/\/+$/, "") + "/" + rel;
  }
  return null;
}

function locateEvidenceInTable(entry) {
  const target = analysisEvidenceTablePath(entry);
  if (!target) return;
  options.locate?.(target);
}

/** 撤销确认层：删除本应用保存的正文与事实包；不承诺清除引擎/服务留存。 */
function confirmAnalysisRevoke(record) {
  const host = analysisBodyEl().querySelector("[data-test='analysis-evidence-card-host']");
  if (!host) return;
  host.innerHTML = `
    <div class="analysis-evidence-card" data-test="analysis-revoke-confirm">
      <p class="hint exclude-warning">即将撤销此解读：删除本应用保存的解读正文与对应事实包。
        已发送给引擎的数据无法撤回；生命周期审计记录会保留撤销时间。</p>
      <div class="exclude-actions">
        <button type="button" class="btn primary" data-test="analysis-revoke-yes">确认撤销</button>
        <button type="button" class="btn" data-test="analysis-revoke-no">取消</button>
      </div>
    </div>`;
  host.querySelector("[data-test='analysis-revoke-no']").addEventListener("click", () => { host.innerHTML = ""; });
  host.querySelector("[data-test='analysis-revoke-yes']").addEventListener("click", async () => {
    const request = beginRequest(domain);
    try {
      await apiSend("DELETE", `/api/analyses/${encodeURIComponent(record.id)}`);
      if (!request.current()) return;
      renderAnalysis({ state: "idle" });
      options.changed?.();
    } catch (e) {
      if (!request.current()) return;
      host.innerHTML = `<p class="hint cfg-error" data-test="analysis-revoke-error">撤销失败：${escapeHtml(e.message)} 解读仍保留，可重试。</p>`;
    }
  });
}

/* ----- 预览生成与主状态机 ----- */

async function startAnalysisPreview() {
  const sel = analysisSel();
  if (!sel.a || !sel.b) return;
  const request = beginRequest(domain);
  const body = analysisBodyEl();
  body.innerHTML = `<p class="hint" data-test="analysis-preview-loading"><span class="dr-loading" data-dr-spin aria-hidden="true"></span>正在生成发送预览（只在本机整理事实包）…</p>`;
  try {
    const reason = options.previewBlock?.();
    if (reason) throw new Error(reason);
    const r = await apiPost("/api/analysis/previews", { a: Number(sel.a), b: Number(sel.b) });
    if (!request.current()) return;
    const preview = await r.json();
    analysisPreview = preview;
    renderAnalysis({ state: "preview", preview });
  } catch (e) {
    if (!request.current()) return;
    const code = e.reason_code;
    if (code === "analysis_disabled") { renderAnalysis({ state: "disabled" }); return; }
    if (code && code.startsWith("runtime_")) {
      body.innerHTML = `
        <p class="hint cfg-error" data-test="analysis-runtime-blocked">无法生成预览：${escapeHtml(e.message)}</p>
        <div class="perm-link-row">
          <button type="button" class="btn" data-test="analysis-goto-settings-2">${icon("settings", 14)} 前往设置重新检测</button>
        </div>`;
      body.querySelector("[data-test='analysis-goto-settings-2']").addEventListener("click", () => {
        window.__fathomSettingsSectionPending = "analysis";
        location.hash = "#/settings";
      });
      return;
    }
    if (code === "snapshot_not_found") {
      body.innerHTML = `<p class="hint cfg-error" data-test="analysis-snapshot-missing">所选快照已不可用（可能已被替换或淘汰）；请改选其他日期。基础事实表不受影响。</p>`;
      return;
    }
    body.innerHTML = `<p class="hint cfg-error" data-test="analysis-preview-error">预览生成失败：${escapeHtml(e.message)}</p>`;
  }
}

/** 主状态机：跟随当前 a/b 判定七主态。所有写入前经请求域守卫；
 * 改选区间会开启新域，旧响应全部作废（世代守卫，反例 2）。 */
async function loadAnalysisPanel() {
  const sel = analysisSel();
  if (!sel.a || !sel.b) {
    resetAnalysisSession({ hide: true });
    return;
  }
  const request = beginRequest(domain);
  clearTimeout(analysisJobTimer);
  analysisJobTimer = null;
  analysisPreview = null;
  analysisJob = null;
  const panel = analysisPanelEl();
  const body = analysisBodyEl();
  panel.hidden = false;
  body.innerHTML = `<p class="hint"><span class="dr-loading" data-dr-spin aria-hidden="true"></span>解读状态加载中…</p>`;

  let cfg;
  try {
    const c = await fetchJSON("/api/config");
    if (!request.current()) return;
    cfg = c.analysis;
  } catch (e) {
    if (!request.current()) return;
    showAnalysisRetry(e.status === 0
      ? "无法连接本地服务，AI 解读状态暂不可用；基础事实不受影响。"
      : `AI 解读状态加载失败：${e.message}`);
    return;
  }
  analysisCfg = cfg;
  if ((!cfg || !cfg.enabled) && !options.readWhenDisabled) {
    renderAnalysis({ state: "disabled" });
    return;
  }

  let records = [];
  try {
    const r = await fetchJSON(`/api/analyses?a=${encodeURIComponent(sel.a)}&b=${encodeURIComponent(sel.b)}`);
    if (!request.current()) return;
    records = Array.isArray(r.analyses) ? r.analyses : [];
  } catch (e) {
    if (!request.current()) return;
    showAnalysisRetry(e.status === 0
      ? "无法连接本地服务，AI 解读状态暂不可用；基础事实不受影响。"
      : `AI 解读记录加载失败：${e.message}`);
    return;
  }
  // 在途任务优先于已保存的旧报告（ISS-135）：新窗口/新会话里 sessionStorage
  // 是空的，若先渲染历史记录，正在跑的分析就被旧报告挡住，用户只看到一个
  // 早已完成的解读。GET /api/analysis/jobs?a=&b= 是纯读、无副作用、不重派。
  let active = [];
  let activeQueryFailed = false;
  try {
    const r = await fetchJSON(
      `/api/analysis/jobs?a=${encodeURIComponent(sel.a)}&b=${encodeURIComponent(sel.b)}`);
    if (!request.current()) return;
    active = Array.isArray(r.jobs) ? r.jobs : [];
  } catch (_) {
    // 查询失败不是致命：仍继续走历史/重入路径，但必须显式告知并给出重试入口
    // （卡片验收「查询失败可重试」）。静默回落会同时造成两件事——在途任务
    // 不可见时，下方「还没有 AI 解读」是不实陈述；而且用户会被引向
    // 「生成预览 → 确认」的死路，后端按单在途租约会返回 busy 409。
    if (!request.current()) return;
    activeQueryFailed = true;
    active = [];
  }
  const activeJob = active.find((j) => sameAnalysisRange(j, sel)) || null;
  if (activeJob) {
    analysisJob = activeJob;
    renderAnalysis({ state: "running", job: activeJob,
                     cancelling: activeJob.status === "cancelling" });
    scheduleAnalysisPoll(activeJob.job_id);
    return;
  }

  const latest = records.find((x) => !x.revoked) || null;
  if (latest) {
    // 存在未撤销的解读记录 ⇒ 该区间已有 job 达成终态。刷新/重入也必须把
    // 本次尝试标为已结束，否则重跑会复用旧键、后端重放上一次的结果
    // （ISS-128）。这条路径是刷新后最常见的落点，漏掉它等于修复在 R4
    // 场景下完全失效。
    markAnalysisAttemptTerminal({ job_id: null, status: "succeeded" }, sel);
    renderAnalysis({ state: latest.expired ? "expired" : "done", record: latest });
    return;
  }

  // 无在途、无有效历史：按 sessionStorage 记录的 job_id 精确重入（GET 纯读，
  // 不隐式重发）。跨会话的发现已由上面的区间查询覆盖，这里是同会话的精确定位。
  const savedJobId = analysisSavedJobId(sel);
  if (savedJobId) {
    try {
      const r = await fetchJSON(`/api/analysis/jobs/${encodeURIComponent(savedJobId)}`);
      if (!request.current()) return;
      const job = r.job;
      if (job && sameAnalysisRange(job, sel)) {
        if (!job.terminal) {
          analysisJob = job;
          renderAnalysis({ state: "running", job, cancelling: job.status === "cancelling" });
          scheduleAnalysisPoll(job.job_id);
          return;
        }
        if (job.status === "succeeded") {
          // succeeded 但 analyses 无未撤销记录：刚撤销或提交与查询竞态——
          // 再查一次记录层（权威），仍无则回落未分析态。无论是否查得到，
          // 这个 job 已经终态，都要标掉本次尝试（ISS-128）。
          markAnalysisAttemptTerminal(job, sel);
          try {
            const r2 = await fetchJSON(`/api/analyses?a=${encodeURIComponent(sel.a)}&b=${encodeURIComponent(sel.b)}`);
            if (!request.current()) return;
            const latest2 = (Array.isArray(r2.analyses) ? r2.analyses : [])
              .find((x) => !x.revoked) || null;
            if (latest2) {
              renderAnalysis({ state: latest2.expired ? "expired" : "done", record: latest2 });
              return;
            }
          } catch (_) { /* 记录层查询失败：按未分析呈现，可手动刷新 */ }
        } else {
          renderAnalysis({ state: "failure", job });
          // 重入读到终态：同样标记尝试已结束，下一次用户尝试才拿新键。
          markAnalysisAttemptTerminal(job, sel);
          return;
        }
      }
    } catch (_) { /* job 不存在（服务重启清理/已淘汰）：回未分析态 */ }
  }
  renderAnalysis({ state: cfg?.enabled ? "idle" : "disabled" });
  if (activeQueryFailed) {
    // 在途状态未知时不能断言「还没有解读」——可能确实有正在跑的任务，且发送
    // 会被占用。复用既有的 showAnalysisRetry 形态（文案 + 重试按钮），不改成
    // 硬失败：历史解读与基础事实仍然可用。
    // 注意 showAnalysisRetry 内部是 replaceChildren，提示必须走它的文案参数，
    // 不能在其之后再 append 节点（会被抹掉）。
    showAnalysisRetry("未能确认该区间是否有正在进行的解读。若已有任务在跑，"
      + "此处暂不显示，发送也会被占用；可重试刷新该状态。",
      "analysis-active-query-failed");
  }
}

/** 会话保存的原 job_id（confirmAnalysisSend 成功后写入）；无记录返回 null。 */
function analysisSavedJobId(sel) {
  try {
    const raw = sessionStorage.getItem(`fathom-aidem:${sel.a}->${sel.b}`);
    if (!raw) return null;
    const saved = JSON.parse(raw);
    return saved && typeof saved.job_id === "string" ? saved.job_id : null;
  } catch (_) { return null; }
}


  return {
    load: loadAnalysisPanel,
    reset: resetAnalysisSession,
    leave() { invalidateRequest(domain); invalidateRequest(pollDomain); resetAnalysisSession(); },
    async showRecord(id) {
      resetAnalysisSession(); invalidateRequest(pollDomain);
      const request = beginRequest(domain);
      try {
        const data = await fetchJSON(`/api/analyses/${encodeURIComponent(id)}`);
        if (!request.current()) return;
        renderAnalysis({state: data.analysis.expired ? "expired" : "done", record: data.analysis});
      } catch (e) { if (request.current()) showAnalysisRetry(`原解读读取失败：${e.message}`, null, () => this.showRecord(id)); }
    },
    showJob(job) {
      resetAnalysisSession(); invalidateRequest(domain); invalidateRequest(pollDomain);
      analysisJob = job;
      if (!job.terminal) { renderAnalysis({state:"running",job,cancelling:job.status === "cancelling"}); scheduleAnalysisPoll(job.job_id); }
      else if (job.revoked) { renderAnalysis({state:"failure",job:{...job,reason_code:"此解读已撤销，正文与事实包不再保留"}}); }
      else if (job.analysis_id) return this.showRecord(job.analysis_id);
      else renderAnalysis({state:"failure",job});
    },
  };
}
