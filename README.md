# Fathom

macOS 本机的目录容量历史追踪工具：记录哪些目录在增长，把最近变化与历史证据放在一起，帮助理解空间去向。

目前是 **v0.3.0 开发线**，已有扫描内核、SQLite 快照、Markdown 日报、正式五页界面，以及可在 Apple Silicon 上构建的未签名自包含 `.app`/`.dmg`。尚未完成新账户安装、发行后台计划、双架构和应用内更新验收，不能据此称为已可公开发布。Apple 签名/公证延期（改为未签名分发 + DMG 内首次打开放行指引）；其余发行目标见 [路线图](docs/ROADMAP.md)。

近期目标是让其他 Mac 用户无需开发工具即可安装使用，并整体提升 UX/UI；远期增加依赖来源/用途识别、扫描结果的可选 Agent 解释与目录标签。**这些智能功能尚未实现。** 源码采用 Apache-2.0 许可证（见 LICENSE），当前仍私有托管，公开发布时机另行确认。

## 当前能做什么

- 用系统 du 记录目录累计占用，持久化 ≥10 MiB 的目录；同根、同阈值、同排除集的当日扫描替换该数据集快照。
- 对比快照，展示增长、缩减及首次/不再被记录的目录；缺失不能直接证明新建/删除。
- 查看目录分布、历史趋势与近期修改的大文件；从界面在 Finder 中显示路径。
- 开发版通过 launchd 安排每日 12:00 扫描及常驻本地 Web 服务。
- API、CLI 与定时扫描共用 `scan_coordinator`；跨进程 `flock` 保证单一扫描，状态与分阶段结果写入 SQLite。
- 运行根、扫描根、资源根和端口可显式隔离；旧开发库打开时会经 schema v5 校验、WAL 一致备份和事务迁移。
- 日报写完后尝试发送 macOS 通知，摘要含增长、首次记录的大目录与剩余空间；首扫和中断也有通知语义，实际展示受系统通知策略影响、仍需实机验收。
- 设置页可持久化监控范围、排除列表、计划时间和阈值；桌面壳已有经确认开启/关闭后台计划的入口，并能提示已保存时间与注册计划的差异、经确认重新安装计划。发行账户中的系统行为仍待验收。
- 设置页已有检查更新、确认下载/安装与确认重启界面；安装前会先停止新写入、请后台服务退出并对数据库做一致性备份，下载有进度、可取消，失败自动回到当前可运行版本（隔离环境五类故障场景验证）。当前仍使用开发公钥和占位更新地址，真实更新源未启用、实机升级未验收，不能据此认为已支持真实升级。

扫描事实与报告保存在本机。当前应用没有 Agent 或远程分析功能；未来云端解释将作为单独启用和授权的能力。工具不执行文件清理。

## 开发者运行

当前代码在 macOS、Python 3.14 环境验证。新克隆需先建立环境（Python 3.14 须已安装）；运行与开发依赖已分层，并由 constraints 固定已验证闭包。

```bash
git clone https://github.com/cat-xierluo/fathom.git
cd fathom
python3.14 -m venv .venv
.venv/bin/python -m pip install -c constraints.txt -r requirements-dev.txt
/bin/bash scripts/ci_pytest.sh
/bin/bash scripts/ci_cargo_locked.sh
```

仓库目前私有，clone 需要访问权限。锁定的本地脚本会运行 pytest、Cargo 和隔离 Chromium/API 检查；GitHub Actions 因账户额度问题暂不可用（2026-09-23 实测 job 在步骤执行前被拒），额度恢复后重新启用。完整复跑命令与临时本地合并门禁见 [TESTING](docs/TESTING.md)。开发 UI 首选其中的隔离夹具服务，可安全查看两天数据及重扫交互，不扫描 HOME。

已了解当前限制并准备监控本机时：

```bash
.venv/bin/python main.py serve      # 127.0.0.1:7952，前台运行
.venv/bin/python main.py status
```

浏览器打开 [本地仪表盘](http://127.0.0.1:7952)。Tauri 开发壳需要 Rust/macOS 构建环境，并依赖上述服务另行运行：

```bash
cd apps/desktop/src-tauri
cargo run
```

关闭窗口会隐藏；退出通过 tray 菜单。此开发入口不能证明发行包可安装。

下列命令会实际扫描本机或修改本用户后台任务，请在选定部署环境执行：

```bash
.venv/bin/python main.py scan                     # 默认 HOME，首扫建立基线
.venv/bin/python main.py report                   # 最近两快照，首日无对比报告
.venv/bin/python main.py bigfiles --days 7 --min-mb 100
.venv/bin/python main.py install                  # 安装本用户两个 LaunchAgent
.venv/bin/python main.py uninstall                # 卸载开发版后台任务
```

当前比较、同日替换与保留按 `(root, min_kb, exclude_names)` 隔离；这不代表多根、多卷产品已经验收，首发仍以单个受支持范围为目标。扫描耗时依磁盘/文件数/权限而异，不承诺固定时长。

## 数据、权限与已知限制

- development 默认数据位于项目下 `data/fathom.db`，报告在 `reports/`、日志在 `logs/`，均不入 Git；release 模式默认使用 `~/Library/Application Support/Fathom`。可用 `FATHOM_RUNTIME_DIR`、`FATHOM_SCAN_ROOT`、`FATHOM_RESOURCE_DIR`、`FATHOM_PORT` 或等价 CLI 参数完整隔离，详见 [TESTING](docs/TESTING.md)。
- 单次 `du` 采集的安全时限由 `FATHOM_DU_TIMEOUT_S` 控制（默认 14400 秒即 4 小时）；超过时限的扫描记为「已中断」并保留上一次有效快照，不会留下半写的数据。该值必须是正的有限数，非法值会让程序在启动时报错而不是静默关闭时限。大目录（如百万级目录的用户主目录）实测可能超过 1 小时，若日报连续显示中断可适当调大。
- `FATHOM_DB` 保留兼容：未指定运行根时，其父目录成为完整运行根，避免只隔离数据库。迁移备份使用 SQLite backup API 包含已提交 WAL；仍不能在普通备份中只拷贝活跃主 DB。
- 未授权的目录可能无法读取；权限错误行数不等于覆盖比例，也不能说明被跳过的数据不重要。发行 helper 的授权主体待实机验证。
- 失败扫描保护、特殊路径解析、首扫成功语义和选择器刷新已修复并有真实入口回归；开发版跨日定时和打包态主界面已有验收记录，系统通知实际展示与其余发行路径仍须单独验收。
- 目录 du 累计大小、文件逻辑大小和整卷可用空间有不同口径；父子行不能直接相加，目录体积不等于可回收空间。
- 保留策略实际总跨度约从今天向前 12 周，其中近 35 天保留每日快照；报告/日志已按可解析日期与各自保留天数在扫描收尾清理。

## 故障处理

| 现象 | 当前可核查信息 |
|---|---|
| 本地页面打不开 | 检查服务是否启动、7952 是否被其他服务占用；已装 launchd 时查 `logs/launchd-web.err.log` |
| 首日无差分 | 需要两个不同有效日期；首扫会成功保存基线，报告状态为暂不可用，分布可在基线完成后使用 |
| Homebrew 升级后后台失效 | 当前 venv 绑定本地解释器，需重建兼容 venv 并在选定部署环境重新安装任务 |
| 通知未显示 | 查看 `logs/notify.log`；命令提交成功不等于横幅已展示，首扫可发送基线通知，调用成功不保证横幅展示 |
| 部分目录看不到 | 核查权限、10 MiB 入库阈值及测量时点，不直接判定目录被删除 |

## 继续开发

贡献者请读 [CONTRIBUTING](.github/CONTRIBUTING.md)；产品目标见 [ROADMAP](docs/ROADMAP.md)，体验合同见 [DESIGN](docs/DESIGN.md)，当前实现见 [ARCHITECTURE](docs/ARCHITECTURE.md)。本地历史数据与客户路径不得进入测试 fixture、截图或 PR。

### 构建未签名的桌面包（开发者，ISS-009 切片 1/2）

当前可在 Apple Silicon 开发机上产出**未签名、未公证**的 `.app` 与 `.dmg`，仅供本机/内部试用。DMG 安装窗口自带安装引导与「首次打开提示」（本应用未经 Apple 签名与公证，首次打开可能被 macOS 阻止；右键点按 Fathom 选「打开」，或前往 系统设置 → 隐私与安全性 点「仍要打开」）——签名/公证延后出 v0.3.0：

```bash
# 1. 冻结 helper（需 apps/desktop/experiments/iss029/.venv-build：PyInstaller 6.22.3 + fastapi/uvicorn 钉定版本）
bash scripts/build_helper.sh
# 2. 打包 Tauri 壳 + helper（产物：apps/desktop/src-tauri/target/release/bundle/{macos/Fathom.app,dmg/Fathom_0.3.0_aarch64.dmg}）
bash scripts/build_app.sh
# 3. 校验：结构 / 只读布局 / 主界面页面 / 含空格与中文路径启动 / 端口冲突让位与零击杀 / 二次启动 / 退出回收 / 陈旧 instance / 端口耗尽 / DMG 安装提示（22 项）
bash scripts/verify_app_bundle.sh
```

- 产物校验值写在 `apps/desktop/src-tauri/target/release/bundle/checksums.txt`（SHA256）；冻结 helper 的 SHA256 由 `build_helper.sh` 打印，同一 pin 集合下可复现。**未签名分发的信任边界**：接收方只能靠校验值比对确认来源，安装与放行步骤见 DMG 背景提示。
- 支持矩阵：仅 `darwin-aarch64`；`x86_64`、Developer ID 签名、Apple 公证与 stapling、应用内更新均未实现（签名/公证延后见 DEC-022；ISS-040/041），发行验收矩阵见 [TESTING §4](docs/TESTING.md#4-桌面与分发矩阵)。
- 应用图标为「层叠深潭」、界面小尺寸（tray/字标/favicon）为「深度环」的双形态品牌体系（ISS-045/DEC-023，2026-09-19 实测 DMG 安装窗口/Dock/Launchpad/Spotlight 与深浅色菜单栏渲染）；图标未验证项：18pt 小菜单栏下 tray 可辨性。其余未验证项：无 Python/Rust/Homebrew 新账户从 DMG 首启、tray 菜单手点退出、断网首启（握手页实机渲染已于 2026-09-18 验证）。

## 许可证状态

本项目采用 **Apache License 2.0**（仓库根 `LICENSE`，Copyright 2026 maoking，与 Folia 一致），第三方组件声明见 `docs/THIRD_PARTY_NOTICES.md`，依赖来源清单见 `docs/plans/2026-09-14-dependency-inventory.md`。发行与更新源的启用状态以 [CHANGELOG](CHANGELOG.md) 为准。
