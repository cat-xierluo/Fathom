/* 轮询层：受生命周期管理的定时器（ISS-027）。
 *
 * 合同：schedule 幂等——同一 poller 任意时刻至多一个未触发的定时器，
 * 切页/重复触发不叠加；cancel 立即停；tick 内异常只告警。
 * 是否继续下一轮由 action 自己决定（观测到空闲就不再 schedule），
 * 因此“扫描进行中每 5s 一条链、结束即停”不会重复累积。
 */

export function createPoller(action, intervalMs) {
  let timer = null;
  return {
    get scheduled() { return timer !== null; },
    schedule() {
      if (timer !== null) return;  // 已有未触发的轮询：不叠加
      timer = setTimeout(async () => {
        timer = null;
        try { await action(); } catch (e) { console.warn("轮询失败：", e); }
      }, intervalMs);
    },
    cancel() {
      if (timer === null) return;
      clearTimeout(timer);
      timer = null;
    },
  };
}

/* 固定间隔心跳（tray 标题）：单实例 start/stop。 */
export function createInterval(action, intervalMs) {
  let timer = null;
  return {
    start() {
      if (timer !== null) return;
      timer = setInterval(() => {
        Promise.resolve(action()).catch((e) => console.warn("轮询失败：", e));
      }, intervalMs);
    },
    stop() {
      if (timer === null) return;
      clearInterval(timer);
      timer = null;
    },
  };
}
