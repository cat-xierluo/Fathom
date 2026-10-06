# ISS-173 交付结果：db.connect 快路径索引 DDL 的写锁竞争收口

- 分支：`iss-173-index-lock-coordination`（基线 `17c9df1`）
- 写域：`fathom/db.py`、`tests/test_trend_path_index.py`、本文件
- 采用方案：**方案 1（让步 + 待办登记 + 下次连接重试）**。方案 2（索引补建挪到扫描协调器写事务路径）需改 `fathom/scan_coordinator.py`，超出本卡写域，故未采用；其效果已由方案 1 的「迁移写事务内 strict 补建」等价覆盖（见下）。

## 1. 问题复现（上游反例，本机独立复现）

上游终审结论：`connect()` 快路径在旧库缺索引时执行 `CREATE INDEX IF NOT EXISTS`，而该 DDL 需要写锁。库被扫描写事务（`BEGIN IMMEDIATE`）持锁时，DDL 抢不到锁 → 等满 `busy_timeout`（10s）→ 抛 `database is locked` → 包装成 `DatabaseOpenError`，即**用户可见的打开失败**。

本机 1M entries 库标定（探针 [5/5] 步）：裸 DDL 在写锁下耗时 **10.39s 后失败**，与上游数字逐位吻合，确认竞争真实存在、让步分支必需。

## 2. 修法（`fathom/db.py`）

`_ensure_indexes()` 重写为三段式：

1. **纯读廉价检查** —— 查 `sqlite_master` 取已存在索引名，全部存在即零成本返回。WAL 下读者不被写事务阻塞，这一步永不触发写锁。
2. **短预算试探** —— 真要建索引时，先把该连接的 `busy_timeout` 压到 `_INDEX_PROBE_TIMEOUT_S = 0.25` 再执行 DDL，抢不到立即让步；`finally` 恢复调用方的原 `busy_timeout`，避免短试探污染后续用户查询。
3. **按路径分语义**：
   - `allow_defer=True`（两个打开路径：`connect()` 快路径、`_prepare_database` 的已是当前 schema 分支）：遇写锁竞争 → 登记待办 + 记一次日志 + 返回 `False`，**打开照常成功**，下次连接重试。
   - `allow_defer=False`（默认；迁移 `BEGIN IMMEDIATE` 事务内）：写锁已自持，竞争不可能发生，故原样上抛，fail-closed，绝不静默留下缺索引的库。

配套：

- `pending_index_builds()`：待补建登记簿快照（进程内 `set` + `threading.Lock`，诊断/测试用，不参与 schema 版本语义）。
- `_register_pending_index()`：登记待办并**每次竞争只记一次** warning（含索引名与原始错误）；`_clear_pending_index()` 在补建成功后清账。
- `_is_lock_contention` 判定用常量 `_LOCK_CONTENTION_HINT = "is locked"`：只对写锁竞争让步，语法错误/只读库/库损坏等 `OperationalError` 原样上抛，不许伪装成「已让步」。
- `_INDEX_STATEMENTS` → `_INDEX_DEFINITIONS`（`(索引名, DDL)` 配对），因为让步登记要按名字记账。该常量原为模块私有且无外部引用（已全仓核对）。

ISS-168 语义零退化：无竞争时仍当场建好索引（`test_normal_open_never_registers_pending_build` 钉住）。

## 3. 验证（真实执行，命令 + 退出码）

### 3.1 指定回归套件（连跑 3 次，全绿）

```bash
python3 -m pytest tests/test_db_migrations.py tests/test_trend_path_index.py -q
```

| 轮次 | 结果 | 退出码 |
| --- | --- | --- |
| 基线（改动前） | 40 passed | 0 |
| 第 1 次 | **47 passed** | 0 |
| 第 2 次 | **47 passed** | 0 |
| 第 3 次 | **47 passed** | 0 |

### 3.2 写锁反例探针（上游形态，退出码 0）

两个连接 + `BEGIN IMMEDIATE` 持锁 + 新 `connect()` 计时断言 < 1s，1M entries 库：

| 检查项 | 实测 | 结论 |
| --- | --- | --- |
| `connect()` 抛 `DatabaseOpenError`？ | 未抛 | ✅ |
| `connect()` 耗时 | **0.293s** < 1.0s | ✅ 旧实现约 10.39s 后失败 |
| 本次是否真让步（索引未建） | 未建 | ✅ 非「建得快」的假阳性 |
| 待补建已登记 | 是 | ✅ 下次连接重试 |
| 让步后读查询可用 | 1 行 `[(1, 0)]` | ✅ 查询不受影响 |
| 释放锁后重连补建 | 1.92s，索引到位 | ✅ |
| 登记簿已清空 | 是 | ✅ |
| 计划改走索引 | `SEARCH entries USING INDEX idx_entries_path (path=?)` | ✅ |
| 反例标定：裸 DDL 在写锁下 | **10.39s 后失败** | ✅ 与上游数字吻合 |

探针源文件：`/tmp/iss173_run/probe.py`（一次性脚本，不入仓）。1M 库规模刻意让建索引耗时（1.9–2.3s）显著超过 1s 阈值，因此「<1s 通过」只可能来自让步，不可能来自「索引建得快」。

### 3.3 全量回归

```bash
python3 -m pytest tests/ -q --ignore=tests/test_ci_frozen_analysis.py \
  --ignore=tests/test_release_gate.py --ignore=tests/test_third_party_notices.py
```

被忽略的 3 个模块是**改动前既已存在**的收集错误（缺 `check_third_party_notices` 等 helper 脚本）：已 `git stash` 到干净树复验，同样报同一 collection error，与本卡无关。

结果见下节。

## 4. EXPECTED_PYTEST 数字（供 PM 收口）

- **新增用例：7 条**（全在 `tests/test_trend_path_index.py`，`test_db_migrations.py` 未改）
- 套件总数：40 → **47**（`test_trend_path_index.py` 8 → 15，`test_db_migrations.py` 31 不变）
- 7 条清单：
  1. `test_bare_index_ddl_under_write_lock_is_blocked` —— 反例自证：裸 DDL 确实抢不到写锁（让步分支非防御性空转）
  2. `test_connect_succeeds_quickly_when_index_build_loses_write_lock` —— 核心红线：持锁 + 缺索引 → 快速成功，且让步后查询可用
  3. `test_lost_index_build_is_logged_once_with_diagnosis` —— 失败诊断：只记一次 warning，含索引名 + 原始错误
  4. `test_pending_index_build_retried_and_cleared_after_unlock` —— 待办入册 → 解锁后重连补建 → 清空
  5. `test_normal_open_never_registers_pending_build` —— 无竞争不得留待办（ISS-168 不退化）
  6. `test_non_lock_errors_are_not_swallowed_as_deferral` —— fail-closed：非竞争失败原样上抛且不登记
  7. `test_migration_path_still_builds_index_under_write_lock` —— 写事务路径 strict：迁移不认让步

另有 1 条 autouse fixture `_reset_pending_index_registry`：待办登记簿是进程内全局态，逐例隔离以免跨例污染/顺序依赖。

## 5. 约束达成

- ✅ `fathom/api.py`、前端、既有迁移测试语义**零改动**（diff 仅 `fathom/db.py` + `tests/test_trend_path_index.py`）
- ✅ `SCHEMA_VERSION` 保持 8，纯索引语义不变
- ✅ 中文提交、无 AI 署名、已 commit、未 push（PM 代提交本文件）

## 6. ARCHITECTURE 备注（**PM 收口时请同步**）

1. **写锁让步是常态路径，不是错误路径**：建索引需写锁，与扫描写事务天然互斥。取舍为「打开永不因索引失败」——索引可能在长写事务期间缺席，trend path 查询退回全表扫描（100 万档 252ms 级），但**不会**把后台补建变成用户可见的打开失败。解锁后的下一次连接自动补齐。
2. **失败诊断**：每次竞争 episode 记一条 `fathom.db` warning（索引名 + 原始错误），不会因反复连接刷屏。
3. **空间开销**：百万档 entries 建 `idx_entries_path`，上游终审实测库增约 **93MB**；本机同档（1,000,000 行、路径形如 `/root/d000/f000000.bin`）复测净增 **26.6MB**（主库 32.7MB → 59.2MB，建索引 0.90s）。两者差异来自路径长度与行数档位，不是本卡引入的变化。用户可通过 `DROP INDEX idx_entries_path` 回收，代价是 trend path 查询退回全表扫描（100 万档 252ms 级）。
   > 测量坑位提示：`DROP INDEX` 只把页归还 freelist、**不缩文件**，故「先建后删再重建」量不出净增；必须从**从未建过索引**的库量，且两侧都 `wal_checkpoint(TRUNCATE)` 后再取文件大小。本卡 RESULT 初稿曾因此误记为「+19MB」，已按干净法复核为 26.6MB。
4. **遗留边界（非本卡范围）**：若库仍处于 rollback journal 模式且被写事务锁住，连 `PRAGMA user_version` 这类**只读**也会被阻塞并等满 10s。该行为属既有连接层语义，不由索引补建引入；生产库在每次打开时都会被设为 WAL，故实际暴露面很小。