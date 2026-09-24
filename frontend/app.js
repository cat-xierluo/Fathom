/* Fathom 前端入口（v0.3 模块化，见 docs/DESIGN.md 与 ISS-027）
 * hash 路由五页：#/overview #/changes #/browse #/bigfiles #/settings
 * 模块边界（frontend/modules/）：请求 request / 格式化 format / 图表 charts /
 * 轮询 polling / Tauri 桥 tauri / 页面控制 router / 状态观测 status。
 * 数据刷新只经 router 的单一刷新入口；Tauri 桥在浏览器中静默降级。
 */
import { icon, ICON_PATHS } from "./icons.js";
import { setRequestScope } from "./modules/request.js";
import { state } from "./modules/state.js";
import { initPages, navigate } from "./modules/router.js";
import { initStatus, loadStatus, triggerScan } from "./modules/status.js";
import { listenTrayActions } from "./modules/tauri.js";

/* ---------- 静态图标注入（icons.js 提供 icon()，无 emoji —— DEC-010） ---------- */

function mountStaticIcons() {
  const brand = document.getElementById("brand-icon");
  if (brand) brand.innerHTML = icon("brandRing", 24, "brand-mark");
  document.querySelectorAll("[data-icon]").forEach((el) => {
    el.innerHTML = icon(el.dataset.icon, 18);
  });
  document.querySelectorAll("[data-icon-inline]").forEach((el) => {
    el.innerHTML = icon(el.dataset.iconInline, 14);
  });
}

/* ---------- ISS-085 深度环装饰挂载（纯装饰，无业务逻辑） ----------
 * 1) [data-dr]：深度环锚点（当前是页头 #page-anchor），brandRing 几何 +
 *    brand-mark 配色（海沟蓝环 + 矿物青探针，与侧栏字标同一规则）；
 * 2) [data-dr-spin]：区域加载占位的旋转加载环（几何同 brandRing，配色与
 *    动画由 style.css .dr-loading 承担；文本由挂载点的既有 textContent/
 *    innerHTML 更新自然接管，环随内容替换消失）。 */
function mountDepthRingDecor() {
  document.querySelectorAll("[data-dr]").forEach((el) => {
    el.innerHTML = icon(el.dataset.dr, 26, "brand-mark");
  });
  document.querySelectorAll("[data-dr-spin]").forEach((el) => {
    el.innerHTML =
      `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
      `stroke-width="2.4" stroke-linecap="butt">${ICON_PATHS.brandRing}</svg>`;
  });
}

/* 页头深度环探针随路由旋转（ISS-085）：五个页面均分环面（72° 间隔），
 * 「探针指向当前页」的仪器方位指示。独立解析 hash（与 router 同规则），
 * 不依赖 router 内部状态；reduced-motion 时 CSS 已关闭过渡，仅跳变方位。 */
const PAGE_ANCHOR_ANGLES = {
  overview: 0, changes: 72, browse: 144, bigfiles: 216, settings: 288,
};

function pointAnchorToCurrentPage() {
  const anchor = document.getElementById("page-anchor");
  if (!anchor) return;
  const page = (location.hash || "#/overview").slice(2);
  const angle = PAGE_ANCHOR_ANGLES[page] ?? 0;
  anchor.style.setProperty("--dr-angle", `${angle}deg`);
}

/* ---------- 启动 ---------- */

(async function init() {
  try {
    setRequestScope(() => state.page);  // 世代号 pageScoped 的判定依据
    mountStaticIcons();
    mountDepthRingDecor();
    pointAnchorToCurrentPage();
    window.addEventListener("hashchange", pointAnchorToCurrentPage);
    initPages();      // 各页一次性事件接线
    initStatus();     // 顶栏扫描按钮 + tray 心跳
    navigate();
    await loadStatus();
    await listenTrayActions({ onScan: triggerScan });
  } catch (e) {
    console.error(e);
    document.getElementById("page-title").textContent = "加载失败：" + e.message;
  }
})();
