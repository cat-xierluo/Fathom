/* 页面控制层：hash 路由、页面可见性切换与单一刷新入口（ISS-027）。
 *
 * 合同：页面数据加载只经 refreshActivePage() 发起——导航、扫描结束刷新、
 * 后续任何重试共用这一个入口，不各自拼装加载列表；进入页面前先显示容器，
 * 再恢复图表尺寸（隐藏容器量不到宽度），最后加载数据；离开页面调用该页
 * leave() 作废在途请求域（世代号 pageScoped 之上的双保险）。
 */
import { state } from "./state.js";
import { resumeChartsIn } from "./charts.js";
import { overviewPage } from "./pages/overview.js";
import { changesPage } from "./pages/changes.js";
import { browsePage } from "./pages/browse.js";
import { bigfilesPage } from "./pages/bigfiles.js";
import { settingsPage } from "./pages/settings.js";

export const PAGE_TITLES = {
  overview: "总览", changes: "变化", browse: "分布",
  bigfiles: "大文件", settings: "设置",
};

const PAGES = {
  overview: overviewPage,
  changes: changesPage,
  browse: browsePage,
  bigfiles: bigfilesPage,
  settings: settingsPage,
};

/* 单一刷新入口：只加载当前激活页的数据。 */
export function refreshActivePage() {
  const page = PAGES[state.page];
  if (!page?.load) return;
  Promise.resolve(page.load()).catch((e) =>
    console.warn(`${PAGE_TITLES[state.page]}刷新失败：`, e));
}

export function navigate() {
  const previous = state.page;
  const hash = (location.hash || "#/overview").slice(2);
  state.page = PAGE_TITLES[hash] ? hash : "overview";
  if (previous !== state.page) PAGES[previous]?.leave?.();

  document.querySelectorAll(".page").forEach((p) => p.classList.add("hidden"));
  const el = document.getElementById("page-" + state.page);
  el.classList.remove("hidden");
  document.querySelectorAll(".nav-item").forEach((a) => {
    a.toggleAttribute("aria-current", a.dataset.page === state.page);
  });
  document.getElementById("page-title").textContent = PAGE_TITLES[state.page];

  PAGES[state.page]?.enter?.();
  resumeChartsIn(el);  // 图表在隐藏容器中拿不到宽度：显示后先恢复尺寸
  refreshActivePage();
}

export function initPages() {
  Object.values(PAGES).forEach((p) => p.init?.());  // 各页一次性事件接线
}

window.addEventListener("hashchange", navigate);
