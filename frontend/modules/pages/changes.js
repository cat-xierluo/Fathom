/* 变化页：快照对比（世代号防倒序 + 用户改选保护 + 404 协调）、可排序变化表、
 * 选中目录详情（侧栏 + Esc 焦点返回）、历史日报。
 *
 * 前端责任（ISS-027 模块合同）：
 * - beginRequest 世代号 + pageScoped：迟到的旧响应不能覆盖较新查询；
 * - 仅 frontend/icons.js 的 SVG 图标；零 emoji；
 *
 * 展示（ISS-028）：
 * - 净变化口径：根同口径差分（b.total_kb − a.total_kb），缺根总量显示"无基线"，
 *   绝不把列表行求和当净变化（行含父子重叠且被截断）；
 * - 状态列：已测量 / 未记录（首次进入统计） / 受限（权限不足时不可知）；
 * - 键盘可达：表行可 Tab 聚焦、Enter 打开详情、Esc 关闭后焦点返回触发行；
 * - 长路径：可一键复制（DESIGN 关键可达性约束）；
 * - 目录详情：路径 + 净变化 + 趋势（来自 /api/trend）+ 当前大小（/api/browse）。
 *
 * 交互（ISS-093）：选择即比对——两个 select 改选后在选齐时自动加载对比，
 * 无确认按钮；未选齐保持空态 + 引导文案；对比失败在状态行内展示
 * 「重试」小按钮（原确认按钮兼任的失败重试语义收拢到失败态）。
 * 连点竞态由既有 diff 域世代号守卫覆盖（迟到的旧响应不得写入 DOM）；
 * 结果区刷新不抢焦点（改选触发的详情关闭不回焦到已销毁的行）。
 *
 * 分区（ISS-094）：页头下横向 tab 条（比对明细/增长最多/缩减最多/新出现/
 * 消失，默认比对明细；历史日报是 tab 外页尾常驻区）。数据加载逻辑不变
 * ——loadDiff 一次渲染全部分区，tab 只切可见性；隐藏分区里初始化的图表
 * 由 tabs.js 激活时经 resumeChartsIn 恢复尺寸。tab 状态记 hash
 * （#/changes/grown；刷新保持由 app.js 在路由前规范化承接，见 tabs.js）。
 *
 * 共享对比上下文（ISS-107）：基线/对比 select、状态行与净变化口径行在
 * tab 条下方的共享面板中，不随明细分区隐藏——任一分区都能判断当前对比
 * 什么并改选日期触发重查。状态三态与实际请求一致：加载中/已完成
 * （「已对比快照 #a → #b」）/失败（内联重试），成功渲染后不得滞留
 * 「正在对比」。增长/缩减图表下方提供同源 Top 12 数据表（含 Finder 入口），
 * 读数不依赖图表悬停。
 */
import { fetchJSON, beginRequest, invalidateRequest, revealInFinder, apiPost, apiSend } from "../request.js";
import { fmtKB, fmtDelta, shortPath, escapeHtml } from "../format.js";
import { initChart, hasChart, clearChart } from "../charts.js";
import { initPageTabs } from "../tabs.js";
import { icon } from "../../icons.js";

/* ISS-084：图表色与 style.css :root 语义 token 同源（单一色源，不硬编码）。
 * 脚本为 module（defer），执行时 CSSOM 已就绪。 */
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

let snapshotSelectionRevision = 0;  // 用户改选计数：晚到的快照列表不得覆盖改选结果
let currentSort = { key: "delta", dir: -1 };
let currentRows = [];
let lastDiff = null;                 // 最近一次成功 diff，用于详情
let activeDetailPath = null;          // 当前详情目录
let activeDetailRow = null;           // 当前详情触发行（Esc 后焦点回此）
let changesTabs = null;               // ISS-094 页内二级导航（五分区 tab）

function setDiffStatus(message) {
  // textContent 赋值整体替换子节点：若此前失败态挂了「重试」按钮，此处一并清除。
  document.getElementById("diff-status").textContent = message;
}

/* ISS-093：对比失败态——文案 + 状态行内联「重试」小按钮。
 * 原确认按钮兼任的失败重试语义收拢到这里；任何后续 setDiffStatus
 * （加载中/成功/新的失败）都会清掉旧按钮，不残留。 */
function showDiffFailure(message) {
  const el = document.getElementById("diff-status");
  el.replaceChildren(document.createTextNode(message));
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "diff-retry";
  retry.textContent = "重试";
  retry.setAttribute("aria-label", "重新加载快照对比");
  retry.addEventListener("click", () => loadDiff());
  el.appendChild(retry);
}

function setDiffControlsEnabled(enabled) {
  document.getElementById("sel-a").disabled = !enabled;
  document.getElementById("sel-b").disabled = !enabled;
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
  // ISS-107：增长/缩减分区的图表同源数据表随结果一起清空
  ["tbl-grown-top", "tbl-shrunk-top"].forEach((id) => {
    document.querySelector(`#${id} tbody`).innerHTML =
      '<tr><td colspan="5" class="hint">暂无可比较数据</td></tr>';
  });
  document.getElementById("changes-body").innerHTML =
    '<tr><td colspan="6" class="hint">暂无可比较数据</td></tr>';
  document.getElementById("changes-net").hidden = true;
  document.getElementById("changes-foot").hidden = true;
  // ISS-035C：解读区随对比结果一起隐藏并重置会话态（离页/改选不隐式重发）
  resetAnalysisSession({ hide: true });
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
  // 根同口径净变化 = b.total_kb − a.total_kb（同一响应内两快照的根累计 KiB）。
  // grown/shrunk 行按 fold_changes 设计可含父子重叠（父行包含子行，DEC-005），
  // 且经 topn 截断与 min_delta_kb 过滤——逐行求和不等于任何口径的净变化，
  // 禁止回退到求和。任一侧缺 total_kb（如无基线）时返回 null。
  const prev = Number(d.a && d.a.total_kb), curr = Number(d.b && d.b.total_kb);
  if (!Number.isFinite(prev) || !Number.isFinite(curr)) return null;
  return curr - prev;
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
      search ? `当前对比结果中没有匹配“${escapeHtml(search)}”的目录。` : "暂无可比较数据"
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
  const span = `${String(d.a?.created_at || "").slice(0, 10)} → ${String(d.b?.created_at || "").slice(0, 10)}`;
  const netHtml = net == null
    ? `<strong class="delta-none">无基线</strong>`
    : `<strong class="${net > 0 ? "delta-grow" : net < 0 ? "delta-shrink" : "delta-none"}">${escapeHtml(fmtDelta(net))}</strong>`;
  // 根目录 X → Y 与净变化同源（都是 a/b.total_kb），数值上严格一致
  const rootNote = net == null
    ? `<span class="hint">${span} 快照缺少根总量（无基线），净变化不可知；不用列表行求和推算。</span>`
    : `<span class="hint">根同口径差分：根目录 ${escapeHtml(fmtKB(d.a.total_kb))} → ${escapeHtml(fmtKB(d.b.total_kb))}（行值不可相加）</span>`;
  const unrecNote = measured === 0 && unrec > 0
    ? `<span class="hint">${span} 期间没有 ≥1MB 可测量行，另有 ${unrec} 个未记录目录，不能判为"无变化"。</span>`
    : "";
  netEl.innerHTML = `<span>根同口径净变化 ${netHtml}</span>` + rootNote + unrecNote;
  netEl.hidden = false;
  footEl.hidden = false;
}

/* ISS-093：选择即比对——select change 后选齐即自动加载，无确认按钮。
 * 连点触发多次 loadDiff 时，diff 域世代号守卫保证只渲染最后一次；
 * 未选齐（某侧为空）保持空态 + 引导文案，不发请求。
 * select 的 change 由键盘改选同样派发（原生行为），路径不变。 */
function onSelectionChange() {
  snapshotSelectionRevision += 1;
  const a = document.getElementById("sel-a").value;
  const b = document.getElementById("sel-b").value;
  if (!a || !b) {
    invalidateRequest("diff");
    clearDiffResults();
    setDiffStatus("请选择基线与对比快照，选齐后自动对比。");
    return;
  }
  // 改选使旧对比的详情侧栏失效：关闭但不回焦（焦点留在用户正在操作的 select）。
  closeDetail({ restoreFocus: false });
  loadDiff();
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
    renderDeltaBars("chart-grown", d.grown, cssVar("--grow"));
    renderDeltaBars("chart-shrunk", d.shrunk, cssVar("--mineral"));
    fillDeltaTable("tbl-grown-top", d.grown);
    fillDeltaTable("tbl-shrunk-top", d.shrunk);
    fillTwoColTable("tbl-added", d.added, (r) => [r.path, fmtKB(r.new_kb)]);
    fillTwoColTable("tbl-removed", d.removed, (r) => [r.path, fmtKB(r.old_kb)]);
    currentRows = synthesizeRows(d);
    renderNetLine(d);
    renderChangesTable();
    // ISS-107：渲染已完成，状态必须是完成态——不得滞留「正在对比」。
    setDiffStatus(successMessage || `已对比快照 #${a} → #${b}`);
    // ISS-035C：基础对比就绪后加载解读区（跟随当前 a/b；可独立失败）。
    loadAnalysisPanel();
  } catch (e) {
    if (!request.current()) return;
    if (e.status === 404 && retryOnMissing) {
      clearDiffResults();
      await loadSnapshotsForDiff({ notice: "所选快照已更新或不再可用，已切换到最近有效快照。" });
      return;
    }
    clearDiffResults();
    if (e.status === 409) {
      // 状态性约束（缺两个不同日期的有效快照）：重试不改变前提，保持纯文案。
      setDiffStatus("还不能比较：需要两个不同日期的有效快照。");
    } else if (e.status === 0) {
      showDiffFailure("无法连接本地服务，快照对比暂不可用。");
    } else {
      showDiffFailure(`快照对比加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`);
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

/* ISS-107：增长/缩减图表的等价可读数据表（Top 12，与图同源）。
 * 图 hover 才能读数、canvas 文本不可选中——表格让读数不依赖指针悬停，
 * 且每行带 Finder 入口（与 added/removed 表同一目录证据语义）。 */
function fillDeltaTable(id, rows) {
  const tbody = document.querySelector(`#${id} tbody`);
  tbody.innerHTML = "";
  const top = (rows || []).slice(0, 12);
  if (!top.length) {
    tbody.innerHTML = '<tr><td colspan="5" style="color:var(--muted)">无</td></tr>';
    return;
  }
  top.forEach((r) => {
    const tr = document.createElement("tr");
    const deltaCls = r.delta_kb > 0 ? "delta-grow" : r.delta_kb < 0 ? "delta-shrink" : "";
    tr.innerHTML = `<td class="path" title="${escapeHtml(r.path)}">${escapeHtml(r.path)}</td>` +
      `<td class="num">${escapeHtml(fmtKB(r.old_kb))}</td>` +
      `<td class="num">${escapeHtml(fmtKB(r.new_kb))}</td>` +
      `<td class="num ${deltaCls}">${escapeHtml(fmtDelta(r.delta_kb))}</td>` +
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
      <h2>${icon("brandRing", 15, "detail-dr")}目录详情</h2>
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

function closeDetail({ restoreFocus = true } = {}) {
  const el = document.getElementById("changes-detail");
  if (el) el.hidden = true;
  activeDetailPath = null;
  document.querySelectorAll("#changes-body tr.focusable.selected").forEach((t) =>
    t.classList.remove("selected"));
  // 焦点返回触发行（DESIGN：Esc 关闭后焦点继续可用）。
  // restoreFocus=false 用于自动刷新路径（ISS-093：改选触发时焦点应留在
  // select 上，结果区刷新不得抢焦点）。
  if (restoreFocus && activeDetailRow && document.body.contains(activeDetailRow)) {
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
        data: ys, itemStyle: { color: cssVar("--trench") }, lineStyle: { width: 2 } }],
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

/* ---------- AI 解读（ISS-035C）：净变化之后、变化表之前的七主态区 ----------
 *
 * 方案 §7 合同：
 * - 七主态：未启用 / 未分析 / 发送预览 / 运行中 / 完成 / 失败 / 过期；
 *   取消/超时/中断以明确原因和手动重试动作呈现；
 * - 预览展示区间、范围与截断（manifest）、发送对象、完整发送文本入口
 *   （prompt_text 展开）、本地保存说明；确认仅一次（幂等键会话生成）；
 * - 世代守卫：解读区独立请求域 + 终态前比对 job 的 a/b 与当前选择——
 *   改选快照后迟到的旧响应不得覆盖新区间的视图（反例 2）；
 * - 离页不隐式重发：离开页面只作废请求域与轮询，不取消已授权任务；
 *   重入按 sessionStorage 记录的原 job 恢复显示（GET jobs/{id} 纯读），
 *   刷新页面不会重复发送（POST jobs 计数不变，反例 4）；
 * - evidence_id 跳转到该报告保存的事实条目；能对位时在变化表内定位，
 *   脱敏路径无法对位时如实说明，不猜（@user 段）；
 * - AI 不可用不影响基础事实：本区任何失败都只发生在本区内部。
 */

const ANALYSIS_PANEL_ID = "analysis-panel";
const ANALYSIS_BODY_ID = "analysis-body";
const ANALYSIS_POLL_MS = 2000;

/* 会话状态：改选区间即重置（preview/job 与旧区间的关联一并作废）。 */
let analysisCfg = null;        // GET /api/config 的 analysis 块
let analysisPreview = null;    // 当前预览（previews 响应体）
let analysisJob = null;        // 最近 job 视图（运行中/终态）
let analysisJobTimer = null;   // 轮询句柄

/* job 失败 reason_code 的可读文案；未登记值原样展示不吞字。 */
const ANALYSIS_JOB_REASON = {
  runner_timed_out: "分析超时：超过单次时限已被终止，可重试",
  runner_cancelled: "分析已取消（已发送部分无法撤回）",
  runner_nonzero_exit: "引擎命令行异常退出",
  runner_spawn_failed: "引擎无法启动（可能已被移动或损坏）",
  runner_output_limit: "引擎输出超出限额，结果不可信",
  runner_decode_error: "引擎输出无法按 UTF-8 解码",
  parse_failed_bad_json: "引擎返回内容不是有效 JSON",
  parse_failed_not_result: "引擎返回缺少 result 结构",
  parse_failed_missing_field: "引擎返回缺少必需字段",
  app_error: "引擎报告了应用层错误",
  cancelled_before_start: "分析在开始前被取消",
  owner_exit: "服务中断：本次分析未完成，不会自动重试",
};

/* 事实条目 kind 的中文说明（与 facts 包 entry_legend 同口径）。 */
const ANALYSIS_ENTRY_KIND = {
  measured: "已测量（两次都被记录，增减为实测）",
  first_recorded: "首次进入统计（刚越过入库阈值；不是文件系统新建）",
  unrecorded: "未记录（可能跌破阈值、权限受限或已被移除；不是删除证据）",
};

function analysisPanelEl() { return document.getElementById(ANALYSIS_PANEL_ID); }
function analysisBodyEl() { return document.getElementById(ANALYSIS_BODY_ID); }

function analysisSel() {
  return {
    a: document.getElementById("sel-a").value,
    b: document.getElementById("sel-b").value,
  };
}

function sameAnalysisRange(obj, sel) {
  return obj && String(obj.a_snapshot_id ?? obj.a) === String(sel.a) &&
    String(obj.b_snapshot_id ?? obj.b) === String(sel.b);
}

/** 读回本区间当前尝试的幂等记录（含是否已终态）。 */
function analysisIdemRecord(sel) {
  try {
    const raw = sessionStorage.getItem(`fathom-aidem:${sel.a}->${sel.b}`);
    if (!raw) return null;
    const saved = JSON.parse(raw);
    return saved && typeof saved.key === "string" ? saved : null;
  } catch (_) { return null; }  /* 隐私模式等场景不可用 */
}

/** 记下本次尝试的 job，供刷新/离页后按 job_id 纯读重入。 */
function saveAnalysisAttempt(sel, { key, digest, job, replayed }) {
  try {
    sessionStorage.setItem(`fathom-aidem:${sel.a}->${sel.b}`, JSON.stringify({
      key, digest,
      job_id: job?.job_id ?? null,
      status: job?.status ?? null,
      terminal: Boolean(job?.terminal),
    }));
  } catch (_) { /* 隐私模式等场景：仅当前会话内可恢复 */ }
}

/** 会话幂等键（确认仅一次的载体）：按区间存 sessionStorage；
 * 绑定 request_digest——digest 变了（新预览）就换新键，避免
 * idempotency_conflict。
 *
 * 键的生命周期绑定「一次用户授权的尝试」，而不是区间（ISS-128）：
 *  - 同 digest 且上次尝试**仍在途** → 复用该键。这是传输层重试与双击，
 *    它们属于同一次尝试，必须收敛到同一份派发。
 *  - 同 digest 但上次尝试**已终态** → 新的用户尝试（重跑、取消后重试、
 *    撤销后再次分析、换引擎跑同一事实）发新键。否则后端只会重放旧终态，
 *    用户点了「重新分析」却看到上一次的旧结果。
 * 刷新重入不走这里：它用 analysisSavedJobId 做纯 GET，不会重复派发。 */
function analysisIdemKey(sel, digest, { create } = {}) {
  const saved = analysisIdemRecord(sel);
  if (saved && saved.digest === digest && !saved.terminal) return saved.key;
  if (!create) return null;
  const key = (crypto.randomUUID ? crypto.randomUUID() : `idem-${Date.now()}-${Math.random()}`);
  saveAnalysisAttempt(sel, { key, digest, job: null, replayed: false });
  return key;
}

/** 改选区间/清空结果时重置会话态；hide=true 同时收起解读区。 */
function resetAnalysisSession({ hide = false } = {}) {
  clearTimeout(analysisJobTimer);
  analysisJobTimer = null;
  analysisPreview = null;
  analysisJob = null;
  if (hide) {
    const panel = analysisPanelEl();
    if (panel) panel.hidden = true;
  }
}

/** 解读区加载失败/离线的就地重试（基础事实不受影响）。 */
function showAnalysisRetry(message) {
  const el = analysisBodyEl();
  el.replaceChildren(document.createTextNode(message));
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "diff-retry";
  retry.textContent = "重试";
  retry.setAttribute("aria-label", "重新加载 AI 解读状态");
  retry.addEventListener("click", () => loadAnalysisPanel());
  el.appendChild(retry);
}

/* ----- 渲染入口：state = {state: 七态之一, ...载荷} ----- */

function renderAnalysis(state) {
  const body = analysisBodyEl();
  const panel = analysisPanelEl();
  if (!body || !panel) return;
  panel.hidden = false;
  switch (state.state) {
    case "disabled": {
      body.innerHTML = `
        <p class="hint" data-test="analysis-state-disabled">AI 解读未启用：下方变化表等基础事实照常可用，不会发送任何数据。</p>
        <div class="perm-link-row">
          <button type="button" class="btn" data-test="analysis-goto-settings">${icon("settings", 14)} 前往设置开启</button>
        </div>`;
      body.querySelector("[data-test='analysis-goto-settings']").addEventListener("click", () => {
        window.__fathomSettingsSectionPending = "analysis";
        location.hash = "#/settings";
      });
      return;
    }
    case "idle": {
      const sel = analysisSel();
      body.innerHTML = `
        <p class="hint" data-test="analysis-state-idle">区间 #${escapeHtml(sel.a)} → #${escapeHtml(sel.b)} 还没有 AI 解读。
          生成发送预览后，你可以先查看将要发送的完整内容再确认；不确认就不会发送。</p>
        <div class="perm-link-row">
          <button type="button" class="btn" data-test="analysis-preview-btn">${icon("sparkles", 14)} 生成发送预览</button>
        </div>
        <p class="hint">生成预览只在本机整理事实包（零外传）；确认后才交给所选引擎分析。</p>`;
      body.querySelector("[data-test='analysis-preview-btn']").addEventListener("click", startAnalysisPreview);
      return;
    }
    case "preview": return renderAnalysisPreview(state);
    case "running": {
      const job = state.job || {};
      const cancelling = state.cancelling;
      body.innerHTML = `
        <p class="hint" data-test="analysis-state-running">
          <span class="dr-loading" data-dr-spin aria-hidden="true"></span>
          正在分析（区间 #${escapeHtml(String(job.a_snapshot_id))} → #${escapeHtml(String(job.b_snapshot_id))}，任务 ${escapeHtml(String(job.job_id || "").slice(0, 8))}）…
        </p>
        <p class="hint">关闭页面不会取消分析；回到本页会自动恢复显示。取消只能停止后续处理，不能撤回已发送给引擎的数据。</p>
        <div class="perm-link-row">
          <button type="button" class="btn" data-test="analysis-cancel-btn"${cancelling ? " disabled" : ""}>${cancelling ? "正在取消…" : "取消分析"}</button>
        </div>`;
      body.querySelector("[data-test='analysis-cancel-btn']").addEventListener("click", cancelAnalysisJob);
      return;
    }
    case "failure": return renderAnalysisFailure(state);
    case "done":
    case "expired": return renderAnalysisResult(state);
    case "pollError": {
      body.innerHTML = `
        <p class="hint" data-test="analysis-poll-error">分析状态查询失败：${escapeHtml(state.error?.message || "")}；任务仍在后台，稍后自动恢复或手动重试。</p>
        <div class="perm-link-row">
          <button type="button" class="btn" data-test="analysis-poll-retry">重试查询</button>
        </div>`;
      body.querySelector("[data-test='analysis-poll-retry']").addEventListener("click", () => {
        if (analysisJob?.job_id) scheduleAnalysisPoll(analysisJob.job_id);
        else loadAnalysisPanel();
      });
      return;
    }
    default:
      body.innerHTML = `<p class="hint">解读状态未知，请刷新。</p>`;
  }
}

/* ----- 未启用 / 未分析 ----- */

/* ----- 发送预览（方案 §4.2：预览=将发送的不可变请求） ----- */

function renderAnalysisPreview({ preview }) {
  const body = analysisBodyEl();
  const m = preview.manifest || {};
  const sel = m.snapshots || {};
  const sampling = m.sampling || {};
  const truncation = m.truncation || {};
  const runtime = preview.runtime || {};
  const aDate = String(sel.a?.created_at || "").slice(0, 16).replace("T", " ");
  const bDate = String(sel.b?.created_at || "").slice(0, 16).replace("T", " ");
  const dataset = m.dataset || {};
  const omits = [];
  if (Number(sampling.omitted) > 0) {
    omits.push(`候选 ${sampling.total_candidates} 条，取样 ${sampling.selected} 条，另有 ${sampling.omitted} 条未进入本包`);
  }
  if (truncation.utf8_truncated) {
    omits.push(`发送文本超出字节上限，已截断 ${truncation.omitted_entries || "?"} 条`);
  }
  body.innerHTML = `
    <div data-test="analysis-state-preview">
      <p class="hint" data-test="analysis-preview-range">将分析区间：<strong>#${escapeHtml(String(sel.a?.snapshot_id))}（${escapeHtml(aDate)}）→ #${escapeHtml(String(sel.b?.snapshot_id))}（${escapeHtml(bDate)}）</strong></p>
      <p class="hint" data-test="analysis-preview-scope">范围：根 <code>${escapeHtml(dataset.root_display || "?")}</code> ·
        入库阈值 ${dataset.min_kb == null ? "未知" : escapeHtml(String(dataset.min_kb)) + " KB"} ·
        排除掩码 ${(dataset.exclude_names || []).length ? escapeHtml((dataset.exclude_names || []).join("; ")) : "无"} ·
        单位 ${escapeHtml(m.units || "KiB")}（子目录行与根累计有重叠，行值不可相加）</p>
      <p class="hint" data-test="analysis-preview-truncation">${omits.length
        ? `截断与取样：${escapeHtml(omits.join("；"))}。模型只看到取样子集。`
        : "全部过阈值候选已进入本包，无取样省略。"}</p>
      <p class="hint" data-test="analysis-preview-target">发送对象：本机命令行
        <strong>${escapeHtml(runtime.display_name || runtime.id || "?")}</strong>${runtime.version ? `（版本 ${escapeHtml(runtime.version)}）` : ""}——
        它可能把数据发送给它配置的模型服务；账号与额度由该命令行管理。</p>
      <p class="hint" data-test="analysis-preview-retain">解读结果与本次批准发送的事实包会保存在本机运行目录，可随时撤销；
        撤销不能清除该命令行或模型服务自身的留存。</p>
      <details class="cfg-details" data-test="analysis-preview-prompt-details">
        <summary>查看完整发送文本</summary>
        <pre class="analysis-prompt" data-test="analysis-prompt-text">${escapeHtml(preview.prompt_text || "")}</pre>
      </details>
      <div class="exclude-actions">
        <button type="button" class="btn primary" data-test="analysis-confirm-btn">确认并发送分析</button>
        <button type="button" class="btn" data-test="analysis-preview-cancel">取消（不发送）</button>
      </div>
      <p class="hint">预览 ${escapeHtml(String(preview.expires_in_s ?? "?"))} 秒后过期；确认只发送这一次，不会自动重发。</p>
    </div>`;
  body.querySelector("[data-test='analysis-confirm-btn']").addEventListener("click", () => confirmAnalysisSend(preview));
  body.querySelector("[data-test='analysis-preview-cancel']").addEventListener("click", () => {
    analysisPreview = null;
    renderAnalysis({ state: "idle" });
  });
}

/** 确认发送（仅一次）：按钮点击即禁用；幂等键按会话/区间取或建；
 * 同键同 digest 重入返回同一 job（后端幂等），不会重复发送。 */
async function confirmAnalysisSend(preview) {
  const body = analysisBodyEl();
  const btn = body.querySelector("[data-test='analysis-confirm-btn']");
  if (btn) { btn.disabled = true; btn.textContent = "已确认，正在提交…"; }
  const sel = analysisSel();
  if (!sameAnalysisRange({ a: preview.a?.snapshot_id, b: preview.b?.snapshot_id }, sel)) {
    // 用户在预览展示期间改选了区间：旧预览不再代表当前视图，直接作废
    resetAnalysisSession();
    loadAnalysisPanel();
    return;
  }
  const digest = preview.request_digest;
  const key = analysisIdemKey(sel, digest, { create: true });
  const request = beginRequest("analysis");
  try {
    const r = await apiPost("/api/analysis/jobs", {
      preview_id: preview.preview_id,
      request_digest: digest,
      idempotency_key: key,
    });
    if (!request.current()) return;
    const data = await r.json();
    // 记录原 job 供刷新/离页后重入恢复（GET jobs/{id} 纯读）。
    saveAnalysisAttempt(sel, { key, digest, job: data.job, replayed: data.replayed });
    analysisPreview = null;
    analysisJob = data.job;
    // 重放（replayed=true）不等于「正在运行」（ISS-128）：后端可能返回的是
    // 一个**已终态**的旧 job——此前这里无条件渲染 running 且不轮询，页面会
    // 永远停在运行中。现在按 job 自身的终态标记分流。
    if (data.job?.terminal) {
      await onAnalysisJobTerminal(data.job);
      return;
    }
    renderAnalysis({ state: "running", job: data.job });
    // 重放在途说明别处已有一次同键派发（典型是本会话较早的尝试）：继续轮询
    // 把它收敛到终态，而不是当作已经结束。
    scheduleAnalysisPoll(data.job.job_id);
  } catch (e) {
    if (!request.current()) return;
    handleAnalysisSendError(e);
  }
}

function handleAnalysisSendError(e) {
  const code = e.reason_code;
  if (code === "preview_expired" || code === "preview_not_found" || code === "preview_stale") {
    analysisPreview = null;
    renderAnalysis({ state: "idle" });
    const body = analysisBodyEl();
    const note = document.createElement("p");
    note.className = "hint cfg-error";
    note.setAttribute("data-test", "analysis-preview-stale");
    note.textContent = code === "preview_stale"
      ? "设置或数据已变化，原预览失效；请重新生成预览。"
      : "预览已过期（有效期 5 分钟）；请重新生成预览。";
    body.prepend(note);
    return;
  }
  if (code === "analysis_busy") {
    renderAnalysis({ state: "idle" });
    const body = analysisBodyEl();
    const note = document.createElement("p");
    note.className = "hint cfg-error";
    note.setAttribute("data-test", "analysis-busy");
    note.textContent = "已有分析在进行中（同一时间只允许一个）；等待其结束后再试，不会排队。";
    body.prepend(note);
    return;
  }
  if (code === "analysis_disabled") {
    renderAnalysis({ state: "disabled" });
    return;
  }
  // 其余（403 引擎不可用 / 404 快照失效 / 400 口径）：可读原因 + 回未分析
  const sel = analysisSel();
  renderAnalysis({ state: "idle" });
  const body = analysisBodyEl();
  const note = document.createElement("p");
  note.className = "hint cfg-error";
  note.setAttribute("data-test", "analysis-send-error");
  note.textContent = `无法开始分析：${e.message}`;
  body.prepend(note);
}

/* ----- 运行中：轮询（离页作废；重入恢复） ----- */

function scheduleAnalysisPoll(jobId) {
  clearTimeout(analysisJobTimer);
  analysisJobTimer = setTimeout(async () => {
    const request = beginRequest("analysisPoll");
    try {
      const r = await fetchJSON(`/api/analysis/jobs/${encodeURIComponent(jobId)}`);
      if (!request.current()) return;
      const job = r.job;
      // 区间世代守卫：job 不属于当前选择的区间时不写入（迟到响应）
      if (!sameAnalysisRange(job, analysisSel())) return;
      analysisJob = job;
      if (job.terminal) {
        await onAnalysisJobTerminal(job);
        return;
      }
      renderAnalysis({ state: "running", job, cancelling: job.status === "cancelling" });
      scheduleAnalysisPoll(jobId);
    } catch (e) {
      if (!request.current()) return;
      renderAnalysis({ state: "pollError", error: e });
    }
  }, ANALYSIS_POLL_MS);
}

async function onAnalysisJobTerminal(job) {
  // 无论哪种终态，都把本次尝试标记为「已结束」：下一次用户发起的尝试会因此
  // 拿到新键，不会重放这一次的旧终态（ISS-128）。此前只在部分状态下清键，
  // 且恰好漏掉 succeeded——成功保留旧键，用户点「重新分析」只会拿到上一次的
  // 结果。job_id 仍保留，刷新重入（纯 GET）不受影响。
  markAnalysisAttemptTerminal(job);
  if (job.status === "succeeded") {
    // 终态成功：以 analyses 读取层为准（含过期评估），不自行渲染 stdout
    await loadAnalysisPanel();
    return;
  }
  renderAnalysis({ state: "failure", job });
}

/** 把指定区间（默认当前区间）的尝试标记为已终态；保留 job_id 供重入。 */
function markAnalysisAttemptTerminal(job, sel = analysisSel()) {
  const saved = analysisIdemRecord(sel);
  if (!saved) return;
  try {
    sessionStorage.setItem(`fathom-aidem:${sel.a}->${sel.b}`, JSON.stringify({
      ...saved,
      job_id: job?.job_id ?? saved.job_id ?? null,
      status: job?.status ?? null,
      terminal: true,
    }));
  } catch (_) { /* 隐私模式等场景：仅当前会话内可恢复 */ }
}

async function cancelAnalysisJob() {
  const job = analysisJob;
  if (!job?.job_id) return;
  renderAnalysis({ state: "running", job, cancelling: true });
  const request = beginRequest("analysis");
  try {
    await apiPost(`/api/analysis/jobs/${encodeURIComponent(job.job_id)}/cancel`, {});
    if (!request.current()) return;
    // 取消受理后由轮询收敛终态（cancelling → cancelled）
    scheduleAnalysisPoll(job.job_id);
  } catch (e) {
    if (!request.current()) return;
    if (e.status === 409) {
      // 终态竞争：任务已结束，取消不再生效——按最终状态呈现（响应已带 job）
      const finalJob = e.body?.job;
      if (finalJob && finalJob.terminal) {
        analysisJob = finalJob;
        await onAnalysisJobTerminal(finalJob);
        return;
      }
      scheduleAnalysisPoll(job.job_id);
      return;
    }
    renderAnalysis({ state: "running", job });
    const body = analysisBodyEl();
    const note = document.createElement("p");
    note.className = "hint cfg-error";
    note.textContent = `取消请求失败：${e.message}；可再次尝试。`;
    body.appendChild(note);
  }
}

/* ----- 失败 / 过期 / 完成 ----- */

function renderAnalysisFailure({ job }) {
  const body = analysisBodyEl();
  const sel = analysisSel();
  const status = job?.status || "failed";
  const code = job?.reason_code;
  const reason = ANALYSIS_JOB_REASON[code] || code || "未知原因";
  const rangeText = job && job.a_snapshot_id != null
    ? `#${escapeHtml(String(job.a_snapshot_id))} → #${escapeHtml(String(job.b_snapshot_id))}`
    : `#${escapeHtml(sel.a)} → #${escapeHtml(sel.b)}`;
  const headline = status === "cancelled" ? "分析已取消"
    : status === "timed_out" ? "分析超时"
      : status === "interrupted" ? "分析被中断"
        : status === "cancelling" ? "正在取消" : "分析失败";
  body.innerHTML = `
    <div data-test="analysis-state-failure">
      <p class="hint" data-test="analysis-failure-headline"><strong>${escapeHtml(headline)}</strong></p>
      <p class="hint" data-test="analysis-failure-reason">原因：${escapeHtml(reason)}${code ? `（${escapeHtml(code)}）` : ""}</p>
      <p class="hint">失败不会产生或改动任何解读记录；区间 ${rangeText} 的基础事实不受影响。</p>
      <div class="perm-link-row">
        <button type="button" class="btn" data-test="analysis-retry-btn">${icon("activity", 14)} 重新生成预览</button>
      </div>
    </div>`;
  body.querySelector("[data-test='analysis-retry-btn']").addEventListener("click", startAnalysisPreview);
}

/** 完成/过期共用的正文渲染（方案 §7：重点/推断/限制/查看方向 + 元信息）。
 * expired=true 时先给原因横幅，仍渲染已保存原文与证据。 */
function renderAnalysisResult({ state, record }) {
  const body = analysisBodyEl();
  const result = record.result || {};
  const runtime = record.runtime || {};
  const facts = record.facts || {};
  const aId = record.a?.snapshot_id, bId = record.b?.snapshot_id;
  const aDate = String(record.a?.created_at || "").slice(0, 16).replace("T", " ");
  const bDate = String(record.b?.created_at || "").slice(0, 16).replace("T", " ");
  const expiredReason = {
    snapshot_replaced: "原区间快照已被同日新快照替换",
    snapshot_pruned: "原区间快照已按保留策略淘汰",
    dataset_unverifiable: "数据集口径（根/阈值/排除）已变化，无法再验证原区间",
  }[record.expired_reason] || record.expired_reason || "原因未上报";
  const findings = Array.isArray(result.findings) ? result.findings : [];
  const limitations = Array.isArray(result.limitations) ? result.limitations : [];
  const inspectNext = Array.isArray(result.inspect_next) ? result.inspect_next : [];
  const evidenceBtn = (eid, label) => `
    <button type="button" class="btn-mini" data-test="analysis-evidence-btn" data-evidence-id="${escapeHtml(eid)}"
            aria-label="查看证据 ${escapeHtml(eid)}" title="查看该编号保存的事实">${escapeHtml(label || eid)}</button>`;
  body.innerHTML = `
    <div data-test="analysis-state-${state === "expired" ? "expired" : "done"}">
      ${state === "expired" ? `
      <p class="hint analysis-expired-note" data-test="analysis-expired-note">
        <strong>该解读已过期</strong>：${escapeHtml(expiredReason)}。
        以下仍可查看原日期（${escapeHtml(aDate)} → ${escapeHtml(bDate)}）保存的原文与证据，但不代表当前数据。</p>` : ""}
      <p class="hint" data-test="analysis-meta">
        ${escapeHtml(runtime.id || "引擎")}${runtime.version ? ` v${escapeHtml(runtime.version)}` : ""} ·
        模型：${escapeHtml(runtime.model || "未上报")} ·
        生成于 ${escapeHtml(String(record.created_at || "").slice(0, 16).replace("T", " "))} ·
        区间 #${escapeHtml(String(aId))} → #${escapeHtml(String(bId))} ·
        解读版本 ${escapeHtml(record.prompt_version || "?")}
      </p>
      <p class="analysis-summary" data-test="analysis-summary">${escapeHtml(result.summary || "（引擎未给出摘要）")}</p>
      ${findings.length ? `
      <h3 class="analysis-subhead">重点与推断</h3>
      <ul class="analysis-findings" data-test="analysis-findings">
        ${findings.map((f) => `
        <li class="analysis-finding">
          <span class="quality-chip ${f.certainty === "hypothesis" ? "warn" : "ok"}" data-test="analysis-certainty">${f.certainty === "hypothesis" ? "推断" : "已观察"}</span>
          <span class="analysis-finding-text">${escapeHtml(f.text || "")}</span>
          <span class="analysis-finding-evidence">${(f.evidence_ids || []).map((eid) => evidenceBtn(eid)).join(" ")}</span>
        </li>`).join("")}
      </ul>` : `<p class="hint" data-test="analysis-no-findings">本次解读没有给出有依据的重点（引擎应说明原因，见下方限制）。</p>`}
      ${limitations.length ? `
      <h3 class="analysis-subhead">限制</h3>
      <ul class="analysis-limitations" data-test="analysis-limitations">
        ${limitations.map((t) => `<li>${escapeHtml(String(t))}</li>`).join("")}
      </ul>` : ""}
      ${inspectNext.length ? `
      <h3 class="analysis-subhead">查看方向</h3>
      <ul class="analysis-inspect" data-test="analysis-inspect-next">
        ${inspectNext.map((it) => `
        <li>${evidenceBtn(it.evidence_id)} <span>${escapeHtml(it.reason || "")}</span></li>`).join("")}
      </ul>` : ""}
      <div class="perm-link-row" data-test="analysis-result-actions">
        <button type="button" class="btn" data-test="analysis-rerun-btn">${icon("activity", 14)} 重跑</button>
        ${state === "done" ? `<button type="button" class="btn" data-test="analysis-revoke-btn">${icon("trash", 14)} 撤销此解读</button>` : ""}
      </div>
      <div data-test="analysis-evidence-card-host"></div>
      ${state === "expired" ? `<div data-test="analysis-revoke-expired-host"></div>` : ""}
    </div>`;
  body.querySelectorAll("[data-test='analysis-evidence-btn']").forEach((btn) => {
    btn.addEventListener("click", () => showAnalysisEvidence(btn.dataset.evidenceId, record));
  });
  body.querySelector("[data-test='analysis-rerun-btn']").addEventListener("click", startAnalysisPreview);
  const revokeBtn = body.querySelector("[data-test='analysis-revoke-btn']");
  if (revokeBtn) revokeBtn.addEventListener("click", () => confirmAnalysisRevoke(record));
  if (state === "expired") {
    const host = body.querySelector("[data-test='analysis-revoke-expired-host']");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn";
    btn.setAttribute("data-test", "analysis-revoke-expired-btn");
    btn.innerHTML = `${icon("trash", 14)} 撤销此历史解读`;
    btn.addEventListener("click", () => confirmAnalysisRevoke(record));
    host?.appendChild(btn);
  }
}

/** 证据条目卡：显示该编号在已保存事实包中的完整事实；能对位时提供
 * 「在变化表中定位」。脱敏路径（@user 段）无法对位时如实说明。 */
function showAnalysisEvidence(evidenceId, record) {
  const host = analysisBodyEl().querySelector("[data-test='analysis-evidence-card-host']");
  if (!host) return;
  const entry = ((record.facts && record.facts.entries) || [])
    .find((x) => x.evidence_id === evidenceId);
  if (!entry) {
    host.innerHTML = `<p class="hint cfg-error" data-test="analysis-evidence-missing">证据 ${escapeHtml(evidenceId)} 不在已保存的事实包中（可能是引擎引用了无效编号，已被验证器拒绝的历史不会出现；如反复出现请重跑）。</p>`;
    return;
  }
  const kindText = ANALYSIS_ENTRY_KIND[entry.kind] || entry.kind;
  const deltaText = entry.delta_kb == null ? "—" : fmtDelta(entry.delta_kb);
  const oldText = entry.old_kb == null ? "—" : fmtKB(entry.old_kb);
  const newText = entry.new_kb == null ? "—" : fmtKB(entry.new_kb);
  const tablePath = analysisEvidenceTablePath(entry);
  host.innerHTML = `
    <div class="analysis-evidence-card" data-test="analysis-evidence-card">
      <div class="analysis-evidence-head">
        <strong>证据 ${escapeHtml(entry.evidence_id)}</strong>
        <span class="hint">${escapeHtml(kindText)}</span>
        <button type="button" class="btn-mini" data-test="analysis-evidence-close" aria-label="关闭证据卡">${icon("x", 12)}</button>
      </div>
      <p class="analysis-evidence-path" data-test="analysis-evidence-path">${escapeHtml(entry.path)}</p>
      <p class="hint">${entry.old_kb == null ? "" : `${escapeHtml(oldText)} → `}
        ${entry.new_kb == null ? "" : escapeHtml(newText)}
        ${entry.delta_kb == null ? "" : ` · 变化 ${escapeHtml(deltaText)}`}</p>
      ${tablePath
        ? `<div class="perm-link-row"><button type="button" class="btn" data-test="analysis-evidence-locate">在变化表中定位</button></div>`
        : `<p class="hint">该路径含脱敏标记或不在当前变化表中（可能未进取样集），不以表行反推事实。</p>`}
    </div>`;
  host.querySelector("[data-test='analysis-evidence-close']").addEventListener("click", () => { host.innerHTML = ""; });
  const locateBtn = host.querySelector("[data-test='analysis-evidence-locate']");
  if (locateBtn) locateBtn.addEventListener("click", () => locateEvidenceInTable(entry));
}

/** 把 facts 的脱敏路径换算成变化表的绝对路径；无法对位（@user 段、
 * 无根路径）返回 null——不猜。 */
function analysisEvidenceTablePath(entry) {
  const root = lastDiff?.a?.root;
  const p = String(entry.path || "");
  if (!root) return null;
  if (p === "@root") return root;
  if (p.startsWith("@root/")) {
    const rel = p.slice("@root/".length);
    if (rel.includes("@user") || rel.includes("@root")) return null;
    return root.replace(/\/+$/, "") + "/" + rel;
  }
  return null;
}

function locateEvidenceInTable(entry) {
  const target = analysisEvidenceTablePath(entry);
  if (!target) return;
  // 清搜索过滤，避免目标行被滤掉；切回比对明细 tab（跨分区数据一致）
  const search = document.getElementById("changes-search");
  if (search && search.value) { search.value = ""; }
  changesTabs?.activate("detail");
  renderChangesTable();
  const row = document.querySelector(`#changes-body tr[data-path="${CSS.escape(target)}"]`);
  if (!row) return;
  row.scrollIntoView({ block: "center" });
  row.classList.add("analysis-locate-flash");
  setTimeout(() => row.classList.remove("analysis-locate-flash"), 2400);
}

/** 撤销确认层：删除本应用保存的正文与事实包；不承诺清除引擎/服务留存。 */
function confirmAnalysisRevoke(record) {
  const host = analysisBodyEl().querySelector("[data-test='analysis-evidence-card-host']");
  if (!host) return;
  host.innerHTML = `
    <div class="analysis-evidence-card" data-test="analysis-revoke-confirm">
      <p class="hint exclude-warning">即将撤销此解读：删除本应用保存的解读正文与对应事实包。
        已发送给引擎的数据无法撤回；生命周期审计记录会保留撤销时间。</p>
      <div class="exclude-actions">
        <button type="button" class="btn primary" data-test="analysis-revoke-yes">确认撤销</button>
        <button type="button" class="btn" data-test="analysis-revoke-no">取消</button>
      </div>
    </div>`;
  host.querySelector("[data-test='analysis-revoke-no']").addEventListener("click", () => { host.innerHTML = ""; });
  host.querySelector("[data-test='analysis-revoke-yes']").addEventListener("click", async () => {
    const request = beginRequest("analysis");
    try {
      await apiSend("DELETE", `/api/analyses/${encodeURIComponent(record.id)}`);
      if (!request.current()) return;
      renderAnalysis({ state: "idle" });
    } catch (e) {
      if (!request.current()) return;
      host.innerHTML = `<p class="hint cfg-error" data-test="analysis-revoke-error">撤销失败：${escapeHtml(e.message)} 解读仍保留，可重试。</p>`;
    }
  });
}

/* ----- 预览生成与主状态机 ----- */

async function startAnalysisPreview() {
  const sel = analysisSel();
  if (!sel.a || !sel.b) return;
  const request = beginRequest("analysis");
  const body = analysisBodyEl();
  body.innerHTML = `<p class="hint" data-test="analysis-preview-loading"><span class="dr-loading" data-dr-spin aria-hidden="true"></span>正在生成发送预览（只在本机整理事实包）…</p>`;
  try {
    const r = await apiPost("/api/analysis/previews", { a: Number(sel.a), b: Number(sel.b) });
    if (!request.current()) return;
    const preview = await r.json();
    analysisPreview = preview;
    renderAnalysis({ state: "preview", preview });
  } catch (e) {
    if (!request.current()) return;
    const code = e.reason_code;
    if (code === "analysis_disabled") { renderAnalysis({ state: "disabled" }); return; }
    if (code && code.startsWith("runtime_")) {
      body.innerHTML = `
        <p class="hint cfg-error" data-test="analysis-runtime-blocked">无法生成预览：${escapeHtml(e.message)}</p>
        <div class="perm-link-row">
          <button type="button" class="btn" data-test="analysis-goto-settings-2">${icon("settings", 14)} 前往设置重新检测</button>
        </div>`;
      body.querySelector("[data-test='analysis-goto-settings-2']").addEventListener("click", () => {
        window.__fathomSettingsSectionPending = "analysis";
        location.hash = "#/settings";
      });
      return;
    }
    if (code === "snapshot_not_found") {
      body.innerHTML = `<p class="hint cfg-error" data-test="analysis-snapshot-missing">所选快照已不可用（可能已被替换或淘汰）；请改选其他日期。基础事实表不受影响。</p>`;
      return;
    }
    body.innerHTML = `<p class="hint cfg-error" data-test="analysis-preview-error">预览生成失败：${escapeHtml(e.message)}</p>`;
  }
}

/** 主状态机：跟随当前 a/b 判定七主态。所有写入前经请求域守卫；
 * 改选区间会开启新域，旧响应全部作废（世代守卫，反例 2）。 */
async function loadAnalysisPanel() {
  const sel = analysisSel();
  if (!sel.a || !sel.b) {
    resetAnalysisSession({ hide: true });
    return;
  }
  const request = beginRequest("analysis");
  clearTimeout(analysisJobTimer);
  analysisJobTimer = null;
  analysisPreview = null;
  analysisJob = null;
  const panel = analysisPanelEl();
  const body = analysisBodyEl();
  panel.hidden = false;
  body.innerHTML = `<p class="hint"><span class="dr-loading" data-dr-spin aria-hidden="true"></span>解读状态加载中…</p>`;

  let cfg;
  try {
    const c = await fetchJSON("/api/config");
    if (!request.current()) return;
    cfg = c.analysis;
  } catch (e) {
    if (!request.current()) return;
    showAnalysisRetry(e.status === 0
      ? "无法连接本地服务，AI 解读状态暂不可用；基础事实不受影响。"
      : `AI 解读状态加载失败：${e.message}`);
    return;
  }
  analysisCfg = cfg;
  if (!cfg || !cfg.enabled) {
    renderAnalysis({ state: "disabled" });
    return;
  }

  let records = [];
  try {
    const r = await fetchJSON(`/api/analyses?a=${encodeURIComponent(sel.a)}&b=${encodeURIComponent(sel.b)}`);
    if (!request.current()) return;
    records = Array.isArray(r.analyses) ? r.analyses : [];
  } catch (e) {
    if (!request.current()) return;
    showAnalysisRetry(e.status === 0
      ? "无法连接本地服务，AI 解读状态暂不可用；基础事实不受影响。"
      : `AI 解读记录加载失败：${e.message}`);
    return;
  }
  const latest = records.find((x) => !x.revoked) || null;
  if (latest) {
    renderAnalysis({ state: latest.expired ? "expired" : "done", record: latest });
    return;
  }

  // 无有效历史：重入查原 job（sessionStorage 记录的 job_id；GET 纯读，
  // 不隐式重发）。跨会话（本浏览器会话无记录）无法发现他方在途 job——
  // 该发现缺口已回写 035B 接缝缺陷；届时用户显式确认会得到 409 提示。
  const savedJobId = analysisSavedJobId(sel);
  if (savedJobId) {
    try {
      const r = await fetchJSON(`/api/analysis/jobs/${encodeURIComponent(savedJobId)}`);
      if (!request.current()) return;
      const job = r.job;
      if (job && sameAnalysisRange(job, sel)) {
        if (!job.terminal) {
          analysisJob = job;
          renderAnalysis({ state: "running", job, cancelling: job.status === "cancelling" });
          scheduleAnalysisPoll(job.job_id);
          return;
        }
        if (job.status === "succeeded") {
          // succeeded 但 analyses 无未撤销记录：刚撤销或提交与查询竞态——
          // 再查一次记录层（权威），仍无则回落未分析态。
          try {
            const r2 = await fetchJSON(`/api/analyses?a=${encodeURIComponent(sel.a)}&b=${encodeURIComponent(sel.b)}`);
            if (!request.current()) return;
            const latest2 = (Array.isArray(r2.analyses) ? r2.analyses : [])
              .find((x) => !x.revoked) || null;
            if (latest2) {
              renderAnalysis({ state: latest2.expired ? "expired" : "done", record: latest2 });
              return;
            }
          } catch (_) { /* 记录层查询失败：按未分析呈现，可手动刷新 */ }
        } else {
          renderAnalysis({ state: "failure", job });
          // 重入读到终态：同样标记尝试已结束，下一次用户尝试才拿新键。
          markAnalysisAttemptTerminal(job, sel);
          return;
        }
      }
    } catch (_) { /* job 不存在（服务重启清理/已淘汰）：回未分析态 */ }
  }
  renderAnalysis({ state: "idle" });
}

/** 会话保存的原 job_id（confirmAnalysisSend 成功后写入）；无记录返回 null。 */
function analysisSavedJobId(sel) {
  try {
    const raw = sessionStorage.getItem(`fathom-aidem:${sel.a}->${sel.b}`);
    if (!raw) return null;
    const saved = JSON.parse(raw);
    return saved && typeof saved.job_id === "string" ? saved.job_id : null;
  } catch (_) { return null; }
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
    // ISS-094：进入页面按 hash 恢复 tab（无段 = 默认比对明细）。
    changesTabs?.applyHash();
    loadSnapshotsForDiff();
    loadReportList();
  },
  init() {
    // ISS-094：页内二级导航（五分区 tab；index.html 静态 DOM）。
    changesTabs = initPageTabs({ page: "changes", defaultTab: "detail" });
    // ISS-093：选择即比对——select 改选（鼠标或键盘）在选齐后自动触发加载。
    ["sel-a", "sel-b"].forEach((id) => {
      document.getElementById(id).addEventListener("change", onSelectionChange);
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
     "detailTrend", "detailBrowse", "analysis", "analysisPoll"].forEach(invalidateRequest);
    // ISS-035C：离页停轮询并清内存预览态；不取消已授权任务（后台继续），
    // 也不清 sessionStorage（回来按原 job 恢复显示，不隐式重发）。
    clearTimeout(analysisJobTimer);
    analysisJobTimer = null;
    analysisPreview = null;
    analysisJob = null;
    closeDetail();
  },
};
