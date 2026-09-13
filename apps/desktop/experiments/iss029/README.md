# ISS-029 自包含运行时实验（当前宿主 arm64）

状态：无安装合同实验 18 pass / 0 fail / 0 blocked；冻结冒烟——11 pass /
0 fail / 3 项 blocked（生产端口 7952 被未识别进程占用，未杀占用者，G6 实证）
基线：`origin/main@50a87d9d6b3e5da6adf326622a9730c9ba429533`
结论与缺口清单见 [findings.md](findings.md)。本目录**不是生产代码**：
生产 helper 仍是仓库根 `fathom/` 包；这里只做发行合同的「先行证明 + 反例」。

## 怎么跑

```sh
bash scripts/build_helper_experiment.sh   # 无安装合同实验（零第三方依赖，18 项断言）
bash scripts/build_helper_smoke.sh        # 冻结冒烟（未授权时 exit 3 打印精确请求；授权后冻结+断言）
```

两个脚本均 fail closed：任一必选断言失败即非零退出；smoke 在
`INSTALL_AUTHORIZATION.json` 未精确授权安装命令时**不安装任何依赖**。

## 文件

| 文件 | 职责 |
|---|---|
| `helper_contract.py` | 合同原型 helper（纯 stdlib）：长期 `flock`、`--version`、`/health`、回环端口、0600 discovery、结构化退出与崩溃接管 |
| `verify_contract.py` | 合同验证器：隔离临时根、并发启动、进程身份跟踪、原子文件与泄漏反例 |
| `freeze_entry.py` | 冻结冒烟入口（包装生产 `fathom.cli`；仅实验用） |
| `results/` | 默认运行证据；由本目录版本化 `.gitignore` 忽略，原始日志不提交 |
| `build/`、`.venv-build/` | 构建产物与 task-local venv（gitignore，不入库） |

## 已证明（当前 arm64 宿主）

- 合同原型 18 项断言通过：health 的版本与实例身份、0600 原子 discovery、
  令牌不进入 stdout/stderr、SIGTERM/SIGINT、未知占用 PID 前后一致、
  8 实例并发只有一个 owner、SIGKILL 后接管、损坏记录接管、替换记录不误删、
  含空格/中文/& 路径，以及按 Popen+启动身份精确回收子进程。
- 冒烟 fail-closed 行为（授权前两轮 exit 3 + 精确安装请求）。
- **授权轮冻结证据**：两次 onedir 冻结（A 无 / B 带 `--hidden-import
  fathom.api`）；`file` = Mach-O 64-bit executable arm64；`otool -L` 清单；
  pip freeze 锁快照；G1 字符串导入反例（A 失败/B 通过）、G3 `--version`
  退 2、G2 冻结树内生成 `{data,logs,reports}`、G6 绑定失败且占用者未受
  影响——全部运行时实证（详见 findings §3）。

## 未证明（NOT_VERIFIED / blocked）

- 冒烟 3 项（健康 serve、冻结产物 Host 守卫、冻结产物 SIGTERM）：7952 被
  未识别进程占用，按合同不杀不碰；G6 修复后重跑 smoke 即可补齐。
- 断网首启、无 Python/Homebrew 的干净账户（本卡未授权新账户/断网操作）。
- x86_64 架构（归 ISS-041 原生 runner 复验）。
- TCC/SMAppService/launchd 注册（本任务禁动，方向见 findings §7）。
