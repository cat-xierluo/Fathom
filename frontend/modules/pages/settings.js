/* 设置页：只读运行配置展示 + 扫描运行历史。
 *
 * 前端责任（ISS-027 模块合同）：
 * - beginRequest 世代号 + pageScoped：迟到的旧响应不能覆盖较新查询；
 * - 仅 frontend/icons.js 的 SVG 图标；零 emoji。
 *
 * 展示（ISS-028）：
 * - 运行配置：监控范围、计划、阈值、库大小等（沿用 ISS-027）；
 * - 扫描运行历史（DESIGN：诊断与卸载区）：最近 N 条扫描结果/状态/失败原因，
 *   不与设置保存按钮（ISS-016 待办）冲突；保留可追踪证据，不假装原子成功。
 */
import { fetchJSON, beginRequest, invalidateRequest } from "../request.js";
import { escapeHtml } from "../format.js";
import { icon } from "../../icons.js";

async function loadSettings() {
  const request = beginRequest("settings");
  const tbody = document.querySelector("#settings-table tbody");
  let s;
  try {
    s = await fetchJSON("/api/status");
  } catch (e) {
    if (!request.current()) return;
    tbody.innerHTML = `<tr><td colspan="2" class="hint">${escapeHtml(e.status === 0
      ? "无法连接本地服务，设置状态暂不可用。"
      : `设置状态加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`)}</td></tr>`;
    return;
  }
  if (!request.current()) return;
  const rows = [
    ["监控根目录", `<code>${escapeHtml(s.root)}</code>`],
    ["扫描计划", "每日 12:00（launchd：com.maoscripts.fathom-scan）"],
    ["快照保留", "近 35 天每日一份 + 更早每周一份（最多 12 周）"],
    ["入库阈值", "目录 ≥ 10MB；差分关注 ≥ 1MB 变化；新增目录 ≥ 100MB"],
    ["服务地址", `http://127.0.0.1:${s.port}（launchd 常驻）`],
    ["数据库", `${escapeHtml(String(s.db_bytes / 1024 / 1024))} MB · data/disk.db`],
    ["桌面壳", "apps/desktop（Tauri 菜单栏 + 主窗口）"],
  ];
  tbody.innerHTML = rows.map((r) =>
    `<tr><td style="width:140px;color:var(--muted)">${r[0]}</td><td>${r[1]}</td></tr>`).join("");

  // 加载扫描运行历史（可独立失败，不影响主配置）
  loadScanHistory();
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

export const settingsPage = {
  id: "settings",
  load() { loadSettings(); },
  leave() { ["settings", "scanHistory"].forEach(invalidateRequest); },
};
