# ISS-151 交付报告：目录内文件排查与 Finder 衔接（第三次接手，前两次无交付）

基线：main `8cf698e`（worktree 全新复位，本次从零实施）。分支 `iss-151-directory-file-inspection`，未 push（由 PM 代办）。

## 一、改动摘要

产品合同落地：从所选历史目录主动「查看当前大文件」，默认 largest、可切 recent；目录详情与独立大文件页共用范围/模式/状态；历史 a→b 与当前 st_size/查询发起时间同屏可辨；取消/过期/失败/无匹配/权限/截断可辨；查询仅明确点击发起（进页、开详情零遍历）；路径失效保留历史并说明无法定位；Finder 仅定位所选当前路径（走既有 reveal 守卫，不读内容）；离开查询面/改范围按 ISS-164 真实句柄取消并以 status 轮询收敛，浏览器中止不冒充服务端取消。

| 文件 | 改动 |
|---|---|
| `frontend/modules/pages/bigfiles.js` | 重写为共享查询域单例 + 页面宿主：`startBigfilesQuery`（wait=false 拿句柄→status 轮询→终态取体）、`cancelActiveBigfiles`（句柄取消+收敛轮询）、`attachBigfiles/detachBigfiles`（挂载计数，双面都不可见才取消；宏任务延迟避免换页误杀）、`setScope` 改范围静默取消旧任务、旧形态 200 直出兼容、六态/取消/越界 400/404 渲染。页面新增范围(path)/模式(mode) 控件消费 |
| `frontend/modules/directory-detail.js` | 详情新增「当前大文件」区：模式切换 + 查看按钮 + 结果表；scope 命中本目录时呈现共享状态（含已取消/404），未命中呈现待查询提示；打开详情 attach、关闭 detach；历史区间读数（区间/之前/现在/净变化/趋势）不受任何影响 |
| `frontend/modules/pages/changes.js` | 树视图跨页保留：`sessionTree`（树实例含 levels/expanded/focus + 滚动/焦点行/打开的详情），离页快照、返回且 a/b 未变时整树恢复不重发 diff/层级请求（在途层重取、焦点 preventScroll、滚动最后落定）；改选/清空即作废 |
| `frontend/index.html` | 大文件页补 150 前端缺失的 mode/path 控件与口径说明行；h2「近期大文件」→「大文件」 |
| `frontend/style.css` | `.detail-bf-bar` 控件条（沿用 .controls 语言） |
| `scripts/verify_frontend_refresh.cjs` | 最小适配：7 处大文件场景入口在 openPage 后补 `click("#btn-bigfiles")`（旧行为进页自动查询，与新合同冲突）；**全部 record 断言原文未动，计数保持 217** |
| `scripts/verify_directory_bigfiles_frontend.cjs` | 新增独立回归套件（36 项，真实 FastAPI + 受控文件树 + 单实例 Chromium 串行），详见下节 |

后端 `fathom/`、schema、README/CHANGELOG、pytest 计数：零改动（符合卡片边界）。

## 二、验证（真实入口，全部实际执行）

环境：worktree `.venv/bin/python`（`fathom.__file__` 指向本 worktree 已核，种子带 FATHOM_RUNTIME_DIR 前缀 fail-closed 断言）；playwright@1.61.1 + chromium 安装于 worktree `.runtime/playwright`（TESTING.md 口径）；隔离 runtime/scanroot/空闲端口；serve 起后断言 `/health` pid == 自 spawn 进程且 `runtime_mode=development`；结束杀进程并复核端口释放。

| 命令 | 退出码 | 结果 |
|---|---|---|
| `FATHOM_PYTHON=.venv/bin/python NODE_PATH=.runtime/playwright/node_modules node scripts/verify_directory_bigfiles_frontend.cjs` | 0 | **36/36 通过**（ok=true failed=0） |
| `FATHOM_PYTHON=… NODE_PATH=… node scripts/verify_tree_changes_frontend.cjs` | 0 | **74/74 通过**（树形/详情既有回归不破） |
| `NODE_PATH=… node scripts/verify_frontend_refresh.cjs` | 0 | **217/217 通过**（断言零改动，仅入口补点击） |

36 项对任务卡验收的覆盖（`/tmp/iss151-run7.json` 留档，截图在 worktree `verify-results/iss151-dir-bigfiles/shots/`）：

1. **三层历史定位→当前 largest→recent 切换**：vault #3→#4 展开 media→archives→old 打开详情（区间读数 19.5/39.1MB 为历史快照口径）；largest 命中 3 个 300MB、mtime 400 天前未改大文件并标注「当前最大/查询发起于/st_size 实测/非 #3→#4 历史区间」；切 recent 同目录无匹配可辨（「近 7 天没有 ≥ 100MB 的文件修改」）。
2. **返回历史 a/b、展开/滚动/焦点保持**：离页→大文件页→返回，a/b=#3/#4、展开深度=2、scrollTop 恢复、焦点回触发行、打开的详情重开，且大文件结果从共享域原样恢复（零新请求）；in-flight 层重取路径同套件覆盖。
3. **明确点击才启动、范围正确、双目录并发取消不误伤**：进页/开详情 1s 零 bigfiles 请求断言；路径范围只出该目录文件；A(慢目录 9 万文件) 在途立即改查 B(many)——status 轮询证实 A=cancelled、B=truncated ok，取消 POST 计数=1（按句柄，无误伤）；手动取消按钮 UI「已取消」+服务端收敛。
4. **移动/权限/空/失败/缓存过期/截断**：目录移走后再查→404「无法定位+历史快照读数不受影响」且区间/趋势画布保留；chmod 目录→permission_denied；空目录→no_match；失败/缓存过期两态由 refresh 合成夹具原断言覆盖（真实 find 难以构造，见 NOT_VERIFIED）；210 个 ≥100MB 命中 topn=200→「结果被截断，已截断到 top 200」且恰 200 行。
5. **路径转义、键盘、三尺寸**：中文+空格目录字面渲染、reveal 请求体精确（页面内经路由拦截，不触真实 Finder）；树行 Enter/Esc/焦点返回；980/1220/1440 无横向溢出、详情大文件区可见。
6. **取消边界（合同核心）**：`boundary-browser-abort-does-not-cancel-server-find`——node 层提交 wait=false 后客户端中止 wait=true，终态=no_match（≠cancelled），证明取消只能来自显式 POST；`leave-page-cancels-task-via-handle`——离开页面→cancel POST 可数→status 收敛=cancelled。
7. **Finder 边界（原始 HTTP）**：reveal 越界路径 400、前缀同名根 400、不存在路径 404（真实 API，无系统副作用）。

过程记录：独立套件第 1-6 轮修复的均为脚本自身竞态（展开切换语义、导航夺焦、tbody 重画致句柄失连、慢目录被前序取消焐热后 find 抢跑——顺序调整为离开取消吃冷缓存并加大种子至 9 万文件）；最后一次全绿运行即上表结果，期间产品代码零回归改动。

**环境事件（已处置）**：refresh 首跑在后期 `GET /__fixture` ECONNRESET——根因为磁盘 98% 满 + 本机遗留 28 个我历次运行的临时目录（约 250 万文件）+ 一个 17h 前孤儿 `fathom serve`（PID 98744，cwd=iss-149 旧 worktree、PPID 1、development 模式，推断为前次会话遗留）。已删除 27 个临时目录（保留最新绿跑一份供复查）、回收该孤儿 serve 后重跑即绿。另请 PM 知悉：本机磁盘可用仅 40Gi。

## 三、NOT_VERIFIED / 未覆盖

- 真实 Finder 系统动作（open -R 实际定位）：归 ISS-161，本卡仅边界请求。
- 真实后端 `failed` / `expired` 终态：本机 find 难以构造非权限失败；expired 自 ISS-032 起管理器对过期缓存即弃即重跑、不再产出该终态——两态 UI 标签由 refresh 合成夹具断言覆盖（`bigfiles-failed-state-shown` / `bigfiles-expired-state-shown`）。
- Tauri 壳 / WKWebView 实机、三桌面尺寸的原生窗口行为：按既有归属另行实测（本套件覆盖浏览器三视口）。
- pytest 全量未跑：tests/ 零改动、计数零变化，前端 JS 不在 pytest 范围。
- 新套件未接入 CI：按卡片约定不自行改 `ci_browser_checks.sh`/`ci.yml`（见下）。

## 四、计数明细与移交建议

- pytest：1219 **不变**（未新增 python 测试，tests/ 未动）。
- API/浏览器 39、前端 refresh **217 不变**（断言零改动）、AI 解读 77 不变、树形 **74 不变**（未给既有套件加断言，故未动 `EXPECTED_TREE_PASSED`）。
- **新增独立套件 36 项**，建议按树形套件同一门禁口径接入：`ci_browser_checks.sh` 与 `.github/workflows/ci.yml` 增设 `EXPECTED_DIR_BIGFILES_PASSED=36`（`FATHOM_PYTHON`/`NODE_PATH` 注入同 EXPECTED_TREE 段），由 PM 落计数 commit。
- commit：单 commit（无计数同步文件改动，无需第二 commit）。未 push。
- 备注：worktree `.runtime/playwright`（gitignore 内）为新装回归依赖；根 RESULT.md 本文件不入库。
