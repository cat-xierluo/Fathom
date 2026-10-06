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
 * - 身份口径复用后端 reports.same_dataset 的 legacy 档三元组
 *   (root, min_kb, exclude_names)，逐字段同源、不新造判定（/api/snapshots
 *   不下发 plan_id，故前端只能取该档；带新身份的行由后端闸门继续把关）；
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
import { fetchJSON, beginRequest, invalidateRequest, revealInFinder, apiPost, apiSend } from "../request.js";
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

function setDiffControlsEnabled(enabled) {
  document.getElementById("sel-a").disabled = !enabled;
  document.getElementById("sel-b").disabled = !enabled;
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

/* ISS-170：数据集身份口径——与后端 fathom/reports.py::same_dataset 的 legacy
 * 档逐字段同源：(root, min_kb, exclude_names) 三元组相等即可比。
 * 归一化照抄后端语义：min_kb 为 NULL 是「该根的口径未知」数据集，彼此可比较；
 * exclude_names 缺列（v5 之前）兜底为 ""，与新写入的「无配置」同身份。
 * /api/snapshots 不下发 plan_id，故前端只能取该档——带新身份（plan_id 非空）
 * 的行是否可比的判断仍由后端闸门负责，本卡不放宽后端。 */
function datasetKey(snap) {
  const root = snap.root == null ? "" : String(snap.root);
  const minKb = snap.min_kb == null ? "" : String(snap.min_kb);
  const exclude = snap.exclude_names == null ? "" : String(snap.exclude_names);
  return JSON.stringify([root, minKb, exclude]);
}

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
  if (!request.current()) return;

  // ISS-170：缓存原始列表（收敛与守卫共用），再按当前生效数据集收敛两侧选项
  snapshotCatalog = snaps;
  /* ISS-170 R2：fetch /api/snapshots 期间用户已改选（revision 推进）→ 本二发
   * 整体放弃：不收敛、不落定 sel、不发 diff，只重建 b 侧全列数据。此前仅用
   * revision 切换「取发起时值还是实时值」，仍会按旧意图落定并以最新世代补发
   * diff，作废用户自己的请求（CI 三连发 (1,4)/(1,2)/(1,4) 的最后一发即此路
   * 径）。收敛落定与 loadDiff 之间是同步代码，放弃判断放在 fetch 之后即无
   * 竞争窗口。 */
  if (snapshotSelectionRevision !== selectionRevision) {
    replaceSnapshotOptions(selB, snaps);
    if (!selA.options.length) replaceSnapshotOptions(selA, snaps);
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
    return;
  }

  if (group.length < 2) {
    invalidateRequest("diff");
    clearDiffResults();
    setDiffControlsEnabled(false);
    // 整个库只有一个快照：沿用既有单快照文案（其他套件按此断言）。
    // 库里有多个快照、但当前生效数据集内不足两个：另给数据集口径的说明，
    // 不得让用户以为「再扫一次就能比较」（同库内已有别的数据集快照）。
    if (group.length === 1 && snaps.length > 1) {
      setDiffStatus("当前生效数据集只有一个快照；需要同一数据集内另一个不同日期的有效快照才能比较，其他数据集的快照不与它构成区间。");
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
function onSelectionChange(changedId) {
  snapshotSelectionRevision += 1;
  sessionTree = null;  // 用户改选：跨页保留的旧树视图作废
  const selA = document.getElementById("sel-a");
  const selB = document.getElementById("sel-b");
  // ISS-170：不对称收敛——锚点恒为 b。改 b 即切换数据集（b 侧全列），a 侧
  // 随之重填为 b 数据集并按「a 取其前驱」落定；改 a 只在 b 数据集内收敛，
  // b 侧选项与取值不动。目录未就绪时不动选项（保留既有选项，交给 loadDiff
  // 的守卫与后端闸门处理）。
  if (snapshotCatalog.length) {
    applyConvergedOptions(selA, selB, snapshotCatalog,
      { keepA: selA.value, keepB: selB.value, userChanged: changedId });
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

/* ---------- AI 解读（ISS-035C）：净变化之后、变化表之前的七主态区 ----------
 *
 * 方案 §7 合同：
 * - 七主态：未启用 / 未分析 / 发送预览 / 运行中 / 完成 / 失败 / 过期；
 *   取消/超时/中断以明确原因和手动重试动作呈现；
 * - 预览展示区间、范围与截断（manifest）、发送对象、完整发送文本入口
 *   （prompt_text 展开）、本地保存说明；确认仅一次（幂等键会话生成）；
 * - 世代守卫：解读区独立请求域 + 终态前比对 job 的 a/b 与当前选择——
 *   改选快照后迟到的旧响应不得覆盖新区间的视图（反例 2）；
 * - 离页不隐式重发：离开页面只作废请求域与轮询，不取消已授权任务；
 *   重入按 sessionStorage 记录的原 job 恢复显示（GET jobs/{id} 纯读），
 *   刷新页面不会重复发送（POST jobs 计数不变，反例 4）；
 * - evidence_id 跳转到该报告保存的事实条目；能对位时在变化表内定位，
 *   脱敏路径无法对位时如实说明，不猜（@user 段）；
 * - AI 不可用不影响基础事实：本区任何失败都只发生在本区内部。
 */

const ANALYSIS_PANEL_ID = "analysis-panel";
const ANALYSIS_BODY_ID = "analysis-body";
const ANALYSIS_POLL_MS = 2000;

/* 会话状态：改选区间即重置（preview/job 与旧区间的关联一并作废）。 */
let analysisCfg = null;        // GET /api/config 的 analysis 块
let analysisPreview = null;    // 当前预览（previews 响应体）
let analysisJob = null;        // 最近 job 视图（运行中/终态）
let analysisJobTimer = null;   // 轮询句柄

/* job 失败 reason_code 的可读文案；未登记值原样展示不吞字。 */
const ANALYSIS_JOB_REASON = {
  runner_timed_out: "分析超时：超过单次时限已被终止，可重试",
  runner_cancelled: "分析已取消（已发送部分无法撤回）",
  runner_nonzero_exit: "引擎命令行异常退出",
  runner_spawn_failed: "引擎无法启动（可能已被移动或损坏）",
  runner_output_limit: "引擎输出超出限额，结果不可信",
  runner_decode_error: "引擎输出无法按 UTF-8 解码",
  parse_failed_bad_json: "引擎返回内容不是有效 JSON",
  parse_failed_not_result: "引擎返回缺少 result 结构",
  parse_failed_missing_field: "引擎返回缺少必需字段",
  app_error: "引擎报告了应用层错误",
  cancelled_before_start: "分析在开始前被取消",
  owner_exit: "服务中断：本次分析未完成，不会自动重试",
  startup_aborted: "分析在启动阶段中断，未产生任何解读",
};

/* 事实条目 kind 的中文说明（与 facts 包 entry_legend 同口径）。 */
const ANALYSIS_ENTRY_KIND = {
  measured: "已测量（两次都被记录，增减为实测）",
  first_recorded: "首次进入统计（刚越过入库阈值；不是文件系统新建）",
  unrecorded: "未记录（可能跌破阈值、权限受限或已被移除；不是删除证据）",
};

function analysisPanelEl() { return document.getElementById(ANALYSIS_PANEL_ID); }
function analysisBodyEl() { return document.getElementById(ANALYSIS_BODY_ID); }

function analysisSel() {
  return {
    a: document.getElementById("sel-a").value,
    b: document.getElementById("sel-b").value,
  };
}

function sameAnalysisRange(obj, sel) {
  return obj && String(obj.a_snapshot_id ?? obj.a) === String(sel.a) &&
    String(obj.b_snapshot_id ?? obj.b) === String(sel.b);
}

/** 读回本区间当前尝试的幂等记录（含是否已终态）。 */
function analysisIdemRecord(sel) {
  try {
    const raw = sessionStorage.getItem(`fathom-aidem:${sel.a}->${sel.b}`);
    if (!raw) return null;
    const saved = JSON.parse(raw);
    return saved && typeof saved.key === "string" ? saved : null;
  } catch (_) { return null; }  /* 隐私模式等场景不可用 */
}

/** 记下本次尝试的 job，供刷新/离页后按 job_id 纯读重入。 */
function saveAnalysisAttempt(sel, { key, digest, job, replayed }) {
  try {
    sessionStorage.setItem(`fathom-aidem:${sel.a}->${sel.b}`, JSON.stringify({
      key, digest,
      job_id: job?.job_id ?? null,
      status: job?.status ?? null,
      terminal: Boolean(job?.terminal),
    }));
  } catch (_) { /* 隐私模式等场景：仅当前会话内可恢复 */ }
}

/** 会话幂等键（确认仅一次的载体）：按区间存 sessionStorage；
 * 绑定 request_digest——digest 变了（新预览）就换新键，避免
 * idempotency_conflict。
 *
 * 键的生命周期绑定「一次用户授权的尝试」，而不是区间（ISS-128）：
 *  - 同 digest 且上次尝试**仍在途** → 复用该键。这是传输层重试与双击，
 *    它们属于同一次尝试，必须收敛到同一份派发。
 *  - 同 digest 但上次尝试**已终态** → 新的用户尝试（重跑、取消后重试、
 *    撤销后再次分析、换引擎跑同一事实）发新键。否则后端只会重放旧终态，
 *    用户点了「重新分析」却看到上一次的旧结果。
 * 刷新重入不走这里：它用 analysisSavedJobId 做纯 GET，不会重复派发。 */
function analysisIdemKey(sel, digest, { create } = {}) {
  const saved = analysisIdemRecord(sel);
  if (saved && saved.digest === digest && !saved.terminal) return saved.key;
  if (!create) return null;
  const key = (crypto.randomUUID ? crypto.randomUUID() : `idem-${Date.now()}-${Math.random()}`);
  saveAnalysisAttempt(sel, { key, digest, job: null, replayed: false });
  return key;
}

/** 改选区间/清空结果时重置会话态；hide=true 同时收起解读区。 */
function resetAnalysisSession({ hide = false } = {}) {
  clearTimeout(analysisJobTimer);
  analysisJobTimer = null;
  analysisPreview = null;
  analysisJob = null;
  if (hide) {
    const panel = analysisPanelEl();
    if (panel) panel.hidden = true;
  }
}

/** 解读区加载失败/离线的就地重试（基础事实不受影响）。 */
function showAnalysisRetry(message, testId) {
  const el = analysisBodyEl();
  el.replaceChildren(document.createTextNode(message));
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "diff-retry";
  retry.textContent = "重试";
  retry.setAttribute("aria-label", "重新加载 AI 解读状态");
  if (testId) retry.setAttribute("data-test", testId);
  retry.addEventListener("click", () => loadAnalysisPanel());
  el.appendChild(retry);
}

/* ----- 渲染入口：state = {state: 七态之一, ...载荷} ----- */

function renderAnalysis(state) {
  const body = analysisBodyEl();
  const panel = analysisPanelEl();
  if (!body || !panel) return;
  panel.hidden = false;
  switch (state.state) {
    case "disabled": {
      body.innerHTML = `
        <p class="hint" data-test="analysis-state-disabled">AI 解读未启用：下方变化表等基础事实照常可用，不会发送任何数据。</p>
        <div class="perm-link-row">
          <button type="button" class="btn" data-test="analysis-goto-settings">${icon("settings", 14)} 前往设置开启</button>
        </div>`;
      body.querySelector("[data-test='analysis-goto-settings']").addEventListener("click", () => {
        window.__fathomSettingsSectionPending = "analysis";
        location.hash = "#/settings";
      });
      return;
    }
    case "idle": {
      const sel = analysisSel();
      body.innerHTML = `
        <p class="hint" data-test="analysis-state-idle">区间 #${escapeHtml(sel.a)} → #${escapeHtml(sel.b)} 还没有 AI 解读。
          生成发送预览后，你可以先查看将要发送的完整内容再确认；不确认就不会发送。</p>
        <div class="perm-link-row">
          <button type="button" class="btn" data-test="analysis-preview-btn">${icon("sparkles", 14)} 生成发送预览</button>
        </div>
        <p class="hint">生成预览只在本机整理事实包（零外传）；确认后才交给所选引擎分析。</p>`;
      body.querySelector("[data-test='analysis-preview-btn']").addEventListener("click", startAnalysisPreview);
      return;
    }
    case "preview": return renderAnalysisPreview(state);
    case "running": {
      const job = state.job || {};
      const cancelling = state.cancelling;
      body.innerHTML = `
        <p class="hint" data-test="analysis-state-running">
          <span class="dr-loading" data-dr-spin aria-hidden="true"></span>
          正在分析（区间 #${escapeHtml(String(job.a_snapshot_id))} → #${escapeHtml(String(job.b_snapshot_id))}，任务 ${escapeHtml(String(job.job_id || "").slice(0, 8))}）…
        </p>
        <p class="hint">关闭页面不会取消分析；回到本页会自动恢复显示。取消只能停止后续处理，不能撤回已发送给引擎的数据。</p>
        <div class="perm-link-row">
          <button type="button" class="btn" data-test="analysis-cancel-btn"${cancelling ? " disabled" : ""}>${cancelling ? "正在取消…" : "取消分析"}</button>
        </div>`;
      body.querySelector("[data-test='analysis-cancel-btn']").addEventListener("click", cancelAnalysisJob);
      return;
    }
    case "failure": return renderAnalysisFailure(state);
    case "done":
    case "expired": return renderAnalysisResult(state);
    case "pollError": {
      body.innerHTML = `
        <p class="hint" data-test="analysis-poll-error">分析状态查询失败：${escapeHtml(state.error?.message || "")}；任务仍在后台，稍后自动恢复或手动重试。</p>
        <div class="perm-link-row">
          <button type="button" class="btn" data-test="analysis-poll-retry">重试查询</button>
        </div>`;
      body.querySelector("[data-test='analysis-poll-retry']").addEventListener("click", () => {
        if (analysisJob?.job_id) scheduleAnalysisPoll(analysisJob.job_id);
        else loadAnalysisPanel();
      });
      return;
    }
    default:
      body.innerHTML = `<p class="hint">解读状态未知，请刷新。</p>`;
  }
}

/* ----- 未启用 / 未分析 ----- */

/* ----- 发送预览（方案 §4.2：预览=将发送的不可变请求） ----- */

function renderAnalysisPreview({ preview }) {
  const body = analysisBodyEl();
  const m = preview.manifest || {};
  const sel = m.snapshots || {};
  const sampling = m.sampling || {};
  const truncation = m.truncation || {};
  const runtime = preview.runtime || {};
  const aDate = String(sel.a?.created_at || "").slice(0, 16).replace("T", " ");
  const bDate = String(sel.b?.created_at || "").slice(0, 16).replace("T", " ");
  const dataset = m.dataset || {};
  const omits = [];
  if (Number(sampling.omitted) > 0) {
    omits.push(`候选 ${sampling.total_candidates} 条，取样 ${sampling.selected} 条，另有 ${sampling.omitted} 条未进入本包`);
  }
  if (truncation.utf8_truncated) {
    omits.push(`发送文本超出字节上限，已截断 ${truncation.omitted_entries || "?"} 条`);
  }
  body.innerHTML = `
    <div data-test="analysis-state-preview">
      <p class="hint" data-test="analysis-preview-range">将分析区间：<strong>#${escapeHtml(String(sel.a?.snapshot_id))}（${escapeHtml(aDate)}）→ #${escapeHtml(String(sel.b?.snapshot_id))}（${escapeHtml(bDate)}）</strong></p>
      <p class="hint" data-test="analysis-preview-scope">范围：根 <code>${escapeHtml(dataset.root_display || "?")}</code> ·
        入库阈值 ${dataset.min_kb == null ? "未知" : escapeHtml(String(dataset.min_kb)) + " KB"} ·
        排除掩码 ${(dataset.exclude_names || []).length ? escapeHtml((dataset.exclude_names || []).join("; ")) : "无"} ·
        单位 ${escapeHtml(m.units || "KiB")}（子目录行与根累计有重叠，行值不可相加）</p>
      <p class="hint" data-test="analysis-preview-truncation">${omits.length
        ? `截断与取样：${escapeHtml(omits.join("；"))}。模型只看到取样子集。`
        : "全部过阈值候选已进入本包，无取样省略。"}</p>
      <p class="hint" data-test="analysis-preview-target">发送对象：本机命令行
        <strong>${escapeHtml(runtime.display_name || runtime.id || "?")}</strong>${runtime.version ? `（版本 ${escapeHtml(runtime.version)}）` : ""}——
        它可能把数据发送给它配置的模型服务；账号与额度由该命令行管理。</p>
      <p class="hint" data-test="analysis-preview-retain">解读结果与本次批准发送的事实包会保存在本机运行目录，可随时撤销；
        撤销不能清除该命令行或模型服务自身的留存。</p>
      <details class="cfg-details" data-test="analysis-preview-prompt-details">
        <summary>查看完整发送文本</summary>
        <pre class="analysis-prompt" data-test="analysis-prompt-text">${escapeHtml(preview.prompt_text || "")}</pre>
      </details>
      <div class="exclude-actions">
        <button type="button" class="btn primary" data-test="analysis-confirm-btn">确认并发送分析</button>
        <button type="button" class="btn" data-test="analysis-preview-cancel">取消（不发送）</button>
      </div>
      <p class="hint">预览 ${escapeHtml(String(preview.expires_in_s ?? "?"))} 秒后过期；确认只发送这一次，不会自动重发。</p>
    </div>`;
  body.querySelector("[data-test='analysis-confirm-btn']").addEventListener("click", () => confirmAnalysisSend(preview));
  body.querySelector("[data-test='analysis-preview-cancel']").addEventListener("click", () => {
    analysisPreview = null;
    renderAnalysis({ state: "idle" });
  });
}

/** 确认发送（仅一次）：按钮点击即禁用；幂等键按会话/区间取或建；
 * 同键同 digest 重入返回同一 job（后端幂等），不会重复发送。 */
async function confirmAnalysisSend(preview) {
  const body = analysisBodyEl();
  const btn = body.querySelector("[data-test='analysis-confirm-btn']");
  if (btn) { btn.disabled = true; btn.textContent = "已确认，正在提交…"; }
  const sel = analysisSel();
  if (!sameAnalysisRange({ a: preview.a?.snapshot_id, b: preview.b?.snapshot_id }, sel)) {
    // 用户在预览展示期间改选了区间：旧预览不再代表当前视图，直接作废
    resetAnalysisSession();
    loadAnalysisPanel();
    return;
  }
  const digest = preview.request_digest;
  const key = analysisIdemKey(sel, digest, { create: true });
  const request = beginRequest("analysis");
  try {
    const r = await apiPost("/api/analysis/jobs", {
      preview_id: preview.preview_id,
      request_digest: digest,
      idempotency_key: key,
    });
    if (!request.current()) return;
    const data = await r.json();
    // 记录原 job 供刷新/离页后重入恢复（GET jobs/{id} 纯读）。
    saveAnalysisAttempt(sel, { key, digest, job: data.job, replayed: data.replayed });
    analysisPreview = null;
    analysisJob = data.job;
    // 重放（replayed=true）不等于「正在运行」（ISS-128）：后端可能返回的是
    // 一个**已终态**的旧 job——此前这里无条件渲染 running 且不轮询，页面会
    // 永远停在运行中。现在按 job 自身的终态标记分流。
    if (data.job?.terminal) {
      await onAnalysisJobTerminal(data.job);
      return;
    }
    renderAnalysis({ state: "running", job: data.job });
    // 重放在途说明别处已有一次同键派发（典型是本会话较早的尝试）：继续轮询
    // 把它收敛到终态，而不是当作已经结束。
    scheduleAnalysisPoll(data.job.job_id);
  } catch (e) {
    if (!request.current()) return;
    handleAnalysisSendError(e);
  }
}

function handleAnalysisSendError(e) {
  const code = e.reason_code;
  if (code === "preview_expired" || code === "preview_not_found" || code === "preview_stale") {
    analysisPreview = null;
    renderAnalysis({ state: "idle" });
    const body = analysisBodyEl();
    const note = document.createElement("p");
    note.className = "hint cfg-error";
    note.setAttribute("data-test", "analysis-preview-stale");
    note.textContent = code === "preview_stale"
      ? "设置或数据已变化，原预览失效；请重新生成预览。"
      : "预览已过期（有效期 5 分钟）；请重新生成预览。";
    body.prepend(note);
    return;
  }
  if (code === "analysis_busy") {
    renderAnalysis({ state: "idle" });
    const body = analysisBodyEl();
    const note = document.createElement("p");
    note.className = "hint cfg-error";
    note.setAttribute("data-test", "analysis-busy");
    note.textContent = "已有分析在进行中（同一时间只允许一个）；等待其结束后再试，不会排队。";
    body.prepend(note);
    return;
  }
  if (code === "analysis_disabled") {
    renderAnalysis({ state: "disabled" });
    return;
  }
  // 其余（403 引擎不可用 / 404 快照失效 / 400 口径）：可读原因 + 回未分析
  const sel = analysisSel();
  renderAnalysis({ state: "idle" });
  const body = analysisBodyEl();
  const note = document.createElement("p");
  note.className = "hint cfg-error";
  note.setAttribute("data-test", "analysis-send-error");
  note.textContent = `无法开始分析：${e.message}`;
  body.prepend(note);
}

/* ----- 运行中：轮询（离页作废；重入恢复） ----- */

function scheduleAnalysisPoll(jobId) {
  clearTimeout(analysisJobTimer);
  analysisJobTimer = setTimeout(async () => {
    const request = beginRequest("analysisPoll");
    try {
      const r = await fetchJSON(`/api/analysis/jobs/${encodeURIComponent(jobId)}`);
      if (!request.current()) return;
      const job = r.job;
      // 区间世代守卫：job 不属于当前选择的区间时不写入（迟到响应）
      if (!sameAnalysisRange(job, analysisSel())) return;
      analysisJob = job;
      if (job.terminal) {
        await onAnalysisJobTerminal(job);
        return;
      }
      renderAnalysis({ state: "running", job, cancelling: job.status === "cancelling" });
      scheduleAnalysisPoll(jobId);
    } catch (e) {
      if (!request.current()) return;
      renderAnalysis({ state: "pollError", error: e });
    }
  }, ANALYSIS_POLL_MS);
}

async function onAnalysisJobTerminal(job) {
  // 无论哪种终态，都把本次尝试标记为「已结束」：下一次用户发起的尝试会因此
  // 拿到新键，不会重放这一次的旧终态（ISS-128）。此前只在部分状态下清键，
  // 且恰好漏掉 succeeded——成功保留旧键，用户点「重新分析」只会拿到上一次的
  // 结果。job_id 仍保留，刷新重入（纯 GET）不受影响。
  markAnalysisAttemptTerminal(job);
  if (job.status === "succeeded") {
    // 终态成功：以 analyses 读取层为准（含过期评估），不自行渲染 stdout
    await loadAnalysisPanel();
    return;
  }
  renderAnalysis({ state: "failure", job });
}

/** 把指定区间（默认当前区间）的尝试标记为已终态；保留 job_id 供重入。 */
function markAnalysisAttemptTerminal(job, sel = analysisSel()) {
  const saved = analysisIdemRecord(sel);
  if (!saved) return;
  try {
    sessionStorage.setItem(`fathom-aidem:${sel.a}->${sel.b}`, JSON.stringify({
      ...saved,
      job_id: job?.job_id ?? saved.job_id ?? null,
      status: job?.status ?? null,
      terminal: true,
    }));
  } catch (_) { /* 隐私模式等场景：仅当前会话内可恢复 */ }
}

async function cancelAnalysisJob() {
  const job = analysisJob;
  if (!job?.job_id) return;
  renderAnalysis({ state: "running", job, cancelling: true });
  const request = beginRequest("analysis");
  try {
    await apiPost(`/api/analysis/jobs/${encodeURIComponent(job.job_id)}/cancel`, {});
    if (!request.current()) return;
    // 取消受理后由轮询收敛终态（cancelling → cancelled）
    scheduleAnalysisPoll(job.job_id);
  } catch (e) {
    if (!request.current()) return;
    if (e.status === 409) {
      // 终态竞争：任务已结束，取消不再生效——按最终状态呈现（响应已带 job）
      const finalJob = e.body?.job;
      if (finalJob && finalJob.terminal) {
        analysisJob = finalJob;
        await onAnalysisJobTerminal(finalJob);
        return;
      }
      scheduleAnalysisPoll(job.job_id);
      return;
    }
    renderAnalysis({ state: "running", job });
    const body = analysisBodyEl();
    const note = document.createElement("p");
    note.className = "hint cfg-error";
    note.textContent = `取消请求失败：${e.message}；可再次尝试。`;
    body.appendChild(note);
  }
}

/* ----- 失败 / 过期 / 完成 ----- */

function renderAnalysisFailure({ job }) {
  const body = analysisBodyEl();
  const sel = analysisSel();
  const status = job?.status || "failed";
  const code = job?.reason_code;
  const reason = ANALYSIS_JOB_REASON[code] || code || "未知原因";
  const rangeText = job && job.a_snapshot_id != null
    ? `#${escapeHtml(String(job.a_snapshot_id))} → #${escapeHtml(String(job.b_snapshot_id))}`
    : `#${escapeHtml(sel.a)} → #${escapeHtml(sel.b)}`;
  const headline = status === "cancelled" ? "分析已取消"
    : status === "timed_out" ? "分析超时"
      : status === "interrupted" ? "分析被中断"
        : status === "cancelling" ? "正在取消" : "分析失败";
  body.innerHTML = `
    <div data-test="analysis-state-failure">
      <p class="hint" data-test="analysis-failure-headline"><strong>${escapeHtml(headline)}</strong></p>
      <p class="hint" data-test="analysis-failure-reason">原因：${escapeHtml(reason)}${code ? `（${escapeHtml(code)}）` : ""}</p>
      <p class="hint">失败不会产生或改动任何解读记录；区间 ${rangeText} 的基础事实不受影响。</p>
      <div class="perm-link-row">
        <button type="button" class="btn" data-test="analysis-retry-btn">${icon("activity", 14)} 重新生成预览</button>
      </div>
    </div>`;
  body.querySelector("[data-test='analysis-retry-btn']").addEventListener("click", startAnalysisPreview);
}

/** 完成/过期共用的正文渲染（方案 §7：重点/推断/限制/查看方向 + 元信息）。
 * expired=true 时先给原因横幅，仍渲染已保存原文与证据。 */
function renderAnalysisResult({ state, record }) {
  const body = analysisBodyEl();
  const result = record.result || {};
  const runtime = record.runtime || {};
  const facts = record.facts || {};
  const aId = record.a?.snapshot_id, bId = record.b?.snapshot_id;
  const aDate = String(record.a?.created_at || "").slice(0, 16).replace("T", " ");
  const bDate = String(record.b?.created_at || "").slice(0, 16).replace("T", " ");
  const expiredReason = {
    snapshot_replaced: "原区间快照已被同日新快照替换",
    snapshot_pruned: "原区间快照已按保留策略淘汰",
    dataset_unverifiable: "数据集口径（根/阈值/排除）已变化，无法再验证原区间",
  }[record.expired_reason] || record.expired_reason || "原因未上报";
  const findings = Array.isArray(result.findings) ? result.findings : [];
  const limitations = Array.isArray(result.limitations) ? result.limitations : [];
  const inspectNext = Array.isArray(result.inspect_next) ? result.inspect_next : [];
  const evidenceBtn = (eid, label) => `
    <button type="button" class="btn-mini" data-test="analysis-evidence-btn" data-evidence-id="${escapeHtml(eid)}"
            aria-label="查看证据 ${escapeHtml(eid)}" title="查看该编号保存的事实">${escapeHtml(label || eid)}</button>`;
  body.innerHTML = `
    <div data-test="analysis-state-${state === "expired" ? "expired" : "done"}">
      ${state === "expired" ? `
      <p class="hint analysis-expired-note" data-test="analysis-expired-note">
        <strong>该解读已过期</strong>：${escapeHtml(expiredReason)}。
        以下仍可查看原日期（${escapeHtml(aDate)} → ${escapeHtml(bDate)}）保存的原文与证据，但不代表当前数据。</p>` : ""}
      <p class="hint" data-test="analysis-meta">
        ${escapeHtml(runtime.id || "引擎")}${runtime.version ? ` v${escapeHtml(runtime.version)}` : ""} ·
        模型：${escapeHtml(runtime.model || "未上报")} ·
        生成于 ${escapeHtml(String(record.created_at || "").slice(0, 16).replace("T", " "))} ·
        区间 #${escapeHtml(String(aId))} → #${escapeHtml(String(bId))} ·
        解读版本 ${escapeHtml(record.prompt_version || "?")}
      </p>
      <p class="analysis-summary" data-test="analysis-summary">${escapeHtml(result.summary || "（引擎未给出摘要）")}</p>
      ${findings.length ? `
      <h3 class="analysis-subhead">重点与推断</h3>
      <ul class="analysis-findings" data-test="analysis-findings">
        ${findings.map((f) => `
        <li class="analysis-finding">
          <span class="quality-chip ${f.certainty === "hypothesis" ? "warn" : "ok"}" data-test="analysis-certainty">${f.certainty === "hypothesis" ? "推断" : "已观察"}</span>
          <span class="analysis-finding-text">${escapeHtml(f.text || "")}</span>
          <span class="analysis-finding-evidence">${(f.evidence_ids || []).map((eid) => evidenceBtn(eid)).join(" ")}</span>
        </li>`).join("")}
      </ul>` : `<p class="hint" data-test="analysis-no-findings">本次解读没有给出有依据的重点（引擎应说明原因，见下方限制）。</p>`}
      ${limitations.length ? `
      <h3 class="analysis-subhead">限制</h3>
      <ul class="analysis-limitations" data-test="analysis-limitations">
        ${limitations.map((t) => `<li>${escapeHtml(String(t))}</li>`).join("")}
      </ul>` : ""}
      ${inspectNext.length ? `
      <h3 class="analysis-subhead">查看方向</h3>
      <ul class="analysis-inspect" data-test="analysis-inspect-next">
        ${inspectNext.map((it) => `
        <li>${evidenceBtn(it.evidence_id)} <span>${escapeHtml(it.reason || "")}</span></li>`).join("")}
      </ul>` : ""}
      <div class="perm-link-row" data-test="analysis-result-actions">
        <button type="button" class="btn" data-test="analysis-rerun-btn">${icon("activity", 14)} 重跑</button>
        ${state === "done" ? `<button type="button" class="btn" data-test="analysis-revoke-btn">${icon("trash", 14)} 撤销此解读</button>` : ""}
      </div>
      <div data-test="analysis-evidence-card-host"></div>
      ${state === "expired" ? `<div data-test="analysis-revoke-expired-host"></div>` : ""}
    </div>`;
  body.querySelectorAll("[data-test='analysis-evidence-btn']").forEach((btn) => {
    btn.addEventListener("click", () => showAnalysisEvidence(btn.dataset.evidenceId, record));
  });
  body.querySelector("[data-test='analysis-rerun-btn']").addEventListener("click", startAnalysisPreview);
  const revokeBtn = body.querySelector("[data-test='analysis-revoke-btn']");
  if (revokeBtn) revokeBtn.addEventListener("click", () => confirmAnalysisRevoke(record));
  if (state === "expired") {
    const host = body.querySelector("[data-test='analysis-revoke-expired-host']");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn";
    btn.setAttribute("data-test", "analysis-revoke-expired-btn");
    btn.innerHTML = `${icon("trash", 14)} 撤销此历史解读`;
    btn.addEventListener("click", () => confirmAnalysisRevoke(record));
    host?.appendChild(btn);
  }
}

/** 证据条目卡：显示该编号在已保存事实包中的完整事实；能对位时提供
 * 「在变化表中定位」。脱敏路径（@user 段）无法对位时如实说明。 */
function showAnalysisEvidence(evidenceId, record) {
  const host = analysisBodyEl().querySelector("[data-test='analysis-evidence-card-host']");
  if (!host) return;
  const entry = ((record.facts && record.facts.entries) || [])
    .find((x) => x.evidence_id === evidenceId);
  if (!entry) {
    host.innerHTML = `<p class="hint cfg-error" data-test="analysis-evidence-missing">证据 ${escapeHtml(evidenceId)} 不在已保存的事实包中（可能是引擎引用了无效编号，已被验证器拒绝的历史不会出现；如反复出现请重跑）。</p>`;
    return;
  }
  const kindText = ANALYSIS_ENTRY_KIND[entry.kind] || entry.kind;
  const deltaText = entry.delta_kb == null ? "—" : fmtDelta(entry.delta_kb);
  const oldText = entry.old_kb == null ? "—" : fmtKB(entry.old_kb);
  const newText = entry.new_kb == null ? "—" : fmtKB(entry.new_kb);
  const tablePath = analysisEvidenceTablePath(entry);
  host.innerHTML = `
    <div class="analysis-evidence-card" data-test="analysis-evidence-card">
      <div class="analysis-evidence-head">
        <strong>证据 ${escapeHtml(entry.evidence_id)}</strong>
        <span class="hint">${escapeHtml(kindText)}</span>
        <button type="button" class="btn-mini" data-test="analysis-evidence-close" aria-label="关闭证据卡">${icon("x", 12)}</button>
      </div>
      <p class="analysis-evidence-path" data-test="analysis-evidence-path">${escapeHtml(entry.path)}</p>
      <p class="hint">${entry.old_kb == null ? "" : `${escapeHtml(oldText)} → `}
        ${entry.new_kb == null ? "" : escapeHtml(newText)}
        ${entry.delta_kb == null ? "" : ` · 变化 ${escapeHtml(deltaText)}`}</p>
      ${tablePath
        ? `<div class="perm-link-row"><button type="button" class="btn" data-test="analysis-evidence-locate">在变化表中定位</button></div>`
        : `<p class="hint">该路径含脱敏标记或不在当前变化表中（可能未进取样集），不以表行反推事实。</p>`}
    </div>`;
  host.querySelector("[data-test='analysis-evidence-close']").addEventListener("click", () => { host.innerHTML = ""; });
  const locateBtn = host.querySelector("[data-test='analysis-evidence-locate']");
  if (locateBtn) locateBtn.addEventListener("click", () => locateEvidenceInTable(entry));
}

/** 把 facts 的脱敏路径换算成变化表的绝对路径；无法对位（@user 段、
 * 无根路径）返回 null——不猜。 */
function analysisEvidenceTablePath(entry) {
  const root = lastDiff?.a?.root;
  const p = String(entry.path || "");
  if (!root) return null;
  if (p === "@root") return root;
  if (p.startsWith("@root/")) {
    const rel = p.slice("@root/".length);
    if (rel.includes("@user") || rel.includes("@root")) return null;
    return root.replace(/\/+$/, "") + "/" + rel;
  }
  return null;
}

function locateEvidenceInTable(entry) {
  const target = analysisEvidenceTablePath(entry);
  if (!target) return;
  // 清搜索过滤，避免目标行被滤掉；切回比对明细 tab（跨分区数据一致）。
  // ISS-148 树表：定位按路径而非行索引；目标行不在当前聚焦层时由
  // revealTreePath 复位筛选/下钻父层后再定位，仍不可见则如实说明。
  const search = document.getElementById("changes-search");
  if (search && search.value) { search.value = ""; }
  changesTabs?.activate("detail");
  revealTreePath(target);
}

/** 撤销确认层：删除本应用保存的正文与事实包；不承诺清除引擎/服务留存。 */
function confirmAnalysisRevoke(record) {
  const host = analysisBodyEl().querySelector("[data-test='analysis-evidence-card-host']");
  if (!host) return;
  host.innerHTML = `
    <div class="analysis-evidence-card" data-test="analysis-revoke-confirm">
      <p class="hint exclude-warning">即将撤销此解读：删除本应用保存的解读正文与对应事实包。
        已发送给引擎的数据无法撤回；生命周期审计记录会保留撤销时间。</p>
      <div class="exclude-actions">
        <button type="button" class="btn primary" data-test="analysis-revoke-yes">确认撤销</button>
        <button type="button" class="btn" data-test="analysis-revoke-no">取消</button>
      </div>
    </div>`;
  host.querySelector("[data-test='analysis-revoke-no']").addEventListener("click", () => { host.innerHTML = ""; });
  host.querySelector("[data-test='analysis-revoke-yes']").addEventListener("click", async () => {
    const request = beginRequest("analysis");
    try {
      await apiSend("DELETE", `/api/analyses/${encodeURIComponent(record.id)}`);
      if (!request.current()) return;
      renderAnalysis({ state: "idle" });
    } catch (e) {
      if (!request.current()) return;
      host.innerHTML = `<p class="hint cfg-error" data-test="analysis-revoke-error">撤销失败：${escapeHtml(e.message)} 解读仍保留，可重试。</p>`;
    }
  });
}

/* ----- 预览生成与主状态机 ----- */

async function startAnalysisPreview() {
  const sel = analysisSel();
  if (!sel.a || !sel.b) return;
  const request = beginRequest("analysis");
  const body = analysisBodyEl();
  body.innerHTML = `<p class="hint" data-test="analysis-preview-loading"><span class="dr-loading" data-dr-spin aria-hidden="true"></span>正在生成发送预览（只在本机整理事实包）…</p>`;
  try {
    const r = await apiPost("/api/analysis/previews", { a: Number(sel.a), b: Number(sel.b) });
    if (!request.current()) return;
    const preview = await r.json();
    analysisPreview = preview;
    renderAnalysis({ state: "preview", preview });
  } catch (e) {
    if (!request.current()) return;
    const code = e.reason_code;
    if (code === "analysis_disabled") { renderAnalysis({ state: "disabled" }); return; }
    if (code && code.startsWith("runtime_")) {
      body.innerHTML = `
        <p class="hint cfg-error" data-test="analysis-runtime-blocked">无法生成预览：${escapeHtml(e.message)}</p>
        <div class="perm-link-row">
          <button type="button" class="btn" data-test="analysis-goto-settings-2">${icon("settings", 14)} 前往设置重新检测</button>
        </div>`;
      body.querySelector("[data-test='analysis-goto-settings-2']").addEventListener("click", () => {
        window.__fathomSettingsSectionPending = "analysis";
        location.hash = "#/settings";
      });
      return;
    }
    if (code === "snapshot_not_found") {
      body.innerHTML = `<p class="hint cfg-error" data-test="analysis-snapshot-missing">所选快照已不可用（可能已被替换或淘汰）；请改选其他日期。基础事实表不受影响。</p>`;
      return;
    }
    body.innerHTML = `<p class="hint cfg-error" data-test="analysis-preview-error">预览生成失败：${escapeHtml(e.message)}</p>`;
  }
}

/** 主状态机：跟随当前 a/b 判定七主态。所有写入前经请求域守卫；
 * 改选区间会开启新域，旧响应全部作废（世代守卫，反例 2）。 */
async function loadAnalysisPanel() {
  const sel = analysisSel();
  if (!sel.a || !sel.b) {
    resetAnalysisSession({ hide: true });
    return;
  }
  const request = beginRequest("analysis");
  clearTimeout(analysisJobTimer);
  analysisJobTimer = null;
  analysisPreview = null;
  analysisJob = null;
  const panel = analysisPanelEl();
  const body = analysisBodyEl();
  panel.hidden = false;
  body.innerHTML = `<p class="hint"><span class="dr-loading" data-dr-spin aria-hidden="true"></span>解读状态加载中…</p>`;

  let cfg;
  try {
    const c = await fetchJSON("/api/config");
    if (!request.current()) return;
    cfg = c.analysis;
  } catch (e) {
    if (!request.current()) return;
    showAnalysisRetry(e.status === 0
      ? "无法连接本地服务，AI 解读状态暂不可用；基础事实不受影响。"
      : `AI 解读状态加载失败：${e.message}`);
    return;
  }
  analysisCfg = cfg;
  if (!cfg || !cfg.enabled) {
    renderAnalysis({ state: "disabled" });
    return;
  }

  let records = [];
  try {
    const r = await fetchJSON(`/api/analyses?a=${encodeURIComponent(sel.a)}&b=${encodeURIComponent(sel.b)}`);
    if (!request.current()) return;
    records = Array.isArray(r.analyses) ? r.analyses : [];
  } catch (e) {
    if (!request.current()) return;
    showAnalysisRetry(e.status === 0
      ? "无法连接本地服务，AI 解读状态暂不可用；基础事实不受影响。"
      : `AI 解读记录加载失败：${e.message}`);
    return;
  }
  // 在途任务优先于已保存的旧报告（ISS-135）：新窗口/新会话里 sessionStorage
  // 是空的，若先渲染历史记录，正在跑的分析就被旧报告挡住，用户只看到一个
  // 早已完成的解读。GET /api/analysis/jobs?a=&b= 是纯读、无副作用、不重派。
  let active = [];
  let activeQueryFailed = false;
  try {
    const r = await fetchJSON(
      `/api/analysis/jobs?a=${encodeURIComponent(sel.a)}&b=${encodeURIComponent(sel.b)}`);
    if (!request.current()) return;
    active = Array.isArray(r.jobs) ? r.jobs : [];
  } catch (_) {
    // 查询失败不是致命：仍继续走历史/重入路径，但必须显式告知并给出重试入口
    // （卡片验收「查询失败可重试」）。静默回落会同时造成两件事——在途任务
    // 不可见时，下方「还没有 AI 解读」是不实陈述；而且用户会被引向
    // 「生成预览 → 确认」的死路，后端按单在途租约会返回 busy 409。
    if (!request.current()) return;
    activeQueryFailed = true;
    active = [];
  }
  const activeJob = active.find((j) => sameAnalysisRange(j, sel)) || null;
  if (activeJob) {
    analysisJob = activeJob;
    renderAnalysis({ state: "running", job: activeJob,
                     cancelling: activeJob.status === "cancelling" });
    scheduleAnalysisPoll(activeJob.job_id);
    return;
  }

  const latest = records.find((x) => !x.revoked) || null;
  if (latest) {
    // 存在未撤销的解读记录 ⇒ 该区间已有 job 达成终态。刷新/重入也必须把
    // 本次尝试标为已结束，否则重跑会复用旧键、后端重放上一次的结果
    // （ISS-128）。这条路径是刷新后最常见的落点，漏掉它等于修复在 R4
    // 场景下完全失效。
    markAnalysisAttemptTerminal({ job_id: null, status: "succeeded" }, sel);
    renderAnalysis({ state: latest.expired ? "expired" : "done", record: latest });
    return;
  }

  // 无在途、无有效历史：按 sessionStorage 记录的 job_id 精确重入（GET 纯读，
  // 不隐式重发）。跨会话的发现已由上面的区间查询覆盖，这里是同会话的精确定位。
  const savedJobId = analysisSavedJobId(sel);
  if (savedJobId) {
    try {
      const r = await fetchJSON(`/api/analysis/jobs/${encodeURIComponent(savedJobId)}`);
      if (!request.current()) return;
      const job = r.job;
      if (job && sameAnalysisRange(job, sel)) {
        if (!job.terminal) {
          analysisJob = job;
          renderAnalysis({ state: "running", job, cancelling: job.status === "cancelling" });
          scheduleAnalysisPoll(job.job_id);
          return;
        }
        if (job.status === "succeeded") {
          // succeeded 但 analyses 无未撤销记录：刚撤销或提交与查询竞态——
          // 再查一次记录层（权威），仍无则回落未分析态。无论是否查得到，
          // 这个 job 已经终态，都要标掉本次尝试（ISS-128）。
          markAnalysisAttemptTerminal(job, sel);
          try {
            const r2 = await fetchJSON(`/api/analyses?a=${encodeURIComponent(sel.a)}&b=${encodeURIComponent(sel.b)}`);
            if (!request.current()) return;
            const latest2 = (Array.isArray(r2.analyses) ? r2.analyses : [])
              .find((x) => !x.revoked) || null;
            if (latest2) {
              renderAnalysis({ state: latest2.expired ? "expired" : "done", record: latest2 });
              return;
            }
          } catch (_) { /* 记录层查询失败：按未分析呈现，可手动刷新 */ }
        } else {
          renderAnalysis({ state: "failure", job });
          // 重入读到终态：同样标记尝试已结束，下一次用户尝试才拿新键。
          markAnalysisAttemptTerminal(job, sel);
          return;
        }
      }
    } catch (_) { /* job 不存在（服务重启清理/已淘汰）：回未分析态 */ }
  }
  renderAnalysis({ state: "idle" });
  if (activeQueryFailed) {
    // 在途状态未知时不能断言「还没有解读」——可能确实有正在跑的任务，且发送
    // 会被占用。复用既有的 showAnalysisRetry 形态（文案 + 重试按钮），不改成
    // 硬失败：历史解读与基础事实仍然可用。
    // 注意 showAnalysisRetry 内部是 replaceChildren，提示必须走它的文案参数，
    // 不能在其之后再 append 节点（会被抹掉）。
    showAnalysisRetry("未能确认该区间是否有正在进行的解读。若已有任务在跑，"
      + "此处暂不显示，发送也会被占用；可重试刷新该状态。",
      "analysis-active-query-failed");
  }
}

/** 会话保存的原 job_id（confirmAnalysisSend 成功后写入）；无记录返回 null。 */
function analysisSavedJobId(sel) {
  try {
    const raw = sessionStorage.getItem(`fathom-aidem:${sel.a}->${sel.b}`);
    if (!raw) return null;
    const saved = JSON.parse(raw);
    return saved && typeof saved.job_id === "string" ? saved.job_id : null;
  } catch (_) { return null; }
}

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
    // ISS-170 R2：真实用户改选（isTrusted）置全局标志——总览交接的迟到 tick
    //（8s 窗口）据此放弃写入，不得把用户已改选的区间覆盖回入口旧值。
    ["sel-a", "sel-b"].forEach((id) => {
      document.getElementById(id).addEventListener("change", (e) => {
        if (e.isTrusted) window.__changesUserTouched = true;
        onSelectionChange(e.target.id);
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
    ["snapshots", "diff", "reportList", "reportContent",
     "detailTrend", "detailBrowse", "analysis", "analysisPoll"].forEach(invalidateRequest);
    // ISS-035C：离页停轮询并清内存预览态；不取消已授权任务（后台继续），
    // 也不清 sessionStorage（回来按原 job 恢复显示，不隐式重发）。
    clearTimeout(analysisJobTimer);
    analysisJobTimer = null;
    analysisPreview = null;
    analysisJob = null;
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
