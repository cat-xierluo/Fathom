/* 应用状态（唯一实例）：跨模块共享的页面/浏览/扫描观测状态。
 * 只存放事实，不放行为；行为分布在 request/router/status 等模块。
 */
export const state = {
  page: "overview",        // 当前激活页（hash 路由解析结果）
  browsePath: null,        // 分布页当前目录（跨刷新保留）
  browseSnapshotId: null,  // 分布页所选快照（ISS-159；null = 最新快照）
  scanWasRunning: false,   // 上次观测时扫描是否在跑（用于结束后刷新当前页）
  /* ISS-158：总览「排查这次变化」带到变化页的真实 a/b 交接。
   * 只存已落库的快照 ID（来自摘要成员），不是页面自造的假基线；
   * 交接后由 overview 一次性写进变化页自有的 a/b 控件并派发 change，
   * 变化页仍按自己的快照列表校验有效性。 */
  pendingChangeEntry: null,
};
