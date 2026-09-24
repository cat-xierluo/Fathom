/* Fathom 前端入口（v0.3 模块化，见 docs/DESIGN.md 与 ISS-027）
 * hash 路由五页：#/overview #/changes #/browse #/bigfiles #/settings
 * 模块边界（frontend/modules/）：请求 request / 格式化 format / 图表 charts /
 * 轮询 polling / Tauri 桥 tauri / 页面控制 router / 状态观测 status。
 * 数据刷新只经 router 的单一刷新入口；Tauri 桥在浏览器中静默降级。
 */
import { icon, ICON_PATHS } from "./icons.js";
import { setRequestScope } from "./modules/request.js";
import { state } from "./modules/state.js";
import { initPages, navigate, PAGE_TITLES } from "./modules/router.js";
import { initStatus, loadStatus, triggerScan } from "./modules/status.js";
import { listenTrayActions } from "./modules/tauri.js";

/* ---------- 静态图标注入（icons.js 提供 icon()，无 emoji —— DEC-010） ---------- */

function mountStaticIcons() {
  const brand = document.getElementById("brand-icon");
  if (brand) brand.innerHTML = icon("brandBasin", 24, "brand-mark");
  document.querySelectorAll("[data-icon]").forEach((el) => {
    el.innerHTML = icon(el.dataset.icon, 18);
  });
  document.querySelectorAll("[data-icon-inline]").forEach((el) => {
    el.innerHTML = icon(el.dataset.iconInline, 14);
  });
}

/* ---------- ISS-085/086 装饰挂载（纯装饰，无业务逻辑） ----------
 * 1) [data-dr]：主窗口品牌站位（当前 #page-anchor = brandBasin，ISS-086
 *    修订；早期 ISS-085 用 brandRing，因形态选用判定错而替换）。几何与
 *    配色由 icons.js + style.css .brand-mark 承担；不再需要探针方位旋转
 *    （brandBasin 是层叠面，不是可旋转的探针）。
 * 2) [data-dr-spin]：区域加载占位的旋转加载环（几何同 brandRing，ISS-085
 *    加载指示合同保留 —— 加载是测深语义、属深度环小尺寸形态担当的场景，
 *    不进入双形态替换范围）。动画由 style.css .dr-loading 承担；文本由
 *    挂载点的既有 textContent/innerHTML 更新自然接管，环随内容替换消失。 */
function mountDepthRingDecor() {
  document.querySelectorAll("[data-dr]").forEach((el) => {
    /* ISS-088：关于区 about-mark 标注 data-dr-size=64（大尺寸应用图标
     * 位，64px 容器此前只注入 26px 图标、图标远小于容器）；未标注的
     * 挂载点（页头 #page-anchor）保持 26px 默认 */
    const size = Number(el.dataset.drSize) || 26;
    el.innerHTML = icon(el.dataset.dr, size, "brand-mark");
  });
  document.querySelectorAll("[data-dr-spin]").forEach((el) => {
    el.innerHTML =
      `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
      `stroke-width="2.4" stroke-linecap="butt">${ICON_PATHS.brandRing}</svg>`;
  });
}

/* ---------- ISS-094 带分段的 hash 规范化 ----------
 * 页内二级导航（tabs.js）把 tab 段写进 hash（#/changes/grown 形态，经
 * replaceState 不触发 hashchange）。刷新/直达这类 URL 时，router.js 的
 * navigate() 会把 "changes/grown" 当未知页回落 overview——本函数在
 * navigate() 之前把 hash 规范化回 #/changes，并把 tab 段挂到
 * window.__fathomTabPending 供对应页面的 tabs.js init 消费；navigate()
 * 之后再写回原分段形态，URL 与页面、tab 三者保持一致（刷新保持 tab）。
 * 只认 #/<page>/<tab> 形态（设置页 ISS-087 的 #settings/<section> 无
 * 斜杠前缀，不在此列，行为不变）。 */
function normalizeTabHash() {
  const m = /^#\/([a-z]+)\/([a-z]+)$/i.exec(location.hash || "");
  if (!m) return null;
  const page = m[1].toLowerCase();
  if (!(page in PAGE_TITLES)) return null;
  const pending = { page, tab: m[2].toLowerCase() };
  history.replaceState(null, "", `#/${page}`);
  window.__fathomTabPending = pending;
  return pending;
}

function restoreTabHash(pending) {
  if (!pending || state.page !== pending.page) {
    window.__fathomTabPending = null;  // 页面不符（防御）：丢弃挂起段
    return;
  }
  history.replaceState(null, "", `#/${pending.page}/${pending.tab}`);
}

/* ---------- 启动 ---------- */

(async function init() {
  try {
    setRequestScope(() => state.page);  // 世代号 pageScoped 的判定依据
    const pendingTab = normalizeTabHash();  // 必须先于 navigate()：分段 hash 会破坏页面解析
    mountStaticIcons();
    mountDepthRingDecor();
    initPages();      // 各页一次性事件接线（tabs.js 在此消费挂起段）
    initStatus();     // 顶栏扫描按钮 + tray 心跳
    navigate();
    restoreTabHash(pendingTab);  // navigate 之后写回分段，路由已完成、不再重解析
    await loadStatus();
    await listenTrayActions({ onScan: triggerScan });
  } catch (e) {
    console.error(e);
    document.getElementById("page-title").textContent = "加载失败：" + e.message;
  }
})();
