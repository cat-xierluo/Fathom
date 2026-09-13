/* 大文件页：显式点击查询近 N 天 ≥ 阈值的修改文件。 */
import { fetchJSON, beginRequest, revealInFinder } from "../request.js";
import { fmtBytes, escapeHtml } from "../format.js";
import { icon } from "../../icons.js";

async function loadBigfiles() {
  const request = beginRequest("bigfiles");
  const days = document.getElementById("bf-days").value || 7;
  const mb = document.getElementById("bf-mb").value || 100;
  const tbody = document.querySelector("#tbl-bigfiles tbody");
  let r;
  try {
    r = await fetchJSON(`/api/bigfiles?days=${days}&min_mb=${mb}&topn=200`);
  } catch (e) {
    if (!request.current()) return;
    tbody.innerHTML = `<tr><td colspan="4" class="hint">${escapeHtml(e.status === 0
      ? "无法连接本地服务，大文件查询暂不可用。"
      : `大文件查询失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`)}</td></tr>`;
    return;
  }
  if (!request.current()) return;
  tbody.innerHTML = "";
  if (!r.files.length) {
    tbody.innerHTML = `<tr><td colspan="4" style="color:var(--muted)">近 ${days} 天没有 ≥ ${mb}MB 的文件修改</td></tr>`;
    return;
  }
  r.files.forEach((f) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td class="num">${fmtBytes(f.size)}</td>` +
      `<td style="white-space:nowrap">${f.mtime}</td>` +
      `<td class="path" title="${escapeHtml(f.path)}">${escapeHtml(f.path)}</td>` +
      `<td><button class="btn-mini" data-reveal="${escapeHtml(f.path)}">${icon("folderOpen", 14)}</button></td>`;
    tbody.appendChild(tr);
    tr.querySelector("[data-reveal]").addEventListener("click", () => revealInFinder(f.path));
  });
}

export const bigfilesPage = {
  id: "bigfiles",
  load() { loadBigfiles(); },
  init() {
    document.getElementById("btn-bigfiles").addEventListener("click", () => loadBigfiles());
  },
};
