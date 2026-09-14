/* 变化页：快照对比（世代号防倒序 + 用户改选保护 + 404 协调）、可排序变化表、
 * 选中目录详情（侧栏 + Esc 焦点返回）、历史日报。
 *
 * 前端责任（ISS-027 模块合同）：
 * - beginRequest 世代号 + pageScoped：迟到的旧响应不能覆盖较新查询；
 * - 仅 frontend/icons.js 的 SVG 图标；零 emoji；
 *
 * 展示（ISS-028）：
 * - 净变化口径：根同口径差分，不对子目录求和为净变化；
 * - 状态列：已测量 / 未记录（首次进入统计） / 受限（权限不足时不可知）；
 * - 键盘可达：表行可 Tab 聚焦、Enter 打开详情、Esc 关闭后焦点返回触发行；
 * - 长路径：可一键复制（DESIGN 关键可达性约束）；
 * - 目录详情：路径 + 净变化 + 趋势（来自 /api/trend）+ 当前大小（/api/browse）。
 */
import { fetchJSON, beginRequest, invalidateRequest, revealInFinder } from "../request.js";
import { fmtKB, fmtDelta, escapeHtml } from "../format.js";
import { initChart, hasChart, clearChart } from "../charts.js";
import { icon } from "../../icons.js";

let snapshotSelectionRevision = 0;  // 用户改选计数：晚到的快照列表不得覆盖改选结果
let currentSort = { key: "delta", dir: -1 };
let currentRows = [];
let lastDiff = null;                 // 最近一次成功 diff，用于详情
let activeDetailPath = null;          // 当前详情目录
let activeDetailRow = null;           // 当前详情触发行（Esc 后焦点回此）

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
    if (hasChart(id)) clearChart(id);
    else document.getElementById(id).replaceChildren();
  });
  ["tbl-added", "tbl-removed"].forEach((id) => {
    document.querySelector(`#${id} tbody`).innerHTML =
      '<tr><td colspan="3" class="hint">暂无可比较数据</td></tr>';
  });
  document.getElementById("changes-body").innerHTML =
    '<tr><td colspan="6" class="hint">暂无可比较数据</td></tr>';
  document.getElementById("changes-net").hidden = true;
  document.getElementById("changes-foot").hidden = true;
  // 关闭详情（如打开）
  closeDetail();
  currentRows = [];
  lastDiff = null;
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

/* ---------- 净变化与可排序表 ---------- */

function computeNet(d) {
  // 根同口径净变化：来自 grown/shrunk 的简单和（API 顶层不带 sum）；不与 added/removed 混算。
  let net = 0;
  for (const r of d.grown || []) net += Number(r.delta_kb || 0);
  for (const r of d.shrunk || []) net += Number(r.delta_kb || 0);
  return net;
}

function synthesizeRows(d) {
  const rows = [];
  for (const r of d.grown || []) {
    rows.push({
      path: r.path, prev: r.old_kb, curr: r.new_kb, delta: r.delta_kb,
      status: "measured", sortKey: Math.abs(r.delta_kb || 0),
    });
  }
  for (const r of d.shrunk || []) {
    rows.push({
      path: r.path, prev: r.old_kb, curr: r.new_kb, delta: r.delta_kb,
      status: "measured", sortKey: Math.abs(r.delta_kb || 0),
    });
  }
  for (const r of d.added || []) {
    rows.push({
      path: r.path, prev: null, curr: r.new_kb, delta: null,
      status: "unrecorded", sortKey: r.new_kb || 0,
    });
  }
  for (const r of d.removed || []) {
    rows.push({
      path: r.path, prev: r.old_kb, curr: null, delta: null,
      status: "unrecorded", sortKey: r.old_kb || 0,
    });
  }
  return rows;
}

function sortRows(rows) {
  const dir = currentSort.dir;
  const key = currentSort.key;
  const out = rows.slice();
  out.sort((a, b) => {
    let av, bv;
    if (key === "path") { av = a.path; bv = b.path; }
    else if (key === "prev") { av = a.prev == null ? -Infinity : a.prev; bv = b.prev == null ? -Infinity : b.prev; }
    else if (key === "curr") { av = a.curr == null ? -Infinity : a.curr; bv = b.curr == null ? -Infinity : b.curr; }
    else { av = a.sortKey || 0; bv = b.sortKey || 0; }
    if (av < bv) return -1 * dir;
    if (av > bv) return 1 * dir;
    return a.path < b.path ? -1 : 1;
  });
  return out;
}

function filterRows(rows, q) {
  if (!q) return rows;
  const needle = q.toLowerCase();
  return rows.filter((r) => r.path.toLowerCase().includes(needle));
}

function statusBadge(st) {
  if (st === "unrecorded") return `<span class="st st-unrecorded"><span class="st-dot"></span>未记录</span>`;
  if (st === "restricted") return `<span class="st st-restricted"><span class="st-dot"></span>受限</span>`;
  return `<span class="st st-measured"><span class="st-dot"></span>已测量</span>`;
}

function deltaCell(row) {
  if (row.delta == null) {
    if (row.status === "unrecorded") {
      if (row.prev == null) return `<span class="delta-none">—（现有 ${escapeHtml(fmtKB(row.curr))}）</span>`;
      return `<span class="delta-none">—（曾有 ${escapeHtml(fmtKB(row.prev))}）</span>`;
    }
    return `<span class="delta-none">—</span>`;
  }
  const cls = row.delta > 0 ? "delta-grow" : row.delta < 0 ? "delta-shrink" : "";
  return `<span class="${cls}">${escapeHtml(fmtDelta(row.delta))}</span>`;
}

function renderChangesTable() {
  const tbody = document.getElementById("changes-body");
  const search = (document.getElementById("changes-search")?.value || "").trim();
  const rows = sortRows(filterRows(currentRows, search));
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="hint">${
      search ? `没有匹配“${escapeHtml(search)}”的目录。` : "暂无可比较数据"
    }</td></tr>`;
  } else {
    tbody.innerHTML = rows.map((r) => {
      const prevTxt = r.prev == null ? `<span class="delta-none">—</span>` : `<span class="num">${escapeHtml(fmtKB(r.prev))}</span>`;
      const currTxt = r.curr == null ? `<span class="delta-none">—</span>` : `<span class="num">${escapeHtml(fmtKB(r.curr))}</span>`;
      return `<tr class="focusable" tabindex="0" role="button"
                  data-path="${escapeHtml(r.path)}" aria-label="查看目录详情：${escapeHtml(r.path)}">
        <td class="path" title="${escapeHtml(r.path)}">
          <span class="cell-path">${escapeHtml(r.path)}</span>
          <button class="copy-path" type="button" data-copy="${escapeHtml(r.path)}"
                  aria-label="复制路径 ${escapeHtml(r.path)}" title="复制路径">复制</button>
        </td>
        <td class="num">${prevTxt}</td>
        <td class="num">${currTxt}</td>
        <td class="num">${deltaCell(r)}</td>
        <td>${statusBadge(r.status)}</td>
        <td>
          <span class="row-actions">
            <button class="btn-mini" data-detail="${escapeHtml(r.path)}"
                    aria-label="查看目录详情" title="查看目录详情">${icon("folderOpen", 14)}</button>
            <button class="btn-mini" data-reveal="${escapeHtml(r.path)}"
                    aria-label="在 Finder 中显示" title="在 Finder 中显示">${icon("folderOpen", 14)}</button>
          </span>
        </td>
      </tr>`;
    }).join("");
  }
  // 更新排序表头 aria-sort
  document.querySelectorAll("#changes-table th").forEach((th) => {
    const btn = th.querySelector(".th-sort");
    if (!btn) return;
    if (btn.dataset.sort === currentSort.key) {
      th.setAttribute("aria-sort", currentSort.dir === -1 ? "descending" : "ascending");
    } else {
      th.removeAttribute("aria-sort");
    }
  });
  bindTableEvents();
}

function bindTableEvents() {
  const tbody = document.getElementById("changes-body");
  // 行点击 / Enter 键：打开详情
  tbody.querySelectorAll("tr.focusable").forEach((tr) => {
    tr.addEventListener("click", (e) => {
      // 避免按钮 / 复制的点击冒泡导致详情打开
      if (e.target.closest("button")) return;
      openDetail(tr.dataset.path, tr);
    });
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openDetail(tr.dataset.path, tr);
      }
    });
  });
  tbody.querySelectorAll("[data-detail]").forEach((b) =>
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      const tr = b.closest("tr");
      openDetail(b.dataset.detail, tr);
    }));
  tbody.querySelectorAll("[data-reveal]").forEach((b) =>
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      revealInFinder(b.dataset.reveal);
    }));
  tbody.querySelectorAll("[data-copy]").forEach((b) =>
    b.addEventListener("click", async (e) => {
      e.stopPropagation();
      const text = b.dataset.copy;
      try {
        if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
        else {
          const ta = document.createElement("textarea");
          ta.value = text;
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          document.body.removeChild(ta);
        }
        b.textContent = "已复制";
        b.classList.add("copied");
        setTimeout(() => {
          b.textContent = "复制";
          b.classList.remove("copied");
        }, 1200);
      } catch (_) {
        // 复制失败：不冒充成功；保留原文字
        b.textContent = "复制失败";
        setTimeout(() => { b.textContent = "复制"; }, 1200);
      }
    }));
}

function renderNetLine(d) {
  const net = computeNet(d);
  const netEl = document.getElementById("changes-net");
  const footEl = document.getElementById("changes-foot");
  const measured = (d.grown || []).length + (d.shrunk || []).length;
  const unrec = (d.added || []).length + (d.removed || []).length;
  const span = `${String(d.a.created_at).slice(0, 10)} → ${String(d.b.created_at).slice(0, 10)}`;
  if (measured === 0 && unrec > 0) {
    netEl.innerHTML = `<span>已测量同口径净变化 <strong class="delta-none">—</strong></span>` +
      `<span class="hint">${span} 期间没有可测量变化，另有 ${unrec} 个未记录目录，不能判为"无变化"。</span>`;
  } else {
    netEl.innerHTML = `<span>已测量同口径净变化 <strong class="${
      net > 0 ? "delta-grow" : net < 0 ? "delta-shrink" : "delta-none"
    }">${escapeHtml(fmtDelta(net))}</strong></span>` +
      `<span class="hint">同根同口径，根目录 ${escapeHtml(fmtKB(d.a.total_kb || 0))} → ${escapeHtml(fmtKB(d.b.total_kb || 0))}</span>`;
  }
  netEl.hidden = false;
  footEl.hidden = false;
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
    lastDiff = d;
    renderDeltaBars("chart-grown", d.grown, "#d64545");
    renderDeltaBars("chart-shrunk", d.shrunk, "#2e9e5b");
    fillTwoColTable("tbl-added", d.added, (r) => [r.path, fmtKB(r.new_kb)]);
    fillTwoColTable("tbl-removed", d.removed, (r) => [r.path, fmtKB(r.old_kb)]);
    currentRows = synthesizeRows(d);
    renderNetLine(d);
    renderChangesTable();
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
      return `${escapeHtml(r.path)}<br/>${fmtKB(r.old_kb)} → ${fmtKB(r.new_kb)}<br/>变化 ${fmtDelta(r.delta_kb)}`;
    } },
    grid: { left: 150, right: 50, top: 6, bottom: 24 },
    xAxis: { type: "value", axisLabel: { formatter: (v) => fmtDelta(v) } },
    yAxis: { type: "category", data: top.map((r) => r.path.length > 32
      ? r.path.slice(0, 14) + "…" + r.path.slice(-15) : r.path),
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
    tbody.innerHTML = '<tr><td colspan="3" style="color:var(--muted)">无</td></tr>';
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

/* ---------- 目录详情侧栏（DESIGN：选中后查看趋势与证据，Esc 关闭，焦点返回触发行） ---------- */

function openDetail(path, sourceRow) {
  activeDetailPath = path;
  activeDetailRow = sourceRow || null;
  const el = document.getElementById("changes-detail");
  el.hidden = false;
  // 标记当前行为选中（视觉焦点）
  if (activeDetailRow) {
    document.querySelectorAll("#changes-body tr.focusable.selected").forEach((t) =>
      t.classList.remove("selected"));
    activeDetailRow.classList.add("selected");
  }
  // 关闭按钮聚焦：键盘可达
  el.innerHTML = renderDetailSkeleton(path);
  const closeBtn = el.querySelector(".detail-close");
  if (closeBtn) {
    closeBtn.addEventListener("click", () => closeDetail());
  }
  // 复制路径
  el.querySelectorAll("[data-copy]").forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(b.dataset.copy);
        b.textContent = "已复制";
        b.classList.add("copied");
        setTimeout(() => { b.textContent = "复制路径"; b.classList.remove("copied"); }, 1200);
      } catch (_) { /* noop */ }
    }));
  // Finder 显示
  el.querySelectorAll("[data-reveal]").forEach((b) =>
    b.addEventListener("click", () => revealInFinder(b.dataset.reveal)));
  // 加载 trend + browse（可独立失败，保持高度）
  loadDetailTrend(path);
  loadDetailBrowse(path);
  closeBtn?.focus();
}

function renderDetailSkeleton(path) {
  const row = currentRows.find((r) => r.path === path);
  const prev = row && row.prev != null ? fmtKB(row.prev) : "—";
  const curr = row && row.curr != null ? fmtKB(row.curr) : "—";
  const delta = row && row.delta != null ? fmtDelta(row.delta) : "—";
  return `
    <div class="detail-head">
      <h2>目录详情</h2>
      <button class="detail-close" type="button" aria-label="关闭详情" title="关闭（Esc）">${icon("x", 14)}</button>
    </div>
    <div class="detail-path">${escapeHtml(path)}</div>
    <div class="detail-stats">
      <div><div class="stat-label">之前</div><div class="stat-value">${escapeHtml(prev)}</div></div>
      <div><div class="stat-label">现在</div><div class="stat-value">${escapeHtml(curr)}</div></div>
      <div><div class="stat-label">变化</div><div class="stat-value">${escapeHtml(delta)}</div></div>
      <div><div class="stat-label">状态</div><div class="stat-value">${row ? statusBadge(row.status) : "—"}</div></div>
    </div>
    <div class="detail-section">
      <h3>历史趋势</h3>
      <div id="detail-trend-chart" class="detail-trend">${icon("fileText", 14)} <span class="hint">加载中…</span></div>
    </div>
    <div class="detail-section">
      <h3>当前所在</h3>
      <div id="detail-current-info" class="hint">加载中…</div>
    </div>
    <div class="detail-actions">
      <button class="copy-path" type="button" data-copy="${escapeHtml(path)}" title="复制路径">复制路径</button>
      <button class="btn-mini" data-reveal="${escapeHtml(path)}" aria-label="在 Finder 中显示" title="在 Finder 中显示">${icon("folderOpen", 14)}</button>
    </div>
  `;
}

function closeDetail() {
  const el = document.getElementById("changes-detail");
  if (el) el.hidden = true;
  activeDetailPath = null;
  document.querySelectorAll("#changes-body tr.focusable.selected").forEach((t) =>
    t.classList.remove("selected"));
  // 焦点返回触发行（DESIGN：Esc 关闭后焦点继续可用）
  if (activeDetailRow && document.body.contains(activeDetailRow)) {
    activeDetailRow.focus();
  }
  activeDetailRow = null;
}

async function loadDetailTrend(path) {
  const target = document.getElementById("detail-trend-chart");
  if (!target) return;
  const request = beginRequest("detailTrend");
  try {
    const r = await fetchJSON(`/api/trend?path=${encodeURIComponent(path)}`);
    if (!request.current() || activeDetailPath !== path) return;
    const pts = r.points || [];
    if (!pts.length) {
      target.innerHTML = `<span class="hint">该路径此前未记录（不冒充增长）。</span>`;
      return;
    }
    // 简易折线（保持高度）
    const xs = pts.map((p) => String(p.created_at || "").slice(5, 10));
    const ys = pts.map((p) => p.size_kb || 0);
    const chart = echarts.init(target);
    chart.setOption({
      tooltip: { trigger: "axis", formatter: (ps) => ps.map((p, i) =>
        `${xs[i]}<br/>${fmtKB(ys[i])}`).join("<br/>") },
      grid: { left: 50, right: 8, top: 8, bottom: 22 },
      xAxis: { type: "category", data: xs, axisLabel: { fontSize: 10 } },
      yAxis: { type: "value", axisLabel: { formatter: (v) => fmtKB(v), fontSize: 10 }, scale: true },
      series: [{ type: "line", smooth: true, symbol: "circle", symbolSize: 5,
        data: ys, itemStyle: { color: "#2f6fed" }, lineStyle: { width: 2 } }],
    }, true);
    // 当侧栏关闭或被复用时回收实例
    request.current();
  } catch (e) {
    if (!request.current() || activeDetailPath !== path) return;
    target.innerHTML = `<span class="hint">趋势加载失败${e.status ? `（HTTP ${e.status}）` : ""}</span>`;
  }
}

async function loadDetailBrowse(path) {
  const target = document.getElementById("detail-current-info");
  if (!target) return;
  const request = beginRequest("detailBrowse");
  try {
    const r = await fetchJSON(`/api/browse?path=${encodeURIComponent(path)}`);
    if (!request.current() || activeDetailPath !== path) return;
    const childN = (r.children || []).length;
    const trendN = (r.trend || []).length;
    target.innerHTML = `<span>当前大小：<strong>${escapeHtml(fmtKB(r.size_kb || 0))}</strong></span>` +
      (r.delta_kb != null
        ? `<span class="num">较上快照：${escapeHtml(fmtDelta(r.delta_kb))}</span>` : "") +
      `<span class="hint">直属子目录 ${childN} 个；该路径同数据集历史 ${trendN} 个点</span>`;
  } catch (e) {
    if (!request.current() || activeDetailPath !== path) return;
    if (e.status === 409) {
      target.innerHTML = `<span class="hint">尚无快照，无法显示当前所在。</span>`;
    } else if (e.status === 0) {
      target.innerHTML = `<span class="hint">无法连接本地服务。</span>`;
    } else {
      target.innerHTML = `<span class="hint">当前所在加载失败（HTTP ${e.status || "?"}）</span>`;
    }
  }
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

export const changesPage = {
  id: "changes",
  load() {
    loadSnapshotsForDiff();
    loadReportList();
  },
  init() {
    document.getElementById("btn-diff").addEventListener("click", () => loadDiff());
    ["sel-a", "sel-b"].forEach((id) => {
      document.getElementById(id).addEventListener("change", () => {
        snapshotSelectionRevision += 1;
        invalidateRequest("diff");
        clearDiffResults();
        setDiffStatus("快照选择已更改，点击“对比”加载结果。");
      });
    });
    // 排序表头
    document.querySelectorAll("#changes-table .th-sort").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.dataset.sort;
        if (currentSort.key === key) {
          currentSort.dir = -currentSort.dir;
        } else {
          currentSort.key = key; currentSort.dir = key === "path" ? 1 : -1;
        }
        renderChangesTable();
      });
    });
    // 搜索
    const search = document.getElementById("changes-search");
    if (search) search.addEventListener("input", () => renderChangesTable());
    // 全局 Esc 关闭详情
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && activeDetailPath) {
        e.preventDefault();
        closeDetail();
      }
    });
  },
  leave() {
    ["snapshots", "diff", "reportList", "reportContent",
     "detailTrend", "detailBrowse"].forEach(invalidateRequest);
    closeDetail();
  },
};
