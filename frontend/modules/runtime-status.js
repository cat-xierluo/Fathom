/* 解读引擎状态的普通面文案（ISS-203 切片 A）。
 *
 * 合同（来源：iss203-status-contract 审查报告 §2「普通面 / 技术详情分层」）：
 * - 普通面（默认可见）只允许一句短状态 + 一个下一动作；技术详情
 *   （reason_code / detail / 路径 / 版本）一律收进默认折叠的
 *   <details class="cfg-details">；
 * - 机器码（reason_code 等枚举值）不得出现在普通面；
 * - 未登记的 reason_code 不得裸出到普通面，只能作为技术详情出现。
 *
 * 本模块是纯数据 + 纯函数（无 DOM/网络依赖），供 Agent 页、设置页与
 * 共用解读面板共享同一份文案，避免各页各写一套映射后互相漂移。
 */

/* ---------- reason_code → 普通面文案 + 下一动作 ----------
 * 覆盖后端 AnalysisError 的 16 个 reason_code 中与「引擎状态」有关的
 * 五个（其余属预览/快照/幂等语义，由解读面板自行处理）：
 *   runtime_not_configured / runtime_unknown / runtime_unsupported /
 *   runtime_unavailable / upgrade_in_progress
 * 每个条目给出：
 *   text    普通面短状态（不含机器码、不含路径）
 *   action  下一动作（人类可读的动作名）
 */
export const RUNTIME_REASON_STATUS = {
  runtime_not_configured: {
    text: "尚未选择解读引擎",
    action: "前往设置选择引擎",
    actionKind: "settings",
  },
  runtime_unknown: {
    text: "已保存的引擎不再受支持",
    action: "前往设置重新检测并选择",
    actionKind: "settings",
  },
  runtime_unsupported: {
    text: "该引擎暂不支持变化解读",
    action: "前往设置改选其他引擎",
    actionKind: "settings",
  },
  runtime_unavailable: {
    text: "解读引擎当前不可用",
    action: "重新检测",
    actionKind: "detect",
  },
  upgrade_in_progress: {
    text: "应用正在升级，暂不能发起分析",
    action: "升级完成后重试",
    actionKind: "retry",
  },
};

/* availability（探测结论）→ 普通面短状态。
 * 与设置页既有徽章词典同口径，但只输出「状态」不带徽章样式。 */
export const RUNTIME_AVAILABILITY_STATUS = {
  ready: "可用",
  unsupported: "该引擎暂不支持",
  not_found: "本机未安装该引擎",
  broken: "引擎启动异常",
};

/* 已知但未登记中文文案的 reason_code → 技术详情里的兜底说明。
 * 目的不是解释机器码，而是让折叠区里有一句人能读的话；机器码本身
 * 只在折叠区出现。 */
export const RUNTIME_REASON_DETAIL = {
  runtime_not_configured: "尚未选择可用的解读引擎，请先在设置中完成检测与选择",
  runtime_unknown: "已保存的解读引擎不再是受支持的候选，请重新检测并选择",
  runtime_unsupported: "该引擎未通过能力门，当前版本暂不可用",
  runtime_unavailable: "已选择的解读引擎当前不可用，请重新检测",
  upgrade_in_progress: "升级事务进行中，分析已被暂停；请待升级完成后重试",
};

/**
 * 取某个 reason_code 的普通面状态描述。
 * 未知码不猜：返回 null，由调用方决定降级路径（折叠区显示原文）。
 */
export function runtimeReasonStatus(code) {
  if (!code) return null;
  return RUNTIME_REASON_STATUS[code] || null;
}

/**
 * 取某个 reason_code 的技术详情说明（折叠区里的一句人话）。
 * 未知码返回 null，调用方应原样展示机器码但不进普通面。
 */
export function runtimeReasonDetail(code) {
  if (!code) return null;
  return RUNTIME_REASON_DETAIL[code] || null;
}

/**
 * 取 availability 的普通面短状态；未知值不猜，原样返回（调用方只
 * 在技术详情里用它）。
 */
export function runtimeAvailabilityStatus(availability) {
  if (!availability) return null;
  return RUNTIME_AVAILABILITY_STATUS[availability] || null;
}

/**
 * 动作类型 → 按钮文案。普通面只给一个下一动作，按钮文案与之同源，
 * 避免「文案说去设置、按钮写重新检测」这类不一致。
 */
export function runtimeActionLabel(actionKind, fallback = "重新生成预览") {
  if (actionKind === "settings") return "前往设置";
  if (actionKind === "detect") return "重新检测";
  if (actionKind === "retry") return "重新生成预览";
  return fallback;
}

/**
 * 探测结论 → 普通面「短状态 + 下一动作」。
 *
 * info: detect 响应里单个 runtime 的字典（availability/reason_code/...）
 * 返回 { text, action, actionKind }：
 *   text       普通面短状态
 *   action     下一动作文案（null 表示无需动作）
 *   actionKind "detect" | "settings" | null —— 供调用方选按钮去向
 */
export function runtimeDetectStatus(info) {
  if (!info) return { text: "尚未检测", action: "检测当前状态", actionKind: "detect" };
  const code = info.reason_code;
  const reason = runtimeReasonStatus(code);
  if (reason) return { text: reason.text, action: reason.action, actionKind: reason.actionKind };
  if (info.availability === "ready") {
    return { text: "可用", action: null, actionKind: null };
  }
  const avail = runtimeAvailabilityStatus(info.availability);
  if (avail) {
    // 未就绪且没有专用 reason_code：按 availability 给通用动作。
    const action = info.availability === "unsupported"
      ? "前往设置改选其他引擎"
      : "重新检测";
    return { text: avail, action, actionKind: actionKindOf(action) };
  }
  return { text: "状态待确认", action: "重新检测", actionKind: "detect" };
}

function actionKindOf(action) {
  if (!action) return null;
  if (action.includes("重新检测") || action.includes("检测")) return "detect";
  if (action.includes("设置")) return "settings";
  return null;
}

/**
 * 已保存配置 + 检测结果的合并状态（普通面三态之一）。
 *
 * saved:  {id, executable, version} | null（来自 GET /api/config 的 analysis.runtime）
 * info:   detect 响应里该 runtime 的字典 | null（未检测过则为 null）
 * enabled: analysis.enabled
 * 返回 { text, action, actionKind, detail }：
 *   text   普通面短状态
 *   action 下一动作文案（null 表示无动作）
 *   detail 一句补充说明（普通面可放，不含机器码与路径）
 */
export function runtimeCombinedStatus({ enabled, saved, info }) {
  if (!enabled) {
    return { text: "变化解读未启用", action: "前往设置开启", actionKind: "settings", detail: "" };
  }
  if (!saved) {
    return { text: "已启用但未选择引擎", action: "前往设置选择", actionKind: "settings", detail: "" };
  }
  if (!info) {
    return { text: "已授权 · 未检测", action: "检测当前状态", actionKind: "detect", detail: "" };
  }
  // 入口漂移：探测到的可执行文件与已保存的不一致（技术细节只进折叠区）。
  const drifted = Boolean(saved.executable && info.executable && info.executable !== saved.executable);
  if (drifted) {
    return {
      text: "引擎入口已变更",
      action: "前往设置重新选择",
      actionKind: "settings",
      detail: "检测到的入口与已保存的入口不同，需要重新选择并授权。",
    };
  }
  const st = runtimeDetectStatus(info);
  return { text: st.text, action: st.action, actionKind: st.actionKind, detail: "" };
}

