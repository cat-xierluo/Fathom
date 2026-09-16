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

function renderEffective(cfg) {
  const target = document.getElementById("config-effective");
  if (!target) return;
  const override = SOURCE_LABELS[cfg.sources?.scan_root];
  target.innerHTML =
    `当前生效：监控根 <code>${escapeHtml(cfg.scan_root)}</code>` +
    ` · 计划 <code>${escapeHtml(cfg.scan_time)}</code>` +
    ` · 入库阈值 <code>${escapeHtml(String(cfg.min_kb))} KB</code>` +
    ` · 低空间提醒 <code>${escapeHtml(String(cfg.free_alert_gb))} GB</code>` +
    (override ? `<br>${escapeHtml(override)}` : "");
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
  }
  const p = c?.policies || {};
  const dbMb = s.db_bytes ? (s.db_bytes / 1024 / 1024).toFixed(1) : "0.0";
  const rows = [
    ["监控根目录", `<code>${escapeHtml(s.root)}</code>`],
    ["服务地址", `<code>http://127.0.0.1:${escapeHtml(String(s.port))}</code>（本地回环）`],
    ["运行根", `<code>${escapeHtml(s.runtime?.runtime_dir || "")}</code>`],
    ["数据库", `<code>${escapeHtml(s.runtime?.db_path || "")}</code> · ${dbMb} MB`],
    ["快照保留", p.keep_daily_days
      ? `近 ${escapeHtml(String(p.keep_daily_days))} 天每日一份 + 更早每周一份（最多 ${escapeHtml(String(p.keep_weekly_weeks))} 周）`
      : "—"],
    ["du 安全时限", p.du_timeout_s ? `<code>${escapeHtml(String(p.du_timeout_s))} 秒</code>` : "—"],
    ["大文件默认范围", p.bigfile_default_days
      ? `近 ${escapeHtml(String(p.bigfile_default_days))} 天 · ≥ ${escapeHtml(String(p.bigfile_default_mb))} MB`
      : "—"],
    ["桌面壳", "apps/desktop（Tauri 菜单栏 + 主窗口）"],
  ];
  if (tbody) {
    tbody.innerHTML = rows.map((r) =>
      `<tr><td style="width:140px;color:var(--muted)">${r[0]}</td><td>${r[1]}</td></tr>`).join("");
  }

  // 加载扫描运行历史（可独立失败，不影响主配置）
  loadScanHistory();
  // 加载"权限与覆盖"小节（ISS-002A；可独立失败，不影响主配置）
  loadPermissions();
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

export const settingsPage = {
  id: "settings",
  load() { loadSettings(); },
  init() {
    document.getElementById("config-form")
      ?.addEventListener("submit", saveConfig);
    document.getElementById("btn-config-reset")
      ?.addEventListener("click", resetToDefaults);
  },
  leave() { ["settings", "scanHistory", "settingsPermissions"].forEach(invalidateRequest); },
};
