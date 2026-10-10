/* 变化页：快照对比（世代号防倒序 + 用户改选保护 + 404 协调）、树形同级
 * 变化表（ISS-148）、共用目录详情（directory-detail.js）、历史日报。
 *
 * 前端责任（ISS-027 模块合同）：
 * - beginRequest 世代号 + pageScoped：迟到的旧响应不能覆盖较新查询；
 * - 仅 frontend/icons.js 的 SVG 图标；零 emoji；
 *
 * 展示（ISS-028）：
 * - 净变化口径：根同口径差分（b.total_kb − a.total_kb），缺根总量显示"无基线"，
 *   绝不把列表行求和当净变化（行含父子重叠且被截断）；
 * - 键盘可达：表行可 Tab 聚焦、Enter 打开详情、Esc 关闭后焦点返回触发行；
 * - 长路径：可一键复制（DESIGN 关键可达性约束）；
 *
 * 树形同级变化（ISS-148）：比对明细表改为消费 ISS-147 的
 * /api/diff/children——默认聚焦数据集根，看的是「同级目录在 a→b 区间内
 * 各变了多少」，不再把 /api/diff 的 Top25 折叠行当树（父子容量不再被
 * 视为独立增长）：
 * - 行状态 measured/first_recorded/unrecorded/structural 直接映射接口
 *   口径；单侧缺测与结构节点不渲染 0（显示 —）；
 * - 目录名（看证据/详情）与展开箭头（逐层展开同级）动作分开；展开按
 *   路径懒加载，同名兄弟在各数据集内唯一；
 * - 「聚焦此目录」+ 面包屑自然下钻（单一树表布局，无双布局开关）；
 *   Esc 先关详情、再逐级返回上级聚焦；
 * - 筛选（all/changed）与排序（delta/size/name）走接口参数，不在本地
 *   重建 Top25；changed 筛选会隐藏无变化方向，说明行（#tree-note）
 *   如实标注；搜索覆盖各级已加载的同级行与已加载子树（ISS-148 审计
 *   返修：命中后代与其祖先链一并显示，祖先行弱化标注），未加载的分页
 *   行不参与，边界在说明行标注；
 * - 分页 has_more/next_cursor 显式呈现为「加载更多」，未加载不冒充
 *   完整同级清单；
 * - 树内每个请求域绑定 treeState 实例 + treeEpoch：改选 a/b、切筛选/
 *   排序即整体作废在途层级请求，迟到的旧层级响应不混入新视图。
 *
 * 数据集收敛（ISS-170）：**不对称收敛**——收敛锚点恒为 b（对比基准侧）：
 * - #sel-b（对比基准侧）**不收敛**：始终列出全部快照（按既有顺序），用户改 b
 *   即切换数据集。这是跨数据集可达性的唯一入口，两侧同收敛会把它堵死；
 * - #sel-a（历史点侧）收敛到**当前 b 所属数据集**，只列同身份快照，跨数据集
 *   组合在选项层不可构造；改 b 后 a 侧选项重填为 b 数据集并按「a 取其前驱」
 *   落定，改 a 只在 b 数据集内收敛、不动 b；
 * - 身份口径复用后端 reports.same_dataset（ISS-176 两档）：plan 档按
 *   plan_id、legacy 档按 (root, min_kb, exclude_names)，逐字段同源、不新造
 *   判定；/api/snapshots 自 ISS-176 起下发 plan_id，前端可取 plan 档；
 * - 残余缝隙（异步竞态、跨页交接）由 loadDiff 发请求前的守卫兜底：跨身份
 *   组合就地给一句说明并保留上一有效读数，不静默、不把 400 伪装成成功。
 *
 * 交互（ISS-093）：选择即比对——两个 select 改选后在选齐时自动加载对比，
 * 无确认按钮；未选齐保持空态 + 引导文案；对比失败在状态行内展示
 * 「重试」小按钮（原确认按钮兼任的失败重试语义收拢到失败态）。
 * 连点竞态由既有 diff 域世代号守卫覆盖（迟到的旧响应不得写入 DOM）；
 * 结果区刷新不抢焦点（改选触发的详情关闭不回焦到已销毁的行）。
 *
 * 分区（ISS-094）：页头下横向 tab 条（比对明细/增长最多/缩减最多/新出现/
 * 消失，默认比对明细；历史日报是 tab 外页尾常驻区）。增长/缩减等排行
 * 分区仍由 /api/diff 渲染（次级可达），数据加载逻辑不变——loadDiff 一次
 * 渲染全部分区，tab 只切可见性；隐藏分区里初始化的图表由 tabs.js 激活时
 * 经 resumeChartsIn 恢复尺寸。tab 状态记 hash（#/changes/grown；刷新保持
 * 由 app.js 在路由前规范化承接，见 tabs.js）。
 *
 * 共享对比上下文（ISS-107）：基线/对比 select、状态行与净变化口径行在
 * tab 条下方的共享面板中，不随明细分区隐藏——任一分区都能判断当前对比
 * 什么并改选日期触发重查。状态三态与实际请求一致：加载中/已完成
 * （「已对比快照 #a → #b」）/失败（内联重试），成功渲染后不得滞留
 * 「正在对比」。增长/缩减图表下方提供同源 Top 12 数据表（含 Finder 入口），
 * 读数不依赖图表悬停。
 */
import { fetchJSON, beginRequest, invalidateRequest, revealInFinder } from "../request.js";
import { state } from "../state.js";
import { createAnalysisPanel } from "../components/analysis-panel.js";
import { datasetKey } from "../dataset.js";
import { fmtKB, fmtDelta, shortPath, escapeHtml } from "../format.js";
import { initChart, hasChart, clearChart } from "../charts.js";
import { initPageTabs } from "../tabs.js";
import { createDirectoryDetail } from "../directory-detail.js";
import { icon } from "../../icons.js";

/* ISS-084：图表色与 style.css :root 语义 token 同源（单一色源，不硬编码）。
 * 脚本为 module（defer），执行时 CSSOM 已就绪。 */
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

let snapshotSelectionRevision = 0;  // 用户改选计数：晚到的快照列表不得覆盖改选结果
let snapshotCatalog = [];           // ISS-170：最近一次 /api/snapshots 原始列表（收敛与守卫的判据）

/* 诊断探针（无副作用）：仅当页面已置 window.__diag 时记录，供实机复现取序列。
 * 不写 console、不改控制流；PM 决定保留或移除。 */
function _diag(kind, detail) {
  if (!window.__diag) return;
  window.__diag.push({ t: Date.now(), kind, ...detail });
}
let currentSort = { key: "delta" };  // 同级排序走接口 sort 参数；方向由接口语义固定
let lastDiff = null;                 // 最近一次成功 diff（/api/diff），用于解读区对位
let diffIntentAB = "";               // ISS-170 R2：最近一次合法发起方落定的 sel 意图（票据）
let changesTabs = null;               // ISS-094 页内二级导航（五分区 tab）

/* ISS-148 树形同级表状态：一次 diff 对应一个 treeState 实例。所有层级
 * 请求闭包持有创建时的实例与 treeEpoch，写回前复核——改选区间/切筛选/
 * 切排序会使旧实例整体作废（迟到响应不混入新视图）。 */
let treeState = null;
let treeEpoch = 0;

/* ISS-151：跨页保留的树视图（离页时快照）。页切换不清 treeState 实例
 * （levels/expanded/focus 随实例存活）；在途层级请求靠 treeState 置空 +
 * treeEpoch 递归作废。返回本页且 a/b 未变时整树恢复，不重发 diff 与层级
 * 请求；改选区间/清空结果即作废。 */
let sessionTree = null;

const TREE_COLUMNS = 6;  // 目录/之前/现在/净变化/状态/操作

function setDiffStatus(message) {
  // textContent 赋值整体替换子节点：若此前失败态挂了「重试」按钮，此处一并清除。
  document.getElementById("diff-status").textContent = message;
}

/* ISS-093：对比失败态——文案 + 状态行内联「重试」小按钮。
 * 原确认按钮兼任的失败重试语义收拢到这里；任何后续 setDiffStatus
 * （加载中/成功/新的失败）都会清掉旧按钮，不残留。 */
function showDiffFailure(message) {
  const el = document.getElementById("diff-status");
  el.replaceChildren(document.createTextNode(message));
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "diff-retry";
  retry.textContent = "重试";
  retry.setAttribute("aria-label", "重新加载快照对比");
  retry.addEventListener("click", () => loadDiff());
  el.appendChild(retry);
}

/* ISS-171：禁用态不得成为死路。#sel-b 全列列出所有快照（ISS-170 不对称收敛），
 * 它是跨数据集切换的**唯一入口**：只要目录里还有别的快照可选，就必须让 b 可用，
 * 用户改 b 即离开当前数据集。「数据集内不足两个快照」只禁 a——a 侧已收敛到 b
 * 所属数据集且该组仅一个快照，a 的任何取值都构不成区间，禁它是对的。
 * exitViaDatasetSwitch 仅在确有其他可选快照时为真（目录为空/加载失败时两侧同禁）。
 * 验收标准：用户永远有出路，任何禁用态下 b 都留有离开当前数据集的改选路径。 */
function setDiffControlsEnabled(enabled, { exitViaDatasetSwitch = false } = {}) {
  document.getElementById("sel-a").disabled = !enabled;
  document.getElementById("sel-b").disabled = !enabled && !exitViaDatasetSwitch;
}

function clearDiffResults() {
  ["chart-grown", "chart-shrunk"].forEach((id) => {
    if (hasChart(id)) clearChart(id);
    else document.getElementById(id).replaceChildren();
  });
  ["tbl-added", "tbl-removed"].forEach((id) => {
    document.querySelector(`#${id} tbody`).innerHTML =
      '<tr><td colspan="3" class="hint">暂无可比较数据</td></tr>';
  });
  // ISS-107：增长/缩减分区的图表同源数据表随结果一起清空
  ["tbl-grown-top", "tbl-shrunk-top"].forEach((id) => {
    document.querySelector(`#${id} tbody`).innerHTML =
      '<tr><td colspan="5" class="hint">暂无可比较数据</td></tr>';
  });
  document.getElementById("changes-body").innerHTML =
    '<tr><td colspan="6" class="hint">暂无可比较数据</td></tr>';
  document.getElementById("changes-net").hidden = true;
  document.getElementById("changes-foot").hidden = true;
  hideTreeChrome();
  // ISS-035C：解读区随对比结果一起隐藏并重置会话态（离页/改选不隐式重发）
  resetAnalysisSession({ hide: true });
  // 关闭详情（如打开）：焦点留在用户当前操作处（清空不是触发行动作）
  directoryDetail.close({ restoreFocus: false });
  // 作废在途树层级请求
  treeEpoch += 1;
  treeState = null;
  lastDiff = null;
  sessionTree = null;
}

/* ISS-176：数据集身份口径升级为两档——与后端 fathom/reports.py::same_dataset
 * 完全同口径（ISS-153 起）：
 * - plan 档（plan_id 非空）：只按 plan_id 判等。v8 的 plan_id 已编码规范根、
 *   范围卷、计量版本、阈值与排除掩码，同 plan 即同数据集；同路径换卷或换
 *   计划都形成新计划，不可比——所以这里**不**再拼三元组，否则同 plan 内
 *   口径微调会被误判成两个数据集；
 * - legacy 档（plan_id 为空）：沿用 (root, min_kb, exclude_names) 三元组
 *   （ISS-021/ISS-066 口径不变），归一化照抄后端语义：min_kb 为 NULL 是
 *   「该根的口径未知」数据集，彼此可比较；exclude_names 缺列（v5 之前）
 *   兜底为 ""，与新写入的「无配置」同身份。
 * 两档不混：key 带档位前缀，故 plan 行与 legacy 行永不相等（后端
 * same_dataset 对混搭同样返回 false）。/api/snapshots 自 ISS-176 起下发
 * plan_id，前端此前拿不到该字段、只能把新身份行按三元组并进 legacy 窗口。
 * 仅收敛判定改口径：不对称收敛与票据语义（R2）零改动。 */

function findSnapshot(id) {
  return snapshotCatalog.find((s) => String(s.id) === String(id)) || null;
}

/** 收敛锚点：当前 b（/api/snapshots 已按时间倒序，列表首个即最新）。
 * 不对称收敛下锚点恒为 b——它是「切换数据集」的唯一入口，也是口径基准。
 * b 不可用时兜底取列表首个，保证总有一个合法 b。 */
function anchorSnapshot(snaps, selectedB) {
  const byId = new Map(snaps.map((s) => [String(s.id), s]));
  return byId.get(String(selectedB)) || snaps[0] || null;
}

/** 与锚点同数据集身份的快照（保持原顺序，不重排）。 */
function sameDatasetGroup(snaps, anchor) {
  if (!anchor) return [];
  const key = datasetKey(anchor);
  return snaps.filter((s) => datasetKey(s) === key);
}

function replaceSnapshotOptions(select, snaps) {
  const options = snaps.map((s) => {
    const label = `#${s.id} ${s.created_at.slice(0, 16).replace("T", " ")}`;
    return new Option(label, String(s.id));
  });
  select.replaceChildren(...options);
}

/** 不对称收敛：b 侧**不收敛**（全列，用户改 b 即切换数据集），a 侧收敛到
 * b 所属数据集（只列同身份快照），跨数据集组合在选项层不可构造。
 * - b = keepB 仍在目录内则保留，否则取目录首个（最新）；
 * - a = keepA 仍在 b 数据集内则保留，否则按「a 取其前驱」落定（组内 b 之外
 *   的最新一个），保证自动对比总有合法区间；
 * - userChanged 非空（用户改选任一侧）时**不排除** keepA === b：那是用户显式
 *   造成的无效区间，交由后端 409/失败态如实暴露，不静默改写用户刚做的选择。
 *   只在初始加载/快照列表刷新（userChanged 为空）时强制取前驱。
 * 返回落定值与 a 侧所属的数据集分组（供「数据集内不足两个」判定复用）。 */
function applyConvergedOptions(selA, selB, snaps, { keepA, keepB, userChanged = "" } = {}) {
  // 用户显式清空某一侧是「请选择基线与对比快照」的合法意图，不静默补回合法值
  const userEmptyA = Boolean(userChanged) && String(keepA) === "";
  const userEmptyB = Boolean(userChanged) && String(keepB) === "";
  const nextB = userEmptyB
    ? ""
    : (findSnapshot(keepB) ? String(keepB) : String((snaps[0] || {}).id ?? ""));
  // a 侧收敛的锚：正常恒为 b；b 被用户清空时退回 a 自身，避免两侧一起变空
  const group = sameDatasetGroup(snaps, findSnapshot(nextB) || findSnapshot(keepA) || snaps[0] || null);
  const ids = group.map((s) => String(s.id));
  const keepAOk = ids.includes(String(keepA)) && String(keepA) !== "";
  const predecessor = ids.find((id) => id !== nextB) || "";
  const nextA = userEmptyA
    ? ""
    : (keepAOk && (userChanged || String(keepA) !== nextB) ? String(keepA) : predecessor);
  replaceSnapshotOptions(selB, snaps);  // b 侧不收敛：跨数据集可达性入口
  replaceSnapshotOptions(selA, group);  // a 侧收敛到 b 所属数据集
  selA.value = nextA;
  selB.value = nextB;
  return { nextA, nextB, group };
}

async function loadSnapshotsForDiff({ notice = "", restore = false } = {}) {
  const request = beginRequest("snapshots");
  invalidateRequest("diff");
  const selA = document.getElementById("sel-a"), selB = document.getElementById("sel-b");
  const previousA = selA.value, previousB = selB.value;
  const selectionRevision = snapshotSelectionRevision;
  _diag("catalog-request", { selectionRevision, a: previousA, b: previousB });
  let snaps;
  try {
    snaps = await fetchJSON("/api/snapshots");
  } catch (e) {
    if (!request.current()) return;
    invalidateRequest("diff");
    clearDiffResults();
    setDiffControlsEnabled(false);
    setDiffStatus(e.status === 0
      ? "无法连接本地服务，快照列表暂不可用。"
      : `快照列表加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`);
    return;
  }
  _diag("catalog-response", { selectionRevision, currentRevision: snapshotSelectionRevision,
    current: request.current(), a: selA.value, b: selB.value });
  if (!request.current()) return;

  // ISS-170：缓存原始列表（收敛与守卫共用），再按当前生效数据集收敛两侧选项
  snapshotCatalog = snaps;
  // 请求发起后有较新的用户/CTA 意图：只更新目录事实，不重新选择或补发 diff。
  // replaceChildren 会把 select 重置为首项，因此重建 b 时须保留当前取值。
  // 旧目录未包含较新选中的点时保留现有选项，交给该意图自己的请求校验，
  // 不把缺失当成替用户选择其他区间的授权。
  if (snapshotSelectionRevision !== selectionRevision) {
    const currentB = selB.value;
    if (!currentB || snaps.some((s) => String(s.id) === currentB)) {
      replaceSnapshotOptions(selB, snaps);
      selB.value = currentB;
    }
    _diag("catalog-superseded", { selectionRevision, currentRevision: snapshotSelectionRevision,
      a: selA.value, b: selB.value });
    return;
  }
  const selectedA = previousA;
  const selectedB = previousB;
  const anchor = anchorSnapshot(snaps, selectedB);
  const group = sameDatasetGroup(snaps, anchor);
  const ids = group.map((s) => String(s.id));
  const allIds = snaps.map((s) => String(s.id));
  // b 侧全列故只需在目录内有效；a 侧收敛后须落在 b 所属数据集内
  const validA = ids.includes(selectedA), validB = allIds.includes(selectedB);
  // 收敛后原选择若被排除在数据集外（异数据集/该数据集内已不可用），按既有形状落定
  const { nextA, nextB } = applyConvergedOptions(selA, selB, snaps,
    { keepA: selectedA, keepB: selectedB });
  _diag("catalog-settled", { selectionRevision, a: nextA, b: nextB });

  const fellBack = Boolean((selectedA && !validA) || (selectedB && !validB));

  /* ISS-151：同会话返回本页且 a/b 未变 → 整树恢复（展开/聚焦/滚动/详情），
   * 不重发 diff 与层级请求；在途层（离页时请求被打断的）单独重取。
   * 改选过区间或结果被清空时 sessionTree 已作废，走正常加载。 */
  const st = sessionTree;
  sessionTree = null;
  if (restore && st && !fellBack && nextA && nextB &&
      st.a === nextA && st.b === nextB) {
    treeEpoch += 1;
    treeState = st.state;
    for (const [p, lv] of treeState.levels) {
      if (lv.status === "loading") loadTreeLevel(p);  // 离页打断的在途层重取
    }
    renderTree();
    if (st.detailPath && findTreeRow(st.detailPath)) {
      openTreeDetail(st.detailPath, null);  // 重开详情（trend/browse 只读重取）
    }
    if (st.focusPath) {
      // open() 会把焦点交给关闭按钮：恢复后按离页焦点回到触发行；
      // preventScroll 避免聚焦把滚动位置拉走（滚动恢复以离页值为准）。
      const row = document.querySelector(
        `#changes-body tr[data-path="${CSS.escape(st.focusPath)}"]`);
      if (row) row.focus({ preventScroll: true });
    }
    if (typeof st.scrollTop === "number") {
      const container = document.querySelector(".page-container");
      if (container) container.scrollTop = st.scrollTop;  // 滚动恢复最后落定
    }
    // ISS-151 回归修复：树恢复不重发 diff，但 AI 解读区状态独立于树域——
    // 它跟随当前授权配置与在途任务，离页期间可能已变化（如设置页刚启用）。
    // 不重取则解读区滞留离页前的 DOM（如 disabled 态），idle 永不出现。
    loadAnalysisPanel();
    // ISS-171：树恢复必须重估禁用态。恢复条件里 nextA 非空即蕴含该数据集已有
    // 两个快照（本组凑不出前驱时 nextA 为空串），故两侧都应可用；离页前若残留
    // disabled（另一数据集快照被清理、退回单快照），在此解开，不让它跨页存续。
    setDiffControlsEnabled(true);
    return;
  }

  if (group.length < 2) {
    invalidateRequest("diff");
    clearDiffResults();
    // ISS-171：本数据集凑不出区间，但 b 侧全列仍是切换数据集的入口——同禁两侧
    // 会把用户锁死在无区间状态（且禁用态跨页存续，返回变化页时只有 group>=2
    // 的正常分支会解开，等于无路可走）。故只禁 a，b 保持可用。
    setDiffControlsEnabled(false, { exitViaDatasetSwitch: snaps.length > 1 });
    // 整个库只有一个快照：沿用既有单快照文案（其他套件按此断言）。
    // 库里有多个快照、但当前生效数据集内不足两个：另给数据集口径的说明，
    // 不得让用户以为「再扫一次就能比较」（同库内已有别的数据集快照）。
    if (group.length === 1 && snaps.length > 1) {
      setDiffStatus("当前生效数据集只有一个快照；需要同一数据集内另一个不同日期的有效快照才能比较，其他数据集的快照不与它构成区间。可改「对比基准」下拉切换到其他数据集。");
    } else {
      setDiffStatus(group.length === 1
        ? "基线已建立；需要另一个不同日期的有效快照才能比较，分布现在可用。"
        : "尚无快照，请先扫描建立基线。");
    }
    return;
  }

  const fallbackNotice = notice || (fellBack
    ? "所选快照已更新或不再可用，已切换到最近有效快照。"
    : "");
  setDiffControlsEnabled(true);
  /* ISS-170 R2：本函数从 enter 钩子/恢复流程异步到达，await /api/snapshots
   * 期间用户可能已改选。此时 nextA/nextB（按发起时意图收敛）已不代表用户
   * 意图——实测三连发 (1,4)/(1,2)/(1,4) 中最后一发即此路径，它以新世代
   * 作废用户自己的 diff，渲染复核门又拦掉它，净变化行无人更新。sel 当前值
   * 与收敛结果不一致说明用户意图已更新，交给用户改选触发的 loadDiff 接管，
   * 这里不再补发。 */
  diffIntentAB = `${nextA}\u0000${nextB}`;
  await loadDiff({ retryOnMissing: false, successMessage: fallbackNotice });
}

/* ---------- 净变化与可排序表 ---------- */

function computeNet(d) {
  // 根同口径净变化 = b.total_kb − a.total_kb（同一响应内两快照的根累计 KiB）。
  // grown/shrunk 行按 fold_changes 设计可含父子重叠（父行包含子行，DEC-005），
  // 且经 topn 截断与 min_delta_kb 过滤——逐行求和不等于任何口径的净变化，
  // 禁止回退到求和。任一侧缺 total_kb（如无基线）时返回 null。
  const prev = Number(d.a && d.a.total_kb), curr = Number(d.b && d.b.total_kb);
  if (!Number.isFinite(prev) || !Number.isFinite(curr)) return null;
  return curr - prev;
}

/* ---------- 树形同级变化表（ISS-148，消费 /api/diff/children） ---------- */

const TREE_STATUS_TEXT = {
  measured: "已测量",
  first_recorded: "首次记录",
  unrecorded: "未记录",
  structural: "结构节点",
};

function statusBadge(st) {
  const text = escapeHtml(TREE_STATUS_TEXT[st] || String(st ?? ""));
  if (st === "unrecorded") return `<span class="st st-unrecorded"><span class="st-dot"></span>${text}</span>`;
  if (st === "first_recorded") return `<span class="st st-first"><span class="st-dot"></span>${text}</span>`;
  if (st === "structural") return `<span class="st st-structural"><span class="st-dot"></span>${text}</span>`;
  if (st === "restricted") return `<span class="st st-restricted"><span class="st-dot"></span>${text}</span>`;
  return `<span class="st st-measured"><span class="st-dot"></span>${text}</span>`;
}

function deltaCell(row) {
  if (row.delta == null) {
    // 单侧缺测不渲染 0：b 未记录（曾有）与 b 首次入库（现有）分别标注
    if (row.status === "unrecorded" || row.status === "first_recorded") {
      if (row.prev == null && row.curr != null) return `<span class="delta-none">—（现有 ${escapeHtml(fmtKB(row.curr))}）</span>`;
      if (row.curr == null && row.prev != null) return `<span class="delta-none">—（曾有 ${escapeHtml(fmtKB(row.prev))}）</span>`;
    }
    return `<span class="delta-none">—</span>`;
  }
  const cls = row.delta > 0 ? "delta-grow" : row.delta < 0 ? "delta-shrink" : "";
  return `<span class="${cls}">${escapeHtml(fmtDelta(row.delta))}</span>`;
}

/** 数据集根下的父路径（不越出根）；已是根返回 null。 */
function parentWithinRoot(path, root) {
  if (!path || path === root) return null;
  const idx = path.lastIndexOf("/");
  const parent = idx <= 0 ? "/" : path.slice(0, idx);
  if (parent.length < root.length) return root;  // 防御：越出根时回到根
  return parent;
}

/** 请求一个层级的同级行。append 时携带游标续页；任何写回前复核
 * treeState 实例、treeEpoch 与请求域，三者任一变化即丢弃。 */
async function loadTreeLevel(path, { cursor = null, append = false } = {}) {
  const t = treeState;
  if (!t) return null;
  const epoch = treeEpoch;
  const request = beginRequest(`tree:${path}`);
  if (!append || !t.levels.has(path)) {
    t.levels.set(path, { rows: [], pagination: null, parent: null,
      status: "loading", error: null });
    renderTree();
  }
  const level = t.levels.get(path);
  const params = new URLSearchParams({ a: t.a, b: t.b, path });
  params.set("filter", t.filter);
  params.set("sort", t.sort);
  if (cursor) params.set("cursor", cursor);
  try {
    const d = await fetchJSON(`/api/diff/children?${params.toString()}`);
    if (treeState !== t || epoch !== treeEpoch || !request.current()) return null;
    level.rows = append ? level.rows.concat(d.children || []) : (d.children || []);
    level.pagination = d.pagination || null;
    level.parent = d.parent || null;
    level.status = "ready";
    level.error = null;
    renderTree();
    return d;
  } catch (e) {
    if (treeState !== t || epoch !== treeEpoch || !request.current()) return null;
    level.status = "error";
    level.error = e;
    renderTree();
    return null;
  }
}

/** 重置树状态并从数据集根开始加载（loadDiff 成功后调用）。 */
function resetTree(d) {
  treeEpoch += 1;
  const root = (d.a && d.a.root) || "/";
  treeState = {
    a: String(d.a.id),
    b: String(d.b.id),
    root,
    focus: root,
    filter: document.getElementById("changes-filter")?.value || "all",
    sort: currentSort.key,
    levels: new Map(),
    expanded: new Set(),
  };
  loadTreeLevel(root);
}

function treeAuxRow(depth, html, cls = "") {
  const indent = `<span class="tree-indent" style="width:${depth * 18}px" aria-hidden="true"></span>`;
  return `<tr class="tree-aux ${cls}"><td colspan="${TREE_COLUMNS}">${indent}${html}</td></tr>`;
}

/** 行自身路径是否命中搜索词（完整路径包含，与既有过滤口径一致）。 */
function rowMatches(r, search) {
  return r.path.toLowerCase().includes(search);
}

/** 该目录的已加载子树内是否存在命中行：仅沿 levels 中已就绪的层级向下看，
 * 不发起请求——未加载的分页与未展开未加载的层级不参与（搜索边界如实）。 */
function subtreeHasMatch(path, search) {
  const level = treeState.levels.get(path);
  if (!level || level.status !== "ready") return false;
  return level.rows.some((r) => rowMatches(r, search) || subtreeHasMatch(r.path, search));
}

function renderLevelRows(path, depth, search) {
  const level = treeState.levels.get(path);
  if (!level) return "";
  if (level.status === "error") {
    const msg = level.error.status === 0
      ? "无法连接本地服务，同级变化暂不可用。"
      : `同级变化加载失败${level.error.status ? `（HTTP ${level.error.status}）` : ""}：${escapeHtml(level.error.message)}`;
    return treeAuxRow(depth,
      `<span class="tree-error-text">${msg}</span> ` +
      `<button class="diff-retry" type="button" data-tree-retry="${escapeHtml(path)}" ` +
      `aria-label="重试加载 ${escapeHtml(path)} 的同级变化">重试</button>`);
  }
  if (level.status === "loading") {
    return treeAuxRow(depth, `<span class="hint">正在加载同级变化…</span>`);
  }
  // 搜索匹配：行自身路径命中，或其已加载子树内有命中（后者以祖先链弱化保留，
  // 保证已加载后代不因父行不匹配而失联）；未加载的分页行不参与。
  const rows = search
    ? level.rows.filter((r) => rowMatches(r, search) || subtreeHasMatch(r.path, search))
    : level.rows;
  let out = "";
  if (!rows.length) {
    out = treeAuxRow(depth,
      search
        ? `<span class="hint">已加载的同级行中没有匹配“${escapeHtml(search)}”的目录。</span>`
        : `<span class="hint">该目录下没有已入库的子目录记录（可能全部低于入库阈值）。</span>`);
  }
  // ISS-148 审计返修：深度优先输出——每行之后紧邻其子树（若有），再输出下一
  // 兄弟，使 DOM 邻接关系与目录包含关系一致；同级之间保持 API 排序不变。
  // 搜索激活时对每个保留行都递归已加载子层（子层未加载自然为空串），命中
  // 后代因此可见；展开状态集合不被搜索读写，清空搜索后恢复折叠视图。
  for (const r of rows) {
    out += treeNodeHtml(r, depth, Boolean(search) && !rowMatches(r, search));
    if (search || treeState.expanded.has(r.path)) {
      out += renderLevelRows(r.path, depth + 1, search);
    }
  }
  const pg = level.pagination;
  if (pg && pg.has_more && pg.next_cursor) {
    const loaded = search
      ? rows.length
      : (pg.offset || 0) + (pg.returned || 0);
    out += treeAuxRow(depth, `
      <button class="tree-load-more" type="button" data-load-more="${escapeHtml(path)}"
              data-cursor="${escapeHtml(pg.next_cursor)}" data-test="tree-load-more"
              aria-label="加载 ${escapeHtml(path)} 的更多同级行">
        加载更多（已显示 ${loaded} / 共 ${escapeHtml(String(pg.total))} 个同级行）</button>`);
  }
  return out;
}

function treeNodeHtml(r, depth, ancestorOnly = false) {
  const expanded = treeState.expanded.has(r.path);
  const indent = `<span class="tree-indent" style="width:${depth * 18}px" aria-hidden="true"></span>`;
  const toggle = r.has_children
    ? `<button class="tree-toggle" type="button" data-expand="${escapeHtml(r.path)}"
               data-test="tree-expand" aria-expanded="${expanded}"
               aria-label="${expanded ? "收起" : "展开"} ${escapeHtml(r.name)} 的下级"
               title="${expanded ? "收起" : "展开"}">${icon("chevron", 13)}</button>`
    : `<span class="tree-toggle tree-toggle-leaf" aria-hidden="true"></span>`;
  const prevTxt = r.old_kb == null ? `<span class="delta-none">—</span>` : `<span class="num">${escapeHtml(fmtKB(r.old_kb))}</span>`;
  const currTxt = r.new_kb == null ? `<span class="delta-none">—</span>` : `<span class="num">${escapeHtml(fmtKB(r.new_kb))}</span>`;
  // 搜索祖先链弱化：本行不命中但已加载后代命中——名称置灰并加「子级命中」
  // 标注（读作导航节点），数值照常，不冒充命中行本身。
  const nameCls = ancestorOnly ? "tree-name tree-name-ancestor" : "tree-name";
  const ancestorMark = ancestorOnly
    ? `<span class="tree-ancestor-mark" title="本行因下级命中搜索而保留">子级命中</span>`
    : "";
  return `<tr class="focusable${ancestorOnly ? " tree-ancestor-hit" : ""}" tabindex="0" role="button" data-path="${escapeHtml(r.path)}"
              data-depth="${depth}" aria-label="查看目录详情：${escapeHtml(r.path)}">
    <td class="dir-cell">${indent}${toggle}<button class="${nameCls}" type="button"
            data-detail="${escapeHtml(r.path)}" title="${escapeHtml(r.path)}">${escapeHtml(r.name)}</button>${ancestorMark}
      <button class="copy-path" type="button" data-copy="${escapeHtml(r.path)}"
              aria-label="复制路径 ${escapeHtml(r.path)}" title="复制路径">复制</button>
    </td>
    <td class="num">${prevTxt}</td>
    <td class="num">${currTxt}</td>
    <td class="num">${deltaCell({ delta: r.delta_kb, prev: r.old_kb, curr: r.new_kb, status: r.status })}</td>
    <td>${statusBadge(r.status)}</td>
    <td>
      <span class="row-actions">
        <button class="btn-mini" data-focus="${escapeHtml(r.path)}" data-test="tree-focus"
                aria-label="聚焦此目录" title="聚焦此目录">${icon("scope", 14)}</button>
        <button class="btn-mini" data-reveal="${escapeHtml(r.path)}"
                aria-label="在 Finder 中显示" title="在 Finder 中显示">${icon("folderOpen", 14)}</button>
      </span>
    </td>
  </tr>`;
}

function renderTree() {
  if (!treeState) return;
  const tbody = document.getElementById("changes-body");
  const search = (document.getElementById("changes-search")?.value || "").trim().toLowerCase();
  updateTreeChrome();
  const html = renderLevelRows(treeState.focus, 0, search);
  tbody.innerHTML = html ||
    `<tr><td colspan="${TREE_COLUMNS}" class="hint">该目录下没有已入库的子目录记录（可能全部低于入库阈值）。</td></tr>`;
  updateSortIndicators();
}

/** 面包屑 / 本级目录摘要 / 动态说明（隐藏方向、搜索范围）。 */
function updateTreeChrome() {
  const crumbsEl = document.getElementById("tree-crumbs");
  const parentEl = document.getElementById("tree-parent");
  const noteEl = document.getElementById("tree-note");
  const t = treeState;
  const search = (document.getElementById("changes-search")?.value || "").trim().toLowerCase();
  // 面包屑：focus 非根时展示祖先链（根 → … → 当前聚焦）
  if (t && t.focus !== t.root) {
    const chain = [];
    if (t.root === "/") {
      chain.push("/");
      const segs = t.focus.split("/").filter(Boolean);
      let acc = "";
      for (const seg of segs) { acc += "/" + seg; chain.push(acc); }
    } else {
      chain.push(t.root);
      const rel = t.focus.slice(t.root.length + 1);
      let acc = t.root;
      for (const seg of rel.split("/")) { acc += "/" + seg; chain.push(acc); }
    }
    const crumbs = [];
    chain.forEach((p, i) => {
      const name = p === "/" ? "/" : p === t.root ? t.root : p.slice(p.lastIndexOf("/") + 1);
      if (i) crumbs.push('<span class="crumb-sep">/</span>');
      crumbs.push(`<button class="crumb${p === t.focus ? " current" : ""}" data-crumb="${escapeHtml(p)}"
        title="${escapeHtml(p)}">${escapeHtml(name)}</button>`);
    });
    crumbsEl.innerHTML = crumbs.join("");
    crumbsEl.hidden = false;
    crumbsEl.querySelectorAll("[data-crumb]").forEach((b) =>
      b.addEventListener("click", () => focusTreePath(b.dataset.crumb)));
  } else {
    crumbsEl.hidden = true;
    crumbsEl.innerHTML = "";
  }
  // 本级目录摘要：聚焦非根时展示其自身区间值（父行口径 = 含全部后代）
  const level = t && t.levels.get(t.focus);
  const parent = level && level.parent;
  if (t && t.focus !== t.root && level && level.status === "ready" && parent) {
    const oldTxt = parent.old_kb == null ? "—" : fmtKB(parent.old_kb);
    const newTxt = parent.new_kb == null ? "—" : fmtKB(parent.new_kb);
    const deltaTxt = parent.delta_kb == null ? "—" : fmtDelta(parent.delta_kb);
    const unknown = parent.old_kb == null && parent.new_kb == null;
    parentEl.innerHTML =
      `<strong>${escapeHtml(t.focus)}</strong> ` +
      (unknown
        ? `<span class="hint">本级目录无直接入库记录（结构导航节点）：大小与差分未知，不以 0 冒充；展开可见其下已入库后代。</span>`
        : `<span class="num">${escapeHtml(oldTxt)} → ${escapeHtml(newTxt)}</span>` +
          `<span>本级净变化 <strong class="${parent.delta_kb > 0 ? "delta-grow" : parent.delta_kb < 0 ? "delta-shrink" : "delta-none"}">${escapeHtml(deltaTxt)}</strong></span>` +
          `<span class="hint">本级大小为累计值（含全部后代），不与子行相加。</span>`) +
      ` ${statusBadge(parent.status)}`;
    parentEl.hidden = false;
  } else if (parentEl) {
    parentEl.hidden = true;
    parentEl.innerHTML = "";
  }
  // 动态说明：changed 筛选的隐藏方向 + 搜索只作用于已加载行
  const notes = [];
  if (t && t.filter === "changed") {
    notes.push("「仅变化」隐藏了本级无变化且无变化后代的同级行；有变化后代的目录行会保留以供展开，切换回「全部同级行」可查看完整同级清单。");
  }
  if (search) {
    notes.push("搜索覆盖各级已加载的同级行与其已加载的子树，命中的下级行与其祖先链（标「子级命中」）一并显示；有「加载更多」时，未载入的分页行不参与搜索。");
  }
  if (noteEl) {
    if (notes.length) {
      noteEl.textContent = notes.join("");
      noteEl.hidden = false;
    } else {
      noteEl.hidden = true;
      noteEl.textContent = "";
    }
  }
}

function hideTreeChrome() {
  ["tree-crumbs", "tree-parent", "tree-note"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) { el.hidden = true; el.innerHTML = ""; }
  });
}

function updateSortIndicators() {
  // 接口排序语义：delta/size 为降序口径，name 为升序口径（方向不可切换）
  document.querySelectorAll("#changes-table th").forEach((th) => {
    const btn = th.querySelector(".th-sort");
    if (!btn) return;
    if (btn.dataset.sort === currentSort.key) {
      th.setAttribute("aria-sort", currentSort.key === "name" ? "ascending" : "descending");
    } else {
      th.removeAttribute("aria-sort");
    }
  });
}

function findTreeRow(path) {
  if (!treeState) return null;
  for (const level of treeState.levels.values()) {
    const hit = level.rows.find((r) => r.path === path);
    if (hit) return hit;
  }
  return null;
}

/** 行内复制路径（DESIGN 长路径可达性）；失败不冒充成功。 */
async function copyTreePath(btn) {
  const text = btn.dataset.copy;
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
    else {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    btn.textContent = "已复制";
    btn.classList.add("copied");
    setTimeout(() => {
      btn.textContent = "复制";
      btn.classList.remove("copied");
    }, 1200);
  } catch (_) {
    btn.textContent = "复制失败";
    setTimeout(() => { btn.textContent = "复制"; }, 1200);
  }
}

function toggleTreeExpand(path) {
  if (!treeState) return;
  if (treeState.expanded.has(path)) {
    treeState.expanded.delete(path);
    renderTree();
  } else {
    treeState.expanded.add(path);
    if (!treeState.levels.has(path)) loadTreeLevel(path);  // 完成后自渲染
    else renderTree();
  }
}

/** 聚焦某目录：面包屑下钻的主入口；返回加载完成的 Promise。
 * 先写 focus 再加载：加载中的加载态/面包屑/本级摘要按新聚焦渲染。 */
async function focusTreePath(path) {
  if (!treeState || !path) return;
  treeState.focus = path;
  if (!treeState.levels.has(path)) await loadTreeLevel(path);
  else renderTree();
}

/** 解读证据定位（ISS-035C）：先看行是否在场；不在场则复位筛选、把焦点
 * 切到目标父层再定位；仍不可见时如实说明，不猜行位置。 */
async function revealTreePath(target) {
  if (!treeState) return;
  const rowSel = `#changes-body tr[data-path="${CSS.escape(target)}"]`;
  let row = document.querySelector(rowSel);
  if (!row && treeState.filter !== "all") {
    // changed 筛选可能藏住目标行：复位为全部同级行并等重载完成
    const sel = document.getElementById("changes-filter");
    if (sel) sel.value = "all";
    await onTreeFilterChange();
    row = document.querySelector(rowSel);
  }
  if (!row) {
    const parent = parentWithinRoot(target, treeState.root);
    if (parent && parent !== treeState.focus) await focusTreePath(parent);
    row = document.querySelector(rowSel);
  }
  if (!row) {
    const noteEl = document.getElementById("tree-note");
    if (noteEl) {
      noteEl.textContent = "证据路径不在当前同级表中（可能未达入库阈值、或是聚焦目录本身或其祖先）；其区间值可见于本级目录摘要或上方净变化口径。";
      noteEl.hidden = false;
    }
    return;
  }
  row.scrollIntoView({ block: "center" });
  row.classList.add("analysis-locate-flash");
  setTimeout(() => row.classList.remove("analysis-locate-flash"), 2400);
}

function onTreeFilterChange() {
  if (!treeState) return Promise.resolve();
  treeEpoch += 1;  // 作废全部在途层级请求（含续页）
  treeState.filter = document.getElementById("changes-filter")?.value || "all";
  treeState.levels.clear();
  treeState.expanded.clear();
  return loadTreeLevel(treeState.focus);
}

function onTreeSortChange(key) {
  if (!key || key === currentSort.key) return;
  currentSort.key = key;
  updateSortIndicators();
  if (!treeState) return;
  treeEpoch += 1;
  treeState.sort = key;
  treeState.levels.clear();
  treeState.expanded.clear();
  loadTreeLevel(treeState.focus);
}

function renderNetLine(d) {
  const net = computeNet(d);
  const netEl = document.getElementById("changes-net");
  const footEl = document.getElementById("changes-foot");
  const measured = (d.grown || []).length + (d.shrunk || []).length;
  const unrec = (d.added || []).length + (d.removed || []).length;
  const span = `${String(d.a?.created_at || "").slice(0, 10)} → ${String(d.b?.created_at || "").slice(0, 10)}`;
  const netHtml = net == null
    ? `<strong class="delta-none">无基线</strong>`
    : `<strong class="${net > 0 ? "delta-grow" : net < 0 ? "delta-shrink" : "delta-none"}">${escapeHtml(fmtDelta(net))}</strong>`;
  // 根目录 X → Y 与净变化同源（都是 a/b.total_kb），数值上严格一致
  const rootNote = net == null
    ? `<span class="hint">${span} 快照缺少根总量（无基线），净变化不可知；不用列表行求和推算。</span>`
    : `<span class="hint">根同口径差分：根目录 ${escapeHtml(fmtKB(d.a.total_kb))} → ${escapeHtml(fmtKB(d.b.total_kb))}（行值不可相加）</span>`;
  const unrecNote = measured === 0 && unrec > 0
    ? `<span class="hint">${span} 期间没有 ≥1MB 可测量行，另有 ${unrec} 个未记录目录，不能判为"无变化"。</span>`
    : "";
  netEl.innerHTML = `<span>根同口径净变化 ${netHtml}</span>` + rootNote + unrecNote;
  netEl.hidden = false;
  footEl.hidden = false;
}

/* ISS-093：选择即比对——select change 后选齐即自动加载，无确认按钮。
 * 连点触发多次 loadDiff 时，diff 域世代号守卫保证只渲染最后一次；
 * 未选齐（某侧为空）保持空态 + 引导文案，不发请求。
 * select 的 change 由键盘改选同样派发（原生行为），路径不变。 */
function onSelectionChange(changedId, { handoff = false } = {}) {
  // 交接也是较新的区间意图，须作废在途目录的旧落定。
  // handoff 仍不作为 userChanged：入口空 a 可以找真实前驱，用户显式空值不补。
  snapshotSelectionRevision += 1;
  const _selA = document.getElementById("sel-a");
  const _selB = document.getElementById("sel-b");
  _diag("onSelectionChange", { changedId, handoff,
    catalogLength: snapshotCatalog.length, userRevision: snapshotSelectionRevision,
    before: { a: _selA.value, b: _selB.value,
      aOptions: [..._selA.options].map((o) => o.value),
      bOptions: [..._selB.options].map((o) => o.value) } });
  sessionTree = null;  // 用户改选：跨页保留的旧树视图作废
  const selA = document.getElementById("sel-a");
  const selB = document.getElementById("sel-b");
  // ISS-170：不对称收敛——锚点恒为 b。改 b 即切换数据集（b 侧全列），a 侧
  // 随之重填为 b 数据集并按「a 取其前驱」落定；改 a 只在 b 数据集内收敛，
  // b 侧选项与取值不动。目录未就绪时不动选项（保留既有选项，交给 loadDiff
  // 的守卫与后端闸门处理）。
  if (snapshotCatalog.length) {
    const { group } = applyConvergedOptions(selA, selB, snapshotCatalog,
      { keepA: selA.value, keepB: selB.value, userChanged: handoff ? "" : changedId });
    // ISS-171：改 b 换到够两个快照的数据集后必须把 a 一并放开——否则
    // 「单快照数据集只禁 a」会把 a 永久留在禁用态，即便新数据集已可构成区间。
    setDiffControlsEnabled(group.length >= 2,
      { exitViaDatasetSwitch: snapshotCatalog.length > 1 });
  }
  const a = selA.value;
  const b = selB.value;
  if (!a || !b) {
    invalidateRequest("diff");
    clearDiffResults();
    setDiffStatus("请选择基线与对比快照，选齐后自动对比。");
    return;
  }
  // 改选使旧对比的详情侧栏失效：关闭并作废在途 trend/browse，迟到响应
  // 不得写入（焦点留在用户正在操作的 select，不回焦已销毁的行）。
  directoryDetail.invalidate();
  diffIntentAB = `${a}\u0000${b}`;
  loadDiff();
}

/** ISS-170 守卫：发请求前判跨数据集组合。选项层已收敛，这里兜底残余缝隙
 * （异步竞态、跨页交接、外部写入 select）。跨身份就地给一句说明并保留上一
 * 有效读数；判据缺失（目录未就绪）时放行给后端闸门——400 是正确的 fail-closed，
 * 会走失败态展示，不静默、不伪装成功。返回空串表示可发请求。 */
function crossDatasetBlock(a, b) {
  const sa = findSnapshot(a), sb = findSnapshot(b);
  if (!sa || !sb || datasetKey(sa) === datasetKey(sb)) return "";
  const keep = lastDiff
    ? "已保留上一次有效读数；"
    : "尚无可保留的读数，已清空结果区；";
  return `快照 #${a} 与 #${b} 不属于同一数据集（测量根或计量阈值口径不同），`
    + `无法构成有效对比区间。${keep}请在基线/对比下拉中改选同一数据集的快照。`;
}

async function loadDiff({ retryOnMissing = true, successMessage = "" } = {}) {
  const a = document.getElementById("sel-a").value;
  const b = document.getElementById("sel-b").value;
  /* ISS-170 R2：意图票据。只有 onSelectionChange / 目录收敛落定这两个合法
   * 发起方设置的最新意图才放行；实测（CI 冷环境 + 本地网络监听）存在迟到的
   * 恢复/交接流程以旧 sel 值补发 diff（三连发 (1,4)/(1,2)/(1,4)），其新世代
   * 会作废用户自己的请求，渲染复核门又拦掉它——净变化行无人更新。票据不符
   * 的调用在门口直接拒绝，不发起请求。 */
  if (diffIntentAB !== `${a}\u0000${b}`) return;
  const request = beginRequest("diff");
  if (!a || !b) return;
  const blocked = crossDatasetBlock(a, b);
  if (blocked) {
    // 有上一有效读数就原样留着（不改成 0、不清空），只把状态行说清楚
    if (!lastDiff) clearDiffResults();
    setDiffStatus(blocked);
    return;
  }
  setDiffStatus("正在加载快照对比…");
  try {
    const d = await fetchJSON(`/api/diff?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
    if (!request.current()) return;
    /* ISS-170 R2：世代守卫之外再复核「最后一次用户意图」。响应到达时若两侧
     * select 已被改选（任何未走 beginRequest("diff") 的写入路径，例如外部脚本
     * 或跨页交接直接改选项），本次读数一律丢弃——用户改选后，任何迟到响应
     * 都不得覆盖他刚做的选择。渲染 #changes-net 的唯一写路径，两道门缺一不可。*/
    if (document.getElementById("sel-a").value !== a ||
        document.getElementById("sel-b").value !== b) return;
    lastDiff = d;
    renderDeltaBars("chart-grown", d.grown, cssVar("--grow"));
    renderDeltaBars("chart-shrunk", d.shrunk, cssVar("--mineral"));
    fillDeltaTable("tbl-grown-top", d.grown);
    fillDeltaTable("tbl-shrunk-top", d.shrunk);
    fillTwoColTable("tbl-added", d.added, (r) => [r.path, fmtKB(r.new_kb)]);
    fillTwoColTable("tbl-removed", d.removed, (r) => [r.path, fmtKB(r.old_kb)]);
    renderNetLine(d);
    // ISS-148：树表默认聚焦数据集根，同级行来自 /api/diff/children。
    // 根层就绪后才置完成态——状态文案出现即代表行可见（回归合同）；
    // 根层失败不拦基础完成态：树区内部展示错误与重试。
    resetTree(d);
    await loadTreeLevel(treeState.focus);
    if (!request.current()) return;
    // ISS-107：渲染已完成，状态必须是完成态——不得滞留「正在对比」。
    setDiffStatus(successMessage || `已对比快照 #${a} → #${b}`);
    // ISS-035C：基础对比就绪后加载解读区（跟随当前 a/b；可独立失败）。
    loadAnalysisPanel();
  } catch (e) {
    if (!request.current()) return;
    if (e.status === 404 && retryOnMissing) {
      clearDiffResults();
      await loadSnapshotsForDiff({ notice: "所选快照已更新或不再可用，已切换到最近有效快照。" });
      return;
    }
    clearDiffResults();
    if (e.status === 409) {
      // 状态性约束（缺两个不同日期的有效快照）：重试不改变前提，保持纯文案。
      setDiffStatus("还不能比较：需要两个不同日期的有效快照。");
    } else if (e.status === 0) {
      showDiffFailure("无法连接本地服务，快照对比暂不可用。");
    } else {
      showDiffFailure(`快照对比加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`);
    }
  }
}

function renderDeltaBars(id, rows, color) {
  const chart = initChart(id);
  const top = rows.slice(0, 12).reverse();
  chart.setOption({
    tooltip: { trigger: "item", formatter: (p) => {
      const r = p.data.raw;
      return `${escapeHtml(r.path)}<br/>${fmtKB(r.old_kb)} → ${fmtKB(r.new_kb)}<br/>变化 ${fmtDelta(r.delta_kb)}`;
    } },
    grid: { left: 150, right: 50, top: 6, bottom: 24 },
    xAxis: { type: "value", axisLabel: { formatter: (v) => fmtDelta(v) } },
    yAxis: { type: "category", data: top.map((r) => shortPath(r.path, 2)),
      axisLabel: { fontSize: 11, width: 140, overflow: "truncate" } },
    series: [{ type: "bar", data: top.map((r) => ({ value: r.delta_kb, raw: r })),
      itemStyle: { color, borderRadius: [0, 3, 3, 0] },
      label: { show: true, position: "right", fontSize: 11, formatter: (p) => fmtDelta(p.value) } }],
  }, true);
}

function fillTwoColTable(id, rows, cols) {
  const tbody = document.querySelector(`#${id} tbody`);
  tbody.innerHTML = "";
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="3" style="color:var(--muted)">无</td></tr>';
    return;
  }
  rows.forEach((r) => {
    const tr = document.createElement("tr");
    const [c0, c1] = cols(r);
    tr.innerHTML = `<td class="path" title="${escapeHtml(r.path)}">${escapeHtml(c0)}</td>` +
      `<td class="num">${c1}</td>` +
      `<td><button class="btn-mini" data-reveal="${escapeHtml(r.path)}" title="在 Finder 中显示" aria-label="在 Finder 中显示">${icon("folderOpen", 14)}</button></td>`;
    tbody.appendChild(tr);
    tr.querySelector("[data-reveal]").addEventListener("click", () => revealInFinder(r.path));
  });
}

/* ISS-107：增长/缩减图表的等价可读数据表（Top 12，与图同源）。
 * 图 hover 才能读数、canvas 文本不可选中——表格让读数不依赖指针悬停，
 * 且每行带 Finder 入口（与 added/removed 表同一目录证据语义）。 */
function fillDeltaTable(id, rows) {
  const tbody = document.querySelector(`#${id} tbody`);
  tbody.innerHTML = "";
  const top = (rows || []).slice(0, 12);
  if (!top.length) {
    tbody.innerHTML = '<tr><td colspan="5" style="color:var(--muted)">无</td></tr>';
    return;
  }
  top.forEach((r) => {
    const tr = document.createElement("tr");
    const deltaCls = r.delta_kb > 0 ? "delta-grow" : r.delta_kb < 0 ? "delta-shrink" : "";
    tr.innerHTML = `<td class="path" title="${escapeHtml(r.path)}">${escapeHtml(r.path)}</td>` +
      `<td class="num">${escapeHtml(fmtKB(r.old_kb))}</td>` +
      `<td class="num">${escapeHtml(fmtKB(r.new_kb))}</td>` +
      `<td class="num ${deltaCls}">${escapeHtml(fmtDelta(r.delta_kb))}</td>` +
      `<td><button class="btn-mini" data-reveal="${escapeHtml(r.path)}" title="在 Finder 中显示" aria-label="在 Finder 中显示">${icon("folderOpen", 14)}</button></td>`;
    tbody.appendChild(tr);
    tr.querySelector("[data-reveal]").addEventListener("click", () => revealInFinder(r.path));
  });
}

/* ---------- 目录详情（ISS-148 起为共用组件 directory-detail.js） ----------
 *
 * 组件持有 #changes-detail 侧栏的渲染与 trend/browse 装载；变化页只负责：
 * - 传当前 a/b（getRange）与状态徽章渲染；
 * - 注入「聚焦此目录」动作（面包屑下钻的次级入口）；
 * - 打开时标记触发行选中，Esc 关闭后焦点返回（DESIGN 键盘可达）。
 */

const directoryDetail = createDirectoryDetail({
  getRange: () => ({
    a: document.getElementById("sel-a")?.value || "",
    b: document.getElementById("sel-b")?.value || "",
  }),
  renderStatus: (st) => statusBadge(st),
  extraActions: [
    { id: "focus", label: "聚焦此目录", icon: "scope", onPick: (p) => focusTreePath(p) },
  ],
});

function openTreeDetail(path, sourceRow) {
  const row = findTreeRow(path);
  if (sourceRow) {
    document.querySelectorAll("#changes-body tr.focusable.selected").forEach((t) =>
      t.classList.remove("selected"));
    sourceRow.classList.add("selected");
  }
  directoryDetail.open({ path, row, sourceRow });
}

const analysisPanel = createAnalysisPanel({
  panelId: "analysis-panel", bodyId: "analysis-body",
  selection: () => ({a: document.getElementById("sel-a").value, b: document.getElementById("sel-b").value}),
  root: () => lastDiff?.a?.root,
  locate(target) {
    const search = document.getElementById("changes-search");
    if (search) search.value = "";
    changesTabs?.activate("detail");
    revealTreePath(target);
  },
});
const resetAnalysisSession = (options) => analysisPanel.reset(options);
const loadAnalysisPanel = () => analysisPanel.load();

/* ---------- 历史日报 ---------- */

async function loadReportList() {
  const request = beginRequest("reportList");
  invalidateRequest("reportContent");
  const el = document.getElementById("report-list");
  const view = document.getElementById("report-view");
  view.classList.add("hidden");
  view.replaceChildren();
  try {
    const r = await fetchJSON("/api/reports");
    if (!request.current()) return;
    if (!r.reports.length) {
      el.innerHTML = '<p class="hint">还没有日报——首次扫描后，从下一次扫描起每天自动生成。</p>';
      return;
    }
    el.innerHTML = r.reports.map((x) =>
      `<button class="report-item" data-date="${x.date}">${icon("fileText", 14)} ${x.date}</button>`).join("");
    el.querySelectorAll(".report-item").forEach((b) =>
      b.addEventListener("click", async () => {
        const contentRequest = beginRequest("reportContent");
        el.querySelectorAll(".report-item").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        try {
          const c = await fetchJSON(`/api/reports/${b.dataset.date}`);
          if (!contentRequest.current()) return;
          view.classList.remove("hidden");
          view.innerHTML = `<pre>${escapeHtml(c.content)}</pre>`;
        } catch (e) {
          if (!contentRequest.current()) return;
          view.classList.remove("hidden");
          view.innerHTML = `<p class="hint">${escapeHtml(e.status === 0
            ? "无法连接本地服务，日报暂不可用。"
            : `日报加载失败${e.status ? `（HTTP ${e.status}）` : ""}：${e.message}`)}</p>`;
        }
      }));
  } catch (e) {
    if (!request.current()) return;
    el.innerHTML = `<p class="hint">${escapeHtml(e.message)}</p>`;
  }
}

export const changesPage = {
  id: "changes",
  load() {
    // ISS-094：进入页面按 hash 恢复 tab（无段 = 默认比对明细）。
    changesTabs?.applyHash();
    // ISS-151：同会话返回且 a/b 未变时恢复树视图（不重发 diff/层级请求）
    loadSnapshotsForDiff({ restore: true });
    loadReportList();
  },
  init() {
    // ISS-094：页内二级导航（五分区 tab；index.html 静态 DOM）。
    changesTabs = initPageTabs({ page: "changes", defaultTab: "detail" });
    // ISS-093：选择即比对——select 改选（鼠标或键盘）在选齐后自动触发加载。
    // 非交接的改选推进用户意图，尚未落位的 CTA 等待器据此放弃写入。
    ["sel-a", "sel-b"].forEach((id) => {
      document.getElementById(id).addEventListener("change", (e) => {
        if (e.__fathomHandoff !== true) window.__changesUserTouched = true;
        /* ISS-178 R3 D2：入口交接的合成事件带标记，转交 onSelectionChange 走
         * 非用户路径（仍推意图世代，但不按 userChanged 处理）。 */
        onSelectionChange(e.target.id, { handoff: e.__fathomHandoff === true });
      });
    });
    // ISS-148：排序表头（delta/size/name 走接口 sort；同级内排序）
    document.querySelectorAll("#changes-table .th-sort").forEach((btn) => {
      btn.addEventListener("click", () => onTreeSortChange(btn.dataset.sort));
    });
    // ISS-148：同级筛选（all/changed 走接口 filter）与搜索（触达已加载子树）
    const filterSel = document.getElementById("changes-filter");
    if (filterSel) filterSel.addEventListener("change", onTreeFilterChange);
    const search = document.getElementById("changes-search");
    if (search) search.addEventListener("input", () => renderTree());
    // ISS-148：树表事件统一委托（行展开/详情/聚焦/Finder/复制/重试/续页）
    const tbody = document.getElementById("changes-body");
    tbody.addEventListener("click", (e) => {
      const retry = e.target.closest("[data-tree-retry]");
      if (retry) {
        const path = retry.dataset.treeRetry;
        treeState?.levels.delete(path);
        loadTreeLevel(path);
        return;
      }
      const more = e.target.closest("[data-load-more]");
      if (more) {
        loadTreeLevel(more.dataset.loadMore, { cursor: more.dataset.cursor, append: true });
        return;
      }
      const expand = e.target.closest("[data-expand]");
      if (expand) {
        e.stopPropagation();
        toggleTreeExpand(expand.dataset.expand);
        return;
      }
      const focus = e.target.closest("[data-focus]");
      if (focus) {
        e.stopPropagation();
        focusTreePath(focus.dataset.focus);
        return;
      }
      const reveal = e.target.closest("[data-reveal]");
      if (reveal) {
        e.stopPropagation();
        revealInFinder(reveal.dataset.reveal);
        return;
      }
      const copy = e.target.closest("[data-copy]");
      if (copy) {
        e.stopPropagation();
        copyTreePath(copy);
        return;
      }
      const detail = e.target.closest("[data-detail]");
      if (detail) {
        e.stopPropagation();
        openTreeDetail(detail.dataset.detail, detail.closest("tr"));
        return;
      }
      const tr = e.target.closest("tr.focusable");
      if (tr) openTreeDetail(tr.dataset.path, tr);
    });
    // 键盘：行 Enter/Space 打开详情；展开/聚焦等按钮自身 Enter 原生激活。
    tbody.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      if (e.target.closest("button")) return;
      const tr = e.target.closest("tr.focusable");
      if (tr) {
        e.preventDefault();
        openTreeDetail(tr.dataset.path, tr);
      }
    });
    // 全局 Esc：先关详情；无详情时逐级返回上级聚焦（面包屑逆向）
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (directoryDetail.isOpen) {
        e.preventDefault();
        directoryDetail.close();
        return;
      }
      if (treeState && treeState.focus !== treeState.root) {
        e.preventDefault();
        const parent = parentWithinRoot(treeState.focus, treeState.root);
        if (parent) focusTreePath(parent);
      }
    });
  },
  leave() {
    // 旧页等待中的总览交接不能在快速重入后继续写选。
    state.pendingChangeEntry = null;
    ["snapshots", "diff", "reportList", "reportContent",
     "detailTrend", "detailBrowse", "analysis", "analysisPoll"].forEach(invalidateRequest);
    // ISS-035C：离页停轮询并清内存预览态；不取消已授权任务（后台继续），
    // 也不清 sessionStorage（回来按原 job 恢复显示，不隐式重发）。
    analysisPanel.leave();
    // ISS-151：快照树视图（实例含 levels/expanded/focus）供同会话返回恢复；
    // 在途层级请求靠下方 treeState 置空 + epoch 作废，不重发。
    const container = document.querySelector(".page-container");
    const activeRow = document.activeElement && document.activeElement.closest
      ? document.activeElement.closest("#changes-body tr.focusable") : null;
    sessionTree = treeState ? {
      state: treeState,
      a: treeState.a,
      b: treeState.b,
      scrollTop: container ? container.scrollTop : 0,
      focusPath: activeRow ? activeRow.dataset.path : null,
      detailPath: directoryDetail.isOpen ? directoryDetail.activePath : null,
    } : null;
    // ISS-148：离页关闭详情并作废全部在途树层级请求
    directoryDetail.close({ restoreFocus: false });
    treeEpoch += 1;
    treeState = null;
  },
};
