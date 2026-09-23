# 贡献指南（草案）

> 本文件是 ISS-037 交付的外部贡献者说明草案；维护者可按需调整措辞。
> 验证协议见 [docs/TESTING.md](docs/TESTING.md)
> 为准，两者与本文冲突时以它们为准。

## 项目是什么

Fathom 是 macOS 本机目录容量历史追踪工具：每日 `du` 快照存 SQLite，
差分出"哪个文件夹冒出来了"。本地优先——无账号、无云依赖，扫描数据
只存在你的机器上。

## 开发环境

- macOS（核心依赖 BSD `du` 行为与系统 WebView）。
- Python 3.14（`fathom/` 与测试；本地约定解释器路径 `.runtime/bin/python`）。
- Rust toolchain（仅改 `apps/desktop/src-tauri/` 时需要；构建走
  `scripts/ci_cargo_locked.sh` 的 `--locked --offline` 口径）。
- 前端无构建链（原生 ES modules + vendored ECharts），改完即生效。

## 提交前自查

1. **测试**：`bash scripts/ci_pytest.sh`（fail-closed：通过数必须精确
   等于脚本内期望值，测试增删要显式同步）。
2. **版本一致性**：`bash scripts/check_version_consistency.sh`——版本
   唯一权威源是 `fathom/__init__.py` 的 `__version__`，不要手改
   `tauri.conf.json` / `Cargo.toml` 里的版本号再忘了同步。改版本后
   同步 `Cargo.lock` 本地包 `fathom-desktop` 的版本行。
3. **真实入口验证**：行为变化请按 [docs/TESTING.md](docs/TESTING.md)
   用隔离数据根跑真实交互（不要触发真实 HOME 扫描、不写生产库）。

## 约定

- **新依赖需先开 issue 讨论**：运行时依赖进入冻结发行产物，锁定与
  许可证核对成本高（见 [docs/plans/2026-09-14-dependency-inventory.md](docs/plans/2026-09-14-dependency-inventory.md)），
  不要在 PR 里顺手加。
- SVG 图标只集中在 `frontend/icons.js` 维护，页面不写装饰 emoji。
- 扫描事实与解释分开：新增展示/建议功能不得修改快照事实或扩大读取
  范围。
- 提交信息用 conventional 风格（`feat:` / `fix:` / `docs:` …），一个
  PR 做一件事。

## 分支与 PR

- 分支命名 `iss-NNN-slug` 或 `topic-slug`，目标分支 `main`。
- PR 描述写清：动机、做了什么、怎么验证的（命令与结果）、没验证
  什么（`NOT_VERIFIED` 如实标注，不装绿）。

## 许可证

仓库 LICENSE 尚未落定（见
[docs/plans/2026-09-14-license-options.md](docs/plans/2026-09-14-license-options.md)）。
在维护者确定许可证之前提交的贡献，默认按确定后的仓库许可证处理；
如需其他安排请提前在 issue 中说明。
