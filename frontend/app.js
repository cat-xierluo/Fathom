/* Fathom前端 v0.2 —— 群晖式信息架构（见 docs/DESIGN.md）
 * hash 路由五页：#/overview #/changes #/browse #/bigfiles #/settings
 * Tauri 桥：__TAURI__ 存在时推送 tray 状态、响应 tray-action
 */
"use strict";

const charts = {};
const state = { page: "overview", browsePath: null };

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
  if (kb == null) return "新";
  const sign = kb > 0 ? "+" : "";
  return sign + fmtKB(Math.abs(kb)).replace("-", "");
}
function shortPath(p, segments = 2) {
  const parts = p.split("/").filter(Boolean);
  return "/" + parts.slice(-segments).join("/");
}
async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    const detail = await res.json().catch(() => "");
    throw new Error(detail.detail || `${url} -> HTTP ${res.status}`);
  }
  return res.json();
}
function initChart(id) {
  if (!charts[id]) charts[id] = echarts.init(document.getElementById(id));
  return charts[id];
}
window.addEventListener("resize", () => Object.values(charts).forEach((c) => c.resize()));
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function revealInFinder(path) {
  fetch("/api/reveal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  }).then((r) => {
    if (!r.ok) r.json().then((e) => alert(e.detail || "打开失败"));
  });
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
  if (state.page === "overview") { loadVolumeTrend(); loadOverviewSummary(); }
  if (state.page === "changes") { loadSnapshotsForDiff(); loadReportList(); }
  if (state.page === "browse") { loadTree(); loadBrowse(state.browsePath); }
  if (state.page === "bigfiles") loadBigfiles();
  if (state.page === "settings") loadSettings();
}
window.addEventListener("hashchange", navigate);

/* ---------- 状态卡与顶栏 ---------- */

async function loadStatus() {
  const s = await fetchJSON("/api/status");
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

  const badge = document.getElementById("scan-badge");
  const btn = document.getElementById("btn-scan");
  if (s.scan.running) {
    badge.textContent = "扫描进行中…"; badge.classList.add("running"); btn.disabled = true;
    setTimeout(() => { loadStatus().then(refreshData); }, 5000);
  } else {
    badge.textContent = s.scan.finished_at
      ? `上次扫描 ${s.scan.finished_at.replace("T", " ")}` : "未手动扫描过";
    badge.classList.remove("running"); btn.disabled = false;
  }
  pushTrayStatus(free, s.snapshot_count, latest);
}

function refreshData() {
  if (state.page === "overview") { loadVolumeTrend(); loadOverviewSummary(); }
  if (state.page === "changes") { loadSnapshotsForDiff(); loadReportList(); }
  if (state.page === "browse") loadBrowse(state.browsePath);
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
  const rows = await fetchJSON("/api/volume-trend");
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
}

async function loadOverviewSummary() {
  const el = document.getElementById("overview-summary");
  try {
    const d = await fetchJSON("/api/diff?topn=5");
    const span = d.b.created_at.slice(5, 10) + " vs " + d.a.created_at.slice(5, 10);
    if (!d.grown.length && !d.shrunk.length) {
      el.innerHTML = `<p class="hint">${escapeHtml(span)} 期间没有 ≥1MB 的目录变化。</p>`;
      return;
    }
    let html = `<p class="hint">对比区间 ${escapeHtml(span)}：</p><table class="tbl">
      <thead><tr><th>方向</th><th>目录</th><th class="num">变化</th><th></th></tr></thead><tbody>`;
    const rows = [
      ...d.grown.map((r) => ({ ...r, dir: icon("arrowUpRight", 14) })),
      ...d.shrunk.map((r) => ({ ...r, dir: icon("arrowDownRight", 14) })),
    ].slice(0, 8);
    rows.forEach((r) => {
      html += `<tr><td>${r.dir}</td><td class="path" title="${escapeHtml(r.path)}">${escapeHtml(shortPath(r.path, 3))}</td>` +
        `<td class="num ${r.delta_kb > 0 ? "delta-grow" : "delta-shrink"}">${fmtDelta(r.delta_kb)}</td>` +
        `<td><button class="btn-mini" data-reveal="${escapeHtml(r.path)}" title="在 Finder 中显示">"+icon("folderOpen", 14)+"</button></td></tr>`;
    });
    el.innerHTML = html + "</tbody></table>";
    el.querySelectorAll("[data-reveal]").forEach((b) =>
      b.addEventListener("click", () => revealInFinder(b.dataset.reveal)));
  } catch (e) {
    el.innerHTML = `<p class="hint">${escapeHtml(e.message)}——需要至少两个快照（明天起可用）。</p>`;
  }
}

/* ---------- 变化页 ---------- */

async function loadSnapshotsForDiff() {
  const snaps = await fetchJSON("/api/snapshots");
  const selA = document.getElementById("sel-a"), selB = document.getElementById("sel-b");
  if (!selA.options.length) {  // 已填过则保留用户选择
    snaps.forEach((s, i) => {
      const label = `#${s.id} ${s.created_at.slice(0, 16).replace("T", " ")}`;
      selA.add(new Option(label, s.id));
      selB.add(new Option(label, s.id));
      if (i === 1) selA.value = s.id;
      if (i === 0) selB.value = s.id;
    });
  }
  if (snaps.length >= 2) loadDiff();
  else document.getElementById("chart-grown").innerHTML = "";
}

async function loadDiff() {
  const a = document.getElementById("sel-a").value;
  const b = document.getElementById("sel-b").value;
  if (!a || !b) return;
  const d = await fetchJSON(`/api/diff?a=${a}&b=${b}`);
  renderDeltaBars("chart-grown", d.grown, "#d64545");
  renderDeltaBars("chart-shrunk", d.shrunk, "#2e9e5b");
  fillTwoColTable("tbl-added", d.added, (r) => [r.path, fmtKB(r.new_kb)]);
  fillTwoColTable("tbl-removed", d.removed, (r) => [r.path, fmtKB(r.old_kb)]);
}

function renderDeltaBars(id, rows, color) {
  const chart = initChart(id);
  const top = rows.slice(0, 12).reverse();
  chart.setOption({
    tooltip: { trigger: "item", formatter: (p) => {
      const r = p.data.raw;
      return `${r.path}<br/>${fmtKB(r.old_kb)} → ${fmtKB(r.new_kb)}<br/>变化 ${fmtDelta(r.delta_kb)}`;
    } },
    grid: { left: 150, right: 50, top: 6, bottom: 24 },
    xAxis: { type: "value", axisLabel: { formatter: (v) => fmtBytes(v * 1024) } },
    yAxis: { type: "category", data: top.map((r) => shortPath(r.path, 2)),
      axisLabel: { fontSize: 11, width: 140, overflow: "truncate" } },
    series: [{ type: "bar", data: top.map((r) => ({ value: r.delta_kb, raw: r })),
      itemStyle: { color, borderRadius: [0, 3, 3, 0] },
      label: { show: true, position: "right", fontSize: 11, formatter: (p) => fmtKB(p.value) } }],
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
      `<td><button class="btn-mini" data-reveal="${escapeHtml(r.path)}">${icon("folderOpen", 14)}</button></td>`;
    tbody.appendChild(tr);
    tr.querySelector("[data-reveal]").addEventListener("click", () => revealInFinder(r.path));
  });
}

/* ---------- 历史日报 ---------- */

async function loadReportList() {
  const el = document.getElementById("report-list");
  const view = document.getElementById("report-view");
  try {
    const r = await fetchJSON("/api/reports");
    if (!r.reports.length) {
      el.innerHTML = '<p class="hint">还没有日报——首次扫描后，从下一次扫描起每天自动生成。</p>';
      return;
    }
    el.innerHTML = r.reports.map((x) =>
      `<button class="report-item" data-date="${x.date}">${icon("fileText", 14)} ${x.date}</button>`).join("");
    el.querySelectorAll(".report-item").forEach((b) =>
      b.addEventListener("click", async () => {
        el.querySelectorAll(".report-item").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        const c = await fetchJSON(`/api/reports/${b.dataset.date}`);
        view.classList.remove("hidden");
        view.innerHTML = `<pre>${escapeHtml(c.content)}</pre>`;
      }));
  } catch (e) {
    el.innerHTML = `<p class="hint">${escapeHtml(e.message)}</p>`;
  }
}

/* ---------- 分布页：旭日图 + 目录浏览器 ---------- */

async function loadTree() {
  const t = await fetchJSON("/api/trees?min_kb=51200");
  if (!t.snapshot_id) return;
  const chart = initChart("chart-sunburst");
  chart.setOption({
    tooltip: { formatter: (p) => `${p.data.path}<br/>${fmtKB(p.value)}` },
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
}

async function loadBrowse(path) {
  const tbody = document.querySelector("#tbl-browse tbody");
  const meta = document.getElementById("browser-meta");
  try {
    const b = await fetchJSON(`/api/browse${path ? "?path=" + encodeURIComponent(path) : ""}`);
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
      const deltaCls = c.delta_kb == null ? "" : (c.delta_kb > 0 ? "delta-grow" : "delta-shrink");
      tr.innerHTML =
        `<td class="dir-name" data-path="${escapeHtml(c.path)}" title="${escapeHtml(c.path)}">${escapeHtml(c.name)}</td>` +
        `<td class="num">${fmtKB(c.size_kb)}</td>` +
        `<td class="num ${deltaCls}">${c.is_new ? icon("plus", 12) + " " : ""}${fmtDelta(c.delta_kb)}</td>` +
        `<td class="num">${((c.size_kb / total) * 100).toFixed(1)}%</td>` +
        `<td><button class="btn-mini" data-reveal="${escapeHtml(c.path)}" title="在 Finder 中显示">"+icon("folderOpen", 14)+"</button></td>`;
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
    meta.textContent = "";
    tbody.innerHTML = `<tr><td colspan="5" class="hint">${escapeHtml(e.message)}</td></tr>`;
  }
}

/* ---------- 大文件页 ---------- */

async function loadBigfiles() {
  const days = document.getElementById("bf-days").value || 7;
  const mb = document.getElementById("bf-mb").value || 100;
  const r = await fetchJSON(`/api/bigfiles?days=${days}&min_mb=${mb}&topn=200`);
  const tbody = document.querySelector("#tbl-bigfiles tbody");
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
  const s = await fetchJSON("/api/status");
  const rows = [
    ["监控根目录", `<code>${escapeHtml(s.root)}</code>`],
    ["扫描计划", "每日 12:00（launchd：com.maoscripts.fathom-scan）"],
    ["快照保留", "近 35 天每日一份 + 更早每周一份（最多 12 周）"],
    ["入库阈值", "目录 ≥ 10MB；差分关注 ≥ 1MB 变化；新增目录 ≥ 100MB"],
    ["服务地址", `http://127.0.0.1:${s.port}（launchd 常驻）`],
    ["数据库", `${escapeHtml(String(s.db_bytes / 1024 / 1024))} MB · data/disk.db`],
    ["桌面壳", "apps/desktop（Tauri 菜单栏 + 主窗口）"],
  ];
  document.querySelector("#settings-table tbody").innerHTML = rows.map((r) =>
    `<tr><td style="width:140px;color:var(--muted)">${r[0]}</td><td>${r[1]}</td></tr>`).join("");
}

/* ---------- 扫描触发 ---------- */

async function triggerScan() {
  const btn = document.getElementById("btn-scan");
  btn.disabled = true;
  try {
    const r = await fetch("/api/scan", { method: "POST" });
    if (r.status === 409) alert("已有扫描在进行中");
  } finally {
    loadStatus();
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
document.getElementById("btn-diff").addEventListener("click", loadDiff);
document.getElementById("btn-bigfiles").addEventListener("click", loadBigfiles);

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
