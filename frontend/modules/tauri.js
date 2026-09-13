/* Tauri 桥接层：__TAURI__ 存在时推送 tray 状态、响应 tray-action；
 * 普通浏览器无桥时静默降级（不推送、不监听），页面功能不受影响。
 */
import { fmtBytes } from "./format.js";

export function tauri() { return window.__TAURI__ || null; }

let trayPushFailed = false;  // 失败只警告一次，恢复后重置（防扫描轮询刷屏）

export function pushTrayStatus(freeBytes, snapCount, latest) {
  const t = tauri();
  if (!t || !t.core?.invoke) return;  // 浏览器环境：静默降级，不推送
  const gb = Math.round(freeBytes / 1024 ** 3);
  const tooltip = latest
    ? `剩余 ${fmtBytes(freeBytes)} · 快照 ${snapCount} 个 · 最近扫描 ${latest.created_at.replace("T", " ")}`
    : `剩余 ${fmtBytes(freeBytes)} · 尚无快照`;
  t.core.invoke("update_tray_status", { title: `${gb} GB`, tooltip })
    .then(() => { trayPushFailed = false; })
    .catch((e) => {
      if (trayPushFailed) return;  // 持续失败不重复刷屏
      trayPushFailed = true;
      console.warn("tray 状态推送失败（将静默重试，排查线索：capability 远程授权/后端未起）：", e);
    });
}

export async function listenTrayActions({ onScan } = {}) {
  const t = tauri();
  if (!t?.event?.listen) return;  // 浏览器环境：无桥可监听
  await t.event.listen("tray-action", (e) => {
    if (e.payload === "scan") onScan?.();
  });
}
