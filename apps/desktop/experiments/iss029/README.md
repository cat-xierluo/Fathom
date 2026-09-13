# ISS-029 自包含运行时实验（当前宿主 arm64）

状态：实验已执行（无安装部分）；冻结冒烟被依赖授权阻塞（BLOCKED_DEPENDENCY）
基线：`origin/main@50a87d9d6b3e5da6adf326622a9730c9ba429533`
结论与缺口清单见 [findings.md](findings.md)。本目录**不是生产代码**：
生产 helper 仍是仓库根 `fathom/` 包；这里只做发行合同的「先行证明 + 反例」。

## 怎么跑

```sh
bash scripts/build_helper_experiment.sh   # 无安装合同实验（零第三方依赖，21 项断言）
bash scripts/build_helper_smoke.sh        # 冻结冒烟（未获安装授权时 exit 3 并打印精确请求）
```

两个脚本均 fail closed：任一必选断言失败即非零退出；smoke 在
`INSTALL_AUTHORIZATION.json` 未精确授权安装命令时**不安装任何依赖**。

## 文件

| 文件 | 职责 |
|---|---|
| `helper_contract.py` | 合同原型 helper（纯 stdlib）：`--version` 身份、`/health`、回环端口+让位、未知占用不杀、结构化退出码、discovery 文件、崩溃接管 |
| `freeze_entry.py` | 冻结冒烟入口（包装生产 `fathom.cli`；仅实验用） |
| `results/experiment-*.json/.log` | 合同实验证据（最新一轮 21/21 PASS，2026-09-13） |
| `results/smoke-*.json/.log` | 冻结冒烟证据（当前轮 verdict=BLOCKED_DEPENDENCY） |
| `build/`、`.venv-build/` | 构建产物与 task-local venv（gitignore，不入库） |

## 已证明（当前 arm64 宿主）

- 合同原型 21/21 断言通过：身份/版本、health+Host 守卫、回环监听、
  令牌停机、SIGTERM/SIGINT 优雅退出、未知占用让位且零击杀、全占用退 3、
  单一所有者退 4、SIGKILL 崩溃留 stale 并被接管、含空格/中文/& 路径、
  资源目录零写入、进程/端口全回收。
- 冒烟脚本的 fail-closed 行为：未授权时 exit 3 并输出精确安装请求。

## 未证明（NOT_VERIFIED）

- PyInstaller 冻结产物本身（`file`/`otool`/断网首启/无 Python 账户）：
  安装授权未批，见 smoke 结果与 findings §9。
- x86_64 架构（归 ISS-041 原生 runner 复验）。
- 新账户、TCC/SMAppService/launchd 注册（本任务禁动，方向见 findings §7）。
