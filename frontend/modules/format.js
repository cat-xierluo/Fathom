/* 格式化层：字节/差值/路径与 HTML 转义（纯函数，无 DOM/网络依赖）。
 * 差值口径（DESIGN.md）：正数带 +、负数带 −、缺失为 —、0 不带方向。
 */

export function fmtBytes(bytes) {
  if (bytes == null) return "-";
  let v = bytes, i = 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(1) + " " + units[i];
}

export function fmtKB(kb) { return fmtBytes((kb || 0) * 1024); }

export function fmtDelta(kb) {
  if (kb == null) return "—";
  if (kb === 0) return fmtKB(0);
  const sign = kb > 0 ? "+" : "−";
  return sign + fmtKB(Math.abs(kb));
}

export function shortPath(p, segments = 2) {
  const parts = p.split("/").filter(Boolean);
  return "/" + parts.slice(-segments).join("/");
}

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
