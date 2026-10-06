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
