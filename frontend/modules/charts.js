/* 图表层：ECharts 实例的唯一注册表与可见性生命周期。
 *
 * 合同（ISS-027）：
 *  - 实例只在可见性恢复后量尺寸：隐藏容器（clientWidth=0）期间窗口变化
 *    只标记过期（stale），不在 0×0 上强行 resize；
 *  - 页面重新显示时由 router 调 resumeChartsIn(root) 恢复尺寸，
 *    保证“隐藏后再显示”图表画布与容器一致；
 *  - 在隐藏容器中初始化的图表同样标记过期，显示时补一次 resize。
 */

const charts = new Map();  // 容器 id -> { instance, stale }

function containerOf(id) { return document.getElementById(id); }
function hasSize(el) { return el.clientWidth > 0 && el.clientHeight > 0; }

export function initChart(id) {
  let entry = charts.get(id);
  if (!entry) {
    const el = containerOf(id);
    entry = { instance: echarts.init(el), stale: !hasSize(el) };
    charts.set(id, entry);
  }
  return entry.instance;
}

export function hasChart(id) { return charts.has(id); }

export function clearChart(id) { charts.get(id)?.instance.clear(); }

export function resetChart(id) {
  const entry = charts.get(id);
  if (entry) {
    entry.instance.dispose();
    charts.delete(id);
  }
  containerOf(id).replaceChildren();
}

export function showChartMessage(id, message) {
  resetChart(id);
  const p = document.createElement("p");
  p.className = "hint";
  p.textContent = message;
  containerOf(id).appendChild(p);
}

/* 页面重新显示后恢复其中图表尺寸（隐藏期间的变化只能先记过期）。 */
export function resumeChartsIn(rootEl) {
  for (const [id, entry] of charts) {
    if (!entry.stale) continue;
    const el = containerOf(id);
    if (el && rootEl.contains(el) && hasSize(el)) {
      entry.instance.resize();
      entry.stale = false;
    }
  }
}

window.addEventListener("resize", () => {
  for (const [id, entry] of charts) {
    const el = containerOf(id);
    if (!el) continue;
    if (hasSize(el)) entry.instance.resize();
    else entry.stale = true;  // 隐藏页的图表：重新显示时再恢复
  }
});
