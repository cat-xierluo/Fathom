/* 分布页：快照选择 + 旭日图 + 目录浏览器（面包屑下钻、行级 Finder 打开、
 * 侧栏趋势）+ 目录详情（所选快照时点）。
 *
 * 前端责任（ISS-027 模块合同）：
 * - beginRequest 世代号 + pageScoped：迟到的旧响应不能覆盖较新查询；
 * - 仅 frontend/icons.js 的 SVG 图标；零 emoji；
 *
 * 展示（ISS-028）：
 * - 行可聚焦（DESIGN 键盘可达）：Tab 进入、Enter 下钻、Esc 焦点返回；
 * - 长路径：可一键复制（DESIGN 关键可达性约束）；
 *
 * 分区（ISS-094）：页头下横向 tab 条（占用分布/目录浏览器，默认占用分布）；
 * 旭日图点击扇区联动切到「目录浏览器」并定位该路径。
 *
 * 快照上下文（ISS-107 起，ISS-159 收紧为显式选择）：
 * - 页头快照选择器驱动两个分区（trees / browse 均带 snapshot_id），默认
 *   最新快照；两个分区的容量主读数都只指所选快照，不暗中转最新；
 * - 与前一可比快照的差分仅为次级：列头与 meta 明示基线快照（「较 #id」），
 *   单快照/跨口径时如实「无可比前驱快照，差分未知」，不冒充基线；
 * - 结构节点（该快照无直接入库记录、仅有已记录后代）大小为「未直接记录」
 *   不填 0，仍可下钻；占比只对有测量行计算；
 * - 分页：未展示子目录通过「显示更多」显式追加（pagination 可见），
 *   不把未展示当作无子目录；
 * - 所选快照被淘汰/路径无记录（404）时明确说明原因，并提供一键恢复到
 *   最新快照；侧栏趋势是历史序列（每点带快照身份），与主读数口径分开。
 *
 * 目录详情（ISS-159）：行内「详情」打开共用组件的 distribution 单时点
 * 模式——所有读数绑定所选快照，趋势锚定该快照，直属子目录列表在详情内
 * 下钻；与变化页的双时点区间详情共用组件但口径同屏可辨。
 */
import { fetchJSON, beginRequest, invalidateRequest, revealInFinder } from "../request.js";
import { fmtBytes, fmtKB, fmtDelta, shortPath, escapeHtml } from "../format.js";
import { initChart, showChartMessage } from "../charts.js";
import { initPageTabs } from "../tabs.js";
import { createDirectoryDetail } from "../directory-detail.js";
import { state } from "../state.js";
import { icon } from "../../icons.js";

/* ISS-084：图表色与 style.css :root 语义 token 同源（单一色源，不硬编码）。
 * 脚本为 module（defer），执行时 CSSOM 已就绪。 */
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/* 旭日图色板（ISS-084）：DEC-023 等深阶地派生的蓝青明度阶梯——
 * 午夜蓝 #0f2a3d（最深）→ 海沟蓝 --trench → 矿物青 --mineral → 亮矿物青
 * #82c8c2（刻度亮档）→ 浅海沟蓝灰；同层兄弟目录在体系内轮换取色，
 * 替代 ECharts 默认高饱和色板。属图表专用渐变（非文本），不设 CSS token。 */
const SUNBURST_PALETTE = [
  "#0f2a3d", "#345d7f", "#3e7e7c",
  "#4d7391", "#5c9490", "#6e8ca6",
  "#82c8c2", "#9db4c6",
];

let browseTabs = null;  // ISS-094 页内二级导航（占用分布 / 目录浏览器）

/* ISS-159：目录详情（distribution 单时点模式）。 */
let browseDetail = null;

/* 最近一次 browse 响应的快照元数据（详情骨架的数据源；null = 未加载）。 */
let lastBrowseMeta = null;

/* 快照列表缓存（质量提示随选择即时切换，不重复请求）。 */
let snapshotCache = [];

const snapshotId = () => state.browseSnapshotId;

const snapshotParam = () => {
  const sid = snapshotId();
  return sid ? `&snapshot_id=${encodeURIComponent(sid)}` : "";
};

function renderSunburstContext(contextEl, t, snapList) {
  if (!t.snapshot_id) {
    contextEl.textContent = "尚无快照；完成首次扫描后显示占用分布。";
    return;
  }
  const snap = (snapList || []).find((s) => String(s.id) === String(t.snapshot_id));
  const when = snap && snap.created_at
    ? snap.created_at.slice(0, 16).replace("T", " ")
    : "未知";
  const rootText = t.root ? t.root : "未知";
  contextEl.textContent = `快照 #${t.snapshot_id} · ${when} · 根 ${rootText}`;
}

/* ---------- 快照选择器（ISS-159） ---------- */

function qualityText(snap) {
  if (!snap) return "";
  if (snap.collection_status === "partial") {
    const bits = [];
    if (snap.vanished_count) bits.push(`${snap.vanished_count} 个路径采集时消失`);
    if (snap.denied_count) bits.push(`${snap.denied_count} 个路径无权限`);
    return `部分采集${bits.length ? `：${bits.join("、")}` : ""}`;
  }
  return "";
}

async function loadSnapshotPicker() {
  const sel = document.getElementById("browse-snap");
  if (!sel) return;
  const request = beginRequest("snapshotPicker");
  let snaps;
  try {
    snaps = await fetchJSON("/api/snapshots");
  } catch (e) {
    if (!request.current()) return;
    sel.replaceChildren(new Option("快照列表不可用", ""));
    sel.disabled = true;
    return;
  }
  if (!request.current()) return;
  snapshotCache = snaps;
  sel.disabled = !snaps.length;
  sel.replaceChildren(...snaps.map((s) =>
    new Option(`#${s.id} ${String(s.created_at || "").slice(0, 16).replace("T", " ")}`,
               String(s.id))));
  // 恢复所选；所选已不在列表（被保留策略淘汰）时回落最新并如实提示。
  const ids = snaps.map((s) => String(s.id));
  const wanted = snapshotId() ? String(snapshotId()) : (ids[0] || "");
  const fellBack = Boolean(snapshotId()) && !ids.includes(wanted);
  sel.value = ids.includes(wanted) ? wanted : (ids[0] || "");
  state.browseSnapshotId = sel.value ? Number(sel.value) : null;
  const qualityEl = document.getElementById("browse-snap-quality");
  if (qualityEl) {
    const snap = snaps.find((s) => String(s.id) === sel.value);
    qualityEl.textContent = fellBack
      ? "所选快照已被保留策略淘汰，已恢复到最新快照。"
      : (qualityText(snap) || "");
  }
}

function onSnapshotChange() {
  const sel = document.getElementById("browse-snap");
  state.browseSnapshotId = sel && sel.value ? Number(sel.value) : null;
  const qualityEl = document.getElementById("browse-snap-quality");
  if (qualityEl) {
    const snap = snapshotCache.find((s) => String(s.id) === sel.value);
    qualityEl.textContent = qualityText(snap) || "";
  }
  if (browseDetail?.isOpen) browseDetail.invalidate();
  loadTree();
  loadBrowse(state.browsePath);
}

function resetToLatestSnapshot() {
  const sel = document.getElementById("browse-snap");
  if (!sel) return;
  const first = sel.querySelector("option")?.value || "";
  sel.value = first;
  onSnapshotChange();
}

/* ---------- 占用分布（旭日图） ---------- */

async function loadTree() {
  const request = beginRequest("tree");
  const contextEl = document.getElementById("sunburst-context");
  try {
    // 快照列表仅用于上下文时点：失败不阻塞树渲染（catch 成 null）。
    const [t, snapList] = await Promise.all([
      fetchJSON(`/api/trees?min_kb=51200${snapshotParam()}`),
      fetchJSON("/api/snapshots").catch(() => null),
    ]);
    if (!request.current()) return;
    if (contextEl) renderSunburstContext(contextEl, t, snapList);
    if (!t.snapshot_id) {
      showChartMessage("chart-sunburst", "尚无快照；完成首次扫描后显示占用分布。");
      return;
    }
    const chart = initChart("chart-sunburst");
    chart.setOption({
      color: SUNBURST_PALETTE,
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
      if (!p.data || !p.data.path) return;
      // ISS-094：扇区点击联动——目录浏览器已改为并列分区，定位前先
      // 切到该 tab（用户停在分布图 tab 时也能一跳即达）。
      browseTabs?.activate("browser", { persistHash: true });
      loadBrowse(p.data.path);
    });
  } catch (e) {
    if (!request.current()) return;
    // 主请求失败：上下文如实未知，不显示任何伪造时点。
    if (contextEl) contextEl.textContent = "快照上下文未知（占用分布加载失败）。";
    showChartMessage("chart-sunburst", e.status === 0
      ? "无法连接本地服务，占用分布暂不可用。"
      : `占用分布加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`);
  }
}

/* ---------- 目录浏览器 ---------- */

function metaText(b) {
  const shown = b.pagination
    ? `已显示 ${b.pagination.offset + b.pagination.returned}/${b.pagination.total} 个`
    : `共 ${b.children.length} 个子目录（≥10MB 才入库）`;
  let base = "";
  if (b.comparison) {
    base = ` · 较 #${b.comparison.snapshot_id}` +
      `（${String(b.comparison.created_at || "").slice(0, 16).replace("T", " ")}）`;
  } else {
    base = " · 无可比前驱快照，差分未知";
  }
  const partial = b.snapshot?.collection_status === "partial" ? " · 部分采集" : "";
  return `${shown}${base} · 快照 #${b.snapshot?.id ?? "?"}${partial}`;
}

function rowCells(c, b) {
  const measured = c.size_kb != null;
  const sizeText = measured ? fmtKB(c.size_kb) : "未直接记录";
  const sizeCls = measured ? "" : ' class="hint"';
  const total = b.size_kb;
  const pct = measured && total
    ? ((c.size_kb / total) * 100).toFixed(1) + "%"
    : "—";
  const deltaCls = c.delta_kb == null || c.delta_kb === 0
    ? "" : (c.delta_kb > 0 ? "delta-grow" : "delta-shrink");
  return {
    sizeCell: `<td${sizeCls}>${measured ? escapeHtml(sizeText) : sizeText}</td>`,
    deltaCell: `<td class="num ${deltaCls}">${c.is_new ? icon("plus", 12) + " " : ""}${fmtDelta(c.delta_kb)}</td>`,
    pctCell: `<td class="num">${pct}</td>`,
  };
}

function appendBrowseRow(tbody, c) {
  const tr = document.createElement("tr");
  tr.className = "focusable";
  tr.tabIndex = 0;
  tr.dataset.path = c.path;
  tr.setAttribute("role", "button");
  tr.setAttribute("aria-label", `下钻到 ${c.path}`);
  const cells = rowCells(c, lastBrowseMeta);
  tr.innerHTML =
    `<td class="dir-name" data-path="${escapeHtml(c.path)}" title="${escapeHtml(c.path)}">${escapeHtml(c.name)}</td>` +
    cells.sizeCell + cells.deltaCell + cells.pctCell +
    `<td>
      <span class="row-actions">
        <button class="copy-path" type="button" data-copy="${escapeHtml(c.path)}"
                aria-label="复制路径 ${escapeHtml(c.path)}" title="复制路径">复制</button>
        <button class="btn-mini" data-detail="${escapeHtml(c.path)}" data-size="${c.size_kb ?? ""}"
                data-status="${escapeHtml(c.status || "measured")}"
                aria-label="查看 ${escapeHtml(c.path)} 的目录详情" title="目录详情（所选快照时点）">${icon("info", 14)}</button>
        <button class="btn-mini" data-reveal="${escapeHtml(c.path)}" title="在 Finder 中显示" aria-label="在 Finder 中显示">${icon("folderOpen", 14)}</button>
      </span>
    </td>`;
  tbody.appendChild(tr);
  tr.addEventListener("click", (e) => {
    // 避免按钮点击冒泡导致下钻
    if (e.target.closest("button")) return;
    loadBrowse(c.path);
  });
  tr.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      loadBrowse(c.path);
    }
  });
  tr.querySelector("[data-reveal]").addEventListener("click", (e) => {
    e.stopPropagation();
    revealInFinder(c.path);
  });
  tr.querySelector("[data-detail]").addEventListener("click", (e) => {
    e.stopPropagation();
    openBrowseDetail(c, tr);
  });
  tr.querySelector("[data-copy]").addEventListener("click", async (e) => {
    e.stopPropagation();
    const text = c.path;
    const btn = e.currentTarget;
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
      btn.textContent = "已复制";
      btn.classList.add("copied");
      setTimeout(() => { btn.textContent = "复制"; btn.classList.remove("copied"); }, 1200);
    } catch (_) {
      btn.textContent = "复制失败";
      setTimeout(() => { btn.textContent = "复制"; }, 1200);
    }
  });
}

function renderMoreRow(tbody, b) {
  if (!b.pagination?.has_more) return;
  const tr = document.createElement("tr");
  tr.innerHTML =
    `<td colspan="5" style="text-align:center">` +
    `<button class="btn" type="button" data-test="browse-more" ` +
    `aria-label="显示更多子目录（已显示 ${b.pagination.offset + b.pagination.returned}，共 ${b.pagination.total} 个）">` +
    `显示更多（已显示 ${b.pagination.offset + b.pagination.returned}/${b.pagination.total}）</button></td>`;
  tbody.appendChild(tr);
  tr.querySelector("button").addEventListener("click", () => {
    loadBrowse(lastBrowseMeta?.path || state.browsePath, { cursor: b.pagination.next_cursor });
  });
}

function renderTrend(b) {
  const chart = initChart("chart-browser-trend");
  chart.setOption({
    tooltip: { trigger: "axis", formatter: (ps) => ps.map((p) => {
      const pt = (b.trend || [])[p.dataIndex];
      if (!pt) return "";
      const sid = pt.snapshot_id != null ? `（#${pt.snapshot_id}）` : "";
      return `${escapeHtml(pt.created_at || "")}${escapeHtml(sid)}<br/>${fmtKB(pt.size_kb)}`;
    }).join("<br/>") },
    grid: { left: 64, right: 12, top: 12, bottom: 24 },
    xAxis: { type: "category", data: b.trend.map((p) => p.created_at.slice(5, 10)) },
    yAxis: { type: "value", axisLabel: { formatter: (v) => fmtBytes(v * 1024) }, scale: true },
    series: [{ type: "line", smooth: true, symbolSize: 4, symbol: "circle",
      data: b.trend.map((p) => p.size_kb),
      areaStyle: { opacity: 0.12 }, itemStyle: { color: cssVar("--trench") } }],
  }, true);
}

function renderBrowseFailure(e) {
  const tbody = document.querySelector("#tbl-browse tbody");
  const meta = document.getElementById("browser-meta");
  meta.textContent = "";
  if (e.status === 404) {
    // 快照被淘汰或路径无记录：明确原因 + 一键恢复最新，不显示空表冒充。
    tbody.innerHTML = `<tr><td colspan="5" class="hint" data-test="browse-not-found">` +
      `${escapeHtml(e.message)}<br/>` +
      `<button class="btn" type="button" data-test="browse-recover-latest" style="margin-top:6px">恢复到最新快照</button>` +
      `</td></tr>`;
    tbody.querySelector("[data-test='browse-recover-latest']")
      .addEventListener("click", resetToLatestSnapshot);
  } else {
    tbody.innerHTML = `<tr><td colspan="5" class="hint">${escapeHtml(e.message)}</td></tr>`;
  }
}

async function loadBrowse(path, { cursor = null } = {}) {
  const request = beginRequest("browse");
  const tbody = document.querySelector("#tbl-browse tbody");
  const meta = document.getElementById("browser-meta");
  try {
    // path 留空 = 所选数据集根；snapshot_id 缺省参数由 API 端旧行为承接。
    const cursorParam = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
    const b = await fetchJSON(
      `/api/browse?path=${encodeURIComponent(path || "")}${snapshotParam()}${cursorParam}`);
    if (!request.current()) return;
    state.browsePath = b.path;
    lastBrowseMeta = b;

    // 面包屑
    document.getElementById("crumbs").innerHTML = b.crumbs.map((c, i) =>
      `${i ? '<span class="crumb-sep">/</span>' : ""}` +
      `<button class="crumb${i === b.crumbs.length - 1 ? " current" : ""}" data-path="${escapeHtml(c.path)}">` +
      `${escapeHtml(c.name)}</button>`).join("");
    document.querySelectorAll("#crumbs .crumb").forEach((el) =>
      el.addEventListener("click", () => loadBrowse(el.dataset.path)));

    // 次级差分的基线明示：列头随 comparison 变化，不暗示「最新」。
    const thDelta = document.getElementById("th-delta");
    if (thDelta) {
      thDelta.textContent = b.comparison
        ? `较 #${b.comparison.snapshot_id}`
        : "较前快照";
    }
    meta.textContent = metaText(b);
    document.getElementById("browser-trend-title").textContent =
      "目录趋势：" + shortPath(b.path, 2);

    // 追加页先移除旧的「显示更多」行；首页则整体清空重画。
    tbody.querySelector("[data-test='browse-more']")?.closest("tr")?.remove();
    if (!cursor) tbody.innerHTML = "";
    if (!b.children.length && !cursor) {
      tbody.innerHTML = '<tr><td colspan="5" style="color:var(--muted)">此目录下没有已记录的直属子目录</td></tr>';
    }
    b.children.forEach((c) => appendBrowseRow(tbody, c));
    renderMoreRow(tbody, b);
    renderTrend(b);
  } catch (e) {
    if (!request.current()) return;
    renderBrowseFailure(e);
  }
}

/* ---------- 目录详情（distribution 单时点，共用组件） ---------- */

function openBrowseDetail(c, sourceRow) {
  browseDetail?.open({
    path: c.path,
    row: { size_kb: c.size_kb, status: c.status || "measured", delta_kb: c.delta_kb },
    sourceRow,
  });
}

function distributionSnapshotMeta() {
  const b = lastBrowseMeta;
  if (!b?.snapshot) return null;
  return {
    id: b.snapshot.id,
    createdAt: b.snapshot.created_at,
    root: b.snapshot.root,
    collectionStatus: b.snapshot.collection_status,
    comparison: b.comparison,
    path: b.path,
    sizeKb: b.size_kb,
    status: b.status,
  };
}

/* ---------- 页面装配 ---------- */

export const browsePage = {
  id: "browse",
  load() {
    // ISS-094：进入页面按 hash 恢复 tab（无段 = 默认占用分布）。
    browseTabs?.applyHash();
    loadSnapshotPicker().then(() => {
      loadTree();
      loadBrowse(state.browsePath);
    });
  },
  init() {
    // ISS-094：页内二级导航（占用分布 / 目录浏览器；index.html 静态 DOM）。
    browseTabs = initPageTabs({ page: "browse", defaultTab: "sunburst" });
    document.getElementById("browse-snap")?.addEventListener("change", onSnapshotChange);
    // ISS-159：详情组件 distribution 模式——单时点读数 + 锚定趋势 +
    // 直属子目录列表（详情内下钻）；「在浏览器中定位」跳回主体表格。
    browseDetail = createDirectoryDetail({
      mountId: "browse-detail",
      mode: "distribution",
      getSnapshot: distributionSnapshotMeta,
      renderStatus: (status) =>
        status === "structural" ? "结构节点（无直接记录）" : "实测",
      extraActions: [{
        id: "locate",
        label: "在浏览器中定位",
        icon: "scope",
        onPick: (p) => {
          browseTabs?.activate("browser", { persistHash: true });
          loadBrowse(p);
        },
      }],
    });
    // 全局 Esc：关闭详情（与 changes 页同一交互合同；详情未开时无操作）。
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (browseDetail?.isOpen) {
        e.preventDefault();
        browseDetail.close();
      }
    });
  },
  leave() {
    invalidateRequest("tree");
    invalidateRequest("browse");
    invalidateRequest("snapshotPicker");
  },
};
