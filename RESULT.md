# ISS-169 A 组交付：时序脆弱测试确定性化——agent 运行时族 10 项

基线：main `9ab8fd7`。分支 `iss-169-timing-group-a`，未 push（PM 代推）。

沿 ISS-165/166/167 同法：只放大**等待预算**与**轮询结构**，不改断言语义、不增删用例。

## 一、逐项处理表（10/10 全部落地）

> 行号说明：任务卡行号取自审计快照，与本分支实际行号有偏移（ISS-165/166/167 合并后行号位移）。下表「审计行号」照录卡片，「实际行号」为本分支改前真实位置。**写域外零改动**，`fathom/` 生产代码全程只读。

| # | 审计行号 | 实际位置 | 现状 | 改法 | 放大后值 |
|---|---|---|---|---|---|
| 1 | seam `:86` | `test_agent_supervisor_seam.py:86` | `assert wall < 6` | 提到 ≥10s，对齐同文件 `:45` 的 15s 口径 | `assert wall < 15`（2.5x） |
| 2 | runtime `:299` | `test_agent_runtime.py:299` | `assert 0.5 <= wall < 5` | 上界与生产 10.0s 回收界限同档；**下界语义保留** | `assert 0.5 <= wall <= 10`（下界 0.5s 不变） |
| 3 | runtime `:360` | `test_agent_runtime.py:360` | `assert r.wall_ms < 10000` | 与生产 `proc.wait(10.0)` 同界限，改 `<=` 精确同档 | `assert r.wall_ms <= 10000` |
| 4 | runtime `:424` | `test_agent_runtime.py:424` | `assert r.wall_ms < 5000` | 上界放大 3x | `assert r.wall_ms <= 15000` |
| 5 | runtime `:266` | `test_agent_runtime.py:266` | `assert monotonic()-t0 < 10` | 放大 3x（该处 >20x 余量，审计标可选） | `assert time.monotonic() - t0 < 30` |
| 6 | runtime `:487-491` | `test_agent_runtime.py:487` | `for _ in range(100): sleep(0.05)` 轮询 pidfile（本文件最紧） | 改 deadline 轮询（与 `wait_terminal` 同法） | 上限 **20s**，失败文案同步 |
| 7 | runtime `:502-505` | `test_agent_runtime.py:502-505` | 8s 轮询上限 + `assert < 6` | 轮询上限与断言上限同步放大 | 轮询 **>20s** fail；`assert ... < 15` |
| 8 | runtime `:617/638/683/703` | 同左（4 处） | `<6/<6/<6/<5` probe 上限（4–12x） | 统一放大到同档 | 4 处统一 `<= 15` |
| 9 | analysis `:843` | **`test_analysis_manager.py:865`** | `assert monotonic()-t0 < 10` | 对齐其上方 `wait_terminal(15)` | `assert time.monotonic() - t0 <= 15` |
| 10 | analysis `:1120,1157` | **`test_analysis_manager.py:1142,1179`** | `time.sleep(0.3)  # 进入 running`（等状态非等稳定） | 改条件等待轮询 `status == "running"` | 新增 `wait_running()`，上限 **15s** |

### 关键取舍说明（3 处值得 PM 复核）

1. **第 9 项行号漂移**：卡片 `:841` 的 `wait_terminal(15)` 在本分支是 `:863`，卡片 `:843` 的 `< 10` 断言实际在 `:865`。按语义（同函数内 `wait_terminal(timeout=15)` 之下的 10s 上界）定位，改的是 `test_timeout_marks_timed_out`。取 `<= 15` 而非 `< 15`：`wait_terminal` 的循环条件是 `while monotonic() < deadline`，末次迭代在 15s 前进入并可能跨过 deadline 才返回，用 `< 15` 会留下约 50ms 的假翻红缝。
2. **第 2/3/4/8 项用 `<=`**：与生产界限精确同档（`fathom/agent_runtime.py:818 proc.wait(timeout=10.0)` 是 reap 硬界限，实际墙钟可贴等号）。`< 10` 会把合法的 reap 路径判成超时——这正是审计指出的「上界窄于生产界限」病根。
3. **第 10 项是真修不只是放大**：`sleep(0.3)` 等的是时钟不是状态，冷启动下 job 还在 `starting`，撤销动作落在运行之前 → 用例测不到「运行中撤销」合同（**假绿**）。`wait_running` 等真实状态；已核 `running` 是生产真实状态（`fathom/analysis_manager.py:798` 做 `starting`→`running` 迁移，`CANCELLABLE_STATUSES` 亦含 `running`），上限到不了就抛断言而非静默通过，不吞真失败。

### 跳过项

无。A 组 10 项全部处理；B 组/C 组/前端 cjs 按卡片不在本批。已修/安全不动区（`runtime:165-188,363-409`、`analysis:777-829,798-843` 对应 ISS-142/ISS-124）全程未触碰。

## 二、验证（真实入口，全部实际执行）

```bash
python3 -m pytest tests/test_agent_runtime.py tests/test_agent_supervisor_seam.py tests/test_analysis_manager.py -q
```

×5 连跑，**逐轮退出码**：

| 轮次 | 退出码 | 用时 | 结果 |
|---|---|---|---|
| 1 | 0 | 77s | 160 passed in 76.02s |
| 2 | 0 | 81s | 160 passed in 78.76s |
| 3 | 0 | 72s | 160 passed in 70.50s |
| 4 | 0 | 77s | 160 passed in 76.89s |
| 5 | 0 | 82s | 160 passed in 79.95s |

**5 轮全 0，无翻红**，无既有 flaky 需甄别。

pytest 计数：`--collect-only -q` 实测 **160 collected**（改前同样 160），未增删用例。本仓库**不存在 `EXPECTED_PYTEST`**（已全仓 grep 确认），故无计数文件可同步——若有，PM 需在别的仓/分支核对。

## 三、NOT_VERIFIED

- **30 连跑归 PM**：本 worker 只做 ×5（退出码全 0），30 连跑按卡片约定归 PM。
- **CI 冷环境首跑未实测**：本机 macOS 已热身环境；真正要防的是 CI 冷启动/高负载假翻红，需 CI 首跑确认（本批改动即为其减轻）。
- **未做人为负载注入**：第 10 项 `wait_running` 的冷启动价值靠代码路径推理（`starting`→`running` 迁移存在）确认，未在真冷缓存/高 CPU 下构造压力场景实测。
- 生产代码 `fathom/` 全程只读、未改动；其余测试文件（B 组/C 组）未动。

## 四、改动文件

| 文件 | 改动 |
|---|---|
| `tests/test_agent_supervisor_seam.py` | 1 处断言上界 6→15s |
| `tests/test_agent_runtime.py` | 8 处：上界放大 4 处、`for range(100)` 轮询改 deadline（20s）、退出轮询 8→20s、probe 4 处统一 15s |
| `tests/test_analysis_manager.py` | 1 处断言 10→15s；新增 `wait_running()` helper；2 处 `sleep(0.3)` 改条件等待 |
| `RESULT.md` | 本文件 |

commit：A 组一批一笔提交。**未 push，PM 代推**。