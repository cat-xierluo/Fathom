/* 总览页：状态卡 + 数据时间/范围/质量行 + 卷容量趋势（含表格等价） +
 * 最近变化摘要 + 最近扫描说明。
 *
 * 前端责任（ISS-027 模块合同）：
 * - beginRequest 世代号保证乱序/迟到响应不覆盖较新查询；
 * - 仅 frontend/icons.js 的 SVG 图标；零 emoji。
 *
 * 展示（ISS-028）：
 * - 数据时间 / 范围 / 质量行：根路径、a → b 时间窗口、覆盖状态（full /
 *   partial / missing），缺路径不可知时显式说明，不冒充完整；
 * - 净变化口径：根同口径差分（不带 +/− 时 0 不着变化色），行值不可累加
 *   已在表头脚注明示；
 * - 走势表格等价：走势图 + "以表格查看"折叠（DESIGN：图表有表格替代）。
 */
import { fetchJSON, beginRequest, revealInFinder } from "../request.js";
import { fmtBytes, fmtKB, fmtDelta, shortPath, escapeHtml } from "../format.js";
import { initChart, showChartMessage } from "../charts.js";
import { icon } from "../../icons.js";

/* 把 ISO 时间戳规整为 "MM-DD HH:MM"（保留日期与时间，便于一眼识别） */
function _shortTs(iso) {
  if (!iso) return "—";
  return String(iso).slice(0, 16).replace("T", " ");
}

/* 覆盖状态判定：collection_status 在 api.snapshots 里有值；fallback 到无 */
function _coverage(snapshot) {
  if (!snapshot) return "missing";
  if (snapshot.collection_status === "partial") return "partial";
  // 兼容：denied_count>0 视为 partial（旧版本可能不返回 collection_status）
  if ((snapshot.denied_count || 0) > 0) return "partial";
  return "full";
}

/* 区域错误占位（DESIGN：保持高度，不塌陷） */
function _regionError(title, detail = "") {
  return `<div class="region-error" role="status">
    <span class="re-title">${escapeHtml(title)}</span>${detail ?
      `<span>${escapeHtml(detail)}</span>` : ""}</div>`;
}

async function loadQualityLine() {
  const request = beginRequest("overviewQuality");
  const el = document.getElementById("overview-quality");
  if (!el) return;
  let snaps;
  try {
    snaps = await fetchJSON("/api/snapshots");
  } catch (e) {
    if (!request.current()) return;
    if (e.status === 0) {
      el.innerHTML = `<span class="quality-chip miss">${icon("alert", 12)} 无法连接本地服务</span>` +
        `<span>上次成功数据：${escapeHtml(_shortTs(window.__FATHOM_LAST_OK__)) || "—"}</span>`;
    } else {
      el.innerHTML = `<span class="quality-chip miss">${icon("alert", 12)} 快照列表加载失败（HTTP ${e.status || "?"}）</span>`;
    }
    return;
  }
  if (!request.current()) return;
  if (!snaps.length) {
    el.innerHTML = `<span class="quality-chip miss">${icon("alert", 12)} 尚无快照</span>` +
      `<span>完成首次扫描后这里会显示数据时间、范围与覆盖质量。</span>`;
    return;
  }
  const latest = snaps[0];
  const cov = _coverage(latest);
  const covChip = cov === "full"
    ? `<span class="quality-chip ok">${icon("alert", 12)} 覆盖完整</span>`
    : cov === "partial"
      ? `<span class="quality-chip warn">${icon("alert", 12)} 部分目录未读取（${latest.denied_count || 0} 个）</span>`
      : `<span class="quality-chip miss">${icon("alert", 12)} 覆盖未知</span>`;
  const rangeChip = snaps.length >= 2
    ? `<span>${escapeHtml(_shortTs(snaps[1].created_at))} → ${escapeHtml(_shortTs(latest.created_at))}</span>`
    : `<span>基线：${escapeHtml(_shortTs(latest.created_at))}（单快照）</span>`;
  el.innerHTML =
    `<span class="path-mono" title="${escapeHtml(latest.root || "")}">${escapeHtml(latest.root || "—")}</span>` +
    rangeChip +
    covChip;
}

async function loadScanNote() {
  const request = beginRequest("overviewScanNote");
  const el = document.getElementById("overview-scan-note");
  if (!el) return;
  // 扫描状态由 status.js 单实例轮询；本节只读取 /api/snapshots 即可，
  // 不再额外请求 /api/status，避免与全局轮询重复计数。
  let snaps;
  try {
    snaps = await fetchJSON("/api/snapshots");
  } catch (e) {
    if (!request.current()) return;
    if (e.status === 0) {
      el.textContent = "无法连接本地服务，扫描状态暂不可用。";
    } else {
      el.textContent = `扫描状态加载失败（HTTP ${e.status || "?"}）：${e.message}`;
    }
    return;
  }
  if (!request.current()) return;
  const latest = snaps && snaps[0];
  if (!latest) {
    el.innerHTML = "尚未扫描。点击顶栏「立即扫描」可手动建立基线；首次扫描后分布立即可用，差分需下一个日期。";
    return;
  }
  const cov = _coverage(latest);
  const parts = [
    `最近扫描 ${escapeHtml(_shortTs(latest.created_at))}`,
    `目录 ${latest.dir_count || 0} 个`,
  ];
  if (cov === "partial") {
    parts.push(`<span class="st st-restricted">${icon("alert", 12)} ${latest.denied_count || 0} 个目录读取受限</span>`);
  } else if (cov === "full") {
    parts.push(`<span class="st st-ok">覆盖完整</span>`);
  }
  el.innerHTML = parts.join(" · ");
}

async function loadVolumeTrend() {
  const request = beginRequest("volumeTrend");
  try {
    const rows = await fetchJSON("/api/volume-trend");
    if (!request.current()) return;
    if (!rows.length) {
      showChartMessage("chart-volume", "尚无快照；完成首次扫描后显示容量趋势。");
      _renderEmptyVolumeTable();
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
    _renderVolumeTable(rows);
  } catch (e) {
    if (!request.current()) return;
    showChartMessage("chart-volume", e.status === 0
      ? "无法连接本地服务，容量趋势暂不可用。"
      : `容量趋势加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`);
  }
}

function _renderEmptyVolumeTable() {
  const el = document.getElementById("overview-volume-table");
  if (!el) return;
  el.innerHTML = `<p class="hint">尚无足够快照绘制走势。</p>`;
}

function _renderVolumeTable(rows) {
  const el = document.getElementById("overview-volume-table");
  if (!el) return;
  // 与图标同语义：每行包含日期/已用/剩余/较前点差值；保留 gap（已有记录点）
  let prevUsed = null;
  const body = rows.map((r) => {
    const used = r.total_bytes - r.free_bytes;
    const dDelta = prevUsed === null ? "—" : (used > prevUsed ? "+" : used < prevUsed ? "−" : "0") +
      fmtBytes(Math.abs(used - prevUsed));
    if (prevUsed !== null) prevUsed = used;
    if (prevUsed === null) prevUsed = used;
    return `<tr>
      <td>${escapeHtml(r.created_at.slice(0, 16).replace("T", " "))}</td>
      <td class="num">${escapeHtml(fmtBytes(used))}</td>
      <td class="num">${escapeHtml(fmtBytes(r.free_bytes))}</td>
      <td class="num">${escapeHtml(dDelta)}</td>
    </tr>`;
  }).join("");
  el.innerHTML = `<table class="tbl tbl-mini">
    <thead><tr><th>快照时间</th><th class="num">已用</th><th class="num">剩余</th><th class="num">较前点</th></tr></thead>
    <tbody>${body}</tbody></table>`;
}

async function loadOverviewSummary() {
  const request = beginRequest("overviewSummary");
  const el = document.getElementById("overview-summary");
  try {
    const d = await fetchJSON("/api/diff?topn=5");
    if (!request.current()) return;
    const span = d.b.created_at.slice(0, 10) + " vs " + d.a.created_at.slice(0, 10);
    const measured = d.grown.length + d.shrunk.length;
    const unrecorded = d.added.length + d.removed.length;
    if (!measured && !unrecorded) {
      el.innerHTML = `<p class="hint">${escapeHtml(span)} 期间没有 ≥1MB 的目录变化。基线已建立；下次扫描前这里不会显示假数据。</p>`;
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
      ...d.added.map((r) => ({ ...r, dir: icon("plus", 14), value: `—（现有 ${fmtKB(r.new_kb)}）`, cls: "delta-none" })),
      ...d.removed.map((r) => ({ ...r, dir: icon("trash", 14), value: `—（曾有 ${fmtKB(r.old_kb)}）`, cls: "delta-none" })),
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
    el.innerHTML = _regionError(message);
  }
}

export const overviewPage = {
  id: "overview",
  load() {
    loadQualityLine();
    loadVolumeTrend();
    loadOverviewSummary();
    loadScanNote();
  },
};
