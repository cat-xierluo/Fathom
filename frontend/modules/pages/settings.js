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
 * 信息架构收敛（ISS-083）：打包态（Tauri 桥在位，与更新/自启面板的
 * 降级检测同信号）默认隐藏技术区块——「运行信息」中的技术行（服务地址/
 * 运行根/数据库/du 时限/桌面壳）与静态「服务管理」面板收进默认折叠的
 * 「高级与诊断」区；能力不删除，展开即全部可见。浏览器/开发态（无桥）
 * 保持原全量展示，不回退。
 */
import { fetchJSON, beginRequest, invalidateRequest, apiPut } from "../request.js";
import { escapeHtml } from "../format.js";
import { icon } from "../../icons.js";
import { triggerScan, loadStatus } from "../status.js";

let lastConfig = null;  // 最近一次生效配置（页面内存；保存/恢复默认的对照源）

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

function renderEffective(cfg) {
  const target = document.getElementById("config-effective");
  if (!target) return;
  const override = SOURCE_LABELS[cfg.sources?.scan_root];
  const masks = excludeMasksFromConfig(cfg.exclude_names);
  target.innerHTML =
    `当前生效：监控根 <code>${escapeHtml(cfg.scan_root)}</code>` +
    ` · 计划 <code>${escapeHtml(cfg.scan_time)}</code>` +
    ` · 入库阈值 <code>${escapeHtml(String(cfg.min_kb))} KB</code>` +
    ` · 低空间提醒 <code>${escapeHtml(String(cfg.free_alert_gb))} GB</code>` +
    ` · 排除掩码 <code>${masks.length ? escapeHtml(masks.join(";")) : "（无）"}</code>` +
    (override ? `<br>${escapeHtml(override)}` : "");
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
      <p class="hint">按名字（fnmatch 通配）跳过整棵子树，例如 <code>node_modules</code>、<code>*.noindex</code>；不含路径分隔符。</p>
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
      <label class="exclude-confirm-label">
        <input type="checkbox" id="exclude-confirm">
        <span>我已知晓：保存后按新数据集扫描，历史对比可能中断</span>
      </label>
      <div class="exclude-actions">
        <button type="button" id="btn-exclude-save" class="btn primary">${icon("filter")}保存排除列表</button>
      </div>
    </div>`;
  // 插在"扫描运行历史"面板之前；找不到则追加到页面末尾
  const history = document.getElementById("scan-history");
  const anchor = history ? history.closest(".panel") : null;
  if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(panel, anchor);
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

function clearInputs() {
  for (const id of ["cfg-scan-root", "cfg-scan-time", "cfg-min-kb", "cfg-free-alert-gb"]) {
    const input = document.getElementById(id);
    if (input) input.value = "";
  }
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
    clearInputs();
    showFeedback(`已保存。${data.hint || ""}`, "ok");
  } catch (e) {
    // 校验失败/服务故障：生效值不重渲染，旧值保持可辨；输入保留供修改
    showFeedback(
      e.status === 0
        ? "保存失败：无法连接本地服务，当前生效值保持不变。"
        : `保存失败：${e.message} 当前生效值保持不变。`,
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
  showFeedback("已填入默认值；仍需点击“保存设置”才会写入。", "");
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
    }
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="2" class="hint">${escapeHtml(e.status === 0
        ? "无法连接本地服务，运行信息暂不可用。"
        : `运行信息加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`)}</td></tr>`;
    }
    return;
  }
  if (!request.current()) return;
  let c = null;
  try {
    c = await fetchJSON("/api/config");
  } catch (e) {
    if (!request.current()) return;
    if (effective) {
      effective.textContent = e.status === 0
        ? "无法连接本地服务，可修改设置暂不可用。"
        : `可修改设置加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`;
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
  // 加载"后台自启"开关（ISS-010B；可独立失败，不影响主配置）
  loadAutostart();
  // 加载"应用更新"区（ISS-040B；可独立失败，不影响主配置）
  loadUpdater();
}

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
          <td>${escapeHtml(run.source || "—")}</td>
          <td><span class="st ${stCls}">${dot}${escapeHtml(st)}</span></td>
          <td>${run.snapshot_id == null ? "—" : "#" + escapeHtml(String(run.snapshot_id))}</td>
          <td>${escapeHtml(run.message || "")}</td>
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
      <p class="hint">三类授权/覆盖状态由最近一次采集推导；本应用不代改系统权限。</p>
    </div>
    <div class="perm-panel" data-test="permissions-panel-body">
      <p class="hint">权限与覆盖信息加载中…</p>
    </div>`;
  // 插在"扫描运行历史"面板之前；找不到则追加到页面末尾
  const history = document.getElementById("scan-history");
  const anchor = history ? history.closest(".panel") : null;
  if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(panel, anchor);
  else page.appendChild(panel);
  return panel;
}

/* 把覆盖判定结果翻译为"授权状态"说明（ISS-002A）：
 *   full       → 已授权扫描范围完整
 *   partial+denied      → 可能未授权或部分目录受限（部分授权）
 *   partial+vanished    → 部分目录在扫描期间消失（采集范围存在不可控变化）
 *   partial+excluded    → 扫描集已自定义（排除掩码生效）
 *   partial+none        → 部分覆盖（具体缺口未上报）
 *   missing   → 尚未扫描或覆盖未知
 * 数字是缺口位置数，不冒充影响大小；不含"数量=影响"或"未记录=删除"表述。 */
function _explainAuthorization(coverage) {
  if (coverage.state === "full") {
    return { text: "已授权：扫描范围完整", cls: "ok" };
  }
  if (coverage.state === "missing") {
    return { text: "尚未扫描或覆盖未知", cls: "miss" };
  }
  if (coverage.denied > 0) {
    return {
      text: `可能未授权或部分目录受限：${coverage.denied} 个目录本次被拒绝读取`,
      cls: "warn",
    };
  }
  if (coverage.vanished > 0) {
    return {
      text: `采集范围存在不可控变化：${coverage.vanished} 个目录在扫描期间已消失`,
      cls: "warn",
    };
  }
  if (coverage.excluded > 0) {
    return {
      text: `扫描集已自定义：${coverage.excluded} 项排除掩码生效（与默认不同）`,
      cls: "warn",
    };
  }
  return { text: "部分覆盖：缺口类型未上报", cls: "warn" };
}

function _coverage(snapshot) {
  if (!snapshot) return { state: "missing", denied: 0, vanished: 0, excluded: 0 };
  const denied = snapshot.denied_count ?? 0;
  const vanished = snapshot.vanished_count ?? 0;
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
  return { state, denied, vanished, excluded };
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
  const auth = _explainAuthorization(coverage);
  const tauri = window.__TAURI__;
  const tauriAvailable = !!(tauri && tauri.core && typeof tauri.core.invoke === "function");
  body.innerHTML = `
    <p class="perm-state">
      <span class="quality-chip ${auth.cls}" data-test="perm-auth-chip">${icon("shield", 12)} ${escapeHtml(auth.text)}</span>
    </p>
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
      <button type="button" id="btn-rescan" class="btn primary perm-rescan"
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

/** 打包态创建「高级与诊断」面板（幂等）并把服务管理面板移入。 */
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
        <summary>${icon("settings", 12)} 高级与诊断</summary>
        <div class="adv-body" data-test="advanced-body">
          <p class="hint">面向排障与支持的运行细节：本地服务地址、运行根与数据库位置、
            采集参数、桌面壳形态与服务管理命令。日常使用无需关注。</p>
          <table class="tbl tbl-mini" id="${TECH_TABLE_ID}"><tbody></tbody></table>
        </div>
      </details>`;
    // 高级区放设置页末尾：不打断「配置 → 运行事实 → 历史」的主流程阅读序
    page.appendChild(panel);
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
 * #settings-table-tech，并确保折叠区已创建（服务管理面板随区移动）。 */
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
  // 插在"扫描运行历史"面板之前；找不到则追加到页面末尾
  const history = document.getElementById("scan-history");
  const anchor = history ? history.closest(".panel") : null;
  if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(panel, anchor);
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
      <p class="hint">命令行开发态仍用 <code>main.py install / uninstall</code>。</p>
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

/* ===== 应用更新（ISS-040B）=====
 * 壳内更新协调的前端接线：检查按钮 → 状态行（含当前版本）；available →
 * 版本 + notes + 「下载并安装」确认层（复用 010B autostart 确认层模式）→
 * 安装成功后「重启以完成」独立确认；取消/失败全部回落可恢复态。
 * - 不静默：下载安装与重启都需显式确认（confirmed=true 才会在壳内执行）；
 *   启动延迟检查（≥10s）只经 updater-state 事件更新状态行，不弹窗不安装。
 * - Tauri 桥可用时经 invoke 调 updater_check/updater_install/updater_restart；
 *   浏览器模式（无桥）如实降级为只读说明，不渲染假入口。
 * - invoke 结果做形状校验：桥异常/mock 桥返回异常结构时按「状态异常」
 *   渲染，不抛未捕获异常。
 * - DOM 在本文件内联创建（与 autostart 面板同模式），零 emoji，无构建链。 */
const UPDATER_PANEL_ID = "updater-panel";

const UPDATER_STATE_LABELS = {
  unconfigured: "未配置更新源：应用内更新不可用",
  unreachable: "更新源不可达：当前更新源处于关闭状态，可稍后重试",
  up_to_date: "已是最新版本",
  available: "有可用更新",
  downloading: "正在下载并安装…",
  installed: "更新已安装，重启后生效",
  failed: "更新检查失败",
};

let lastUpdaterStatus = null;  // 最近一次检查/安装状态（确认层取消后的回落源）

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
      <p class="hint">检查、下载与安装均需手动确认；启动后也会延迟自动检查一次（仅提示，不安装）。</p>
    </div>
    <div class="perm-panel" data-test="updater-panel-body">
      <p class="hint">应用更新状态加载中…</p>
    </div>`;
  // 插在"扫描运行历史"面板之前；找不到则追加到页面末尾（与 autostart 面板同锚点，
  // 本面板后创建，渲染在 autostart 面板之后）
  const history = document.getElementById("scan-history");
  const anchor = history ? history.closest(".panel") : null;
  if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(panel, anchor);
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

function renderUpdaterBody(body, status, extraNote) {
  const invoke = tauriInvoke();
  if (!invoke) {
    body.innerHTML = `
      <p class="hint" data-test="updater-browser-note">浏览器模式没有桌面壳桥接：应用更新的检查与安装只在 Fathom 桌面应用的设置页可用。</p>
      <p class="hint">版本以安装的应用为准；命令行开发态的版本见 <code>fathom --version</code>。</p>`;
    return;
  }
  const line = status
    ? updaterStatusLine(status)
    : "尚未检查更新；点击「检查更新」获取当前版本与可用更新。";
  const available = status?.state === "available"
    ? `
    <p class="hint" data-test="updater-available">
      可更新到 <code>${escapeHtml(String(status.available_version || "未知"))}</code>；
      下载与安装需要你确认，安装完成后需重启应用。
    </p>
    ${status.notes ? `<p class="hint" data-test="updater-notes">更新说明：${escapeHtml(status.notes)}</p>` : ""}`
    : "";
  const installed = status?.state === "installed"
    ? `
    <p class="hint" data-test="updater-installed">已安装 <code>${escapeHtml(String(status.available_version || "新版本"))}</code>；重启前保持当前版本运行。</p>`
    : "";
  const installBtn = status?.state === "available"
    ? `<button type="button" id="btn-updater-install" class="btn primary" data-test="updater-install-btn">下载并安装</button>`
    : "";
  const restartBtn = status?.state === "installed"
    ? `<button type="button" id="btn-updater-restart" class="btn primary" data-test="updater-restart-btn">重启以完成</button>`
    : "";
  body.innerHTML = `
    <p class="hint" data-test="updater-status-text">${escapeHtml(line)}</p>
    ${available}
    ${installed}
    ${extraNote ? `<p class="hint cfg-error" data-test="updater-note">${escapeHtml(extraNote)}</p>` : ""}
    <div class="perm-link-row">
      <button type="button" id="btn-updater-check" class="btn" data-test="updater-check-btn">检查更新</button>
      ${installBtn}
      ${restartBtn}
    </div>
    <div id="updater-confirm" data-test="updater-confirm" hidden></div>
    <div id="updater-restart-confirm" data-test="updater-restart-confirm" hidden></div>`;
  const checkBtn = document.getElementById("btn-updater-check");
  if (checkBtn) checkBtn.addEventListener("click", () => checkUpdater(body));
  const installBtnNode = document.getElementById("btn-updater-install");
  if (installBtnNode) installBtnNode.addEventListener("click", () => confirmUpdaterInstall(body));
  const restartBtnNode = document.getElementById("btn-updater-restart");
  if (restartBtnNode) restartBtnNode.addEventListener("click", () => confirmUpdaterRestart(body));
}

/** 手动检查：按钮防重入；结果做形状校验后渲染（失败也是可恢复状态行）。 */
async function checkUpdater(body) {
  const invoke = tauriInvoke();
  const checkBtn = document.getElementById("btn-updater-check");
  if (!invoke || !checkBtn) return;
  checkBtn.disabled = true;
  let status = null;
  try {
    status = await invoke("updater_check", {});
  } catch (e) {
    renderUpdaterBody(body, lastUpdaterStatus, `检查请求失败：${e?.message || e}`);
    return;
  } finally {
    checkBtn.disabled = false;
  }
  if (!status || typeof status.state !== "string" || !UPDATER_STATE_LABELS[status.state]) {
    renderUpdaterBody(body, lastUpdaterStatus, "状态返回异常（不是预期的更新状态结构）。");
    return;
  }
  lastUpdaterStatus = status;
  renderUpdaterBody(body, status);
}

/** 「下载并安装」确认层：展示版本与后果，确认后才 invoke（confirmed=true）；
 * 取消/失败回落 available 状态，可再次尝试。 */
async function confirmUpdaterInstall(body) {
  const layer = document.getElementById("updater-confirm");
  const invoke = tauriInvoke();
  if (!layer || !invoke) return;
  const status = lastUpdaterStatus;
  const version = status?.available_version || "未知";
  layer.hidden = false;
  layer.innerHTML = `
    <p class="hint">即将下载并安装 <code>${escapeHtml(String(version))}</code>：安装包会经内置公钥验签（不可关闭），
      安装完成后需重启应用才切换到新版本；安装失败会保持当前版本可继续使用。</p>
    ${status?.notes ? `<p class="hint">更新说明：${escapeHtml(status.notes)}</p>` : ""}
    <div class="exclude-actions">
      <button type="button" id="updater-confirm-yes" class="btn primary" data-test="updater-confirm-yes">确认下载并安装</button>
      <button type="button" id="updater-confirm-no" class="btn" data-test="updater-confirm-no">取消</button>
    </div>`;
  const no = document.getElementById("updater-confirm-no");
  if (no) no.addEventListener("click", () => {
    // 取消回落：确认层收起，available 状态与入口保持可再次尝试
    layer.hidden = true;
    layer.innerHTML = "";
  });
  const yes = document.getElementById("updater-confirm-yes");
  if (yes) {
    yes.addEventListener("click", async () => {
      yes.disabled = true;
      let outcome = null;
      try {
        outcome = await invoke("updater_install", { confirmed: true });
      } catch (e) {
        renderUpdaterBody(body, lastUpdaterStatus, `安装请求失败：${e?.message || e}`);
        return;
      }
      if (outcome && outcome.ok === true) {
        lastUpdaterStatus = {
          state: "installed",
          current_version: outcome.current_version || status?.current_version,
          available_version: outcome.available_version || version,
        };
        renderUpdaterBody(body, lastUpdaterStatus);
      } else {
        const why = outcome && outcome.error ? outcome.error : "未知原因";
        renderUpdaterBody(body, lastUpdaterStatus, `安装未完成：${why}`);
      }
    });
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
        renderUpdaterBody(body, lastUpdaterStatus, `重启请求失败：${e?.message || e}`);
      }
    });
  }
}

function loadUpdater() {
  const panel = _ensureUpdaterPanel();
  if (!panel) return;
  const body = panel.querySelector("[data-test='updater-panel-body']");
  if (!body) return;
  const invoke = tauriInvoke();
  if (!invoke) {
    renderUpdaterBody(body, null);
    return;
  }
  renderUpdaterBody(body, lastUpdaterStatus);
  // 启动延迟检查（壳内 ≥10s 后 emit）只更新状态行：不弹窗、不安装。
  const tauri = window.__TAURI__;
  if (tauri && tauri.event && typeof tauri.event.listen === "function") {
    tauri.event.listen("updater-state", (event) => {
      const payload = event && event.payload;
      if (payload && typeof payload.state === "string" && UPDATER_STATE_LABELS[payload.state]) {
        lastUpdaterStatus = payload;
        renderUpdaterBody(body, payload);
      }
    });
  }
}

export const settingsPage = {
  id: "settings",
  load() { loadSettings(); },
  init() {
    document.getElementById("config-form")
      ?.addEventListener("submit", saveConfig);
    document.getElementById("btn-config-reset")
      ?.addEventListener("click", resetToDefaults);
  },
  leave() { ["settings", "scanHistory", "settingsPermissions", "settingsAutostart", "settingsUpdater"].forEach(invalidateRequest); },
};
