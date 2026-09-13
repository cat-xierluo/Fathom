# ISS-029 技术验证发现与选型建议

状态：当前宿主（arm64）验证完成；冻结冒烟被安装授权阻塞
日期：2026-09-13 · 会话 fathom-release-iss-029
基线：`origin/main@50a87d9d6b3e5da6adf326622a9730c9ba429533`
证据：[results/](results/)（experiment 21/21 PASS；smoke BLOCKED_DEPENDENCY）
范围声明：本文只做当前宿主技术验证，不实现 ISS-009/010/040/041；
共享文档（TASKS/DECISIONS/ARCHITECTURE 等）由 PM 独占回写（见 §10 草案）。

## 1. 结论摘要

1. **冻结方式建议锁定 PyInstaller（onedir）**，pin `pyinstaller==6.22.3`
   （依据见 §5 与 requirements-runtime-build.txt）。本机当前未安装且
   policy=deny_by_default，冒烟按合同 fail closed（exit 3），未安装、
   未冒充自包含。
2. **发行 helper 的进程合同已在本机 arm64 用纯 stdlib 原型完整证明**
   （21/21 断言，results/experiment-20260913T044621Z.json）：身份/版本面、
   health+Host 守卫、回环端口+让位、未知占用零击杀、单一所有者、
   结构化退出码、崩溃 stale 接管、含空格/中文/& 路径、资源只读、全回收。
3. **生产 fathom 包直接冻结存在 7 个缺口（G1-G7，§3）**，其中 G1/G2/G6
   是阻塞项：不改生产代码就无法产生合规冻结产物。生产修改不在本卡授权
   写入范围，已整理为 ISS-009 前置改造清单交 PM 排期。

## 2. 主机环境证据（experiment results env 块）

- 架构 `arm64`（uname -m），CPU Apple Silicon（详见 results env.cpu）
- macOS 版本/build：见 results env（sw_vers）
- python3：3.14.6 @ `/opt/homebrew/bin/python3`（Homebrew framework，
  base_prefix=/opt/homebrew/opt/python@3.14/Frameworks/...）
- PyInstaller / Nuitka：均未安装（env.pyinstaller_import=not-installed）
- 工具齐备：file/curl/lsof/codesign/otool/pgrep/shasum
- 本机 7952（生产开发端口）冒烟时未被占用（smoke 日志无占用记录）；
  合同实验全程使用独立端口段 17963-17967

## 3. 生产 helper 冻结缺口清单（审查基线 50a87d9d）

| # | 缺口 | 位置 | 影响 | 证据状态 |
|---|---|---|---|---|
| G1 | `uvicorn.run("fathom.api:app", ...)` 字符串导入不被 PyInstaller 静态分析 | `fathom/cli.py` cmd_serve | 冻结产物 serve 启动即 `ModuleNotFoundError: fathom.api`；需 `--hidden-import fathom.api` 或改为对象导入 `uvicorn.run(api.app)` | 分析确认；冻结反例已写入 smoke 脚本（freeze A/B 对比），待授权后执行 |
| G2 | 运行时目录跟着 `PROJECT_ROOT=Path(__file__).parent.parent` 走，冻结后在 bundle 内创建 `data/reports/logs` | `fathom/config.py` | 违反「app 资源只读、用户数据写 Application Support」；`FATHOM_DB` 只隔离 DB 不隔离三个目录 | 分析确认；smoke 已内置运行后指纹断言，待授权后执行 |
| G3 | CLI 无 `--version`/身份面 | `fathom/cli.py` | app 壳无法在启动前后校验 helper 身份与版本（发行方案要求）；smoke 断言冻结产物 `--version` 退出码 2 | 分析确认 |
| G4 | API 无 `/health` 身份端点：`/api/status` 不含 service/protocol_version | `fathom/api.py` | 端口被占时无法做「同服务身份探测」，只能 HTTP 200 猜测——发行方案明令禁止 | 分析确认 |
| G5 | 版本三处不一致：`fathom/__init__.py` 0.1.0、`api.py` FastAPI(version="0.2.0")、发行目标 v0.3 | 同左 | 单一版本源要求（发行方案 §6）无法满足 | 分析确认 |
| G6 | 固定端口 7952，无让位/探测/零击杀语义 | `fathom/config.py` PORT、api 守卫 | 端口冲突时要么启动失败要么误连他人服务；发行必需语义见 §4 原型 | **原型已证明语义可行**（C5/C6/C7） |
| G7 | 后台唯一所有者未定义（开发 launchd 标签 vs 发行 app 派生） | `fathom/launchd.py`、发行方案 §4 | 双实例/孤儿进程风险；归 ISS-010 决策 | **原型已证明退出码 4 单所有者语义** |

## 4. 合同原型（helper_contract.py）已验证语义

- 身份：`--version` 单行 JSON（service/protocol_version/helper_version/
  python/machine/exe）；app 壳唯一的静态身份面。
- 端口：默认 7963 + `--port-range N` 依序让位；仅绑 127.0.0.1（C2 断言
  非 0.0.0.0）；候选被「非本服务」占用→记录 pid 并让位（C5）；全部被占
  →退出码 3、零击杀、不写 discovery（C6）。
- 单一所有者：探测到同 service+协议的健康实例→第二实例退出码 4（C7）。
- health：`GET /health` 200 JSON 含身份/pid/port/uptime；Host 非 loopback
  别名→403（与生产 api.py 守卫同构，C2）。
- 受控停机：`POST /shutdown` 需 discovery 文件内随机 token，错令牌 403；
  SIGTERM/SIGINT→优雅退出 0 并删 discovery（C3/C4）。
- 崩溃语义：SIGKILL→shell 137、discovery 残留；下次启动按 pid 存活检测
  接管同端口并轮换 instance_id（C8a/C8b）——app 壳崩溃恢复依据。
- 数据边界：唯一写入 `--data-dir`（必填、无 HOME 默认）；含空格/中文/&
  的可执行目录全文指纹零变化（C9）；结束后进程/端口零残留（Z1）。
- 退出码合同：0 优雅 / 2 用法 / 3 端口耗尽 / 4 单所有者 / 70 内部 /
  137 被杀。

## 5. 冻结方式对比与推荐

| 方案 | 双架构原生构建 | 签名/公证 | 只读 bundle | 启动 | 评估 |
|---|---|---|---|---|---|
| **PyInstaller onedir**（推荐） | 官方明确非交叉编译，arm64/x86_64 各自原生 runner 构建（与发行方案一致） | 可 nested codesign（binary → _internal → app），hardened runtime 兼容 | 资源可全只读；无运行期解包 | 快（无解包） | pin 6.22.3；G1 需 hidden-import/改对象导入；CI 用 python.org 解释器（setup-python），Homebrew python 仅限本机合同冒烟 |
| PyInstaller onefile | 同上 | 单文件签名简单 | 首启解包到 /var/folders（写临时区） | 慢（每次首启解包） | 备选；升级 delta 大、首启离线性差于 onedir |
| Nuitka | 原生编译 | 同样可签 | 同 onedir | 快 | 编译链重、构建时长高、对 fastapi/uvicorn 生态 hook 成熟度不如 PyInstaller；不建议首个发行版引入 |
| 内嵌 python.org framework | 手工布局 | 可行但要自管 sys.path/入口 | 可 | 快 | 等价于 PyInstaller 内部产物的手工版；维护面大，无收益 |
| Briefcase | 包装打包流程 | — | — | — | 项目结构改造大，不采用 |

版本依据（2026-09-13 PyPI 官方元数据）：pyinstaller 6.22.3
`requires_python ">=3.8,<3.16"`、官方描述支持 Python 3.8-3.15 与
macOS 10.15+；fastapi 0.141.1 / uvicorn 0.52.4 均 `>=3.10`（本机
3.14.6 满足）。ISS-031 在 CI 锁定最终解释器版本时应连同传递依赖
`pip freeze` 出 lock 快照作为冻结证据。

## 6. 端口策略提案（语义已验证，具体数值归 PM）

- 固定默认端口（沿用 7952 或另选）+ 小范围让位段；仅 loopback。
- 端口占用时先做**身份探测**（/health 同 service+协议版本才算自己人），
  绝不「HTTP 200 就当自己人」，绝不杀未知占用进程。
- discovery 文件（Application Support 数据根内）承载 port/pid/token/
  instance_id；app 壳读它完成启动握手与受控停机。
- 单一所有者：第二实例退出码 4；崩溃由 pid 存活检测接管。

## 7. 服务唯一所有者与 TCC 授权主体方向（决策归 PM/ISS-010）

- 方案 A（本实验已证进程语义）：app 派生 helper 子进程，app 退出→helper
  随之收尾；discovery+token+SIGTERM 链路即受控生命周期。TCC 责任进程
  归属 app（子进程的授权弹窗归属父 app），符合「授权主体=安装的 .app」。
- 方案 B：SMAppService 注册 login item 常驻 agent（本任务禁注册，未验证）。
  适合「app 不在也要定时扫描」的现有产品语义；但 TCC 主体、卸载注销、
  双架构 plist 都要额外验证（归 ISS-010）。
- 建议：v0.3 先 A（最小可分发闭环），B 作为 ISS-010 演进；DEC 草案见 §10。

## 8. 跨架构复验计划（x86_64，归 ISS-041 原生 runner）

1. runner：GitHub macOS x86_64 原生 runner（发行方案已明确不得交叉冻结）。
2. 同 pins（requirements-runtime*.txt）+ python.org 解释器（setup-python）。
3. 先给 `scripts/build_helper_experiment.sh` 的 arm64 门禁加
   `FATHOM_ISS029_EXPECT_ARCH=x86_64` 覆盖（当前按本卡合同 fail closed
   仅放行 arm64，属预期行为，需 ISS-041 任务卡内修改）。
4. 依序执行两个脚本；冻结产物断言 `file`（Mach-O x86_64、无 arm64）、
   `otool -L`、`lipo -info`；干净无 Python/Homebrew 账户、断网首启、
   quarantine 保留启动（后者需签名公证链路，随 ISS-041 CI）。
5. 证据回写 ISS-041 任务卡；在本卡 TASKS 中仅登记指针。

## 9. 阻塞与 NOT_VERIFIED

- **BLOCKED**：PyInstaller 安装授权（policy=deny_by_default；精确请求见
  `results/smoke-*.log` REQUEST 块，命令/版本/依据/环境/磁盘/清理齐备）。
  授权前不产生任何「已冻结」声明。
- NOT_VERIFIED：冻结产物 file/otool/断网首启/无 Python 账户；G1/G2 的
  运行态实证（反例脚本就绪）；新账户/干净机；TCC/SMAppService/launchd
  注册；x86_64；签名公证（无证书材料，DEC-008）。
- 冒烟端口说明：生产 serve 只绑 7952（G6），冒烟已内建「被占即不杀、
  转 PARTIAL_BLOCKED_PORT_7952」分支，避免与开发 launchd 服务互踩。

## 10. DEC 草案（供 PM 录入 docs/DECISIONS.md）

> **DEC-0xx（草案）· 发行 helper 冻结与进程合同（ISS-029，2026-09-13）**
> 采纳 PyInstaller onedir（pin 6.22.3，目标架构原生构建）作为 v0.3.x
> helper 冻结方式；helper 进程合同按 iss029 原型固化：--version 身份面、
> /health 身份探测、回环默认端口+让位段、未知占用零击杀（退 3）、单一
> 所有者（退 4）、token 受控停机与 SIGTERM/SIGINT 退 0、崩溃 stale 由
> pid 存活检测接管、数据仅写 Application Support 数据根。
> 生产改造前置（ISS-009 前完成）：G1 hidden-import/对象导入、G2 冻结
> 感知数据根（建议 FATHOM_DATA_ROOT，三目录+DB 同源）、G3/G4 身份面、
> G5 单一版本源、G6/G7 按上述合同实现。所有者先取 app 派生（方案 A），
> SMAppService 常驻归 ISS-010 重估。重评条件：PyInstaller 在目标
> Python/macOS 组合失支持、或公证链路对 onedir 布局提出新约束。
