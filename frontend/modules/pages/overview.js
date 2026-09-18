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

/* 覆盖状态判定：collection_status 在 api.snapshots 里有值；fallback 到无
 * 返回结构含三类缺口计数（ISS-002A）：
 *   state: missing | partial | full
 *   denied:      权限受限位置数（denied_count，?? 0 防御缺失字段）
 *   vanished:    扫描期间消失位置数（vanished_count，字段 ISS-066 落地后才存在）
 *   excluded:    排除掩码项数（exclude_names 数组长度；缺字段 0）
 * 数字仅代表"未被采集的位置数"，不求和不推比例；详见 _explainClasses。 */
function _coverage(snapshot) {
  if (!snapshot) return { state: "missing", denied: 0, vanished: 0, excluded: 0 };
  const denied = snapshot.denied_count ?? 0;
  const vanished = snapshot.vanished_count ?? 0;
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
  return { state, denied, vanished, excluded };
}

/* 三类缺口的「意味着什么 / 不意味着什么」文案（ISS-002A）。
 * 每类固定句：means 是真实事实，doesn't-means 是反对误读。
 * 严禁出现"数量=影响大小"或"未记录=已删除"表述。 */
const COV_NOTES = {
  denied: {
    label: "权限受限",
    means: "这些目录本次未被系统授权读取；仅记录采集时被拒的位置。",
    doesnt: "数量不代表影响大小，也无法判断真实占用。",
  },
  vanished: {
    label: "扫描期间消失",
    means: "目录在 du 输出时是目录，扫描结束前被系统清理。",
    doesnt: "扫描期间的事实以读取时为准；未记录不构成删除证据。",
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
  const order = ["denied", "vanished", "excluded"];
  const counts = { denied: coverage.denied, vanished: coverage.vanished, excluded: coverage.excluded };
  const items = order
    .filter((k) => counts[k] > 0)
    .map((k) => {
      const note = COV_NOTES[k];
      const chipCls = k === "excluded" ? "quality-chip miss" : "quality-chip warn";
      const chipIcon = k === "excluded" ? "filter" : "alert";
      const unit = k === "excluded" ? "项" : "处";
      // ISS-002A 计数前置（N 处权限受限 / N 处扫描期间消失 / N 项排除掩码）：
      // 验收契约要求「计数在前、类目在后」，避免读成「类目 N」被误当影响大小。
      return `<li class="cov-class">
        <span class="cov-class-head">
          <span class="${chipCls}">${icon(chipIcon, 12)} ${counts[k]} ${unit}${escapeHtml(note.label)}</span>
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
  // 既有口径（ISS-028 m1 验收字符串）：保留「部分目录未读取 / N 个」字样，
  // 避免后续回归（state-matrix-partial-quality-shown 等既有检查依赖此句）。
  // ISS-002A 三类缺口的详细可解释文案移到独立 #overview-coverage-note 区块。
  const covChip = cov.state === "full"
    ? `<span class="quality-chip ok">${icon("alert", 12)} 覆盖完整</span>`
    : cov.state === "partial"
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
  // 既有口径（ISS-028 m3 验收字符串）：保留「读取受限 / N 个目录读取受限」
  // 字样，避免后续回归（state-matrix-partial-scan-note-shown 等既有检查
  // 依赖此句）。三类缺口详细解释放到独立区块 #overview-coverage-note，
  // 本节只在存在 denied 时追加一句紧凑提示（不替换既有文案）。
  if (cov.denied > 0) {
    parts.push(`<span class="st st-restricted">${icon("alert", 12)} ${cov.denied} 个目录读取受限</span>`);
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
 * [data-test='coverage-classes'] 复用同一 DOM 节点）。 */
function _ensureCoverageNote() {
  let container = document.getElementById("overview-coverage-note");
  if (container) return container;
  const scanNote = document.getElementById("overview-scan-note");
  if (!scanNote || !scanNote.parentNode) return null;
  container = document.createElement("div");
  container.id = "overview-coverage-note";
  container.className = "coverage-note-block";
  container.setAttribute("aria-live", "polite");
  // 追加在 #overview-scan-note 之后；同一 panel 内，仍属「最近扫描」区块。
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
          areaStyle: { opacity: 0.12 }, itemStyle: { color: "#345d7f" } },
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
