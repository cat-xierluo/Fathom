/* 大文件页（ISS-032）：显式点击查询近 N 天 ≥ 阈值的修改文件。

前端责任（ISS-027 模块合同）：
- beginRequest 世代号保证乱序/迟到响应不覆盖较新查询；
- fetchJSON（失败抛 status 0 / HTTP code）；
- 仅 ``frontend/icons.js`` 的 SVG 图标；零 emoji。

展示（ISS-032 Phase 2）：
- 进度：scope（根路径 + days + min_mb + topn）+ 墙钟/行数；
- state：ok / no_match / permission_denied / failed / truncated / expired；
- 截断/过期/缓存标志；
- 错误信息（state=failed/permission_denied 时）。
状态信息以表格首行或首单元格形式呈现；不引入新 DOM 节点。
*/

import { fetchJSON, beginRequest, revealInFinder } from "../request.js";
import { fmtBytes, escapeHtml } from "../format.js";
import { icon } from "../../icons.js";

function _shortRoot(root) {
  if (!root) return "-";
  const parts = String(root).split("/").filter(Boolean);
  if (parts.length <= 2) return root;
  return ".../" + parts.slice(-2).join("/");
}

function _stateLabel(state) {
  switch (state) {
    case "ok": return "查询完成";
    case "no_match": return "无匹配文件";
    case "permission_denied": return "权限受限";
    case "failed": return "查询失败";
    case "truncated": return "结果被截断";
    case "expired": return "缓存已过期";
    default: return state;
  }
}

function _stateIcon(state) {
  if (state === "failed" || state === "permission_denied") return icon("alert", 14);
  return icon("fileText", 14);
}

function _statusRowHtml(scope, body, colspan = 4) {
  /* 表格首行的状态摘要行；不破坏现有 verify 脚本对 #tbl-bigfiles tbody 的查询。 */
  const parts = [];
  if (scope) {
    parts.push(
      `<span class="hint">${escapeHtml(_shortRoot(scope.root))} · ` +
      `${scope.days} 天 · ≥ ${scope.min_mb} MB · top ${scope.topn}</span>`
    );
  }
  if (body) {
    parts.push(
      `<span style="display:inline-flex;align-items:center;gap:4px;color:var(--muted)">` +
        `${_stateIcon(body.state)}` +
        `<span>${escapeHtml(_stateLabel(body.state))}</span>` +
      `</span>`
    );
    if (body.truncated) {
      parts.push(
        `<span style="display:inline-flex;align-items:center;gap:4px;color:var(--muted)">` +
          `${icon("alert", 12)}` +
          `<span class="hint">` +
            `结果数 ≥ ${escapeHtml(String(body.stats.find_output_lines))} 行，已截断到 top ${escapeHtml(String(body.scope.topn))}` +
          `</span>` +
        `</span>`
      );
    }
    if (body.expired) {
      parts.push(
        `<span style="display:inline-flex;align-items:center;gap:4px;color:var(--muted)">` +
          `${icon("alert", 12)}` +
          `<span class="hint">缓存已过期（${escapeHtml(String(Math.round((body.cache_age_s || 0) * 10) / 10))} 秒）</span>` +
        `</span>`
      );
    } else if (body.cached) {
      parts.push(
        `<span class="hint">命中缓存（${escapeHtml(String(Math.round((body.cache_age_s || 0) * 10) / 10))} 秒前）</span>`
      );
    }
    if (body.error_message) {
      parts.push(
        `<span style="display:inline-flex;align-items:center;gap:4px;color:var(--muted)">` +
          `${icon("alert", 12)}` +
          `<span class="hint">${escapeHtml(body.error_message)}</span>` +
        `</span>`
      );
    }
    if (body.stats && typeof body.stats.wall_ms === "number") {
      parts.push(
        `<span class="hint">墙钟 ${body.stats.wall_ms} ms · find 行数 ${body.stats.find_output_lines}</span>`
      );
    }
  }
  return (
    `<tr class="hint-row"><td colspan="${colspan}" class="hint" style="font-size:12px">` +
    parts.join(" · ") +
    `</td></tr>`
  );
}

async function loadBigfiles() {
  const request = beginRequest("bigfiles");
  const days = document.getElementById("bf-days").value || 7;
  const mb = document.getElementById("bf-mb").value || 100;
  const tbody = document.querySelector("#tbl-bigfiles tbody");
  // 立即进入查询态：深度环加载占位（ISS-085，几何出自 icons.js brandRing，
  // 配色与旋转由 style.css .dr-loading 承担）+「查询中…」文本
  if (tbody) {
    tbody.innerHTML =
      `<tr class="hint-row"><td colspan="4" class="hint">${icon("brandRing", 14, "dr-loading")}查询中…</td></tr>`;
  }
  let r;
  try {
    r = await fetchJSON(`/api/bigfiles?days=${days}&min_mb=${mb}&topn=200`);
  } catch (e) {
    if (!request.current()) return;
    if (tbody) {
      tbody.innerHTML =
        `<tr><td colspan="4" class="hint">${escapeHtml(
          e.status === 0
            ? "无法连接本地服务，大文件查询暂不可用。"
            : `大文件查询失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`)}</td></tr>`;
    }
    return;
  }
  if (!request.current()) return;

  const statusRow = _statusRowHtml(r.scope, r, 4);

  if (tbody) tbody.innerHTML = statusRow;

  if (!r.files || !r.files.length) {
    if (r.state === "permission_denied") {
      tbody.insertAdjacentHTML(
        "beforeend",
        `<tr><td colspan="4" class="hint">` +
          `${icon("alert", 14)} 权限受限：${escapeHtml(r.error_message || "find 命中权限拒绝行")}` +
        `</td></tr>`
      );
    } else if (r.state === "failed") {
      tbody.insertAdjacentHTML(
        "beforeend",
        `<tr><td colspan="4" class="hint">` +
          `${icon("alert", 14)} 查询失败：${escapeHtml(r.error_message || "find 返回非零")}` +
        `</td></tr>`
      );
    } else if (r.state === "expired") {
      tbody.insertAdjacentHTML(
        "beforeend",
        `<tr><td colspan="4" class="hint">缓存已过期，重新查询…</td></tr>`
      );
    } else if (r.state === "no_match") {
      tbody.insertAdjacentHTML(
        "beforeend",
        `<tr><td colspan="4" style="color:var(--muted)">` +
          `近 ${r.scope.days} 天没有 ≥ ${r.scope.min_mb}MB 的文件修改` +
        `</td></tr>`
      );
    }
    return;
  }

  r.files.forEach((f) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td class="num">${fmtBytes(f.size)}</td>` +
      `<td style="white-space:nowrap">${escapeHtml(f.mtime)}</td>` +
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