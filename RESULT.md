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
