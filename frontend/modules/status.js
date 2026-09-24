/* 状态卡/顶栏层：/api/status 观测、扫描触发与轮询生命周期。
 *
 * 轮询合同（ISS-027）：扫描运行中每 5s 单实例轮询（polling.js 保证幂等，
 * 快速切页不叠加）；观测到空闲立即取消；由 running 转 idle 时经 router 的
 * 单一刷新入口刷新当前页，不自行拼装加载列表。
 */
import { fetchJSON, apiPost, beginRequest } from "./request.js";
import { fmtBytes, fmtKB, fmtDuration } from "./format.js";
import { ICON_PATHS } from "../icons.js";
import { pushTrayStatus } from "./tauri.js";
import { createPoller, createInterval } from "./polling.js";
import { state } from "./state.js";
import { refreshActivePage } from "./router.js";

const SCAN_POLL_INTERVAL_MS = 5000;
const TRAY_HEARTBEAT_INTERVAL_MS = 10 * 60 * 1000;

const scanPoll = createPoller(() => loadStatus(), SCAN_POLL_INTERVAL_MS);
const trayHeartbeat = createInterval(() => loadStatus(), TRAY_HEARTBEAT_INTERVAL_MS);

// ISS-073：扫描徽章 = 可选旋转深度环 + 文本。SVG 只在进入/离开 running 时
// 增删一次；文本节点首次创建后持有引用，轮询 tick 仅改其 textContent——
// 不重建任何元素，动画与文本都稳定（reviewer 对 v1 的跳位 bug 修复）。
let badgeRingEl = null;
let badgeTextEl = null;

function setBadge(running, text) {
  const badge = document.getElementById("scan-badge");
  if (!badge) return;
  if (running && !badgeRingEl) {
    badgeRingEl = document.createElement("span");
    badgeRingEl.className = "badge-ring";
    badgeRingEl.setAttribute("aria-hidden", "true");
    badgeRingEl.innerHTML =
      `<svg viewBox="0 0 24 24" fill="none" stroke-width="2.4" stroke-linecap="butt">${ICON_PATHS.brandRing}</svg>`;
    badge.prepend(badgeRingEl);
  }
  if (!running && badgeRingEl) {
    badgeRingEl.remove();
    badgeRingEl = null;
  }
  if (!badgeTextEl || !badgeTextEl.isConnected) {
    // 首次接管：清掉 HTML 里的初始占位等历史文本节点，此后文本只有受控这一个
    [...badge.childNodes].forEach((node) => {
      if (node.nodeType === Node.TEXT_NODE) node.remove();
    });
    badgeTextEl = document.createTextNode("");
    badge.append(badgeTextEl);
  }
  badgeTextEl.textContent = text;
}

export async function loadStatus() {
  const request = beginRequest("status", { pageScoped: false });
  const badge = document.getElementById("scan-badge");
  const btn = document.getElementById("btn-scan");
  let s;
  try {
    s = await fetchJSON("/api/status");
  } catch (e) {
    if (!request.current()) return null;
    setBadge(false, e.status === 0 ? "服务未连接" : "状态加载失败");
    badge.classList.remove("running");
    btn.disabled = false;
    if (state.scanWasRunning) scanPoll.schedule();  // 扫描中失联：保住轮询链待恢复
    throw e;
  }
  if (!request.current()) return null;
  const free = s.disk.free_bytes, total = s.disk.total_bytes;
  const used = total - free;

  const elFree = document.getElementById("card-free");
  elFree.textContent = fmtBytes(free);
  elFree.classList.toggle("warn", free < 50 * 1024 ** 3);

  const pct = total ? Math.round((used / total) * 100) : 0;
  const elUsed = document.getElementById("card-used");
  elUsed.textContent = `${fmtBytes(used)}（${pct}%）`;
  elUsed.classList.toggle("warn", pct >= 95);

  document.getElementById("card-snaps").textContent = String(s.snapshot_count);
  const latest = s.latest_snapshot;
  document.getElementById("card-latest").textContent = latest
    ? latest.created_at.replace("T", " ") : "尚无快照";

  const wasRunning = state.scanWasRunning;
  state.scanWasRunning = Boolean(s.scan.running);
  if (s.scan.running) {
    // ISS-090：live 进度是可选字段（旧后端/夹具无该键，或心跳超时/写通道
    // 故障时为 null）——缺失即回退既有文案，不伪造计数。计数是进行中的
    // 事实（du 无总量分母，不做百分比）；fmtKB 复用既有 GB/MB 换算惯例。
    const live = s.scan.live;
    const text = live && live.active
      ? `扫描中 · 已扫 ${live.dirs_scanned} 目录 · ${fmtKB(live.bytes_seen_kb)} · ${fmtDuration(live.elapsed_s)}`
      : "扫描进行中…";
    setBadge(true, text); badge.classList.add("running"); btn.disabled = true;
    scanPoll.schedule();
  } else {
    scanPoll.cancel();
    setBadge(false, s.scan.finished_at
      ? `上次扫描 ${s.scan.finished_at.replace("T", " ")}` : "未手动扫描过");
    badge.classList.remove("running"); btn.disabled = false;
    if (wasRunning) refreshActivePage();  // 扫描结束：单一刷新入口刷新当前页
  }
  pushTrayStatus(free, s.snapshot_count, latest);
}

export async function triggerScan() {
  const btn = document.getElementById("btn-scan");
  btn.disabled = true;
  try {
    await apiPost("/api/scan");  // 令牌随请求头发送，不进 URL
    state.scanWasRunning = true;
  } catch (e) {
    if (e.status === 409) alert("已有扫描在进行中");
    else alert(e.message || "扫描启动失败");
  } finally {
    try { await loadStatus(); } catch (e) { console.warn("扫描后状态加载失败：", e); }
  }
}

export function initStatus() {
  document.getElementById("btn-scan").addEventListener("click", triggerScan);
  trayHeartbeat.start();  // tray 标题心跳
}
