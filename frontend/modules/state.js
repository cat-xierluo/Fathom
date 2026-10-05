/* 应用状态（唯一实例）：跨模块共享的页面/浏览/扫描观测状态。
 * 只存放事实，不放行为；行为分布在 request/router/status 等模块。
 */
export const state = {
  page: "overview",        // 当前激活页（hash 路由解析结果）
  browsePath: null,        // 分布页当前目录（跨刷新保留）
  browseSnapshotId: null,  // 分布页所选快照（ISS-159；null = 最新快照）
  scanWasRunning: false,   // 上次观测时扫描是否在跑（用于结束后刷新当前页）
};
