/* 总览页：质量行 + 主结论区（变化优先）+ 最近变化摘要 + 卷容量走势（次级，
 * 读数行 + 迷你图 + 表格等价） + 最近扫描说明。
 *
 * 前端责任（ISS-027 模块合同）：
 * - beginRequest 世代号保证乱序/迟到响应不覆盖较新查询；
 * - 仅 frontend/icons.js 的 SVG 图标；零 emoji。
 *
 * 展示（ISS-028；层级重排与状态独立化归 ISS-106）：
 * - 数据时间 / 范围 / 质量行：根路径、a → b 时间窗口、覆盖状态（full /
 *   partial / missing），缺路径不可知时显式说明，不冒充完整；
 * - 主结论区先回答「本次比上次变化多少」：headline 净变化 = 根同口径差分
 *   （b.total_kb − a.total_kb，与变化页 renderNetLine 同一口径；grown/shrunk
 *   行按 fold_changes 可含父子重叠且经 topn/min_delta 截断，禁止逐行求和
 *   推净变化）；任一侧缺 total_kb（无基线）显示不可知，绝不伪造 0；
 * - 五态容器（ISS-106 验收 2）：无快照 / 单快照 / 零变化 / 部分覆盖 /
 *   请求失败各有准确说明与可达下一步——等待型（state-wait，bg 块）不当成
 *   失败，错误型（state-error，--danger 派生浅底）带重试且位于首屏主结论
 *   区，不藏在图表下面；部分覆盖以质量徽章 + 覆盖说明块（conclusion-foot）
 *   与结论同屏；
 * - 走势表格等价：迷你走势图 + "以表格查看"折叠（DESIGN：图表有表格替代）。
 */
import { fetchJSON, beginRequest, revealInFinder } from "../request.js";
import { fmtBytes, fmtKB, fmtDelta, shortPath, escapeHtml } from "../format.js";
import { initChart, showChartMessage } from "../charts.js";
import { state } from "../state.js";
import { icon } from "../../icons.js";

/* ISS-084：图表色与 style.css :root 语义 token 同源（单一色源，不硬编码）。
 * 脚本为 module（defer），执行时 CSSOM 已就绪。 */
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/* 把 ISO 时间戳规整为 "MM-DD HH:MM"（保留日期与时间，便于一眼识别） */
function _shortTs(iso) {
  if (!iso) return "—";
  return String(iso).slice(0, 16).replace("T", " ");
}

/* 覆盖状态判定：collection_status 在 api.snapshots 里有值；fallback 到无
 * 返回结构含三类缺口计数（ISS-002A）：
 *   state: missing | partial | full
 *   denied:      du stderr 权限错误行数（denied_count，?? 0 防御缺失字段）
 *   vanished:    du 输出后校验时未能确认仍存在的目录路径数（vanished_count）
 *   excluded:    排除掩码项数（exclude_names 数组长度；缺字段 0）
 * denied 不等于不同目录数；三类计数不求和不推比例。 */
function _coverage(snapshot) {
  if (!snapshot) return { state: "missing", denied: 0, vanished: 0, confirmed: null, unverified: null, excluded: 0 };
  const denied = snapshot.denied_count ?? 0;
  const vanished = snapshot.vanished_count ?? 0;
  const classified = Number.isInteger(snapshot.confirmed_missing_count) &&
    Number.isInteger(snapshot.path_unverified_count);
  const confirmed = classified ? snapshot.confirmed_missing_count : null;
  const unverified = classified ? snapshot.path_unverified_count : null;
  const ex = snapshot.exclude_names;
  let excluded = 0;
  if (Array.isArray(ex)) excluded = ex.length;
  else if (typeof ex === "string" && ex.trim()) {
    // ISS-066 持久化为规范串（";" 分隔）；前端取非空段数
    excluded = ex.split(";").filter((s) => s.trim()).length;
  }
  let state;
  if (snapshot.collection_status === "partial") state = "partial";
  else if (denied > 0) state = "partial";  // 兼容旧版本
  else state = "full";
  return { state, denied, vanished, confirmed, unverified, excluded };
}

/* 三类缺口的「意味着什么 / 不意味着什么」文案（ISS-002A）。
 * 每类固定句：means 是真实事实，doesn't-means 是反对误读。
 * 严禁出现"数量=影响大小"或"未记录=已删除"表述。 */
const COV_NOTES = {
  denied: {
    label: "权限受限",
    means: "最近扫描的 du 输出了读取权限错误；每条代表一行错误记录。",
    doesnt: "同一路径可能出现多条记录；行数不等于未读取的目录数，也不代表影响大小。",
  },
  vanished: {
    label: "目录状态未确认",
    means: "du 输出后，校验时未能确认这些目录路径仍存在。",
    doesnt: "可能已移动、被清理，也可能因权限无法访问；不能认定已删除或据此计算影响空间。",
  },
  confirmed: {
    label: "目录校验时路径不存在",
    means: "du 输出后，校验时确认这些目录路径不存在。",
    doesnt: "这只说明校验时的状态；不能推断由谁删除，也不能据此计算影响空间。",
  },
  unverified: {
    label: "目录状态无法确认",
    means: "du 输出后，校验时无法确认这些目录路径是否仍存在。",
    doesnt: "可能因权限或其他读取错误无法访问；不能认定已删除或据此计算影响空间。",
  },
  excluded: {
    label: "排除掩码",
    means: "这些目录从未进入扫描集；与默认配置形成不同数据集。",
    doesnt: "历史可比范围相应收窄，但不改变已记录的事实。",
  },
};

/* 把三类缺口渲染成可解释块；缺一类即不渲染该块；full 时只显示完整覆盖 */
function _renderCoverageClasses(coverage) {
  if (coverage.state === "full") {
    return `<span class="quality-chip ok">${icon("shield", 12)} 完整覆盖</span>`;
  }
  const classified = coverage.confirmed !== null && coverage.unverified !== null;
  const order = classified
    ? ["denied", "confirmed", "unverified", "excluded"]
    : ["denied", "vanished", "excluded"];
  const counts = { denied: coverage.denied, vanished: coverage.vanished,
    confirmed: coverage.confirmed, unverified: coverage.unverified, excluded: coverage.excluded };
  const items = order
    .filter((k) => counts[k] > 0)
    .map((k) => {
      const note = COV_NOTES[k];
      const chipCls = k === "excluded" ? "quality-chip miss" : "quality-chip warn";
      const chipIcon = k === "excluded" ? "filter" : "alert";
      const unit = k === "denied" ? "条读取受限记录" : k === "excluded" ? "项" : "个";
      // 计数前置；denied 是错误行数，vanished 是目录数，excluded 是掩码数。
      // 验收契约要求「计数在前、类目在后」，避免读成「类目 N」被误当影响大小。
      return `<li class="cov-class">
        <span class="cov-class-head">
          <span class="${chipCls}">${icon(chipIcon, 12)} ${counts[k]} ${unit}${k === "denied" ? "" : escapeHtml(note.label)}</span>
        </span>
        <p class="cov-class-note">${escapeHtml(note.means)} ${escapeHtml(note.doesnt)}</p>
      </li>`;
    });
  if (!items.length) {
    // partial 但三类计数全 0（极端：旧快照仅有 collection_status 标记）——
    // 仍显式说明，避免误以为完整覆盖
    return `<span class="quality-chip warn">${icon("alert", 12)} 部分覆盖（未统计缺口细节）</span>`;
  }
  return `<ul class="cov-classes" data-test="coverage-classes">${items.join("")}</ul>`;
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
  // 质量徽章概括最新快照；denied 是错误行数，不称不同目录数。
  // 详细分类放在独立 #overview-coverage-note 区块。
  const pathChip = cov.confirmed !== null
    ? (cov.confirmed > 0 ? `${cov.confirmed} 个目录校验时路径不存在` :
      cov.unverified > 0 ? `${cov.unverified} 个目录状态无法确认` : "")
    : (cov.vanished > 0 ? `${cov.vanished} 个目录状态未确认` : "");
  const covChip = cov.state === "full"
    ? `<span class="quality-chip ok">${icon("alert", 12)} 覆盖完整</span>`
    : cov.state === "partial"
      ? `<span class="quality-chip warn">${icon("alert", 12)} 部分覆盖${cov.denied > 0 ? `（${cov.denied} 条读取受限记录）` : pathChip ? `（${pathChip}）` : ""}</span>`
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
    // 快照状态不可知时同步清空覆盖说明：不留上一次的过期内容。
    _renderCoverageNoteBlock(null, { state: "missing" });
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
  // 最近扫描的权限错误是 du stderr 行数，不冒充不同目录数。
  if (cov.denied > 0) {
    parts.push(`<span class="st st-restricted">${icon("alert", 12)} ${cov.denied} 条读取受限记录</span>`);
  }
  if (cov.state === "full") {
    parts.push(`<span class="st st-ok">覆盖完整</span>`);
  } else if (cov.state === "partial" && !cov.denied) {
    // partial 但三类缺口计数为 0（罕见：旧快照仅有 collection_status 标记）
    parts.push(`<span class="st st-restricted">部分覆盖</span>`);
  }
  el.innerHTML = parts.join(" · ");
  // 追加式三类覆盖说明（ISS-002A）：独立区块，不动既有 scan-note 文案。
  _renderCoverageNoteBlock(latest, cov);
}

/* ISS-002A 三类覆盖说明——追加式（不替换既有 scan-note）。
 * 数据来自 _coverage() 的 denied / vanished / excluded；
 * full 时只渲染「完整覆盖」；三类缺口按需渲染，每个缺口都带
 * 「意味着什么 / 不意味着什么」文案（详见 COV_NOTES）。
 * 容器 id 由调用方决定：默认 `#overview-coverage-note`（与既有
 * [data-test='coverage-classes'] 复用同一 DOM 节点）。
 * ISS-106：容器挂主结论区的 #overview-coverage-slot（与质量徽章、
 * 28px 结论同屏）——部分覆盖说明不再藏在图表或页尾之下；slot 缺失时
 * 回退追加在 #overview-scan-note 之后（旧结构防御）。 */
function _ensureCoverageNote() {
  let container = document.getElementById("overview-coverage-note");
  if (container) return container;
  const slot = document.getElementById("overview-coverage-slot");
  container = document.createElement("div");
  container.id = "overview-coverage-note";
  container.className = "coverage-note-block";
  container.setAttribute("aria-live", "polite");
  if (slot) {
    slot.appendChild(container);
    return container;
  }
  const scanNote = document.getElementById("overview-scan-note");
  if (!scanNote || !scanNote.parentNode) return null;
  scanNote.parentNode.insertBefore(container, scanNote.nextSibling);
  return container;
}

function _renderCoverageNoteBlock(latest, cov) {
  const container = _ensureCoverageNote();
  if (!container) return;
  // 不可知：保留旧"加载中…"或留空，避免误以为完整覆盖
  if (!latest || cov.state === "missing") {
    container.innerHTML = "";
    return;
  }
  const markup = _renderCoverageClasses(cov);
  // _renderCoverageClasses 在 partial 时返回 <ul data-test="coverage-classes">…</ul>，
  // 在 full 时返回不带 data-test 的「完整覆盖」chip。两者都渲染：
  // full 时该区块显式给出「完整覆盖」，避免读者把「区块为空」误当成未知；
  // 同时因无 coverage-classes 节点，既有 state-matrix-partial-* 检查不受影响。
  container.innerHTML = markup;
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
          areaStyle: { opacity: 0.12 }, itemStyle: { color: cssVar("--used") } },
        { name: "剩余", type: "line", smooth: true, symbol: "none",
          data: rows.map((r) => r.free_bytes), itemStyle: { color: cssVar("--mineral") } },
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

/* ---------- 主结论区 + 最近变化摘要（ISS-106：变化优先、五态独立） ---------- */

/* 根同口径净变化 = b.total_kb − a.total_kb（与变化页 renderNetLine 同一口径：
 * grown/shrunk 行按 fold_changes 可含父子重叠（DEC-005）且经 topn/min_delta_kb
 * 截断——逐行求和不等于任何口径的净变化，禁止回退到求和。
 * 任一侧缺 total_kb（如首扫无基线）返回 null，调用方不得显示 0。 */
function _computeNet(d) {
  const prev = Number(d.a && d.a.total_kb), curr = Number(d.b && d.b.total_kb);
  if (!Number.isFinite(prev) || !Number.isFinite(curr)) return null;
  return curr - prev;
}

function _setConclusionActions(html) {
  const el = document.getElementById("conclusion-actions");
  if (el) el.innerHTML = html;
}

/* 就绪态：28px 一级结论（每页唯一 display 刻度）+ 来源一句 + 定位入口。
 * headline 按根同口径净变化着色（增长赭 / 缩减青 / 零或不可知中性）。 */
function _renderConclusionReady(d) {
  const body = document.getElementById("conclusion-body");
  if (!body) return;
  const net = _computeNet(d);
  const measured = (d.grown || []).length + (d.shrunk || []).length;
  const unrecorded = (d.added || []).length + (d.removed || []).length;
  let headText, headCls;
  if (net == null) { headText = "净变化不可知"; headCls = ""; }
  else if (net > 0) { headText = `最近增长 ${fmtDelta(net)}`; headCls = "delta-grow"; }
  else if (net < 0) { headText = `最近缩减 ${fmtDelta(net)}`; headCls = "delta-shrink"; }
  else { headText = "最近无净变化"; headCls = ""; }
  const span = `${String(d.a?.created_at || "").slice(0, 10)} → ${String(d.b?.created_at || "").slice(0, 10)}`;
  const kicker = `最近变化 · 已对比快照 #${d.a?.id ?? "?"} → #${d.b?.id ?? "?"}（${span}）`;
  let sub;
  if (net == null) {
    sub = "快照缺少根总量（无基线），净变化不可知；不用列表行求和推算。";
  } else if (measured) {
    const srcs = (d.grown || []).slice(0, 2)
      .map((r) => `${shortPath(r.path, 2)}（${fmtDelta(r.delta_kb)}）`);
    sub = srcs.length ? `主要来自 ${srcs.join(" 与 ")}` : "存在 ≥1MB 的可测量变化";
    const shrunkTop = (d.shrunk || [])[0];
    if (shrunkTop) sub += `；${shortPath(shrunkTop.path, 2)} 释放 ${fmtKB(Math.abs(shrunkTop.delta_kb))}`;
    sub += "。来源用途未识别时，只描述路径与变化量。";
    if (unrecorded) sub += `另有 ${unrecorded} 个新增或未记录目录，不计入净变化。`;
  } else if (unrecorded) {
    sub = `没有 ≥1MB 的可测量行；另有 ${unrecorded} 个新增或未记录目录，不计入净变化——不能判为整体无变化。`;
  } else {
    sub = "对比期间没有 ≥1MB 的目录变化；基线已建立，下次扫描前这里不会显示假数据。";
  }
  body.innerHTML =
    `<p class="conclusion-kicker">${escapeHtml(kicker)}</p>` +
    `<h2 class="conclusion-headline ${headCls}" data-test="conclusion-headline">${escapeHtml(headText)}</h2>` +
    `<p class="conclusion-sub" data-test="conclusion-sub">${escapeHtml(sub)}</p>`;
  // 实心主按钮每视口至多一个（ISS-104 候选 #3）：总览的主任务 = 查看变化
  _setConclusionActions(
    `<a class="btn primary" href="#/changes" data-test="cta-view-changes">查看完整对比 →</a>` +
    `<a class="btn" href="#/browse">查看分布</a>`);
}

/* 等待型（ISS-104 候选 #10）：无快照 / 单快照是正常等待，不是失败——
 * bg 块容器，说明 + 可达下一步；扫描运行中由顶栏徽章表达，此处不渲染错误样式 */
function _renderConclusionWaiting(kind, snap) {
  const body = document.getElementById("conclusion-body");
  if (!body) return;
  const head = kind === "empty" ? "尚无快照" : "基线已建立，等待下一次扫描";
  const note = kind === "empty"
    ? "完成首次扫描建立基线后，这里会显示数据时间、范围与最近变化；点击右上角「立即扫描」即可开始，监控范围可在设置中确认。"
    : `已有快照 #${snap?.id ?? "?"}（${escapeHtml(_shortTs(snap?.created_at))}）。需要另一个不同日期的有效快照才能比较；等待下次扫描自动建立对比，分布现在可用。`;
  body.innerHTML =
    `<p class="conclusion-kicker">最近变化</p>` +
    `<div class="state-wait" data-test="conclusion-wait" role="status">` +
    `<span class="sw-title">${escapeHtml(head)}</span>` +
    `<p class="sw-note">${note}</p></div>`;
  _setConclusionActions(kind === "single"
    ? `<a class="btn" href="#/browse">查看分布</a>`
    : `<a class="btn" href="#/settings">检查监控范围</a>`);
}

/* 错误型：--danger 派生浅红底容器，位于主结论区（首屏），保留上次成功
 * 数据时间并提供重试——不藏在图表下面，也不与等待态共用容器 */
function _renderConclusionError(e, snaps) {
  const body = document.getElementById("conclusion-body");
  if (!body) return;
  const title = e.status === 0
    ? "无法连接本地服务"
    : `最近变化加载失败（HTTP ${e.status || "?"}）`;
  const note = e.status === 0
    ? "最近变化暂不可用。请确认 Fathom 服务正在运行后重试。"
    : `${e.message}。上次有效数据不代表本次无变化。`;
  const lastOk = snaps && snaps[0]
    ? `<p class="se-note">上次成功数据：${escapeHtml(_shortTs(snaps[0].created_at))}（快照 #${snaps[0].id ?? "?"}）</p>`
    : "";
  body.innerHTML =
    `<p class="conclusion-kicker">最近变化</p>` +
    `<div class="state-error" data-test="conclusion-error" role="status">` +
    `<span class="se-title">${escapeHtml(title)}</span>` +
    `<span>${escapeHtml(note)}</span>${lastOk}` +
    `<button type="button" class="diff-retry" data-test="conclusion-retry">重试</button></div>`;
  _setConclusionActions("");
  body.querySelector("[data-test='conclusion-retry']")
    ?.addEventListener("click", () => loadOverviewChanges());
}

function _renderSummaryWaiting(kind) {
  const el = document.getElementById("overview-summary");
  if (!el) return;
  el.innerHTML = kind === "empty"
    ? `<p class="hint">还不能比较：尚无快照。完成首次扫描建立基线后，这里会显示最近变化。</p>`
    : `<p class="hint">还不能比较：基线已建立；需要另一个不同日期的有效快照，分布现在可用。</p>`;
}

function _renderSummaryError(e) {
  const el = document.getElementById("overview-summary");
  if (!el) return;
  const message = e.status === 0
    ? "无法连接本地服务，最近变化暂不可用。请确认 Fathom 服务正在运行后重试。"
    : `最近变化加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}。上次有效数据不代表本次无变化。`;
  el.innerHTML = `<p class="hint">${escapeHtml(message)}</p>`;
}

function _renderSummaryTable(d) {
  const el = document.getElementById("overview-summary");
  if (!el) return;
  const span = d.b.created_at.slice(0, 10) + " vs " + d.a.created_at.slice(0, 10);
  const measured = d.grown.length + d.shrunk.length;
  const unrecorded = d.added.length + d.removed.length;
  if (!measured && !unrecorded) {
    el.innerHTML = `<p class="hint">${escapeHtml(span)} 期间没有 ≥1MB 的目录变化。基线已建立；下次扫描前这里不会显示假数据。</p>`;
    return;
  }
  const summary = !measured
    ? `没有 ≥1MB 的可测量行，仅发现 ${unrecorded} 个新增或未记录目录；不能判断为“无变化”。`
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
}

/* 主结论与摘要共用一次快照 + 一次差分请求（避免双请求竞态），先以快照
 * 数量分流五态：无快照/单快照直接进入等待型（不发差分请求）；其余拉取
 * 差分后渲染就绪结论与明细表；请求失败进入错误型（含重试）。 */
async function loadOverviewChanges() {
  const request = beginRequest("overviewSummary");
  let snaps;
  try {
    snaps = await fetchJSON("/api/snapshots");
  } catch (e) {
    if (!request.current()) return;
    _renderConclusionError(e, null);
    _renderSummaryError(e);
    return;
  }
  if (!request.current()) return;
  if (snaps.length < 2) {
    const kind = snaps.length === 0 ? "empty" : "single";
    _renderConclusionWaiting(kind, snaps[0]);
    _renderSummaryWaiting(kind);
    return;
  }
  let d;
  try {
    d = await fetchJSON("/api/diff?topn=5");
  } catch (e) {
    if (!request.current()) return;
    _renderConclusionError(e, snaps);
    _renderSummaryError(e);
    return;
  }
  if (!request.current()) return;
  _renderConclusionReady(d);
  _renderSummaryTable(d);
}

export const overviewPage = {
  id: "overview",
  load() {
    loadQualityLine();
    loadOverviewChanges();
    loadVolumeTrend();
    loadScanNote();
    loadStorageOverview();
  },
};

/* ===== ISS-158 · 整盘总览与变化入口 =====
 *
 * 一条主要结果 + 一个主按钮「排查这次变化」。三条红线：
 *  1. **共享剩余只算一次**：多卷共享容器剩余空间，页面读 capacity.free_bytes
 *     单值，绝不把卷级 free 相加（同 157 shared_free_once）。
 *  2. **差额是「未知差额」不是「可清理」**：unexplained 是有符号差值，
 *     措辞用「未知差额 / 尚无法由目录变化解释」，不出现回收/垃圾/可释放。
 *  3. **不可比就写不可比**：comparable=false 时给 reason，绝不补 0。
 * legacy 首页（scope.mode 为空、未启用范围能力）不伪装整盘：显示
 * 「尚未启用整盘范围」并给设置入口，而不是渲染一个假整盘结论。
 */
function _ovSlot() { return document.getElementById("overview-storage"); }

/* 整盘能力是否真的启用：容器身份或范围模式齐备才算整盘口径。 */
function _isWholeDisk(summary) {
  const scope = summary?.scope || {};
  return Boolean(scope.mode) && Array.isArray(scope.roots) && scope.roots.length > 0;
}

/* ISS-158：summary.unexplained.bytes 是**字节**（157 侧 capacity 口径为
 * unit=bytes，目录侧已显式换算）；既有 fmtDelta 吃的是 KB，混用会差 1024
 * 倍。这里显式做带符号字节格式化，绝不复用 fmtDelta。 */
function _fmtSignedBytes(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n)) return "—";
  const sign = n > 0 ? "+" : n < 0 ? "−" : "";
  return sign + fmtBytes(Math.abs(n));
}

function _ovMeta(summary) {
  const scope = summary?.scope || {};
  const roots = Array.isArray(scope.roots) ? scope.roots : [];
  const r = summary?.round || null;
  const p = summary?.previous_round || null;
  const bits = [];
  // 长范围名：完整路径在 title 里，正文截断但不隐藏
  bits.push(roots.length
    ? `范围 ${roots.map((x) => shortPath(x, 3)).join("、")}`
    : "范围未选择");
  const span = `${_shortTs(p?.started_at)} → ${_shortTs(r?.finished_at || r?.started_at)}`;
  bits.push(`起止 ${span}`);
  bits.push(r?.status ? `采集 ${r.status}` : "无采集轮次");
  return bits.join(" · ");
}

function _ovUnexplainedBlock(u) {
  if (!u) return "";
  if (u.comparable === false || u.bytes === null || u.bytes === undefined) {
    // 不可比/缺读数：写明原因，绝不补 0
    return `<div class="ov-unexplained" data-test="storage-unexplained" data-state="unknown">
      <span class="rl">未知差额</span>
      <span class="rv rv-s">不可计算</span>
      <p class="hint">${escapeHtml(u.reason || "两侧不可比：缺同主体/同计划身份或存在无效样本。")}</p>
    </div>`;
  }
  const b = Number(u.bytes);
  const cls = b > 0 ? "delta-grow" : b < 0 ? "delta-shrink" : "";
  const word = b > 0 ? "占用增加多于目录测量" : b < 0 ? "容器占用减少" : "两侧一致";
  return `<div class="ov-unexplained" data-test="storage-unexplained" data-state="known" data-bytes="${b}">
    <span class="rl">未知差额</span>
    <span class="rv ${cls}" data-test="storage-unexplained-value">${escapeHtml(_fmtSignedBytes(b))}</span>
    <p class="hint">${escapeHtml(u.limitation || "尚无法由目录变化解释")}（${escapeHtml(word)}）；
      不代表垃圾量或可回收空间，也不提供任何删除或清理操作。</p>
  </div>`;
}

function _ovMembersBlock(attr) {
  const stale = Array.isArray(attr?.stale_members) ? attr.stale_members : [];
  const absorbed = Array.isArray(attr?.absorbed_roots) ? attr.absorbed_roots : [];
  const roots = Array.isArray(attr?.attribution_roots) ? attr.attribution_roots : [];
  const out = [];
  out.push(`<div class="ov-members" data-test="storage-roots">测量根 ${roots.length} 个${
    absorbed.length ? ` · 已吸收子根 ${absorbed.length} 个（父子不可加）` : ""}</div>`);
  if (stale.length) {
    out.push(`<ul class="cov-classes" data-test="storage-stale">${stale.map((m) => `
      <li class="cov-class"><span class="quality-chip warn">${icon("alert", 12)} ${
        escapeHtml(m.display_name || m.root || m.scope_id || "成员")}</span>
      <p class="cov-class-note">${escapeHtml(m.stale_note ||
        "本轮无新快照：若有旧有效值只作历史参考，不当本轮贡献。")}</p></li>`).join("")}</ul>`);
  }
  if (attr && attr.comparable_to_previous === false) {
    out.push(`<p class="hint" data-test="storage-incomparable">${
      escapeHtml(attr.comparable_note ||
        "需要同主体、同计划、两侧都有效才可比；否则差额不可计算。")}</p>`);
  }
  return out.join("");
}

function _ovCta(summary) {
  const attr = summary?.attribution || null;
  // b = 摘要**本轮成员**已落库的快照 ID（真实数据，非自造）。
  // a 不从本轮成员取（那会和 b 相同）：摘要只暴露本轮归因，前一轮成员
  // 未随负载给出，故不编造 a，交接时留空让变化页按自己的快照列表选一个
  // 真实且不同于 b 的前驱；找不到就放弃交接，绝不塞假基线。
  const bId = attr?.measured_members?.find((m) => m.snapshot_id)?.snapshot_id ?? null;
  if (!bId) return "";
  return `<a class="btn primary" href="#/changes" data-test="storage-cta"
    data-a="" data-b="${bId}">排查这次变化</a>`;
}

/* ISS-158 a/b 交接：主按钮把**摘要里真实的快照 ID**带到变化页。
 * 变化页是只读依赖（不在本卡可写范围），因此这里只做一次性交接：
 * 点击时记下 a/b，进入 #/changes 后等它自己的快照选项就绪，再写进
 * 它自有的 sel-a/sel-b 并派发 change——由变化页按自己的列表校验有效性。
 * 绝不伪造基线 ID；找不到对应选项就放弃交接（变化页保持自己的默认选择）。 */
/* 从变化页自己的快照列表里取 b 之前最近的一个真实快照 ID（列表已按时间
 * 倒序：新 → 旧）。找不到（b 之外没有别的快照）返回 null，交接放弃。 */
function _pickPredecessorId(selA, b) {
  const values = [...selA.options].map((o) => o.value);
  const i = values.indexOf(String(b));
  if (i < 0) return values[0] ?? null;
  return i + 1 < values.length ? values[i + 1] : null;
}

function _handOffChangeEntry(a, b) {
  state.pendingChangeEntry = { a: a || null, b: String(b) };
  window.addEventListener("hashchange", function once() {
    if (!state.pendingChangeEntry) {
      window.removeEventListener("hashchange", once);
      return;
    }
    if (!(location.hash || "").startsWith("#/changes")) return;
    window.removeEventListener("hashchange", once);
    const deadline = Date.now() + 8000;
    const tick = () => {
      const entry = state.pendingChangeEntry;
      if (!entry) return;
      /* ISS-170 R2：用户已在变化页真实改选过（isTrusted change 置标志）时，
       * 入口交接立即放弃——迟到 tick 不得把用户刚选的区间覆盖回入口旧值
       * （实测三连发 (1,4)/(1,2)/(1,4) 的最后一发即本 tick 所写）。 */
      if (window.__changesUserTouched) {
        state.pendingChangeEntry = null;
        window.removeEventListener("hashchange", once);
        return;
      }
      const selA = document.getElementById("sel-a");
      const selB = document.getElementById("sel-b");
      const ready = selA && selB && selB.options.length > 0;
      const hasB = ready && [...selB.options].some((o) => o.value === entry.b);
      if (hasB) {
        selB.value = entry.b;
        // a 优先用入口带来的真实 ID；入口没带 a 时，从变化页**自己的**快照
        // 列表里挑一个真实存在且不同于 b 的前驱（取 b 之前最近的一个）。
        // 绝不让 a === b（那是无效区间），也绝不塞自造的假基线 ID。
        const aWanted = entry.a && [...selA.options].some((o) => o.value === entry.a)
          && entry.a !== entry.b
          ? entry.a
          : _pickPredecessorId(selA, entry.b);
        if (aWanted) selA.value = aWanted;
        state.pendingChangeEntry = null;
        selA.dispatchEvent(new Event("change", { bubbles: true }));
        selB.dispatchEvent(new Event("change", { bubbles: true }));
        return;
      }
      if (Date.now() > deadline) { state.pendingChangeEntry = null; return; }
      setTimeout(tick, 100);
    };
    tick();
  });
}

function _renderStorageReady(summary) {
  const el = _ovSlot();
  if (!el) return;
  const cap = summary.capacity || {};
  const attr = summary.attribution || null;
  const free = cap.free_bytes ?? null;
  const total = cap.total_bytes ?? null;
  const used = (Number.isFinite(free) && Number.isFinite(total))
    ? total - free : null;
  const r = summary.round || null;
  const kicker = r ? `最近一轮 · 容器占用读数` : "最近一轮 · 无轮次";
  el.innerHTML =
    `<p class="conclusion-kicker" data-test="storage-kicker">${escapeHtml(kicker)}</p>
     <div class="vol-readout">
       <div class="readout"><span class="rl">整体已用</span><span class="rv rv-used" data-test="storage-used">${
         used === null ? "不可知" : escapeHtml(fmtBytes(used))}</span></div>
       <div class="readout"><span class="rl">整体剩余</span><span class="rv rv-ok" data-test="storage-free">${
         free === null ? "不可知" : escapeHtml(fmtBytes(free))}</span></div>
     </div>
     <p class="hint" data-test="storage-meta" title="${escapeHtml(_ovMeta(summary))}">${
       escapeHtml(_ovMeta(summary))}</p>
     ${_ovUnexplainedBlock(summary.unexplained)}
     ${_ovMembersBlock(attr)}
     <div class="conclusion-actions">${_ovCta(summary)}</div>`;
  // 主按钮交接真实 a/b 给变化页（变化页自身校验）
  el.querySelector('[data-test="storage-cta"]')?.addEventListener("click", (ev) => {
    const btn = ev.currentTarget;
    _handOffChangeEntry(btn.dataset.a, btn.dataset.b);
  });
}

function _renderStorageLegacy(summary) {
  const el = _ovSlot();
  if (!el) return;
  el.innerHTML =
    `<p class="conclusion-kicker" data-test="storage-kicker">整盘总览</p>
     <h2 class="conclusion-headline" data-test="storage-headline">尚未启用整盘范围</h2>
     <p class="conclusion-sub" data-test="storage-sub">当前只在单个目录范围内扫描，
       还没有整盘口径的容量与变化数据；这里不显示整盘结论，也不把目录结果冒充整盘。
       启用范围并完成首扫后，这里会出现「排查这次变化」。</p>
     <div class="conclusion-actions"><a class="btn primary" href="#/settings"
       data-test="storage-settings-entry">去设置范围</a></div>`;
}

function _renderStorageWaiting(kind) {
  const el = _ovSlot();
  if (!el) return;
  const text = kind === "empty"
    ? "还没有整盘采集数据。完成首扫后这里会出现整盘占用与变化；现在不显示任何容量数字。"
    : "只有一轮采集，缺同计划前一轮可比基线，变化与未知差额都不可知；不会用单轮冒充差分。";
  el.innerHTML =
    `<p class="conclusion-kicker" data-test="storage-kicker">整盘总览</p>
     <div class="state-wait" data-test="storage-state"><span class="dr-loading" data-dr-spin aria-hidden="true"></span>${escapeHtml(text)}</div>
     <div class="conclusion-actions"><a class="btn" href="#/settings"
       data-test="storage-settings-entry">去设置范围</a></div>`;
}

function _renderStorageError(e) {
  const el = _ovSlot();
  if (!el) return;
  const why = e?.status === 0
    ? "无法连接本地服务，整盘摘要暂不可用。"
    : `整盘摘要加载失败${e?.status ? `（HTTP ${e.status}）` : ""}：${e?.message || "未知错误"}`;
  el.innerHTML =
    `<p class="conclusion-kicker" data-test="storage-kicker">整盘总览</p>
     <h2 class="conclusion-headline state-error" data-test="storage-state">整盘摘要不可用</h2>
     <p class="conclusion-sub" data-test="storage-sub">${escapeHtml(why)}
       下方仍是上次成功读取的目录结果，旧数据不被错误状态的大数字掩盖。</p>
     <div class="conclusion-actions"><button class="btn" data-test="storage-retry">重试</button></div>`;
}

async function loadStorageOverview() {
  const request = beginRequest("overviewStorage");
  const el = _ovSlot();
  if (!el) return;
  let summary;
  try {
    summary = await fetchJSON("/api/storage/summary");
  } catch (e) {
    if (!request.current()) return;
    _renderStorageError(e);
    el.querySelector('[data-test="storage-retry"]')
      ?.addEventListener("click", () => loadStorageOverview());
    return;
  }
  if (!request.current()) return;
  // legacy：未启用范围能力 → 不伪装整盘
  if (!_isWholeDisk(summary)) { _renderStorageLegacy(summary); return; }
  const rounds = summary.round && summary.previous_round ? 2
    : (summary.round ? 1 : 0);
  if (rounds < 2) { _renderStorageWaiting(rounds === 0 ? "empty" : "single"); return; }
  _renderStorageReady(summary);
}
