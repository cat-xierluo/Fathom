/* 分布页：旭日图 + 目录浏览器（面包屑下钻、行级 Finder 打开、侧栏趋势）。 */
import { fetchJSON, beginRequest, invalidateRequest, revealInFinder } from "../request.js";
import { fmtBytes, fmtKB, fmtDelta, shortPath, escapeHtml } from "../format.js";
import { initChart, showChartMessage } from "../charts.js";
import { state } from "../state.js";
import { icon } from "../../icons.js";

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

export const browsePage = {
  id: "browse",
  load() {
    loadTree();
    loadBrowse(state.browsePath);
  },
  leave() {
    // 离开页面：作废在途的树/目录请求，防止迟到的响应改写隐藏 DOM
    invalidateRequest("tree");
    invalidateRequest("browse");
  },
};
