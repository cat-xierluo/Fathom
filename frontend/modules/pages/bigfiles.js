/* 大文件查询域（ISS-151）：目录详情与独立大文件页共用的范围/模式/状态。
 *
 * 显式触发合同（ISS-151）：
 * - 查询只由用户明确点击发起（页「查询」按钮 / 详情「查看当前大文件」），
 *   进入页面或打开详情绝不后台遍历；
 * - 每次查询先 `GET /api/bigfiles?wait=false` 拿真实任务句柄（202 task_id，
 *   ISS-164），再 `GET /api/bigfiles/status?task_id=` 轮询至终态，终态非
 *   cancelled 时才按原参数取结果本体（命中缓存即时返回）；
 * - 离开查询面（页与详情都不可见）或改查询范围时 `POST /api/bigfiles/cancel`
 *   按句柄取消在途任务，并继续 status 轮询至服务端真实收敛——浏览器中止
 *   只断连接不取消服务端 find，前端绝不以「页面没了」冒充已取消；
 * - 句柄即凭证：取消/轮询都绑定 exact task_id，双目录并发的取消互不误伤。
 *
 * 兼容（ISS-032 合成夹具/旧调用方）：wait=false 响应若直接携带 files
 * （旧形态 200），跳过句柄流程原样渲染。
 *
 * 前端责任（ISS-027 模块合同）：世代号 + 宿主面挂载计数防跨页竞态；
 * fetchJSON（失败抛 status 0 / HTTP code）；仅 icons.js 的 SVG 图标；零 emoji。
 */

import { fetchJSON, apiPost, revealInFinder } from "../request.js";
import { fmtBytes, escapeHtml } from "../format.js";
import { icon } from "../../icons.js";

const POLL_MS = 800;

/* ---------- 共享查询域（单例） ---------- */

const engine = {
  scope: { path: "", mode: "recent", days: 7, minMb: 100, topn: 200 },
  phase: "idle",     // idle | submitting | running | cancelling | done | error | cancelled
  taskId: null,
  body: null,        // 最近一次终态结果体（wait=true 或 legacy 200）
  error: null,       // { status, message }（phase=error 时）
  requestedAt: null, // 本次查询发起时间（当前查询口径，区别于历史 a→b）
  gen: 0,            // 世代号：范围变更/新查询使旧轮询与旧取体全部作废
  cancelAfterSubmit: false,  // 提交期内用户已请求取消：句柄返回后立即受理
};

const listeners = new Set();
function emit() { for (const fn of listeners) fn(); }

/** 宿主面挂载计数：页与详情都不可见时才真正取消在途任务。
 * detach 的取消决定延迟一个宏任务——路由先调旧页 leave() 再调新页 load()，
 * 同步换页（详情 → 大文件页继续看同一查询）不得误杀共享任务。 */
const attached = new Set();
let pendingLeaveCancel = null;

export function attachBigfiles(surface) {
  attached.add(surface);
  if (pendingLeaveCancel) { clearTimeout(pendingLeaveCancel); pendingLeaveCancel = null; }
}

export function detachBigfiles(surface) {
  attached.delete(surface);
  if (attached.size) return;
  if (engine.phase === "submitting" || engine.phase === "running") {
    pendingLeaveCancel = setTimeout(() => {
      pendingLeaveCancel = null;
      cancelActiveBigfiles();
    }, 0);
  }
}

function scopeKey(s) {
  return `${s.path}\u0000${s.mode}\u0000${s.days}\u0000${s.minMb}\u0000${s.topn}`;
}

/** 更新共享范围：范围真正变化即视为「改范围」——作废在途轮询/取体并按
 * 句柄静默取消旧任务（取消收敛只留给 status 轮询与日志，不写界面）。 */
function setScope(partial) {
  const next = { ...engine.scope, ...partial };
  next.days = clampDays(next.days);
  next.minMb = clampMb(next.minMb);
  if (scopeKey(next) === scopeKey(engine.scope)) return engine.scope;
  const oldTaskId = engine.taskId;
  const wasInFlight = engine.phase === "submitting" || engine.phase === "running"
    || engine.phase === "cancelling";
  engine.scope = next;
  engine.gen += 1;
  engine.taskId = null;
  engine.body = null;
  engine.error = null;
  engine.phase = "idle";
  if (wasInFlight && oldTaskId) cancelTaskQuietly(oldTaskId);
  return engine.scope;
}

function clampDays(v) {
  const n = Math.round(Number(v));
  return Number.isFinite(n) ? Math.min(90, Math.max(1, n)) : 7;
}

function clampMb(v) {
  const n = Math.round(Number(v));
  return Number.isFinite(n) ? Math.min(10240, Math.max(1, n)) : 100;
}

/** 发起查询（唯一入口，仅由明确点击调用）。scopeOverride 非空时先改范围。 */
export async function startBigfilesQuery(scopeOverride) {
  if (scopeOverride) setScope(scopeOverride);
  engine.gen += 1;
  const gen = engine.gen;
  engine.phase = "submitting";
  engine.taskId = null;
  engine.body = null;
  engine.error = null;
  engine.requestedAt = new Date();
  engine.cancelAfterSubmit = false;
  emit();
  const params = queryParams(engine.scope, { wait: "false" });
  let r;
  try {
    r = await fetchJSON(`/api/bigfiles?${params}`);
  } catch (e) {
    if (gen !== engine.gen) return;
    engine.phase = "error";
    engine.error = e;
    emit();
    return;
  }
  if (gen !== engine.gen) return;
  if (r && Array.isArray(r.files)) { applyBody(r, gen); return; }  // 旧形态 200
  if (r && r.task_id) {
    engine.taskId = r.task_id;
    if (engine.cancelAfterSubmit) {
      // 句柄返回前用户已要求取消：受理后走既有轮询收敛，不取结果本体
      engine.cancelAfterSubmit = false;
      engine.phase = "cancelling";
      emit();
      try {
        await apiPost("/api/bigfiles/cancel", { task_id: r.task_id });
      } catch (_) { /* 受理失败时轮询仍会呈现真实终态 */ }
      if (gen !== engine.gen) return;
      pollTask(gen);
      return;
    }
    if (r.terminal) { await fetchBody(gen); return; }  // 提交时已终态（缓存命中）
    engine.phase = "running";
    emit();
    pollTask(gen);
    return;
  }
  engine.phase = "error";
  engine.error = new Error("服务响应缺少任务句柄");
  emit();
}

function queryParams(scope, { wait }) {
  const params = new URLSearchParams({
    days: String(scope.days),
    min_mb: String(scope.minMb),
    topn: String(scope.topn),
    mode: scope.mode,
    wait: String(wait),
  });
  if (scope.path) params.set("path", scope.path);  // 空 = 监控根
  return params.toString();
}

function pollTask(gen) {
  setTimeout(async () => {
    if (gen !== engine.gen || !engine.taskId) return;
    let s;
    try {
      s = await fetchJSON(
        `/api/bigfiles/status?task_id=${encodeURIComponent(engine.taskId)}`);
    } catch (e) {
      if (gen !== engine.gen) return;
      if (e.status === 404) {  // 句柄失效（如服务重启）：如实报错，不冒充完成
        engine.phase = "error";
        engine.error = e;
        emit();
        return;
      }
      pollTask(gen);  // 瞬时网络抖动：继续轮询
      return;
    }
    if (gen !== engine.gen) return;
    if (!s.terminal) {
      if (s.cancel_requested && engine.phase === "running") {
        engine.phase = "cancelling";
        emit();
      }
      pollTask(gen);
      return;
    }
    if (s.state === "cancelled") {
      engine.phase = "cancelled";
      emit();
      return;
    }
    await fetchBody(gen);
  }, POLL_MS);
}

/** 终态非 cancelled 时按原参数取结果本体（命中缓存即时返回；
 * 取消竞争 409 如实落为 cancelled，不谎称完成）。 */
async function fetchBody(gen) {
  const params = queryParams(engine.scope, { wait: "true" });
  let body;
  try {
    body = await fetchJSON(`/api/bigfiles?${params}`);
  } catch (e) {
    if (gen !== engine.gen) return;
    if (e.status === 409 && e.body && e.body.state === "cancelled") {
      engine.phase = "cancelled";
    } else {
      engine.phase = "error";
      engine.error = e;
    }
    emit();
    return;
  }
  if (gen !== engine.gen) return;
  applyBody(body, gen);
}

function applyBody(body, gen) {
  if (gen !== engine.gen) return;
  engine.body = body;
  engine.phase = "done";
  emit();
}

/** 用户可见的取消（运行中「取消」按钮 / 离开查询面）：按句柄受理后由
 * 既有 status 轮询收敛到 cancelled；404 = 句柄已不存在，如实回 idle。 */
export async function cancelActiveBigfiles() {
  const gen = engine.gen;
  const taskId = engine.taskId;
  if (!taskId) {
    if (gen === engine.gen && engine.phase === "submitting") {
      engine.phase = "cancelling";  // 句柄未返回即取消：提交返回后立即受理
      engine.cancelAfterSubmit = true;
      emit();
    }
    return;
  }
  if (engine.phase !== "running" && engine.phase !== "submitting") return;
  engine.phase = "cancelling";
  emit();
  try {
    await apiPost("/api/bigfiles/cancel", { task_id: taskId });
  } catch (e) {
    if (gen !== engine.gen) return;
    if (e.status === 404) { engine.phase = "idle"; emit(); return; }
    engine.phase = "running";  // 受理失败可重试：恢复运行态，不谎报取消
    emit();
    return;
  }
  // 收敛由 pollTask 完成（submitting 期取消时该轮询尚未启动，这里补一次）
  if (gen === engine.gen && engine.phase === "cancelling") pollTask(gen);
}

/** 静默取消（改范围淘汰的旧任务）：POST + status 轮询至真实收敛，
 * 不触碰共享状态、不写任何界面。 */
function cancelTaskQuietly(taskId) {
  const converge = async () => {
    try {
      await apiPost("/api/bigfiles/cancel", { task_id: taskId });
      for (let i = 0; i < 20; i += 1) {
        const s = await fetchJSON(
          `/api/bigfiles/status?task_id=${encodeURIComponent(taskId)}`);
        if (s.terminal) return;
        await new Promise((r) => setTimeout(r, 200));
      }
    } catch (_) { /* 淘汰任务的清理失败不影响新查询 */ }
  };
  converge();
}

export function subscribeBigfiles(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** 共享查询域只读视图（宿主面判断 scope 是否命中自身、读取状态渲染）。 */
export function bigfilesEngine() { return engine; }

/* ---------- 状态 → 文案（两个宿主面共用同一映射） ---------- */

function _shortRoot(root) {
  if (!root) return "-";
  const parts = String(root).split("/").filter(Boolean);
  if (parts.length <= 2) return root;
  return ".../" + parts.slice(-2).join("/");
}

export function bigfilesStateLabel(state) {
  switch (state) {
    case "ok": return "查询完成";
    case "no_match": return "无匹配文件";
    case "permission_denied": return "权限受限";
    case "failed": return "查询失败";
    case "truncated": return "结果被截断";
    case "expired": return "缓存已过期";
    default: return state;
  }
}

function _stateIcon(state) {
  if (state === "failed" || state === "permission_denied") return icon("alert", 14);
  return icon("fileText", 14);
}

/** 状态摘要行（表格首行）：scope + 六态 + 截断/过期/缓存/错误 + 墙钟。
 * 文案串保持 ISS-032 既有回归合同（「结果被截断」「缓存已过期」等）。 */
function _statusRowHtml(engine, colspan = 4) {
  const scope = engine.body ? engine.body.scope : null;
  const body = engine.body;
  const parts = [];
  if (scope) {
    const resolved = scope.resolved_root || scope.root;
    parts.push(
      `<span class="hint">${escapeHtml(_shortRoot(resolved))} · ` +
      (scope.mode === "largest"
        ? "当前最大"
        : `${scope.days} 天`) +
      ` · ≥ ${scope.min_mb} MB · top ${scope.topn}</span>`
    );
  }
  if (body) {
    parts.push(
      `<span style="display:inline-flex;align-items:center;gap:4px;color:var(--muted)">` +
        `${_stateIcon(body.state)}` +
        `<span>${escapeHtml(bigfilesStateLabel(body.state))}</span>` +
      `</span>`);
    if (body.truncated) {
      parts.push(
        `<span style="display:inline-flex;align-items:center;gap:4px;color:var(--muted)">` +
          `${icon("alert", 12)}` +
          `<span class="hint">` +
            `结果数 ≥ ${escapeHtml(String(body.stats.find_output_lines))} 行，已截断到 top ${escapeHtml(String(body.scope.topn))}` +
          `</span>` +
        `</span>`);
    }
    if (body.expired) {
      parts.push(
        `<span style="display:inline-flex;align-items:center;gap:4px;color:var(--muted)">` +
          `${icon("alert", 12)}` +
          `<span class="hint">缓存已过期（${escapeHtml(String(Math.round((body.cache_age_s || 0) * 10) / 10))} 秒）</span>` +
        `</span>`);
    } else if (body.cached) {
      parts.push(
        `<span class="hint">命中缓存（${escapeHtml(String(Math.round((body.cache_age_s || 0) * 10) / 10))} 秒前）</span>`);
    }
    if (body.error_message) {
      parts.push(
        `<span style="display:inline-flex;align-items:center;gap:4px;color:var(--muted)">` +
          `${icon("alert", 12)}` +
          `<span class="hint">${escapeHtml(body.error_message)}</span>` +
        `</span>`);
    }
    if (body.stats && typeof body.stats.wall_ms === "number") {
      parts.push(
        `<span class="hint">墙钟 ${body.stats.wall_ms} ms · find 行数 ${body.stats.find_output_lines}</span>`);
    }
  }
  return (
    `<tr class="hint-row"><td colspan="${colspan}" class="hint" style="font-size:12px">` +
    parts.join(" · ") +
    `</td></tr>`);
}

/** 结果行：st_size 当前逻辑大小 + mtime + 路径 + Finder 定位（只定位，
 * 不读取/打开内容，走既有 reveal 守卫）。 */
function _fileRowsHtml(body) {
  return (body.files || []).map((f) =>
    `<tr>` +
      `<td class="num">${fmtBytes(f.size)}</td>` +
      `<td style="white-space:nowrap">${escapeHtml(f.mtime)}</td>` +
      `<td class="path" title="${escapeHtml(f.path)}">${escapeHtml(f.path)}</td>` +
      `<td><button class="btn-mini" data-reveal="${escapeHtml(f.path)}" aria-label="在 Finder 中显示" title="在 Finder 中显示">${icon("folderOpen", 14)}</button></td>` +
    `</tr>`).join("");
}

/** 共享渲染：把查询域当前状态画进任意结果 tbody（页表格与详情表格同构）。
 * 绑定宿主的 reveal 点击由调用方按 data-reveal 委托接线。 */
export function renderBigfilesTbody(engineState) {
  const colspan = 4;
  switch (engineState.phase) {
    case "idle":
      return `<tr><td colspan="${colspan}" class="hint">点「查询」查看所选范围内此刻的大文件（不会自动开始，也不后台遍历）。</td></tr>`;
    case "submitting":
    case "running":
      return (
        `<tr class="hint-row"><td colspan="${colspan}" class="hint">` +
        `${icon("brandRing", 14, "dr-loading")}查询中…` +
        `<button type="button" class="btn-mini" data-test="bigfiles-cancel" ` +
        `aria-label="取消本次大文件查询" style="margin-left:8px">取消</button>` +
        `</td></tr>`);
    case "cancelling":
      return (
        `<tr class="hint-row"><td colspan="${colspan}" class="hint">` +
        `${icon("brandRing", 14, "dr-loading")}正在取消…（等待服务端确认，不冒充已取消）` +
        `</td></tr>`);
    case "cancelled":
      return (
        `<tr><td colspan="${colspan}" class="hint">` +
        `${icon("alert", 14)} 本次查询已取消（服务端已确认）。可重新点「查询」发起。` +
        `</td></tr>`);
    case "error": {
      const e = engineState.error || {};
      let msg;
      if (e.status === 404) {
        msg = "查询目录当前已无法定位（可能已被移动或删除）；历史快照读数不受影响。";
      } else if (e.status === 400) {
        msg = `查询范围无效（越界或参数错误）：${e.message || ""}`;
      } else if (e.status === 0) {
        msg = "无法连接本地服务，大文件查询暂不可用。";
      } else {
        msg = `大文件查询失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`;
      }
      return `<tr><td colspan="${colspan}" class="hint">${escapeHtml(msg)}</td></tr>`;
    }
    default: {  // done
      const body = engineState.body;
      if (!body) return `<tr><td colspan="${colspan}" class="hint">暂无结果。</td></tr>`;
      let html = _statusRowHtml(engineState, colspan);
      if (!body.files || !body.files.length) {
        if (body.state === "permission_denied") {
          html += `<tr><td colspan="${colspan}" class="hint">${icon("alert", 14)} 权限受限：${escapeHtml(body.error_message || "find 命中权限拒绝行")}</td></tr>`;
        } else if (body.state === "failed") {
          html += `<tr><td colspan="${colspan}" class="hint">${icon("alert", 14)} 查询失败：${escapeHtml(body.error_message || "find 返回非零")}</td></tr>`;
        } else if (body.state === "expired") {
          html += `<tr><td colspan="${colspan}" class="hint">缓存已过期，重新查询…</td></tr>`;
        } else if (body.state === "no_match") {
          html += `<tr><td colspan="${colspan}" style="color:var(--muted)">` +
            (body.scope.mode === "largest"
              ? `该范围内没有 ≥ ${escapeHtml(String(body.scope.min_mb))}MB 的文件`
              : `近 ${escapeHtml(String(body.scope.days))} 天没有 ≥ ${escapeHtml(String(body.scope.min_mb))}MB 的文件修改`) +
            `</td></tr>`;
        }
        return html;
      }
      return html + _fileRowsHtml(body);
    }
  }
}

/** 当前查询时间说明行（区别于历史 a→b 区间口径）：仅在 done 后有值。 */
export function bigfilesRequestedAtText(engineState) {
  const at = engineState.requestedAt;
  if (!at || engineState.phase !== "done") return "";
  const hh = String(at.getHours()).padStart(2, "0");
  const mm = String(at.getMinutes()).padStart(2, "0");
  const ss = String(at.getSeconds()).padStart(2, "0");
  return `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(2, "0")}-${String(at.getDate()).padStart(2, "0")} ${hh}:${mm}:${ss}`;
}

/* ---------- 独立大文件页（共享查询域的宿主面之一） ---------- */

function syncInputsFromEngine() {
  const s = engine.scope;
  const pathInput = document.getElementById("bf-path");
  const modeSel = document.getElementById("bf-mode");
  const daysInput = document.getElementById("bf-days");
  const mbInput = document.getElementById("bf-mb");
  if (pathInput) pathInput.value = s.path;
  if (modeSel) modeSel.value = s.mode;
  if (daysInput) daysInput.value = String(s.days);
  if (mbInput) mbInput.value = String(s.minMb);
  if (daysInput) daysInput.disabled = s.mode === "largest";
}

function renderPage() {
  const tbody = document.querySelector("#tbl-bigfiles tbody");
  if (!tbody) return;
  tbody.innerHTML = renderBigfilesTbody(engine);
}

async function onQueryClick() {
  const modeSel = document.getElementById("bf-mode");
  await startBigfilesQuery({
    path: (document.getElementById("bf-path")?.value || "").trim(),
    mode: modeSel ? modeSel.value : "recent",
    days: document.getElementById("bf-days")?.value || 7,
    minMb: document.getElementById("bf-mb")?.value || 100,
  });
}

export const bigfilesPage = {
  id: "bigfiles",
  load() {
    attachBigfiles("page");
    syncInputsFromEngine();
    renderPage();
  },
  leave() {
    detachBigfiles("page");
  },
  init() {
    document.getElementById("btn-bigfiles").addEventListener("click", onQueryClick);
    const modeSel = document.getElementById("bf-mode");
    if (modeSel) {
      modeSel.addEventListener("change", () => {
        const days = document.getElementById("bf-days");
        if (days) days.disabled = modeSel.value === "largest";
      });
    }
    // 结果表事件委托：Finder 定位与取消（共享域动作）
    const tbody = document.querySelector("#tbl-bigfiles tbody");
    if (tbody) {
      tbody.addEventListener("click", (e) => {
        const cancel = e.target.closest("[data-test='bigfiles-cancel']");
        if (cancel) { cancelActiveBigfiles(); return; }
        const reveal = e.target.closest("[data-reveal]");
        if (reveal) revealInFinder(reveal.dataset.reveal);
      });
    }
    subscribeBigfiles(renderPage);
  },
};
