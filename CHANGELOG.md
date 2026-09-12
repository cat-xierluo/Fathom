# 变更记录

本文件是用户可见变更的权威记录。格式遵循 legal-skills 的 CHANGELOG 规范。
（0.2.0 及以前项目名为 disk-sentinel / 容量哨兵，0.3.0 起更名 Fathom，历史条目保留原名。）

## [0.3.0] - 2026-09-12

### 改进
- **更名 Fathom**：延续 Folia/Funes 的 F 系列命名。fathom（英寻）是水深测量单位，动词意为"测深、弄明白"——双关直击产品要回答的问题："I can't fathom where my disk space went"。涉及：项目目录、Python 包、launchd 标签（com.maoscripts.fathom-*，旧任务自动卸载）、Tauri identifier/productName/crate、全部文档；数据库文件平滑迁移（disk.db → fathom.db，保留首扫基线数据）
- **UI 全面去 emoji，对齐 Folia 视觉规范**：导航/面板/按钮/日报全部改用内联 SVG 线条图标（lucide 同款 stroke 风格，currentColor 跟随语义色），Markdown 日报标题同步去 emoji

## [0.2.0] - 2026-09-12

### 新增
- **Tauri 2 桌面壳**（apps/desktop/）：macOS 菜单栏常驻图标（圆环 template 图标 + 剩余 GB 标题，由前端心跳推送）+ 原生主窗口（1220×820，居中）；左键点 tray 图标打开窗口；tray 菜单：状态行 / 打开主界面 / 立即扫描（触发前端扫描）/ 退出；关闭窗口=隐藏（菜单栏应用惯例）
- **目录浏览器**（分布页）：面包屑下钻、子目录表（大小/较上快照变化/占比）、行内 🔍 在 Finder 中显示、侧栏目录趋势小图；旭日图点击与浏览器联动定位（QDirStat 双栏联动模式）
- **历史日报档案**（变化页）：reports/*.md 列表化回看
- **群晖式信息架构重构**：单页纵向堆叠 → 左侧导航五页（总览/变化/分布/大文件/设置），hash 路由；总览页新增"今日摘要"（结论先行）
- 后端新 API：`/api/browse`（目录浏览器数据）、`/api/reports`（日报列表/内容）、`/api/reveal`（Finder 显示，越界拒绝）；CORS 允许 Tauri loader 页探测
- **内部研究文档体系**（docs/research/，已 gitignore 不入库）：群晖存储空间分析器 UX 与功能映射、五个开源项目（duc/disktracker/QDirStat/WinDirStat/DaisyDisk）功能抽取与编排分析
- **DESIGN.md 升级为 UX 合同**（参照 badminton-lab 方法论）：UX 总纲（按任务组织/结论先行/单一真值/状态全覆盖）+ 信息架构合同 + 五页职责合同 + 状态约定
- tray 图标生成脚本（scripts/make_tray_icon.py，纯 stdlib PNG 编码）

### 改进
- 快照对选择在页面切换后保留用户选择
- 大文件表上限提到 200 行（对齐群晖"最多 200 个"）

### 待办事项
- tray 标题（剩余 GB）与 tray 菜单实际显示效果待用户菜单栏实测（ISS-008）
- .app 打包（tauri bundle）与 Dock/启动台图标待做（ISS-009）
- 文件类型分布、重复文件检测列入路线图（v0.3）

## [0.1.0] - 2026-09-12

### 新增
- 每日快照内核：调用系统 `du -xk` 扫描 `$HOME`，目录级大小（≥10MB）存入 SQLite；同日重复扫描自动覆盖，保留策略为近 35 天每日 + 更早每周一份（最多 12 周）
- 差分引擎：Top 增长/缩减（父子折叠）、新增目录（≥100MB）、消失目录四类视图
- Markdown 日报：每次扫描后自动生成到 `reports/YYYY-MM-DD.md`，含卷剩余变化与无权限目录提示
- 近期大文件查询：`find` 近 N 天修改的 ≥100MB 文件
- Web 仪表盘（http://127.0.0.1:7952）：状态卡（剩余空间低量红色告警）、卷容量趋势、快照对比条形图、当前占用旭日图（点击下钻 + 目录历史趋势联动）、大文件表、手动扫描按钮
- FastAPI 服务：9 个 API 端点，自动文档在 `/docs`
- launchd 部署：每日 12:00 自动扫描 + 常驻 Web 服务（KeepAlive 自动拉起），`install`/`uninstall` 一键管理
- 9 个单元测试覆盖扫描、差分折叠、保留策略、路径转义还原

### 已知限制
- 扫描 `~/Library` 受 TCC 保护目录需授予完全磁盘访问权限，未授权时如实标注无权限数（实测仅 6 个目录受限，影响很小）
- APFS clone 文件的共享块会被 du 重复计量（与群晖行为一致，属已知偏差）
- venv 绑定 Homebrew Python 3.14，brew 大版本升级后需重建（见 README 故障排查）
