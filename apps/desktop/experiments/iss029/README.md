# ISS-029 自包含运行时实验（当前宿主 arm64）

状态：无安装合同实验 21/21 PASS；冻结冒烟已获 PM 授权执行——14 pass /
0 fail / 3 项 blocked（生产端口 7952 被未识别进程占用，未杀占用者，G6 实证）
基线：`origin/main@50a87d9d6b3e5da6adf326622a9730c9ba429533`
结论与缺口清单见 [findings.md](findings.md)。本目录**不是生产代码**：
生产 helper 仍是仓库根 `fathom/` 包；这里只做发行合同的「先行证明 + 反例」。

## 怎么跑

```sh
bash scripts/build_helper_experiment.sh   # 无安装合同实验（零第三方依赖，21 项断言）
bash scripts/build_helper_smoke.sh        # 冻结冒烟（未授权时 exit 3 打印精确请求；授权后冻结+断言）
```

两个脚本均 fail closed：任一必选断言失败即非零退出；smoke 在
`INSTALL_AUTHORIZATION.json` 未精确授权安装命令时**不安装任何依赖**。

## 文件

| 文件 | 职责 |
|---|---|
| `helper_contract.py` | 合同原型 helper（纯 stdlib）：`--version` 身份、`/health`、回环端口+让位、未知占用不杀、结构化退出码、discovery 文件、崩溃接管 |
| `freeze_entry.py` | 冻结冒烟入口（包装生产 `fathom.cli`；仅实验用） |
| `results/experiment-*.json/.log` | 合同实验证据（最新 20260913T050439Z，21/21 PASS） |
| `results/smoke-*.json/.log` | 冒烟证据：授权前 fail-closed 两轮（044957Z/045329Z）+ 授权轮 PARTIAL_BLOCKED_PORT_7952（050248Z，含 file/otool/freeze.lock） |
| `build/`、`.venv-build/` | 构建产物与 task-local venv（gitignore，不入库） |

## 已证明（当前 arm64 宿主）

- 合同原型 21/21 断言通过：身份/版本、health+Host 守卫、回环监听、
  令牌停机、SIGTERM/SIGINT 优雅退出、未知占用让位且零击杀、全占用退 3、
  单一所有者退 4、SIGKILL 崩溃留 stale 并被接管、含空格/中文/& 路径、
  资源目录零写入、进程/端口全回收。
- 冒烟 fail-closed 行为（授权前两轮 exit 3 + 精确安装请求）。
- **授权轮冻结证据**：两次 onedir 冻结（A 无 / B 带 `--hidden-import
  fathom.api`）；`file` = Mach-O 64-bit executable arm64；`otool -L` 清单；
  pip freeze 锁快照；G1 字符串导入反例（A 失败/B 通过）、G3 `--version`
  退 2、G2 冻结树内生成 `{data,logs,reports}`、G6 绑定失败且占用者未受
  影响——全部运行时实证（详见 findings §3）。

## 未证明（NOT_VERIFIED / blocked）

- 冒烟 3 项（健康 serve、冻结产物 Host 守卫、冻结产物 SIGTERM）：7952 被
  pid 6026 占用，按合同不杀不碰；G6 修复后重跑 smoke 即可补齐。
- 断网首启、无 Python/Homebrew 的干净账户（本卡未授权新账户/断网操作）。
- x86_64 架构（归 ISS-041 原生 runner 复验）。
- TCC/SMAppService/launchd 注册（本任务禁动，方向见 findings §7）。
