/* 页内二级导航（ISS-094）：内容页（变化/分布）页头下的横向 tab 条。
 *
 * 与设置页（ISS-087 左导航 + 右 section）是同一交互语言的两种形态——
 * 设置页承载的是并列的功能分区（左导航更稳），内容页承载的是同一数据集
 * 的不同视图（横向 tab 条更贴近「翻页」心智，不占纵向宽度）。
 *
 * 合同（ISS-094）：
 * - 分区切换不改数据加载逻辑：各分区仍由页面 load() 一次渲染，
 *   tab 只切可见性；隐藏分区里初始化的图表量不到尺寸（charts.js 记
 *   stale），激活分区时调 resumeChartsIn(section) 补一次 resize；
 * - 键盘可达（与设置页 nav 同模式）：Tab 进出 tab 条，方向键 ←/→ 与
 *   Home/End 在按钮间移动焦点（只移焦点不激活），Enter/Space 激活；
 * - aria-current="true" 标选中（与侧栏/设置导航同 token：深潭墨实底 +
 *   象牙白字，ISS-092 选中语言）；
 * - hash 持久化：`#/changes/grown` 形态经 history.replaceState 写入——
 *   不触发 hashchange，router.js 的页面级路由不感知 tab 段；刷新时由
 *   app.js 在 navigate() 前把带 tab 段的 hash 规范化回 `#/changes` 并把
 *   tab 段挂到 window.__fathomTabPending，本组件 init 时消费；
 * - 回默认 tab 时 hash 回到无 tab 段形态（hash 是 tab 状态的持久真源：
 *   从侧栏进入页面 = 默认 tab，页面内切换 = 写段，刷新带段 = 恢复）。
 */

import { resumeChartsIn } from "./charts.js";

/* app.js 在 navigate() 前挂起的 tab 段（{ page, tab }）；init 时取走。
 * 经 window 传递而不是模块 import：app.js 是入口模块，页面模块反向
 * import 它会形成循环依赖（app → router → pages → app）。 */
function consumePendingTab(page) {
  const pending = window.__fathomTabPending;
  if (pending && pending.page === page) {
    window.__fathomTabPending = null;
    return pending.tab;
  }
  return null;
}

/**
 * 初始化一个页面的 tab 条。DOM 结构由 index.html 静态提供：
 *   <nav class="page-tabs" aria-label="…分区导航">
 *     <button class="page-tab" data-tab="…">…</button>…
 *   </nav>
 *   <section class="page-tab-section" data-tab="…">…</section>…
 * 按钮与分区按 data-tab 一一对应（:scope 限定在本页内，防嵌套误取）。
 *
 * @param {object} options
 * @param {string} options.page   页面 id（"changes" / "browse"）
 * @param {string} options.defaultTab 默认 tab id；hash 无段/未知段时回落
 * @returns {{ activate(tab: string, opts?: {persistHash?: boolean}): void,
 *             applyHash(): void, active: string } | null}
 *   activate：程序化切换（如分布页旭日图点扇区联动切到目录浏览器）；
 *   applyHash：页面每次 load()（进入页面）时调用，按当前 hash 恢复 tab
 *   （无段 = 默认）；首次调用在 init 之后立即发生（navigate→load 链），
 *   由组件内部跳过，避免覆盖 init 已消费的挂起段。
 */
export function initPageTabs({ page, defaultTab }) {
  const root = document.getElementById("page-" + page);
  if (!root) return null;
  const buttons = [...root.querySelectorAll(":scope .page-tabs .page-tab")];
  const sections = [...root.querySelectorAll(":scope > .page-tab-section")];
  if (!buttons.length || buttons.length !== sections.length) return null;
  const ids = buttons.map((b) => b.dataset.tab);
  if (!ids.includes(defaultTab)) return null;

  let active = null;
  let skipNextApply = true;  // init 激活后的首次 applyHash（load 链）跳过

  const readHashTab = () => {
    const m = new RegExp(`^#/${page}/([a-z]+)$`, "i").exec(location.hash || "");
    if (!m) return null;
    const tab = m[1].toLowerCase();
    return ids.includes(tab) ? tab : null;
  };

  const writeHash = (tab) => {
    // replaceState（而非 location.hash=…）：避免触发 hashchange 让
    // router.js 把 `#/changes/grown` 解析成未知页回落 overview。
    const next = tab === defaultTab ? `#/${page}` : `#/${page}/${tab}`;
    if ((location.hash || "") === next) return;
    history.replaceState(null, "", next);
  };

  const activate = (tab, { persistHash = false } = {}) => {
    if (!ids.includes(tab)) tab = defaultTab;
    active = tab;
    for (const btn of buttons) {
      if (btn.dataset.tab === tab) btn.setAttribute("aria-current", "true");
      else btn.removeAttribute("aria-current");
    }
    for (const sec of sections) {
      if (sec.dataset.tab === tab) sec.removeAttribute("hidden");
      else sec.setAttribute("hidden", "");
    }
    const section = sections.find((s) => s.dataset.tab === tab);
    if (section) resumeChartsIn(section);  // 隐藏分区里初始化的图表恢复尺寸
    if (persistHash) writeHash(tab);
  };

  /* 方向键 ←/→ 在 tab 间循环移动焦点（不激活——与设置页 nav 同模式：
   * 焦点浏览与激活分离，Enter/Space 才切换）；Home/End 跳首尾。
   * 不拦截 Tab（焦点序）与 PageUp/PageDown（页面级滚动）。 */
  const handleKeydown = (event) => {
    let nextIndex = null;
    if (event.key === "ArrowRight") {
      nextIndex = (buttons.findIndex((b) => b === document.activeElement) + 1) % buttons.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (buttons.findIndex((b) => b === document.activeElement) - 1 + buttons.length) % buttons.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = buttons.length - 1;
    }
    if (nextIndex !== null) {
      event.preventDefault();
      buttons[nextIndex]?.focus();
    }
  };

  for (const btn of buttons) {
    btn.addEventListener("click", () => activate(btn.dataset.tab, { persistHash: true }));
    btn.addEventListener("keydown", (event) => {
      if (event.key === " ") {
        // 与设置页 nav 同处理：统一拦截 Space 由 click 激活，
        // Enter 由 button 原生 click 兜底。
        event.preventDefault();
        btn.click();
        return;
      }
      handleKeydown(event);
    });
  }

  // 初始 tab：挂起段（刷新/直达 URL 的 tab 段，app.js 提取）> hash 段 > 默认
  activate(consumePendingTab(page) || readHashTab() || defaultTab);

  return {
    activate,
    applyHash() {
      if (skipNextApply) { skipNextApply = false; return; }
      activate(readHashTab() || defaultTab);
    },
    get active() { return active; },
  };
}
