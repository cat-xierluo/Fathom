/* 总览页：状态卡之外的卷容量趋势 + 最近变化摘要。 */
import { fetchJSON, beginRequest, revealInFinder } from "../request.js";
import { fmtBytes, fmtKB, fmtDelta, shortPath, escapeHtml } from "../format.js";
import { initChart, showChartMessage } from "../charts.js";
import { icon } from "../../icons.js";

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

export const overviewPage = {
  id: "overview",
  load() {
    loadVolumeTrend();
    loadOverviewSummary();
  },
};
