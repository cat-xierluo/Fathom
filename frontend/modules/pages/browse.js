/* 分布页：旭日图 + 目录浏览器（面包屑下钻、行级 Finder 打开、侧栏趋势）。
 *
 * 前端责任（ISS-027 模块合同）：
 * - beginRequest 世代号 + pageScoped：迟到的旧响应不能覆盖较新查询；
 * - 仅 frontend/icons.js 的 SVG 图标；零 emoji；
 *
 * 展示（ISS-028）：
 * - 行可聚焦（DESIGN 键盘可达）：Tab 进入、Enter 下钻、Esc 焦点返回；
 * - 长路径：可一键复制（DESIGN 关键可达性约束）；
 * - 趋势图与表格不重复：本页已有侧栏趋势，分布面板的图表与下钻表格
 *   共存于浏览器内（DESIGN：图表有表格替代/并列）。
 *
 * 分区（ISS-094）：页头下横向 tab 条（占用分布/目录浏览器，默认占用分布）。
 * 数据加载逻辑不变——load() 仍一次拉取 trees + browse 渲染两个分区，
 * tab 只切可见性；旭日图点击扇区联动切到「目录浏览器」并定位该路径。
 */
import { fetchJSON, beginRequest, invalidateRequest, revealInFinder } from "../request.js";
import { fmtBytes, fmtKB, fmtDelta, shortPath, escapeHtml } from "../format.js";
import { initChart, showChartMessage } from "../charts.js";
import { initPageTabs } from "../tabs.js";
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

    meta.textContent = `共 ${b.children.length} 个子目录（≥10MB 才入库） · 快照 ${escapeHtml(b.snapshot_at || "—")}`;
    document.getElementById("browser-trend-title").textContent =
      "目录趋势：" + shortPath(b.path, 2);

    tbody.innerHTML = "";
    if (!b.children.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="color:var(--muted)">此目录下没有 ≥10MB 的子目录</td></tr>';
    }
    const total = b.size_kb || 1;
    b.children.forEach((c) => {
      const tr = document.createElement("tr");
      tr.className = "focusable";
      tr.tabIndex = 0;
      tr.dataset.path = c.path;
      tr.setAttribute("role", "button");
      tr.setAttribute("aria-label", `下钻到 ${c.path}`);
      const deltaCls = c.delta_kb == null || c.delta_kb === 0
        ? "" : (c.delta_kb > 0 ? "delta-grow" : "delta-shrink");
      tr.innerHTML =
        `<td class="dir-name" data-path="${escapeHtml(c.path)}" title="${escapeHtml(c.path)}">${escapeHtml(c.name)}</td>` +
        `<td class="num">${fmtKB(c.size_kb)}</td>` +
        `<td class="num ${deltaCls}">${c.is_new ? icon("plus", 12) + " " : ""}${fmtDelta(c.delta_kb)}</td>` +
        `<td class="num">${((c.size_kb / total) * 100).toFixed(1)}%</td>` +
        `<td>
          <span class="row-actions">
            <button class="copy-path" type="button" data-copy="${escapeHtml(c.path)}"
                    aria-label="复制路径 ${escapeHtml(c.path)}" title="复制路径">复制</button>
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
        areaStyle: { opacity: 0.12 }, itemStyle: { color: cssVar("--trench") } }],
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
    // ISS-094：进入页面按 hash 恢复 tab（无段 = 默认占用分布）。
    browseTabs?.applyHash();
    loadTree();
    loadBrowse(state.browsePath);
  },
  init() {
    // ISS-094：页内二级导航（占用分布 / 目录浏览器；index.html 静态 DOM）。
    browseTabs = initPageTabs({ page: "browse", defaultTab: "sunburst" });
  },
  leave() {
    invalidateRequest("tree");
    invalidateRequest("browse");
  },
};
