/* Fathom前端 v0.2 —— 群晖式信息架构（见 docs/DESIGN.md）
 * hash 路由五页：#/overview #/changes #/browse #/bigfiles #/settings
 * Tauri 桥：__TAURI__ 存在时推送 tray 状态、响应 tray-action
 */
"use strict";

const charts = {};
const state = { page: "overview", browsePath: null, scanWasRunning: false };
let scanPollTimer = null;
let snapshotSelectionRevision = 0;
const requestGenerations = new Map();

/* ---------- 工具 ---------- */

function fmtBytes(bytes) {
  if (bytes == null) return "-";
  let v = bytes, i = 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(1) + " " + units[i];
}
function fmtKB(kb) { return fmtBytes((kb || 0) * 1024); }
function fmtDelta(kb) {
  if (kb == null) return "—";
  if (kb === 0) return fmtKB(0);
  const sign = kb > 0 ? "+" : "−";
  return sign + fmtKB(Math.abs(kb));
}
function shortPath(p, segments = 2) {
  const parts = p.split("/").filter(Boolean);
  return "/" + parts.slice(-segments).join("/");
}
async function fetchJSON(url, opts) {
  let res;
  try {
    res = await fetch(url, opts);
  } catch (cause) {
    const err = new Error("无法连接本地服务");
    err.status = 0;
    err.cause = cause;
    throw err;
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => "");
    const err = new Error(detail.detail || `${url} -> HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}
function initChart(id) {
  if (!charts[id]) charts[id] = echarts.init(document.getElementById(id));
  return charts[id];
}
function resetChart(id) {
  if (charts[id]) {
    charts[id].dispose();
    delete charts[id];
  }
  document.getElementById(id).replaceChildren();
}
function showChartMessage(id, message) {
  resetChart(id);
  const p = document.createElement("p");
  p.className = "hint";
  p.textContent = message;
  document.getElementById(id).appendChild(p);
}
window.addEventListener("resize", () => Object.values(charts).forEach((c) => c.resize()));
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------- 本地写令牌（ISS-022）：仅内存，不进 localStorage/URL ---------- */

let apiToken = null;  // 页面内存持有；后端重启会轮换，403 时自动重新获取

async function getApiToken() {
  if (apiToken) return apiToken;
  const res = await fetch("/api/bootstrap");  // 同源受控发放，跨站 Origin 被服务端拒绝
  if (!res.ok) throw new Error("获取本地写令牌失败");
  apiToken = (await res.json()).token;
  return apiToken;
}

async function apiPost(url, body) {
  const send = (token) => fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Fathom-Token": token },
    body: body === undefined ? null : JSON.stringify(body),
  });
  let res = await send(await getApiToken());
  if (res.status === 403) {  // 令牌失效（如后端已重启）：重新获取后重试一次
    apiToken = null;
    res = await send(await getApiToken());
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    const err = new Error(detail.detail || `${url} -> HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res;
}

async function revealInFinder(path) {
  try {
    await apiPost("/api/reveal", { path });
  } catch (e) {
    alert(e.message || "打开失败");
  }
}

/* ---------- 路由 ---------- */

const PAGE_TITLES = {
  overview: "总览", changes: "变化", browse: "分布",
  bigfiles: "大文件", settings: "设置",
};

function navigate() {
  const hash = (location.hash || "#/overview").slice(2);
  state.page = PAGE_TITLES[hash] ? hash : "overview";
  document.querySelectorAll(".page").forEach((p) => p.classList.add("hidden"));
  document.getElementById("page-" + state.page).classList.remove("hidden");
  document.querySelectorAll(".nav-item").forEach((a) => {
    a.toggleAttribute("aria-current", a.dataset.page === state.page);
  });
  document.getElementById("page-title").textContent = PAGE_TITLES[state.page];

  // 页面激活时懒加载对应数据（图表在隐藏容器中初始化拿不到宽度）
  if (state.page === "overview") { runQuietly(loadVolumeTrend(), "卷容量趋势"); loadOverviewSummary(); }
  if (state.page === "changes") { loadSnapshotsForDiff(); loadReportList(); }
  if (state.page === "browse") { runQuietly(loadTree(), "占用分布"); loadBrowse(state.browsePath); }
  if (state.page === "bigfiles") runQuietly(loadBigfiles(), "大文件");
  if (state.page === "settings") runQuietly(loadSettings(), "设置");
}
window.addEventListener("hashchange", navigate);

function runQuietly(promise, label) {
  Promise.resolve(promise).catch((e) => console.warn(`${label}加载失败：`, e));
}

function beginRequest(domain, { pageScoped = true } = {}) {
  const generation = (requestGenerations.get(domain) || 0) + 1;
  requestGenerations.set(domain, generation);
  const page = state.page;
  return {
    current: () => requestGenerations.get(domain) === generation &&
      (!pageScoped || state.page === page),
  };
}

function invalidateRequest(domain) {
  requestGenerations.set(domain, (requestGenerations.get(domain) || 0) + 1);
}

function scheduleScanStatusPoll() {
  if (scanPollTimer != null) return;
  scanPollTimer = setTimeout(async () => {
    scanPollTimer = null;
    try { await loadStatus(); } catch (e) { console.warn("扫描状态轮询失败：", e); }
  }, 5000);
}

/* ---------- 状态卡与顶栏 ---------- */

async function loadStatus() {
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
    if (state.scanWasRunning) scheduleScanStatusPoll();
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
    scheduleScanStatusPoll();
  } else {
    if (scanPollTimer != null) clearTimeout(scanPollTimer);
    scanPollTimer = null;
    badge.textContent = s.scan.finished_at
      ? `上次扫描 ${s.scan.finished_at.replace("T", " ")}` : "未手动扫描过";
    badge.classList.remove("running"); btn.disabled = false;
    if (wasRunning) runQuietly(refreshData(), "扫描结果");
  }
  pushTrayStatus(free, s.snapshot_count, latest);
}

function refreshData() {
  const jobs = [];
  if (state.page === "overview") jobs.push(loadVolumeTrend(), loadOverviewSummary());
  if (state.page === "changes") jobs.push(loadSnapshotsForDiff(), loadReportList());
  if (state.page === "browse") jobs.push(loadTree(), loadBrowse(state.browsePath));
  return Promise.allSettled(jobs);
}

/* ---------- Tauri 桥 ---------- */

function tauri() { return window.__TAURI__ || null; }

let trayPushFailed = false;  // 失败只警告一次，恢复后重置（防扫描轮询刷屏）

function pushTrayStatus(freeBytes, snapCount, latest) {
  const t = tauri();
  if (!t || !t.core?.invoke) return;  // 浏览器环境：静默降级，不推送
  const gb = Math.round(freeBytes / 1024 ** 3);
  const tooltip = latest
    ? `剩余 ${fmtBytes(freeBytes)} · 快照 ${snapCount} 个 · 最近扫描 ${latest.created_at.replace("T", " ")}`
    : `剩余 ${fmtBytes(freeBytes)} · 尚无快照`;
  t.core.invoke("update_tray_status", { title: `${gb} GB`, tooltip })
    .then(() => { trayPushFailed = false; })
    .catch((e) => {
      if (trayPushFailed) return;  // 持续失败不重复刷屏
      trayPushFailed = true;
      console.warn("tray 状态推送失败（将静默重试，排查线索：capability 远程授权/后端未起）：", e);
    });
}

async function listenTrayActions() {
  const t = tauri();
  if (!t?.event?.listen) return;
  await t.event.listen("tray-action", (e) => {
    if (e.payload === "scan") triggerScan();
  });
}

/* ---------- 总览页 ---------- */

async function loadVolumeTrend() {
  const request = beginRequest("volumeTrend");
  try {
    const rows = await fetchJSON("/api/volume-trend");
    if (!request.current()) return;
    if (!rows.length) {
      showChartMessage("chart-volume", "尚无快照；完成首次扫描后显示容量趋势。");
      return;
    }
    const chart = initChart("chart-volume");
    const xs = rows.map((r) => r.created_at.slice(5, 16).replace("T", " "));
    chart.setOption({
      tooltip: { trigger: "axis" },
      legend: { data: ["已用", "剩余"], top: 0 },
      grid: { left: 70, right: 20, top: 30, bottom: 28 },
      xAxis: { type: "category", data: xs },
      yAxis: { type: "value", axisLabel: { formatter: (v) => fmtBytes(v) }, scale: true },
      series: [
        { name: "已用", type: "line", smooth: true, symbolSize: 5, symbol: "circle",
          data: rows.map((r) => r.total_bytes - r.free_bytes),
          areaStyle: { opacity: 0.12 }, itemStyle: { color: "#2f6fed" } },
        { name: "剩余", type: "line", smooth: true, symbol: "none",
          data: rows.map((r) => r.free_bytes), itemStyle: { color: "#2e9e5b" } },
      ],
    }, true);
  } catch (e) {
    if (!request.current()) return;
    showChartMessage("chart-volume", e.status === 0
      ? "无法连接本地服务，容量趋势暂不可用。"
      : `容量趋势加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`);
  }
}

async function loadOverviewSummary() {
  const request = beginRequest("overviewSummary");
  const el = document.getElementById("overview-summary");
  try {
    const d = await fetchJSON("/api/diff?topn=5");
    if (!request.current()) return;
    const span = d.b.created_at.slice(5, 10) + " vs " + d.a.created_at.slice(5, 10);
    const measured = d.grown.length + d.shrunk.length;
    const unrecorded = d.added.length + d.removed.length;
    if (!measured && !unrecorded) {
      el.innerHTML = `<p class="hint">${escapeHtml(span)} 期间没有 ≥1MB 的目录变化。</p>`;
      return;
    }
    const summary = !measured
      ? `${span} 仅发现 ${unrecorded} 个新增或未记录目录，缺少可比基线，不能判断为“无变化”。`
      : `对比区间 ${span}：`;
    let html = `<p class="hint">${escapeHtml(summary)}</p><table class="tbl">
      <thead><tr><th>方向</th><th>目录</th><th class="num">变化</th><th></th></tr></thead><tbody>`;
    const rows = [
      ...d.grown.map((r) => ({ ...r, dir: icon("arrowUpRight", 14), value: fmtDelta(r.delta_kb), cls: "delta-grow" })),
      ...d.shrunk.map((r) => ({ ...r, dir: icon("arrowDownRight", 14), value: fmtDelta(r.delta_kb), cls: "delta-shrink" })),
      ...d.added.map((r) => ({ ...r, dir: icon("plus", 14), value: `—（现有 ${fmtKB(r.new_kb)}）`, cls: "" })),
      ...d.removed.map((r) => ({ ...r, dir: icon("trash", 14), value: `—（曾有 ${fmtKB(r.old_kb)}）`, cls: "" })),
    ].slice(0, 8);
    rows.forEach((r) => {
      html += `<tr><td>${r.dir}</td><td class="path" title="${escapeHtml(r.path)}">${escapeHtml(shortPath(r.path, 3))}</td>` +
        `<td class="num ${r.cls}">${r.value}</td>` +
        `<td><button class="btn-mini" data-reveal="${escapeHtml(r.path)}" title="在 Finder 中显示" aria-label="在 Finder 中显示">${icon("folderOpen", 14)}</button></td></tr>`;
    });
    if (measured && unrecorded) {
      html += `</tbody></table><p class="hint">另有 ${unrecorded} 个新增或未记录目录，不计作可测量净变化。</p>`;
    } else {
      html += "</tbody></table>";
    }
    el.innerHTML = html;
    el.querySelectorAll("[data-reveal]").forEach((b) =>
      b.addEventListener("click", () => revealInFinder(b.dataset.reveal)));
  } catch (e) {
    if (!request.current()) return;
    let message;
    if (e.status === 409) {
      message = "还不能比较：需要两个不同日期的有效快照。已有一个快照时，基线已经建立；分布现在可用。";
    } else if (e.status === 0) {
      message = "无法连接本地服务，最近变化暂不可用。请确认 Fathom 服务正在运行后重试。";
    } else {
      message = `最近变化加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}。上次有效数据不代表本次无变化。`;
    }
    el.innerHTML = `<p class="hint">${escapeHtml(message)}</p>`;
  }
}

/* ---------- 变化页 ---------- */

function setDiffStatus(message) {
  document.getElementById("diff-status").textContent = message;
}

function setDiffControlsEnabled(enabled) {
  document.getElementById("sel-a").disabled = !enabled;
  document.getElementById("sel-b").disabled = !enabled;
  document.getElementById("btn-diff").disabled = !enabled;
}

function clearDiffResults() {
  ["chart-grown", "chart-shrunk"].forEach((id) => {
    if (charts[id]) charts[id].clear();
    else document.getElementById(id).replaceChildren();
  });
  ["tbl-added", "tbl-removed"].forEach((id) => {
    document.querySelector(`#${id} tbody`).innerHTML =
      '<tr><td colspan="3" class="hint">暂无可比较数据</td></tr>';
  });
}

function replaceSnapshotOptions(select, snaps) {
  const options = snaps.map((s) => {
    const label = `#${s.id} ${s.created_at.slice(0, 16).replace("T", " ")}`;
    return new Option(label, String(s.id));
  });
  select.replaceChildren(...options);
}

async function loadSnapshotsForDiff({ notice = "" } = {}) {
  const request = beginRequest("snapshots");
  invalidateRequest("diff");
  const selA = document.getElementById("sel-a"), selB = document.getElementById("sel-b");
  const previousA = selA.value, previousB = selB.value;
  const selectionRevision = snapshotSelectionRevision;
  let snaps;
  try {
    snaps = await fetchJSON("/api/snapshots");
  } catch (e) {
    if (!request.current()) return;
    invalidateRequest("diff");
    clearDiffResults();
    setDiffControlsEnabled(false);
    setDiffStatus(e.status === 0
      ? "无法连接本地服务，快照列表暂不可用。"
      : `快照列表加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`);
    return;
  }
  if (!request.current()) return;

  const ids = snaps.map((s) => String(s.id));
  const selectedA = snapshotSelectionRevision === selectionRevision ? previousA : selA.value;
  const selectedB = snapshotSelectionRevision === selectionRevision ? previousB : selB.value;
  const validA = ids.includes(selectedA), validB = ids.includes(selectedB);
  let nextB = validB ? selectedB : (ids[0] || "");
  let nextA = validA && selectedA !== nextB
    ? selectedA : (ids.find((id) => id !== nextB) || "");

  replaceSnapshotOptions(selA, snaps);
  replaceSnapshotOptions(selB, snaps);
  selA.value = nextA;
  selB.value = nextB;

  const fellBack = Boolean((selectedA && !validA) || (selectedB && !validB));
  if (snaps.length < 2) {
    invalidateRequest("diff");
    clearDiffResults();
    setDiffControlsEnabled(false);
    setDiffStatus(snaps.length === 1
      ? "基线已建立；需要另一个不同日期的有效快照才能比较，分布现在可用。"
      : "尚无快照，请先扫描建立基线。");
    return;
  }

  const fallbackNotice = notice || (fellBack
    ? "所选快照已更新或不再可用，已切换到最近有效快照。"
    : "");
  setDiffControlsEnabled(true);
  await loadDiff({ retryOnMissing: false, successMessage: fallbackNotice });
}

async function loadDiff({ retryOnMissing = true, successMessage = "" } = {}) {
  const request = beginRequest("diff");
  const a = document.getElementById("sel-a").value;
  const b = document.getElementById("sel-b").value;
  if (!a || !b) return;
  setDiffStatus("正在加载快照对比…");
  try {
    const d = await fetchJSON(`/api/diff?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
    if (!request.current()) return;
    renderDeltaBars("chart-grown", d.grown, "#d64545");
    renderDeltaBars("chart-shrunk", d.shrunk, "#2e9e5b");
    fillTwoColTable("tbl-added", d.added, (r) => [r.path, fmtKB(r.new_kb)]);
    fillTwoColTable("tbl-removed", d.removed, (r) => [r.path, fmtKB(r.old_kb)]);
    setDiffStatus(successMessage || `正在对比快照 #${a} → #${b}`);
  } catch (e) {
    if (!request.current()) return;
    if (e.status === 404 && retryOnMissing) {
      clearDiffResults();
      await loadSnapshotsForDiff({ notice: "所选快照已更新或不再可用，已切换到最近有效快照。" });
      return;
    }
    clearDiffResults();
    if (e.status === 409) {
      setDiffStatus("还不能比较：需要两个不同日期的有效快照。");
    } else if (e.status === 0) {
      setDiffStatus("无法连接本地服务，快照对比暂不可用。");
    } else {
      setDiffStatus(`快照对比加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`);
    }
  }
}

function renderDeltaBars(id, rows, color) {
  const chart = initChart(id);
  const top = rows.slice(0, 12).reverse();
  chart.setOption({
    tooltip: { trigger: "item", formatter: (p) => {
      const r = p.data.raw;
      // tooltip 以 HTML 解释：路径来自扫描数据，必须按文本转义（ISS-022）
      return `${escapeHtml(r.path)}<br/>${fmtKB(r.old_kb)} → ${fmtKB(r.new_kb)}<br/>变化 ${fmtDelta(r.delta_kb)}`;
    } },
    grid: { left: 150, right: 50, top: 6, bottom: 24 },
    xAxis: { type: "value", axisLabel: { formatter: (v) => fmtDelta(v) } },
    yAxis: { type: "category", data: top.map((r) => shortPath(r.path, 2)),
      axisLabel: { fontSize: 11, width: 140, overflow: "truncate" } },
    series: [{ type: "bar", data: top.map((r) => ({ value: r.delta_kb, raw: r })),
      itemStyle: { color, borderRadius: [0, 3, 3, 0] },
      label: { show: true, position: "right", fontSize: 11, formatter: (p) => fmtDelta(p.value) } }],
  }, true);
}

function fillTwoColTable(id, rows, cols) {
  const tbody = document.querySelector(`#${id} tbody`);
  tbody.innerHTML = "";
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="2" style="color:var(--muted)">无</td></tr>';
    return;
  }
  rows.forEach((r) => {
    const tr = document.createElement("tr");
    const [c0, c1] = cols(r);
    tr.innerHTML = `<td class="path" title="${escapeHtml(r.path)}">${escapeHtml(c0)}</td>` +
      `<td class="num">${c1}</td>` +
      `<td><button class="btn-mini" data-reveal="${escapeHtml(r.path)}" title="在 Finder 中显示" aria-label="在 Finder 中显示">${icon("folderOpen", 14)}</button></td>`;
    tbody.appendChild(tr);
    tr.querySelector("[data-reveal]").addEventListener("click", () => revealInFinder(r.path));
  });
}

/* ---------- 历史日报 ---------- */

async function loadReportList() {
  const request = beginRequest("reportList");
  invalidateRequest("reportContent");
  const el = document.getElementById("report-list");
  const view = document.getElementById("report-view");
  view.classList.add("hidden");
  view.replaceChildren();
  try {
    const r = await fetchJSON("/api/reports");
    if (!request.current()) return;
    if (!r.reports.length) {
      el.innerHTML = '<p class="hint">还没有日报——首次扫描后，从下一次扫描起每天自动生成。</p>';
      return;
    }
    el.innerHTML = r.reports.map((x) =>
      `<button class="report-item" data-date="${x.date}">${icon("fileText", 14)} ${x.date}</button>`).join("");
    el.querySelectorAll(".report-item").forEach((b) =>
      b.addEventListener("click", async () => {
        const contentRequest = beginRequest("reportContent");
        el.querySelectorAll(".report-item").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        try {
          const c = await fetchJSON(`/api/reports/${b.dataset.date}`);
          if (!contentRequest.current()) return;
          view.classList.remove("hidden");
          view.innerHTML = `<pre>${escapeHtml(c.content)}</pre>`;
        } catch (e) {
          if (!contentRequest.current()) return;
          view.classList.remove("hidden");
          view.innerHTML = `<p class="hint">${escapeHtml(e.status === 0
            ? "无法连接本地服务，日报暂不可用。"
            : `日报加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`)}</p>`;
        }
      }));
  } catch (e) {
    if (!request.current()) return;
    el.innerHTML = `<p class="hint">${escapeHtml(e.message)}</p>`;
  }
}

/* ---------- 分布页：旭日图 + 目录浏览器 ---------- */

async function loadTree() {
  const request = beginRequest("tree");
  try {
    const t = await fetchJSON("/api/trees?min_kb=51200");
    if (!request.current()) return;
    if (!t.snapshot_id) {
      showChartMessage("chart-sunburst", "尚无快照；完成首次扫描后显示占用分布。");
      return;
    }
    const chart = initChart("chart-sunburst");
    chart.setOption({
      tooltip: { formatter: (p) => `${escapeHtml(p.data.path)}<br/>${fmtKB(p.value)}` },
      series: [{
        type: "sunburst", radius: [40, "92%"], nodeClick: "rootToNode",
        data: t.children[0] ? t.children[0].children || [] : [],
        label: { fontSize: 11, minAngle: 8, hideOverlap: true },
        levels: [{}, { r0: 40, r: "55%" }, { r0: "55%", r: "75%" }, { r0: "75%", r: "92%" }],
      }],
    }, true);
    chart.off("click");
    chart.on("click", (p) => {
      if (p.data && p.data.path) loadBrowse(p.data.path);
    });
  } catch (e) {
    if (!request.current()) return;
    showChartMessage("chart-sunburst", e.status === 0
      ? "无法连接本地服务，占用分布暂不可用。"
      : `占用分布加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`);
  }
}

async function loadBrowse(path) {
  const request = beginRequest("browse");
  const tbody = document.querySelector("#tbl-browse tbody");
  const meta = document.getElementById("browser-meta");
  try {
    const b = await fetchJSON(`/api/browse${path ? "?path=" + encodeURIComponent(path) : ""}`);
    if (!request.current()) return;
    state.browsePath = b.path;

    // 面包屑
    document.getElementById("crumbs").innerHTML = b.crumbs.map((c, i) =>
      `${i ? '<span class="crumb-sep">/</span>' : ""}` +
      `<button class="crumb${i === b.crumbs.length - 1 ? " current" : ""}" data-path="${escapeHtml(c.path)}">` +
      `${escapeHtml(c.name)}</button>`).join("");
    document.querySelectorAll("#crumbs .crumb").forEach((el) =>
      el.addEventListener("click", () => loadBrowse(el.dataset.path)));

    meta.textContent = `共 ${b.children.length} 个子目录（≥10MB 才入库）`;
    document.getElementById("browser-trend-title").textContent =
      "目录趋势：" + shortPath(b.path, 2);

    tbody.innerHTML = "";
    if (!b.children.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="color:var(--muted)">此目录下没有 ≥10MB 的子目录</td></tr>';
    }
    const total = b.size_kb || 1;
    b.children.forEach((c) => {
      const tr = document.createElement("tr");
      const deltaCls = c.delta_kb == null || c.delta_kb === 0
        ? "" : (c.delta_kb > 0 ? "delta-grow" : "delta-shrink");
      tr.innerHTML =
        `<td class="dir-name" data-path="${escapeHtml(c.path)}" title="${escapeHtml(c.path)}">${escapeHtml(c.name)}</td>` +
        `<td class="num">${fmtKB(c.size_kb)}</td>` +
        `<td class="num ${deltaCls}">${c.is_new ? icon("plus", 12) + " " : ""}${fmtDelta(c.delta_kb)}</td>` +
        `<td class="num">${((c.size_kb / total) * 100).toFixed(1)}%</td>` +
        `<td><button class="btn-mini" data-reveal="${escapeHtml(c.path)}" title="在 Finder 中显示" aria-label="在 Finder 中显示">${icon("folderOpen", 14)}</button></td>`;
      tbody.appendChild(tr);
      tr.querySelector(".dir-name").addEventListener("click", () => loadBrowse(c.path));
      tr.querySelector("[data-reveal]").addEventListener("click", () => revealInFinder(c.path));
    });

    // 侧栏趋势
    const chart = initChart("chart-browser-trend");
    chart.setOption({
      tooltip: { trigger: "axis" },
      grid: { left: 64, right: 12, top: 12, bottom: 24 },
      xAxis: { type: "category", data: b.trend.map((p) => p.created_at.slice(5, 10)) },
      yAxis: { type: "value", axisLabel: { formatter: (v) => fmtBytes(v * 1024) }, scale: true },
      series: [{ type: "line", smooth: true, symbolSize: 4, symbol: "circle",
        data: b.trend.map((p) => p.size_kb),
        areaStyle: { opacity: 0.12 }, itemStyle: { color: "#2f6fed" } }],
    }, true);
  } catch (e) {
    if (!request.current()) return;
    meta.textContent = "";
    tbody.innerHTML = `<tr><td colspan="5" class="hint">${escapeHtml(e.message)}</td></tr>`;
  }
}

/* ---------- 大文件页 ---------- */

async function loadBigfiles() {
  const request = beginRequest("bigfiles");
  const days = document.getElementById("bf-days").value || 7;
  const mb = document.getElementById("bf-mb").value || 100;
  const tbody = document.querySelector("#tbl-bigfiles tbody");
  let r;
  try {
    r = await fetchJSON(`/api/bigfiles?days=${days}&min_mb=${mb}&topn=200`);
  } catch (e) {
    if (!request.current()) return;
    tbody.innerHTML = `<tr><td colspan="4" class="hint">${escapeHtml(e.status === 0
      ? "无法连接本地服务，大文件查询暂不可用。"
      : `大文件查询失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`)}</td></tr>`;
    return;
  }
  if (!request.current()) return;
  tbody.innerHTML = "";
  if (!r.files.length) {
    tbody.innerHTML = `<tr><td colspan="4" style="color:var(--muted)">近 ${days} 天没有 ≥ ${mb}MB 的文件修改</td></tr>`;
    return;
  }
  r.files.forEach((f) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td class="num">${fmtBytes(f.size)}</td>` +
      `<td style="white-space:nowrap">${f.mtime}</td>` +
      `<td class="path" title="${escapeHtml(f.path)}">${escapeHtml(f.path)}</td>` +
      `<td><button class="btn-mini" data-reveal="${escapeHtml(f.path)}">${icon("folderOpen", 14)}</button></td>`;
    tbody.appendChild(tr);
    tr.querySelector("[data-reveal]").addEventListener("click", () => revealInFinder(f.path));
  });
}

/* ---------- 设置页 ---------- */

async function loadSettings() {
  const request = beginRequest("settings");
  const tbody = document.querySelector("#settings-table tbody");
  let s;
  try {
    s = await fetchJSON("/api/status");
  } catch (e) {
    if (!request.current()) return;
    tbody.innerHTML = `<tr><td colspan="2" class="hint">${escapeHtml(e.status === 0
      ? "无法连接本地服务，设置状态暂不可用。"
      : `设置状态加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`)}</td></tr>`;
    return;
  }
  if (!request.current()) return;
  const rows = [
    ["监控根目录", `<code>${escapeHtml(s.root)}</code>`],
    ["扫描计划", "每日 12:00（launchd：com.maoscripts.fathom-scan）"],
    ["快照保留", "近 35 天每日一份 + 更早每周一份（最多 12 周）"],
    ["入库阈值", "目录 ≥ 10MB；差分关注 ≥ 1MB 变化；新增目录 ≥ 100MB"],
    ["服务地址", `http://127.0.0.1:${s.port}（launchd 常驻）`],
    ["数据库", `${escapeHtml(String(s.db_bytes / 1024 / 1024))} MB · data/disk.db`],
    ["桌面壳", "apps/desktop（Tauri 菜单栏 + 主窗口）"],
  ];
  tbody.innerHTML = rows.map((r) =>
    `<tr><td style="width:140px;color:var(--muted)">${r[0]}</td><td>${r[1]}</td></tr>`).join("");
}

/* ---------- 扫描触发 ---------- */

async function triggerScan() {
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

/* ---------- 静态图标注入（icons.js 提供 icon()，无 emoji —— DEC-010） ---------- */

function mountStaticIcons() {
  const brand = document.getElementById("brand-icon");
  if (brand) brand.innerHTML = icon("anchor", 26, "brand-anchor");
  document.querySelectorAll("[data-icon]").forEach((el) => {
    el.innerHTML = icon(el.dataset.icon, 18);
  });
  document.querySelectorAll("[data-icon-inline]").forEach((el) => {
    el.innerHTML = icon(el.dataset.iconInline, 14);
  });
}

/* ---------- 启动 ---------- */

document.getElementById("btn-scan").addEventListener("click", triggerScan);
document.getElementById("btn-diff").addEventListener("click", () => loadDiff());
document.getElementById("btn-bigfiles").addEventListener("click", loadBigfiles);
["sel-a", "sel-b"].forEach((id) => {
  document.getElementById(id).addEventListener("change", () => {
    snapshotSelectionRevision += 1;
    invalidateRequest("diff");
    clearDiffResults();
    setDiffStatus("快照选择已更改，点击“对比”加载结果。");
  });
});

(async function init() {
  try {
    mountStaticIcons();
    navigate();
    await loadStatus();
    await listenTrayActions();
    setInterval(loadStatus, 10 * 60 * 1000);  // tray 标题心跳
  } catch (e) {
    console.error(e);
    document.getElementById("page-title").textContent = "加载失败：" + e.message;
  }
})();
