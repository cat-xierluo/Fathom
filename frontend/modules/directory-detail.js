/* 目录详情共用组件（ISS-148）：从变化页树形表抽出的「目录证据」侧栏。
 *
 * 与 ISS-148 前的 changes.js 内联详情（ISS-028 起）同一 DOM 合同——
 * #changes-detail 容器、h2「目录详情」、.detail-path、.detail-close、
 * #detail-trend-chart 画布——既有回归（refresh/ISS-143 悬停）按这些
 * 选择器断言，组件化不改合同。
 *
 * 区间与趋势两条口径（ISS-148 交互合同「详情绑定当前 a/b，最新记录
 * 另标时间」；ISS-149 趋势改锚定所选区间）：
 * - 区间证据：之前/现在/净变化/状态来自调用方传入的树行（绑定当前
 *   a/b 快照对），并以「区间 #a → #b」明示口径；a/b 区间读数以
 *   /api/diff/children（ISS-147）为准，趋势曲线不替代它；
 * - 历史趋势（ISS-149）：以所选对比快照 b 为显式锚（anchor_snapshot_id）
 *   请求 /api/trend，只画 b 所属数据集（同根同阈值同排除掩码）的窗口，
 *   选择旧 a/b 时不再锚到最新数据集；窗口内每个快照一个点，缺条目为
 *   null（connectNulls=false 断线呈现，不补 0），tooltip/等价表格给出
 *   完整扫描时间与快照号，同日多次扫描可区分；响应旧形态（无锚定字段，
 *   如旧 API）回落到既有「最新快照口径」渲染，保持兼容。
 *
 * 世代守卫：每次 open 以 `a|b|path` 为键，trend/browse 响应写回前
 * 复核键与请求域；区间变更（调用方 invalidate）后迟到的旧响应不得
 * 覆盖新详情（_changes 内 beginRequest 世代号 + 本组件键守卫双保险）。
 *
 * extraActions：调用方注入的动作按钮（如「聚焦此目录」），组件不
 * 感知树表语义，保持共用性；按钮渲染于 detail-actions 区。
 *
 * 当前大文件区（ISS-151）：详情内「此刻 st_size 实测」与上方「历史区间
 * a→b」两种口径同屏可辨；查询域是 pages/bigfiles.js 的共享单例（与独立
 * 大文件页共用范围/模式/状态），查询仅由「查看当前大文件」明确点击发起，
 * 打开详情不触发任何遍历；scope 命中本目录时呈现共享状态（含取消/404
 * 无法定位），关闭详情即离开查询面（两面都不可见时按句柄真实取消）。
 *
 * distribution 单时点模式（ISS-159）：mode="distribution" 时详情绑定
 * getSnapshot() 提供的所选快照——大小/状态是该快照实点（measured /
 * structural），与前一可比快照的差分仅为次级且基线区间明示（单快照或
 * 跨口径时「差分未知」）；趋势锚定该快照；「直属子目录」区列出该快照
 * 下的历史直属子行（可下钻）；大文件区标注「非快照时点」。所有读数不
 * 暗中转最新——与 changes 双时点模式共用组件但口径同屏可辨。
 */
import { fetchJSON, beginRequest, revealInFinder } from "./request.js";
import { fmtKB, fmtDelta, escapeHtml } from "./format.js";
import { icon } from "../icons.js";
import {
  attachBigfiles, detachBigfiles, startBigfilesQuery, cancelActiveBigfiles,
  renderBigfilesTbody, bigfilesRequestedAtText, subscribeBigfiles, bigfilesEngine,
} from "./pages/bigfiles.js";

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export function createDirectoryDetail({
  mountId = "changes-detail",
  mode = "changes",       // "changes"（a→b 双时点）| "distribution"（单快照时点，ISS-159）
  getRange,
  getSnapshot,            // distribution：() => ({id, createdAt, root, comparison, ...}) | null
  renderStatus,
  extraActions = [],
} = {}) {
  const hostEl = () => document.getElementById(mountId);
  const isDist = mode === "distribution";
  let activeKey = null;   // changes: `${a}|${b}|${path}`；distribution: `${sid}|${path}`
  let activePath = null;
  let sourceRow = null;   // 触发行（Esc 关闭后焦点返回）
  let snapMeta = null;    // distribution：open 时快照元数据（键守卫与骨架共用）

  function keyOf(range, path) {
    if (isDist) return `${snapMeta?.id ?? ""}|${path}`;
    return `${range ? range.a : ""}|${range ? range.b : ""}|${path}`;
  }

  function rangeOf() {
    const range = getRange ? getRange() : { a: "", b: "" };
    return { a: String(range.a ?? ""), b: String(range.b ?? "") };
  }

  /* ---------- 骨架（DOM 合同选择器保持不变） ---------- */

  function actionsHtml(path) {
    return extraActions.map((a) => `
      <button type="button" class="btn" data-detail-action="${escapeHtml(a.id)}"
              data-test="tree-detail-${escapeHtml(a.id)}">${a.icon ? icon(a.icon, 14) : ""} ${escapeHtml(a.label)}</button>`).join("");
  }

  function bigfilesSectionHtml(rangeLabel) {
    return `
      <div class="detail-section" data-test="detail-bigfiles-section">
        <h3>当前大文件<span class="hint">（此刻 st_size 实测，${rangeLabel}）</span></h3>
        <div class="detail-bf-bar">
          <select id="detail-bf-mode" class="detail-bf-mode" data-test="detail-bf-mode" aria-label="大文件查询模式">
            <option value="largest">当前最大</option>
            <option value="recent">近期修改</option>
          </select>
          <button type="button" class="btn" data-test="detail-bf-query">查看当前大文件</button>
        </div>
        <p class="hint" id="detail-bf-context" data-test="detail-bf-context" hidden></p>
        <div id="detail-bigfiles-host">
          <table class="tbl" data-test="detail-bigfiles-table">
            <thead><tr><th class="num">大小</th><th>修改时间</th><th>文件</th><th></th></tr></thead>
            <tbody data-test="detail-bigfiles-tbody"></tbody>
          </table>
        </div>
      </div>`;
  }

  function skeleton(range, path, row) {
    const prev = row && row.old_kb != null ? fmtKB(row.old_kb) : "—";
    const curr = row && row.new_kb != null ? fmtKB(row.new_kb) : "—";
    const delta = row && row.delta_kb != null ? fmtDelta(row.delta_kb) : "—";
    const status = row ? (renderStatus ? renderStatus(row.status) : "—") : "—";
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
        <div><div class="stat-label">状态</div><div class="stat-value">${escapeHtml(status)}</div></div>
      </div>
      <div class="detail-section">
        <h3>${escapeHtml(range.b ? `历史趋势（#${escapeHtml(range.b)} 锚定同数据集）` : "历史趋势（最新快照口径）")}</h3>
        <div id="detail-trend-chart" class="detail-trend">${icon("fileText", 14)} <span class="hint">加载中…</span></div>
        <p class="hint" id="detail-trend-latest" data-test="tree-detail-trend-latest" hidden></p>
        <div id="detail-trend-table-host" hidden></div>
      </div>
      <div class="detail-section">
        <h3>当前所在（最新快照）</h3>
        <div id="detail-current-info" class="hint">加载中…</div>
      </div>
      ${bigfilesSectionHtml(`非 #${escapeHtml(range.a)} → #${escapeHtml(range.b)} 历史区间`)}
      <div class="detail-actions">
        ${actionsHtml(path)}
        <button class="copy-path" type="button" data-copy="${escapeHtml(path)}" title="复制路径">复制路径</button>
        <button class="btn-mini" data-reveal="${escapeHtml(path)}" aria-label="在 Finder 中显示" title="在 Finder 中显示">${icon("folderOpen", 14)}</button>
      </div>
    `;
  }

  /* distribution 单时点骨架（ISS-159）：主读数 = 所选快照实点；差分仅为
   * 次级且基线区间明示；直属子目录列表可下钻；趋势锚定该快照。 */
  function skeletonDist(snap, path, row) {
    const sid = snap?.id != null ? String(snap.id) : "?";
    const when = snap?.createdAt
      ? String(snap.createdAt).slice(0, 16).replace("T", " ") : "未知";
    const measured = row && row.size_kb != null;
    const sizeText = measured ? fmtKB(row.size_kb) : "未直接记录";
    const status = row ? (renderStatus ? renderStatus(row.status) : "—") : "—";
    const cmp = snap?.comparison || null;
    const deltaText = cmp
      ? (row && row.delta_kb != null ? `较 #${cmp.snapshot_id}：${fmtDelta(row.delta_kb)}` : `较 #${cmp.snapshot_id}：基线缺测`)
      : "无可比前驱快照，差分未知";
    const cmpNote = cmp
      ? `差分区间：#${escapeHtml(String(cmp.snapshot_id))}（${escapeHtml(String(cmp.created_at || "").slice(0, 16).replace("T", " "))}）→ #${escapeHtml(sid)}；主读数为快照 #${escapeHtml(sid)} 实点，差分仅作参考。`
      : "该快照没有可比的前驱（单快照或前驱跨口径）：差分未知，不冒充基线。";
    return `
      <div class="detail-head">
        <h2>${icon("brandRing", 15, "detail-dr")}目录详情</h2>
        <button class="detail-close" type="button" aria-label="关闭详情" title="关闭（Esc）">${icon("x", 14)}</button>
      </div>
      <div class="detail-path">${escapeHtml(path)}</div>
      <div class="detail-stats" data-test="browse-detail-stats">
        <div><div class="stat-label">快照</div><div class="stat-value" data-test="browse-detail-snapshot">#${escapeHtml(sid)}</div></div>
        <div><div class="stat-label">时点</div><div class="stat-value">${escapeHtml(when)}</div></div>
        <div><div class="stat-label">大小</div><div class="stat-value" data-test="browse-detail-size">${escapeHtml(sizeText)}</div></div>
        <div><div class="stat-label">状态</div><div class="stat-value" data-test="browse-detail-status">${escapeHtml(status)}</div></div>
        <div><div class="stat-label">次级差分</div><div class="stat-value" data-test="browse-detail-delta">${escapeHtml(deltaText)}</div></div>
      </div>
      <p class="hint" data-test="browse-detail-delta-range">${cmpNote}</p>
      <div class="detail-section">
        <h3>历史趋势（#${escapeHtml(sid)} 锚定同数据集）</h3>
        <div id="detail-trend-chart" class="detail-trend">${icon("fileText", 14)} <span class="hint">加载中…</span></div>
        <p class="hint" id="detail-trend-latest" data-test="tree-detail-trend-latest" hidden></p>
        <div id="detail-trend-table-host" hidden></div>
      </div>
      <div class="detail-section">
        <h3>直属子目录（快照 #${escapeHtml(sid)}）</h3>
        <div id="detail-children-host" data-test="browse-detail-children"><span class="hint">加载中…</span></div>
      </div>
      ${bigfilesSectionHtml(`非快照 #${escapeHtml(sid)} 时点`)}
      <div class="detail-actions">
        ${actionsHtml(path)}
        <button class="copy-path" type="button" data-copy="${escapeHtml(path)}" title="复制路径">复制路径</button>
        <button class="btn-mini" data-reveal="${escapeHtml(path)}" aria-label="在 Finder 中显示" title="在 Finder 中显示">${icon("folderOpen", 14)}</button>
      </div>
    `;
  }

  /* ---------- 对外动作 ---------- */

  function open({ path, row = null, sourceRow: src = null }) {
    const host = hostEl();
    if (!host) return;
    // distribution：先取快照元数据再算键（键守卫绑定快照身份）。
    snapMeta = isDist && getSnapshot ? getSnapshot() : null;
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
    host.innerHTML = isDist ? skeletonDist(snapMeta, path, row)
                            : skeleton(range, path, row);
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
    // ISS-151「当前大文件」区：挂载共享查询域 + 本区事件委托（查询按钮 /
    // 模式切换 / 取消 / Finder 定位）。查询仅由明确点击发起，打开详情
    // 不触发任何遍历；关闭详情即离开查询面（页也不在时按句柄取消）。
    attachBigfiles("detail");
    const bfQueryBtn = host.querySelector("[data-test='detail-bf-query']");
    if (bfQueryBtn) {
      bfQueryBtn.addEventListener("click", () => {
        const modeSel = host.querySelector("#detail-bf-mode");
        startBigfilesQuery({ path: activePath, mode: modeSel ? modeSel.value : "largest" });
      });
    }
    const bfTable = host.querySelector("[data-test='detail-bigfiles-table']");
    if (bfTable) {
      bfTable.addEventListener("click", (e) => {
        const cancel = e.target.closest("[data-test='bigfiles-cancel']");
        if (cancel) { cancelActiveBigfiles(); return; }
        const reveal = e.target.closest("[data-reveal]");
        if (reveal) revealInFinder(reveal.dataset.reveal);
      });
    }
    renderDetailBigfiles();
    loadTrend(path);
    if (isDist) loadChildren(path);
    else loadBrowse(path);
  }

  function close({ restoreFocus = true } = {}) {
    const host = hostEl();
    if (host) host.hidden = true;
    activeKey = null;
    activePath = null;
    detachBigfiles("detail");
    if (restoreFocus && sourceRow && document.body.contains(sourceRow)) {
      sourceRow.focus();
    }
    sourceRow = null;
  }

  /** 「当前大文件」区渲染：共享查询域 scope 命中本目录时呈现其状态/结果
   * （含取消后的已取消态与 404 无法定位说明）；未命中时呈现待查询提示，
   * 绝不自动发起查询。历史 a→b 读数在本组件其余区块，互不覆盖。 */
  function renderDetailBigfiles() {
    const host = hostEl();
    if (!host || !activePath) return;
    const tbody = host.querySelector("[data-test='detail-bigfiles-tbody']");
    const modeSel = host.querySelector("#detail-bf-mode");
    const ctxEl = host.querySelector("[data-test='detail-bf-context']");
    if (!tbody) return;
    const eng = bigfilesEngine();
    const shared = eng.scope.path === activePath;
    if (modeSel) modeSel.value = shared ? eng.scope.mode : "largest";
    if (shared) {
      tbody.innerHTML = renderBigfilesTbody(eng);
      const at = bigfilesRequestedAtText(eng);
      if (ctxEl) {
        ctxEl.textContent = at
          ? `查询发起于 ${at}；以下大小为此刻 st_size 实测，与上方历史快照（KiB 累计）口径不同。`
          : "";
        ctxEl.hidden = !at;
      }
    } else {
      tbody.innerHTML =
        `<tr><td colspan="4" class="hint">尚未查询该目录。点「查看当前大文件」开始` +
        `（不会自动开始，也不后台遍历；离开本详情或改范围时自动取消在途查询）。</td></tr>`;
      if (ctxEl) { ctxEl.hidden = true; ctxEl.textContent = ""; }
    }
  }

  /** 区间变更/离页：关闭并作废在途 trend/browse（迟到响应不得写入）。 */
  function invalidate() {
    close({ restoreFocus: false });
  }

  /* ---------- 历史趋势（独立请求域 + 键守卫） ----------
   *
   * ISS-149：以所选对比快照 b 为显式锚请求 /api/trend——曲线只含 b 所属
   * 数据集（同根同阈值同排除掩码）的窗口，选择旧 a/b 时不再锚到最新数据
   * 集。响应锚定形态（含 dataset/anchor_snapshot_id）按窗口逐点渲染：
   * 缺条目为 null gap（connectNulls=false），tooltip 与等价表格读完整
   * 扫描时间 + 快照号（同日多次扫描可区分）；响应无锚定字段（旧 API）
   * 回落既有「最新快照口径」渲染。迟到响应由 beginRequest + activeKey
   * 双重守卫丢弃：a/b 变更后旧曲线不得恢复。 */

  function trendAnchorOf() {
    if (isDist) {
      // distribution：锚定所选快照本身——曲线窗口是该快照所属数据集。
      const sid = snapMeta?.id;
      return sid != null && /^\d+$/.test(String(sid)) ? String(sid) : null;
    }
    const b = rangeOf().b;
    return /^\d+$/.test(b) ? b : null;
  }

  async function loadTrend(path) {
    const target = hostEl()?.querySelector("#detail-trend-chart");
    if (!target) return;
    const request = beginRequest("detailTrend");
    const anchor = trendAnchorOf();
    const url = anchor
      ? `/api/trend?path=${encodeURIComponent(path)}&anchor_snapshot_id=${encodeURIComponent(anchor)}`
      : `/api/trend?path=${encodeURIComponent(path)}`;
    try {
      const r = await fetchJSON(url);
      if (!request.current() || keyOf(rangeOf(), path) !== activeKey) return;
      if (anchor && r.dataset) renderAnchoredTrend(target, r, anchor);
      else renderLegacyTrend(target, r);
    } catch (e) {
      if (!request.current() || keyOf(rangeOf(), path) !== activeKey) return;
      const host = hostEl();
      if (!host) return;
      const el = host.querySelector("#detail-trend-chart");
      if (el) el.innerHTML = `<span class="hint">趋势加载失败${e.status ? `（HTTP ${e.status}）` : ""}</span>`;
    }
  }

  /** 锚定形态：同数据集窗口逐点渲染，缺测为 null gap 不补 0。 */
  function renderAnchoredTrend(target, r, anchor) {
    const host = hostEl();
    const pts = r.points || [];
    const recorded = pts.filter((p) => p.recorded && p.size_kb != null);
    const latestEl = host?.querySelector("#detail-trend-latest");
    const tableHost = host?.querySelector("#detail-trend-table-host");
    // 窗口说明：数据集身份 + 截断如实标注（limit 截掉的是最早端）。
    const ds = r.dataset || {};
    const excludes = String(ds.exclude_names || "")
      .split(";").filter(Boolean).join("、") || "无";
    const hidden = r.truncated
      ? `，另有 ${Number(r.total_snapshots) - pts.length} 个较早快照未显示`
      : "";
    if (latestEl) {
      latestEl.textContent =
        `锚定 #${anchor}（同数据集快照 ${Number(r.total_snapshots)} 个，窗口显示最新 ${pts.length} 个${hidden}）· ` +
        `数据集：根 ${ds.root ?? "?"} · 阈值 ${ds.min_kb == null ? "未知" : ds.min_kb + " KB"} · 排除：${excludes}` +
        `（缺测点为未记录：可能低于入库阈值、权限受限或已移除，不构成删除证据）`;
      latestEl.hidden = false;
    }
    if (!recorded.length) {
      // 整个窗口都无记录：不画无数据的空线，也不列全"—"表格
      target.innerHTML =
        `<span class="hint">该路径在 #${escapeHtml(anchor)} 所属数据集内没有已记录的历史点（窗口 ${pts.length} 个快照）。不会用 0 冒充增长。</span>`;
      if (tableHost) { tableHost.hidden = true; tableHost.replaceChildren(); }
      return;
    }
    const xs = pts.map((p) => String(p.created_at || ""));
    const ys = pts.map((p) => (p.recorded && p.size_kb != null ? p.size_kb : null));
    const chart = echarts.init(target);
    chart.setOption({
      tooltip: { trigger: "axis", formatter: (ps) => ps.map((p) => {
        const pt = pts[p.dataIndex];
        if (!pt) return "";
        const head = `${pt.created_at || ""}${pt.snapshot_id != null ? `（#${pt.snapshot_id}）` : ""}`;
        const size = pt.recorded && pt.size_kb != null ? fmtKB(pt.size_kb) : "未记录（缺测）";
        return `${escapeHtml(head)}<br/>${escapeHtml(size)}`;
      }).join("<br/>") },
      grid: { left: 50, right: 8, top: 8, bottom: 22 },
      xAxis: { type: "category", data: xs,
        axisLabel: { fontSize: 10, formatter: (v) => String(v).slice(5, 10) } },
      yAxis: { type: "value", axisLabel: { formatter: (v) => fmtKB(v), fontSize: 10 }, scale: true },
      series: [{ type: "line", smooth: true, symbol: "circle", symbolSize: 5,
        connectNulls: false,  // 缺测断线：不把未记录快照连成实测
        data: ys, itemStyle: { color: cssVar("--trench") }, lineStyle: { width: 2 } }],
    }, true);
    if (tableHost) {
      // 等价读数表：读数不依赖指针悬停，键盘 Tab 逐行聚焦可读时间与容量；
      // 同日多次扫描按完整扫描时间（到秒）与快照号区分。
      const body = pts.map((pt) => {
        const at = String(pt.created_at || "").replace("T", " ");
        const sid = pt.snapshot_id != null ? `#${pt.snapshot_id}` : "";
        const known = pt.recorded && pt.size_kb != null;
        const sizeText = known ? fmtKB(pt.size_kb) : "—（未记录）";
        return `<tr tabindex="0" data-test="detail-trend-row" aria-label="扫描时间 ${escapeHtml(at)}，快照 ${escapeHtml(sid)}，` +
          `${known ? `大小 ${escapeHtml(sizeText)}` : "未记录（缺测，不构成删除证据）"}">` +
          `<td>${escapeHtml(at)}</td><td>${escapeHtml(sid)}</td>` +
          `<td class="num">${known ? escapeHtml(sizeText) : `<span class="delta-none">—</span>`}</td></tr>`;
      }).join("");
      tableHost.innerHTML =
        `<p class="hint" style="margin:6px 0 2px">各快照读数（缺测行如实标注；键盘 Tab 逐行可读）</p>` +
        `<div style="max-height:160px;overflow:auto;border:1px solid var(--border,#e0e0e0);border-radius:4px">` +
        `<table style="width:100%;border-collapse:collapse;font-size:12px" data-test="detail-trend-table">` +
        `<thead><tr><th style="text-align:left;padding:3px 6px">扫描时间</th>` +
        `<th style="text-align:left;padding:3px 6px">快照</th>` +
        `<th style="text-align:right;padding:3px 6px">大小</th></tr></thead>` +
        `<tbody>${body}</tbody></table></div>`;
      tableHost.hidden = false;
    }
  }

  /** 旧形态（无锚定字段）：保持 ISS-143 悬停合同不变。 */
  function renderLegacyTrend(target, r) {
    const pts = r.points || [];
    const latestEl = hostEl()?.querySelector("#detail-trend-latest");
    const tableHost = hostEl()?.querySelector("#detail-trend-table-host");
    if (tableHost) { tableHost.hidden = true; tableHost.replaceChildren(); }
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

  /** distribution（ISS-159）：所选快照下该目录的历史直属子行。
   * 点击子行在详情内下钻（子路径重开详情，键守卫绑定同一快照）；
   * 分页只提示总数，完整列表由「在浏览器中定位」承接。 */
  async function loadChildren(path) {
    const target = hostEl()?.querySelector("#detail-children-host");
    if (!target) return;
    const sid = snapMeta?.id;
    if (sid == null || !/^\d+$/.test(String(sid))) {
      target.innerHTML = `<span class="hint">快照上下文不可用，无法读取该快照下的直属子目录。</span>`;
      return;
    }
    const request = beginRequest("detailChildren");
    try {
      const r = await fetchJSON(
        `/api/browse?path=${encodeURIComponent(path)}&snapshot_id=${encodeURIComponent(String(sid))}`);
      if (!request.current() || keyOf(rangeOf(), path) !== activeKey) return;
      const kids = r.children || [];
      const pag = r.pagination;
      const head = pag && pag.total > pag.returned
        ? `<p class="hint">显示前 ${pag.returned} 个（共 ${pag.total} 个，未展示不当作无子目录）——完整列表用「在浏览器中定位」。</p>`
        : "";
      if (!kids.length) {
        target.innerHTML = `${head}<span class="hint" data-test="browse-detail-children-empty">该目录在此快照下没有已记录的直属子目录。</span>`;
        return;
      }
      const cmpHead = r.comparison ? `较 #${escapeHtml(String(r.comparison.snapshot_id))}` : "较前快照";
      const rows = kids.map((k) => {
        const known = k.size_kb != null;
        const deltaCls = k.delta_kb == null || k.delta_kb === 0
          ? "" : (k.delta_kb > 0 ? "delta-grow" : "delta-shrink");
        return `<tr tabindex="0" data-child-path="${escapeHtml(k.path)}"` +
          ` data-child-size="${k.size_kb ?? ""}" data-child-status="${escapeHtml(k.status || "measured")}"` +
          ` aria-label="在详情中打开 ${escapeHtml(k.path)}">` +
          `<td>${escapeHtml(k.name)}</td>` +
          `<td class="num">${known ? escapeHtml(fmtKB(k.size_kb)) : '<span class="hint">未直接记录</span>'}</td>` +
          `<td class="num ${deltaCls}">${escapeHtml(fmtDelta(k.delta_kb))}</td></tr>`;
      }).join("");
      target.innerHTML =
        `${head}<table class="tbl" data-test="browse-detail-children-table">` +
        `<thead><tr><th>子目录</th><th class="num">大小</th><th class="num">${cmpHead}</th></tr></thead>` +
        `<tbody>${rows}</tbody></table>`;
      target.querySelectorAll("[data-child-path]").forEach((el) => {
        const go = () => open({
          path: el.dataset.childPath,
          row: {
            size_kb: el.dataset.childSize === "" ? null : Number(el.dataset.childSize),
            status: el.dataset.childStatus || "measured",
          },
          sourceRow: null,
        });
        el.addEventListener("click", go);
        el.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
        });
      });
    } catch (e) {
      if (!request.current() || keyOf(rangeOf(), path) !== activeKey) return;
      const el = hostEl()?.querySelector("#detail-children-host");
      if (el) el.innerHTML =
        `<span class="hint">直属子目录加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${escapeHtml(e.message)}</span>`;
    }
  }

  // ISS-151：共享大文件查询域状态变化（提交/运行/取消/终态/错误）时，
  // 打开中的详情即时重画「当前大文件」区；详情关闭时重画为无害空操作。
  subscribeBigfiles(() => renderDetailBigfiles());

  return {
    open,
    close,
    invalidate,
    get isOpen() { const host = hostEl(); return Boolean(host && !host.hidden); },
    get activePath() { return activePath; },
  };
}
