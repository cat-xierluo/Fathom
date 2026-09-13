/* 状态卡/顶栏层：/api/status 观测、扫描触发与轮询生命周期。
 *
 * 轮询合同（ISS-027）：扫描运行中每 5s 单实例轮询（polling.js 保证幂等，
 * 快速切页不叠加）；观测到空闲立即取消；由 running 转 idle 时经 router 的
 * 单一刷新入口刷新当前页，不自行拼装加载列表。
 */
import { fetchJSON, apiPost, beginRequest } from "./request.js";
import { fmtBytes } from "./format.js";
import { pushTrayStatus } from "./tauri.js";
import { createPoller, createInterval } from "./polling.js";
import { state } from "./state.js";
import { refreshActivePage } from "./router.js";

const SCAN_POLL_INTERVAL_MS = 5000;
const TRAY_HEARTBEAT_INTERVAL_MS = 10 * 60 * 1000;

const scanPoll = createPoller(() => loadStatus(), SCAN_POLL_INTERVAL_MS);
const trayHeartbeat = createInterval(() => loadStatus(), TRAY_HEARTBEAT_INTERVAL_MS);

export async function loadStatus() {
  const request = beginRequest("status", { pageScoped: false });
  const badge = document.getElementById("scan-badge");
  const btn = document.getElementById("btn-scan");
  let s;
  try {
    s = await fetchJSON("/api/status");
  } catch (e) {
    if (!request.current()) return null;
    badge.textContent = e.status === 0 ? "服务未连接" : "状态加载失败";
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
    badge.textContent = "扫描进行中…"; badge.classList.add("running"); btn.disabled = true;
    scanPoll.schedule();
  } else {
    scanPoll.cancel();
    badge.textContent = s.scan.finished_at
      ? `上次扫描 ${s.scan.finished_at.replace("T", " ")}` : "未手动扫描过";
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
