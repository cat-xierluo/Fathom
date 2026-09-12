/* 容量哨兵前端逻辑（无构建链，原生 ES2018+）
 * 数据源：/api/*（见 disk_sentinel/api.py）
 * 图表：本地化 ECharts（frontend/vendor/echarts.min.js）
 */
"use strict";

const charts = {};

/* ---------- 工具 ---------- */

function fmtBytes(bytes) {
  if (bytes == null) return "-";
  let v = bytes, i = 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(1) + " " + units[i];
}
function fmtKB(kb) { return fmtBytes((kb || 0) * 1024); }

function shortPath(p, segments = 2) {
  const parts = p.split("/").filter(Boolean);
  return "/" + parts.slice(-segments).join("/");
}

async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
  return res.json();
}

function initChart(id) {
  if (!charts[id]) {
    charts[id] = echarts.init(document.getElementById(id));
  }
  return charts[id];
}

window.addEventListener("resize", () => {
  Object.values(charts).forEach((c) => c.resize());
});

/* ---------- 状态卡与顶栏 ---------- */

async function loadStatus() {
  const s = await fetchJSON("/api/status");
  const free = s.disk.free_bytes, total = s.disk.total_bytes;
  const used = total - free;

  document.getElementById("root-info").textContent =
    `监控 ${s.root} · http://127.0.0.1:${s.port}`;

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
    ? latest.created_at.replace("T", " ")
    : "尚无快照";

  const badge = document.getElementById("scan-badge");
  if (s.scan.running) {
    badge.textContent = "扫描进行中…";
    badge.classList.add("running");
    document.getElementById("btn-scan").disabled = true;
    setTimeout(refreshAfterScan, 5000);
  } else {
    badge.textContent = s.scan.finished_at
      ? `上次扫描 ${s.scan.finished_at.replace("T", " ")}`
      : "未手动扫描过";
    badge.classList.remove("running");
    document.getElementById("btn-scan").disabled = false;
  }
}

function refreshAfterScan() {
  loadStatus().then(() => {
    loadSnapshotsForDiff();
    loadVolumeTrend();
    loadTree();
  });
}

/* ---------- 卷容量趋势 ---------- */

async function loadVolumeTrend() {
  const rows = await fetchJSON("/api/volume-trend");
  const chart = initChart("chart-volume");
  const xs = rows.map((r) => r.created_at.slice(5, 16).replace("T", " "));
  chart.setOption({
    tooltip: { trigger: "axis" },
    legend: { data: ["已用", "剩余"], top: 0 },
    grid: { left: 70, right: 20, top: 30, bottom: 28 },
    xAxis: { type: "category", data: xs },
    yAxis: {
      type: "value", axisLabel: { formatter: (v) => fmtBytes(v) }, scale: true,
    },
    series: [
      {
        name: "已用", type: "line", smooth: true, symbol: "circle", symbolSize: 5,
        data: rows.map((r) => r.total_bytes - r.free_bytes),
        areaStyle: { opacity: 0.12 }, itemStyle: { color: "#2f6fed" },
      },
      {
        name: "剩余", type: "line", smooth: true, symbol: "none",
        data: rows.map((r) => r.free_bytes),
        itemStyle: { color: "#2e9e5b" },
      },
    ],
  });
}

/* ---------- 快照对比 ---------- */

async function loadSnapshotsForDiff() {
  const snaps = await fetchJSON("/api/snapshots");
  const selA = document.getElementById("sel-a");
  const selB = document.getElementById("sel-b");
  selA.innerHTML = "";
  selB.innerHTML = "";
  snaps.forEach((s, i) => {
    const label = `#${s.id} ${s.created_at.slice(0, 16).replace("T", " ")}`;
    const oa = new Option(label, s.id);
    const ob = new Option(label, s.id);
    selA.add(oa);
    selB.add(ob);
    if (i === 1) selA.value = s.id;
    if (i === 0) selB.value = s.id;
  });
  if (snaps.length >= 2) loadDiff();
}

async function loadDiff() {
  const a = document.getElementById("sel-a").value;
  const b = document.getElementById("sel-b").value;
  if (!a || !b || a === b) return;
  const d = await fetchJSON(`/api/diff?a=${a}&b=${b}`);

  renderDeltaBars("chart-grown", d.grown, "#d64545");
  renderDeltaBars("chart-shrunk", d.shrunk, "#2e9e5b");

  fillTable("tbl-added", d.added, (r) => [r.path, fmtKB(r.new_kb)]);
  fillTable("tbl-removed", d.removed, (r) => [r.path, fmtKB(r.old_kb)]);
}

function renderDeltaBars(id, rows, color) {
  const chart = initChart(id);
  const top = rows.slice(0, 12).reverse();
  chart.setOption({
    tooltip: {
      trigger: "item",
      formatter: (p) => {
        const r = p.data.raw;
        return `${r.path}<br/>${fmtKB(r.old_kb)} → ${fmtKB(r.new_kb)}<br/>变化 ${fmtKB(r.delta_kb)}`;
      },
    },
    grid: { left: 150, right: 40, top: 6, bottom: 24 },
    xAxis: { type: "value", axisLabel: { formatter: (v) => fmtBytes(v * 1024) } },
    yAxis: {
      type: "category",
      data: top.map((r) => shortPath(r.path, 2)),
      axisLabel: { fontSize: 11, width: 140, overflow: "truncate" },
    },
    series: [{
      type: "bar",
      data: top.map((r) => ({ value: r.delta_kb, raw: r })),
      itemStyle: { color, borderRadius: [0, 3, 3, 0] },
      label: {
        show: true, position: "right", fontSize: 11,
        formatter: (p) => fmtKB(p.value),
      },
    }],
  }, true);
}

function fillTable(id, rows, cols) {
  const tbody = document.querySelector(`#${id} tbody`);
  tbody.innerHTML = "";
  if (!rows.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = '<td colspan="2" style="color:var(--muted)">无</td>';
    tbody.appendChild(tr);
    return;
  }
  rows.forEach((r) => {
    const tr = document.createElement("tr");
    const [c0, c1] = cols(r);
    tr.innerHTML = `<td class="path" title="${escapeHtml(r.path)}">${escapeHtml(c0)}</td>` +
                   `<td class="num">${c1}</td>`;
    tbody.appendChild(tr);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------- 旭日图 + 目录趋势 ---------- */

async function loadTree() {
  const t = await fetchJSON("/api/trees?min_kb=51200");
  if (!t.snapshot_id) return;
  const chart = initChart("chart-sunburst");
  chart.setOption({
    tooltip: {
      formatter: (p) => `${p.data.path}<br/>${fmtKB(p.value)}`,
    },
    series: [{
      type: "sunburst",
      radius: [40, "92%"],
      nodeClick: "rootToNode",
      data: t.children[0] ? t.children[0].children || [] : [],
      label: { fontSize: 11, minAngle: 8, hideOverlap: true },
      levels: [
        {},
        { r0: 40, r: "55%", itemStyle: { borderWidth: 1 } },
        { r0: "55%", r: "75%", itemStyle: { borderWidth: 0.5 } },
        { r0: "75%", r: "92%", itemStyle: { borderWidth: 0.5 } },
      ],
    }],
  }, true);
  chart.off("click");
  chart.on("click", (p) => {
    if (p.data && p.data.path) loadDirTrend(p.data.path);
  });
}

async function loadDirTrend(path) {
  document.getElementById("trend-title").textContent =
    "目录趋势：" + shortPath(path, 3);
  document.getElementById("trend-title").title = path;
  const t = await fetchJSON(`/api/trend?path=${encodeURIComponent(path)}`);
  const chart = initChart("chart-trend");
  chart.setOption({
    tooltip: { trigger: "axis" },
    grid: { left: 70, right: 20, top: 16, bottom: 28 },
    xAxis: { type: "category", data: t.points.map((p) => p.created_at.slice(5, 10)) },
    yAxis: { type: "value", axisLabel: { formatter: (v) => fmtBytes(v * 1024) }, scale: true },
    series: [{
      type: "line", smooth: true, symbol: "circle", symbolSize: 5,
      data: t.points.map((p) => p.size_kb),
      areaStyle: { opacity: 0.12 }, itemStyle: { color: "#2f6fed" },
    }],
  }, true);
}

/* ---------- 近期大文件 ---------- */

async function loadBigfiles() {
  const days = document.getElementById("bf-days").value || 7;
  const mb = document.getElementById("bf-mb").value || 100;
  const r = await fetchJSON(`/api/bigfiles?days=${days}&min_mb=${mb}&topn=50`);
  const tbody = document.querySelector("#tbl-bigfiles tbody");
  tbody.innerHTML = "";
  if (!r.files.length) {
    tbody.innerHTML = `<tr><td colspan="3" style="color:var(--muted)">近 ${days} 天没有 ≥ ${mb}MB 的文件修改</td></tr>`;
    return;
  }
  r.files.forEach((f) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td class="num">${fmtBytes(f.size)}</td>` +
      `<td style="white-space:nowrap">${f.mtime}</td>` +
      `<td class="path" title="${escapeHtml(f.path)}">${escapeHtml(f.path)}</td>`;
    tbody.appendChild(tr);
  });
}

/* ---------- 启动 ---------- */

document.getElementById("btn-scan").addEventListener("click", async () => {
  const btn = document.getElementById("btn-scan");
  btn.disabled = true;
  try {
    const r = await fetch("/api/scan", { method: "POST" });
    if (r.status === 409) alert("已有扫描在进行中");
  } finally {
    loadStatus();
  }
});
document.getElementById("btn-diff").addEventListener("click", loadDiff);
document.getElementById("btn-bigfiles").addEventListener("click", loadBigfiles);

(async function init() {
  try {
    await loadStatus();
    await loadVolumeTrend();
    await loadSnapshotsForDiff();
    await loadTree();
    await loadBigfiles();
  } catch (e) {
    console.error(e);
    document.getElementById("root-info").textContent = "加载失败：" + e.message;
  }
})();
