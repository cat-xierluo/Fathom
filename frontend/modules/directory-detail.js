/* 目录详情共用组件（ISS-148）：从变化页树形表抽出的「目录证据」侧栏。
 *
 * 与 ISS-148 前的 changes.js 内联详情（ISS-028 起）同一 DOM 合同——
 * #changes-detail 容器、h2「目录详情」、.detail-path、.detail-close、
 * #detail-trend-chart 画布——既有回归（refresh/ISS-143 悬停）按这些
 * 选择器断言，组件化不改合同。
 *
 * 区间与最新两条口径（ISS-148 交互合同「详情绑定当前 a/b，最新记录
 * 另标时间」）：
 * - 区间证据：之前/现在/净变化/状态来自调用方传入的树行（绑定当前
 *   a/b 快照对），并以「区间 #a → #b」明示口径；
 * - 最新口径：/api/trend 与 /api/browse 恒绑定最新快照（后端合同，
 *   ISS-024/AUD-09），与区间无关——分区标题标注「最新快照口径」，
 *   并把最新记录时间单列一行，不与区间值混排。
 *
 * 世代守卫：每次 open 以 `a|b|path` 为键，trend/browse 响应写回前
 * 复核键与请求域；区间变更（调用方 invalidate）后迟到的旧响应不得
 * 覆盖新详情（_changes 内 beginRequest 世代号 + 本组件键守卫双保险）。
 *
 * extraActions：调用方注入的动作按钮（如「聚焦此目录」），组件不
 * 感知树表语义，保持共用性；按钮渲染于 detail-actions 区。
 */
import { fetchJSON, beginRequest, revealInFinder } from "./request.js";
import { fmtKB, fmtDelta, escapeHtml } from "./format.js";
import { icon } from "../icons.js";

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export function createDirectoryDetail({
  mountId = "changes-detail",
  getRange,
  renderStatus,
  extraActions = [],
} = {}) {
  const hostEl = () => document.getElementById(mountId);
  let activeKey = null;   // `${a}|${b}|${path}`：当前详情绑定的区间与路径
  let activePath = null;
  let sourceRow = null;   // 触发行（Esc 关闭后焦点返回）

  const keyOf = (range, path) => `${range ? range.a : ""}|${range ? range.b : ""}|${path}`;

  function rangeOf() {
    const range = getRange ? getRange() : { a: "", b: "" };
    return { a: String(range.a ?? ""), b: String(range.b ?? "") };
  }

  /* ---------- 骨架（DOM 合同选择器保持不变） ---------- */

  function skeleton(range, path, row) {
    const prev = row && row.old_kb != null ? fmtKB(row.old_kb) : "—";
    const curr = row && row.new_kb != null ? fmtKB(row.new_kb) : "—";
    const delta = row && row.delta_kb != null ? fmtDelta(row.delta_kb) : "—";
    const status = row ? (renderStatus ? renderStatus(row.status) : "—") : "—";
    const actions = extraActions.map((a) => `
      <button type="button" class="btn" data-detail-action="${escapeHtml(a.id)}"
              data-test="tree-detail-${escapeHtml(a.id)}">${a.icon ? icon(a.icon, 14) : ""} ${escapeHtml(a.label)}</button>`).join("");
    return `
      <div class="detail-head">
        <h2>${icon("brandRing", 15, "detail-dr")}目录详情</h2>
        <button class="detail-close" type="button" aria-label="关闭详情" title="关闭（Esc）">${icon("x", 14)}</button>
      </div>
      <div class="detail-path">${escapeHtml(path)}</div>
      <div class="detail-stats" data-test="tree-detail-range-stats">
        <div><div class="stat-label">区间</div><div class="stat-value">#${escapeHtml(range.a)} → #${escapeHtml(range.b)}</div></div>
        <div><div class="stat-label">之前</div><div class="stat-value">${escapeHtml(prev)}</div></div>
        <div><div class="stat-label">现在</div><div class="stat-value">${escapeHtml(curr)}</div></div>
        <div><div class="stat-label">净变化</div><div class="stat-value">${escapeHtml(delta)}</div></div>
        <div><div class="stat-label">状态</div><div class="stat-value">${status}</div></div>
      </div>
      <div class="detail-section">
        <h3>历史趋势（最新快照口径）</h3>
        <div id="detail-trend-chart" class="detail-trend">${icon("fileText", 14)} <span class="hint">加载中…</span></div>
        <p class="hint" id="detail-trend-latest" data-test="tree-detail-trend-latest" hidden></p>
      </div>
      <div class="detail-section">
        <h3>当前所在（最新快照）</h3>
        <div id="detail-current-info" class="hint">加载中…</div>
      </div>
      <div class="detail-actions">
        ${actions}
        <button class="copy-path" type="button" data-copy="${escapeHtml(path)}" title="复制路径">复制路径</button>
        <button class="btn-mini" data-reveal="${escapeHtml(path)}" aria-label="在 Finder 中显示" title="在 Finder 中显示">${icon("folderOpen", 14)}</button>
      </div>
    `;
  }

  /* ---------- 对外动作 ---------- */

  function open({ path, row = null, sourceRow: src = null }) {
    const host = hostEl();
    if (!host) return;
    const range = rangeOf();
    activePath = path;
    activeKey = keyOf(range, path);
    sourceRow = src || null;
    // 重开前丢弃旧画布实例（innerHTML 替换会分离旧节点，echarts 实例
    // 需显式 dispose，避免同 id 反复 init 堆积；首开无宿主节点则跳过）
    const prevChartHost = host.querySelector("#detail-trend-chart");
    const prevChart = prevChartHost && window.echarts
      ? window.echarts.getInstanceByDom(prevChartHost) : null;
    if (prevChart) prevChart.dispose();
    host.innerHTML = skeleton(range, path, row);
    host.hidden = false;
    host.querySelector(".detail-close").addEventListener("click", () => close());
    host.querySelectorAll("[data-copy]").forEach((b) =>
      b.addEventListener("click", async () => {
        try {
          if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(b.dataset.copy);
          else {
            const ta = document.createElement("textarea");
            ta.value = b.dataset.copy;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand("copy");
            document.body.removeChild(ta);
          }
          b.textContent = "已复制";
          b.classList.add("copied");
          setTimeout(() => { b.textContent = "复制路径"; b.classList.remove("copied"); }, 1200);
        } catch (_) { /* 复制失败：保留原文字，不冒充成功 */ }
      }));
    host.querySelectorAll("[data-reveal]").forEach((b) =>
      b.addEventListener("click", () => revealInFinder(b.dataset.reveal)));
    host.querySelectorAll("[data-detail-action]").forEach((b) => {
      const action = extraActions.find((a) => a.id === b.dataset.detailAction);
      if (action) b.addEventListener("click", () => action.onPick(activePath));
    });
    host.querySelector(".detail-close")?.focus();
    loadTrend(path);
    loadBrowse(path);
  }

  function close({ restoreFocus = true } = {}) {
    const host = hostEl();
    if (host) host.hidden = true;
    activeKey = null;
    activePath = null;
    if (restoreFocus && sourceRow && document.body.contains(sourceRow)) {
      sourceRow.focus();
    }
    sourceRow = null;
  }

  /** 区间变更/离页：关闭并作废在途 trend/browse（迟到响应不得写入）。 */
  function invalidate() {
    close({ restoreFocus: false });
  }

  /* ---------- 最新口径数据（独立请求域 + 键守卫） ---------- */

  async function loadTrend(path) {
    const target = hostEl()?.querySelector("#detail-trend-chart");
    if (!target) return;
    const request = beginRequest("detailTrend");
    try {
      const r = await fetchJSON(`/api/trend?path=${encodeURIComponent(path)}`);
      if (!request.current() || keyOf(rangeOf(), path) !== activeKey) return;
      const pts = r.points || [];
      const latestEl = hostEl()?.querySelector("#detail-trend-latest");
      if (!pts.length) {
        target.innerHTML = `<span class="hint">该路径此前未记录（不冒充增长）。</span>`;
        return;
      }
      const xs = pts.map((p) => String(p.created_at || "").slice(5, 10));
      const ys = pts.map((p) => p.size_kb || 0);
      const chart = echarts.init(target);
      chart.setOption({
        tooltip: { trigger: "axis", formatter: (ps) => ps.map((p) =>
          `${escapeHtml(pts[p.dataIndex].created_at || "")}<br/>${fmtKB(ys[p.dataIndex])}`).join("<br/>") },
        grid: { left: 50, right: 8, top: 8, bottom: 22 },
        xAxis: { type: "category", data: xs, axisLabel: { fontSize: 10 } },
        yAxis: { type: "value", axisLabel: { formatter: (v) => fmtKB(v), fontSize: 10 }, scale: true },
        series: [{ type: "line", smooth: true, symbol: "circle", symbolSize: 5,
          data: ys, itemStyle: { color: cssVar("--trench") }, lineStyle: { width: 2 } }],
      }, true);
      if (latestEl) {
        latestEl.textContent =
          `最新记录：${String(pts[pts.length - 1].created_at || "").slice(0, 16).replace("T", " ")}（${pts.length} 个历史点，非当前区间口径）`;
        latestEl.hidden = false;
      }
    } catch (e) {
      if (!request.current() || keyOf(rangeOf(), path) !== activeKey) return;
      const host = hostEl();
      if (!host) return;
      const el = host.querySelector("#detail-trend-chart");
      if (el) el.innerHTML = `<span class="hint">趋势加载失败${e.status ? `（HTTP ${e.status}）` : ""}</span>`;
    }
  }

  async function loadBrowse(path) {
    const target = hostEl()?.querySelector("#detail-current-info");
    if (!target) return;
    const request = beginRequest("detailBrowse");
    try {
      const r = await fetchJSON(`/api/browse?path=${encodeURIComponent(path)}`);
      if (!request.current() || keyOf(rangeOf(), path) !== activeKey) return;
      const childN = (r.children || []).length;
      const trendN = (r.trend || []).length;
      const at = r.snapshot_at ? String(r.snapshot_at).slice(0, 16).replace("T", " ") : "";
      target.innerHTML = `<span>当前大小：<strong>${escapeHtml(fmtKB(r.size_kb || 0))}</strong></span>` +
        (r.delta_kb != null
          ? `<span class="num">较上快照：${escapeHtml(fmtDelta(r.delta_kb))}</span>` : "") +
        `<span class="hint">直属子目录 ${childN} 个；该路径同数据集历史 ${trendN} 个点</span>` +
        (at ? `<span class="hint" data-test="tree-detail-snapshot-at">最新快照：${escapeHtml(at)}</span>` : "");
    } catch (e) {
      if (!request.current() || keyOf(rangeOf(), path) !== activeKey) return;
      const host = hostEl();
      if (!host) return;
      const el = host.querySelector("#detail-current-info");
      if (!el) return;
      if (e.status === 409) {
        el.innerHTML = `<span class="hint">尚无快照，无法显示当前所在。</span>`;
      } else if (e.status === 0) {
        el.innerHTML = `<span class="hint">无法连接本地服务。</span>`;
      } else if (e.status === 400) {
        // browse 恒绑定最新快照的数据集：所选 a/b 不是最新数据集时路径越界
        // 属预期——如实说明口径差异，不冒充「当前所在」可用。
        el.innerHTML = `<span class="hint">当前所在不可用：${escapeHtml(e.message)}；` +
          `本区绑定最新快照，不代表所选 #${escapeHtml(rangeOf().a)} → #${escapeHtml(rangeOf().b)} 区间。</span>`;
      } else {
        el.innerHTML = `<span class="hint">当前所在加载失败（HTTP ${e.status || "?"}）</span>`;
      }
    }
  }

  return {
    open,
    close,
    invalidate,
    get isOpen() { const host = hostEl(); return Boolean(host && !host.hidden); },
    get activePath() { return activePath; },
  };
}
