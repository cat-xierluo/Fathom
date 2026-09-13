/* 设置页：只读运行配置展示。 */
import { fetchJSON, beginRequest } from "../request.js";
import { escapeHtml } from "../format.js";

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
}

export const settingsPage = {
  id: "settings",
  load() { loadSettings(); },
};
