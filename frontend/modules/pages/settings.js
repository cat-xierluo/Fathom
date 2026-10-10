/* 设置页：真实配置读取/编辑（ISS-016A）+ 扫描运行历史。
 *
 * 前端责任（ISS-027 模块合同）：
 * - beginRequest 世代号 + pageScoped：迟到的旧响应不能覆盖较新查询；
 * - 所有展示值来自 /api/config 与 /api/status，不硬编码路径/端口/阈值；
 * - 仅 frontend/icons.js 的 SVG 图标；零 emoji。
 *
 * 编辑合同（ISS-016A）：
 * - 保存走 PUT /api/config（写令牌）；校验以服务端为准，无效输入 400 时
 *   页面显示原因，当前生效值保持旧值可辨（DESIGN：保存失败必须保持旧值可辨）；
 * - 服务不注册/不重载 launchd：保存成功也如实显示“需重新安装计划才生效”；
 * - 换根形成新数据集（不删旧数据），被环境变量/命令行覆盖的字段如实标注。
 *
 * 计划一致性（ISS-016B）：消费 service_reload_state 四态（见下方 reloadStateInfo）；
 * 服务自身零系统写入口——drift 态的重装只经 010B autostart 确认层执行。
 *
 * ISS-156：「范围与覆盖」作为「监控」分区内的独立面板承载（左导航保持 ISS-087
 * 六分区不变，既有 IA 合同与 217 项前端回归依赖该结构）。
 * 信息架构（ISS-087）：左导航 + 右 section 的二分区布局——
 *   监控 / 计划与通知 / 权限 / 高级与诊断 / 关于 五个 section 互斥可见；
 *   ISS-083 折叠区原封迁入「高级与诊断」，ISS-040B 检查更新区迁入「关于」，
 *   ISS-002A 权限与覆盖、ISS-010B 后台自启、ISS-028 m3 扫描运行历史归入「计划与通知」；
 *   ISS-111「权限」分区：各权限状态可见 + 一键深链授权（091 监控卡数据迁入）。
 *   默认 section = 监控；URL hash 可选持久化（`#settings/about` 等）。
 *   浏览器/开发态无 Tauri 桥时全部 section 仍可达（不删除/隐藏入口）。
 */
import { fetchJSON, beginRequest, invalidateRequest, apiPut, apiPost } from "../request.js";
import { escapeHtml, fmtBytes, fmtKB } from "../format.js";
import { icon } from "../../icons.js";
import { triggerScan, loadStatus } from "../status.js";

let lastConfig = null;  // 最近一次生效配置（页面内存；保存/恢复默认的对照源）
let lastStatusAppVersion = null;  // 最近一次 /api/status 的 app_version（ISS-111 版本回填源）

/* ---------- ISS-087：左导航 + 右 section 切换 ----------
 * 设计参照 Folia/Fomo 设置页：左侧导航项 + 右侧动态 section；
 * 本实现为 vanilla JS，无路由库，section state 由 settingsNav.active 控制。
 * - 默认 section = monitoring（ISS-087 合同）；URL hash 可选 `#settings/about` 持久化
 *   （与现有 #/overview 等路由解耦：本卡不引入新路由，仅页面内 hash 分段）
 * - 浏览器/开发态（无 Tauri 桥）保持所有 section 可达（不删除/隐藏入口）
 * - 键盘可达：Tab 进入 nav 项，方向键 ↑/↓ 切换，Enter/Space 激活；
 *   原生 button 元素承担焦点与键盘行为，role/aria 由 markup 静态提供
 */
/* ISS-111：权限分区排在「计划与通知」之后、「高级与诊断」之前——
 * 监控/计划与通知/权限同属用户级日常设置（授权操作是用户裁决的主入口），
 * 高级与诊断（排障）与关于（元信息）靠后。
 * ISS-035C：「AI 分析」分区随用户级组（权限之后、高级与诊断之前）——
 * 变化页 AI 解读的引擎检测/选择/授权都在这里完成。 */
const SETTINGS_SECTIONS = ["monitoring", "schedule", "permissions", "analysis", "advanced", "about"];
const SETTINGS_SECTION_LABELS = {
  monitoring: "监控",
  schedule: "计划与通知",
  permissions: "权限",
  analysis: "AI 分析",
  advanced: "高级与诊断",
  about: "关于",
};
const settingsNav = {
  active: "monitoring",
  /* 跨调用缓存 nav 按钮引用，避免每点一次都 querySelectorAll */
  buttons: null,
  sections: null,
};

function _readHashSection() {
  // `#settings/about` / `#settings/advanced` 持久化当前 section；
  // 兼容不带 section 段（视为 monitoring 默认）。
  const m = /#settings\/([a-z]+)/i.exec(location.hash || "");
  if (!m) return null;
  const sec = m[1].toLowerCase();
  return SETTINGS_SECTIONS.includes(sec) ? sec : null;
}

function _writeHashSection(section) {
  if (!SETTINGS_SECTIONS.includes(section)) return;
  // 用 history.replaceState 而非 location.hash = …——避免触发 window 的
  // hashchange 事件，让 router.js 的 navigate() 误以为页面切换并把
  // state.page 改成 "settings/advanced" 之类的无效键。视觉上 URL 仍落到
  // `#settings/<section>`；离开设置页后 router 自己用 #/<page> 形态覆盖。
  const cur = location.hash || "";
  const next = `#settings/${section}`;
  if (cur === next) return;
  history.replaceState(null, "", next);
}

function _cacheSettingsNavRefs() {
  const page = document.getElementById("page-settings");
  if (!page) return false;
  settingsNav.buttons = [...page.querySelectorAll(".settings-nav-item")];
  settingsNav.sections = [...page.querySelectorAll(".settings-section")];
  return settingsNav.buttons.length === SETTINGS_SECTIONS.length;
}

function activateSettingsSection(section, { persistHash = false } = {}) {
  if (!SETTINGS_SECTIONS.includes(section)) section = "monitoring";
  settingsNav.active = section;
  if (settingsNav.buttons) {
    for (const btn of settingsNav.buttons) {
      const on = btn.dataset.section === section;
      // setAttribute("aria-current", "true"/"false") 比 toggleAttribute(name, bool)
      // 更利于 CSS 选择器与测试断言：属性值是字面字符串，不是布尔存在性。
      if (on) btn.setAttribute("aria-current", "true");
      else btn.removeAttribute("aria-current");
    }
  }
  if (settingsNav.sections) {
    for (const sec of settingsNav.sections) {
      const on = sec.dataset.section === section;
      if (on) sec.removeAttribute("hidden");
      else sec.setAttribute("hidden", "");
    }
  }
  if (persistHash) _writeHashSection(section);
}

/* 方向键 ↑/↓ 在 nav 项之间循环切换；Home/End 跳到首尾。
 * 不阻止 PageUp/PageDown（保留页面级滚动）；不拦截 Tab（焦点序）。 */
function _handleSettingsNavKeydown(event) {
  if (!settingsNav.buttons || !settingsNav.buttons.length) return;
  const key = event.key;
  let nextIndex = null;
  if (key === "ArrowDown" || key === "ArrowRight") {
    nextIndex = settingsNav.buttons.findIndex((b) => b === document.activeElement) + 1;
    if (nextIndex >= settingsNav.buttons.length) nextIndex = 0;
  } else if (key === "ArrowUp" || key === "ArrowLeft") {
    nextIndex = settingsNav.buttons.findIndex((b) => b === document.activeElement) - 1;
    if (nextIndex < 0) nextIndex = settingsNav.buttons.length - 1;
  } else if (key === "Home") {
    nextIndex = 0;
  } else if (key === "End") {
    nextIndex = settingsNav.buttons.length - 1;
  }
  if (nextIndex !== null) {
    event.preventDefault();
    const target = settingsNav.buttons[nextIndex];
    target?.focus();
  }
}

function initSettingsNav() {
  if (!_cacheSettingsNavRefs()) return;
  for (const btn of settingsNav.buttons) {
    btn.addEventListener("click", () => {
      const sec = btn.dataset.section || "monitoring";
      activateSettingsSection(sec, { persistHash: true });
    });
    // Enter/Space 在 button 上原生触发 click；但部分浏览器对 Space 的 keydown
    // 处理与 Enter 不同（Enter 不触发 keydown Space press），这里统一拦截
    // Space 以保证两种键都能激活 section（Enter 已由 button 原生 click 兜底）。
    btn.addEventListener("keydown", (event) => {
      if (event.key === " ") {
        event.preventDefault();
        btn.click();
        return;
      }
      _handleSettingsNavKeydown(event);
    });
  }
  // URL hash 优先；空 hash 视为 monitoring 默认。
  // ISS-035C：跨页深链挂起段（window.__fathomSettingsSectionPending）——
  // 变化页「前往设置开启」按钮置段后跳 #/settings；hash 形态 `#settings/x`
  // 会触发 hashchange 且 router 解析不出页面，故跨页跳转走 pending 而非
  // hash（与 tabs.js 的 __fathomTabPending 同一模式）。
  const pendingSection = window.__fathomSettingsSectionPending;
  window.__fathomSettingsSectionPending = null;
  const fromHash = _readHashSection();
  const initial = pendingSection && SETTINGS_SECTIONS.includes(pendingSection)
    ? pendingSection : (fromHash || "monitoring");
  activateSettingsSection(initial, { persistHash: Boolean(pendingSection) });
}

/* 深链目标：macOS 系统设置 → 隐私与安全性 → 完全磁盘访问。
 * Tauri 端经 opener 插件跳转；浏览器环境静默降级为显示路径文字，
 * 不渲染假<a href>（前端不能伪造可点击的本地 URL）。 */
const PREFS_DEEP_LINK = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles";
const PREFS_PATH_TEXT = "系统设置 › 隐私与安全性 › 完全磁盘访问";

const SOURCE_LABELS = {
  env: "该字段被环境变量 FATHOM_SCAN_ROOT 覆盖，保存不会改变当前生效值",
  cli: "该字段被启动参数覆盖，保存不会改变当前生效值",
};

/* ISS-069：exclude_names 的覆盖来源文案（与 SOURCE_LABELS 同口径，但指名
 * 真实的环境变量 FATHOM_EXCLUDE_NAMES——后端 sources.exclude_names 只会是
 * env / settings / default，不存在 cli 分支）。 */
const EXCLUDE_SOURCE_OVERRIDE = {
  env: "扫描排除列表被环境变量 FATHOM_EXCLUDE_NAMES 覆盖，此处保存不会改变当前生效值",
};

/* ISS-108（ISS-104 候选 #8）：当前值预填输入框——底部不再用长摘要罗列全部
 * 生效值，用户改哪个字段就在哪个输入框里看到当前值。#config-effective
 * 只承载加载失败/异常态（正常态保持隐藏）。env/cli 覆盖改为字段级就近标注
 * （原摘要行的 override 提示语义原样保留，位置移到被覆盖字段下方）。 */
function renderEffective(cfg) {
  if (!cfg) return;
  const fill = (id, value) => {
    const input = document.getElementById(id);
    if (input) input.value = value == null ? "" : String(value);
  };
  fill("cfg-scan-root", cfg.scan_root);
  fill("cfg-min-kb", cfg.min_kb);
  fill("cfg-scan-time", cfg.scan_time);
  fill("cfg-free-alert-gb", cfg.free_alert_gb);
  const override = SOURCE_LABELS[cfg.sources?.scan_root];
  const note = document.querySelector('[data-test="cfg-scan-root-override"]');
  if (note) {
    note.textContent = override || "";
    note.hidden = !override;
  }
  // 入库阈值可展开说明里的等效读数（5120 KB → 5.0 MB）：帮助不用 KB 思考的用户
  const equiv = document.querySelector('[data-test="cfg-min-kb-equiv"]');
  if (equiv) {
    const kb = Number(cfg.min_kb);
    equiv.textContent = Number.isFinite(kb) && kb > 0
      ? `当前值 ${cfg.min_kb} KB 折合约 ${fmtKB(kb)}。`
      : "";
  }
  const effective = document.getElementById("config-effective");
  if (effective) effective.hidden = true;
}

/* ---------- ISS-069 排除列表编辑器 ----------
 * 消费 GET /api/config 的 exclude_names（列表或规范串，两端皆可），
 * 保存只走既有 PUT /api/config。排除集变化会形成新数据集（旧数据不删），
 * 故保存前必须显式勾选确认；非法输入（`/`、`.`、`..`、空项、重复）在
 * 前端即时反馈，最终仍以服务端 400 为准。 */
const EXCLUDE_PANEL_ID = "exclude-panel";
const MAX_EXCLUDE_NAMES = 50;  // 与 fathom/config.py 一致（防御性上限）

/** GET /api/config 的 exclude_names 既可能是列表也可能是规范串，统一成数组。 */
function excludeMasksFromConfig(raw) {
  if (Array.isArray(raw)) return raw.filter((s) => typeof s === "string" && s.trim());
  if (typeof raw === "string" && raw.trim()) return raw.split(";").filter((s) => s.trim());
  return [];
}

/** 单项即时校验：返回错误文案，合法则返回 ""。与后端合同同序同口径。 */
function validateExcludeMask(mask) {
  const value = String(mask ?? "").trim();
  if (!value) return "排除掩码不能为空";
  if (value.includes("/")) return "排除掩码不得包含路径分隔符 “/”（只按名字匹配）";
  if (value === "." || value === "..") return `排除掩码不得为 “${value}”`;
  return "";
}

function _ensureExcludePanel() {
  const page = document.getElementById("page-settings");
  if (!page) return null;
  let panel = document.getElementById(EXCLUDE_PANEL_ID);
  if (panel) return panel;
  panel = document.createElement("div");
  panel.id = EXCLUDE_PANEL_ID;
  panel.className = "panel";
  panel.innerHTML = `
    <div class="panel-head">
      <h2>扫描排除列表</h2>
      <p class="hint">按名字跳过整棵子树，例如 <code>node_modules</code>；不含路径分隔符。</p>
    </div>
    <div class="exclude-panel" data-test="exclude-panel-body">
      <ul class="exclude-list" id="exclude-list"></ul>
      <div class="exclude-add">
        <input class="ctl-input cfg-wide" id="exclude-new-input" type="text"
               placeholder="新增掩码，例如 node_modules" autocomplete="off">
        <button type="button" id="btn-exclude-add" class="btn">${icon("plus")}新增</button>
      </div>
      <p class="hint cfg-error" id="exclude-inline-error" hidden></p>
      <p class="hint exclude-override" id="exclude-override-note" data-test="exclude-override-note" hidden></p>
      <p class="hint exclude-warning" data-test="exclude-dataset-warning">修改排除列表会形成新的数据集用于后续扫描；旧数据不会被删除，但新数据集与既有历史不可直接对比。</p>
      <details class="cfg-details" data-test="exclude-rules-details">
        <summary>支持通配符写法吗？</summary>
        <p class="cfg-desc">匹配规则与产品其余部分一致（fnmatch 通配）：<code>*</code> 匹配任意字符、<code>?</code> 匹配单个字符，
          例如 <code>*.noindex</code> 会跳过所有以 .noindex 结尾的名字。掩码只按名字匹配，不含路径分隔符。</p>
      </details>
      <label class="exclude-confirm-label">
        <input type="checkbox" id="exclude-confirm">
        <span>我已知晓：保存后按新数据集扫描，历史对比可能中断</span>
      </label>
      <div class="exclude-actions">
        <!-- ISS-105（用户裁决 + ISS-104 候选）：排除列表为低频高危操作，
             保存降为次级描边，不与「保存设置」争夺每视口唯一实心主按钮 -->
        <button type="button" id="btn-exclude-save" class="btn">${icon("filter")}保存排除列表</button>
      </div>
    </div>`;
  // ISS-087：排除列表编辑器归「监控」section，挂在 #settings-monitor-extra 容器下；
  // 该容器随监控 section 显示/隐藏（详见 mountPanelsForSections）。
  const host = document.getElementById("settings-monitor-extra");
  if (host) host.appendChild(panel);
  else page.appendChild(panel);
  document.getElementById("btn-exclude-add")?.addEventListener("click", addExcludeMask);
  document.getElementById("exclude-new-input")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); addExcludeMask(); }
  });
  document.getElementById("btn-exclude-save")?.addEventListener("click", saveExcludes);
  return panel;
}

/** 当前编辑态掩码（DOM 为单一真源，保存时据此组装 PUT body）。 */
function currentExcludeMasks() {
  return [...document.querySelectorAll(`#${EXCLUDE_PANEL_ID} [data-test='exclude-row']`)]
    .map((row) => row.getAttribute("data-mask"));
}

function setExcludeInlineError(text) {
  const node = document.getElementById("exclude-inline-error");
  if (!node) return;
  node.textContent = text || "";
  node.hidden = !text;
}

/* ISS-069：生效值被环境变量钉住时，编辑器必须整体只读。后端 sources 为 env
 * 时 PUT 虽能落盘 settings.json，但生效值仍取环境变量（实测：PUT 后 sources
 * 仍为 env、生效值不变），若继续允许编辑并显示“已保存（N 项）”，用户会以为
 * 改动生效、实际被静默丢弃。这里如实标注并锁定输入。 */
function excludeOverriddenByEnv(cfg) {
  return cfg?.sources?.exclude_names === "env";
}

function applyExcludeOverrideLock(cfg) {
  const overridden = excludeOverriddenByEnv(cfg);
  const note = document.getElementById("exclude-override-note");
  if (note) {
    note.textContent = overridden ? EXCLUDE_SOURCE_OVERRIDE.env : "";
    note.hidden = !overridden;
  }
  for (const id of ["exclude-new-input", "btn-exclude-add", "btn-exclude-save", "exclude-confirm"]) {
    const node = document.getElementById(id);
    if (node) node.disabled = overridden;
  }
  const panel = document.getElementById(EXCLUDE_PANEL_ID);
  if (panel) panel.classList.toggle("exclude-readonly", overridden);
  return overridden;
}

function renderExcludeEditor(cfg) {
  const panel = _ensureExcludePanel();
  if (!panel) return;
  const list = document.getElementById("exclude-list");
  if (!list) return;
  const masks = excludeMasksFromConfig(cfg?.exclude_names);
  list.innerHTML = masks.length
    ? masks.map((mask) => `
      <li class="exclude-row" data-test="exclude-row" data-mask="${escapeHtml(mask)}">
        <code>${escapeHtml(mask)}</code>
        <button type="button" class="btn-mini" data-test="exclude-remove"
                aria-label="删除 ${escapeHtml(mask)}">${icon("trash")}</button>
      </li>`).join("")
    : `<li class="hint exclude-empty">当前无排除掩码（扫描全部目录）。</li>`;
  const overridden = applyExcludeOverrideLock(cfg);
  for (const button of list.querySelectorAll("[data-test='exclude-remove']")) {
    button.disabled = overridden;
    button.addEventListener("click", () => {
      if (excludeOverriddenByEnv(lastConfig)) return;  // 只读锁定：UI 禁用外的第二道防线
      button.closest("[data-test='exclude-row']")?.remove();
      setExcludeInlineError("");
      if (!list.querySelector("[data-test='exclude-row']")) {
        list.innerHTML = `<li class="hint exclude-empty">当前无排除掩码（扫描全部目录）。</li>`;
      }
    });
  }
  setExcludeInlineError("");
}

function addExcludeMask() {
  if (excludeOverriddenByEnv(lastConfig)) {
    setExcludeInlineError(EXCLUDE_SOURCE_OVERRIDE.env);
    return;
  }
  const input = document.getElementById("exclude-new-input");
  if (!input) return;
  const value = input.value.trim();
  const error = validateExcludeMask(value);
  if (error) { setExcludeInlineError(error); return; }
  const masks = currentExcludeMasks();
  if (masks.includes(value)) { setExcludeInlineError(`“${value}” 已在列表中`); return; }
  if (masks.length >= MAX_EXCLUDE_NAMES) {
    setExcludeInlineError(`排除掩码项数不得超过 ${MAX_EXCLUDE_NAMES} 项`);
    return;
  }
  const list = document.getElementById("exclude-list");
  const empty = list.querySelector(".exclude-empty");
  if (empty) empty.remove();
  const row = document.createElement("li");
  row.className = "exclude-row";
  row.setAttribute("data-test", "exclude-row");
  row.setAttribute("data-mask", value);
  row.innerHTML =
    `<code>${escapeHtml(value)}</code>` +
    `<button type="button" class="btn-mini" data-test="exclude-remove" aria-label="删除 ${escapeHtml(value)}">${icon("trash")}</button>`;
  row.querySelector("[data-test='exclude-remove']").addEventListener("click", () => {
    row.remove();
    setExcludeInlineError("");
    if (!list.querySelector("[data-test='exclude-row']")) {
      list.innerHTML = `<li class="hint exclude-empty">当前无排除掩码（扫描全部目录）。</li>`;
    }
  });
  list.appendChild(row);
  input.value = "";
  setExcludeInlineError("");
}

async function saveExcludes() {
  // 保存前显式确认：区别于其余字段的就地保存，排除集变化会形成新数据集。
  if (!document.getElementById("exclude-confirm")?.checked) {
    showFeedback("请先勾选确认：修改排除列表会形成新数据集用于后续扫描。", "error");
    return;
  }
  const masks = currentExcludeMasks();
  for (const mask of masks) {
    const error = validateExcludeMask(mask);
    if (error) { setExcludeInlineError(error); showFeedback(error, "error"); return; }
  }
  // 上限必须在保存路径上再校一次：addExcludeMask 的守卫只覆盖“新增”这一条
  // 路径，删除/重渲染/后续批量入口都可能造出超限列表。不在此处拦截就会把
  // 必然 400 的请求发出去，用户只能从服务端错误里倒推原因。
  if (masks.length > MAX_EXCLUDE_NAMES) {
    const error = `排除掩码项数不得超过 ${MAX_EXCLUDE_NAMES} 项（当前 ${masks.length} 项）`;
    setExcludeInlineError(error);
    showFeedback(error, "error");
    return;
  }
  try {
    const res = await apiPut("/api/config", { exclude_names: masks });
    const data = await res.json();
    // 一次确认只对一次保存有效：保存成功后复位勾选框，避免下一次增删
    // 「沿用」旧勾选绕过显式确认。保存失败不复位，便于用户直接重试。
    const confirmBox = document.getElementById("exclude-confirm");
    if (confirmBox) confirmBox.checked = false;
    showFeedback(
      `已保存排除列表（${masks.length} 项）。${data.hint || "需重新安装计划才生效。"}`, "ok");
    // 保存成功后以服务端返回值重新渲染；等同重新拉取一次生效值。
    try {
      const fresh = await fetchJSON("/api/config");
      lastConfig = fresh;
      renderEffective(fresh);
      renderExcludeEditor(fresh);
      renderReloadSection();
    } catch { /* 刷新失败不覆盖已成功的保存反馈 */ }
  } catch (e) {
    showFeedback(
      e.status === 0
        ? "保存失败：无法连接本地服务，当前生效值保持不变。"
        : `保存失败：${e.message} 当前生效值保持不变。`,
      "error");
  }
}

function showFeedback(text, kind) {
  const feedback = document.getElementById("config-feedback");
  if (!feedback) return;
  feedback.textContent = text;
  feedback.className = kind ? `hint cfg-${kind}` : "hint";
  feedback.hidden = false;
}

/* ISS-108：预填模式下不再清空输入框——保存成功后 renderEffective 已用服务端
 * 返回的生效值重填（清空会制造「输入框为空但当前值存在」的反查负担）。
 * 留空字段仍按「不修改该项」跳过（防御路径，与既有 PUT 合同一致）。 */
function rootForComparison(value) {
  const root = String(value ?? "").trim();
  // 只消除路径书写格式；不在前端猜测符号链接或 .. 的实际目标。
  return root.startsWith("/") ? root.replace(/\/+/g, "/").replace(/\/$/, "") || "/" : root;
}

async function saveConfig(event) {
  event.preventDefault();
  const body = {};
  const values = {
    scan_root: document.getElementById("cfg-scan-root")?.value.trim(),
    scan_time: document.getElementById("cfg-scan-time")?.value.trim(),
    min_kb: document.getElementById("cfg-min-kb")?.value.trim(),
    free_alert_gb: document.getElementById("cfg-free-alert-gb")?.value.trim(),
  };
  // 留空 = 不修改该项；数值字段可解析为有限数则转 JSON 数值，否则原样上送
  // 由服务端拒绝（校验以服务端为单一权威：正数/有限/格式都在后端钉住）
  for (const [key, value] of Object.entries(values)) {
    if (!value) continue;
    // 预填旧 HOME 只是当前历史根，不代表用户要求改下一轮默认范围。
    // 只有实际改根才写 scan_root；失败回填后仍与当前生效值比较。
    if (key === "scan_root" && lastConfig &&
        rootForComparison(value) === rootForComparison(lastConfig.scan_root)) continue;
    if (key === "min_kb" || key === "free_alert_gb") {
      const numeric = Number(value);
      body[key] = Number.isFinite(numeric) ? numeric : value;
    } else {
      body[key] = value;
    }
  }
  if (!Object.keys(body).length) {
    showFeedback("没有要保存的修改：所有字段都留空了。", "");
    return;
  }
  try {
    const res = await apiPut("/api/config", body);
    const data = await res.json();
    lastConfig = data.config;
    renderEffective(data.config);
    // PUT 返回完整生效配置：一并刷新排除编辑器的只读锁定，避免保存其它字段
    // 后锁状态与 sources 不一致（例如 exclude_names 被环境变量钉住时）。
    renderExcludeEditor(data.config);
    // 计划时间保存后漂移态可能变化（ISS-016B）：随嵌套配置刷新一致性小节。
    renderReloadSection();
    showFeedback(`已保存。${data.hint || ""}`, "ok");
  } catch (e) {
    // 校验失败/服务故障：生效值不重渲染、旧值保持可辨——输入框恢复为当前
    // 生效值（renderEffective(lastConfig)），避免失败后输入框停留无效输入、
    // 被读成「当前值」（ISS-108：输入框=当前生效值是页面不变量）。
    if (lastConfig) renderEffective(lastConfig);
    showFeedback(
      e.status === 0
        ? "保存失败：无法连接本地服务，当前生效值保持不变（输入框已恢复为当前生效值）。"
        : `保存失败：${e.message} 当前生效值保持不变（输入框已恢复为当前生效值）。`,
      "error",
    );
  }
}

function resetToDefaults() {
  const defaults = lastConfig?.defaults;
  if (!defaults) return;
  const fill = (id, value) => {
    const input = document.getElementById(id);
    if (input) input.value = value;
  };
  fill("cfg-scan-root", defaults.scan_root || "");
  fill("cfg-scan-time", defaults.scan_time || "");
  fill("cfg-min-kb", defaults.min_kb == null ? "" : String(defaults.min_kb));
  fill("cfg-free-alert-gb", defaults.free_alert_gb == null ? "" : String(defaults.free_alert_gb));
  showFeedback("已填入默认值；仍需点击「保存设置」才会写入。", "");
}

/* ISS-087：计划与通知 section 独立表单（#schedule-form）只覆盖
 * scan_time + free_alert_gb；与 #config-form（监控：scan_root + min_kb）
 * 共用 showFeedback / renderEffective / lastConfig。scan_time 与
 * free_alert_gb 通过 document.getElementById 读取（与 saveConfig 同口径），
 * 故分两个 form 仍能由各自的 submit handler 各自 PUT。 */
async function saveScheduleConfig(event) {
  event.preventDefault();
  const body = {};
  const values = {
    scan_time: document.getElementById("cfg-scan-time")?.value.trim(),
    free_alert_gb: document.getElementById("cfg-free-alert-gb")?.value.trim(),
  };
  for (const [key, value] of Object.entries(values)) {
    if (!value) continue;
    if (key === "free_alert_gb") {
      const numeric = Number(value);
      body[key] = Number.isFinite(numeric) ? numeric : value;
    } else {
      body[key] = value;
    }
  }
  if (!Object.keys(body).length) {
    showFeedback("没有要保存的修改：计划与通知的所有字段都留空了。", "");
    return;
  }
  try {
    const res = await apiPut("/api/config", body);
    const data = await res.json();
    lastConfig = data.config;
    renderEffective(data.config);
    renderExcludeEditor(data.config);
    renderReloadSection();
    showFeedback(`已保存。${data.hint || ""}`, "ok");
  } catch (e) {
    // 与 saveConfig 同口径（ISS-108）：失败后输入框恢复当前生效值，旧值可辨
    if (lastConfig) renderEffective(lastConfig);
    showFeedback(
      e.status === 0
        ? "保存失败：无法连接本地服务，当前生效值保持不变（输入框已恢复为当前生效值）。"
        : `保存失败：${e.message} 当前生效值保持不变（输入框已恢复为当前生效值）。`,
      "error",
    );
  }
}

async function loadSettings() {
  const request = beginRequest("settings");
  const tbody = document.querySelector("#settings-table tbody");
  const effective = document.getElementById("config-effective");
  let s;
  try {
    s = await fetchJSON("/api/status");
  } catch (e) {
    if (!request.current()) return;
    if (effective) {
      effective.textContent = e.status === 0
        ? "无法连接本地服务，设置状态暂不可用。"
        : `设置状态加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`;
      // ISS-108：effective 默认隐藏（预填模式下无摘要），异常态显式示出
      effective.hidden = false;
    }
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="2" class="hint">${escapeHtml(e.status === 0
        ? "无法连接本地服务，运行信息暂不可用。"
        : `运行信息加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`)}</td></tr>`;
    }
    return;
  }
  if (!request.current()) return;
  // ISS-111：版本在构建时已知——status 一到就回填关于页版本号，
  // 不再依赖用户点「检查更新」；更新检查返回值仍可覆盖（两源一致）。
  lastStatusAppVersion = typeof s.app_version === "string" && s.app_version
    ? s.app_version : null;
  renderAboutVersion();
  let c = null;
  try {
    c = await fetchJSON("/api/config");
  } catch (e) {
    if (!request.current()) return;
    if (effective) {
      effective.textContent = e.status === 0
        ? "无法连接本地服务，可修改设置暂不可用（输入框为空不代表当前没有配置）。"
        : `可修改设置加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`;
      effective.hidden = false;
    }
  }
  if (!request.current()) return;
  if (c) {
    lastConfig = c;
    renderEffective(c);
    renderExcludeEditor(c);
  }
  const p = c?.policies || {};
  const dbMb = s.db_bytes ? (s.db_bytes / 1024 / 1024).toFixed(1) : "0.0";
  // ISS-083：第三列标记技术行——打包态收进折叠区，浏览器态全量按原序渲染
  const rows = [
    ["监控根目录", `<code>${escapeHtml(s.root)}</code>`, false],
    ["服务地址", `<code>http://127.0.0.1:${escapeHtml(String(s.port))}</code>（本地回环）`, true],
    ["运行根", `<code>${escapeHtml(s.runtime?.runtime_dir || "")}</code>`, true],
    ["数据库", `<code>${escapeHtml(s.runtime?.db_path || "")}</code> · ${dbMb} MB`, true],
    ["快照保留", p.keep_daily_days
      ? `近 ${escapeHtml(String(p.keep_daily_days))} 天每日一份 + 更早每周一份（最多 ${escapeHtml(String(p.keep_weekly_weeks))} 周）`
      : "—", false],
    ["du 安全时限", p.du_timeout_s ? `<code>${escapeHtml(String(p.du_timeout_s))} 秒</code>` : "—", true],
    ["大文件默认范围", p.bigfile_default_days
      ? `近 ${escapeHtml(String(p.bigfile_default_days))} 天 · ≥ ${escapeHtml(String(p.bigfile_default_mb))} MB`
      : "—", false],
    ["桌面壳", "apps/desktop（Tauri 菜单栏 + 主窗口）", true],
  ];
  renderRunInfoTables(rows);

  // 加载扫描运行历史（可独立失败，不影响主配置）
  loadScanHistory();
  // 加载"权限与覆盖"小节（ISS-002A；可独立失败，不影响主配置）
  loadPermissions();
  // 加载「权限」分区三项权限卡（ISS-111；可独立失败，不影响主配置）
  loadPermissionHub();
  // 加载"后台自启"开关（ISS-010B；可独立失败，不影响主配置）
  loadAutostart();
  // 加载"应用更新"区（ISS-040B；可独立失败，不影响主配置）
  loadUpdater();
  // 加载"AI 分析"分区（ISS-035C；可独立失败，不影响主配置）
  loadAnalysisSettings();
  // 加载"范围与覆盖"分区（ISS-156；可独立失败，不影响其它分区）
  loadScopeSettings();
}

/* ISS-109：状态/来源英文枚举的中文展示映射；未登记的值原样展示不吞字 */
const SCAN_STATUS_LABELS = {
  done: "完成", failed: "失败", interrupted: "已中断",
  running: "运行中", cancelled: "已取消",
};
const SCAN_SOURCE_LABELS = { scheduled: "计划", cli: "命令行", api: "手动" };

async function loadScanHistory() {
  const target = document.getElementById("scan-history");
  if (!target) return;
  const request = beginRequest("scanHistory");
  try {
    const r = await fetchJSON("/api/scan/status?history=5");
    if (!request.current()) return;
    const runs = r.runs || [];
    if (!runs.length) {
      target.innerHTML = `<p class="hint">尚无扫描运行记录。</p>`;
      return;
    }
    target.innerHTML = `<table class="tbl tbl-mini">
      <thead><tr><th>开始时间</th><th>结束时间</th><th>来源</th><th>状态</th><th>快照</th><th>说明</th></tr></thead>
      <tbody>${runs.map((run) => {
        const st = run.status || "—";
        const stCls = st === "done" ? "st-ok" : st === "failed" ? "st-fail" : "";
        const dot = st === "done"
          ? `<span class="st-dot" style="background:var(--ok)"></span>`
          : st === "failed"
            ? `<span class="st-dot" style="background:var(--danger)"></span>`
            : `<span class="st-dot"></span>`;
        return `<tr>
          <td>${escapeHtml((run.started_at || "").slice(0, 16).replace("T", " "))}</td>
          <td>${escapeHtml((run.finished_at || "").slice(0, 16).replace("T", " "))}</td>
          <td>${escapeHtml(SCAN_SOURCE_LABELS[run.source] || run.source || "—")}</td>
          <td><span class="st ${stCls}">${dot}${escapeHtml(SCAN_STATUS_LABELS[st] || st)}</span></td>
          <td>${run.snapshot_id == null ? "—" : "#" + escapeHtml(String(run.snapshot_id))}</td>
          <td class="run-message">${escapeHtml(run.message || "")}</td>
        </tr>`;
      }).join("")}</tbody></table>
      <p class="hint">${icon("fileText", 12)} 仅展示最近 5 条；历史归档见 reports/。</p>`;
  } catch (e) {
    if (!request.current()) return;
    target.innerHTML = `<p class="hint">${escapeHtml(e.status === 0
      ? "无法连接本地服务，扫描历史暂不可用。"
      : `扫描历史加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`)}</p>`;
  }
}

/* ===== 权限与覆盖（ISS-002A） =====
 * DOM 在 settings.js 内联创建（合同未授权修改 index.html），插在
 * "扫描运行历史"面板之前；同一 ID 仅创建一次以避免重复挂事件。 */
const PERMISSIONS_PANEL_ID = "permissions-panel";

function _ensurePermissionsPanel() {
  const page = document.getElementById("page-settings");
  if (!page) return null;
  let panel = document.getElementById(PERMISSIONS_PANEL_ID);
  if (panel) return panel;
  panel = document.createElement("div");
  panel.id = PERMISSIONS_PANEL_ID;
  panel.className = "panel";
  panel.innerHTML = `
    <div class="panel-head">
      <h2>权限与覆盖</h2>
      <p class="hint">以下只说明最近一次扫描的覆盖情况；当前完全磁盘访问的探测结果见「权限」分区。</p>
    </div>
    <div class="perm-panel" data-test="permissions-panel-body">
      <p class="hint">权限与覆盖信息加载中…</p>
    </div>`;
  // ISS-087：权限与覆盖归「计划与通知」section，挂在 #settings-schedule-extra 容器下。
  const host = document.getElementById("settings-schedule-extra");
  if (host) host.appendChild(panel);
  else page.appendChild(panel);
  return panel;
}

/* 最近快照的覆盖说明（ISS-002A）：不能由扫描结果反推当前授权。
 *   full       → 最近扫描范围完整
 *   partial+denied      → du 报告了读取受限错误行
 *   partial+vanished    → du 输出后校验未确认路径仍存在
 *   partial+excluded    → 扫描集已自定义（排除掩码生效）
 *   partial+none        → 部分覆盖（具体缺口未上报）
 *   missing   → 尚未扫描或覆盖未知
 * denied 是 stderr 行数，不冒充不同目录数或影响大小。 */
function _explainCoverage(coverage) {
  if (coverage.state === "full") {
    return { text: "最近扫描：范围完整", cls: "ok" };
  }
  if (coverage.state === "missing") {
    return { text: "尚未扫描或覆盖未知", cls: "miss" };
  }
  if (coverage.denied > 0) {
    return {
      text: `最近扫描：${coverage.denied} 条读取受限记录`,
      cls: "warn",
    };
  }
  if (coverage.confirmed !== null && coverage.unverified !== null) {
    if (coverage.confirmed > 0) return {
      text: `最近扫描：${coverage.confirmed} 个目录校验时路径不存在`, cls: "warn",
    };
    if (coverage.unverified > 0) return {
      text: `最近扫描：${coverage.unverified} 个目录状态无法确认`, cls: "warn",
    };
  }
  if (coverage.vanished > 0) {
    return {
      text: `最近扫描：${coverage.vanished} 个目录状态未确认`,
      cls: "warn",
    };
  }
  if (coverage.excluded > 0) {
    return {
      text: `最近扫描：${coverage.excluded} 项排除掩码生效（与默认不同）`,
      cls: "warn",
    };
  }
  return { text: "部分覆盖：缺口类型未上报", cls: "warn" };
}

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
    excluded = ex.split(";").filter((s) => s.trim()).length;
  }
  let state;
  if (snapshot.collection_status === "partial") state = "partial";
  else if (denied > 0) state = "partial";
  else state = "full";
  return { state, denied, vanished, confirmed, unverified, excluded };
}

async function loadPermissions() {
  const panel = _ensurePermissionsPanel();
  if (!panel) return;
  const body = panel.querySelector("[data-test='permissions-panel-body']");
  if (!body) return;
  const request = beginRequest("settingsPermissions");
  let statusData = null;
  try {
    statusData = await fetchJSON("/api/status");
    if (!request.current()) return;
  } catch (e) {
    if (!request.current()) return;
    body.innerHTML = `<p class="hint">${escapeHtml(e.status === 0
      ? "无法连接本地服务，权限与覆盖信息暂不可用。"
      : `权限与覆盖信息加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`)}</p>`;
    return;
  }
  const latest = statusData?.latest_snapshot;
  const coverage = _coverage(latest);
  const auth = _explainCoverage(coverage);
  const tauri = window.__TAURI__;
  const tauriAvailable = !!(tauri && tauri.core && typeof tauri.core.invoke === "function");
  body.innerHTML = `
    <p class="perm-state">
      <span class="quality-chip ${auth.cls}" data-test="perm-auth-chip">${icon("shield", 12)} ${escapeHtml(auth.text)}</span>
    </p>
    ${latest?.created_at ? `<p class="perm-note">快照时间：${escapeHtml(String(latest.created_at).slice(0, 16).replace("T", " "))}；授权变更不会更新已有快照。</p>` : ""}
    <p class="perm-note">本应用不代改系统权限，授权由你在系统设置完成；
      在浏览器模式下不会自动跳转系统设置，下方显示的是应进入的路径。</p>
    <div class="perm-link-row">
      <button type="button" id="btn-open-system-prefs" class="perm-link"
              data-test="perm-open-prefs-btn"${tauriAvailable ? "" : " hidden"}>
        ${icon("externalLink", 14)} 打开系统设置 → 隐私与安全性 → 完全磁盘访问
      </button>
      <span class="perm-link-fallback" data-test="perm-path-fallback"${tauriAvailable ? " hidden" : ""}>
        ${escapeHtml(PREFS_PATH_TEXT)}
      </span>
    </div>
    <p class="perm-note">授权变更或排除列表调整后，本应用不会自动重新扫描；
      点击下方按钮复用既有扫描入口与状态反馈，不另起轮询。</p>
    <div class="perm-link-row">
      <!-- ISS-105：与「保存设置」同分区共存，降为次级描边——实心主按钮
           每视口至多一个（监控分区主任务是保存设置） -->
      <button type="button" id="btn-rescan" class="btn perm-rescan"
              data-test="perm-rescan-btn">${icon("activity", 14)} 重新扫描</button>
      <span id="perm-rescan-status" class="hint" data-test="perm-rescan-status" hidden></span>
    </div>`;

  const openBtn = document.getElementById("btn-open-system-prefs");
  if (openBtn && tauriAvailable) {
    openBtn.addEventListener("click", async () => {
      openBtn.disabled = true;
      try {
        // Tauri opener 插件命令：plugin:opener|open_url；名称与 tauri-plugin-opener 1.x 一致
        await tauri.core.invoke("plugin:opener|open_url", { url: PREFS_DEEP_LINK });
      } catch (e) {
        // 跳转失败不应静默吞错：显示路径文字作为降级
        alert("无法打开系统设置。请手动前往 " + PREFS_PATH_TEXT + "。");
      } finally {
        openBtn.disabled = false;
      }
    });
  }

  const rescanBtn = document.getElementById("btn-rescan");
  const rescanStatus = document.getElementById("perm-rescan-status");
  if (rescanBtn) {
    rescanBtn.addEventListener("click", async () => {
      rescanBtn.disabled = true;
      if (rescanStatus) {
        rescanStatus.hidden = false;
        rescanStatus.textContent = "已发起扫描，请等待。";
      }
      try {
        await triggerScan();
        if (rescanStatus) rescanStatus.textContent = "扫描进行中；顶栏状态条与本节随生命周期更新。";
        try { await loadStatus(); } catch (e) { /* 状态刷新失败不影响已发起的事实 */ }
      } catch (e) {
        if (rescanStatus) {
          rescanStatus.textContent = e.status === 409
            ? "已有扫描在进行中。"
            : `扫描启动失败：${e.message || ""}`;
        }
      } finally {
        rescanBtn.disabled = false;
      }
    });
  }
}

/* ===== 权限分区（ISS-111，承接 ISS-091 卡数据）=====
 * 设置页新增「权限」分区：各权限当前状态可见 + 一键深链授权。用户裁决：
 * 「应该有一个权限管理的页面……在这个页面里面点击让我去授权，和当前已
 * 授予权限的显示」。
 * 三张权限项卡（数据源 = GET /api/permissions）：
 * - 完全磁盘访问：后端对 TCC 保护路径只读探测的 granted/denied/unknown
 *   三态（探测异常如实 unknown，不伪造）；辅助呈现最近扫描的受限/校验未确认
 *   计数（ISS-091 监控卡数据迁入本分区，原卡移除、原位留交叉
 *   说明——见 index.html）；深链 = 系统设置完全磁盘访问页。
 * - 通知：最近一次扫描的 notification_status（submitted/failed/未登记
 *   如实展示，不推断）；深链 = 系统设置通知页。
 * - 后台计划：交叉引用「计划与通知」分区的既有 autostart 状态（一行
 *   链接式引导，不重复实现）。
 * 通用合同：
 * - macOS 不允许应用自提权：每卡一句白话说明，授权动作只在系统设置完成，
 *   本应用不代改系统权限；
 * - 深链按钮复用 091 的 opener 模式（plugin:opener|open_url，与
 *   ISS-002A 同命令）；浏览器态（无 Tauri 桥）按钮隐藏、降级为路径文字，
 *   不渲染假 <a href>；
 * - 状态徽章配色用既有语义 token：granted→--ok、denied→--danger、
 *   unknown→--muted（.quality-chip 既有 ok/miss 类 + ISS-111 新增 danger
 *   浅底派生，无新色板）；零 emoji；
 * - denied 是 du stderr 权限错误行数，与已记录目录数不是同一单位，不展示
 *   相除所得百分比或倍数。 */
const PERM_HUB_PANEL_ID = "permissions-hub-panel";

/* 通知页深链：与 FDA 深链同走 opener 权限（ISS-068 ACL 已备
 * x-apple.systempreferences:*）。浏览器态降级显示路径文字。 */
const NOTIFS_DEEP_LINK = "x-apple.systempreferences:com.apple.preference.notifications";
const NOTIFS_PATH_TEXT = "系统设置 › 通知";

/* 状态徽章语义映射：unknown/未登记是真实探测结论，不是加载失败。 */
const PERM_FDA_BADGES = {
  granted: { text: "探测可读", cls: "ok" },
  denied: { text: "探测受限", cls: "danger" },
  unknown: { text: "未知", cls: "miss" },
};
const PERM_NOTIF_BADGES = {
  submitted: { text: "已提交", cls: "ok" },
  failed: { text: "失败", cls: "danger" },
};

function _ensurePermHubPanel() {
  const host = document.getElementById("settings-permissions-extra");
  if (!host) return null;
  let panel = document.getElementById(PERM_HUB_PANEL_ID);
  if (panel) return panel;
  panel = document.createElement("div");
  panel.id = PERM_HUB_PANEL_ID;
  panel.innerHTML = `
    <div class="panel perm-item" id="perm-fda-card" data-test="perm-fda-card">
      <div class="panel-head">
        <h2>完全磁盘访问</h2>
        <span class="quality-chip miss" data-test="perm-fda-badge">${icon("shield", 12)} 检测中…</span>
      </div>
      <div class="perm-panel" data-test="perm-fda-body">
        <p class="hint">权限状态加载中…</p>
      </div>
    </div>
    <div class="panel perm-item" id="perm-notif-card" data-test="perm-notif-card">
      <div class="panel-head">
        <h2>通知</h2>
        <span class="quality-chip miss" data-test="perm-notif-badge">${icon("activity", 12)} 检测中…</span>
      </div>
      <div class="perm-panel" data-test="perm-notif-body">
        <p class="hint">通知状态加载中…</p>
      </div>
    </div>
    <div class="panel perm-item" id="perm-schedule-card" data-test="perm-schedule-card">
      <div class="panel-head">
        <h2>后台计划</h2>
      </div>
      <div class="perm-panel" data-test="perm-schedule-body">
        <p class="hint">定时扫描与开机自启的开启状态在「计划与通知」分区查看与调整；这里不重复实现。</p>
        <div class="perm-link-row">
          <button type="button" class="btn" data-test="perm-schedule-goto-btn">${icon("clock", 14)} 前往计划与通知</button>
        </div>
      </div>
    </div>`;
  host.appendChild(panel);
  const gotoBtn = panel.querySelector("[data-test='perm-schedule-goto-btn']");
  if (gotoBtn) {
    gotoBtn.addEventListener("click", () => {
      activateSettingsSection("schedule", { persistHash: true });
    });
  }
  return panel;
}

/** FDA 三态的白话说明：只描述当前服务对一个保护位置的探测。 */
function _permFdaExplain(status) {
  if (status === "granted") {
    return "当前服务可读取探测位置（如用户目录下的 Containers）；这不能保证扫描范围内所有路径都可读。";
  }
  if (status === "denied") {
    return "当前服务读取探测位置遭拒。请核对 Fathom 的「完全磁盘访问」；授权变更后重启后台服务并重新扫描，才能获得新结果。";
  }
  return "本次探测未得出结论（保护路径不存在或读取异常）。状态未知时不猜测，可稍后重试。";
}

/** FDA 卡：三态徽章 + 白话说明 + 091 受限/校验未确认事实 + 深链。 */
function renderPermFda(body, fda, coverage) {
  const invoke = tauriInvoke();
  const tauriAvailable = !!invoke;
  const badgeNode = document.querySelector("#perm-fda-card [data-test='perm-fda-badge']");
  const badge = PERM_FDA_BADGES[fda?.status] || PERM_FDA_BADGES.unknown;
  if (badgeNode) {
    badgeNode.className = `quality-chip ${badge.cls}`;
    badgeNode.innerHTML = `${icon("shield", 12)} ${escapeHtml(badge.text)}`;
  }
  // 最近快照的受限错误行数与校验未确认路径数；与当前授权探测分开。
  let factsHtml;
  if (coverage && coverage.snapshot_id != null) {
    const denied = Number(coverage.denied_count ?? 0) || 0;
    const vanished = Number(coverage.vanished_count ?? 0) || 0;
    const classified = Number.isInteger(coverage.confirmed_missing_count) &&
      Number.isInteger(coverage.path_unverified_count);
    const confirmed = classified ? coverage.confirmed_missing_count : 0;
    const unverified = classified ? coverage.path_unverified_count : 0;
    const scanTime = coverage.created_at
      ? escapeHtml(String(coverage.created_at).slice(0, 16).replace("T", " "))
      : "时间未知";
    factsHtml = `
      <p class="perm-note">最近扫描：${scanTime}。以下是当时的记录；授权变更不会改写旧快照，请重新扫描后比较。</p>
      <div class="perm-facts" data-test="perm-fda-facts">
        <div class="perm-fact">
          <span class="perm-fact-num" data-test="perm-fda-denied" data-sev="${denied > 0 ? "warn" : "ok"}">${escapeHtml(String(denied))}</span>
          <span class="perm-fact-label">读取受限错误（条）</span>
        </div>
        ${classified ? `
          <div class="perm-fact">
            <span class="perm-fact-num" data-test="perm-fda-confirmed-missing" data-sev="${confirmed > 0 ? "warn" : "ok"}">${escapeHtml(String(confirmed))}</span>
            <span class="perm-fact-label">校验时路径不存在（个目录）</span>
          </div>
          <div class="perm-fact">
            <span class="perm-fact-num" data-test="perm-fda-path-unverified" data-sev="${unverified > 0 ? "warn" : "ok"}">${escapeHtml(String(unverified))}</span>
            <span class="perm-fact-label">目录状态无法确认（个）</span>
          </div>` : `
          <div class="perm-fact">
            <span class="perm-fact-num" data-test="perm-fda-vanished" data-sev="${vanished > 0 ? "warn" : "ok"}">${escapeHtml(String(vanished))}</span>
            <span class="perm-fact-label">目录状态未确认（个）</span>
          </div>`}
      </div>
      <p class="perm-note">受限数字是 du 输出的权限错误行数，同一路径可能出现多条；${classified
        ? "校验时路径不存在只说明当时状态；无法确认可能由权限或其他读取错误造成。不能据此认定谁删除了目录。"
        : "目录状态未确认表示 du 输出后校验时未能确认路径仍存在，可能已移动、被清理或无法访问，不能认定已删除。"}${classified ? "不同类别" : "两类"}计数可能指向同一路径，不能相加成不同目录数，也不能推出受影响空间大小。</p>`;
  } else {
    factsHtml =
      `<p class="hint" data-test="perm-fda-not-yet">尚未扫描：完成首次扫描后，这里会显示读取受限与目录状态未确认的情况。</p>`;
  }
  body.innerHTML = `
    <p class="perm-note">${escapeHtml(_permFdaExplain(fda?.status))}</p>
    ${factsHtml}
    <div class="perm-link-row">
      <button type="button" id="btn-perm-fda-open" class="perm-link"
              data-test="perm-fda-open-btn"${tauriAvailable ? "" : " hidden"}>
        ${icon("externalLink", 14)} 打开系统设置 → 完全磁盘访问
      </button>
      <span class="perm-link-fallback" data-test="perm-fda-path-fallback"${tauriAvailable ? " hidden" : ""}>
        ${escapeHtml(PREFS_PATH_TEXT)}
      </span>
    </div>`;
  const openBtn = document.getElementById("btn-perm-fda-open");
  if (openBtn && invoke) {
    openBtn.addEventListener("click", async () => {
      openBtn.disabled = true;
      try {
        // 与 ISS-002A 深链同命令同目标：tauri-plugin-opener 1.x 的 open_url
        await invoke("plugin:opener|open_url", { url: PREFS_DEEP_LINK });
      } catch (e) {
        // 跳转失败不静默吞错：弹路径文字作为降级（与 ISS-002A 同形态）
        alert("无法打开系统设置。请手动前往 " + PREFS_PATH_TEXT + "。");
      } finally {
        openBtn.disabled = false;
      }
    });
  }
}

/** 通知卡：最近一次扫描的 notification_status 如实展示 + 通知页深链。 */
function renderPermNotif(body, notif) {
  const invoke = tauriInvoke();
  const tauriAvailable = !!invoke;
  const status = notif?.notification_status;
  const badge = PERM_NOTIF_BADGES[status] || { text: "未登记", cls: "miss" };
  const badgeNode = document.querySelector("#perm-notif-card [data-test='perm-notif-badge']");
  if (badgeNode) {
    badgeNode.className = `quality-chip ${badge.cls}`;
    badgeNode.innerHTML = `${icon("activity", 12)} ${escapeHtml(badge.text)}`;
  }
  const run = notif?.run_id != null ? `#${notif.run_id}` : "";
  let explain;
  if (status === "submitted") {
    explain = `最近一次扫描${run}完成时，系统通知已提交。`;
  } else if (status === "failed") {
    explain = `最近一次扫描${run}的系统通知提交失败；可核对系统通知设置后重试扫描。`;
  } else if (status) {
    explain = `最近一次扫描${run}的通知状态为「${status}」（如实展示，不推断）。`;
  } else {
    explain = "最近一次扫描未登记通知状态（可能尚未产生扫描运行）。";
  }
  body.innerHTML = `
    <p class="perm-note">${escapeHtml(explain)} 通知经系统通知中心投递；如未看到通知，可在系统设置的通知页允许 Fathom。</p>
    <div class="perm-link-row">
      <button type="button" id="btn-perm-notif-open" class="perm-link"
              data-test="perm-notif-open-btn"${tauriAvailable ? "" : " hidden"}>
        ${icon("externalLink", 14)} 打开系统设置 → 通知
      </button>
      <span class="perm-link-fallback" data-test="perm-notif-path-fallback"${tauriAvailable ? " hidden" : ""}>
        ${escapeHtml(NOTIFS_PATH_TEXT)}
      </span>
    </div>`;
  const openBtn = document.getElementById("btn-perm-notif-open");
  if (openBtn && invoke) {
    openBtn.addEventListener("click", async () => {
      openBtn.disabled = true;
      try {
        await invoke("plugin:opener|open_url", { url: NOTIFS_DEEP_LINK });
      } catch (e) {
        alert("无法打开系统设置。请手动前往 " + NOTIFS_PATH_TEXT + "。");
      } finally {
        openBtn.disabled = false;
      }
    });
  }
}

async function loadPermissionHub() {
  const panel = _ensurePermHubPanel();
  if (!panel) return;
  const fdaBody = panel.querySelector("[data-test='perm-fda-body']");
  const notifBody = panel.querySelector("[data-test='perm-notif-body']");
  if (!fdaBody || !notifBody) return;
  const request = beginRequest("permHub");
  let data = null;
  try {
    data = await fetchJSON("/api/permissions");
    if (!request.current()) return;
  } catch (e) {
    if (!request.current()) return;
    const msg = e.status === 0
      ? "无法连接本地服务，权限状态暂不可用。"
      : `权限状态加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`;
    fdaBody.innerHTML = `<p class="hint">${escapeHtml(msg)}</p>`;
    notifBody.innerHTML = `<p class="hint">${escapeHtml(msg)}</p>`;
    return;
  }
  renderPermFda(fdaBody, data?.fda, data?.coverage);
  renderPermNotif(notifBody, data?.notification);
}

/* ===== AI 分析（ISS-035C）=====
 * 变化页 AI 解读的引擎检测/选择/授权分区。合同（方案 §7 + 用户裁决
 * 2026-09-29）：
 * - 检测只经用户点击触发（POST /api/analysis/runtimes/detect；探测只读，
 *   不发送业务数据）；
 * - 四家候选全部列出：Claude Code 首位 + 「推荐」徽章（用户裁决）；
 *   Codex（OS 级只读沙箱）与 Hermes（调用级只读工具集）按 DEC-030 防破坏
 *   门可选（能力边界在行内披露），ZCode 不隐藏不折叠——不支持/未安装/
 *   启动异常如实显示原因；
 * - --version 成功不等于已登录：ready 家 auth_status=unknown 时显示
 *   「认证待确认」，任何路径不出现「已登录」表述（反例 1 的断言锚点）；
 * - 只有 availability=ready 的候选可选中；选择必经授权确认层——发送对象
 *   说明（本机 CLI 可能把数据发送给其配置的模型服务，不暗示本地推理）、
 *   本地保存与撤销说明、账号额度由该 CLI 管理；
 * - 授权/关闭/换引擎都走既有 PUT /api/config 的 analysis 键（部分合并）；
 *   失败保持旧值可辨（与 ISS-108 同口径：以 lastConfig 重渲染）；
 * - 保存成功后提示：变化页已保存的发送预览已失效，下次分析需重新预览
 *   （后端 refresh_policy 同步取消在途分析，前端只提示不替后端表态）。
 */
const ANALYSIS_PANEL_ID = "analysis-settings-panel";

/* 候选展示顺序：Claude Code 首推固定首位；其余按注册表顺序。
 * detect 响应缺失的候选也照常渲染（显示「未检测」），不隐藏。 */
const ANALYSIS_RUNTIME_ORDER = ["claude-code", "zcode", "codex-cli", "hermes-agent"];
const ANALYSIS_RECOMMENDED_ID = "claude-code";

const ANALYSIS_AVAIL_BADGES = {
  ready: { text: "可用", cls: "ok" },
  unsupported: { text: "暂不支持", cls: "miss" },
  not_found: { text: "未安装", cls: "miss" },
  broken: { text: "启动异常", cls: "danger" },
};

/* reason_code 的用户可读文案；未登记的值原样展示不吞字（与既有映射
 * SCAN_STATUS_LABELS 同口径）。 */
const ANALYSIS_REASON_LABELS = {
  verified_version: "已验证版本",
  not_found: "本机未找到该命令行",
  version_probe_failed: "版本探测失败（启动后无有效输出）",
  version_unrecognized: "版本号未识别（可能未经测试）",
  version_unverified: "版本不在已验证清单内",
  probe_budget_exhausted: "本轮探测预算用尽，可稍后重测",
  unsupported_by_contract: "当前产品合同未开放该引擎",
  tool_disable_unverifiable: "无法从外部证明其工具已全部禁用",
  tool_disable_ineffective_by_design: "仅支持按工具名逐个禁用，无全部禁用或白名单开关（实测禁用被其余工具绕过）",
  no_readonly_enforcement: "只读形态写路径未封死：只读模式仍放行可执行写入的外部工具，且无法从命令行禁用",
  tool_gate_absent_by_design: "无工具级禁用参数或配置键；只读沙箱实测允许运行只读命令并读取工作目录外文件",
  tool_events_unobservable: "调用形态无法观察工具事件",
};

/* ready 家的能力边界披露（DEC-030：用户裁决允许读任意本机文件、允许联网，
 * 防破坏是硬底线——如实分写各家在分析期间对本身机的实际能力边界，
 * 与授权确认层的发送对象/本地保存说明互补）。 */
const ANALYSIS_RUNTIME_CAP_NOTES = {
  "claude-code":
    "运行时禁用全部工具：只产出文本解读，不执行命令、不读写本机文件、不联网。",
  "codex-cli":
    "运行于系统级只读沙箱：不可写入、删除文件或执行破坏性命令（系统层强制拦截）；" +
    "可读取本机任意文件、可联网访问其模型服务。",
  "hermes-agent":
    "以只读工具集运行（仅网页搜索/网页读取/图像分析）：不可写入、删除文件或执行命令。" +
    "其图像分析工具本身能读取本机图片并把图片内容交给其模型服务，但本应用不会向它提供" +
    "本机路径（事实包内路径已做别名脱敏），且提示词明确要求不调用工具，因此当前解读不会触发；" +
    "不读取其他类型的本机文件；可联网访问其模型服务并搜索网页。",
};

/* 认证三态：unknown 是探测的真实结论（--version 不解释登录态）。 */
const ANALYSIS_AUTH_BADGES = {
  verified: { text: "已验证登录", cls: "ok" },
  required: { text: "需要登录", cls: "warn" },
  unknown: { text: "认证待确认", cls: "warn" },
};

function _ensureAnalysisPanel() {
  const host = document.getElementById("settings-analysis-extra");
  if (!host) return null;
  let panel = document.getElementById(ANALYSIS_PANEL_ID);
  if (panel) return panel;
  panel = document.createElement("div");
  panel.id = ANALYSIS_PANEL_ID;
  panel.className = "panel";
  panel.innerHTML = `
    <div class="panel-head">
      <h2>变化解读引擎</h2>
      <span class="quality-chip miss" data-test="analysis-state-chip">${icon("sparkles", 12)} 未知</span>
    </div>
    <div class="perm-panel" data-test="analysis-panel-body">
      <p class="hint">状态加载中…</p>
    </div>`;
  host.appendChild(panel);
  return panel;
}

/** 当前授权状态的 chip（含引擎名/版本，未配置如实显示）。 */
function analysisStateChip(cfgAnalysis) {
  const node = document.querySelector("#" + ANALYSIS_PANEL_ID + " [data-test='analysis-state-chip']");
  if (!node) return;
  const runtime = cfgAnalysis?.runtime;
  if (cfgAnalysis?.enabled && runtime) {
    node.className = "quality-chip ok";
    node.innerHTML = `${icon("sparkles", 12)} 已授权 ${escapeHtml(runtime.id)}` +
      (runtime.version ? ` v${escapeHtml(runtime.version)}` : "");
  } else {
    node.className = "quality-chip miss";
    node.innerHTML = `${icon("sparkles", 12)} 未启用`;
  }
}

/** 渲染检测出的四家候选行。detectResult: {id: RuntimeInfo dict} | null（未检测）。 */
function renderAnalysisRuntimes(body, cfgAnalysis, detectResult, detecting) {
  const runtime = cfgAnalysis?.runtime || null;
  const enabled = Boolean(cfgAnalysis?.enabled);
  const rows = ANALYSIS_RUNTIME_ORDER.map((id) => {
    const info = detectResult ? detectResult[id] : null;
    const metaRecommended = id === ANALYSIS_RECOMMENDED_ID;
    if (!info) {
      return `
      <li class="analysis-runtime-row" data-test="analysis-runtime-row" data-runtime-id="${id}">
        <div class="analysis-runtime-main">
          <span class="analysis-runtime-name">${escapeHtml(id)}</span>
          ${metaRecommended ? `<span class="analysis-reco-badge" data-test="analysis-reco-badge">推荐</span>` : ""}
          <span class="quality-chip miss"><span class="st-dot"></span>未检测</span>
        </div>
        <p class="analysis-reason">点击上方「检测可用引擎」查看本机可用性。</p>
      </li>`;
    }
    const avail = ANALYSIS_AVAIL_BADGES[info.availability] ||
      { text: String(info.availability), cls: "miss" };
    const reasonText = ANALYSIS_REASON_LABELS[info.reason_code] || info.reason_code || "";
    const capNote = ANALYSIS_RUNTIME_CAP_NOTES[id];   // 仅 ready 家登记（DEC-030 披露）
    const auth = ANALYSIS_AUTH_BADGES[info.auth_status];
    const selectable = info.availability === "ready";
    const active = enabled && runtime && runtime.id === id;
    const versionText = info.version ? `版本 ${escapeHtml(info.version)}` : "";
    const isCurrent = runtime && runtime.id === id;
    const drift = isCurrent && info.executable && runtime.executable &&
      info.executable !== runtime.executable;
    return `
      <li class="analysis-runtime-row${selectable ? "" : " analysis-runtime-disabled"}"
          data-test="analysis-runtime-row" data-runtime-id="${escapeHtml(id)}"
          data-availability="${escapeHtml(info.availability)}">
        <div class="analysis-runtime-main">
          <span class="analysis-runtime-name">${escapeHtml(info.display_name || id)}</span>
          ${metaRecommended ? `<span class="analysis-reco-badge" data-test="analysis-reco-badge">推荐</span>` : ""}
          <span class="quality-chip ${avail.cls}" data-test="analysis-avail-badge">${escapeHtml(avail.text)}</span>
          ${selectable && auth ? `<span class="quality-chip ${auth.cls}" data-test="analysis-auth-badge">${escapeHtml(auth.text)}</span>` : ""}
          ${active ? `<span class="quality-chip ok" data-test="analysis-active-badge">当前使用</span>` : ""}
          ${versionText ? `<span class="hint">${versionText}</span>` : ""}
        </div>
        ${reasonText ? `<p class="analysis-reason" data-test="analysis-reason">${escapeHtml(reasonText)}${info.detail && info.availability !== "ready" ? `：${escapeHtml(info.detail)}` : ""}</p>` : ""}
        ${selectable && capNote ? `<p class="analysis-reason" data-test="analysis-cap-note">${escapeHtml(capNote)}</p>` : ""}
        ${drift ? `<p class="analysis-reason cfg-error">检测到的入口与已保存路径不同；重新选择会更新保存的引擎。</p>` : ""}
        ${selectable ? `
        <div class="analysis-runtime-actions">
          <button type="button" class="btn" data-test="analysis-pick-btn" data-runtime-id="${escapeHtml(id)}">
            ${active ? "重新确认授权" : "选择此引擎"}
          </button>
        </div>` : ""}
      </li>`;
  }).join("");
  const detectNote = detecting
    ? `<span class="dr-loading" data-dr-spin aria-hidden="true"></span>正在检测（只读探测，最多约 20 秒）…`
    : "";
  body.innerHTML = `
    <p class="perm-note">解读在分析时把两个快照的脱敏目录事实打包交给本机命令行引擎；
      引擎可能把数据发送给它配置的模型服务。账号、额度与模型配置由该命令行管理，
      Fathom 不保存密钥。检测只读取本机命令行的版本信息，不发送业务数据。</p>
    <div class="perm-link-row">
      <button type="button" id="btn-analysis-detect" class="btn" data-test="analysis-detect-btn"${detecting ? " disabled" : ""}>
        ${icon("settings", 14)} 检测可用引擎
      </button>
      <span class="hint" data-test="analysis-detect-status">${detectNote}</span>
    </div>
    <ul class="analysis-runtime-list" data-test="analysis-runtime-list">${rows}</ul>
    <p class="hint" data-test="analysis-save-note">${escapeHtml(analysisSaveNote(cfgAnalysis))}</p>
    <div id="analysis-confirm-layer" data-test="analysis-confirm-layer" hidden></div>`;
  const detectBtn = document.getElementById("btn-analysis-detect");
  if (detectBtn) detectBtn.addEventListener("click", () => detectAnalysisRuntimes(body));
  body.querySelectorAll("[data-test='analysis-pick-btn']").forEach((btn) => {
    btn.addEventListener("click", () => confirmAnalysisPick(body, btn.dataset.runtimeId, detectResult));
  });
}

/** 保存说明：revision 变化提示重新预览（范围合同）。 */
function analysisSaveNote(cfgAnalysis) {
  if (!cfgAnalysis?.enabled) {
    return "当前未启用 AI 解读：变化页只显示基础事实，不发送任何数据。";
  }
  const runtime = cfgAnalysis.runtime;
  if (!runtime) return "已启用但未选择引擎（异常状态）：请重新检测并选择。";
  return `已授权 ${runtime.id}${runtime.version ? ` v${runtime.version}` : ""}。` +
    "改动保存后，变化页已生成的发送预览即失效（如有），下次分析前需重新生成预览；" +
    "在途分析会被取消。";
}

/** 检测（用户点击触发）：失败显示可读原因与重试（detect 按钮常驻即重试入口）。 */
async function detectAnalysisRuntimes(body) {
  const request = beginRequest("settingsAnalysis");
  const status = body.querySelector("[data-test='analysis-detect-status']");
  renderAnalysisDetecting(body);
  try {
    const r = await apiPost("/api/analysis/runtimes/detect", {});
    if (!request.current()) return;
    const data = await r.json();
    const runtimes = data && data.runtimes && typeof data.runtimes === "object"
      ? data.runtimes : null;
    if (!runtimes) {
      renderAnalysisBody(body, null, "检测返回异常（不是预期的候选结构）。");
      return;
    }
    renderAnalysisBody(body, { detect: runtimes }, "");
  } catch (e) {
    if (!request.current()) return;
    const msg = e.status === 0
      ? "无法连接本地服务，检测暂不可用。"
      : `检测失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`;
    renderAnalysisBody(body, { detectError: msg }, "");
  }
}

/** 检测中的过渡渲染（按钮禁用 + 状态行），保留当前授权状态 chip。 */
function renderAnalysisDetecting(body) {
  const status = body.querySelector("[data-test='analysis-detect-status']");
  const btn = document.getElementById("btn-analysis-detect");
  if (btn) btn.disabled = true;
  if (status) {
    status.innerHTML = `<span class="dr-loading" data-dr-spin aria-hidden="true"></span>正在检测（只读探测，最多约 20 秒）…`;
  }
}

/** 授权确认层：发送对象/保存与撤销/认证待确认说明；确认才 PUT。 */
function confirmAnalysisPick(body, runtimeId, detectResult) {
  const layer = body.querySelector("[data-test='analysis-confirm-layer']");
  const info = detectResult ? detectResult[runtimeId] : null;
  if (!layer) return;
  const name = (info && info.display_name) || runtimeId;
  const auth = info ? ANALYSIS_AUTH_BADGES[info.auth_status] : ANALYSIS_AUTH_BADGES.unknown;
  const authNote = auth && auth.text === "认证待确认"
    ? `<p class="hint">该引擎的登录状态暂无法确认（版本探测成功不代表已登录）；
        首次分析如遇认证问题，分析会失败并给出原因。</p>`
    : "";
  layer.hidden = false;
  layer.innerHTML = `
    <p class="hint exclude-warning">即将授权使用 <strong>${escapeHtml(name)}</strong> 作为变化解读引擎：</p>
    <p class="hint">发送对象：分析时，Fathom 把两个快照的脱敏目录事实（目录路径与大小，无文件内容）
      交给本机的 ${escapeHtml(name)} 命令行；该命令行可能把数据发送给它配置的模型服务。
      账号与额度由该命令行管理，Fathom 不做密钥与模型配置。</p>
    <p class="hint">本地保存：解读结果与批准发送的事实包保存在本机运行目录，
      可随时在变化页撤销；撤销不能清除该命令行或模型服务自身的留存。</p>
    ${authNote}
    <div class="exclude-actions">
      <button type="button" id="btn-analysis-confirm-yes" class="btn primary" data-test="analysis-confirm-yes">确认授权</button>
      <button type="button" id="btn-analysis-confirm-no" data-test="analysis-confirm-no">取消</button>
    </div>`;
  const no = document.getElementById("btn-analysis-confirm-no");
  if (no) no.addEventListener("click", () => { layer.hidden = true; layer.innerHTML = ""; });
  const yes = document.getElementById("btn-analysis-confirm-yes");
  if (yes) {
    yes.addEventListener("click", async () => {
      yes.disabled = true;
      await saveAnalysisAuthorization(body, {
        enabled: true,
        runtime: {
          id: runtimeId,
          executable: info?.executable || "",
          version: info?.version ?? null,
        },
      }, `已授权 ${name}。变化页已生成的发送预览即失效（如有），下次分析前需重新生成预览。`);
    });
  }
}

/** 关闭授权确认层：关闭会取消在途分析（后端行为），已保存解读保留可撤销。 */
function confirmAnalysisDisable(body) {
  const layer = body.querySelector("[data-test='analysis-confirm-layer']");
  if (!layer) return;
  layer.hidden = false;
  layer.innerHTML = `
    <p class="hint exclude-warning">即将关闭 AI 解读：在途分析会被取消；已保存的解读保留，可随时撤销。</p>
    <div class="exclude-actions">
      <button type="button" id="btn-analysis-disable-yes" class="btn primary" data-test="analysis-disable-yes">确认关闭</button>
      <button type="button" id="btn-analysis-disable-no" data-test="analysis-disable-no">取消</button>
    </div>`;
  const no = document.getElementById("btn-analysis-disable-no");
  if (no) no.addEventListener("click", () => { layer.hidden = true; layer.innerHTML = ""; });
  const yes = document.getElementById("btn-analysis-disable-yes");
  if (yes) {
    yes.addEventListener("click", async () => {
      yes.disabled = true;
      await saveAnalysisAuthorization(body, { enabled: false },
        "已关闭 AI 解读。在途分析已请求取消；已保存的解读保留，可在变化页撤销。");
    });
  }
}

/** 授权写入（PUT /api/config 的 analysis 键，部分合并）：成功后重读生效值
 * 重渲染；失败保持旧值可辨（不重渲染授权态，只显示错误——与 ISS-108
 * 「保存失败必须保持旧值可辨」同口径）。 */
async function saveAnalysisAuthorization(body, patch, successNote) {
  try {
    await apiPut("/api/config", { analysis: patch });
    let fresh = null;
    try { fresh = await fetchJSON("/api/config"); } catch { /* 重读失败不阻塞成功反馈 */ }
    if (fresh) lastConfig = fresh;
    renderAnalysisBody(body, { note: successNote }, "");
  } catch (e) {
    const msg = e.status === 0
      ? "保存失败：无法连接本地服务，当前授权保持不变。"
      : `保存失败：${e.message} 当前授权保持不变。`;
    renderAnalysisBody(body, { error: msg }, "");
  }
}

function renderAnalysisBody(body, extra, unusedNote) {
  const cfgAnalysis = lastConfig?.analysis || null;
  analysisStateChip(cfgAnalysis);
  renderAnalysisRuntimes(body, cfgAnalysis, extra?.detect || null, false);
  const note = body.querySelector("[data-test='analysis-save-note']");
  if (extra?.note && note) {
    const ok = document.createElement("p");
    ok.className = "hint cfg-ok";
    ok.setAttribute("data-test", "analysis-note");
    ok.textContent = extra.note;
    note.after(ok);
  }
  if (extra?.error) {
    const err = document.createElement("p");
    err.className = "hint cfg-error";
    err.setAttribute("data-test", "analysis-error");
    err.textContent = extra.error;
    note?.after(err);
  }
  if (extra?.detectError) {
    const err = document.createElement("p");
    err.className = "hint cfg-error";
    err.setAttribute("data-test", "analysis-detect-error");
    err.textContent = extra.detectError;
    body.querySelector("[data-test='analysis-detect-status']")?.after(err);
  }
  // 关闭入口（已启用才显示；与授权确认同层互斥）
  if (cfgAnalysis?.enabled) {
    const row = document.createElement("div");
    row.className = "perm-link-row";
    row.innerHTML = `<button type="button" class="btn" data-test="analysis-disable-btn">${icon("trash", 14)} 关闭 AI 解读</button>`;
    body.querySelector("[data-test='analysis-confirm-layer']")?.before(row);
    row.querySelector("[data-test='analysis-disable-btn']").addEventListener("click", () => confirmAnalysisDisable(body));
  }
}

async function loadAnalysisSettings() {
  const panel = _ensureAnalysisPanel();
  if (!panel) return;
  const body = panel.querySelector("[data-test='analysis-panel-body']");
  if (!body) return;
  const request = beginRequest("settingsAnalysis");
  let c = lastConfig;
  if (!c) {
    try {
      c = await fetchJSON("/api/config");
      if (!request.current()) return;
      lastConfig = c;
    } catch (e) {
      if (!request.current()) return;
      body.innerHTML = `<p class="hint">${escapeHtml(e.status === 0
        ? "无法连接本地服务，AI 分析设置暂不可用。"
        : `AI 分析设置加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`)} 可稍后重试；变化页基础事实不受影响。</p>`;
      return;
    }
  }
  renderAnalysisBody(body, {});
}

/* ===== 后台自启（ISS-010B）=====
 * 发行态 launchd 注册桥的设置页接线：开关开启前必须解释并征求同意
 * （展示将写入的 plist 摘要与 launchctl 命令清单），取消/失败回落原状态；
 * 关闭开关走注销。状态查询复用 ISS-010A 只读桥（autostart_status）。
 * - Tauri 桥可用时经 invoke 调 autostart_register_plan / autostart_register /
 *   autostart_unregister；浏览器模式（无桥）如实降级为只读说明。
 * - 所有 invoke 结果都做形状校验：桥异常/mock 桥返回 undefined 时按
 *   「状态未知」渲染，不抛未捕获异常。
 * - DOM 在本文件内联创建（与权限面板同模式），零 emoji，无构建链。 */
const AUTOSTART_PANEL_ID = "autostart-panel";

const AUTOSTART_STATE_LABELS = {
  enabled: "运行中",
  disabled: "未注册",
  unknown: "未知",
};

/* ===== 计划一致性（ISS-016B）=====
 * 消费 GET/PUT /api/config 的 service_reload_state（后端只读漂移检测）：
 * - 四态文案 in_sync/drift/not_registered/unknown，无 emoji、未知不猜测；
 * - drift 态出现「重新安装计划」入口，复用 010B autostart 确认层
 *   （展示计划 → 确认 → 执行 → 状态回读）；取消/失败后开关与状态回读
 *   系统真值（与 010B 面板同口径）；
 * - 浏览器模式（无桥）如实降级为只读说明，不渲染假入口；
 * - 接口未返回该字段（旧后端/mock 桥）时不渲染，不伪造状态。 */
function reloadStateInfo(sr) {
  const reg = sr && typeof sr.registered_scan_time === "string" ? sr.registered_scan_time : "未知";
  const cur = sr && typeof sr.current_scan_time === "string" ? sr.current_scan_time : "未知";
  switch (sr && sr.state) {
    case "in_sync":
      return { text: `计划时间一致：已注册计划 ${reg} 与当前设置相同，无需重装。`, reinstall: false };
    case "drift":
      return {
        text: `计划时间不一致：已注册计划 ${reg}，当前设置 ${cur}；保存后的新计划尚未安装，需重新安装计划才生效。`,
        reinstall: true,
      };
    case "not_registered":
      return { text: "尚未注册后台计划：可先在下方开启后台自启，再核对计划一致性。", reinstall: false };
    case "unknown":
      return { text: "无法读取已注册计划（读取失败或内容异常）：一致性未知，不猜测。", reinstall: false };
    default:
      return null;
  }
}

function reloadStateSectionHtml() {
  const info = reloadStateInfo(lastConfig && lastConfig.service_reload_state);
  if (!info) return "";
  const invoke = tauriInvoke();
  const button = info.reinstall && invoke
    ? `<div class="perm-link-row"><button type="button" id="btn-reinstall-plan" class="btn" data-test="reinstall-plan-btn">${icon("settings", 12)} 重新安装计划</button></div>`
    : "";
  const browserNote = info.reinstall && !invoke
    ? `<p class="hint" data-test="reload-browser-note">计划已漂移，但重新安装入口只在 Fathom 桌面应用的设置页可用；浏览器模式为只读。</p>`
    : "";
  return `<p class="hint" data-test="reload-state-text">${escapeHtml(info.text)}</p>${button}${browserNote}`;
}

/** 把计划一致性小节渲染进 autostart 面板的挂载点（配置变化后可单独刷新）。 */
function renderReloadSection() {
  const holder = document.querySelector(`#${AUTOSTART_PANEL_ID} [data-test='reload-section']`);
  if (!holder) return;
  holder.innerHTML = reloadStateSectionHtml();
  const btn = document.getElementById("btn-reinstall-plan");
  if (btn) {
    btn.addEventListener("click", () => {
      const body = document.getElementById(AUTOSTART_PANEL_ID)
        ?.querySelector("[data-test='autostart-panel-body']");
      // 复用 010B 确认层：展示计划 → 确认 → 执行 → 状态回读。
      if (body) confirmRegister(body);
    });
  }
}

function tauriInvoke() {
  const t = window.__TAURI__;
  if (t && t.core && typeof t.core.invoke === "function") return t.core.invoke;
  return null;
}

/* ===== 高级与诊断折叠区（ISS-083）=====
 * 打包态（Tauri 桥在位）时创建：技术区块收纳进默认折叠的 <details>，
 * 与 overview 卷趋势的 .tbl-toggle 同为原生 details/summary（键盘可达，
 * Enter/Space 原生支持），零 JS 展开状态。浏览器/开发态不创建任何节点，
 * 运行信息表保持原 8 行全量渲染，服务管理面板保持原位（不回退）。 */
const ADVANCED_PANEL_ID = "advanced-panel";
const TECH_TABLE_ID = "settings-table-tech";

/** 是否运行在桌面壳（打包态）内：与更新/自启面板的桥降级检测同信号。 */
function isPackagedMode() {
  return !!tauriInvoke();
}

/** 找到 index.html 静态的「服务管理」面板（该文件不在本卡白名单内，
 * 打包态以运行时 DOM 移动方式收进折叠区，不改静态结构）。经 h2 反查最近的
 * .panel 祖先定位：面板被移入折叠区后，「服务管理」h2 位于 advanced 面板
 * 子树内，若按 .panel 列表正向匹配会先命中外层容器。 */
function findServicePanel() {
  const page = document.getElementById("page-settings");
  if (!page) return null;
  const h2 = [...page.querySelectorAll(".panel-head h2")]
    .find((node) => node.textContent === "服务管理");
  return h2?.closest(".panel") || null;
}

/** 打包态创建「高级与诊断」面板（幂等）并把服务管理面板移入。
 * ISS-087：原 ISS-083 折叠区整体迁入「高级与诊断」section，挂在
 * #settings-advanced-extra 容器下；服务管理面板同区域。 */
function _ensureAdvancedPanel() {
  const page = document.getElementById("page-settings");
  if (!page) return null;
  let panel = document.getElementById(ADVANCED_PANEL_ID);
  if (!panel) {
    panel = document.createElement("div");
    panel.id = ADVANCED_PANEL_ID;
    panel.className = "panel";
    panel.setAttribute("data-test", "advanced-panel");
    panel.innerHTML = `
      <details class="adv-toggle" data-test="advanced-toggle">
        <summary>${icon("settings", 12)} 高级与诊断${icon("chevron", 14, "adv-chevron")}</summary>
        <div class="adv-body" data-test="advanced-body">
          <p class="hint">面向排障与支持的运行细节：本地服务地址、运行根与数据库位置、
            采集参数、桌面壳形态与服务管理命令。日常使用无需关注。</p>
          <table class="tbl tbl-mini" id="${TECH_TABLE_ID}"><tbody></tbody></table>
        </div>
      </details>`;
    // ISS-087：高级折叠区放进「高级与诊断」section 的容器；
    // 浏览器/开发态不创建任何节点（行全量渲染进 #settings-table）。
    const host = document.getElementById("settings-advanced-extra");
    if (host) host.appendChild(panel);
    else page.appendChild(panel);
  }
  const body = panel.querySelector("[data-test='advanced-body']");
  const service = findServicePanel();
  // 服务管理面板移入折叠区（Element.appendChild 自动从原位置摘除）；
  // 已在区内（重复进入设置页）时跳过，保持插入顺序稳定。
  if (body && service && !body.contains(service)) {
    body.appendChild(service);
  }
  return panel;
}

/** 渲染运行信息表：浏览器态全量渲染进 #settings-table（原行序）；
 * 打包态把用户必要行留在 #settings-table、技术行渲染进折叠区内的
 * #settings-table-tech，并确保折叠区已创建（服务管理面板随区移动）。
 * ISS-087：「运行信息」面板整体迁入「高级与诊断」section；
 * 浏览器/开发态依然全量显示，不回退。 */
function renderRunInfoTables(rows) {
  const rowHtml = (list) => list.map((r) =>
    `<tr><td style="width:140px;color:var(--muted)">${r[0]}</td><td>${r[1]}</td></tr>`).join("");
  const packaged = isPackagedMode();
  if (packaged) _ensureAdvancedPanel();
  const main = document.querySelector("#settings-table tbody");
  if (main) main.innerHTML = rowHtml(packaged ? rows.filter((r) => !r[2]) : rows);
  if (packaged) {
    const tech = document.querySelector(`#${TECH_TABLE_ID} tbody`);
    if (tech) tech.innerHTML = rowHtml(rows.filter((r) => r[2]));
  }
}

function _ensureAutostartPanel() {
  const page = document.getElementById("page-settings");
  if (!page) return null;
  let panel = document.getElementById(AUTOSTART_PANEL_ID);
  if (panel) return panel;
  panel = document.createElement("div");
  panel.id = AUTOSTART_PANEL_ID;
  panel.className = "panel";
  panel.innerHTML = `
    <div class="panel-head">
      <h2>后台自启（launchd）</h2>
      <p class="hint">开启前会展示将写入的 launchd 配置与命令并请求确认；取消或失败都会回到系统当前状态。</p>
    </div>
    <div class="perm-panel" data-test="autostart-panel-body">
      <p class="hint">后台自启状态加载中…</p>
    </div>`;
  // ISS-083：打包态不向普通用户暴露 launchd 术语（开关与状态保留，
  // 仅措辞收敛）；浏览器/开发态保持原文案。
  if (isPackagedMode()) {
    const h2 = panel.querySelector(".panel-head h2");
    if (h2) h2.textContent = "后台自启";
    const headHint = panel.querySelector(".panel-head .hint");
    if (headHint) {
      headHint.textContent = "开启后每日定时扫描并随开机自动运行；开启前会展示将写入的配置与命令并请求确认，取消或失败都会回到当前状态。";
    }
  }
  // ISS-087：后台自启归「计划与通知」section（与扫描计划、权限、自启同组）。
  const host = document.getElementById("settings-schedule-extra");
  if (host) host.appendChild(panel);
  else page.appendChild(panel);
  return panel;
}

function autostartStateText(record) {
  const scan = AUTOSTART_STATE_LABELS[record?.scan] || "未知";
  const web = AUTOSTART_STATE_LABELS[record?.web] || "未知";
  const login = AUTOSTART_STATE_LABELS[record?.login_item] || "未知";
  const base = `定时扫描：${scan} · 常驻服务：${web} · 登录项：${login}`;
  // 「SMAppService 未接」是实现注脚：打包态不展示（登录项未知如实保留），
  // 开发态保留原文案供排障。
  return isPackagedMode() ? base : `${base}（SMAppService 未接，恒未知）`;
}

/** 开关的呈现态由系统状态推导：两标签 enabled → on；两标签 disabled → off；
 * 混合/未知 → "part"（不猜测，如实显示）。 */
function autostartSwitchState(record) {
  if (!record) return "unknown";
  if (record.scan === "enabled" && record.web === "enabled") return "on";
  if (record.scan === "disabled" && record.web === "disabled") return "off";
  return "part";
}

function renderAutostartBody(body, record, extraNote) {
  const invoke = tauriInvoke();
  if (!invoke) {
    body.innerHTML = `
      <p class="hint">浏览器模式没有桌面壳桥接：后台自启的查询与注册/注销只在 Fathom 桌面应用的设置页可用。</p>
      <p class="hint">命令行开发态仍用 <code>python -m fathom install / uninstall</code>。</p>
      <div data-test="reload-section"></div>`;
    renderReloadSection();
    return;
  }
  const sw = autostartSwitchState(record);
  const swLabel = sw === "on" ? "已开启" : sw === "off" ? "已关闭" : sw === "part" ? "部分注册（状态不一致）" : "未知";
  body.innerHTML = `
    <p class="perm-state">
      <span class="quality-chip ${sw === "on" ? "ok" : sw === "off" ? "miss" : "warn"}" data-test="autostart-chip">
        ${icon("activity", 12)} ${escapeHtml(swLabel)}
      </span>
    </p>
    <p class="hint" data-test="autostart-status-text">${escapeHtml(autostartStateText(record))}</p>
    ${extraNote ? `<p class="hint cfg-error" data-test="autostart-note">${escapeHtml(extraNote)}</p>` : ""}
    <div class="perm-link-row">
      <label class="exclude-confirm-label">
        <input type="checkbox" id="autostart-toggle" data-test="autostart-toggle"
               ${sw === "on" ? "checked" : ""}>
        <span>开启后台自启（定时扫描 + 常驻服务开机自动运行）</span>
      </label>
    </div>
    <div id="autostart-confirm" data-test="autostart-confirm" hidden></div>
    <div data-test="reload-section"></div>`;
  const toggle = document.getElementById("autostart-toggle");
  if (toggle) {
    // 变更不立即生效：先弹解释+确认层；取消/失败后由真实系统状态回写开关。
    toggle.addEventListener("change", () => {
      if (toggle.checked) confirmRegister(body);
      else confirmUnregister(body);
    });
  }
  renderReloadSection();
}

/** 开关打开：解释并征求同意（plist 摘要 + 命令清单），确认后才 invoke 注册。 */
async function confirmRegister(body) {
  const toggle = document.getElementById("autostart-toggle");
  const layer = document.getElementById("autostart-confirm");
  const invoke = tauriInvoke();
  if (!layer || !invoke) return;
  let plan = null;
  try {
    plan = await invoke("autostart_register_plan",
      { scanTime: (lastConfig && lastConfig.scan_time) || null });
  } catch (e) {
    renderAutostartBody(body, null, `无法生成注册计划：${e?.message || e}`);
    return;
  }
  if (!plan || !Array.isArray(plan.plist_files) || !Array.isArray(plan.commands)) {
    renderAutostartBody(body, null, "注册计划返回异常（不是预期的清单结构），已取消。");
    return;
  }
  const plists = plan.plist_files.map((p) => `
    <li>将写入 <code>${escapeHtml(p.label)}</code> → <code>${escapeHtml(p.path)}</code></li>`).join("");
  const cmds = plan.commands.map((c) => `<code>${escapeHtml((c || []).join(" "))}</code>`).join("<br>");
  layer.hidden = false;
  layer.innerHTML = `
    <p class="hint">即将在 <code>~/Library/LaunchAgents</code> 写入两份 plist 并执行 launchd 注册：</p>
    <ul>${plists}</ul>
    <p class="hint">将执行的命令（uid 以实际值替换）：</p>
    <p class="hint">${cmds}</p>
    <p class="hint exclude-warning">开启后：每日定时扫描自动运行，常驻服务开机自动拉起（崩溃自动重启）。写入与注册失败会自动回滚，不留半注册状态。</p>
    <div class="exclude-actions">
      <button type="button" id="autostart-confirm-yes" class="btn primary" data-test="autostart-confirm-yes">确认开启</button>
      <button type="button" id="autostart-confirm-no" class="btn" data-test="autostart-confirm-no">取消</button>
    </div>`;
  const yes = document.getElementById("autostart-confirm-yes");
  const no = document.getElementById("autostart-confirm-no");
  if (no) no.addEventListener("click", () => { closeConfirmAndResync(body); });
  if (yes) {
    yes.addEventListener("click", async () => {
      yes.disabled = true;
      try {
        const outcome = await invoke("autostart_register",
          { confirmed: true, scanTime: (lastConfig && lastConfig.scan_time) || null });
        if (outcome && outcome.ok === true) {
          await refreshAutostart(body, "已开启后台自启。");
        } else {
          const why = outcome && outcome.error ? outcome.error : "未知原因";
          await refreshAutostart(body, `注册未完成：${why}`);
        }
      } catch (e) {
        await refreshAutostart(body, `注册请求失败：${e?.message || e}`);
      }
    });
  }
  if (toggle) toggle.checked = true; // 计划展示期间保持打开；取消/失败经 resync 回落
}

/** 开关关闭：确认层（说明注销动作），确认后 invoke 注销。 */
function confirmUnregister(body) {
  const layer = document.getElementById("autostart-confirm");
  const invoke = tauriInvoke();
  if (!layer || !invoke) return;
  layer.hidden = false;
  layer.innerHTML = `
    <p class="hint">即将停止并注销后台自启：对两个标签执行 <code>launchctl bootout</code>，并删除
      <code>~/Library/LaunchAgents</code> 下的两份 plist。已入库的扫描数据不会被删除。</p>
    <div class="exclude-actions">
      <button type="button" id="autostart-unregister-yes" class="btn primary" data-test="autostart-unregister-yes">确认关闭</button>
      <button type="button" id="autostart-unregister-no" class="btn" data-test="autostart-unregister-no">取消</button>
    </div>`;
  const yes = document.getElementById("autostart-unregister-yes");
  const no = document.getElementById("autostart-unregister-no");
  if (no) no.addEventListener("click", () => { closeConfirmAndResync(body); });
  if (yes) {
    yes.addEventListener("click", async () => {
      yes.disabled = true;
      try {
        const outcome = await invoke("autostart_unregister", { confirmed: true });
        if (outcome && outcome.ok === true) {
          await refreshAutostart(body, "已关闭后台自启。");
        } else {
          const why = outcome && outcome.error ? outcome.error : "未知原因";
          await refreshAutostart(body, `注销未完成：${why}`);
        }
      } catch (e) {
        await refreshAutostart(body, `注销请求失败：${e?.message || e}`);
      }
    });
  }
}

/** 取消确认或操作结束：重新只读查询系统状态并按其回写开关（UI 与系统一致）。 */
async function refreshAutostart(body, note) {
  const invoke = tauriInvoke();
  if (!invoke) return;
  let record = null;
  try {
    record = await invoke("autostart_status", {});
  } catch (e) {
    renderAutostartBody(body, null, `${note || ""}（状态回读失败：${e?.message || e}）`);
    return;
  }
  if (!record || typeof record.scan !== "string" || typeof record.web !== "string") {
    renderAutostartBody(body, null, `${note || ""}（状态回读异常）`);
    return;
  }
  // 重装/取消后同步刷新计划一致性（重装成功应回到 in_sync）；刷新失败
  // 不影响系统状态回读（开关与真值一致优先于漂移文案更新）。
  try {
    const fresh = await fetchJSON("/api/config");
    lastConfig = fresh;
    renderEffective(fresh);
  } catch { /* 配置刷新失败不影响状态回读 */ }
  renderAutostartBody(body, record, note);
}

function closeConfirmAndResync(body) {
  const layer = document.getElementById("autostart-confirm");
  if (layer) { layer.hidden = true; layer.innerHTML = ""; }
  refreshAutostart(body, "");
}

async function loadAutostart() {
  const panel = _ensureAutostartPanel();
  if (!panel) return;
  const body = panel.querySelector("[data-test='autostart-panel-body']");
  if (!body) return;
  const request = beginRequest("settingsAutostart");
  const invoke = tauriInvoke();
  if (!invoke) {
    if (!request.current()) return;
    renderAutostartBody(body, null);
    return;
  }
  let record = null;
  try {
    record = await invoke("autostart_status", {});
    if (!request.current()) return;
  } catch (e) {
    if (!request.current()) return;
    renderAutostartBody(body, null, `状态查询失败：${e?.message || e}`);
    return;
  }
  if (!request.current()) return;
  if (!record || typeof record.scan !== "string" || typeof record.web !== "string") {
    renderAutostartBody(body, null, "状态返回异常（不是预期的三态结构）。");
    return;
  }
  renderAutostartBody(body, record);
}

/* ===== 应用更新（ISS-040B；ISS-102 阶段/字节进度/下载取消；ISS-113 无感化）=====
 * 呈现收敛（ISS-113，参照 Folia AboutSection 的 UpdateSnapshot 语义）：
 * 默认两行内——「当前版本 X · 状态一句话」+ 动作行（检查更新 / 自动下载
 * 开关）；长说明收进「了解详情」展开（ISS-108 details 模式）。状态语义：
 * 检查中 / 下载中（进度%）/ 已就绪（安装（需重启）确认）/ 最新 / 失败（重试）。
 * - 自动下载（ISS-113）：开关默认开（/api/config 的 auto_download_updates，
 *   经既有 PUT /api/config 写 settings.json；Rust 壳只读同一文件）。开启时
 *   检查发现 available 由壳自动后台下载（downloading 事件复用既有进度呈现，
 *   可取消）；下载完成 downloaded 事件 → ready 态，用户唯一动作 = 「安装
 *   （需重启）」确认。关闭则回 040B 现状：available + 「下载并安装」确认层。
 * - 不静默（边界保留）：下载安装与重启都需显式确认（confirmed=true 才在壳内
 *   执行）；确认层收起（确认前取消）不算下载取消，不发 updater-cancel-requested。
 * - 安装事务阶段事件（ISS-102）：preparing/downloading/installing/verifying/
 *   finalizing；下载字节进度（total 未知时只显示字节数，不伪造百分比）；
 *   下载阶段可取消（emit 既有 updater-cancel-requested）；installing 后取消
 *   不可触发并说明原因；cancelled/failed 呈现原因与重试入口（候选保留）；
 *   安装成功「重启以完成」独立确认；取消/失败全部回落可恢复态。
 * - Tauri 桥可用时经 invoke 调 updater_check/updater_install/updater_restart；
 *   浏览器模式（无桥）如实降级为只读说明，不渲染假入口。invoke 结果做形状
 *   校验；未知事件不猜测、不覆盖既有呈现。
 * - DOM 在本文件内联创建（与 autostart 面板同模式），零 emoji，无构建链。 */
const UPDATER_PANEL_ID = "updater-panel";

/* 检查态标签（updater_check 返回与检查类事件共用）。 */
const UPDATER_STATE_LABELS = {
  unconfigured: "未配置更新源：应用内更新不可用",
  unreachable: "更新源不可达：当前更新源处于关闭状态，可稍后重试",
  up_to_date: "已是最新版本",
  available: "有可用更新",
  installed: "更新已安装，重启后生效",
  failed: "更新检查失败",
};

/* ISS-113：ready 态一句话（downloaded 事件；字节已下载并验签）。 */
const UPDATER_READY_LABEL = "已下载就绪";

/* ISS-102：安装事务阶段标签（updater-state 事件，lib.rs UpdaterInstallPhase
 * 合同）。preparing/downloading/installing/verifying/finalizing 为进行中，
 * cancelled/failed 为终态；前端不猜测未登记的阶段（未知事件按状态异常处理
 * 不覆盖呈现）。verifying/finalizing 目前 Rust 事务中不发独立事件，但阶段
 * 属于既有合同（任务卡），前端先行呈现，事件到达即显示。 */
const UPDATER_PHASE_LABELS = {
  preparing: "正在准备升级（停写、备份数据并落盘升级日志）",
  downloading: "正在下载更新",
  installing: "正在安装更新",
  verifying: "正在核验新版本身份",
  finalizing: "正在收尾升级事务",
  cancelled: "下载已取消",
  failed: "更新失败",
};

/* 取消边界（与 lib.rs UpdaterInstallPhase::allows_cancel 同合同）：仅
 * preparing/downloading 受理取消。payload 显式 cancellable=false 优先
 * （取消拒绝事件），缺省时按阶段推导。 */
const UPDATER_CANCELLABLE_PHASES = new Set(["preparing", "downloading"]);

/* 进入安装后不可取消的说明（与 lib.rs UPDATER_INSTALL_NOT_CANCELLABLE_HINT
 * 同源；Rust 侧 installing 事件自带 hint，缺省时前端用本兜底文案）。 */
const UPDATER_INSTALL_BLOCKED_HINT =
  "已进入安装阶段，不可取消；若安装失败将自动回滚到当前版本";

/* 取消请求事件名（与 lib.rs UPDATER_CANCEL_EVENT 同合同）。 */
const UPDATER_CANCEL_EVENT_NAME = "updater-cancel-requested";

let lastUpdaterStatus = null;  // 最近一次检查态（确认层取消后的回落源）
let updaterInstall = null;     // 最近一次安装事务呈现（updater-state 事件驱动；null=无事务）
let updaterCancelRequested = false;  // 取消请求已发出（按钮转「正在取消…」，cancelled 后复位）
let updaterPendingNote = null; // 已确认但首个阶段事件未到达前的过渡说明（不伪造阶段名）
let updaterReady = null;       // ISS-113：downloaded 事件载荷（后台下载完成，待安装确认；null=无）
let updaterChecking = false;   // ISS-113：检查请求在途（「正在检查更新」一句话）
let updaterGeneration = -1, updaterRevision = -1;
let updaterRecoveryEpoch = 0;
let updaterEventSequence = 0;
let updaterListener = null;
let updaterToggleNote = null;  // ISS-113：自动下载开关保存失败的就近提示（成功即清）

/** ISS-113：自动下载开关当前生效值（/api/config 的 auto_download_updates；
 * 后端默认 true，未到达/缺键时按默认开呈现——与 Rust 侧宽读同口径）。 */
function updaterAutoDownload() {
  const value = lastConfig?.auto_download_updates;
  return typeof value === "boolean" ? value : true;
}

function _ensureUpdaterPanel() {
  const page = document.getElementById("page-settings");
  if (!page) return null;
  let panel = document.getElementById(UPDATER_PANEL_ID);
  if (panel) return panel;
  panel = document.createElement("div");
  panel.id = UPDATER_PANEL_ID;
  panel.className = "panel";
  panel.innerHTML = `
    <div class="panel-head">
      <h2>应用更新</h2>
      <p class="hint">有新版本时自动在后台下载；安装与重启需要你确认。详情见下方「了解详情」。</p>
    </div>
    <div class="perm-panel" data-test="updater-panel-body">
      <p class="hint">应用更新状态加载中…</p>
    </div>`;
  // ISS-087：应用更新区归「关于」section（与版本/更新流程/致谢同组，
  // 参照 Folia AboutSection 的「检查更新」位），挂在 #settings-about-extra 容器下。
  const host = document.getElementById("settings-about-extra");
  if (host) host.appendChild(panel);
  else page.appendChild(panel);
  return panel;
}

/** 状态行文案：标签 + 失败态的壳侧错误详情（不吞错）。 */
function updaterStatusLine(status) {
  const label = UPDATER_STATE_LABELS[status?.state] || "状态未知";
  const version = status && typeof status.current_version === "string"
    ? status.current_version : "未知";
  const error = status?.state === "unreachable" || status?.state === "failed"
    ? (status?.error ? `（${status.error}）` : "")
    : "";
  return `当前版本 ${version} · ${label}${error}`;
}

/** ISS-113：状态行首行（两行合同的行 1）——检查中 / 安装事务 / ready /
 * 检查态 / 尚未检查，五个语境同一「当前版本 X · 一句话」形态。版本兜底序：
 * 检查态 → ready 载荷 → /api/status 的 app_version（页面加载即回填）。 */
function updaterHeadLine(status, install, ready, checking) {
  const version = (typeof status?.current_version === "string" && status.current_version)
    || (typeof ready?.current_version === "string" && ready.current_version)
    || (typeof lastStatusAppVersion === "string" && lastStatusAppVersion)
    || "未知";
  if (checking) return `当前版本 ${version} · 正在检查更新…`;
  if (install) return updaterInstallLine(install, status);
  if (ready) {
    const target = ready.available_version || "未知";
    return `当前版本 ${version} · 新版本 ${target} ${UPDATER_READY_LABEL}`;
  }
  if (status) return updaterStatusLine(status);
  return `当前版本 ${version} · 尚未检查更新`;
}

/** 安装事务进行中的阶段行（ISS-102）：检查态版本优先，事件载荷兜底；
 * 终态不加「目标」后缀（目标版本已在可用区块呈现）。 */
function updaterInstallLine(install, status) {
  const label = UPDATER_PHASE_LABELS[install.state] || "更新进行中";
  const cur = typeof install.current_version === "string" ? install.current_version
    : (typeof status?.current_version === "string" ? status.current_version : "未知");
  const terminal = install.state === "cancelled" || install.state === "failed";
  const target = install.available_version || status?.available_version;
  return `当前版本 ${cur} · ${label}${!terminal && target ? `（目标 ${target}）` : ""}`;
}

/** 取消边界：payload 显式 cancellable 优先，缺省按阶段推导（合同见
 * UPDATER_CANCELLABLE_PHASES 注释）。终态不可取消。 */
function updaterPhaseCancellable(install) {
  if (!install) return false;
  if (install.state === "cancelled" || install.state === "failed") return false;
  if (typeof install.cancellable === "boolean") return install.cancellable;
  return UPDATER_CANCELLABLE_PHASES.has(install.state);
}

/** 下载字节进度（ISS-102 合同）：downloading 且 downloaded 为有限非负数时
 * 渲染。total 已知：进度条（role=progressbar）+「已下载 X / Y（P%）」，
 * 百分比按真实字节比计算（下载量钳制在 total 内防越界）；total 未知
 * （null/缺失）：只显示字节数并明示「总大小未知」，不渲染进度条、
 * 不伪造百分比。 */
function updaterProgressHtml(install) {
  if (!install || install.state !== "downloading") return "";
  const downloaded = Number(install.downloaded);
  if (!Number.isFinite(downloaded) || downloaded < 0) return "";
  const totalNum = Number(install.total);
  const hasTotal = Number.isFinite(totalNum) && totalNum > 0;
  if (!hasTotal) {
    return `<p class="hint updater-progress" data-test="updater-progress"
      data-downloaded="${downloaded}">已下载 ${escapeHtml(fmtBytes(downloaded))}（总大小未知，不显示百分比）</p>`;
  }
  const clamped = Math.min(downloaded, totalNum);
  const pct = Math.floor((clamped / totalNum) * 100);
  return `<div class="updater-progress" data-test="updater-progress"
      data-downloaded="${clamped}" data-total="${totalNum}" data-percent="${pct}">
    <div class="updater-progress-bar" role="progressbar" aria-label="更新下载进度"
      aria-valuemin="0" aria-valuemax="${totalNum}" aria-valuenow="${clamped}"
      aria-valuetext="已下载 ${escapeHtml(fmtBytes(clamped))}，共 ${escapeHtml(fmtBytes(totalNum))}">
      <div class="updater-progress-fill" style="width:${pct}%"></div>
    </div>
    <p class="hint">已下载 ${escapeHtml(fmtBytes(clamped))} / ${escapeHtml(fmtBytes(totalNum))}（${pct}%）</p>
  </div>`;
}

/** 安装事务呈现（ISS-102）：下载进度 + 取消按钮/不可取消说明 + 终态原因
 * 与重试语境。阶段名由状态行承载（updater-status-text），此处不重复。 */
function updaterInstallHtml(install, status) {
  const terminal = install.state === "cancelled" || install.state === "failed";
  const cancellable = !terminal && updaterPhaseCancellable(install);
  const hint = terminal
    ? ""
    : (typeof install.hint === "string" && install.hint
      ? install.hint
      : (cancellable ? "" : UPDATER_INSTALL_BLOCKED_HINT));
  const cancelBtn = cancellable
    ? `<button type="button" id="btn-updater-cancel" class="btn" data-test="updater-cancel-btn"
        aria-label="取消下载更新"${updaterCancelRequested ? " disabled" : ""}>${updaterCancelRequested ? "正在取消…" : "取消下载"}</button>`
    : "";
  /* cancelled 的 Rust hint 与本文案同义（「旧版本保持运行，可再次安装」），
   * 不重复拼接；failed 的 hint 是失败类别的补充恢复提示（与 error 不重复），保留。 */
  const terminalText = install.state === "cancelled"
    ? `下载已取消：旧版本保持运行，候选保留，可再次安装。`
    : `更新失败：${install.error || "未知原因"}${install.hint ? `（${install.hint}）` : ""}`;
  const terminalBlock = terminal
    ? `<p class="hint ${install.state === "failed" ? "cfg-error" : ""}" data-test="updater-terminal" data-kind="${escapeHtml(install.kind || "")}">${escapeHtml(terminalText)}</p>`
    : "";
  return `
    ${updaterProgressHtml(install)}
    ${hint ? `<p class="hint" data-test="updater-phase-hint">${escapeHtml(hint)}</p>` : ""}
    ${terminalBlock}
    ${cancelBtn ? `<div class="perm-link-row">${cancelBtn}</div>` : ""}`;
}

function renderUpdaterBody(body, status, install, extraNote) {
  const invoke = tauriInvoke();
  if (!invoke) {
    body.innerHTML = `
      <p class="hint" data-test="updater-browser-note">浏览器模式没有桌面壳桥接：应用更新的检查与安装只在 Fathom 桌面应用的设置页可用。</p>
      <p class="hint">版本以安装的应用为准；命令行开发态的版本见 <code>fathom --version</code>。</p>`;
    return;
  }
  const terminal = install && (install.state === "cancelled" || install.state === "failed");
  const busy = !!install && !terminal;  // 安装事务进行中：检查/安装入口收起
  const ready = !install && updaterReady ? updaterReady : null;
  const auto = updaterAutoDownload();
  const line = updaterHeadLine(status, install, ready, updaterChecking);
  /* available 区块：无事务/事务终态且未 ready 时呈现检查结果；终态保留——
   * cancelled/failed 的候选仍有效，同一入口即重试（Rust 侧候选保留、独占
   * 门已释放）。文案按开关分流：开（默认）只说明后台下载去向；关则回
   * 040B 现状——手动「下载并安装」确认层。 */
  const showAvailable = (!install || terminal) && !ready && status?.state === "available";
  const available = showAvailable
    ? `
    <p class="hint" data-test="updater-available">
      可更新到 <code>${escapeHtml(String(status.available_version || "未知"))}</code>；${auto
        ? "已开启自动下载，即将在后台下载，完成后在此确认安装。"
        : "下载与安装需要你确认，安装完成后需重启应用。"}
    </p>`
    : "";
  /* ready 区块（ISS-113）：下载完成（已验签），用户唯一动作 = 安装（需重启）确认。 */
  const readyBlock = ready
    ? `
    <p class="hint" data-test="updater-ready">
      新版本 <code>${escapeHtml(String(ready.available_version || "未知"))}</code> 已下载并验签；点击「安装（需重启）」开始安装。
    </p>`
    : "";
  const installed = !install && !ready && status?.state === "installed"
    ? `
    <p class="hint" data-test="updater-installed">已安装 <code>${escapeHtml(String(status.available_version || "新版本"))}</code>；重启前保持当前版本运行。</p>`
    : "";
  /* 动作行上下文主按钮：ready → 安装（需重启）；开关关 + available →
   * 下载并安装（040B 现状）；开关开 + 下载终态（无 ready 字节）→ 重试
   * 下载（重新检查即由壳再触发后台下载）；installed → 重启以完成。 */
  const readyBtn = ready
    ? `<button type="button" id="btn-updater-install-ready" class="btn primary" data-test="updater-install-ready-btn">安装（需重启）</button>`
    : "";
  const installBtn = (!busy && !ready && !auto && showAvailable)
    ? `<button type="button" id="btn-updater-install" class="btn primary" data-test="updater-install-btn">下载并安装</button>`
    : "";
  const retryDownloadBtn = (!busy && !ready && auto && terminal)
    ? `<button type="button" id="btn-updater-retry-download" class="btn" data-test="updater-retry-download-btn">重试下载</button>`
    : "";
  const restartBtn = !install && !ready && status?.state === "installed"
    ? `<button type="button" id="btn-updater-restart" class="btn primary" data-test="updater-restart-btn">重启以完成</button>`
    : "";
  /* 了解详情（ISS-108 details 模式）：长说明与更新说明（notes）收进默认
   * 折叠区，默认视图保持两行内（状态一句话 + 动作行）。 */
  const details = `
    <details class="cfg-details" data-test="updater-details">
      <summary>了解详情</summary>
      ${status?.notes ? `<p class="cfg-desc" data-test="updater-notes">更新说明：${escapeHtml(status.notes)}</p>` : ""}
      <p class="cfg-desc">安装包经内置公钥验签（不可关闭）；下载阶段可取消，进入安装后不可取消，失败会自动回滚到当前版本。
        安装会先暂停写入并自动备份数据，完成后需重启应用才切换到新版本。启动后会自动检查一次更新；
        「自动下载更新」开启时，发现新版本即在后台下载，安装始终需要你确认。任何失败路径下，旧版本与已入库数据都不受影响。</p>
    </details>`;
  body.innerHTML = `
    <p class="hint" data-test="updater-status-text">${escapeHtml(line)}</p>
    ${install ? updaterInstallHtml(install, status) : ""}
    ${available}
    ${readyBlock}
    ${installed}
    ${updaterPendingNote ? `<p class="hint" data-test="updater-pending-note">${escapeHtml(updaterPendingNote)}</p>` : ""}
    ${updaterToggleNote ? `<p class="hint cfg-error" data-test="updater-toggle-note">${escapeHtml(updaterToggleNote)}</p>` : ""}
    ${extraNote ? `<p class="hint cfg-error" data-test="updater-note">${escapeHtml(extraNote)}</p>` : ""}
    <div class="perm-link-row">
      <button type="button" id="btn-updater-check" class="btn" data-test="updater-check-btn"${busy ? " disabled" : ""}>检查更新</button>
      ${readyBtn}
      ${installBtn}
      ${retryDownloadBtn}
      ${restartBtn}
      <label class="exclude-confirm-label updater-auto-label" for="updater-auto-download">
        <input type="checkbox" id="updater-auto-download" data-test="updater-auto-download-toggle"${auto ? " checked" : ""}>
        <span>自动下载更新</span>
      </label>
    </div>
    ${details}
    <div id="updater-confirm" data-test="updater-confirm" hidden></div>
    <div id="updater-restart-confirm" data-test="updater-restart-confirm" hidden></div>`;
  const checkBtn = document.getElementById("btn-updater-check");
  if (checkBtn) checkBtn.addEventListener("click", () => checkUpdater(body));
  const readyBtnNode = document.getElementById("btn-updater-install-ready");
  if (readyBtnNode) readyBtnNode.addEventListener("click", () => confirmUpdaterInstallReady(body));
  const installBtnNode = document.getElementById("btn-updater-install");
  if (installBtnNode) installBtnNode.addEventListener("click", () => confirmUpdaterInstall(body));
  const retryDownloadNode = document.getElementById("btn-updater-retry-download");
  if (retryDownloadNode) retryDownloadNode.addEventListener("click", () => checkUpdater(body, true));
  const restartBtnNode = document.getElementById("btn-updater-restart");
  if (restartBtnNode) restartBtnNode.addEventListener("click", () => confirmUpdaterRestart(body));
  const cancelBtnNode = document.getElementById("btn-updater-cancel");
  if (cancelBtnNode) cancelBtnNode.addEventListener("click", () => requestUpdaterCancel(body));
  const autoToggle = document.getElementById("updater-auto-download");
  if (autoToggle) {
    // 变更即时保存（经既有 PUT /api/config 写 settings.json，壳侧只读）；
    // 失败按 ISS-108 口径恢复旧值可辨（重渲染以生效值回写开关）。
    autoToggle.addEventListener("change", () => {
      saveUpdaterAutoDownload(body, autoToggle.checked);
    });
  }
}

/** ISS-113：保存「自动下载更新」开关（经既有 PUT /api/config；不新增桥
 * 命令）。成功：以返回的生效配置刷新 lastConfig 并重渲染；失败：开关由
 * 重渲染按生效值恢复（旧值可辨），就近显示失败说明，不吞错。 */
async function saveUpdaterAutoDownload(body, checked) {
  try {
    const res = await apiPut("/api/config", { auto_download_updates: checked });
    const data = await res.json();
    if (data && data.config) {
      lastConfig = data.config;
      renderEffective(data.config);
    }
    updaterToggleNote = null;
  } catch (e) {
    updaterToggleNote = e.status === 0
      ? "保存失败：无法连接本地服务，开关已恢复为当前生效值。"
      : `保存失败：${e.message} 开关已恢复为当前生效值。`;
  }
  renderUpdaterBody(body, lastUpdaterStatus, updaterInstall);
}

/** 手动检查（ISS-113 兼「重试下载」入口）：按钮防重入；在途显示「正在检查
 * 更新…」（Folia checking 语义）；结果做形状校验后渲染（失败也是可恢复
 * 状态行）。壳内发现 available 且开关开启时，检查返回即由后台下载事件
 * 推进（downloading → downloaded），前端无需本地触发。 */
async function checkUpdater(body, retry = false) {
  const invoke = tauriInvoke();
  const checkBtn = document.getElementById("btn-updater-check");
  if (!invoke || !checkBtn) return;
  if (updaterChecking) return;
  const epoch = ++updaterRecoveryEpoch;
  checkBtn.disabled = true;
  updaterChecking = true;
  renderUpdaterBody(body, lastUpdaterStatus, updaterInstall);
  let status = null;
  try {
    status = await invoke("updater_check", retry ? { retry: true } : {});
  } catch (e) {
    if (epoch !== updaterRecoveryEpoch) return;
    updaterChecking = false;
    renderUpdaterBody(body, lastUpdaterStatus, updaterInstall, `检查请求失败：${e?.message || e}`);
    return;
  }
  if (epoch !== updaterRecoveryEpoch) return;
  updaterChecking = false;
  checkBtn.disabled = false;
  if (!status || typeof status.state !== "string" || !UPDATER_STATE_LABELS[status.state]) {
    renderUpdaterBody(body, lastUpdaterStatus, updaterInstall, "状态返回异常（不是预期的更新状态结构）。");
    return;
  }
  if (!acceptUpdaterRevision(status)) {
    renderUpdaterBody(body, lastUpdaterStatus, updaterInstall); return;
  }
  // 普通查询不能消费已安装事务；运行中的旧壳版本仍可能检查到同一候选。
  // installed 必须保持到用户明确重启，不能退回 downloaded/再次安装。
  if (lastUpdaterStatus?.state === "installed") {
    renderUpdaterBody(body, lastUpdaterStatus, updaterInstall);
    return;
  }
  lastUpdaterStatus = status;
  // 只有显式重试才放下失败终态；查询结果与失败原因可以同时呈现。
  if (retry) updaterInstall = null;
  if (status.state !== "available") updaterReady = null;
  renderUpdaterBody(body, status, updaterInstall);
  renderAboutVersion();
}

/** 共享安装事务执行（ISS-113 从确认层抽出）：清上一轮终态与 ready 呈现 →
 * 过渡说明 → invoke updater_install（confirmed=true）→ 按返回值渲染
 * （与 updater-state 事件幂等）。进度由事件驱动（ISS-102）。 */
async function runUpdaterInstallTransaction(body, pendingNote, fallbackVersion, fallbackCurrent) {
  const invoke = tauriInvoke();
  if (!invoke) return;
  ++updaterRecoveryEpoch;
  const previousInstall = updaterInstall, previousReady = updaterReady;
  const previousRevision = updaterRevision, previousGeneration = updaterGeneration;
  const previousEventSequence = updaterEventSequence;
  updaterChecking = false;
  // 新一轮事务：清除上一轮终态呈现与取消标记，避免旧终态残留误导
  updaterInstall = null;
  updaterCancelRequested = false;
  updaterReady = null;  // ready 已消费（进入安装事务）
  // 首个阶段事件到达前的过渡说明（事件到达即清除，不伪造阶段名）
  updaterPendingNote = pendingNote;
  renderUpdaterBody(body, lastUpdaterStatus, updaterInstall);
  let outcome = null;
  try {
    outcome = await invoke("updater_install", { confirmed: true });
  } catch (e) {
    updaterPendingNote = null;
    renderUpdaterBody(body, lastUpdaterStatus, updaterInstall, `安装请求失败：${e?.message || e}`);
    return;
  }
  updaterPendingNote = null;
  // busy 是本次命令拒绝，不是已受理事务的新终态；保留已知快照并就近解释。
  if (outcome?.state === "busy") {
    if (updaterRevision === previousRevision && updaterGeneration === previousGeneration
        && updaterEventSequence === previousEventSequence) {
      updaterInstall = previousInstall;
      updaterReady = previousReady;
    }
    renderUpdaterBody(body, lastUpdaterStatus, updaterInstall,
      `安装未完成：${outcome.error || "已有下载或安装事务在进行中"}`);
    return;
  }
  if (outcome?.state && !acceptUpdaterRevision(outcome)) {
    renderUpdaterBody(body, lastUpdaterStatus, updaterInstall); return;
  }
  if (outcome && outcome.ok === true) {
    // 事件 installed 可能尚未到达：按返回值渲染（事件到达后幂等覆盖）
    if (!updaterInstall || updaterInstall.state !== "installed") {
      updaterInstall = null;
      updaterReady = null;
      lastUpdaterStatus = {
        ...outcome,
        state: "installed",
        current_version: outcome.current_version || fallbackCurrent,
        available_version: outcome.available_version || fallbackVersion,
      };
    }
    renderUpdaterBody(body, lastUpdaterStatus, updaterInstall);
  } else if (outcome && (outcome.state === "cancelled" || outcome.state === "failed")) {
    // 终态：updater-state 事件通常先于 invoke 返回到达；仅当事件未到时
    // 以返回值补建终态呈现（错误可读，不吞错）。ready 字节被 Rust 保留，
    // 若终态源自已就绪安装的失败，重试入口按 ready 语境回到「安装（需重启）」。
    if (!updaterInstall || updaterInstall.state !== outcome.state) {
      updaterInstall = { ...outcome, error: outcome?.error || "未知原因" };
    }
    renderUpdaterBody(body, lastUpdaterStatus, updaterInstall);
  } else {
    const why = outcome && outcome.error ? outcome.error : "未知原因";
    renderUpdaterBody(body, lastUpdaterStatus, updaterInstall, `安装未完成：${why}`);
  }
}

/** 确认层取消回落（两个安装入口共用）：收起层即取消，不发
 * updater-cancel-requested（确认前尚无下载/安装事务可取消），原状态与
 * 入口保持可再次尝试。 */
function dismissUpdaterConfirm(layer) {
  layer.hidden = true;
  layer.innerHTML = "";
}

/** 「下载并安装」确认层（开关关闭的 040B 现状路径）：展示版本与后果，
 * 确认后才 invoke（confirmed=true）。 */
function confirmUpdaterInstall(body) {
  const layer = document.getElementById("updater-confirm");
  const invoke = tauriInvoke();
  if (!layer || !invoke) return;
  const status = lastUpdaterStatus;
  const version = status?.available_version || "未知";
  layer.hidden = false;
  layer.innerHTML = `
    <p class="hint">即将下载并安装 <code>${escapeHtml(String(version))}</code>：安装包会经内置公钥验签（不可关闭），
      下载阶段可取消，进入安装后不可取消（失败会自动回滚到当前版本）；
      安装完成后需重启应用才切换到新版本。</p>
    ${status?.notes ? `<p class="hint">更新说明：${escapeHtml(status.notes)}</p>` : ""}
    <div class="exclude-actions">
      <button type="button" id="updater-confirm-yes" class="btn primary" data-test="updater-confirm-yes">确认下载并安装</button>
      <button type="button" id="updater-confirm-no" class="btn" data-test="updater-confirm-no">取消</button>
    </div>`;
  const no = document.getElementById("updater-confirm-no");
  if (no) no.addEventListener("click", () => dismissUpdaterConfirm(layer));
  const yes = document.getElementById("updater-confirm-yes");
  if (yes) {
    yes.addEventListener("click", async () => {
      yes.disabled = true;
      dismissUpdaterConfirm(layer);
      await runUpdaterInstallTransaction(
        body,
        "已确认下载并安装；更新开始后此处显示进度。",
        version,
        status?.current_version,
      );
    });
  }
}

/** ISS-113「安装（需重启）」确认层（ready 路径）：更新包已后台下载并验签，
 * 确认后进入既有安装事务（复用 updater_install + confirmed=true，六步合同
 * 不变）；取消回落保持 ready 态可再次尝试。 */
function confirmUpdaterInstallReady(body) {
  const layer = document.getElementById("updater-confirm");
  const invoke = tauriInvoke();
  if (!layer || !invoke) return;
  const ready = updaterReady || {};
  const version = ready.available_version || lastUpdaterStatus?.available_version || "未知";
  layer.hidden = false;
  layer.innerHTML = `
    <p class="hint">即将安装 <code>${escapeHtml(String(version))}</code>：更新包已在后台下载并经内置公钥验签，
      无需再次下载；安装会暂停写入并自动备份数据，进入安装后不可取消（失败会自动回滚到当前版本）；
      完成后需重启应用才切换到新版本。</p>
    <div class="exclude-actions">
      <button type="button" id="updater-confirm-yes" class="btn primary" data-test="updater-ready-confirm-yes">确认安装</button>
      <button type="button" id="updater-confirm-no" class="btn" data-test="updater-ready-confirm-no">取消</button>
    </div>`;
  const no = document.getElementById("updater-confirm-no");
  if (no) no.addEventListener("click", () => dismissUpdaterConfirm(layer));
  const yes = document.getElementById("updater-confirm-yes");
  if (yes) {
    yes.addEventListener("click", async () => {
      yes.disabled = true;
      dismissUpdaterConfirm(layer);
      await runUpdaterInstallTransaction(
        body,
        "已确认安装；更新开始后此处显示进度。",
        version,
        ready.current_version || lastUpdaterStatus?.current_version,
      );
    });
  }
}

/** 下载取消（ISS-102）：emit 既有 updater-cancel-requested（与
 * lib.rs UPDATER_CANCEL_EVENT 同合同），Rust 在下载 poll 边界受理并回滚；
 * 按钮转「正在取消…」防重复发送，cancelled 事件到达后复位。
 * emit 失败不吞错：复位按钮并提示可再次尝试。 */
async function requestUpdaterCancel(body) {
  const t = window.__TAURI__;
  const emitFn = t && t.event && typeof t.event.emit === "function" ? t.event.emit : null;
  if (!emitFn || updaterCancelRequested) return;
  updaterCancelRequested = true;
  renderUpdaterBody(body, lastUpdaterStatus, updaterInstall);
  try {
    await emitFn(UPDATER_CANCEL_EVENT_NAME, null);
  } catch (e) {
    updaterCancelRequested = false;
    renderUpdaterBody(body, lastUpdaterStatus, updaterInstall,
      `取消请求发送失败：${e?.message || e}；可再次尝试。`);
  }
}

/** 「重启以完成」确认层：重启是独立确认；取消则保持已安装待重启状态。 */
function confirmUpdaterRestart(body) {
  const layer = document.getElementById("updater-restart-confirm");
  const invoke = tauriInvoke();
  if (!layer || !invoke) return;
  layer.hidden = false;
  layer.innerHTML = `
    <p class="hint">重启应用以完成更新：当前窗口会关闭，后台计划与常驻服务随退出流程
      一并回收，重启后按新版本运行。已入库的扫描数据不受影响。</p>
    <div class="exclude-actions">
      <button type="button" id="updater-restart-yes" class="btn primary" data-test="updater-restart-yes">确认重启</button>
      <button type="button" id="updater-restart-no" class="btn" data-test="updater-restart-no">取消</button>
    </div>`;
  const no = document.getElementById("updater-restart-no");
  if (no) no.addEventListener("click", () => {
    layer.hidden = true;
    layer.innerHTML = "";
  });
  const yes = document.getElementById("updater-restart-yes");
  if (yes) {
    yes.addEventListener("click", async () => {
      yes.disabled = true;
      try {
        // 进程可能随重启退出而收不到返回值；失败路径回落后可再次尝试
        await invoke("updater_restart", { confirmed: true });
      } catch (e) {
        renderUpdaterBody(body, lastUpdaterStatus, updaterInstall, `重启请求失败：${e?.message || e}`);
      }
    });
  }
}

/** 世代/修订号来自壳；未标号载荷只兼容旧壳，在已同步新协议后不能覆盖快照。 */
function acceptUpdaterRevision(payload) {
  if (!payload || typeof payload !== "object") return false;
  const generation = payload.generation, revision = payload.revision;
  if (!Number.isSafeInteger(generation) || !Number.isSafeInteger(revision)) {
    return updaterGeneration < 0;
  }
  if (generation < updaterGeneration || revision < updaterRevision) return false;
  updaterGeneration = generation;
  updaterRevision = revision;
  return true;
}

function applyUpdaterPayload(payload) {
  const state = payload && typeof payload.state === "string" ? payload.state : null;
  if (!state) return;
  if (payload.presentation === "check") {
    // 启动检查只更新检查事实；已安装的重启入口仍由事务终态决定。
    if (lastUpdaterStatus?.state !== "installed") lastUpdaterStatus = payload;
    return true;
  }
  if (state === "installed") {
    // 事务成功：清安装事务/ready 呈现与过渡说明，检查态转 installed
    lastUpdaterStatus = { ...(lastUpdaterStatus || {}), ...payload };
    updaterInstall = null;
    updaterReady = null;
    updaterPendingNote = null;
    updaterCancelRequested = false;
  } else if (state === "downloaded") {
    const terminal = updaterInstall || lastUpdaterStatus;
    if (Number.isSafeInteger(payload.generation) && terminal?.generation === payload.generation
        && ["failed", "cancelled", "installed"].includes(terminal.state)) return false;
    // ISS-113：后台下载完成（ready）——旧终态呈现清除，等待安装确认。
    updaterReady = payload;
    updaterInstall = null;
    updaterPendingNote = null;
    updaterCancelRequested = false;
  } else if (UPDATER_PHASE_LABELS[state]) {
    // 安装事务阶段/终态：合并载荷（downloading 进度按节流事件推进）。
    // downloading 到达即视为新一轮下载在途（壳预下载或事务内下载），
    // 旧 ready 呈现不再成立。
    updaterInstall = { ...payload };
    if (state === "downloading") updaterReady = null;
    if (state === "cancelled") updaterCancelRequested = false;
    if (state === "preparing") updaterPendingNote = null;
  } else if (UPDATER_STATE_LABELS[state]) {
    // 检查类事件（unconfigured/unreachable/up_to_date/available/failed）
    lastUpdaterStatus = payload;
    // 非 available 的确定态（如已最新）使旧 ready 呈现不再成立；
    // available 同版本时 ready 仍有效（壳会随即重发 downloaded）。
    if (state !== "available") updaterReady = null;
    else if (updaterReady && payload.available_version
      && payload.available_version !== updaterReady.available_version) {
      updaterReady = null;
    }
  } else {
    return;
  }
  return true;
}

async function loadUpdater() {
  const panel = _ensureUpdaterPanel();
  if (!panel) return;
  const body = panel.querySelector("[data-test='updater-panel-body']");
  const invoke = tauriInvoke();
  if (!body) return;
  if (!invoke) { renderUpdaterBody(body, null, null); return; }
  renderUpdaterBody(body, lastUpdaterStatus, updaterInstall);
  const epoch = ++updaterRecoveryEpoch;
  // 先订阅、再回读，消除 listener 注册与 snapshot 返回之间的空窗；单文档只订阅一次。
  const tauri = window.__TAURI__;
  if (!updaterListener && typeof tauri?.event?.listen === "function") {
    updaterListener = Promise.resolve(tauri.event.listen("updater-state", event => {
      const payload = event?.payload;
      if (!acceptUpdaterRevision(payload)) return;
      if (!applyUpdaterPayload(payload)) return;
      ++updaterEventSequence;
      renderUpdaterBody(body, lastUpdaterStatus, updaterInstall);
      renderAboutVersion();
    })).catch(() => { updaterListener = null; });
  }
  if (updaterListener) await updaterListener;
  if (epoch !== updaterRecoveryEpoch) return;
  try {
    const snapshot = await invoke("updater_check", { recover: true });
    if (epoch !== updaterRecoveryEpoch) return;
    if (snapshot?.recovery === true) {
      if (snapshot.error) throw new Error(snapshot.error);
      if (acceptUpdaterRevision(snapshot)) {
        lastUpdaterStatus = snapshot.status || null;
        updaterInstall = null;
        updaterReady = null;
        updaterPendingNote = null;
        updaterCancelRequested = false;
        if (snapshot.activity) applyUpdaterPayload({ ...snapshot.activity,
          generation: snapshot.generation, revision: snapshot.revision });
      }
    } else if (acceptUpdaterRevision(snapshot)) {
      // 兼容旧壳：旧 updater_check 不识别 recover，会返回检查状态。
      applyUpdaterPayload(snapshot);
    }
    renderUpdaterBody(body, lastUpdaterStatus, updaterInstall);
    renderAboutVersion();
    if (!lastUpdaterStatus && !updaterInstall && !updaterReady) checkUpdater(body);
  } catch (e) {
    if (epoch !== updaterRecoveryEpoch) return;
    renderUpdaterBody(body, lastUpdaterStatus, updaterInstall,
      `更新状态恢复失败：${e?.message || e}；可手动检查更新。`);
  }
}

/* ISS-087：注入应用版本号到「关于」section 的 #about-version-num 节点；
 * 版本源 = fathom.__version__（ISS-111 起随 /api/status 的 app_version
 * 下发），由两处回填，更新检查返回值优先（两源一致时相同）：
 *  - 打包态：version 实际由 loadUpdater 通过 updater_check 返回 current_version 渲染
 *  - 任意态：loadSettings 拉到 /api/status 后立即回填 lastStatusAppVersion——
 *    版本在构建时已知，不再依赖用户点「检查更新」
 * 兜底：两源都未到达时保留「未知」占位，不伪造数字。 */
function renderAboutVersion() {
  const node = document.querySelector('[data-test="about-version-num"]');
  if (!node) return;
  const fromUpdater = lastUpdaterStatus?.current_version;
  if (typeof fromUpdater === "string" && fromUpdater.length > 0) {
    node.textContent = fromUpdater;
    return;
  }
  // ISS-111：/api/status 的 app_version 已到即可回填（页面加载即拉取，
  // 无需进入关于分区后再点检查更新）。
  if (typeof lastStatusAppVersion === "string" && lastStatusAppVersion.length > 0) {
    node.textContent = lastStatusAppVersion;
    return;
  }
  // 两源都未到达：保留「未知」字面量占位，避免硬编码误导。
  if (!node.textContent || node.textContent === "—") {
    node.textContent = "未知";
  }
}

/* ---------- ISS-156：范围与覆盖分区（容器/卷发现 + 范围选择 + 计划预览 + 保存） ----------
 *
 * 合同要点（任务卡）：
 * - **发现 ≠ 已监控**：发现列表只回答「有哪些可选项」，当前生效范围单独一行
 *   （来源 + 版本 + 修订号），不由发现结果推断；
 * - 明确推荐「启动盘整体」或「自定义目录」；启动容器内的其它卷**独立勾选**，
 *   不随启动盘整体被顺带纳入；
 * - 计划预览回显真实身份（scope_id/plan_id）、读取限制（每根 du 时限）、
 *   资源预算（min_kb/排除）与 identity 版本；预览**不落盘**；
 * - 新口径要新基线：保存成功才变更 UI（旧 revision 保持可辨），并提示旧 HOME
 *   历史保留为 legacy 口径、不与新基线混比；
 * - 保存**只影响下一轮计划**，运行中改设置不打断本轮；
 * - 首次启用引导：首次配置后首扫是**独立且明确**的动作（不由保存隐式触发）；
 *   单快照可看分布，变化需等第二个可比日期。
 *
 * 失败不谎报：预览失败/发现失败/409 冲突各自有可辨文案，旧值一律保持可辨。
 */
const SCOPE_PANEL_ID = "scope-panel";
const SCOPE_STATUS_LABELS = {
  accessible: "可访问",
  unmounted: "未挂载",
  locked: "已锁定（FileVault 未解锁）",
  unknown: "状态未知",
};

let scopeState = {
  view: null,        // GET /api/storage/plan/preview（无参数）的生效视图
  discovery: null,   // GET /api/storage/discovery 的发现结果
  discoveryError: null,
  preview: null,     // 最近一次「候选」预览（只读预览结果）
  previewError: null,
  feedback: null,    // {text, kind}
  firstRun: null,    // 保存成功后的首扫引导
  draftMode: null,
  draftRoots: null,
};

function _scopeRootsInput() {
  return document.getElementById("scope-roots");
}

function _scopeMode() {
  const checked = document.querySelector('input[name="scope-mode"]:checked');
  return checked ? checked.value : null;
}

/** 当前编辑中的候选选择（表单事实，不预设已保存）。 */
function _scopeCandidate() {
  const mode = _scopeMode();
  if (!mode) return null;
  const roots = mode === "startup_storage" ? [] : (_scopeRootsInput()?.value || "")
    .split("\n").map((s) => s.trim()).filter(Boolean);
  return { mode, roots };
}

function _scopeVolumeRow(v) {
  const status = SCOPE_STATUS_LABELS[v.status] || v.status || "状态未知";
  const name = escapeHtml(v.name || v.mount_point || v.device_identifier || v.volume_id || "未命名卷");
  const role = (v.roles || []).map((r) => escapeHtml(r)).join(" / ");
  // 卷信息只读展示；未挂载/锁定不因「发现到」而成为可采集入口。
  const disabled = v.status && v.status !== "accessible";
  return `<li class="scope-volume" data-test="scope-volume" data-volume-id="${escapeHtml(v.volume_id || "")}"
        data-status="${escapeHtml(v.status || "unknown")}"${disabled ? " data-disabled=\"1\"" : ""}>
        <span class="scope-volume-label">
          <span class="scope-volume-name">${name}</span>
        </span>
        <span class="scope-volume-meta">${status}${role ? ` · ${role}` : ""}
          ${v.mount_point ? ` · <code>${escapeHtml(v.mount_point)}</code>` : ""}</span>
        ${v.filevault ? `<span class="scope-volume-meta">FileVault：${escapeHtml(v.filevault)}</span>` : ""}
      </li>`;
}

function _scopeDeviceRow(d) {
  const mounted = Boolean(d.mount_point);
  return `<li class="scope-volume" data-test="scope-device" data-device-id="${escapeHtml(d.device_id || "")}">
      <span class="scope-volume-name">${escapeHtml(d.name || d.device_id || "未命名设备")}</span>
      <span class="scope-volume-meta">非启动容器 ${mounted
        ? `· 可选 <code>${escapeHtml(d.mount_point)}</code>` : "· 未挂载（不提供监控入口）"}</span>
    </li>`;
}

function _scopeCurrentRow() {
  const view = scopeState.view;
  if (!view) {
    return `<p class="hint" data-test="scope-current">当前生效范围读取中…</p>`;
  }
  const override = view.scan_override;
  if (override) {
    const source = override.source === "cli" ? "启动参数 --scan-root" : "环境变量 FATHOM_SCAN_ROOT";
    const saved = view.selection;
    return `<p class="hint" data-test="scope-current" data-enabled="${view.enabled ? "1" : "0"}"
        data-revision="${escapeHtml(String(saved?.revision ?? 0))}">
      已保存范围（本进程未使用）：${saved
        ? `模式 <code>${escapeHtml(saved.mode)}</code> · 修订 <code>${escapeHtml(String(saved.revision))}</code>
           · 根：${(saved.roots || []).map((r) => `<code>${escapeHtml(r)}</code>`).join("、")}`
        : "尚未保存范围选择"}</p>
      <p class="hint" data-test="scope-scan-override" data-source="${escapeHtml(override.source)}">
        本进程实际扫描范围：<code>${escapeHtml(override.root)}</code> · 来源：${escapeHtml(source)}。
        使用单目录扫描，不使用上方保存范围；保存或预览不会解除该覆盖。
        如需采集保存范围，请从未指定扫描根覆盖的入口启动。</p>`;
  }
  if (!view.enabled || !view.selection) {
    return `<p class="hint" data-test="scope-current" data-enabled="0">
      当前生效范围：<strong>尚未启用范围能力</strong>（来源：${escapeHtml(view.source || "default")}）
      ——${lastConfig?.next_scan_default === "startup_storage"
        ? "下一次点击扫描将默认采集内置启动盘整体；旧用户目录历史保留。"
        : "扫描仍按旧单根口径运行，保存后仅下一轮改用新范围。"}</p>`;
  }
  const s = view.selection;
  return `<p class="hint" data-test="scope-current" data-enabled="1"
      data-revision="${escapeHtml(String(s.revision))}"
      data-identity-version="${escapeHtml(String(view.identity_version))}">
    当前生效范围：模式 <code>${escapeHtml(s.mode)}</code>
    · 修订 <code>${escapeHtml(String(s.revision))}</code>
    · 身份版本 <code>${escapeHtml(String(view.identity_version))}</code>
    · 来源 settings.json
    ${s.roots && s.roots.length
      ? `· 根：${s.roots.map((r) => `<code>${escapeHtml(r)}</code>`).join("、")}`
      : "· 根：启动盘整体（由计划解析）"}
  </p>`;
}

function _scopePlanBlock() {
  const plan = scopeState.preview;
  if (scopeState.previewError) {
    return `<div class="hint cfg-error" data-test="scope-preview-error">
      计划预览失败：${escapeHtml(scopeState.previewError)}（当前生效范围未改动）</div>`;
  }
  if (!plan) {
    return `<p class="hint" data-test="scope-preview-empty">尚未预览候选计划。</p>`;
  }
  const rows = (plan.plans || []).map((p) => `<tr>
      <td><code>${escapeHtml(p.root)}</code></td>
      <td><code>${escapeHtml(p.display_name || "")}</code></td>
      <td><code>${escapeHtml(p.scope_id)}</code></td>
      <td><code>${escapeHtml(p.plan_id)}</code></td>
    </tr>`).join("");
  const rl = plan.read_limits || {};
  const bg = plan.budget || {};
  return `<div data-test="scope-preview">
    <p class="hint" data-test="scope-preview-kind">范围候选计划；是否用于扫描以上方实际范围为准。</p>
    <table class="scope-plan-table">
      <thead><tr><th>根目录</th><th>名称</th><th>范围身份</th><th>计划身份</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="hint" data-test="scope-preview-limits">
      读取限制：每根 du 超时 <code>${escapeHtml(String(rl.du_timeout_s ?? "—"))}</code> 秒
      · 资源预算：min_kb <code>${escapeHtml(String(bg.min_kb ?? "—"))}</code>
      · 排除项 ${escapeHtml(String((bg.exclude_names || []).length))} 条
      · 身份版本 <code>${escapeHtml(String(plan.identity_version))}</code>
    </p>
    <p class="hint" data-test="scope-preview-uncertainty">
      耗时不确定：实际时长随文件数量与权限可读性变化，预览不给出时间估计。
    </p>
  </div>`;
}

function _scopeFirstRunBlock() {
  const fr = scopeState.firstRun;
  if (!fr) return "";
  const override = scopeState.view?.scan_override;
  if (override) {
    return `<div class="panel scope-firstrun" data-test="scope-firstrun">
      <h3>范围已保存，等待解除启动覆盖</h3>
      <p class="hint">本进程仍采集 <code>${escapeHtml(override.root)}</code>（旧单目录口径），
        不使用已保存范围。保存范围不会自动扫描，其新基线尚未采集。</p>
      <button type="button" class="btn" data-test="scope-firstscan-btn" disabled>采集已保存范围（当前被覆盖）</button>
      <p class="hint" data-test="scope-snapshot-expect">如需采集保存范围，请从未指定扫描根覆盖的入口启动。</p>
    </div>`;
  }
  return `<div class="panel scope-firstrun" data-test="scope-firstrun">
      <h3>首次采集已就绪（独立动作）</h3>
      <p class="hint">保存范围**不会**自动扫描。首轮采集是下面这个独立动作，完成后才形成新基线。</p>
      <button type="button" class="btn" id="btn-scope-firstscan" data-test="scope-firstscan-btn">
        ${icon("activity")}开始首次采集</button>
      <span id="scope-firstscan-status" class="hint" data-test="scope-firstscan-status" hidden></span>
      <p class="hint" data-test="scope-snapshot-expect">
        一次扫描只得到单个快照：可看容量分布，<strong>变化要等第二个可比日期</strong>。
        同日再次扫描会替换当天快照（不产生第二个跨日基线）。</p>
      <p class="hint" data-test="scope-legacy-note">
        旧 HOME 历史快照保留为 legacy 口径，不与新基线混比。</p>
    </div>`;
}

function renderScopePanel() {
  const panel = document.getElementById(SCOPE_PANEL_ID);
  if (!panel) return;
  const view = scopeState.view;
  const sel = view?.selection;
  const mode = scopeState.draftMode || sel?.mode || "startup_storage";
  const roots = scopeState.draftRoots ?? sel?.roots ?? [];
  const discovery = scopeState.discovery;
  const fb = scopeState.feedback;

  let discoveryBlock;
  if (scopeState.discoveryError) {
    discoveryBlock = `<p class="hint cfg-error" data-test="scope-discovery-error">
      发现失败：${escapeHtml(scopeState.discoveryError)}。
      不代表「没有可监控的卷」，也无法据此断言可读目录为 0。</p>`;
  } else if (discovery?.ok) {
    const d = discovery.discovery || {};
    const container = d.startup_container;
    const volumes = d.startup_volumes || [];
    const others = d.other_devices || [];
    discoveryBlock = `<div data-test="scope-discovery">
      <p class="hint" data-test="scope-discovery-note">
        以下是<b>发现结果</b>（有哪些可选项），<b>不代表已在监控</b>；当前生效范围见下方一行。</p>
      ${container ? `<p class="hint" data-test="scope-container">
        启动容器：<code>${escapeHtml(container.container_reference || container.container_id || "")}</code>
        · 共享剩余空间（不按卷相加）</p>` : ""}
      <ul class="scope-volume-list">${volumes.map(_scopeVolumeRow).join("")}</ul>
      ${others.length ? `<p class="hint">其它设备（非启动容器，独立入口）：</p>
        <ul class="scope-volume-list">${others.map(_scopeDeviceRow).join("")}</ul>` : ""}
    </div>`;
  } else {
    discoveryBlock = `<p class="hint" data-test="scope-discovery-pending">容器与卷信息读取中…</p>`;
  }

  panel.innerHTML = `
    ${discoveryBlock}
    <div class="cfg-row">
      <label class="cfg-label" for="scope-roots">监控范围</label>
      <div class="cfg-field">
        <label class="scope-mode">
          <input type="radio" name="scope-mode" value="startup_storage" data-test="scope-mode-startup"
                 ${mode === "startup_storage" ? "checked" : ""}>
          内置启动盘整体（默认）</label><br>
        <label class="scope-mode">
          <input type="radio" name="scope-mode" value="custom_directory" data-test="scope-mode-custom"
                 ${mode === "custom_directory" ? "checked" : ""}>
          自定义目录（只监控列出的目录）</label>
        <textarea class="ctl-input cfg-wide" id="scope-roots" rows="3" data-test="scope-roots"
                  spellcheck="false"${mode === "startup_storage" ? " hidden disabled" : ""}
                  placeholder="自定义模式每行一个绝对路径">${escapeHtml(roots.join("\n"))}</textarea>
        <p class="cfg-desc" data-test="scope-mode-desc">
          启动盘模式无需填写路径，自动从真实挂载入口生成计划；未挂载或锁定的卷不能采集，外接磁盘不自动纳入。
          换范围会形成新数据集与新基线。</p>
      </div>
    </div>
    <div class="cfg-actions">
      <button type="button" class="btn" id="btn-scope-preview" data-test="scope-preview-btn">
        ${icon("stethoscope")}预览计划</button>
      <button type="button" class="btn" id="btn-scope-save" data-test="scope-save-btn">
        ${icon("scope")}保存范围</button>
      <button type="button" class="btn" id="btn-scope-reload" data-test="scope-reload-btn">
        ${icon("activity")}重新读取</button>
    </div>
    ${_scopeCurrentRow()}
    ${_scopePlanBlock()}
    ${fb ? `<p class="hint cfg-${fb.kind}" data-test="scope-feedback" data-kind="${escapeHtml(fb.kind)}">
      ${escapeHtml(fb.text)}</p>` : ""}
    ${_scopeFirstRunBlock()}
  `;

  panel.querySelector("#btn-scope-preview")?.addEventListener("click", () => previewScope());
  panel.querySelector("#btn-scope-save")?.addEventListener("click", () => saveScope());
  panel.querySelector("#btn-scope-reload")?.addEventListener("click", () => loadScopeSettings());
  panel.querySelector("#btn-scope-firstscan")?.addEventListener("click", startFirstScan);
  panel.querySelectorAll('input[name="scope-mode"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      scopeState.draftMode = radio.value;
      const input = _scopeRootsInput();
      if (input) {
        input.hidden = radio.value === "startup_storage";
        input.disabled = input.hidden;
      }
    });
  });
}

async function loadScopeSettings() {
  const request = beginRequest("settingsScope");
  renderScopePanel();
  // 发现与生效视图互不依赖：任一失败都不阻断另一部分（失败不谎报 0）。
  try {
    const d = await fetchJSON("/api/storage/discovery");
    if (!request.current()) return;
    if (d && d.ok) { scopeState.discovery = d; scopeState.discoveryError = null; }
    else { scopeState.discovery = null; scopeState.discoveryError = d?.error || "未知原因"; }
  } catch (e) {
    if (!request.current()) return;
    scopeState.discovery = null;
    scopeState.discoveryError = e.status === 0 ? "无法连接本地服务" : (e.message || `HTTP ${e.status}`);
  }
  if (!request.current()) return;
  try {
    scopeState.view = await fetchJSON("/api/storage/plan/preview");
    if (!request.current()) return;
  } catch (e) {
    if (!request.current()) return;
    scopeState.view = null;
    scopeState.feedback = {
      kind: "error",
      text: `范围配置读取失败：${e.status === 0 ? "无法连接本地服务" : e.message}`,
    };
  }
  if (!request.current()) return;
  renderScopePanel();
}

async function previewScope() {
  const candidate = _scopeCandidate();
  if (!candidate) {
    scopeState.feedback = { kind: "error", text: "请先选择范围模式。" };
    renderScopePanel();
    return;
  }
  scopeState.draftMode = candidate.mode;
  scopeState.draftRoots = candidate.roots;
  const request = beginRequest("settingsScope");
  const qs = new URLSearchParams({ mode: candidate.mode, roots: candidate.roots.join(";") });
  try {
    const data = await fetchJSON(`/api/storage/plan/preview?${qs.toString()}`);
    if (!request.current()) return;
    scopeState.preview = data.plan || null;
    scopeState.previewError = null;
    scopeState.feedback = {
      kind: "",
      text: data.hint || "候选预览不改任何配置；保存后只影响下一轮计划，不触发扫描。",
    };
  } catch (e) {
    if (!request.current()) return;
    scopeState.preview = null;
    scopeState.previewError = e.status === 0 ? "无法连接本地服务" : (e.message || `HTTP ${e.status}`);
    scopeState.feedback = null;
  }
  if (!request.current()) return;
  renderScopePanel();
}

async function saveScope() {
  const candidate = _scopeCandidate();
  if (!candidate) {
    scopeState.feedback = { kind: "error", text: "请先选择范围模式。" };
    renderScopePanel();
    return;
  }
  const request = beginRequest("settingsScope");
  const body = { mode: candidate.mode, roots: candidate.roots };
  // 初次保存同样带版本 0，预览后若其他窗口保存过则明确冲突。
  body.expected_revision = 0;
  const current = scopeState.view?.selection;
  if (current && typeof current.revision === "number") {
    body.expected_revision = current.revision;   // 乐观并发：预览后被改动 → 409
  }
  try {
    const res = await apiPut("/api/storage/scope", body);
    const data = await res.json();
    if (!request.current()) return;
    // 只有保存成功才变更 UI（服务端返回的生效视图为准）
    scopeState.view = {
      enabled: data.scope?.enabled,
      identity_version: data.scope?.identity_version,
      source: data.scope?.source,
      selection: data.scope?.selection,
      scan_override: data.scope?.scan_override,
    };
    scopeState.preview = data.plan || null;
    scopeState.previewError = null;
    scopeState.firstRun = { savedAt: data.scope?.selection?.revision ?? null };
    scopeState.draftMode = null;
    scopeState.draftRoots = null;
    scopeState.feedback = {
      kind: "ok",
      text: data.hint || "已保存：只影响下一轮计划，不触发扫描。",
    };
  } catch (e) {
    if (!request.current()) return;
    // 失败/409：旧值保持可辨——不乐观改写 view，feedback 明确冲突与旧修订号。
    const currentRev = scopeState.view?.selection?.revision;
    const isConflict = e.status === 409;
    scopeState.feedback = {
      kind: "error",
      text: isConflict
        ? `保存冲突：${e.message}（旧值未改动，当前生效修订 ${currentRev ?? "—"}；请重新读取后再预览保存）`
        : `保存失败：${e.status === 0 ? "无法连接本地服务" : e.message}。当前生效范围保持不变。`,
    };
  }
  if (!request.current()) return;
  renderScopePanel();
}

async function startFirstScan() {
  const status = document.getElementById("scope-firstscan-status");
  const btn = document.getElementById("btn-scope-firstscan");
  if (btn) btn.disabled = true;
  if (status) { status.hidden = false; status.textContent = "正在请求首次采集…"; }
  try {
    const res = await apiPost("/api/scan", {});
    if (status) {
      status.textContent = res.status === 409
        ? "已有扫描在进行中；本次设置将在下一轮生效。"
        : "已开始首次采集：完成后形成新基线。";
    }
  } catch (e) {
    if (status) {
      status.textContent = `无法开始首次采集：${e.status === 0 ? "无法连接本地服务" : e.message}`;
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

export const settingsPage = {
  id: "settings",
  enter() {
    // 常驻页面初始化后，跨页的 AI 配置入口仍须在每次进入时消费深链。
    const pending = window.__fathomSettingsSectionPending;
    window.__fathomSettingsSectionPending = null;
    const section = pending || _readHashSection();
    if (SETTINGS_SECTIONS.includes(section)) {
      activateSettingsSection(section, { persistHash: Boolean(pending) });
    }
  },
  load() {
    loadSettings();
    // 关于区版本：loadUpdater 完成后回填（异步），这里先设一次占位
    renderAboutVersion();
  },
  init() {
    initSettingsNav();
    document.getElementById("config-form")
      ?.addEventListener("submit", saveConfig);
    document.getElementById("btn-config-reset")
      ?.addEventListener("click", resetToDefaults);
    document.getElementById("schedule-form")
      ?.addEventListener("submit", saveScheduleConfig);
    // ISS-111：监控分区末尾交叉说明的「前往权限分区」按钮（index.html 静态）
    document.getElementById("btn-goto-permissions")
      ?.addEventListener("click", () => {
        activateSettingsSection("permissions", { persistHash: true });
      });
  },
  leave() { ["settings", "scanHistory", "settingsPermissions", "permHub", "settingsAutostart", "settingsUpdater", "settingsAnalysis", "settingsScope"].forEach(invalidateRequest); },
};
