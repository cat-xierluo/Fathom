# Fathom · Fathom

macOS 本机的目录容量变化追踪软件——复刻群晖「存储空间分析器」最有价值的能力：**每日扫描，差分出哪个文件夹在悄悄吃掉你的磁盘**。

## 它解决什么问题

磁盘天天告急，却不知道空间去哪了。本软件每天自动扫描一次，记录每个目录（≥10MB）的大小快照，对比生成：

- 📈 增长最多的目录（Top 25，父子折叠不刷屏）
- 🆕 新出现的大目录（≥100MB）
- 📉 缩减 / 消失的目录
- 📦 近 N 天新增/修改的大文件（≥100MB）
- 整卷容量趋势、目录级历史趋势折线、当前占用旭日图

所有数据只存在本机 SQLite，不联网。

## 快速开始

```bash
cd ~/Library/Application\ Support/maoscripts/fathom
.venv/bin/python main.py install    # 安装 launchd：每日 12:00 扫描 + 常驻 Web 服务
```

**两种使用形态（同一前端）**：

1. **桌面软件**（推荐）：
   ```bash
   cd apps/desktop/src-tauri && cargo run   # 菜单栏常驻 + 原生窗口
   ```
   菜单栏出现圆环图标与剩余 GB；左键点图标打开主窗口；关闭窗口=隐藏，退出走 tray 菜单。（.app 打包见 ISS-009，当前为 debug 直跑）
2. **浏览器**：打开 [http://127.0.0.1:7952](http://127.0.0.1:7952)（Safari 可"添加到 Dock"）

手动操作：

```bash
.venv/bin/python main.py scan       # 立即扫描一次（首次基线，全盘约 5-15 分钟）
.venv/bin/python main.py status     # 查看快照数、磁盘剩余
.venv/bin/python main.py report     # 输出最近两快照的对比日报（Markdown）
.venv/bin/python main.py bigfiles --days 7 --min-mb 100
.venv/bin/python main.py uninstall  # 卸载 launchd 任务
```

## 权限说明

- 扫描 `$HOME` 时，`~/Library` 下受 TCC 保护的部分目录（邮件、通讯录等）可能无权限，软件会**如实统计并在报告中标注**无权限目录数，不静默漏报。
- 如需完整覆盖：系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 为运行 `main.py scan` 的终端（或 launchd 场景下的 `/opt/homebrew/opt/python@3.14/bin/python3.14`）授权。

## 故障排查

| 症状 | 原因与处理 |
|------|-----------|
| Web 服务打不开 | `launchctl list \| grep fathom` 查状态；日志在 `logs/launchd-web.err.log` |
| launchd 任务失效、报 python 不存在 | Homebrew 大版本升级移除了旧 Python：`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` 重建后重跑 `install` |
| 首日看不到对比 | 差分需要 ≥2 个快照；首扫是基线，第二天 12:00 后自动产生首份日报 |
| 扫描很慢 | 冷缓存全盘约 5-15 分钟属正常（约 1100 万文件），后台执行不影响使用 |

## 开发

技术栈与架构见 [AGENTS.md](AGENTS.md) 与 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)；决策记录见 [docs/DECISIONS.md](docs/DECISIONS.md)。

```bash
.venv/bin/python -m pytest tests/ -q        # 9 个单元测试
FATHOM_DB=/tmp/x.db .venv/bin/python main.py serve   # 冒烟（不碰生产库）
```

## 许可证

私有项目（maoscripts），不对外分发。
