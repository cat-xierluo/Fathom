# Fathom

macOS 本机的目录容量历史追踪工具：记录哪些目录在增长，把最近变化与历史证据放在一起，帮助理解空间去向。

目前是 **v0.3.0 开发线**，提供每日扫描内核、SQLite 快照、Markdown 日报、五页 Web 仪表盘和 Tauri 开发壳。v0.3.0 是首个可分发版本的目标，当前还没有面向普通用户的完整安装包或 Release；自包含运行时、正式 UI、Developer ID 签名/公证和应用内更新仍在任务队列，详情见 [路线图](docs/ROADMAP.md)、[任务](docs/TASKS.md) 与 [发行方案](docs/plans/2026-09-13-v0.3-release-design.md)。

近期目标是让其他 Mac 用户无需开发工具即可安装使用，并整体提升 UX/UI；远期增加依赖来源/用途识别、扫描结果的可选 Agent 解释与目录标签。**这些智能功能尚未实现。** 当前源码私有托管，未来开源与许可证选择另行确认。

## 当前能做什么

- 用系统 du 记录目录累计占用，持久化 ≥10 MiB 的目录；同根同日扫描替换当日快照。
- 对比快照，展示增长、缩减及首次/不再被记录的目录；缺失不能直接证明新建/删除。
- 查看目录分布、历史趋势与近期修改的大文件；从界面在 Finder 中显示路径。
- 开发版通过 launchd 安排每日 12:00 扫描及常驻本地 Web 服务。
- 手动扫描状态与历史写入 SQLite，可在服务重启后查询；当前仅按单 Web 进程运行，CLI/定时入口尚未统一协调。
- 日报写完后尝试发送 macOS 通知，摘要含增长、首次记录的大目录与剩余空间；实际展示受系统通知策略影响，首扫无日报时尚不通知。

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

仓库目前私有，clone 需要访问权限。CI 还会用锁定的 Node/Playwright 运行隔离 Chromium/API 检查；完整复跑命令见 [TESTING](docs/TESTING.md)。开发 UI 首选其中的隔离夹具服务，可安全查看两天数据及重扫交互，不扫描 HOME。

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

当前 `scan --root` 虽可指定路径，多根对比和保留尚有缺陷；不要将不同根混入同一生产库。扫描耗时依磁盘/文件数/权限而异，不承诺固定时长。

## 数据、权限与已知限制

- 开发版数据位于项目下 `data/fathom.db`，报告在 `reports/`、日志在 `logs/`，均不入 Git。备份应使用 SQLite 一致备份或停止写入后处理，不能在 WAL 活跃时只拷贝主 DB 当作完整备份。
- **FATHOM_DB 仅覆盖数据库位置**，不隔离报告、日志、扫描根或端口；完整隔离方法见 [TESTING](docs/TESTING.md)。
- 未授权的目录可能无法读取；权限错误行数不等于覆盖比例，也不能说明被跳过的数据不重要。发行 helper 的授权主体待实机验证。
- 失败扫描覆盖当天有效数据的问题已修复并由故障注入回归覆盖；网页首扫误报失败、特殊路径解析和选择器过期仍在任务队列，详情见 [审查记录](docs/plans/2026-09-12-project-review.md) 与 [任务](docs/TASKS.md)。
- 目录 du 累计大小、文件逻辑大小和整卷可用空间有不同口径；父子行不能直接相加，目录体积不等于可回收空间。
- 保留策略实际总跨度约从今天向前 12 周，其中近 35 天保留每日快照；报告/日志独立清理尚未完成。

## 故障处理

| 现象 | 当前可核查信息 |
|---|---|
| 本地页面打不开 | 检查服务是否启动、7952 是否被其他服务占用；已装 launchd 时查 `logs/launchd-web.err.log` |
| 首日无差分 | 需要两个不同有效日期；分布可在基线完成后使用；网页误报失败为已登记问题 |
| Homebrew 升级后后台失效 | 当前 venv 绑定本地解释器，需重建兼容 venv 并在选定部署环境重新安装任务 |
| 通知未显示 | 查看 `logs/notify.log`；命令提交成功不等于横幅已展示，首扫无日报时不会通知 |
| 部分目录看不到 | 核查权限、10 MiB 入库阈值及测量时点，不直接判定目录被删除 |

## 继续开发

新模型从 [AGENTS](AGENTS.md) 和 [TASKS](docs/TASKS.md) 接手；产品目标见 [ROADMAP](docs/ROADMAP.md)，体验合同见 [DESIGN](docs/DESIGN.md)，当前实现见 [ARCHITECTURE](docs/ARCHITECTURE.md)。本地历史数据与客户路径不得进入测试 fixture、截图或 PR。

## 许可证状态

当前未授予开源许可证，不作公开分发声明。仓库保持 private 时只准备内部 RC/draft Release，普通客户端不能把 private GitHub Release 当作匿名自动更新源。依赖/资源许可清单及项目许可证选择由 ISS-037 完成，用户批准后再开放发布。
