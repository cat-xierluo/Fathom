# ISS-148 交付报告：默认树形变化与目录详情接线

- 分支：`iss-148-hierarchical-changes`（基线 main `a387bd4d15df5aef66d5cf0f6bcadd2dc64f7f29`，已含 ISS-147 `/api/diff/children`）
- 交付 commit：见本文件末尾（本卡不 push、不建 PR，PM 收口）
- 实施会话说明：前任实施 agent 中断，遗留 `frontend/icons.js`（chevronDown 图标）与 `frontend/index.html`（filter/面包屑/表头）未提交改动；经审阅与本卡方案一致，予以保留复用（chevronDown 后按实现取舍移除，改用既有 chevron + CSS 旋转），遗留改动未导致任何验收步骤被跳过。

## ① 做了什么 / 接口消费方式

**比对明细 tab 的变化表由 /api/diff 的 Top25 折叠行改为消费 ISS-147 `GET /api/diff/children`（真实 147 HTTP）**：

- 默认聚焦数据集根（`a.root`），行 = 同级目录在 a→b 区间的差分；行状态直接映射接口口径 `measured / first_recorded / unrecorded / structural`，单侧缺测与结构节点渲染 `—`，绝不渲染 0。
- **目录名与展开箭头动作分开**：点目录名打开目录详情（证据），点箭头逐层展开同级（按路径懒加载，`has_children` 才出箭头）；展开/收起 `aria-expanded` 表达。
- **聚焦下钻**：行操作「聚焦此目录」+ 详情内「聚焦此目录」→ 树表切到该目录的同级视图，面包屑（根→…→当前）自然下钻；Esc 先关详情、再逐级返回上级聚焦；单一树表布局，无双布局主开关。
- **筛选/排序走接口参数**：filter=all/changed（`#changes-filter`）、sort=delta/size/name（表头），不在本地重建 Top25；filter=changed 会隐藏无变化方向，`#tree-note` 如实标注「已隐藏…切换回全部同级行可查看」；搜索只过滤已加载行，同样在说明行标注。
- **分页显式呈现**：`has_more/next_cursor` 渲染为「加载更多（已显示 X / 共 Y 个同级行）」，不冒充完整清单；游标绑定五元由后端 400 兜底。
- **本级目录摘要**（`#tree-parent`）：聚焦非根时展示该目录自身区间值（累计值含全部后代、不可与子行相加）；双侧无直接记录时如实说明「结构导航节点，大小未知不填 0」。
- **目录详情抽为共用组件 `frontend/modules/directory-detail.js`**：区间证据块绑定当前 a/b（「区间 #a → #b」+ 之前/现在/净变化/状态）；历史趋势与当前所在标注「最新快照口径」并单列「最新记录」时间；所选 a/b 非最新数据集时 `/api/browse` 路径越界 400 按真实语义如实说明（「本区绑定最新快照，不代表所选区间」）。区间变更/离页 `invalidate()`：关闭详情 + 作废在途 trend/browse，迟到响应不得复活旧详情（请求域世代号 + `a|b|path` 键守卫双保险）。
- **竞态**：树内每层级请求闭包绑定创建时的 `treeState` 实例 + `treeEpoch`，改选 a/b、切筛选、切排序即整体作废（迟到旧层级响应不混入新视图）；`loadDiff` 在根层同级行就绪后才置完成态（状态文案出现即行可见）。
- **AI 解读区只保不破**：预览/授权/过期七态逻辑零改动；证据定位 `locateEvidenceInTable` 改走 `revealTreePath`——按路径（`data-path`，非行索引）定位，行不在当前聚焦层时复位筛选、下钻目标父层后再定位，仍不可见如实说明。增长/缩减/新出现/消失排行分区与历史日报保持次级可达（仍由 /api/diff 与 /api/reports 驱动）。
- 图标零新增 SVG 路径（复用 chevron + CSS 旋转；scope 作聚焦图标）；用户可见处零 emoji；色采沿用中性/蓝青基调（first_recorded 用 --mineral、structural 用 --border 中性点，非警示色）。

## ② 验证与真实结果（命令 + 退出码 + 关键断言与截图）

隔离方法按 docs/TESTING.md：临时运行根 + 合成快照直连隔离库（7 个数据集、15 个快照：chain 父+20/子+18/孙+2/曾孙、netzero 父 0 子对冲、hist 三快照旧区间、gap 缺父结构、oneside 单侧未记录/首录、pages 130 兄弟分页、weird HTML/引号/换行/中文目录名）；`FATHOM_RUNTIME_DIR/FATHOM_DB/FATHOM_SCAN_ROOT/FATHOM_PORT` 全隔离；起服务前断言 `/health` pid == 自 spawn 进程且 `runtime_mode=development`（ISS-035B 合同）。测试后自起进程 SIGTERM 回收并断言端口释放。

| 命令 | 退出码 | 结果 |
|---|---|---|
| `node scripts/verify_tree_changes_frontend.cjs`（本卡新增） | 0 | **53/53 PASS** |
| `node scripts/verify_frontend_refresh.cjs` | 0 | **217/217 PASS** |
| `node scripts/verify_analysis_frontend.cjs` | 0 | **77/77 PASS** |
| `node scripts/verify_api_security.cjs` | 0 | **39/39 PASS** |
| `.venv/bin/python -m pytest tests/ -q -k "refresh or changes"` | 0 | 15 passed |
| `.venv/bin/python -m pytest tests/ -q`（全量） | 0 | **1147 passed**（与 ci.yml `EXPECTED_PYTEST_PASSED=1147` 精确一致） |

本卡脚本 53 项的关键断言（生产页面 Playwright 实点，非 formatter 模拟）：

- 默认载入即树表（weird 数据集）：5 行渲染；`<img src=x onerror=alert(1)>`/换行/引号/中文目录名全程纯文本，无 img[onerror]/svg[onload] 注入、无脚本执行、零 dialog。
- 三层展开实点：child@0 → grand@1 → great@2（层级缩进、aria-expanded、收起隐藏子树、键盘聚焦箭头 Enter 展开）。
- 数值口径：child +17.6 MB、other 0.0 B（净 0 渲染值而非 —）、netzero 父 0.0 B 且展开 up +19.5 MB / down −19.5 MB、filter=changed 保留净 0 但有变化后代的父行。
- 结构节点：gap/deep 之前/现在/净变化全 `—` + 「结构节点」徽章，展开后叶子 +100.0 KB 实测。
- 单侧缺测：未记录 `—（曾有 4.9 MB）`、首次记录 `—（现有 6.8 MB）`，徽章「未记录/首次记录」。
- 分页：首页 100 行 + 「加载更多（已显示 100 / 共 130）」，点击后 130 行无重复、按钮消失。
- 详情绑定：区间块 `#1 → #2` 与 80.1→97.7 MB；hist（#5→#6）详情仍显 5.9→6.8 MB 而非最新 s3；趋势区「最新记录：2026-09-07 12:00」另标；browse 区如实说明绑定最新快照。
- 错误与重试：children 中断 → diff 完成态不受影响、表内错误行 + 重试按钮，重试后行恢复。
- 竞态：注入 2s 延迟的 pages 根层响应在改选 chain 后被丢弃（无 c0xx 行混入）；打开详情后改选区间，详情立即失效且不被迟到 trend/browse 复活。
- 焦点管理：详情 Esc 后焦点回触发行（`activeElement.dataset.path` 断言）；Esc 逐级返回、根上 Esc 无动作；面包屑点击往返。
- 三视口 980×640 / 1220×820 / 1440×900：三层展开 + 详情打开（最重状态）下无页面横向溢出，详情可见、宽度 ≤ 视口、关闭按钮可见（截图在案）。
- 次级可达：AI 解读区「未启用」态照常；排行 tab 图表+同源表渲染；历史日报区在位。
- 代码级零 console.error / 零 pageerror（Chromium 对预期内 4xx 的网络资源日志单独归类，其产品行为已由显式断言覆盖：跨数据集瞬时 400 后自动恢复、browse 400 的口径说明）。

截图（前后同场景 + 三视口，合成数据）：
`verify-results/iss148-tree/shots/`（worktree 内，gitignore）
- `chain-root-before-expand.png` / `chain-expanded-three-levels.png`（展开前后同场景）
- `chain-detail-open.png`、`chain-focused-child.png`（面包屑 + 本级摘要）
- `viewport-980x640.png`、`viewport-1220x820.png`、`viewport-1440x900.png`

## ③ NOT_VERIFIED

- **Tauri WebView 实机**：本卡全部验证在真实 Chromium + 真实 FastAPI 上完成；Tauri 壳内 WebView、原生手势/触控板滚动、真实 Finder 动作未验证（沿 ISS-114 边界，属父卡 144/146 与原生交互 ISS-161 范围）。
- **更大生产库的根层首屏耗时**：本卡种子最大 130 兄弟；10 万条目级性能已在 ISS-147 采样（同级量级），但本卡未重复采样。
- **旧 `changes-search` 的产品语义**（按路径过滤当前结果 → 只过滤已加载行）：行为已实现并在 tree-note 标注，但「用户长按搜索深路径」的实机手感未做人工走查。
- **CI 云端实跑**：本地全绿；push/PR/云端 job 由 PM 收口，未验证。

## ④ 需 PM 回写的建议

1. **CI 接线（建议）**：本卡新增独立回归脚本 `scripts/verify_tree_changes_frontend.cjs`（53 项，真实 API 口径，与 39 项 security 夹具/217 项 refresh 合成夹具互补）。如需纳入 CI 门禁，建议在 `ci_browser_checks.sh` 追加一段并新增 `EXPECTED_TREE_PASSED: "53"`（本卡未改任何 EXPECTED_* —— 39/217/77 三套既有套件的断言数量均未变化，无需同步）。
2. **夹具镜像端点说明（越权披露）**：为使既有套件在生产前端消费新端点后保持合同有效，本卡在 `verify_frontend_refresh.cjs` 与 `verify_analysis_frontend.cjs` 的**合成 API 夹具内**新增了 `/api/diff/children` 镜像处理器（从各自既有 diff 行数据按路径段推导层级），未改任何既有断言与计数；两套件 217/77 全数原样通过。这是「合成夹具须镜像生产 API 面」惯例的必要接线，请 PM 复核认可。
3. **TASKS/文档回写**：变化页交互合同（箭头展开/名字详情/聚焦面包屑/changed 隐藏说明）建议按本报告 ① 更新 DESIGN 变化页节；`docs/TESTING.md` 的浏览器回归命令清单如纳入新脚本请同步。
4. **verify-results 归档**：`verify-results/iss148-tree/`（结果 JSON + 截图）在 worktree 本地（gitignore），如需入库归档请由 PM 决定落点（参照 iss147 证据目录惯例）。

## 改动文件清单

| 文件 | 性质 |
|---|---|
| `frontend/modules/pages/changes.js` | 改：比对明细改树形同级表（消费 /api/diff/children）、竞态/筛选/排序/分页/Esc 层级；详情与解读定位接线 |
| `frontend/modules/directory-detail.js` | 新：目录详情共用组件（区间/最新双口径、键守卫、动作注入） |
| `frontend/index.html` | 改：changes-filter、tree-crumbs、tree-parent、tree-note、表头（name/size/delta）、说明文案 |
| `frontend/style.css` | 增：树表缩进/箭头/状态徽章/加载更多/面包屑/本级摘要样式 |
| `frontend/icons.js` | 注释：chevron 复用说明（净变化仅注释行，无新 SVG） |
| `scripts/verify_tree_changes_frontend.cjs` | 新：53 项真实 API 浏览器回归（含种子、身份断言、进程回收） |
| `scripts/verify_frontend_refresh.cjs` | 夹具增 `/api/diff/children` 镜像（无断言变更） |
| `scripts/verify_analysis_frontend.cjs` | 夹具增 `/api/diff/children` 镜像（无断言变更） |

**计数同步明细**：未改动任何 `EXPECTED_*`——`EXPECTED_BROWSER_PASSED=39`、`EXPECTED_REFRESH_PASSED=217`、`EXPECTED_ANALYSIS_PASSED=77` 三套件断言数量与内容均未变化（本地实测 39/217/77 全绿）；`EXPECTED_PYTEST_PASSED=1147` 未动（本卡未增 pytest 用例，全量 1147 实测一致）。
