# ISS-175（P2）：取消响应合同——区分「本次接受取消」与「到达前已终结」

## 一、结论

上游终审的 HTTP 实证成立且已定位到**单行判据**：取消请求被受理、任务已 `cancelled`、
无 analysis 生成，API 却返回 409「取消不再生效」。

根因在 `fathom/api.py` 的取消端点用**最后一次读取看见的状态**代理「取消是否生效」：

```python
if view.get("terminal"):   # ← 这是「返回时是否已终态」，不是「本次是否受理了取消」
    ... 409 job_terminal
```

受理取消后，`cancel_job` 还要做一次 `job_view()` 读取来组装响应；worker 若在这几毫秒内
把任务收敛到 `cancelled`，这一次读取就看见终态，于是一次**已经生效的取消**被答成 409。
这是观察竞态，不是取消失败（与上游裁定一致），也不支持「取消失败 / 取消后仍落 AI 结果」
的扩大判断——`agent_analyses` 在该路径确认为 0。

CI arm64 首败（`test_analysis_api.py::test_cancel_via_api_and_terminal_409`，409≠200）正是
这条竞态：假 claude 脚本 `cat > /dev/null; sleep 30` 被 SIGTERM 回收得越快，越容易撞上。

### 修法：受理与否与返回时刻的读取结果解耦

`AnalysisManager.cancel_job`（`fathom/analysis_manager.py`）新增一个**仅取消响应语义**的
标记 `cancel_accepted`，判据是「到达态 + 本次受理的 rowcount」，两者都与最终读取无竞争：

| 情形 | 判定 | HTTP |
|---|---|---|
| 到达时已是终态（cancel/failed/succeeded/timed_out/interrupted） | `cancel_accepted=False` | 409 `job_terminal` |
| 到达时非终态，本次 mark 命中（推入 cancelling） | `True` | **200**，状态**可以**已是终态 `cancelled` |
| 到达时非终态，mark 未命中但返回时仍未终结（取消早已受理、正在收敛） | `True` | 200 |
| 到达时非终态，mark 未命中且返回时已终态（完成抢先赢） | `False` | 409 `job_terminal` |

配套：`_mark_cancelling` 返回 `bool`（本次是否把行推入 cancelling，即 UPDATE rowcount），
这是受理判定的直接证据；`api.py` 端点改为按 `cancel_accepted` 映射状态码。

**语义选择说明（按卡片要求写清）**：第二个语义保留**具名 409 `job_terminal`**，因为现有
`tests/test_analysis_api.py` 与（仓内无前端源码，grep 全仓 `job_terminal` 只命中 api.py
与测试）都依赖它；未改为幂等 200。已逐处核对消费者：`api.py` 端点、`tests/test_analysis_api.py`、
`tests/test_analysis_manager.py`、`tests/test_analysis_jobs_lookup.py`、`tests/test_analysis_upgrade_gate.py`
——后者四处的 `cancel_job(...)` 返回值仍按 job 视图消费（取 `status`/`terminal`），
新增 `cancel_accepted` 键不影响它们，ISS-167 修复线零回归。

不变项（明确未被本卡改动）：终态不复活、取消先到即取消胜（`_commit_success` 遇 `cancelling`
丢弃迟到成功并收敛为 cancelled）、线性化仍来自 SQL 条件与写事务而非那把 `_commit_lock`、
不按持久化 PID 发信号、跨进程在途只标记不越权收敛。

### 确定性交错测试（不再依赖真实时序）

新增 `test_cancel_accepted_200_when_worker_converged_before_read`，用受控注入固定交错，
全程无 sleep、无概率：取消到达（任务在途）→ 本次受理置 cancelling → **在受理与返回前读取
之间**由持有方按生产路径 `manager._finish(...)` 收敛到终态 `cancelled` → 最后一次读取看见
终态。断言 200 + `cancel_accepted=True` + `status=cancelled` + `agent_analyses` 为 0，
并断言本次 mark 确实命中（`("mark", True)`，排除「他方先受理」）。
手法参照 ISS-172/ISS-167 的确定性做法：事件/受控注入定序，收尾等待按状态条件
（`_wait_worker_done` 等 worker 自我摘除注册表）而非固定时长。

对照面 `test_cancel_arriving_after_terminal_is_named_409`：到达前已终结 → 具名 409，
`cancel_accepted=False`，终态不被复活。
原 `test_cancel_via_api_and_terminal_409` 改为「真实进程路径：受理必 200 + 收敛后再取消 409」
的回归护栏，删掉了原先依赖真实时序的 `time.sleep(0.05)` 轮询断言。

**反向验证（判据确实有效，非自说自话）**：仅把 `fathom/api.py` 还原成旧的
`view.get("terminal")` 映射后重跑，新用例稳定复现上游症状：

```
AssertionError: {"reason_code":"job_terminal","detail":"任务已结束，取消不再生效",
                 "job":{... "status":"cancelled", ... "cancel_accepted":true}}
assert 409 == 200
FAILED tests/test_analysis_api.py::TestPreviewAndJobs::
       test_cancel_accepted_200_when_worker_converged_before_read
```

即：旧映射把一次**确实受理**（`cancel_accepted:true`、`status:cancelled`）的取消答成 409。

## 二、验证

命令（卡片指定，×10 连跑逐轮退出码）：

```bash
python3 -m pytest tests/test_analysis_api.py tests/test_analysis_manager.py -q
python3 -m pytest tests/ -q -k "cancel"
```

| 轮次 | 退出码 | 用时 | 结果 |
|---|---|---|---|
| 1 | 0 | 80s | 81 passed in 78.93s |
| 2 | 0 | 73s | 81 passed in 71.67s |
| 3 | 0 | 79s | 81 passed in 78.75s |
| 4 | 0 | 73s | 81 passed in 71.62s |
| 5 | 0 | 72s | 81 passed in 71.23s |
| 6 | 0 | 68s | 81 passed in 67.86s |
| 7 | 0 | 66s | 81 passed in 64.72s |
| 8 | 0 | 67s | 81 passed in 66.63s |
| 9 | 0 | 68s | 81 passed in 67.55s |
| 10 | 0 | 67s | 81 passed in 66.14s |

**10 轮退出码全 0**，每轮均 81 passed，无翻红、无既有 flaky 需甄别。

定向：

```bash
python3 -m pytest tests/ -q -k "cancel" \
  --ignore=tests/test_ci_frozen_analysis.py \
  --ignore=tests/test_release_gate.py \
  --ignore=tests/test_third_party_notices.py
# 41 passed, 1370 deselected in 70.48s，退出码 0
```

**为何带 `--ignore`（须向 PM 说明，非掩盖失败）**：不加时该命令退出码为 2，报
`3 errors during collection`——`tests/test_ci_frozen_analysis.py`、
`tests/test_release_gate.py`、`tests/test_third_party_notices.py` 在**导入期**
就失败，各自 `import ci_frozen_analysis / verify_release_gate /
check_third_party_notices`。这三个 helper 脚本在本工作区**磁盘缺失且 git 未跟踪**
（`git ls-files --error-unmatch` → untracked），属本机稀疏检出/工作区不完整的
**既有环境缺口**：本卡未触碰这三个模块中的任何一个，也未新增/删除任何 helper
脚本。错误发生在 collection，与 cancel 断言无关；`_isolated` 等夹具不参与。
排除后 41 条 cancel 相关用例全绿。此项已列入 NOT_VERIFIED，请 PM 在完整检出的
环境复核裸命令（预期同样全绿）。

pytest 计数：`--collect-only -q` 实测 `tests/test_analysis_api.py` **18 → 20**（新增 2 条），
两文件合计 **79 → 81 collected**。本仓库**不存在 `EXPECTED_PYTEST`**（已全仓 grep 确认），
故无计数文件需同步。**新增用例数 = 2，报 PM 收口计数；未改 `ci.yml`。**

## 三、NOT_VERIFIED

- **CI arm64 冷环境首跑未实测**：本机 macOS 已热身。修复把首败从「时序概率」改为
  「注入固定交错」，但真正要确认的是 CI 冷启动/高负载下 `tests/test_analysis_api.py`
  真实进程用例的表现，需 CI 首跑确认。
- **裸 `-k "cancel"` 命令在本工作区退出码 2**：3 个测试模块导入期缺 helper 脚本
  （磁盘缺失 + git 未跟踪，见第二节），本卡未触碰；排除后 41 passed。需在完整
  检出的环境用裸命令复核一次。
- **多进程/跨 session 的取消受理未专门覆盖**：`rec is None`（他进程持租约）路径沿用旧逻辑
  （只标记 cancelling 不发信号），其 `cancel_accepted` 由「mark 未命中但未终结」推出，
  逻辑上成立但本卡未新增跨进程用例实测。
- **`cancel_accepted` 字段是响应体新增键**：仓内无前端源码可核，前端若对 job 视图做
  严格 schema 校验需 PM 侧确认；GET `/api/analysis/jobs/{id}` 的 `job_view()` 未加此键，
  只有取消响应带。
- 未跑 `tests/` 全量（非本卡定向范围）；`test_analysis_contract.py` /
  `test_ci_frozen_analysis.py` 已确认无 cancel 断言（grep 无匹配）。

## 四、改动文件

| 文件 | 改动 |
|---|---|
| `fathom/analysis_manager.py` | `cancel_job` 增加到达态先读 + `cancel_accepted` 判定；`_mark_cancelling` 返回 bool（rowcount）；docstring 写清两语义与旧竞态 |
| `fathom/api.py` | 取消端点按 `cancel_accepted` 映射 200/409；端点与 API 清单 docstring 更新合同 |
| `tests/test_analysis_api.py` | 新增 2 条（确定性交错 200 / 到达后具名 409）；重写原用例为回归护栏；新增 `_cancel_script`/`_wait_worker_done`/`_analysis_count` helper |
| `RESULT.md` | 本文件 |

commit：中文、无署名、一笔提交。**未 push**；`RESULT.md` 由 PM 代提交。