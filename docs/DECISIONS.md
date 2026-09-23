# Fathom 决策记录

格式参照 Folia 项目：DEC-XXX 编号，新决策在前。每条含背景/决策/验证/影响。
（0.2.0 及以前项目名为 disk-sentinel/容量哨兵，DEC-010 起更名 Fathom，历史条目保留原名。）

---

### [DEC-024] - 2026-09-23 - Actions 额度恢复与首轮 GitHub Actions 发版范围：arm64 未签名 draft；开源声明按 Folia 基线收口

**背景**：ISS-040C 合并（#152）与 Wave40 收口（#153）后代码队列无 READY。用户 2026-09-23 晚间直接指令：①确认 GitHub Actions 额度已恢复，要求尽快推进 GitHub Actions 发版；②license/开源声明部分「直接参照 Folia 项目」。参照对象为同开发者公开仓库 cat-xierluo/Folia：仅单个 Apache-2.0 LICENSE（Fathom 经 DEC-020 已同款落地），无逐项第三方声明文件，以未签名 DMG 公开分发（截至 v0.8.1）。

**决策**：①按 DEC-021 预留恢复命令经 API 将 `CI` workflow（id 356941919）重新启用；②ISS-037 验收框 2 口径修订为「无已知事实错误」：许可证值等须与本地包元数据一致，UNKNOWN 项保留说明、不作为阻断项（超出默认修复预算的第 3 轮经用户明确授权重派，续用 repair2 分支成果）；③ISS-041 拆出首轮切片 ISS-041A：仅 arm64、未签名（DEC-022 延续）、tag 触发、draft Release（DMG+checksums.txt）；updater 产物/latest.json、x86_64 双架构、publish 与仓库转公开仍留父卡及人工门——生产 updater keypair（G10）未决策前 workflow 不得引用任何 TAURI_SIGNING_* secret。转公开时机仍由用户另行决定，本决策不改变该人工门。

**验证**：enable 执行后 API 返回 `state=active`（2026-09-23 实测）；额度恢复以恢复后首个真实 CI run（本决策的登记 PR 触发）为准，若在步骤前被 billing 拒绝仍按 DEC-018 记 `NOT_RUN` 并修正本条表述；Folia 基线以该仓库实地核对（LICENSE 单文件、无 NOTICE/THIRD_PARTY 文件、README 一行许可声明、Release 公开未签名分发）。

**影响**：ISS-037 转 IN_PROGRESS（repair3 在途）；新增 ISS-041A 并派发（在途）；README/TESTING 的「Actions 停用」表述更新；ISS-041/030/033 父卡验收矩阵不变，切片 DONE 不关闭父卡。

---

### [DEC-023] - 2026-09-19 - Fathom 品牌双形态体系：层叠深潭为 App 图标，深度环用于界面小尺寸（当日晚间修订）

**背景**：ISS-045 自项目早期就是 Logo 人工门（仓库外 A/B/C 概念板仅供选择，PM 曾推荐「A 深度环骨架＋B 一层轻微不规则等深线」混合）。用户 2026-09-18 评审产品 UI 选定 R3 测深视觉签名方向（ISS-072 已把海沟蓝/深度环/等深线落进界面），2026-09-19 指令「这个 logo 也是要合并到主分支，然后运用这个 logo 去编译软件的」确认深度环为正式 Logo，人工门关闭。

**决策**（同日晚间由用户在 Logo 设计会话的选择修订为双形态体系）：**App 图标 = 「层叠深潭」**——用户选中的第二轮图案（原稿 `assets/brand/fathom-approved-concept.png`，image_gen 生成，来源与权利说明见同目录 README），象牙白 squircle 底板 + 蓝青四层嵌套等深阶地 + 午夜蓝中心 + 顶部测深刻痕；由 `scripts/build_app_icon.py` 本地 Pillow 抠图生成 1024 源（不调用生成模型、可重复）；底板按 Apple macOS 11+ 图标网格缩放为 824/1024（80.5%）居中、四周透明——用户 2026-09-19 反馈满幅偏大，实测对齐同开发者 Folia 图标的 80.3% 占比。**界面小尺寸 = 「深度环」**——菜单栏 tray（22pt 灰度 template）、侧栏字标、favicon 保留线条深度环（海沟蓝 #345d7f 底 + 白色开放环右上 50° 缺口 + 中心探针 + 亮矿物青 #82c8c2 刻度），几何声明在 `apps/desktop/src-tauri/icons/icon.svg`，四处几何（icons.js/icon.svg/favicon.svg/make_tray_icon.py）由 `ci_brand_geometry.sh` 门禁保证同步。明确取舍：①深潭不做 tray/小尺寸形态——四层结构在 22pt 灰度下不可辨；②深潭 16/32px 退化为色块轮廓属已知取舍（64px 起结构完整）；③深潭深蓝中心保持不透明（用户要求）；④深潭未交付透明裸核心（当前无使用场景）。深度环曾是同日清晨的 App 图标形态（PR #116），当日被深潭替换，其 tray/字标/favicon 交付继续有效。深潭原稿为 image_gen 生成物，权利与许可不作超出已知事实的保证（assets/brand/README.md）；深度环为自绘几何，随仓库 Apache-2.0 发布（DEC-020）。

**验证**：2026-09-19 实测——深潭形态：DMG 安装窗口 Finder 图标、Dock、Launchpad/Spotlight 检索（GUI 自动化）四入口渲染正确，全链编译 + `verify_app_bundle.sh` 22/22；深度环形态：深色/浅色模式（暗壁纸）菜单栏 tray 均白色 template 渲染（像素级），前端 86/86 与浏览器 39/39 在 favicon 接线后无回归。`NOT_VERIFIED`：透明裸核心；18pt 小菜单栏（ISS-009 人工门）。证据见 TASKS ISS-045 卡与 `verify-results/iss045-live/`（深度环）、`verify-results/iss045-basin-live/`（深潭，gitignore）。

**影响**：ISS-045 关闭；ISS-009/037 的图标依赖可直接消费（打包链已用新 icns 编译）。将来若调整深潭 App 图标，须改 `assets/brand/` 原稿链并重跑 `build_app_icon.py --force` 与 `build_icons.sh`；若调整深度环几何，须同步四处并重跑 `make_tray_icon.py`；不得只改位图。

---

### [DEC-022] - 2026-09-18 - v0.3.0 不做 Developer ID 签名与公证，改为 DMG 安装界面提示首次打开放行方法

**背景**：ROADMAP M2 原门槛含「Developer ID 签名、公证与 draft Release 齐备」（ISS-041）。用户 2026-09-18 明确：签名/公证先不管——同开发者的 Folia 即以未签名方式分发；直接在安装页面做好提示即可。技术事实：未签名且未公证的 app 被他人从下载渠道获取时，首次双击会被 Gatekeeper 阻止（「无法验证开发者」），需要右键打开或系统设置放行；提示必须在用户首次双击 app **之前**触达，DMG 安装窗口（打开 DMG 后、拖拽安装前即呈现）是正确载体。

**决策**：v0.3.0 分发产物保持未签名、未公证；DMG 安装窗口背景图承载安装引导与「首次打开提示」（未签名说明 + 右键「打开」/系统设置 → 隐私与安全性 →「仍要打开」两步放行指引）。落地为 `scripts/build_dmg_background.py`（Pillow 生成，产物 `icons/dmg-background.png` 入库，日常构建不依赖 Pillow）、`tauri.conf.json` 的 `bundle.macOS.dmg`（background/windowSize/appPosition/applicationFolderPosition）与 `verify_app_bundle.sh` (j) 段（DMG 内背景图 + app + Applications 链接在位断言）。ISS-041 的签名/公证部分**延后至另行决策**（其双架构与 Release CI 部分不受本条影响）；将来正式公开发布是否恢复签名公证，届时修订本条。

**验证**：2026-09-18 实测——重建 DMG 后 `hdiutil attach` 确认 `.background/dmg-background.png`（约 41KB）与 `Fathom.app`、`Applications` 符号链接在位；Finder 打开挂载卷截图目检：绿色箭头从 app 图标准确指向 Applications、全部中文文案完整无乱码、背景与窗口尺寸匹配；verify 全量含 (j) 段通过（见 TASKS ISS-009 卡记录）。

**影响**：内测接收方首次打开会按预期被 Gatekeeper 拦截一次，需按背景指引放行——这是明示的产品行为而非缺陷；`x86_64` 双架构、应用内更新的签名防绕过等 ISS-040/041 其余范围不变；ROADMAP M2 门槛描述已随本条加注。未签名包不通过公证渠道时的信任边界（来源可校验性仅靠 checksums.txt）由 README 分发说明明示。

---

### [DEC-021] - 2026-09-15 - 账户 Actions 额度耗尽期间停用 CI workflow，以本地同口径门禁为主

**背景**：DEC-018 生效后，`CI` workflow 仍对每个 pull_request 与每次 push main 触发 5 个 macOS runner job（私有仓 macOS 分钟按 10 倍计费），自 2026-09-14T16:58Z 起全部在执行任何步骤前被 billing 拒绝（check-run annotation "The job was not started because recent account payments have failed or your spending limit needs to be increased"），累计 108 个 run 无一执行，另有 1 个 run 长期卡在 queued。用户 2026-09-15 确认账户额度已用光，要求能本地跑的 CI 就本地跑、收回 Actions 消耗。

**决策**：经 REST API 把 workflow `CI`（id `356941919`，`.github/workflows/ci.yml`）置为 `disabled_manually`，**不修改 ci.yml**；此后 push/PR 不再创建 run，也不会在计费恢复的瞬间自动开跑。恢复只需一步：`gh api -X PUT repos/cat-xierluo/fathom/actions/workflows/356941919/enable`，须由用户在额度恢复后确认执行。停用期间合并门禁仍按 DEC-018 四项组合执行，其中"本地全量检查"以 CI 同款 fail-closed 脚本为准（见 [TESTING §1.1](TESTING.md#11-actions-无额度期间的临时本地合并门禁)）：`scripts/ci_pytest.sh`（断言 338）、`scripts/ci_browser_checks.sh`（断言 39）、`scripts/ci_cargo_locked.sh`（arm64 锁定离线构建），并新增 x86_64 交叉检查：`RUSTC="$HOME/.rustup/toolchains/stable-aarch64-apple-darwin/bin/rustc" rustup run stable cargo check --target x86_64-apple-darwin --locked --offline --manifest-path apps/desktop/src-tauri/Cargo.toml`（本机 rustup `stable` 恰为 CI 钉定的 Rust 1.88.0；必须显式 `RUSTC`，否则 rustup 的 cargo 会按 PATH 拿到 Homebrew rustc 1.98 而报 `E0463 can't find crate for std`）。

**验证**：2026-09-15 于 main `73e0798` 本地复跑全部通过：`ci_pytest.sh` 338 passed；`ci_browser_checks.sh` 39 passed；`ci_cargo_locked.sh` ok（PATH 上为 Homebrew cargo 1.98）；rustup 1.88 arm64 `cargo build --locked --offline` ok；x86_64 交叉 `cargo check --locked --offline` ok（仅 2 个既有 dead_code warning）。`gh api repos/cat-xierluo/fathom/actions/workflows/356941919` 返回 `state=disabled_manually`。停用后 PR #70/#71 与对应 push 均未再创建 run。

**运行记录清理（2026-09-15 用户指令"删除不必要的 GitHub Actions"）**：108 条 run 中，经 REST 逐条 `DELETE /actions/runs/{id}` 删除 103 条计费拒启的 failure 记录；**保留 4 条 2026-09-13 真实执行成功的 run**（`ci: add reproducible macOS test matrix`（含 #16）与 `spike(runtime): validate self-contained helper` 各 2 条——ISS-031/ISS-029 卡片引用的"双架构 CI 五项通过"云端证据）；1 条 2026-09-13 的 run（34749027254，`ci: update exact pytest gate to 168 (#23)`）在 GitHub 侧处于自相矛盾状态（run 显示 queued、cancel 报"已完成"、force-cancel 报"尚未排队"、delete 返回 403），API 无法移除，workflow 已停用故其永不会启动，记为已知僵死项。Actions 缓存与 artifact 均为 0，无需清理。

**影响/重评条件**：本地覆盖 CI 5 个 job 中的 4 个。**x86_64 pytest 无法本地覆盖**（本机无 x86_64 Python），x86_64 cargo 为交叉 `check` 而非原生 runner 上的 `build`——两者继续按 DEC-018 记 `NOT_RUN`，不得写成通过。本地 PATH 的 Homebrew Rust 1.98 与 CI 钉定 1.88 不一致：需要与 CI 同版本证据时走 `rustup run stable` 路径。额度恢复后先 enable、让一次完整云端 run 转绿，再撤回本决定的"停用"部分；DEC-018 其余条款不变。

---

### [DEC-020] - 2026-09-14 - 项目许可证采用 Apache-2.0（与 Folia 一致）

**背景**：ISS-037 交付了许可证选项对比（MIT / Apache-2.0 / MPL-2.0 / AGPL-3.0）并推荐 Apache-2.0，但按合同不落 LICENSE，等用户选择。依赖侧 PyInstaller 为 GPLv2 + 允许冻结产物以任意许可分发的例外条款、Tauri/tao 系 crate 与 ECharts 为 Apache-2.0/MIT，均与 Apache-2.0 兼容。

**决策**：用户 2026-09-14 指示“和 Folia 一样”，采用 Apache License 2.0。LICENSE 正文与 Folia 仓库逐字一致，版权行按 Fathom 最早提交年份写 `Copyright 2026 maoking`；Cargo package 声明 `license = "Apache-2.0"`；README、THIRD_PARTY_NOTICES 与许可证方案文档联动。授予许可证不改变仓库 private 状态与公开发布时机（仍由用户决定）。

**验证**：`diff` 证明 LICENSE 除版权行外与 Folia 一致；`bash scripts/check_version_consistency.sh` 不受影响；THIRD_PARTY_NOTICES 已注明项目自身许可。

**影响/重评条件**：ISS-041 打包须把 LICENSE 与 THIRD_PARTY_NOTICES 纳入 .app/.dmg Resources；若未来引入 GPL 强传染依赖或需双许可，重开决策。ISS-037 验收框“用户选定许可证后才落入 LICENSE”据此满足。

---

### [DEC-019] - 2026-09-13 - PM 交接以仓库任务源和唯一 owner 为准

**背景**：用户改由其他 Agent 担任 PM。旧自动巡检依赖同一对话和私有 orchestration 状态；若新旧 PM 同时派发或合并，会重复实现、争用 worktree，并让任务状态失去唯一来源。

**决策**：新 PM 必须按 AGENTS → TASKS 完整卡片 → 对应 ARCHITECTURE/DESIGN/TESTING/发行方案的顺序接手，再核对 `origin/main`、开放 PR 与所有 worktree。TASKS 是唯一队列；旧对话和 Git common dir 的私有 `orchestration/` 不是接手依赖。旧 `fathom-m0-pm` 心跳保持 PAUSED；恢复自动化须由用户明确要求或新 PM 建立唯一 owner，禁止双 PM。不得重做已合并任务、PR #17 的 ISS-029 spike 或 PR #10 已验收的 R3 工程。

**验证**：ISS-020/044/046、PR #10 head、READY/WAITING/BLOCKED 依赖、179 项门禁与发行 `NOT_VERIFIED` 已回写到仓库权威文档；ISS-038 以文档索引/卡片、依赖、链接与资源终态核对收口。

**影响/重评条件**：自动推进授权范围仍以 TASKS 为准，但暂停状态不会因旧授权自动恢复。新 PM 若恢复编排，应创建可审计的新运行记录并先确认没有其他 owner；公开发布、许可证、两个视觉门和 Apple 材料仍由用户决定。

---

### [DEC-018] - 2026-09-13 - GitHub Actions 无额度期间采用固定候选本地门禁

**背景**：PR #18 的 GitHub Actions 五个 job 均在任何步骤执行前被账户 billing/spending limit 拒绝，不能提供云端验证证据。用户确认当前 Actions 无额度，并明确授权本轮及额度恢复前在本机核验后合并，避免普通开发工作停滞。

**决策**：临时以四项组合门禁替代普通云端 CI：候选必须基于最新 main 并固定 40 位 head；在该候选运行任务要求的本地全量测试和真实入口探针；由不同 worker 对同一 fixed head 独立审查；PM 合并前再次核对 exact head、diff 和证据。云端 job 若在步骤前被 billing 拒绝，一律记录为 `NOT_RUN` 及原因，不能写成失败测试或通过。本决定不替代发行矩阵中的原生 x86_64 冻结、Tauri GUI、隔离账户安装、Developer ID 签名、Apple 公证/stapling 和真实更新验证。

**验证**：PR #18 在本地完成文档链接、diff-check 与 doc-curator 后合并为 `fc1cf91`；PR #19/#20/#21/#25/#26 均取得独立 fixed-head ACCEPT，PM 核对 exact head，并以本地全量测试、Chromium/API、真实 BSD `du`/SQLite 或跨进程扫描探针后合并。对应云端 job 均未进入执行步骤，状态保留为 `NOT_RUN`。

**影响/重评条件**：额度恢复后重新启用普通 GitHub CI，并继续保留本地可复跑入口。任何缺少固定 head、独立 reviewer、真实入口或 exact-head 核对的候选不得引用本决定直接合并；涉及 release、x86_64、签名或公证时仍按 [TESTING 发行矩阵](TESTING.md#4-桌面与分发矩阵) 执行。

---

### [DEC-017] - 2026-09-13 - v0.3 helper 采用 PyInstaller onedir 与应用派生进程合同

**背景**：当前 Tauri 壳依赖开发机上的 FastAPI 服务，无法随 `.app` 独立安装。ISS-029 比较了冻结方式，并在 arm64 上复现字符串导入、bundle 写入、缺少身份面和固定 7952 端口冲突；同时验证了 discovery、端口让位、并发所有权与受控退出合同。

**决策**：v0.3 的 Python helper 使用固定版本 PyInstaller onedir，并在 arm64 与 x86_64 原生 runner 分别冻结。helper 必须提供 `--version` 与 `/health` 身份面；只绑定回环地址，在一个小端口段内让位；用 Application Support 数据根内的全生命周期文件锁保证单一 owner，以权限 `0600` 的原子 discovery 记录 port、pid、instance_id 与随机控制令牌。未知端口占用者永不终止，关闭和清理前必须再次核对进程及 discovery 身份。首个可分发闭环由 Tauri 应用派生 helper；登录项与 UI 退出后常驻语义留给 ISS-010 实测后重评。

**验证**：ISS-029 合同原型 18/18 通过，包括 8 进程同步竞争仅一个 owner、畸形记录接管、替换记录保留、token 不出日志、未知占用 PID 前后不变及只读资源指纹。PyInstaller 6.22.3 onedir A/B 均构建为 arm64 Mach-O。生产 helper 的冻结健康、Host 守卫和 SIGTERM 因 7952 已被未知进程占用而保持 `NOT_VERIFIED`；x86_64 冻结、干净账户、TCC、签名和公证也尚未验证。

**影响/重评条件**：ISS-009 实装前必须关闭 G1–G6：显式导入、Application Support 数据根、统一版本、身份健康端点和动态端口；ISS-041 必须在两种原生 runner 复验 thin helper。若 PyInstaller 不再支持目标 Python/macOS 组合，或公证对 onedir 布局提出不可接受限制，再比较 onefile、内嵌 framework 或其他方案。实验与缺口见 [ISS-029 findings](../apps/desktop/experiments/iss029/findings.md)。

---

### [DEC-016] - 2026-09-13 - v0.3.0 以双架构签名公证与安全更新为发行门槛

**背景**：用户确认当前开发线可以作为 v0.3.0，并要求同步 GitHub、建立 release 编译、参照 Folia 配置密钥及应用内自动更新。独立审查发现当前壳仍依赖外部 Python/FastAPI、`bundle.active=false`、生产 UI 未接原型、版本漂移且没有 CI/Secrets。Folia 的 updater 流程可参考，但没有 Developer ID 签名和 Apple 公证。

**决策**：v0.3.0 作为首个对外可安装目标，而非对当前代码已可发行的声明。先分别验证 arm64/x86_64 自包含 helper，再完成正式 UI、后台退出及一致备份/恢复协议；随后接入 Fathom 独立 Tauri updater key、双架构 `latest.json`，由固定 action SHA 的 CI 完成 Developer ID 签名、公证、stapling 与 private draft Release，最后使用真实 draft 产物验收 N→N+1 和失败回退。Tauri updater 签名和 Apple 签名/公证是两套独立信任链，均不可省略。公开 Release 与仓库可见性仍以具体候选供用户最终审阅。

**验证**：基线 `main@8e7c07b` 的发行差距与 Folia 仓库由两个只读审查独立核对；Fathom GitHub Secrets 当前为 0，本机 codesigning identity 为 0，未找到 `FathomNotary` keychain profile。实现、签名、公证、下载启动与 v0.3.0→测试 v0.3.1 均为 NOT_VERIFIED，任务见 ISS-029/031/009/010/028/037/040/041/030。

**影响/重评条件**：private GitHub Release 不能作为普通用户匿名更新源，客户端不得嵌入 PAT。对外自动更新前需把仓库转 public，或选择独立公开下载仓/CDN。凭据只以 secret 名进入方案，真实值由 Apple/GitHub 账户安全创建与保存；详细方案见 [v0.3.0 发行与自动更新方案](plans/2026-09-13-v0.3-release-design.md)。

---

### [DEC-015] - 2026-09-13 - 保留原型流程，按原生 Mac 方向重做视觉

**背景**：用户实际试用第一轮可点击原型后反馈“流程可以，但视觉需要明显提升”，并在三个方向中选择“原生 Mac：克制、精致、轻量”。工程交互检查通过不足以证明视觉达到预期。

**决策**：保留已认可的导航与旅程，继续在独立原型中迭代视觉；具体约束以 [DESIGN](DESIGN.md) 为准，执行边界见 [第二轮方案](plans/2026-09-13-native-mac-prototype-design.md)。工程验收与用户视觉认可分别记录，不提前将 ISS-026 标为 DONE。

**验证**：第一轮原型经过 PM 的 91 项真实浏览器检查。第二轮通过 99 项浏览器检查、13 张截图及 15 个视口页面无横向溢出；用户实测认为流程、图标与优雅程度已经明显改善，但仍偏中规中矩，希望加入个人特色。第三轮“测深/等深线＋深度环”已在 PR #10 head `a564e5e1287f89bf2719964a7339110923433a18` 完成，114/114 原型检查、13 张截图与独立 fixed-head review ACCEPT；用户对该具体原型的主观确认仍 `NOT_VERIFIED`。

**影响/重评条件**：ISS-026 保持 WAITING，用户确认前不合并 PR #10 或启动 ISS-028。Logo/App Icon 是 ISS-045 的独立人工门，不由页面原型确认代替。第三轮继续把品牌识别限制在侧栏品牌区、总览 hero、选中态和关键图表，不进入数据表或替代语义色。

---

### [DEC-014] - 2026-09-12 - M0 采用有界 PM 自动编排

**背景**：用户要求 PM 按 multi-agent-orchestration 派发 worker 自动推进，并已授权审查合并。任务源已有明确 M0 修复与原型卡，需要避免多个模型同时修改共享文件或把未验证成果自动关闭。

**决策**：授权范围、泳道、暂停/撤销规则只在 [TASKS 的 PM 策略](TASKS.md#pm-自动推进策略2026-09-12) 维护。采用独立工作区和写范围约束，PM 单写共享文档，实施和审查分离。使用当前任务心跳补偿巡检；正式 UI 仍需对具体原型的用户反馈。

**验证**：三项派发价值门通过，Orca Run/Task 在启动前预建，三个 worker 的隔离、额度、内存与身份门通过且 dispatch 绑定成功；周期心跳创建为 ACTIVE。这里只证明启动与巡检配置，业务交付、完整监督闭环及跨关机恢复尚未验证。

**影响/重评条件**：常规 M0 交付可按已有授权经 PR 收口；实际发行、生产部署、权限变更与远期智能能力保持各自门槛。用户撤销或触发暂停条件后停止新派，安全收口在途工作。

---

### [DEC-013] - 2026-09-12 - Wave 1 基础实现与最终验收分开接续

**背景**：用户授权审查并合并通知、scan_runs 和 tray PR。已有成果可复用，但复审发现通知遗漏首次记录目录、扫描失败无法可靠收尾、tray 双实例与状态行不更新；首扫、多进程协调和原生实测仍未满足规划合同。

**决策**：在原 PR 修正可独立验证的缺陷后集成基础实现。ISS-007 以持久化审查及真实进程重启验证收口；ISS-003 等统一生命周期，ISS-008 保留真机验收。进程内锁只在单 Web 进程约定下解释恢复，不能当作存活 owner 证明；全局协调继续由 ISS-020 完成。保留单一 Rust tray 作为图标、菜单和推送的所有者。

**验证**：36 项组合测试通过；真实临时目录 API/du、次日报告、通知 stub 与进程重启验证通过；Rust 离线锁定构建、JS 桥函数降级和恢复检查通过。系统通知和原生菜单交互 NOT_VERIFIED。

**影响/重评条件**：不提升发布阶段，不把代码合并自动折算为整张体验任务 DONE。ISS-020 或原生验收取得新证据后再更新状态与架构；记录中的 NOT_VERIFIED 保持可见。

---

### [DEC-012] - 2026-09-12 - 事实、解释与执行权限分离

**背景**：用户提出远期识别 AI 软件安装的不同依赖来源、解释用途/影响、将扫描结果交给 Agent，并为目录打标。误判共享或仍在使用的依赖会误导清理。

**决策**：以“本地有证据识别 → 可选只读 Agent 解读 → 可撤销标签”分层；来源可多值、用途未知须保留。Agent 输出不修改扫描事实、不执行项目代码或清理命令，远程发送只在产品内获得相应范围授权后进行。具体 provider/runtime 仍待实验，不在本次绑定框架。

**验证**：任务 ISS-034/035/036 已规定输入/输出、版本/证据绑定、未知/共享反例、超时/取消/预算、注入与过期测试；仅为目标合同，功能 NOT_VERIFIED。

**影响**：原 ISS-004 的路径模式判“安全”改为依据用途与影响解释，不要求本机必须产出固定数量的建议。设计详情见 [目标方案](plans/2026-09-12-delivery-and-intelligence.md)。

---

### [DEC-011] - 2026-09-12 - 从个人开发版转向可分发桌面产品，先修可信度与体验

**背景**：用户明确要求其他 Mac 用户可安装、未来开源（仓库先私有），并整体提升粗糙的初版 UX/UI。基线审查已复现错误快照替换、首扫状态与页面过期问题；既有 `.app` 任务没有覆盖运行时和安装生命周期。

**决策**：保留当前技术栈，以 M0 可信基线 → M1 完整体验 → M2 可安装内测 → M3 开放发布 → M4 智能解释的结果门槛取代旧版本功能清单。先做可评审交互原型；安装路线先验证“自包含 helper + 单一后台服务所有者”，不直接重写内核。任务卡明确范围、反例、验证与已有分支接续。

**验证**：main 33e81f9 的 9 测试通过；隔离 API/SQLite、真实 BSD du 与浏览器交互揭示缺口，见 [审查证据](plans/2026-09-12-project-review.md)。本次是规划交付，不代表缺陷已修或发行就绪。

**影响与历史适用范围**：
- DEC-008 的“不做 supervisor/CI/发行工程”是个人开发版边界；发行设计现在允许重新评估服务生命周期，要求基本 CI 和安装升级证据。具体所有者在 ISS-029 实验后另记决策。
- DEC-005 的 10 MiB 过滤暂保留，但缺失必须表达未知，不能把不在 entries 直接认作删除；父子折叠列表不可求和为净收益。
- DEC-006 的“约 9 个月”是错误估算：当前代码从今天向前 12 周取截止，近 35 天每日保留；当前事实已在 ARCHITECTURE/README 纠正，历史正文不覆盖。
- DEC-001 的竞争生态“空白”、DEC-002 的固定耗时与历史低盘空间数字不再作为当前产品承诺；有实测再更新。
- README/ARCHITECTURE/DESIGN/ROADMAP/TASKS 各自承担现状、体验、目标和执行职责；不依赖其他私有项目文档。合并/公开/发布仍由用户决定。

---


### [DEC-010] - 2026-09-12 - 更名 Fathom + UI 去 emoji（Folia 视觉规范对齐）

**背景**：用户指出两点——①旗下软件均为 F 开头有含义命名（Folia 叶、Funes 记忆之神）；②UI 不得使用 emoji，Folia 与 badminton-lab 均无 emoji（各自 DESIGN.md 有明确视觉规范）。

**决策**：
1. 定名 **Fathom**（用户四选一拍板：Fathom/Forester/Foresight/Falcon）。fathom 是水深测量单位，动词"测深、弄明白"，双关产品核心问题（磁盘空间去哪了）；
2. 系统性重命名：目录、Python 包、launchd 标签（旧 disk-sentinel 任务自动卸载，避免双跑）、Tauri crate/identifier/productName；`data/disk.db` 平滑迁移为 `fathom.db`（保留 9-12 基线快照，趋势不断档）；
3. UI 图标改为**内联 SVG 线条图标**（lucide 风格：24 viewBox、stroke=currentColor、1.5-2px 描边），不引入图标库依赖（无构建链约束）；Markdown 日报的 emoji 标题同步去除，保持产品气质统一；
4. CHANGELOG/DECISIONS 历史条目保留原名，顶部注记更名。

**验证**：改名后 9 个单元测试通过；服务以新标签运行；前端/壳渲染正常。

**影响**：品牌与 F 系列一致；后续新 UI 元素一律 SVG 线条图标（已写进 DESIGN.md 视觉规范）。

---

### [DEC-009] - 2026-09-12 - UX 重构为群晖式信息架构 + 研究文档不入 Git

**背景**：用户提出三点——①整体 UX 向群晖 DSM 看齐（功能如何编排展示同样重要）；②能点开目录查看；③前期调研的参照项目（duc/disktracker/QDirStat 等）作为内部研究文档保留但不 git 上传。UX 方法论参照 badminton-lab（其 DESIGN.md 的 UX 合同体系）。

**决策**：
1. 信息架构从单页纵向堆叠重构为左侧导航五页（总览/变化/分布/大文件/设置），对齐群晖"总览=结论先行、报告=档案"的编排（映射表见 docs/research/synology-storage-analyzer.md）；
2. 新增目录浏览器（面包屑 + 子目录表 + Finder 打开 + 趋势侧栏），旭日图与浏览器联动（QDirStat 双栏模式）；
3. `docs/research/` 目录沉淀研究文档，`.gitignore` 覆盖；DECISIONS 只保留决策结论与必要链接；
4. DESIGN.md 升级为 UX 合同（badminton-lab 方法论精简版）：UX 总纲 + 信息架构合同 + 页面职责合同 + 状态约定，单一真值。

**验证**：Chrome 实测分布页——旭日图（Documents 552GB/Library 516GB/Downloads 96.9GB）、面包屑、四列目录表渲染正常，无报错。

**影响**：前端从单文件堆叠变为路由化五页，后续新功能按页面合同落位；研究资料可自由引用而不污染仓库。

---

### [DEC-008] - 2026-09-12 - 桌面壳：菜单栏 tray + 主窗口（用户核心诉求）

**背景**：用户要求"软件形态"：macOS 菜单栏常驻监控 + 点击打开完整软件页面（浏览器 Tab 不算软件）。本机已具备 Rust 工具链（cargo 1.98）。

**决策**：Tauri 2 薄壳（apps/desktop/），参照 badminton-lab 的 apps/desktop 形态但从简：
1. 壳只做 UI（tray + 窗口），**不做 supervisor**——Python 后端已由 launchd 常驻（比 Badminton Lab 的 sidecar 管理简单一个量级）；
2. 窗口经本地 loader 页（tauri://localhost）轮询 FastAPI 可达后跳转 127.0.0.1:7952，复用同一前端（浏览器与壳共用，capability 配 remote urls）；
3. tray 标题（剩余 GB）由前端心跳 invoke `update_tray_status` 推送，Rust 侧零 HTTP 依赖；
4. 不做 updater/SBOM/CI 矩阵（个人软件，Badminton Lab 的产品级工程暂不需要）；
5. 关闭窗口=隐藏，退出走 tray 菜单（macOS 菜单栏应用惯例）。

**验证**：编译通过；壳进程运行，原生窗口（1220×820 居中）打开并完整渲染五页仪表盘（截图确认：左侧导航、状态卡告警、旭日图数据）。tray 图标注册无错（进程存活）；标题显示效果待用户菜单栏实测（ISS-008）。

**影响**：软件形态达成"菜单栏监控 + 独立窗口"；后续 .app 打包（bundle）后可入启动台/配 Dock 图标（ISS-009）。

---

### [DEC-007] - 2026-09-12 -（并入 DEC-008）

---

### [DEC-006] - 2026-09-12 - 快照保留策略：35 日全量 + 12 周每周一份

**背景**：数据卷仅剩 23GB，监控自身不能成为新的容量负担。群晖趋势以周/月为观察尺度。

**决策**：近 35 天保留每日快照；超过 35 天的每个 ISO 周保留最早一份，最多再留 12 周（约 9 个月趋势）。周保留用快照 id 锚定（同时间戳的两条也能正确只留一条——集合标记法在此场景失效，测试中暴露后修复）。同一天重复扫描覆盖旧快照，保证"一天一行"的趋势语义。

**验证**：`TestPrune.test_daily_and_weekly_retention`（近 3 天全留、同周两条只留一、超 12 周删除）。

**影响**：数据库长期稳定在几十 MB 以内；用户手动补扫不会产生趋势毛刺。

---

### [DEC-005] - 2026-09-12 - 只持久化 ≥10MB 目录 + Top-N 父子折叠算法

**背景**：1100 万文件的盘目录数以十万计，全量存储单快照即数百 MB。

**决策**：entries 只存 ≥10MB 的目录（du 累计语义保证大目录的父链必然也在集合内，旭日图不断层）；差分 Top-N 用 fold_changes 折叠：单链下沉（子 ≥90% 父时替换最近祖先）、兄弟共存（<90% 保留）、祖先残余量检查（<10% 或 1MB 不入选）。`topn` 只限制完整折叠并稳定排序后的最终结果，不能提前截断候选；路径 Trie 维护已选目录和正负子树总量，避免完整候选集退化为两两扫描。

**验证**：`TestFoldChanges` 覆盖兄弟 33/33/34 不折叠、单链 100/99 下沉、`topn=1`、独立高排名目录、根路径、相似前缀、尾斜杠、正负变化和大输入复杂度；禁用父替换逻辑会使三个判别测试失败。端到端冒烟确认增长/新增/消失识别正确。

**影响**：回答"哪个文件夹冒出来了"所需的最小数据集；折叠算法保证列表每个条目都有增量信息。

---

### [DEC-004] - 2026-09-12 - 运行时数据不入 Git

**背景**：data/disk.db 与 reports/*.md 含全盘目录路径与文件清单，属敏感信息；且数据库二进制不适合版本管理。

**决策**：`data/`、`reports/`、`logs/` 全部 gitignore。

**验证**：`.gitignore` 覆盖三个目录；`git status` 干净。

**影响**：仓库只含代码与文档；换机迁移时需单独备份 `data/disk.db`。

---

### [DEC-003] - 2026-09-12 - 前后端选型：FastAPI + 无构建链原生前端

**背景**：用户要求"软件形态、有前端后端"，并参照 Folia（Tauri + React + Vite）。需要决定本项目的技术栈。

**决策**：FastAPI（Python，与扫描内核同语言）+ 原生 HTML/JS + ECharts 本地化（vendor/，不依赖 CDN）。**不用** Tauri/React/Vite：
1. 本软件形态是"无人值守后台服务 + 偶尔看一眼的界面"（群晖 Storage Analyzer 本身也是 DSM Web 界面），不是每天打开的生产力工具；
2. 无 npm/构建链，AI 后续维护（用户的核心诉求）上下文最短、不会陷入构建问题；
3. 升级路径平滑：Tauri 包壳指向 127.0.0.1:7952 即可获得原生窗口，内核零改动。

**验证**：前端页面浏览器实测渲染正常（状态卡告警、趋势图、条形图、表格）。

**影响**：前端无 TypeScript 类型检查，靠简洁结构控制复杂度；若未来界面复杂度上升，再评估引入框架（届时优先保持内核不动）。

---

### [DEC-002] - 2026-09-12 - 扫描引擎用系统 du -xk，不引入 duc

**背景**：调研了三个引擎选项——自研 Python 遍历、`duc`（709★，brew 可装 1.4.6）、系统 `du`。

**决策**：用系统 `du -xk`。理由：
1. 输出天然是"目录→累计大小"，正是差分所需的全部数据，解析即可；
2. BSD du 的 C 级性能足够（1100 万文件 5-15 分钟，每天后台跑一次无感）；
3. 零依赖，skill/项目可移植；
4. duc 的核心增值（交互式旭日图浏览、全文件级索引库）不是本项目痛点，且其索引库单份约 1GB+，在 23GB 剩余空间下是负担。

**验证**：单元测试 + 冒烟走通完整链路；UTF-8 路径八进制转义还原有专门测试。

**影响**：交互式终端浏览需求（ncdu 类）不在本项目范围，需要时单独装 duc/ncdu，与本项目数据互不依赖。

---

### [DEC-001] - 2026-09-12 - 不 fork 现有开源项目，自建"引擎复用 + 薄胶水层"

**背景**：用户提出优先使用/fork 成熟开源项目。调研结论（2026-09-12）：
- 功能完全对口的 [disktracker](https://github.com/pratham15541/disktracker)（4★）核心机制绑定 Windows NTFS USN 日志，macOS 无法编译；
- [duc](https://github.com/zevv/duc)（709★）只到"索引数据库"层，无差分报告；
- QDirStat 的"快照对比"是 2018 年至今的 open feature request（issue #139）——目录级历史差分是开源生态真实空白；
- 群晖 Storage Analyzer 自身也不提供共享文件夹级历史趋势（社区实测）。

**决策**：不 fork。扫描引擎复用系统 du（见 DEC-002），自建约千行的"调度 + 快照 + 差分 + 报告"胶水层——这恰是没有任何成熟项目提供的部分。disktracker 的 snapshot diff 与 facts 表设计作为设计参考。

**验证**：调研来源见项目会话记录；核心链接已在本条目与 TASKS 中留档。

**影响**：本项目成为该能力在 macOS 上的独立实现；如未来出现成熟跨平台替代，内核模块（scanner/reports）可独立替换。
