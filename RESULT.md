# ISS-170 变化页 a/b 快照选项按数据集收敛 — 实现结果

分支：`iss-170-snapshot-options-dataset`　提交：`f181ef8`（未 push，由 PM 代推）

## 1. 改动摘要

### 1.1 反例如何被消除

ISS-160 独立套件钉住的事实：`#sel-a`/`#sel-b` 选项平铺 `/api/snapshots` 全量快照，
旧 HOME 系列（#1–#4）、新整盘系列（#5–#6）与 decoy（#7）三组身份不同的快照混排在同一
列表里。用户可选出跨身份组合 → 后端 `/api/diff` 闸门以 400 正确拒绝 → 但页面净变化行
停在上一区间不更新、且无就地说明，用户面对一个界面无解释的无效区间。

本卡把跨身份组合移到**不可构造**的位置：选项层只列同身份快照。

### 1.2 代码改动（`frontend/modules/pages/changes.js`）

| 改动 | 内容 |
| --- | --- |
| `datasetKey(snap)` | 数据集身份键，三元组 `root` / `min_kb` / `exclude_names` |
| `snapshotCatalog` | 模块级缓存最近一次 `/api/snapshots` 原始列表（收敛与守卫共用判据） |
| `anchorSnapshot(snaps, a, b)` | 收敛锚点：当前 b → 当前 a → 列表首个（列表已按时间倒序，即最新） |
| `sameDatasetGroup(snaps, anchor)` | 取与锚点同身份键的快照子集，**保持原顺序不重排** |
| `applyConvergedOptions(...)` | 把两侧选项重填为同一数据集的快照并落定合法值（`b` 最新、`a` 其前驱，保证 `a !== b`） |
| `loadSnapshotsForDiff` | 加载后先缓存目录，再按锚点收敛；`group.length < 2` 的空态按数据集口径判断 |
| `onSelectionChange(changedId)` | 改选任一侧即把两侧选项收敛到该侧所属数据集 |
| `crossDatasetBlock(a, b)` + `loadDiff` 守卫 | 发请求前判跨身份组合，就地说明 + 保留上一有效读数 |
| `init()` 监听 | `change` 事件带上被改的 select id（`e.target.id`），供收敛判定锚点 |

用户可见行为：

1. 进入变化页 → 选项按当前生效数据集（默认最新快照所属数据集）收敛，异数据集快照不再出现。
2. 用户改选 a 或 b → 两侧选项即时收敛到该侧所属数据集；伙伴侧若落在该数据集外，按
   「b 取最新、a 取其前驱」重新锚定，不留空值、不留跨身份组合。
3. 残余缝隙（异步竞态、overview 跨页交接、外部脚本写 select）由 `loadDiff` 的守卫兜底：
   跨身份组合不发请求，状态行就地给一句说明（写明「不属于同一数据集（测量根或计量阈值
   口径不同）」），并保留上一有效读数；有读数时不改成 0、不清空，无读数时清空结果区
   以免留空壳。**不静默、不把 400 伪装成成功。**

### 1.3 `overview.js` 查证结果：未改动

查证结论：`overview.js` **不构建** `#sel-a`/`#sel-b` 的选项。它只在
`_handOffChangeEntry`（ISS-158 交接）里读变化页自己的 `sel-a/sel-b`，等待选项就绪后写入
真实快照 ID 并派发 `change`，且已有既定降级路径：找不到对应选项就放弃交接（变化页保持
自己的默认选择），绝不伪造基线 ID。因此不涉及共用选择器构建函数，按卡片要求不动该文件。

交接路径在收敛后的行为是安全的：它只写**数据集中真实存在**的 ID，跨页交接若命中另一个
数据集的 b，收敛会把它重新锚定或拒绝该组合（守卫），不会出现无解释的无效区间。

### 1.4 套件改动（`scripts/verify_storage_investigation_frontend.cjs`）

只改 D1 断言一处，转为通过态；**用例总数 49 不变**，其余 48 项断言零改动，
同数据集历史区间断言（`journey-history-non-latest` 等）零改动：

```diff
     record("journey-snapshot-options-cross-dataset-exposed",
-      crossDatasetOffered.length > 0,
-      `已登记缺陷：选项含 …`);
+      crossDatasetOffered.length === 0,
+      `ISS-170 返修后选项已收敛：…`);
```

## 2. 数据集身份判定：既有口径出处

**不新造判定**，逐字段复用后端 `fathom/reports.py::same_dataset`（第 57 行起）：

- 后端两档口径：任一行 `plan_id` 非空（新身份）→ 仅当双方同一 `plan_id` 才可比；
  双方均 legacy（`plan_id IS NULL`）→ 按三元组 `(root, min_kb, exclude_names)` 比。
- 同一口径在别处的既有引用：`fathom/analysis_contract.py:490,535`（解读证据按
  `reports.same_dataset` 校验，不符即 `dataset_mismatch`）、`fathom/analysis_manager.py:1240`
  （`_dataset_matches`）、`fathom/api.py` 的 `/api/trend` 窗口限定 legacy 档（ISS-155 B1）。

前端可取字段的边界（如实说明）：`/api/snapshots`（`fathom/api.py:286-292`）下发
`id, created_at, root, min_kb, exclude_names, collection_status, …`，**不下发 `plan_id`**，
因此前端只能实现 legacy 档三元组判定。归一化照抄后端语义：

- `min_kb` 为 NULL 是「该根的口径未知」数据集，彼此可比较 → 归一为 `""`；
- `exclude_names` 缺列（v5 之前旧记录）兜底为 `""`，与新写入的「无配置」同身份
  （对应后端 `_row_exclude_names`）→ 归一为 `""`。

带新身份（`plan_id` 非空）的行之间是否可比的判断仍由后端闸门负责：**本卡不放宽后端**，
后端 400 闸门语义保持为正确的 fail-closed 行为。

## 3. 验证（真实入口，全部实际执行）

worktree 内无 `.venv`，`FATHOM_PYTHON` 指向同项目兄弟 worktree 的已装依赖解释器
（`iss-157-storage-summary/.venv/bin/python`，已校验 `import fastapi, uvicorn` 通过）；
playwright 1.61.1 已在 worktree 可用（无需安装）。所有用例走真实 `fathom serve` +
真实 Chromium，未使用任何测试替身。

### 3.1 ISS-160 套件（期望 49/49，D1 转通过态）

| 轮次 | 命令 | 退出码 | 结果 |
| --- | --- | --- | --- |
| 1 | `node scripts/verify_storage_investigation_frontend.cjs` | 1 | 48/49，D1 已通过；`journey-keyboard-expand-esc-focus-return` 失败 |
| 2 | 同上 | 0 | **49/49** |
| 3 | 同上 | 0 | **49/49** |
| 4 | 同上 | 0 | **49/49** |

×3 连跑达成：第 2/3/4 轮连续 49/49、退出码 0（D1 断言在四轮中均为通过态）。第 1 轮的
单项失败见下方如实记录，不掩盖。

D1 断言在第 1 轮即为通过态，实测证据：

```
PASS  journey-snapshot-options-cross-dataset-exposed |
      ISS-170 返修后选项已收敛：同数据集 4 项（#4,#3,#2,#1），无异数据集快照
```

即原先混排的 7 个快照（新整盘 #5/#6 与 decoy #7）已全部从选项中消失，只剩旧 HOME
数据集的 4 项；`journey-default-range-latest-two` 仍为 `a=#3 b=#4`（默认区间语义未变），
`journey-history-non-latest` 仍按 `#1→#2` 断言 9.5 GB → 10.5 GB（同数据集历史区间零改动）。

第 1 轮那项键盘焦点失败与本卡无关，如实记录：失败项是
`journey-keyboard-expand-esc-focus-return`（"Enter 未打开详情（focus 起点 BUTTON）"），
落在 `scripts/...:672-698` 的 J4-a 焦点返回用例——该用例对树表做 `page.focus` 后按 Enter，
树层级重渲染会使已聚焦的 `[data-detail]` 按钮脱离文档，Enter 落到失效节点上；本卡未触碰树表
渲染、焦点或键盘路径，且第 2/3 轮同一用例通过，判定为该套件既有的时序 flake 而非本卡回归。

### 3.2 既有套件零破坏

| 套件 | 期望 | 实测 | 退出码 |
| --- | --- | --- | --- |
| `node scripts/verify_frontend_refresh.cjs` | 217 | 见下 | 见下 |
| `node scripts/verify_scope_settings_frontend.cjs` | 37 | **37/37 通过**（`{"ok":true,"passed":37,"failed":0}`） | **0** |

### 3.3 NOT_VERIFIED

- `plan_id` 新身份档的前端收敛**未验证**：`/api/snapshots` 不下发该字段，前端无法按新身份
  档收敛（已在代码与本文件如实标注）。该档的可比性判断仍完全由后端闸门承担，未放宽。
- 「无判据放行给后端闸门」这条守卫分支（`snapshotCatalog` 为空时）未由套件覆盖：
  正常路径下目录先于对比就绪，套件未构造该竞态。
- 「当前生效数据集内不足两个快照」的新增空态文案分支未由 ISS-160 套件覆盖
  （种子中每个数据集都有 ≥2 个快照）；该分支只改文案，不改请求与渲染路径。
- 未跑 `git push`（按卡片红线由 PM 代推）；未改动后端 `fathom/` 与 `tests/`（只读）。

---

# ISS-170 R1（窄返修）：收敛改为不对称，保住跨数据集可达性

## 4. 返修起因

CI 实测反例（main 合成种子）：`scripts/verify_tree_changes_frontend.cjs:275` 的
`page.selectOption("#sel-a", "14")` 直接抛 `did not find some options`——R0 的**对称收敛**
让初始选项只含默认数据集（#19/#20 系），跨数据集快照（#14/#15 weird 系）在选项里根本不存在。
用户没有任何 UI 路径切换数据集：这是产品级可达性缺陷，不是套件问题。
根因：把「跨数据集组合不可构造」实现成了「跨数据集快照不可见」，收敛锚点同时钉在两侧。

## 5. 修法（不对称收敛）

| 侧 | 行为 |
| --- | --- |
| `#sel-b`（对比基准侧） | **不收敛**：始终列出全部快照（按既有顺序）。用户改 b 即切换数据集 |
| `#sel-a`（历史点侧） | 收敛到**当前 b 所属数据集**：改 b 后 a 侧选项重填为 b 数据集并按「a 取其前驱」落定；改 a 只在 b 数据集内收敛，不动 b |
| 初始加载 | b=最新，a=其前驱（现状不变）；a 选项=b 数据集子集 |
| `loadDiff` 守卫 | 保留（竞态兜底语义不变） |
| `datasetKey` / `snapshotCatalog` / 归一化语义 | 零改动 |

代码改动（`frontend/modules/pages/changes.js`）：

| 改动 | 内容 |
| --- | --- |
| `anchorSnapshot(snaps, selectedB)` | 锚点恒为 b（原 `b → a → 首个` 三级兜底去掉 a 那一级）；b 不可用时兜底取列表首个 |
| `applyConvergedOptions(selA, selB, snaps, {...})` | 第三参由「数据集分组」改为**全量目录**；`replaceSnapshotOptions(selB, snaps)` 全列、`replaceSnapshotOptions(selA, group)` 收敛。返回值新增 `group` |
| 落定规则 | `b = keepB 仍在目录内则保留，否则目录首个`；`a = keepA 仍在 b 数据集内则保留，否则取组内 b 之外最新者`。**用户改选任一侧**时 `a === b` 一律保留（用户显式造成的无效区间交由后端如实暴露，不静默改写）；只在初始加载/快照列表刷新（无用户改选）时强制取前驱 |
| 用户清空某一侧 | 保留空值不补回——空选是 ISS-093「请选择基线与对比快照，选齐后自动对比」的合法意图。b 被清空时 a 侧收敛锚退回 a 自身，避免两侧一起变空 |
| `onSelectionChange(changedId)` | 不再按 changedId 选锚点，统一以 b 为锚；目录未就绪（`snapshotCatalog` 为空）时不动选项 |
| `loadSnapshotsForDiff` | `validB` 判据由「分组内」改为「目录内」（b 侧全列），`validA` 仍判分组内 |

不变量：两侧恒满足 `datasetKey(a) === datasetKey(b)`，跨数据集组合在选项层仍不可构造；
新增的是**切换数据集的入口**，不是放宽可比性闸门。

## 6. 套件同步

- `scripts/verify_storage_investigation_frontend.cjs`：D1 断言改为不对称口径——a 侧无异数据集
  快照**且** b 侧仍是全量（`histPair.b.length >= OLDHOME_IDS.size`，显式钉住切换入口）。
  用例名与总数（49）不变。
- `scripts/verify_tree_changes_frontend.cjs`（本次新增写域，**仅改选择顺序**）：全部 16 组
  `selectOption("#sel-a", X)` / `selectOption("#sel-b", Y)` 改为先 b 后 a。该种子下每个快照
  都带各自的 root，故每组都是跨数据集区间，必须先由 b 打开该数据集再选 a。
  **断言语义零改动**（同一组 a/b 取值、同一 waitUntil、同一 record 名）。

## 7. 验证（全部实际执行）

```bash
export FATHOM_PYTHON=/Users/maoking/orca/workspaces/fathom/iss-157-storage-summary/.venv/bin/python
node scripts/verify_storage_investigation_frontend.cjs   # 49/49 ×2
node scripts/verify_tree_changes_frontend.cjs            # 74/74 ×2
node scripts/verify_frontend_refresh.cjs                 # 217 零破坏 ×1
```

| 命令 | 期望 | 第 1 轮实测 | 第 1 轮退出码 | 第 2 轮实测 | 第 2 轮退出码 |
| --- | --- | --- | --- | --- | --- |
| `verify_storage_investigation_frontend.cjs` | 49 | `{"ok":true,"passed":49,"failed":0}` | 0 | `{"ok":true,"passed":49,"failed":0}` | 0 |
| `verify_tree_changes_frontend.cjs` | 74 | `{"passed":74,"failed":0}` | 0 | `{"passed":74,"failed":0}` | 0 |
| `verify_frontend_refresh.cjs` | 217 | `217 PASS，0 FAIL` | 0 | — | — |

（表中为**最终代码**上的连跑结果。）

返修过程中 refresh 套件先后暴露了 R1 收敛的两处**过强**语义，均已修正并补跑：

| 暴露点 | 症状 | 修正 |
| --- | --- | --- |
| `verify_frontend_refresh.cjs:2283` | 用户把 b 改到与 a 相同的值（显式 `a===b` 区间），收敛却把 a 改写成 b 的前驱，`已对比快照 #2 → #2` 永不出现 → 30s 超时 | 落定规则的「不改写用户选择」豁免扩展到**用户改选的任一侧**（不只 a），只在初始加载/列表刷新时强制取前驱 |
| `verify_frontend_refresh.cjs:2293` | 用户把 a 清空（ISS-093 空选引导态），收敛却把 a 静默补回合法值，`选齐后自动对比` 永不出现 → 30s 超时 | 用户显式清空某一侧时保留空值不补回；b 被清空时 a 侧锚退回 a 自身，避免两侧一起变空 |

D1 在 R1 口径下的实测输出（不对称口径直接可读）：

```
PASS  journey-snapshot-options-cross-dataset-exposed |
      ISS-170 R1 不对称口径：a 侧同数据集 4 项（#4,#3,#2,#1），无异数据集快照；
      b 侧全列 7 项（跨数据集切换入口保留）
```

即原先从两侧一起消失的新整盘 #5/#6 与 decoy #7，现在只在 a 侧消失（a 侧 4 项，零异数据集），
b 侧 7 项全列保留了切换入口；`journey-default-range-latest-two` 仍为 `a=#3 b=#4`，
`journey-history-non-latest` 仍按 `#1→#2` 断言 9.5 GB → 10.5 GB。

## 8. R1 NOT_VERIFIED

- `plan_id` 新身份档的前端收敛仍**未验证**（`/api/snapshots` 不下发该字段，与 R0 相同）。
- 「当前生效数据集内不足两个快照」的文案分支仍**未覆盖**（各套件种子中每个数据集都有 ≥2 快照）。
- 「判据缺失放行给后端闸门」分支（`snapshotCatalog` 为空）仍**未覆盖**。
- 未跑 `git push`；未改动后端 `fathom/` 与 `tests/`（只读）。
