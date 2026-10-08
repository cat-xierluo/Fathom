# ISS-178 交付报告：范围采集 UI 接线（D1）+ 交接基线空（D2）

基线 `601636c`（ISS-177 bigfiles 范围语义）。分支 `iss-178-scope-collect-wiring`。中文提交、无署名、未 push。

## 前置说明

任务卡引用的 `verify-results/iss161-native-20261007-191108/REPORT.md` **不在本仓库任何 worktree 中**
（`fathom/` 下 14 个 worktree 全量查找无 `iss161` 目录）。本卡的修法完全依据任务卡内转述的实机验收
结论推进，症状与卡内描述逐条对齐；若上游报告有更细的复现步骤，需二次核对。

---

## D1（P1）：范围采集按钮没走多范围路径

### 根因（已复现确认）

`fathom/api.py` 的 `POST /api/scan` 恒调 `scan_coordinator.start_scan(source="api")`，**不传 `scopes`**。
`start_scan` 的 `scopes` 为空 → `ScanSession.scopes == []` → `is_round` 为假 → 走原**单根旧路径**。

于是：快照 `root` = `config.DEFAULT_ROOT`（打包态即隔离 HOME）、`plan_id` 为 NULL，
`scan_plans` / `scan_rounds` / `scan_round_members` 三表全空。

CLI 侧 `cmd_scan` 走 `_scan_scopes(args)` → 传 `scopes=` → 走多范围执行体，因此 CLI 贯通。
**同一环境两种入口分叉**，是 D1 的全部来源。设置页 `settings.js:2887` 的
`apiPost("/api/scan", {})` 正是这个端点，所以「开始首次采集」= 旧单根口径。

### 修法

**1. `fathom/cli.py`：范围构造逻辑公共化（CLI/UI 唯一实现）**

- 新增 `scope_specs_from_paths(paths, ids)`：顺序即采集顺序、`ids` 按位置一一对应、
  缺省由 `ScopeSpec.from_path` 按规范根派生 path 型 ID（**绝不伪造** `apfs-volume:<uuid>`，ISS-153 身份合同）。
- 新增 `scope_specs_from_effective_selection()`：由 `config.effective_scope_selection()` 构造；
  为 `None`（用户从未选择范围）时返回 `[]`，调用方据此回落旧单根口径。
- `_scan_scopes(args)` 改为调用 `scope_specs_from_paths`，**CLI 行为与出口码零变化**。

**2. `fathom/api.py`：`/api/scan` 复用多范围执行体**

- 延迟导入 `cli`，取 `scope_specs_from_effective_selection()` 传入 `start_scan(scopes=...)`。
- 新增 `ScanScopeError` → **400 如实报错**，绝不静默回落到单根旧路径假装扫描成功。
- 响应新增 `scopes`（本轮范围数）如实带出单根/多范围形态。

**3. 语义保持不变**：「保存只影响下一轮计划」「立即扫描＝旧单根口径手动扫描入口」均不变形——
范围计划由**已保存的生效选择**构造，不是临时猜测；`scan_coordinator` 零改动。

### D2（P2）：交接后基线（a 侧）选项为空

### 根因（先诊断后修，非试改）

`overview.js` 的 `_handOffChangeEntry` 在 tick 里的顺序是：

```
selB.value = entry.b;
const aWanted = ... [...selA.options] ...   ← 在这里读 a 侧选项
selB.dispatchEvent(change)                   ← 变化页此时才重填 a 侧
```

而 **a 侧选项是变化页按 b 所属数据集收敛后才重填的**（`changes.js` 的 `onSelectionChange`
→ `applyConvergedOptions`：a 侧收敛到 b 的数据集、b 侧不收敛）。交接时 b 刚被改写、a 侧尚未重填，
读到的是**旧 b 的选项集**（常为空）→ `aWanted` 为空 → a 侧留空 → 无法构造对比。

**这不是「R2 收敛逻辑没跑到」也不是「被让位拦截」**：R2 的 `window.__changesUserTouched` 让位门在
交接场景下并不触发（真实用户改选才置标志，交接是合成事件）。真正断的是**读取时机**。

### 修法（`frontend/modules/pages/overview.js`）

- 改为**先派发 `selB` 的 change**，让变化页按自己的收敛逻辑把 a 侧重填为 b 数据集、
  并按「a 取其前驱」落定（**同 plan 前驱由变化页给出**）；
- 随后**入口真的带了 a**（真实快照 ID）且它此刻确实在 a 侧选项里时才覆盖。
- 删除死函数 `_pickPredecessorId`（它读的正是收敛**之前**的旧选项集，是 D2 留空的根因），
  避免两处前驱判定漂移。

**交接语义不变**：真实 a 照旧带入；入口不带 a 时取同数据集真实前驱；
a 为空或 `a === b` 都绝不塞假基线，找不到就放弃交接。
**「跨数据集不可比」既有断言零回归**——收敛逻辑未动，只改了读取时机。

---

## 测试

### pytest：新增 12 个用例（`tests/test_scope_collect_wiring.py`，全新文件）

1. **D1 核心反例**：`test_api_scan_with_scope_selection_lands_plan_and_round`——UI 触发落库
   **带 plan_id**、`scan_rounds` 1 条、`scan_round_members` 2 条、成员都挂上快照、轮次状态如实。
2. `test_api_scan_uses_derived_scope_id_when_ids_absent`——未给 ID 时派生 `path:` 型，不伪造卷身份。
3. `test_scope_ids_mismatch_returns_400_not_silent_legacy`——错位 → 400，不静默回落，不留锁。
4. `test_no_scope_selection_keeps_single_root_legacy_path`——未启用时 `plan_id IS NULL`、无轮次（**逐字节不变**）。
5. `test_legacy_snapshot_root_is_default_root`——legacy 根仍是 `FATHOM_SCAN_ROOT` 解析值。
6. `test_cli_and_api_share_one_builder`——CLI 入口与公共段产出**完全相同**的范围规格。
7. `test_effective_selection_builder_matches_selection` / `test_no_selection_yields_empty_scopes`
   / `test_preserves_collection_order_of_selection` / `test_id_count_mismatch_raises_scope_error`。
8. `test_scan_status_endpoint_still_polls` / `test_api_scan_source_stays_api`——既有轮询面与来源登记不漂移。

**先红后绿已实证**：临时 `git stash` 掉三处源文件改动后重跑 → **8 failed / 4 passed**；
恢复后 **12 passed**。

### 前端套件：D2 断言落在 `verify_storage_overview_frontend.cjs`（**与任务卡指定位置不同，已改**）

**卡要求把 D2 断言加进 `verify_storage_investigation_frontend.cjs`（55→N）。实测该套件夹具
从不让范围能力生效**——总览恒渲染「尚未启用整盘范围」，`[data-test='storage-cta']` 永不出现
（套件内 `journey-rescan-shows-real-before-after` 的 detail 即为「尚未启用整盘范围」）。
故 D2 交接在 160 套件里**根本无法构造**，断言放进去只会 3 条恒红（已实证：加进去即
`passed:55, failed:3`，detail=「总览未渲染整盘 CTA」）。已把该改动回退，套件回到 55 零破坏。

D2 断言改加在 **`scripts/verify_storage_overview_frontend.cjs`**——该套件夹具**确实启用了
范围能力**，且 C3 段本就是整盘 CTA 交接旅程（`ui-cta-carries-real-b` / `ui-cta-a-real-predecessor`）。
新增 3 条（59 → 64 passed）：

- `ui-cta-handoff-baseline-non-empty`：交接后 a 侧选项非空、选中值非空、`a !== b`。
- `ui-cta-baseline-same-dataset-as-b`：a 侧已收敛到 b 所属数据集（判据取 `/api/snapshots` 的
  真实 `plan_id`，ISS-176 前端同源口径，不假设夹具里哪两个集合跨数据集），**无异数据集残留**。
- `ui-cta-diff-autoconstructed`：diff 自动构造（结果区非空、非加载态）。

**为什么原套件抓不到 D2**：原 C3 点的 CTA 其 `data-b` 就是变化页的**默认 b**（最新快照），
交接不发生数据集切换，a 侧本来就是对的——旧代码因此 59 全绿。新断言先**预热**变化页让 a 侧
先行收敛落定，再交接 summary 侧的真实 b。

---

## 验证（真实执行，命令 + 退出码）

| 命令 | 退出码 | 结果 |
|---|---|---|
| `python3 -m pytest tests/test_scope_collect_wiring.py -q` | 0 | 12 passed |
| `python3 -m pytest tests/test_plan_identity_consumers.py tests/test_scope_config.py -q`（×3） | 0 / 0 / 0 | 66 passed ×3，稳定 |
| `python3 -m pytest tests/ -q -k "scan or scope"` | 0 | 398 passed（重跑；见下偶发项） |
| `node scripts/verify_storage_investigation_frontend.cjs` | 0 | 55 passed, 0 failed（零破坏） |
| `node scripts/verify_storage_overview_frontend.cjs` | 1 | 64 passed, 1 failed（既有偶发项，见下） |
| `node scripts/verify_frontend_refresh.cjs` | 见下 | 217 |

### 两处非本卡引入的失败（均已归因，不掩盖）

1. **`pytest -k "scan or scope"` 首轮**：`test_analysis_upgrade_gate.py::test_analysis_running_scan_flock_and_short_writes_free`
   首轮 397 passed / 1 failed（`AnalysisError`）。**重跑全绿：398 passed，退出码 0**；
   且该用例单独运行带修复/不带修复均通过 → 既有的顺序/时序偶发，与本卡无因果。
2. **`verify_storage_overview_frontend.cjs` 的 `ui-console-clean`**（detail「无法连接本地服务」）：
   **已实测基线**——把 `overview.js` 与该套件脚本一起 stash 回基线后重跑，
   同样失败（`passed:58, failed:1`，同一条 detail）。基线 58+1=59 条既有检查，
   修复后 64 passed + 同一条既有失败 → **59 条既有检查零破坏**，失败为既有问题、非本卡引入。

### 关于 D2 断言的诚实说明（先红未能在前端套件达成）

D2 的**根因诊断与修法**是确定的（a 侧选项在 b 侧 change 之后才重填，读取时机错位）。
但**本仓库两个前端套件的夹具都无法复现 D2 的触发时序**：
- `verify_storage_investigation_frontend.cjs` 夹具从不让范围能力生效 → 整盘 CTA 永不出现；
- `verify_storage_overview_frontend.cjs` 夹具只有一个数据集，且 CTA 的 `data-b` 恰是变化页的
  **默认 b** → 交接**不发生数据集切换**，a 侧本就是对的，修复前后都通过。

因此新增的 3 条断言是**交接合同的回归护栏**（a 侧非空 / 与 b 同数据集 / diff 自动构造），
**不是**对旧代码的「先红」证明。D2 的先红证据目前只有根因代码分析，无自动化反例——
已如实上报 PM，建议由 PM 决定是否追加一个跨数据集夹具（需新造第二个数据集快照）来补这条红。

（新增断言已用「页内 hash 导航」而非整页 `goto`：整页导航会卸载文档、打断在途 fetch，
凭空制造「无法连接本地服务」控制台错误，污染 `ui-console-clean`。）

## PM 收口附记（2026-10-07 深夜）

- 验证复核：test_scope_collect_wiring 12 passed；160 套件 55/55（首轮 keyboard 单项=ISS-172 既有债，重跑绿）；overview 套件「无法连接本地服务」console 噪音（stash 基线同挂的 serve 竞态回声）按资源噪音过滤后 **65/65 ×2 全绿**——计数定稿 OVERVIEW 59→65、PYTEST 1492→**1504**（12 例 diff 核定），双处同步由 PM 补提交。
- 160 前端消费端断言缺失（先红未达）如实保留：消费端行为归 161 完整 UI 轮实机复验。

---

# R2 窄返修（D2 两处 CI 失败，plan 档 / CI 慢环境）

基线 `ea79fbf`（D1 交付，PYTEST 1517 五 job 绿）。分支 `iss-178-r2-plan-tier-converge`。
**D1 三文件（`fathom/api.py`、`fathom/cli.py`、`tests/test_scope_collect_wiring.py`）零改动。**

两处失败都在「本机热页 65/65 全绿、CI arm64 冷环境才现」，因此**先定位时序差，再决定改哪一侧**。

## 失败 1：`ui-cta-baseline-same-dataset-as-b`（detail `b=#4(plan:p-kid) a 侧=["3","1"]`）

### 归因：断言抢在收敛落地前读表（不是收敛逻辑错判数据集）

原断言只等「a 侧非空且取值非空」：

```js
await page.waitForFunction(() => {          // ← 上一版判据
  const sel = document.getElementById("sel-a");
  return sel && sel.options.length > 0 && sel.value !== "";
}, null, { timeout: 20000 }).catch(() => {});
```

而**预热段遗留的旧选项集**（预热把 b 选到 #1，a 侧已收敛为 `#1` 所属数据集 `[#3,#1]`、a=#3）
在交接后的收敛重填发生前，**同样满足**这个判据。慢环境下断言抢跑，读到的正是过渡态的
`["3","1"]`——与 b（`#4`，`plan:p-kid`）不同数据集，于是报「异数据集残留=[3,1]」。
本地热页收敛在断言前已落地，故全绿；CI 冷环境才现。**这是时序假红**。

关键佐证：`applyConvergedOptions`（`changes.js:218`）里 b 侧不收敛、全列，a 侧收敛到 b 数据集；
一旦收敛真的跑过，a 侧**不可能**同时留着别的数据集的选项。读到 `["3","1"]` 只可能是**还没重填**。

### 任务卡给的①②取舍：本轮**只取①的判据形态，不动前端收敛**

- ①「catalog 失效刷新」：查过 `changes.js`，`loadSnapshotsForDiff` 每次都重新 fetch
  `/api/snapshots` 并在 fetch 后 `snapshotCatalog = snaps` 再收敛（`changes.js:262`），
  **没有**需要失效的持久陈旧缓存；交接 tick 期间用的 catalog 即使是旧的，
  在途的那次 fetch 落地后也会**按新数据重新收敛**（自愈）。再加一层刷新只是多一次往返，
  且会牵动 `onSelectionChange` 的世代/票据守卫（ISS-170 R2 的三连发防线），风险大于收益。
- ②「派发后异步确认重试」：与①同样问题——它治的是**产品行为**，而本处证据表明产品行为最终是对的。
- **取舍结论**：过渡态是**测试读数**问题，不是产品缺陷。改断言的**稳定性判据**（等收敛真落定），
  是最小、最贴合证据的修法；前端收敛逻辑与交接语义一字不动。

### 修法（`scripts/verify_storage_overview_frontend.cjs`）

断言前等 a 侧**收敛真的落定**，判据与断言**同源**（`/api/snapshots` 的真实 `plan_id`，ISS-176 前端口径）：

- a 侧选项**全部**与 b 同数据集 → 收敛完成；
- a 侧为空 → 无可比区间，交给变化页就地说明（也接受）；
- 其余形态继续等，**等到超时再让断言如实判红**（detail 追加「a 侧未在 20s 内收敛到 b 数据集」）——
  不再 `.catch` 掉后立刻拿过渡态当结果。

## 失败 2：stale 相 `browser-flow-error`（waitForFunction 20s 超时）

### 归因：交接等待窗口 8s 在冷环境下先到期，套件 20s 等一个不会来的 b

`_handOffChangeEntry`（`overview.js`）的 tick 每 100ms 轮询等 b 落进 `sel-b` 选项，
到 `deadline = Date.now() + 8000` 就**静默放弃交接**（`state.pendingChangeEntry = null`）。
8s 是按「本机热页」的直觉定的：变化页挂载 + `/api/snapshots` 往返 + 首次渲染。冷启动
（CI arm64 首访）完全可能超过它——**tick 到期即放弃 ⇒ `sel-b` 永远停在旧值**，
套件随后在「等 b 落位」处 20s 超时，未捕获 ⇒ 整段判 `browser-flow-error`。
只出现在 stale 相，是因为它是同一进程里的**第二轮**：服务端刚起、首个真实请求，全链路最冷。

### 修法（`frontend/modules/pages/overview.js`）

交接等待窗口 **8s → 30s**，并把**套件侧等待窗口同步对齐到 30s**：套件窗口必须 ≥ 页面窗口，
否则慢环境下是**套件先放弃**（假红），而不是页面交接失败。

**交接语义一字未变**：不塞假基线、a 为空即放弃、用户已改选（`__changesUserTouched`）立即放弃、
收敛逻辑（`applyConvergedOptions`）与票据守卫零改动。放宽窗口只推迟「确实没有该快照」时的放弃。

### stale 相断言改为宽容终态（`ui-cta-diff-autoconstructed`）

身份闸门（ISS-176 起）变严后，混搭身份（legacy↔plan）被后端 400 拒绝是**正确行为**——
绝不能为凑一条对比而造无效区间。stale 相尤其如此：本轮有失败成员、父子根被吸收，
交接落下的 a/b 未必落在同一数据集，此时期望的终态是「**就地说明为什么不可比**」，
而不是死等一个不会来的完成态。断言放宽为二选一：

1. **diff 完成态**：净变化行渲染出来，且不在加载中；
2. **就地说明**：状态行出现身份/不可比说明（如「不属于同一数据集」「无法构成有效对比区间」）。

两者都要求状态行**不滞留加载态**；页面崩溃不放行——由 `ui-console-clean` 的 `pageerror`
收集单独判红。

### 是否该在 overview 侧就拦掉 stale 相的 CTA？（任务卡征询）——**不该，理由如下**

- CTA 的存在依据是 `attr.measured_members` 里有**真实落库的快照 ID**，stale 相里该条件成立；
  stale 影响的是「本轮与前轮**可比**性」，不是「b 这个快照是否存在」。
- 交接落下的 a/b 若真的跨身份，**正确终态就是就地说明**——这正是上面第 ② 分支要断言的行为。
  在 overview 侧提前拦掉，等于把「不可比」藏起来，用户看不到任何解释，反而违背 ISS-176 的 fail-closed 口径。
- 且这需要改 CTA 渲染条件，任务卡明确划为不扩张的范围。**故不动。**

---

## R2 验证（真实执行）

| 命令 | 结果 |
|---|---|
| `python3 -m pytest tests/test_scope_collect_wiring.py tests/test_plan_identity_consumers.py -q` | **26 passed**（D1 零回归，7.29s） |
| `node scripts/verify_storage_overview_frontend.cjs`（含 clean/stale 两相） | **65/65**（完成轮次） |
| `node scripts/verify_storage_investigation_frontend.cjs` | **54/55**（唯一失败为既有债，见下） |
| `git diff ea79fbf --name-only` | 仅 3 个写域文件；**D1 三文件 0 改动** |

### 写域合规

```
frontend/modules/pages/overview.js            |  8 ++-   （交接等待窗口 8s→30s + 注释）
scripts/verify_storage_overview_frontend.cjs | 98 +++-- （断言与等待窗口）
RESULT.md                                    | 90 ++
fathom/api.py · fathom/cli.py · tests/test_scope_collect_wiring.py → 0 改动
```

`changes.js` **零改动**：查证后确认收敛逻辑无缺陷（见失败 1 取舍），不需要动它。

### 未达成的部分与既有债（如实记录，不掩盖）

1. **`investigation` 套件 54/55**：`journey-keyboard-expand-esc-focus-return`
   （detail「Enter 开详情→Esc 关闭→焦点回到 BODY」）。这是本卡上一轮 PM 已记录的
   **ISS-172 既有债**（RESULT.md 上文「160 套件 55/55（首轮 keyboard 单项=ISS-172
   既有债，重跑绿）」）。本轮 `changes.js` 零改动、该用例走的键盘焦点链路不在
   写域内，与本卡无因果。
2. **`overview` 套件仍有 1 处间歇失败**：clean 相 D2 段
   `page.waitForFunction`（等 `sel-b` 选项 > 1）**45s 未落位** → 记 `browser-flow-error`，
   当轮 51/52（后续十几条被跳过）。**该等待在改动前就存在**（原 20s），
   本轮只是把窗口放宽到 45s；且同一份代码在完成轮次上 65/65 全绿。
   **未能在本地稳定复现、也未能归因到确切的页面/后端环节**——按实况上报，
   不宣称已解决。CI 若仍现，方向是给该等待**兜底成一条记录**（而非中断整段），
   但套件 CI 按 **65 条精确计数**（`EXPECTED_STORAGE_OVERVIEW_PASSED`），
   改成跳过/兜底会动计数、须 PM 拍板，故本轮未擅自改。
3. **失败 1 的「先红」仍只能在慢环境复现**：本地热页收敛总在断言前落地，
   改前 65/65、改后 65/65 均绿。本轮的判据修正依据是**代码级时序分析**
   （预热旧选项集同样满足旧判据）+ CI detail 的 `["3","1"]` 形态
   （`applyConvergedOptions` 收敛过后 a 侧不可能残留别的数据集选项），
   **不是**本地可复现的先红证据——如实标注。

---

# R3（D2 实机基线仍空：根因钉死与修复）

基线 `c476bfb`（D2 前一轮 #273）。分支 `iss-178-r3-d2-native-rootcause`。
**D1 三文件（`fathom/api.py`、`fathom/cli.py`、`tests/test_scope_collect_wiring.py`）与
`scan_coordinator`/scanner/db 零改动**（`git diff --name-only` 核定）。

## 根因（代码级钉死，附判定链）

实机形态是 **b 侧正确带入、a 侧 `value` 为空且选项状态未知**——不是「选错」，是**从未落定**。
任务卡列的四个怀疑项里，**b) `__changesUserTouched` 让位**与 **d) tick 超时**可排除：
交接派发的是合成事件（`isTrusted === false`），让位标志不会被它置上（changes.js:1883）；
tick 超时会留下 `sel-b` 停在旧值，与实机「b 正确」相反。

真正断的是 **c 的近亲：交接的合成 change 被变化页当成了「用户改选」**。
`overview.js` 的 `_handOffChangeEntry` 写入两侧后 `dispatchEvent(new Event("change"))`，
`changes.js` 的监听器只区分 `isTrusted`（用于置让位标志），随后**一律**调
`onSelectionChange(e.target.id)`——而 `onSelectionChange` 的语义是「用户改选」：

```
onSelectionChange(changedId):
  snapshotSelectionRevision += 1          ← ①
  applyConvergedOptions(..., { userChanged: changedId })   ← ②
```

两条后果叠加，正是实机形态：

1. **② `userChanged` 误判**：`applyConvergedOptions` 里
   `userEmptyA = Boolean(userChanged) && String(keepA) === ""`（changes.js:220）。
   交接此刻 a 侧尚未落定、`keepA` 为空串，而 `userChanged` 因 `changedId="sel-a"/"sel-b"`
   非空而**为真** → 判定成「用户显式清空了基线」→ `nextA = ""`，**a 侧被钉死为空**。
   注释写明的「不静默补回合法值」是为用户意图设计的保护，被合成事件误触发。
2. **① 推高用户世代**：交接恰落在 `loadSnapshotsForDiff` 的 `await fetchJSON("/api/snapshots")`
   期间（打包态冷启动首次 CTA 必然如此：变化页刚挂载即交接）→ 命中
   `changes.js:269` 的「fetch 期间用户改选 → 整体放弃」分支。该分支
   `replaceSnapshotOptions(selB, snaps)` 后**直接 return**：不收敛、不落定 a、**不补发 diff**。
   于是 a 侧既没被填、也没被补发，停在「value 空、选项状态未知」。

**为什么套件抓不到**：套件 C3 段的 CTA `data-a` 是**空串**（`data-a=""`，overview.js:635），
且交接前变化页已预热出可用的 a 侧；`userEmptyA` 需要 `keepA === ""` 才误判，
预热把 a 填上后该分支**恰好不成立**——夹具的预热顺序正好掩盖了实机的冷启动时序。
即：**热页 + 预热 ⇒ 缺陷不可见；冷启动首次 CTA ⇒ 缺陷必现。**

## 修法（交接与用户改选分流）

- `overview.js`：新增 `_handoffEvent()`，交接派发的两个 change 带 `__fathomHandoff = true` 标记。
- `changes.js`：监听器把该标记传给 `onSelectionChange(id, { handoff })`；
  `onSelectionChange` 在 `handoff` 为真时**不推 `snapshotSelectionRevision`**
  （在途目录请求仍按本次收敛正常落定），并以 `userChanged: ""` 调用
  `applyConvergedOptions`（不再误判为用户清空基线）。

**交接语义一字未变**：不塞假基线、a 为空即放弃、用户已改选（`__changesUserTouched`）
立即放弃、`applyConvergedOptions` 的收敛口径与票据守卫零改动。真实用户改选路径
（`handoff === false`）逐字节保持原行为，ISS-170 R2 的三连发防线不受影响。

**覆盖两种时序**：冷启动首次 CTA（`snapshotCatalog` 未就绪 → `onSelectionChange` 的
`if (snapshotCatalog.length)` 跳过收敛，交接事件不推世代 → 在途 fetch 落地后按
`changedId` 之外的正常路径收敛落定 a）；进过变化页后二次 CTA（catalog 已就绪 →
交接事件走 `userChanged: ""`，a 按「取其前驱」落定）。

## 诊断探针（无副作用，供实机取证）

`window.__diag` 存在时记录：`overview.js` 每次 tick 的 `ready/hasB/entry/两侧 value 与
options/userTouched` 与 `tick-deadline`；`changes.js` 每次 `onSelectionChange` 的
`changedId/handoff/catalogLength/userRevision/收敛前后两侧取值与选项`。
不写 console、不改控制流；**PM 决定保留（降为无副作用探针）或移除**。

```js
// 实机取序列：点击 CTA 前后在控制台执行
window.__diag = [];
// …点击「排查这次变化」…
copy(JSON.stringify(window.__diag, null, 1));
```

## 验证（真实执行）

> 环境说明：本 worktree 无 `.venv`，套件按 `FATHOM_PYTHON=$(which python3)` 注入解释器
> 运行（`fastapi/uvicorn` 已在系统解释器可用）。**打包态实机复验仍待 PM 执行（最终门）。**

| 命令 | 结果 |
|---|---|
| `verify_storage_overview_frontend.cjs` ×4（含带诊断探针的最终态） | **65/65 全绿 ×4** |
| `verify_storage_investigation_frontend.cjs` ×2 | **55/55 ×2** |
| `verify_frontend_refresh.cjs` | **217/217，0 failed** |
| `pytest tests/test_scope_collect_wiring.py -q` | **12 passed**（D1 零回归） |

D2 相关断言逐条实测（含带探针的最终态 ov4）：

```
PASS ui-cta-handoff-baseline-non-empty   | 预热 a 侧=["3","1"] → 交接后 a=#3（#3,#1）b=#1
PASS ui-cta-baseline-same-dataset-as-b   | b=#1(plan:p-root) a 侧=["3","1"] 异数据集残留=[]
PASS ui-cta-diff-autoconstructed         | 完成态：根同口径净变化 −20.0 MB（a=#3 b=#1）
PASS ui-console-clean                    | 探针零副作用，未引入控制台噪音
```

## 如实记录的未达成部分

1. **实机复现未能在本 worktree 达成**：打包态实例需 PM 启动（HOME 重定向隔离 +
   FATHOM_PORT 注入 + 受控 APFS 卷 + 旧 HOME legacy 种子），本地只有套件夹具。
   上述根因是**代码级钉死**（`userEmptyA` 误判 + 在途 fetch 放弃分支，两条都可在源码
   逐行核对），并已装好 `window.__diag` 探针供实机取证——**但仍不是实机 __diag 序列本身**。
2. **套件仍无法复现该缺陷的触发时序**（沿用前一轮结论）：C3 段 CTA 的 `data-a` 是空串且
   交接前已预热出可用 a 侧，`userEmptyA` 的误判前提（`keepA === ""`）不成立。
   新增探针与修复**未**把套件从「恒绿」变成「先红」——修法在套件形态下与旧行为等价。
   要让套件具备先红能力，需要一个「冷启动即交接、a 侧未预热」的夹具变体，
   属套件侧工作，本卡未擅自改动 CI 计数敏感区。
3. **诊断探针去留待 PM 定**：当前为无副作用形态（仅在 `window.__diag` 存在时写数组，
   不写 console、不改控制流），四个套件全绿未受其影响。
