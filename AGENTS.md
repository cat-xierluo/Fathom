# Fathom 项目协作指南

全程中文。Fathom 是 macOS 本机目录容量历史追踪工具，正从个人开发版推进为可分发桌面应用。远期增加依赖用途识别与可选 Agent 解释；当前尚无这些智能功能。

## 新会话接手

1. 读本文 → [TASKS](docs/TASKS.md) 的领取规则和完整任务卡 → 对应的 [ARCHITECTURE](docs/ARCHITECTURE.md)、[DESIGN](docs/DESIGN.md) 或目标方案。
2. 运行 `git status --short --branch`、`git worktree list`、`git remote -v`，并核对 `origin/main`、开放 PR 和所有 worktree。其他会话可能正在集成分支，不能把当前目录或旧对话当作 main；私有 `orchestration/` 状态不是接手前置。
3. 用户当前指令优先。普通执行只领取状态为 READY、依赖已验收的任务；不按标题猜范围，不把已有分支重新实现。
4. 在独立分支/工作区推进，项目分支名用 `iss-NNN-slug`。同一分支完成一个可独立验收的任务；超出卡片范围先登记拆分，不顺手做未来功能。
5. 先复现卡片反例，再实现，再按 [TESTING](docs/TESTING.md) 验证真实入口。记录基线/命令/结果/未验证范围。测试通过只证明覆盖到的行为。
6. 提交 PR，回写任务状态为 REVIEW；合并规则与临时本地门禁见 [TASKS 自动推进策略](docs/TASKS.md#pm-自动推进策略2026-09-12)。旧 `fathom-m0-pm` 心跳已暂停；恢复自动化必须由用户明确要求或建立一个新 PM 唯一 owner，禁止双 PM。无远端时只做本地提交并记录阻塞。

## 接手与收口的事实检查

- 当前阶段与唯一接续顺序只读 TASKS 的当前摘要和索引；完整卡片中的日期记录、旧“下一项”和 plans 历史建议不是当前派发指令。已有分支或 review worktree 即使尚无 PR，也先核对 owner 与成果。
- 对每个子成果区分代码已实现、尚未接入产品、真实路径已验证和 `NOT_VERIFIED`；子卡 DONE 不关闭父卡。夹具自行模拟的流程不能证明生产入口已调用该流程。
- 合并后同步摘要、索引与当前卡片；按实际变化更新 README/ARCHITECTURE/CHANGELOG，并检查 DESIGN、TESTING 和被引用操作清单里的“未实现/待确认”是否已过期。只读 Git 与静态核对不写成测试复跑。
- 没有合法 READY 时列清具体实现缺口与环境缺口。继续投入须能指向现有阶段退出条件；不能只为保持派发增加文档、夹具、视觉小修或远期功能，也不能把尚缺产品接线归为“只待人工”。

## 文档职责与冲突处理

| 文档 | 唯一职责 |
|---|---|
| [README](README.md) | 当前用户能使用什么、如何运行、限制与故障处理 |
| [ARCHITECTURE](docs/ARCHITECTURE.md) | 指定基线已实现的模块、数据流、接口和局限 |
| [DESIGN](docs/DESIGN.md) | 页面职责、交互与视觉合同；明确目标尚未实现的部分 |
| [ROADMAP](docs/ROADMAP.md) | 产品方向、阶段退出条件、延后事项 |
| [TASKS](docs/TASKS.md) | 领取状态、依赖、范围、验收、证据 |
| [DECISIONS](docs/DECISIONS.md) | 真实取舍与适用范围变化，历史记录保留 |
| [TESTING](docs/TESTING.md) | 隔离验证方法和发布验收矩阵 |
| [CHANGELOG](CHANGELOG.md) | 已交付及待合并的用户可见变化 |
| docs/plans/ | 有日期的审查证据与目标设计；不维护任务状态 |

代码与文档冲突时以代码/真实结果确定“现状”，登记缺口；不能为了让实现显得合规而降低已确认验收标准。本文、任务卡须能在新克隆中独立理解，不依赖用户其他私有项目或未安装 Skill。

## 模块边界

- `fathom/scanner.py`：du 采集、快照与保留策略；`fathom/db.py`：连接和 schema。
- `fathom/reports.py`：差分及 Markdown；`fathom/notify.py`：报告完成后的系统通知尝试；`fathom/bigfiles.py`：近期修改大文件查询。
- `fathom/scan_coordinator.py`：API/CLI/定时扫描的统一生命周期、跨进程 `flock` 与分阶段状态；`fathom/api.py`：HTTP 查询/入口；`fathom/cli.py`：CLI；`main.py` 是入口包装。
- `fathom/config.py`：当前常量与路径；`fathom/launchd.py`：开发版后台任务安装。
- `frontend/`：无构建链 HTML/JS/CSS，ECharts 本地资源。SVG 图标只在 `frontend/icons.js` 集中维护，禁止装饰 emoji。
- `apps/desktop/`：Tauri 壳。打包态（ISS-009 切片 1 起）由壳拉起 `Contents/Resources/helper/` 内的 PyInstaller 冻结 helper 并按身份握手/让位/回收（`src-tauri/src/helper.rs`）；开发态仍依赖独立运行的 FastAPI。产物未签名、未公证、仅 arm64、未在新账户实测，不得宣称为可分发的自包含包。
- 新模块只按任务卡引入，未来模块/接口先看目标方案中的“拟新增”，不要从文档假定文件已存在。

## 数据与操作不变量

- 生产 `data/`、`reports/`、`logs/` 以及 `docs/research/` 不入库；示例、截图与测试用合成路径和数据。
- `FATHOM_DB` 是兼容入口：未显式设置 `FATHOM_RUNTIME_DIR` 时，其父目录成为完整运行根；扫描根、只读资源根和端口仍必须显式隔离。不得触发真实 HOME 扫描或覆盖生产日报，步骤见 TESTING。
- 验证不执行 `install/uninstall`、不修改生产调度或权限；确需部署验收时另按已授权环境进行。禁止覆盖其他工作区改动。
- 扫描事实与解释分开；缺失条目不是删除证据，目录累计大小不可逐行相加当作可回收空间。
- Agent 解释、目录标签不能修改快照事实、扩大读取范围或触发清理。远程分析必须在产品内得到相应数据发送授权。
- 配置/schema/API 的改动必须有旧数据兼容路径和失败回退；不以删除旧库作为升级方案。

## 完成边界

验收框全通过、相关真实交互验证、文档同步、任务证据可复查后才能进入 REVIEW。未执行的实机/跨日/签名验证写 `NOT_VERIFIED` 并留下未勾项，不能用 mock 或 cargo build 替代。DONE 表示在用户授权范围内合并且证据齐全；历史“已完成”条目不自动满足新发行门槛。
