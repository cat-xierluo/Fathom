/* 变化页：快照对比（世代号防倒序覆盖 + 用户改选保护 + 404 自动协调）与历史日报。 */
import { fetchJSON, beginRequest, invalidateRequest, revealInFinder } from "../request.js";
import { fmtKB, fmtDelta, shortPath, escapeHtml } from "../format.js";
import { initChart, hasChart, clearChart } from "../charts.js";
import { icon } from "../../icons.js";

let snapshotSelectionRevision = 0;  // 用户改选计数：晚到的快照列表不得覆盖改选结果

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
  // 等待期间用户改过选择则尊重当前值，否则用请求前快照
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

async function loadDiff({ retryOnMissing = true, successMessage = "" } = {}) {
  const request = beginRequest("diff");
  const a = document.getElementById("sel-a").value;
  const b = document.getElementById("sel-b").value;
  if (!a || !b) return;
  setDiffStatus("正在加载快照对比…");
  try {
    const d = await fetchJSON(`/api/diff?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
    if (!request.current()) return;
    renderDeltaBars("chart-grown", d.grown, "#d64545");
    renderDeltaBars("chart-shrunk", d.shrunk, "#2e9e5b");
    fillTwoColTable("tbl-added", d.added, (r) => [r.path, fmtKB(r.new_kb)]);
    fillTwoColTable("tbl-removed", d.removed, (r) => [r.path, fmtKB(r.old_kb)]);
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
      // tooltip 以 HTML 解释：路径来自扫描数据，必须按文本转义（ISS-022）
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
    tbody.innerHTML = '<tr><td colspan="2" style="color:var(--muted)">无</td></tr>';
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
  },
  leave() {
    // 离开页面：作废本页在途请求，防止迟到的响应改写隐藏 DOM
    ["snapshots", "diff", "reportList", "reportContent"].forEach(invalidateRequest);
  },
};
