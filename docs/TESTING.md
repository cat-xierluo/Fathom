# 验证与交付方法

本文件提供可重复的验证入口。具体任务结果只写 [TASKS](TASKS.md)，已知基线反例见 [审查证据](plans/2026-09-12-project-review.md)。本文件是验证协议；列出的反例与验收项不代表已修复或已通过。

## 1. 环境与隔离

当前源码使用 Python 3.14；依赖安装见 [README](../README.md)。新工作区没有 venv 时可用已知兼容解释器的绝对路径运行，必须确认 `fathom.__file__` 指向待测工作区，不能误测别处源码。

```bash
.venv/bin/python -c 'import fathom; print(fathom.__file__)'
.venv/bin/python -m pytest tests/ -q
```

**当前只设置 FATHOM_DB 不足以隔离：** CLI scan/API scan 仍可能扫描 HOME，报告/日志仍在源码工作区，serve 默认仍用生产端口。必须同时隔离 DATA_DIR、DB_PATH、REPORTS_DIR、LOGS_DIR、DEFAULT_ROOT、端口；install/uninstall/权限与真实 Finder 动作另属实机验证。ISS-025 交付后同步本节到正式配置入口。

不复制生产库到仓库，不在报告贴私人路径。数据量测试用合成目录或经用户选择的测试范围。故障测试 mock `open`、通知、launchctl 等系统动作，检查“有没有被调用”，不实际动生产服务。

针对 Wave 1 的回归入口：

```bash
.venv/bin/python -m pytest tests/test_notification.py tests/test_scan_runs.py -q
cargo build --locked --offline --manifest-path apps/desktop/src-tauri/Cargo.toml
```

持久化验收必须补一次真实进程重启：为测试服务选择独立端口、临时根和固定的临时运行目录，先核对实例身份再执行扫描，记录 run_id；停止该测试服务后，以同一临时库重启，再检查结束态与历史。不能把换一个 threading.Lock 的 mock 当作真实重启证据。首扫无报告的当前已知缺陷应单独记录，不能伪造成功结果。通知可以 stub 隔离；原生通知与菜单交互另行实测。

## 2. 可直接运行的 UI 夹具服务（当前基线）

在待测仓库根运行以下命令，以临时目录和 8799 端口启动；如果端口已被使用，改成另一个明确测试端口，不停止未知进程。这个夹具替换 du，仅用于前端状态交互；扫描器正确性必须另跑真实 du 测试。

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
import datetime as dt
import json
import uuid
from fathom import config, db, scanner
from fastapi.responses import JSONResponse
import uvicorn

with TemporaryDirectory(prefix='fathom-ui-') as runtime:
    base = Path(runtime)
    config.DATA_DIR = base / 'data'
    config.DB_PATH = config.DATA_DIR / 'fixture.db'
    config.REPORTS_DIR = base / 'reports'
    config.LOGS_DIR = base / 'logs'
    config.DEFAULT_ROOT = base / 'root'
    config.DEFAULT_ROOT.mkdir()
    config.PORT = 8799
    config.ensure_runtime_dirs()
    root = str(config.DEFAULT_ROOT)
    conn = db.connect()
    for ago, size in [(1, 400000), (0, 300000)]:
        scanner.run_du = lambda _, value=size: (
            {root: value, root + '/Archive': value - 100000,
             root + '/Notes': 100000}, 0)
        sid = scanner.create_snapshot(conn)
        day = (dt.date.today() - dt.timedelta(days=ago)).isoformat()
        conn.execute('UPDATE snapshots SET created_at=? WHERE id=?',
                     (day + 'T10:00:00', sid))
        conn.commit()
    conn.close()
    scanner.run_du = lambda _: (
        {root: 250000, root + '/Archive': 150000,
         root + '/Notes': 100000}, 0)
    # ISS-003 集成后也不得让夹具发真实系统通知。
    try:
        from fathom import notify
    except ImportError:
        pass
    else:
        notify.notify_scan_done = lambda *args, **kwargs: None
    from fathom.api import app
    fixture_id = uuid.uuid4().hex

    @app.middleware('http')
    async def fixture_identity(request, call_next):
        if request.url.path == '/__fixture':
            return JSONResponse({'fixture_id': fixture_id, 'root': root})
        return await call_next(request)

    print(json.dumps({'fixture_id': fixture_id,
                      'url': f'http://127.0.0.1:{config.PORT}'}), flush=True)
    uvicorn.run(app, host='127.0.0.1', port=config.PORT)
PY
```

启动后先读取终端的 `fixture_id`，再 GET `http://127.0.0.1:8799/__fixture` 核对同一个值，同时确认启动进程仍在运行。标识不匹配、接口不存在或进程退出时，立即停止该次验证，不向此端口发送扫描请求，也不能仅因 `/api/status` 返回 200 就判冒烟成功。这一身份端点仅存在于夹具，不属于产品 API。

身份确认后浏览器访问 `http://127.0.0.1:8799`，走总览 → 变化 → 扫描 → 对比 → 分布 → 子目录 → 返回。检查负号/未知、SVG 按钮、快照 IDs 更新和失败反馈。不要点 Finder；需要检查请求时 mock 系统动作。Ctrl-C 退出后临时目录自动释放。

在夹具里省略 seed 循环得到空库，保留一次得到单快照。500/延迟/响应乱序用可控测试服务/拦截器模拟，不能断开生产服务。截图只用合成数据，记录视口尺寸和关键 DOM 断言。

## 3. 必须覆盖的回归场景

| 层 | 代表输入 | 检查真实结果 |
|---|---|---|
| 采集 | 普通目录、空目录、中文/tab/换行/反斜杠名称、fatal exit、权限缺口 | 实际 du 路径与大小、质量/耗时；失败保留旧快照 |
| SQLite | 当日替换、旧 schema、迁移失败、磁盘写失败、两个根 | 事务回滚、外键一致、无跨根淘汰、备份可恢复 |
| 差分 | 11→9 MiB、9→101 MiB、权限减少、移除、单链 topn=1、重叠父子 | “未记录”不被当确定删除，净变化口径与证据可解释 |
| 运行生命周期 | 空库首扫、次日扫描、报告失败、两个独立触发进程、owner 被终止 | 持久状态、有效 snapshot_id、唯一 du、退出后资源回收 |
| API | 非法 ID/日期/limit/根、外站 Origin/Host、越界路径、非对象 JSON | 正确 4xx；被拒绝请求无系统副作用；500 不伪装空结果 |
| 查询资源 | 超 limit 历史、超过树预算、并发大文件查询、超时/取消 | 最新窗口、明确截断、可控 IO/内存/线程数 |
| 浏览器 | 空/单/双快照、只有新增、缺失点、重扫、切页/乱序、HTML 外观路径 | 无注入/旧值/未处理异常；正确状态与按钮反馈 |
| UX | 980×640、1220×820、1440×900；长路径；键盘导航 | 不溢出、图表非零尺寸、焦点可见/返回、三步定位 |

修复首先补能失败的反例测试；禁止 `or True`、只断言执行未抛错、全路径 mock 后称端到端。改变一个纯文案/间距不必堆单元测试，DOM/截图核对即可。核心行为改变必须测试失败路径。

## 4. 桌面与分发矩阵

| 验收入口 | 必须执行 | 不足以替代的证据 |
|---|---|---|
| Tauri 开发壳 | 启动窗口，实测 tray 数量/图标/标题/菜单/隐藏与恢复、remote IPC | cargo build、JS mock |
| 新账户安装 | 无开发工具安装，断网首启，部分权限首扫，第二日对比 | 开发机 cargo run |
| 后台服务 | 登录、退出 UI、休眠错过计划、重启中断、拒绝/撤销权限 | 只读 plist 文本 |
| 升级/恢复 | N→N+1、迁移失败、空间不足、备份恢复、版本不兼容 | 新库安装成功 |
| 卸载/重装 | 停止并移除自己服务，默认保留历史，重装接回 | 删除 .app 图标 |
| 公开下载包 | 声明的 CPU/最低及当前支持 OS，签名/公证/校验，真实下载后启动 | 本机未隔离的 unsigned 包 |

不随意选择生产环境跑这些步骤。没有测试账户/机器/签名凭据时保留未勾项并写 NOT_VERIFIED；其他独立任务照常推进。UI 最小窗口通过不代表手机布局已支持。

## 5. 证据格式与收口

任务卡证据至少写：`基线 commit；受测环境/视口；代表命令或交互；关键断言及结果；NOT_VERIFIED 范围；PR/产物位置`。只汇总与任务直接相关的结果，运行数据和长日志留本地，PR 用匿名摘要。

纯规划/文档 PR：检查 Markdown 链接、源文件引用、任务编号唯一、依赖无环、READY 条件、路线/任务/设计一致；核对现状语句与代码。不得因为描述了目标就把对应功能标完成。

持续集成由 ISS-031 建立，当前没有仓库 CI。发布前所需自动检查与真实矩阵都通过后才能请用户批准发布；缺少自动检查不能被理解为“全部通过”。
