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

/* 运行时长（ISS-090 扫描徽章）：M:SS，满 1 小时切 H:MM:SS（生产首扫
 * 可达小时级）。秒数补零两位，分钟不满 1 小时不补前导零（如 47:05）。 */
export function fmtDuration(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(h ? 2 : 1, "0");
  const ss = String(sec).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

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
