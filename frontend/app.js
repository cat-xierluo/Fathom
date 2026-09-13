/* Fathom 前端入口（v0.3 模块化，见 docs/DESIGN.md 与 ISS-027）
 * hash 路由五页：#/overview #/changes #/browse #/bigfiles #/settings
 * 模块边界（frontend/modules/）：请求 request / 格式化 format / 图表 charts /
 * 轮询 polling / Tauri 桥 tauri / 页面控制 router / 状态观测 status。
 * 数据刷新只经 router 的单一刷新入口；Tauri 桥在浏览器中静默降级。
 */
import { icon } from "./icons.js";
import { setRequestScope } from "./modules/request.js";
import { state } from "./modules/state.js";
import { initPages, navigate } from "./modules/router.js";
import { initStatus, loadStatus, triggerScan } from "./modules/status.js";
import { listenTrayActions } from "./modules/tauri.js";

/* ---------- 静态图标注入（icons.js 提供 icon()，无 emoji —— DEC-010） ---------- */

function mountStaticIcons() {
  const brand = document.getElementById("brand-icon");
  if (brand) brand.innerHTML = icon("anchor", 26, "brand-anchor");
  document.querySelectorAll("[data-icon]").forEach((el) => {
    el.innerHTML = icon(el.dataset.icon, 18);
  });
  document.querySelectorAll("[data-icon-inline]").forEach((el) => {
    el.innerHTML = icon(el.dataset.iconInline, 14);
  });
}

/* ---------- 启动 ---------- */

(async function init() {
  try {
    setRequestScope(() => state.page);  // 世代号 pageScoped 的判定依据
    mountStaticIcons();
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
